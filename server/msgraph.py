"""Microsoft 365 mail via the Graph API, device-code OAuth.

Why not IMAP: Exchange Online disabled basic authentication (which app
passwords ride on) for IMAP tenant-wide in 2023 — the "does the tenant
allow IMAP app passwords" answer is no, nobody's does anymore. The
deterministic replacement is Graph with a one-time device-code login:
the user enters a short code at microsoft.com/devicelogin, Vira stores the
refresh token in the Keychain (service vira-mail-graph) and the mail
watcher polls /me/messages the same way it polls IMAP INBOXes.

Requires a public-client app registration. Config > Connect mail guides
registration, saves its identifiers, and starts device login. Registration
identifiers are local configuration; refresh tokens stay in the secrets store.
"""
import json
import re
import uuid
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import channels, jsonstore, secrets, settings

SCOPE ="https://graph.microsoft.com/Mail.ReadWrite offline_access"
# device login asks for calendar too (brief v2); token refreshes keep
# requesting only the scope they need, so a pre-calendar refresh token
# keeps working for mail and simply can't mint a calendar token until
# the user reconnects once.
SCOPE_CAL = "https://graph.microsoft.com/Calendars.Read offline_access"
SCOPE_LOGIN = ("https://graph.microsoft.com/Mail.ReadWrite "
               "https://graph.microsoft.com/Calendars.Read offline_access")
GRAPH = "https://graph.microsoft.com/v1.0"
KEYCHAIN_SERVICE = "vira-mail-graph"   # namespaced per instance by settings.keychain_service
_DATA = Path(__file__).resolve().parent.parent / "data"
ACCOUNTS = _DATA / "mail-accounts.json"
CONFIG = _DATA / "config.json"
# Provision the publisher-owned, multitenant public client before setting this.
# Never fill this with an installation's private app registration.
PUBLISHER_CLIENT_ID = ""
PUBLISHER_TENANT = "common"


def _registration(cfg):
    client_id = str(cfg.get("msgraph_client_id") or "").strip()
    tenant = str(cfg.get("msgraph_tenant") or "organizations").strip()
    if not client_id:
        raise ValueError("Set up the Microsoft app registration in Config > Connect mail first.")
    try:
        client_id = str(uuid.UUID(client_id))
    except ValueError:
        raise ValueError("Application (client) ID must be the UUID from the registration's Overview.") from None
    # Microsoft's authority accepts a tenant UUID, verified domain, or these
    # account-type aliases. Never permit a URL or path in the authority.
    if tenant not in {"organizations", "common", "consumers"}:
        try:
            tenant = str(uuid.UUID(tenant))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", tenant):
                raise ValueError("Directory (tenant) ID must be a UUID, tenant domain, or organizations/common/consumers.") from None
    return client_id, tenant


def _read_registration_config():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RuntimeError("Cannot read saved configuration. Restore a readable config before saving Microsoft registration.") from e
    if not isinstance(cfg, dict):
        raise RuntimeError("Saved configuration must be a JSON object.")
    return cfg


def registration_status():
    cfg = {}
    source = "none"
    try:
        cfg = _read_registration_config()
        if cfg.get("msgraph_client_id"):
            source = "local"
        elif PUBLISHER_CLIENT_ID:
            cfg = {**cfg, "msgraph_client_id": PUBLISHER_CLIENT_ID,
                   "msgraph_tenant": PUBLISHER_TENANT}
            source = "publisher"
        client_id, tenant = _registration(cfg)
        error = None
    except (ValueError, RuntimeError) as e:
        client_id = str(cfg.get("msgraph_client_id") or "")
        tenant = str(cfg.get("msgraph_tenant") or "")
        error = str(e)
    return {"configured": error is None, "source": source, "client_id": client_id,
            "tenant": tenant, "error": error}


_flows = {}    # email -> {user_code, verification_uri, expires_at, error, connected}
_tokens = {}   # (email, scope) -> {access_token, expires_at}
_lock = threading.Lock()
_registration_lock = threading.RLock()


def save_registration(client_id, tenant):
    """Validated native setup; preserve unrelated config and existing tokens."""
    from . import onboard
    client_id, tenant = _registration({"msgraph_client_id": client_id,
                                      "msgraph_tenant": tenant})
    with _registration_lock:
        def validate(cfg):
            _read_registration_config()  # Fail closed under the config store lock.
            old = (str(cfg.get("msgraph_client_id") or "").strip(),
                   str(cfg.get("msgraph_tenant") or "organizations").strip())
            if old == (client_id, tenant):
                return
            if any(f.get("expires_at", 0) > time.time() and not f.get("error")
                   and not f.get("connected") for f in _flows.values()):
                raise RuntimeError("Finish the pending Microsoft sign-in or wait for its code to expire before changing registration.")
            if channels.graph_accounts(ACCOUNTS):
                raise RuntimeError("Remove the Microsoft mailboxes in Config before changing registration, then add them again.")
        onboard.config_set(_validate=validate, msgraph_client_id=client_id,
                           msgraph_tenant=tenant)
    return registration_status()


def _auth():
    """Fresh configuration makes setup take effect without a restart."""
    status = registration_status()
    if not status["configured"]:
        raise ValueError(status["error"])
    return status["client_id"], "https://login.microsoftonline.com/" + status["tenant"]


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def _graph_request(email, path, method="GET", payload=None, scope=SCOPE,
                   headers=None):
    tok = _access_token(email, scope)
    req = urllib.request.Request(
        GRAPH + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"authorization": "Bearer " + tok,
                 "content-type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def get_bytes(email, path, scope=SCOPE):
    """Raw bytes of a Graph resource (e.g. an attachment's /$value) — used
    for attachments too large to ride inline as contentBytes."""
    tok = _access_token(email, scope)
    req = urllib.request.Request(
        GRAPH + path,
        headers={"authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


# ---------- keychain ----------

def _stored_refresh_token(email):
    return secrets.get(settings.keychain_service(KEYCHAIN_SERVICE), email) or None


def _store_refresh_token(email, token, *, required=False):
    # The rotating refresh token: losing a write logs the account out, so
    # let the ladder land it in whatever store this machine has. The mac
    # backend keeps the argv-safety pattern (`security -i`, audit P1-1).
    try:
        secrets.set(settings.keychain_service(KEYCHAIN_SERVICE), email, token)
    except RuntimeError:
        if required:
            raise
        # Existing refresh callers retain the best-effort contract.


def connected(email):
    return _stored_refresh_token(email) is not None


# ---------- tokens ----------

def _accept_tokens(email, payload, scope=SCOPE, *, persist_required=False):
    if persist_required and not payload.get("refresh_token"):
        raise RuntimeError("Microsoft did not return a refresh token. Sign in again with offline access enabled.")
    if payload.get("refresh_token"):
        _store_refresh_token(email, payload["refresh_token"], required=persist_required)
    with _lock:
        _tokens[(email, scope)] = {
            "access_token": payload["access_token"],
            "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 120,
        }


def _access_token(email, scope=SCOPE):
    with _lock:
        tok = _tokens.get((email, scope))
        if tok and tok["expires_at"] > time.time():
            return tok["access_token"]
    refresh = _stored_refresh_token(email)
    if not refresh:
        raise RuntimeError("not connected — run the device login in settings")
    client_id, authority = _auth()
    payload = _post_form(authority + "/oauth2/v2.0/token", {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": scope,
    })
    if "access_token" not in payload:
        raise RuntimeError("token refresh failed: "
                           + payload.get("error_description", "")[:200])
    _accept_tokens(email, payload, scope)
    return payload["access_token"]


# ---------- device-code flow ----------

def _spawn_device_poll(*args):
    threading.Thread(target=_poll_for_token, args=args, daemon=True,
                     name="vira-graph-devicecode").start()


def start_device_flow(email):
    email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a mailbox email address before signing in.")
    with _registration_lock:
        client_id, authority = _auth()
        payload = _post_form(authority + "/oauth2/v2.0/devicecode", {
            "client_id": client_id, "scope": SCOPE_LOGIN,
        })
        if "device_code" not in payload:
            raise RuntimeError(_login_error(payload))
        flow = {
            "user_code": payload["user_code"],
            "verification_uri": payload.get("verification_uri",
                                            "https://microsoft.com/devicelogin"),
            "expires_at": time.time() + int(payload.get("expires_in", 900)),
            "error": None, "connected": False,
        }
        _flows[email] = flow
        _spawn_device_poll(email, payload["device_code"],
                           max(1, int(payload.get("interval", 5))), flow,
                           client_id, authority)
    return {"user_code": flow["user_code"],
            "verification_uri": flow["verification_uri"],
            "expires_in": int(payload.get("expires_in", 900))}


def _login_error(payload):
    detail = payload.get("error_description", payload.get("error", "Microsoft sign-in failed"))
    if "AADSTS7000218" in detail:
        return "Enable Allow public client flows in the registration's Authentication settings, save, and try again."
    if "AADSTS700016" in detail:
        return "Microsoft could not find this app in the selected tenant. Check Application (client) ID and Directory (tenant) ID in Config."
    if "AADSTS50011" in detail:
        return "Add http://localhost/api/mail/graph/browser/callback under Authentication > Mobile and desktop applications in the Microsoft registration, or use device login in Manual setup."
    if "AADSTS65001" in detail or payload.get("error") == "consent_required":
        return "Microsoft requires consent for this app. Ask your tenant administrator to approve delegated Mail.ReadWrite and Calendars.Read, then try again."
    return detail[:300]  # Keep provider diagnostics bounded in the setup card.


def _poll_for_token(email, device_code, interval, flow, client_id, authority):
    # Keep the exact registration that issued this code; a stale flow must
    # never update another sign-in's status or tokens.
    try:
        while time.time() < flow["expires_at"]:
            time.sleep(interval)
            if _flows.get(email) is not flow:
                return
            if time.time() >= flow["expires_at"]:
                break
            payload = _post_form(authority + "/oauth2/v2.0/token", {
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            })
            with _registration_lock:
                if _flows.get(email) is not flow:
                    return
                if "access_token" in payload:
                    _accept_tokens(email, payload, persist_required=True)
                    _ensure_account_entry(email)
                    flow["connected"] = True
                    return
                err = payload.get("error", "")
                if err in ("authorization_pending", "slow_down"):
                    if err == "slow_down":
                        interval += 5
                    continue
                flow["error"] = _login_error(payload)
                return
        flow["error"] = "Device code expired. Sign in again for a new code."
    except Exception as e:  # Surface network/storage failures in the sign-in card.
        flow["error"] = str(e)[:300]  # Same diagnostic bound as provider failures.


def _ensure_account_entry(email):
    """Once connected, register the account so the mail watcher polls it."""
    def add(raw):
        accounts = raw if isinstance(raw, list) else raw.setdefault("accounts", [])
        if not any(a.get("email") == email and a.get("type") == "graph" for a in accounts):
            accounts.append({"email": email, "type": "graph"})
    jsonstore.mutate(ACCOUNTS, add, [], indent=1)


def flow_status(email):
    flow = _flows.get(email, {})
    expired = bool(flow) and not flow.get("connected") and time.time() >= flow.get("expires_at", 0)
    return {
        "connected": bool(flow.get("connected") or (not flow and connected(email))),
        "pending": bool(flow) and not flow.get("connected") and not flow.get("error") and not expired,
        "user_code": flow.get("user_code"),
        "verification_uri": flow.get("verification_uri"),
        "error": flow.get("error") or ("Device code expired. Sign in again for a new code." if expired else None),
    }


# ---------- mail ----------

def fetch_new_messages(email, last_iso, seen_ids=None):
    """Inbox messages newer than the watermark. First run baselines at the
    newest message and emits nothing old (same contract as the IMAP path).

    Watermark gotcha: Graph returns receivedDateTime truncated to whole
    seconds, but Exchange stores it with sub-second precision — so a
    `gt <watermark>` filter re-matches the newest message on EVERY poll
    (22:57:11.489 > 22:57:11) and it echoes into the feed once a minute
    until newer mail arrives. Fix: filter `ge` (so same-second siblings
    are never missed either) and let the caller pass the already-emitted
    message ids, which are excluded here."""
    if last_iso is None:
        return [], channels.graph_newest_received(
            email, "/me/mailFolders/inbox/messages")
    q = ("/me/mailFolders/inbox/messages"
         f"?$filter=receivedDateTime%20ge%20{last_iso}"
         "&$orderby=receivedDateTime%20asc&$top=20"
         "&$select=id,subject,from,receivedDateTime,bodyPreview,internetMessageId")
    raw = _graph_request(email, q).get("value", [])
    watermark = raw[-1]["receivedDateTime"] if raw else last_iso
    seen = set(seen_ids or ())
    return [m for m in raw if m.get("id") not in seen], watermark


def create_draft(email, to, subject, body):
    """Ready-to-send draft in the M365 mailbox."""
    msg = _graph_request(email, "/me/messages", method="POST", payload={
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to}}],
    })
    return {"saved": True, "account": email, "folder": "Drafts",
            "id": msg.get("id")}


# ---------- calendar + drafts (brief v2) ----------

def calendar_events(email, start_iso, end_iso, tz="America/New_York"):
    """Expanded occurrences from the M365 calendar between two local ISO
    datetimes. Raises RuntimeError (e.g. "needs consent") until the account
    has been reconnected once with the calendar scope."""
    try:
        q = ("/me/calendarView"
             f"?startDateTime={urllib.parse.quote(start_iso)}"
             f"&endDateTime={urllib.parse.quote(end_iso)}"
             "&$orderby=start/dateTime&$top=50"
             "&$select=id,subject,start,end,isAllDay,location,organizer,"
             "isOnlineMeeting,webLink,bodyPreview")
        out = _graph_request(email, q, scope=SCOPE_CAL,
                             headers={"Prefer": f'outlook.timezone="{tz}"'})
    except RuntimeError as e:
        if "AADSTS65001" in str(e) or "consent" in str(e).lower():
            raise RuntimeError("needs consent — reconnect M365 in settings")
        raise
    events = []
    for ev in out.get("value", []):
        events.append({
            "id": ev.get("id") or "",
            "title": ev.get("subject") or "(no title)",
            "all_day": bool(ev.get("isAllDay")),
            "start": (ev.get("start") or {}).get("dateTime", "")[:19],
            "end": (ev.get("end") or {}).get("dateTime", "")[:19],
            "online": bool(ev.get("isOnlineMeeting")),
            "location": ((ev.get("location") or {}).get("displayName") or ""),
            "organizer": (((ev.get("organizer") or {}).get("emailAddress")
                            or {}).get("name") or ""),
            "body_preview": ev.get("bodyPreview") or "",
            "web_link": ev.get("webLink") or "",
        })
    return events


def list_drafts(email, limit=10):
    """Drafts sitting in the M365 mailbox, newest first."""
    out = _graph_request(
        email, "/me/mailFolders/drafts/messages"
               f"?$orderby=lastModifiedDateTime%20desc&$top={limit}"
               "&$select=id,subject,toRecipients,lastModifiedDateTime,"
               "webLink,bodyPreview")
    drafts = []
    for d in out.get("value", []):
        tos = [((r.get("emailAddress") or {}).get("name")
                or (r.get("emailAddress") or {}).get("address") or "")
               for r in d.get("toRecipients", [])]
        drafts.append({
            "id": d.get("id") or "",
            "subject": d.get("subject") or "(no subject)",
            "to": ", ".join(t for t in tos if t) or "(no recipient)",
            "modified": d.get("lastModifiedDateTime"),
            "account": email,
            "body_preview": d.get("bodyPreview") or "",
            "web_link": d.get("webLink") or "",
        })
    return drafts

"""Email in the feed: IMAP inbox watcher. Deterministic — polls INBOX for new
messages, joins senders to CRM people, and merges items into the same live
feed as iMessage.

Dormant until an account is configured. Setup (one time, per account):
  1. Store the password in the secrets store (server/secrets.py — the
     macOS Keychain here; Credential Manager on Windows). On a Mac:
       security add-generic-password -a you@yourdomain.com -s vira-mail -w
     (Gmail: use an app password from myaccount.google.com/apppasswords;
      Outlook/M365: an app password if the tenant allows IMAP, else IMAP is
      blocked and that account stays on the connector path.)
  2. Add the account to data/mail-accounts.json:
       [{"email": "you@yourdomain.com", "host": "outlook.office365.com"},
        {"email": "you@gmail.com", "host": "imap.gmail.com"}]
The watcher picks up the file within one poll cycle; no restart needed.
"""
import email
import email.header
import email.message
import email.utils
import imaplib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import channels
from . import data as crm
from . import jsonstore, secrets, settings

ACCOUNTS = channels.ACCOUNTS
STATE = Path(__file__).resolve().parent.parent / "data" / "mail-state.json"
# The watcher's per-account health, written after every poll so a process
# that does not hold the watcher (onboard.status, a CLI) can still say
# which account is failing. Regenerable; the watcher rewrites it each cycle.
HEALTH = Path(__file__).resolve().parent.parent / "data" / "mail-health.json"
# A health entry older than this many poll intervals reads as "stale" —
# the honest word for a snapshot nothing has refreshed (a passive clone,
# a watcher that is down).
STALE_POLLS = 3
PROBE_TIMEOUT_S = 20


def keychain_service():
    return settings.keychain_service("vira-mail")


def keychain_password(account_email):
    return secrets.get(keychain_service(), account_email) or None


def load_accounts():
    """The configured accounts (channels.mail_accounts over this
    module's ACCOUNTS path, which tests patch)."""
    return channels.mail_accounts(ACCOUNTS)


def _save_accounts(accts):
    ACCOUNTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS.with_name(ACCOUNTS.name + ".tmp")
    tmp.write_text(json.dumps(accts, indent=1))
    tmp.replace(ACCOUNTS)


def add_imap_account(account_email, host, password):
    """Wire a Gmail/IMAP mailbox from the app: the password lands in the
    secrets ladder (never the JSON), the {email, host} row in
    mail-accounts.json. The watcher picks it up within one poll — no
    restart. Re-adding the same address updates its host in place."""
    account_email = (account_email or "").strip().lower()
    host = (host or "").strip()
    password = password or ""
    if "@" not in account_email:
        raise ValueError("a valid email address is required")
    if not host:
        raise ValueError("an IMAP host is required (e.g. imap.gmail.com)")
    if not password:
        raise ValueError("a password is required")
    secrets.set(keychain_service(), account_email, password)
    accts = load_accounts()
    for a in accts:
        if (a.get("email") or "").strip().lower() == account_email \
                and a.get("type") != "graph":
            a["host"] = host
            _save_accounts(accts)
            return {"email": account_email, "host": host, "added": False}
    accts.append({"email": account_email, "host": host})
    _save_accounts(accts)
    return {"email": account_email, "host": host, "added": True}



# ---------- account kinds, hosts, and what a failure means ----------

_HOSTS = {
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com", "msn.com": "outlook.office365.com",
    "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "yahoo.com": "imap.mail.yahoo.com", "aol.com": "imap.aol.com",
    "fastmail.com": "imap.fastmail.com",
}
GMAIL_APP_PASSWORDS = "https://myaccount.google.com/apppasswords"


def default_host(account_email):
    """The IMAP host an address almost certainly uses — the consumer
    providers by table, anything else the conventional imap.<domain>. A
    suggestion for the form to prefill, never a fact the probe skips."""
    addr = (account_email or "").strip().lower()
    if "@" not in addr:
        return ""
    domain = addr.rsplit("@", 1)[1]
    return _HOSTS.get(domain) or ("imap." + domain if domain else "")


def account_kind(acct):
    """graph | gmail | imap — what the row IS, for the card's glyph, its
    label and the fix text a failure carries."""
    if acct.get("type") == "graph":
        return "graph"
    host = (acct.get("host") or "").lower()
    addr = (acct.get("email") or "").lower()
    if "gmail" in host or addr.endswith(("@gmail.com", "@googlemail.com")):
        return "gmail"
    return "imap"


KIND_LABEL = {"graph": "Microsoft 365", "gmail": "Gmail", "imap": "IMAP"}

_AUTH_RE = re.compile(
    r"authenticat|invalid credentials|login failed|invalid_grant|aadsts|"
    r"token (?:has )?expired|refresh token|unauthori[sz]ed|\[authenticationfailed\]|"
    r"application-specific password|app password", re.I)
_NET_RE = re.compile(
    r"timed? ?out|getaddrinfo|nodename nor servname|name or service not known|"
    r"connection refused|unreachable|network is|ssl|eof occurred|"
    r"connection reset|no route to host|temporarily unavailable", re.I)


def classify(status_text, kind="imap"):
    """The watcher's raw status string read as a state the owner can act
    on. `fix` is the plain-English next step for THIS kind of account, so
    the card never shows a bare IMAP error code as its whole explanation.
    The raw text still rides along as `detail` — it is the evidence."""
    raw = (status_text or "").strip()
    low = raw.lower()
    label = KIND_LABEL.get(kind, "IMAP")
    if not raw:
        return {"state": "unknown", "title": "Not checked yet",
                "fix": "Vira checks every mailbox about once a minute.",
                "detail": ""}
    if low == "ok":
        return {"state": "ok", "title": "Connected", "fix": "", "detail": ""}
    if low.startswith("no password"):
        return {"state": "no_password", "title": "No password on file",
                "fix": f"Enter the {label} password below to connect this "
                       "mailbox.", "detail": raw}
    if kind == "graph" and low.startswith("not connected"):
        return {"state": "not_connected", "title": "Not signed in",
                "fix": "Sign in again with a one-time device login.",
                "detail": raw}
    if _AUTH_RE.search(low):
        if kind == "gmail":
            fix = ("Gmail rejected the app password. Google revokes app "
                   "passwords when you change your Google password or turn "
                   "off 2-step verification. Make a new one and reconnect.")
        elif kind == "graph":
            fix = ("Microsoft no longer accepts the saved sign-in. Reconnect "
                   "with a fresh device login — no password is stored.")
        else:
            fix = ("The server rejected the sign-in. Enter the current "
                   "password to reconnect.")
        return {"state": "auth", "title": "Sign-in rejected", "fix": fix,
                "detail": raw}
    if _NET_RE.search(low):
        return {"state": "network", "title": "Could not reach the server",
                "fix": "Usually transient. If it persists, check the host "
                       "name and that this machine is online.",
                "detail": raw}
    return {"state": "error", "title": "Polling failed",
            "fix": "Check now to retry; reconnect if it keeps failing.",
            "detail": raw}


def probe_imap(account_email, host, password):
    """One real IMAP login, then logout. Returns {ok: True, host} or
    {ok: False, state, title, fix, detail} — the classified failure, never
    an exception, so the form can render what went wrong before anything
    is saved. Writes nothing."""
    addr = (account_email or "").strip().lower()
    host = (host or "").strip() or default_host(addr)
    if "@" not in addr:
        return {"ok": False, **classify("invalid address", "imap"),
                "title": "Enter an email address", "state": "error"}
    if not host:
        return {"ok": False, "state": "error", "title": "Enter the IMAP host",
                "fix": "Gmail is imap.gmail.com.", "detail": ""}
    if not password:
        return {"ok": False, "state": "no_password",
                "title": "Enter the password", "fix": "", "detail": ""}
    kind = account_kind({"email": addr, "host": host})
    try:
        con = imaplib.IMAP4_SSL(host, timeout=PROBE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — network failures are the point
        return {"ok": False, **classify(str(e)[:200] or "connect failed", kind)}
    try:
        con.login(addr, password)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, **classify(str(e)[:200] or "login failed", kind)}
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "host": host, "kind": kind}


def reconnect_imap(account_email, password, host=None, verify=True):
    """Replace the password on file for an EXISTING IMAP account — the
    answer to a mailbox that stopped signing in. The watermark in
    mail-state.json is kept, so nothing already seen is re-emitted into
    the feed. With verify (the default) the new password is tried against
    the server FIRST and a rejected one is never stored: saving a bad
    password would only move the failure one poll later."""
    addr = (account_email or "").strip().lower()
    accts = load_accounts()
    row = next((a for a in accts
                if (a.get("email") or "").strip().lower() == addr
                and a.get("type") != "graph"), None)
    if row is None:
        raise ValueError(f"{addr or 'that address'} is not a connected "
                         "IMAP mailbox — add it instead")
    if not password:
        raise ValueError("a password is required")
    host = (host or "").strip() or row.get("host") or default_host(addr)
    if verify:
        r = probe_imap(addr, host, password)
        if not r["ok"]:
            raise ValueError(f"{r['title']}: {r['fix']} ({r['detail']})"
                             if r.get("detail") else f"{r['title']}. {r['fix']}")
    secrets.set(keychain_service(), addr, password)
    if row.get("host") != host:
        row["host"] = host
        _save_accounts(accts)
    return {"email": addr, "host": host, "verified": bool(verify)}


def remove_account(account_email, kind=None):
    """Take a mailbox out of Vira: the row, its secret (the IMAP password
    or the Graph refresh token), its watermark and its health entry.
    `kind` narrows to graph or imap when one address carries both. Returns
    how many rows went; zero is not an error."""
    from . import msgraph
    addr = (account_email or "").strip().lower()
    accts = load_accounts()
    keep, gone = [], []
    for a in accts:
        same = (a.get("email") or "").strip().lower() == addr
        is_graph = a.get("type") == "graph"
        if same and (kind is None or (kind == "graph") == is_graph):
            gone.append(a)
        else:
            keep.append(a)
    if not gone:
        return {"removed": 0, "email": addr}
    _save_accounts(keep)
    for a in gone:
        if a.get("type") == "graph":
            secrets.delete(settings.keychain_service(msgraph.KEYCHAIN_SERVICE), addr)
        else:
            secrets.delete(keychain_service(), addr)
    state = jsonstore.read(STATE, {})
    for k in (addr, "graph:" + addr, "graph_seen:" + addr):
        if kind is None or (k == addr) == (kind != "graph"):
            state.pop(k, None)
    jsonstore.write_atomic(STATE, state)
    health = jsonstore.read(HEALTH, {})
    for a in gone:
        health.pop(_health_key(a), None)
    jsonstore.write_atomic(HEALTH, health, indent=1)
    return {"removed": len(gone), "email": addr}


def _health_key(acct):
    # One address can carry an IMAP row AND a Graph row; health is per row.
    return (("graph:" if acct.get("type") == "graph" else "")
            + (acct.get("email") or "").strip().lower())


def health_snapshot():
    """The last health the watcher wrote, for a process that does not hold
    it (onboard.status reads this to say a mail row needs attention)."""
    return jsonstore.read(HEALTH, {})


def summary(health=None):
    """{accounts, ok, failing, attention} over the configured rows — the
    one sentence a Config row needs. A row with no health yet is neither
    ok nor failing."""
    health = health if health is not None else health_snapshot()
    accts = load_accounts()
    ok = failing = 0
    names = []
    for a in accts:
        h = health.get(_health_key(a)) or {}
        st = h.get("state")
        if st == "ok":
            ok += 1
        elif st in ("auth", "no_password", "not_connected", "error", "network"):
            failing += 1
            names.append(a.get("email") or "")
    attention = ""
    if failing == 1:
        attention = f"{names[0]} needs attention"
    elif failing > 1:
        attention = f"{failing} accounts need attention"
    return {"accounts": len(accts), "ok": ok, "failing": failing,
            "attention": attention}


def accounts_view(live_health=None, poll_seconds=60):
    """Every configured mailbox with its kind, host and classified health
    — what the Config mail card renders. Live watcher health outranks the
    snapshot; a snapshot older than STALE_POLLS polls is marked stale
    rather than passed off as current."""
    from . import msgraph
    snap = health_snapshot()
    now = time.time()
    rows = []
    for a in load_accounts():
        key = _health_key(a)
        h = (live_health or {}).get(key) or snap.get(key) or {}
        kind = account_kind(a)
        cls = classify(h.get("status", ""), kind)
        checked = h.get("checked_at")
        stale = bool(checked) and (now - checked) > STALE_POLLS * poll_seconds
        if not checked:
            cls = classify("", kind)
        row = {
            "email": (a.get("email") or "").strip().lower(),
            "kind": kind, "label": KIND_LABEL[kind],
            "host": a.get("host") or ("graph.microsoft.com" if kind == "graph" else ""),
            "state": cls["state"], "title": cls["title"], "fix": cls["fix"],
            "detail": cls["detail"], "checked_at": checked,
            "ok_at": h.get("ok_at"), "fail_since": h.get("fail_since"),
            "stale": stale, "key": key,
        }
        if kind == "graph":
            row["signed_in"] = msgraph.connected(row["email"])
            if not row["signed_in"] and row["state"] in ("ok", "unknown"):
                row.update(classify("not connected", "graph"))
        if kind == "gmail":
            row["help_url"] = GMAIL_APP_PASSWORDS
        rows.append(row)
    return {"accounts": rows, "summary": summary(
        {r["key"]: {"state": r["state"]} for r in rows}),
        "poll_seconds": poll_seconds}


def _decode_header(raw):
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            # errors="replace" only helps for a KNOWN codec; a header
            # advertising a codec Python has no name for ("unknown-8bit"
            # in the wild) raises LookupError before errors= applies —
            # one such Subject wedged the 2026-07-28 backlog walk.
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("latin-1", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def strip_html(text):
    """HTML mail body -> readable plain text. Shared with the body index
    (textindex), which gets its HTML from Graph rather than MIME."""
    text = re.sub(r"<style.*?</style>|<script.*?</script>", " ", text or "",
                  flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", text)


def _body_preview(msg, limit=400):
    """Plain-text preview of the message body."""
    part = msg
    if msg.is_multipart():
        part = next((p for p in msg.walk()
                     if p.get_content_type() == "text/plain"
                     and "attachment" not in str(p.get("Content-Disposition", ""))),
                    None)
        if part is None:
            part = next((p for p in msg.walk()
                         if p.get_content_type() == "text/html"), None)
        if part is None:
            return ""
    try:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8",
                              errors="replace")
    except Exception:  # noqa: BLE001 — malformed MIME; skip preview
        return ""
    if part.get_content_type() == "text/html":
        text = strip_html(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _drafts_folder(con):
    """Find the mailbox flagged \\Drafts (RFC 6154); Gmail's is [Gmail]/Drafts."""
    return channels.imap_special_folder(con, "\\Drafts", "Drafts")


def create_draft(account, to, subject, body, in_reply_to=None, references=None):
    """Ready-to-send draft in the account's Drafts folder. Gmail/IMAP path is
    an APPEND with the \\Draft flag (shows up everywhere Gmail does); Graph
    accounts go through the Graph API."""
    accounts = channels.mail_accounts(ACCOUNTS)
    acct = next((a for a in accounts if a["email"] == account), None) \
        or (accounts[0] if accounts else None)
    if not acct:
        raise RuntimeError("no mail account configured")
    addr = acct["email"]
    if acct.get("type") == "graph":
        from . import msgraph
        return msgraph.create_draft(addr, to, subject, body)

    password = keychain_password(addr)
    if not password:
        raise RuntimeError(f"no password in keychain for {addr} "
                           f"(service {keychain_service()})")
    msg = email.message.EmailMessage()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = ((references + " ") if references else "") + in_reply_to
    msg.set_content(body)
    con = imaplib.IMAP4_SSL(acct["host"], timeout=30)
    try:
        con.login(addr, password)
        folder = _drafts_folder(con)
        status, data = con.append(
            f'"{folder}"', r"(\Draft)",
            imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if status != "OK":
            raise RuntimeError(f"IMAP append failed: {data}")
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001
            pass
    return {"saved": True, "account": addr, "folder": folder}


class MailWatcher:
    """Polls each configured account's INBOX for messages newer than the last
    seen UID. Pushes feed items shaped like the iMessage ones (channel=email)."""

    def __init__(self, imessage_watcher, poll_seconds=60):
        self.watcher = imessage_watcher   # shared feed + listeners
        self.poll = poll_seconds
        self.state = {}
        self.status = {}                  # email -> ok | error text | "no password"
        # per-row health: {status, state, checked_at, ok_at, fail_since}
        self.health = {}
        self._stop = threading.Event()
        self._lock = threading.RLock()    # one poll of one account at a time

    def accounts(self):
        return channels.mail_accounts(ACCOUNTS)

    def _load_state(self):
        try:
            self.state = json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            self.state = {}

    def _save_state(self):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.state))

    def start(self):
        self._load_state()
        threading.Thread(target=self._run, daemon=True, name="vira-mail").start()

    def _run(self):
        while not self._stop.is_set():
            for acct in self.accounts():
                self._poll_one(acct)
            self._write_health()
            self._stop.wait(self.poll)

    def _poll_one(self, acct):
        """Poll one account and record what happened. The status string
        keeps its shape (the feed's filter chips read it); the health
        entry carries the classified state and the clock."""
        addr = acct["email"]
        with self._lock:
            # _poll_account names the two states it can see itself (no
            # password / not connected) and otherwise says nothing, so the
            # slot is cleared first: a clean return over a stale error
            # text must read as ok, not as last cycle's failure.
            self.status.pop(addr, None)
            try:
                self._poll_account(acct)
                self.status[addr] = self.status.get(addr) or "ok"
            except Exception as e:  # noqa: BLE001 — keep polling others
                self.status[addr] = str(e)[:200] or "error"
            return self._record(acct, self.status[addr])

    def _record(self, acct, text):
        now = time.time()
        key = _health_key(acct)
        prev = self.health.get(key) or {}
        cls = classify(text, account_kind(acct))
        entry = {"status": text, "state": cls["state"], "checked_at": now,
                 "ok_at": now if cls["state"] == "ok" else prev.get("ok_at"),
                 "fail_since": None if cls["state"] == "ok"
                 else (prev.get("fail_since") or now)}
        self.health[key] = entry
        return entry

    def _write_health(self):
        try:
            jsonstore.write_atomic(HEALTH, self.health, indent=1)
        except OSError:
            pass  # a snapshot nobody could write is not a reason to stop polling

    def check_account(self, account_email):
        """Poll ONE account right now (the card's Check now / the moment
        after a reconnect) and return its fresh health entry. Same code
        path as the loop, so the answer is what the loop would say."""
        addr = (account_email or "").strip().lower()
        if not self.state:
            self._load_state()
        hits = [a for a in self.accounts()
                if (a.get("email") or "").strip().lower() == addr]
        if not hits:
            raise ValueError(f"{addr or 'that address'} is not a connected mailbox")
        out = None
        for acct in hits:
            out = self._poll_one(acct)
        self._write_health()
        return out

    def _poll_account(self, acct):
        if acct.get("type") == "graph":
            self._poll_graph(acct)
            return
        addr, host = acct["email"], acct["host"]
        password = keychain_password(addr)
        if not password:
            self.status[addr] = f"no password in keychain (service {keychain_service()})"
            return
        # timeout, not default-None: a stalled read otherwise wedges the
        # watcher thread forever (the textindex Indexer incident)
        con = imaplib.IMAP4_SSL(host, timeout=30)
        try:
            con.login(addr, password)
            con.select("INBOX", readonly=True)
            last_uid, baselined = channels.first_run_baseline(
                self.state.get(addr),
                lambda: channels.imap_newest_uid(con, "INBOX"))
            if baselined:
                # first run: baseline at the newest message, emit nothing old
                self.state[addr] = last_uid
                self._save_state()
                return
            _, data = con.uid("search", None, f"UID {last_uid + 1}:*")
            uids = [int(u) for u in data[0].split() if int(u) > last_uid]
            for uid in uids[:20]:
                _, msgdata = con.uid("fetch", str(uid), "(RFC822)")
                if not msgdata or msgdata[0] is None:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                self._emit(addr, uid, msg)
                self.state[addr] = uid
            if uids:
                self._save_state()
        finally:
            try:
                con.logout()
            except Exception:  # noqa: BLE001
                pass

    def _poll_graph(self, acct):
        from . import msgraph
        addr = acct["email"]
        if not msgraph.connected(addr):
            self.status[addr] = "not connected — connect Microsoft 365 in settings"
            return
        last = self.state.get("graph:" + addr)
        seen = list(self.state.get("graph_seen:" + addr) or [])
        msgs, watermark = msgraph.fetch_new_messages(addr, last, seen)
        for m in msgs:
            self._emit_graph(addr, m)
            if m.get("id"):
                seen.append(m["id"])
        if msgs or watermark != last:
            self.state["graph:" + addr] = watermark
            self.state["graph_seen:" + addr] = seen[-80:]
            self._save_state()

    def _emit_graph(self, account, m):
        sender = (m.get("from") or {}).get("emailAddress") or {}
        sender_addr = (sender.get("address") or "").lower()
        subject = m.get("subject") or ""
        preview = re.sub(r"\s+", " ", m.get("bodyPreview") or "").strip()
        self._push_item(
            account=account,
            rowid=f"mail-{account}-{m.get('id', '')[-24:]}",
            when=m.get("receivedDateTime"),
            sender_addr=sender_addr,
            sender_name=sender.get("name") or "",
            subject=subject,
            preview=preview,
            message_id=m.get("internetMessageId"),
        )

    def _emit(self, account, uid, msg):
        sender_name, sender_addr = email.utils.parseaddr(msg.get("From", ""))
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
            when = dt.astimezone().isoformat()
        except (TypeError, ValueError):
            when = datetime.now(timezone.utc).astimezone().isoformat()
        self._push_item(
            account=account,
            rowid=f"mail-{account}-{uid}",
            when=when,
            sender_addr=(sender_addr or "").lower(),
            sender_name=_decode_header(sender_name),
            subject=_decode_header(msg.get("Subject", "")),
            preview=_body_preview(msg),
            message_id=(msg.get("Message-ID") or "").strip() or None,
        )

    def _push_item(self, account, rowid, when, sender_addr, sender_name,
                   subject, preview, message_id):
        from . import photos
        pid = crm.resolve_handle(sender_addr)
        person = crm._load()["by_id"].get(pid) if pid else None
        item = {
            "rowid": rowid,
            "when": when,
            "channel": "email",
            "account": account,
            "subject": subject,
            "message_id": message_id,
            "text": (subject + " — " + preview).strip(" —")[:500],
            "handle": sender_addr,
            "group": False,
            "group_name": None,
            "person_id": pid,
            "person_name": person["name"] if person else (
                sender_name or sender_addr),
            "known": pid is not None,
            "has_photo": bool(pid and photos.photo_path(pid)),
        }
        if not channels.push_feed_item(self.watcher, item):
            return                    # refetch echo — already in the feed
        from . import notify
        notify.maybe_notify(item)  # high-value senders ping the owner's phone

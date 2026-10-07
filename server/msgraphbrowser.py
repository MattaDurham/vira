"""Local-browser Microsoft sign-in: authorization code with PKCE.

The fragment callback keeps codes out of server/access logs. A separate
HttpOnly cookie binds it to the browser that started sign-in; state alone
is not browser authentication. Phone/LAN clients use the device flow.
"""
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import time
import urllib.parse
import urllib.request

from . import msgraph, systembrowser

CALLBACK = "/api/mail/graph/browser/callback"
# Ten minutes covers interactive sign-in without retaining unused verifiers.
TTL = 600
SCOPE = msgraph.SCOPE_LOGIN + " https://graph.microsoft.com/User.Read"
# A launch ticket only bridges browsers; it never carries a Microsoft token.
LAUNCH_TTL = 120
_launches = {}
_bindings = {}


def origin(request):
    if request.url.hostname != "localhost" or request.url.scheme not in {"http", "https"}:
        raise ValueError("For browser sign-in, open Vira on this computer at localhost. On a phone or another computer, use device login in Advanced setup.")
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid local Vira port.")
    suffix = "" if port == (443 if request.url.scheme == "https" else 80) else f":{port}"
    return f"{request.url.scheme}://localhost{suffix}"


def cookie_name(request):
    # Cookies ignore ports; separate preview and live sign-ins explicitly.
    return "vira-msgraph-" + str(request.url.port or (443 if request.url.scheme == "https" else 80))


def check_origin(request):
    if request.headers.get("origin") != origin(request):
        raise ValueError("Start Microsoft sign-in from this Vira window.")


def check_local(request):
    check_origin(request)
    host = request.client.host if request.client else ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ValueError("Open Vira at localhost on the computer running it to launch the system browser. Remote clients can use device login in Advanced setup.")


def _key(cookie):
    return _bindings.get(cookie, "browser:" + str(cookie or ""))


def _prune():
    for ticket, launch in list(_launches.items()):
        if launch["expires_at"] <= time.time() or launch["key"] not in msgraph._flows:
            del _launches[ticket]
    for cookie, key in list(_bindings.items()):
        if key not in msgraph._flows:
            del _bindings[cookie]


def start(request):
    check_origin(request)
    with msgraph._registration_lock:
        client_id, authority = msgraph._auth()
        old = _key(request.cookies.get(cookie_name(request)))
        previous = msgraph._flows.get(old)
        if previous and previous.get("redeeming"):
            raise ValueError("Microsoft sign-in is finishing. Wait for this connection before trying again.")
        msgraph._flows.pop(old, None)
        # Remove expired browser secrets; device pollers own their own flows.
        for key, flow in list(msgraph._flows.items()):
            if key.startswith("browser:") and flow["expires_at"] <= time.time() and not flow.get("redeeming"):
                del msgraph._flows[key]
        _prune()
        cookie, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        redirect = origin(request) + CALLBACK
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        msgraph._flows[_key(cookie)] = {
            "state": state, "verifier": verifier, "redirect": redirect,
            "client_id": client_id, "authority": authority,
            "expires_at": time.time() + TTL, "connected": False, "error": None,
        }
    params = {"client_id": client_id, "response_type": "code",
              "redirect_uri": redirect, "response_mode": "fragment",
              "scope": SCOPE, "state": state, "code_challenge": challenge,
              "code_challenge_method": "S256"}
    return cookie, {"authorize_url": authority + "/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params)}


def open_system(request):
    check_local(request)
    cookie, body = start(request)
    ticket = secrets.token_urlsafe(32)
    with msgraph._registration_lock:
        key = _key(cookie)
        msgraph._flows[key].update(system_launch=True, browser_ready=False,
                                  expires_at=time.time() + LAUNCH_TTL)
        _launches[ticket] = {"key": key, "origin": origin(request),
                             "expires_at": time.time() + LAUNCH_TTL,
                             "authorize_url": body["authorize_url"]}
    try:
        systembrowser.open_url(origin(request) + "/api/mail/graph/browser/launch#ticket=" + ticket)
    except Exception:
        with msgraph._registration_lock:
            _launches.pop(ticket, None)
            msgraph._flows.pop(key, None)
        raise
    return cookie, {"opened": True}


def claim_launch(request, ticket):
    check_local(request)
    with msgraph._registration_lock:
        _prune()
        launch = _launches.get(ticket)
        if (not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket)
                or not launch or launch["origin"] != origin(request)):
            raise ValueError("This browser launch is invalid or expired. Return to Vira and click Connect Microsoft again.")
        flow = msgraph._flows.get(launch["key"])
        if not flow or flow.get("consumed") or time.time() >= flow["expires_at"]:
            raise ValueError("Microsoft sign-in expired. Return to Vira and connect again.")
        del _launches[ticket]
        cookie = secrets.token_urlsafe(32)
        _bindings[cookie] = launch["key"]
        flow.update(browser_ready=True, expires_at=time.time() + TTL)
        return cookie, {"authorize_url": launch["authorize_url"]}


def _profile(access_token):
    req = urllib.request.Request(msgraph.GRAPH + "/me?$select=mail,userPrincipalName",
                                 headers={"authorization": "Bearer " + access_token})
    with urllib.request.urlopen(req, timeout=30) as response:
        profile = json.loads(response.read())
    email = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise RuntimeError("Microsoft did not return a mailbox address. Use device login with your mailbox email in Advanced setup.")
    return email


def status(cookie):
    with msgraph._registration_lock:
        flow = msgraph._flows.get(_key(cookie))
        if not flow:
            return {"pending": False, "connected": False, "error": "Sign-in session missing. Connect Microsoft again."}
        expired = time.time() >= flow["expires_at"]
        return {"pending": not flow.get("connected") and not flow.get("error") and not expired,
                "connected": bool(flow.get("connected")), "email": flow.get("email"),
                "browser_ready": flow.get("browser_ready", True),
                "error": flow.get("error") or (("The system browser did not start sign-in. Check your default browser and click Connect Microsoft again."
                    if flow.get("system_launch") and not flow.get("browser_ready")
                    else "Microsoft sign-in expired. Connect Microsoft again.") if expired and not flow.get("connected") else None)}


def complete(request, state, code="", error="", error_description=""):
    check_origin(request)
    key = _key(request.cookies.get(cookie_name(request)))
    with msgraph._registration_lock:
        flow = msgraph._flows.get(key)
        if (not flow or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
                or not secrets.compare_digest(flow.get("state", ""), state)
                or flow.get("redeeming") or flow.get("consumed") or time.time() >= flow["expires_at"]):
            raise ValueError("Microsoft sign-in session is invalid or expired. Connect Microsoft again from the original Vira window.")
        flow["consumed"] = True
        verifier = flow.pop("verifier")
        flow.pop("state")
        flow["redeeming"] = True
    try:
        if error:
            raise RuntimeError(msgraph._login_error({"error": error, "error_description": error_description or error}))
        if not code:
            raise RuntimeError("Microsoft returned no sign-in code. Connect Microsoft again.")
        payload = msgraph._post_form(flow["authority"] + "/oauth2/v2.0/token", {
            "client_id": flow["client_id"], "grant_type": "authorization_code",
            "code": code, "redirect_uri": flow["redirect"],
            "code_verifier": verifier, "scope": SCOPE,
        })
        if "access_token" not in payload:
            raise RuntimeError(msgraph._login_error(payload))
        email = _profile(payload["access_token"])
        with msgraph._registration_lock:
            if msgraph._flows.get(key) is not flow or time.time() >= flow["expires_at"]:
                raise RuntimeError("Microsoft sign-in expired while finishing. Connect again.")
            msgraph._accept_tokens(email, payload, persist_required=True)
            msgraph._ensure_account_entry(email)
            flow.update(connected=True, email=email)
    except Exception as exc:  # Never return token-response bodies or HTTP headers.
        flow["error"] = (str(exc)[:300] if isinstance(exc, RuntimeError)
                         else "Could not finish Microsoft sign-in. Check the connection and try again.")
    finally:
        flow["redeeming"] = False
    return status(request.cookies.get(cookie_name(request)))

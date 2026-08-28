"""Push notifications on high-value inbound, delivered over iMessage.

Channel decision (2026-07-07): Vira texts the owner via Messages.app — the
same AppleScript path as /api/send — instead of ntfy/APNs/Tailscale Serve.
Zero new infrastructure, lands on every Apple device, and the send path is
already proven. Inbound iMessages are deliberately NOT notified (the phone
already surfaces those natively); the gap this closes is email — mail is
only seen when the inbox is open, so a note from an active-tier contact
can sit for hours.

Rule (deterministic): notify when an inbound email's sender resolves to a
CRM person whose tier is "active". Throttles: one notification per sender
per 6h window, max 20/day, so a busy thread can't storm the phone.

Config lives in data/config.json (notify_enabled, notify_handle) and is
editable from the settings sheet. Dormant until notify_handle is set —
use your own iMessage self-thread number (mind carrier quirks: a handle
that exists only as a message-less RCS row can time out AppleScript
sends; use the handle your self-thread actually lives on). State + a
rolling log live in data/notify-log.json (surfaced in the Jobs window).
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import data as crm

_DATA = Path(__file__).resolve().parent.parent / "data"
CONFIG = _DATA / "config.json"
LOG = _DATA / "notify-log.json"

SENDER_COOLDOWN = 6 * 3600
DAILY_CAP = 20

# Every Vira-originated message in the self-thread MUST start with this.
# It is half of the reply channel's echo filter (server/inbound.py): the
# thread loops back — a message sent to your own number lands AGAIN as
# is_from_me=0 — and chat.db carries no column that separates Vira's send
# from the owner's (measured 2026-08-28: service, account, is_sent,
# destination_caller_id and every other candidate are byte-identical
# across the two). Text is the only discriminator there is.
VIRA_PREFIX = "Vira: "

# Conversational replies are not notifications: they answer something the
# owner just said, so they bypass the sender cooldown and the notification
# cap and carry their own runaway backstop instead.
REPLY_DAILY_CAP = 60

_lock = threading.Lock()


def config():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    return {
        "enabled": bool(cfg.get("notify_enabled", True)),
        "handle": cfg.get("notify_handle") or "",  # empty = dormant
        "tier": cfg.get("notify_tier", "active"),
    }


def save_config(updates):
    with _lock:
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
        if "enabled" in updates:
            cfg["notify_enabled"] = bool(updates["enabled"])
        if "handle" in updates and updates["handle"] is not None:
            cfg["notify_handle"] = str(updates["handle"]).strip()
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return config()


def _load_log():
    try:
        return json.loads(LOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sent": []}


def _save_log(log):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(log, indent=1), encoding="utf-8")


def recent(limit=40):
    return list(reversed(_load_log().get("sent", [])))[:limit]


def _record(entry):
    with _lock:
        log = _load_log()
        log.setdefault("sent", []).append(entry)
        log["sent"] = log["sent"][-200:]
        _save_log(log)


def _throttled(person_id):
    now = time.time()
    sent = _load_log().get("sent", [])
    today = datetime.now().date().isoformat()
    ok_today = [e for e in sent if e.get("ok") and
                (e.get("at") or "").startswith(today)]
    if len(ok_today) >= DAILY_CAP:
        return "daily cap reached"
    for e in reversed(sent):
        if e.get("person_id") == person_id and e.get("ok"):
            try:
                at = datetime.fromisoformat(e["at"]).timestamp()
            except (KeyError, ValueError):
                continue
            if now - at < SENDER_COOLDOWN:
                return "sender cooldown"
            break
    return None


def _companion_paired():
    """A paired Android companion device is a delivery channel of its own
    — pings work even where iMessage cannot (a Windows hub, no
    notify_handle configured)."""
    try:
        from . import companion
        return companion.has_devices()
    except Exception:  # noqa: BLE001 — never let ping plumbing break notify
        return False


def maybe_notify(item):
    """Called by the mail watcher for every new inbound email feed item.
    Fires the iMessage in a thread so the poll loop never blocks on
    osascript."""
    cfg = config()
    if not cfg["enabled"] or not (cfg["handle"] or _companion_paired()):
        return
    if item.get("channel") != "email" or not item.get("person_id"):
        return
    person = crm._load()["by_id"].get(item["person_id"])
    if not person:
        return
    tier = person.get("profile_tier") or person.get("master_tier")
    if tier != cfg["tier"]:
        return
    why = _throttled(item["person_id"])
    if why:
        return
    subject = item.get("subject") or (item.get("text") or "")[:80]
    text = f"{VIRA_PREFIX}{person['name']} emailed — {subject[:140]}"
    item = dict(item, ref={
        "kind": "email",
        "account": item.get("account") or "",
        "rowid": item.get("rowid") or "",
        "subject": subject,
        "from_addr": item.get("from_addr") or "",
        "person_id": item.get("person_id") or "",
        "person_name": person["name"],
    })
    threading.Thread(target=_send, args=(cfg["handle"], text, item),
                     daemon=True, name="vira-notify").start()


def agent_ping(text, key=None):
    """Agentic-OS completion pings (muse proposals, circuit finishes,
    routine outcomes) on the same iMessage path. `key` rides the throttle
    as a pseudo person id — a unique key per event means the 6h sender
    cooldown dedupes retries of the SAME event while distinct events still
    ping; the daily cap always applies."""
    cfg = config()
    if not cfg["enabled"] or not (cfg["handle"] or _companion_paired()):
        return False
    key = key or "agent"
    if _throttled(f"agent:{key}"):
        return False
    threading.Thread(
        target=_send, args=(cfg["handle"], text[:300],
                            {"person_id": f"agent:{key}",
                             "person_name": "Vira", "channel": "agent"}),
        daemon=True, name="vira-agent-ping").start()
    return True


def _send(handle, text, item):
    """Deliver on every channel that exists: iMessage when a handle is
    configured, a companion ping when an Android phone is paired. ok =
    at least one landed."""
    from . import send
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "person_id": item.get("person_id"),
        "person_name": item.get("person_name"),
        "channel": item.get("channel"),
        "text": text,
        # What this notification was ABOUT, in enough detail to act on it
        # later. A bare "Reply yes" is meaningless without it: the reply
        # channel binds an answer to the newest notification, and RSVPing
        # an invite needs the account and uid of the mail it rode in on.
        "ref": item.get("ref") or None,
        "ok": False,
    }
    via = []
    if handle:
        try:
            send.send_imessage(text, handle=handle)
            via.append("imessage")
        except Exception as e:  # noqa: BLE001 — log the failure, never crash
            entry["error"] = str(e)[:200]
    try:
        from . import companion
        if companion.queue_ping(text, kind=item.get("channel") or "notify"):
            via.append("companion")
    except Exception as e:  # noqa: BLE001
        entry.setdefault("error", str(e)[:200])
    entry["ok"] = bool(via)
    entry["via"] = via
    _record(entry)


def subs_renewals():
    """Renewal radar rule (subscriptions phase 4): ping for a renewal
    within 7 days when the per-cycle amount clears
    `subs_notify_threshold_usd` (default 100) — or for ANY annual renewal
    (a yearly hit is always worth a heads-up). Deduped per
    (merchant, renewal date) in the subs ledger meta, so the 6h Mercury
    poll cycle can call this freely; the shared daily cap and per-sender
    cooldown still apply on top. Returns the number of pings sent."""
    cfg = config()
    if not cfg["enabled"] or not cfg["handle"]:
        return 0
    from . import settings, subscriptions
    threshold = float(settings.get("subs_notify_threshold_usd") or 100)
    cycles = {"monthly": 12, "quarterly": 4, "semi-annual": 2, "annual": 1}
    r = subscriptions.reconcile()
    conn = subscriptions.ledger_connect()
    try:
        try:
            seen = json.loads(
                subscriptions.meta_get(conn, "subs_notified") or "{}")
        except json.JSONDecodeError:
            seen = {}
        sent = 0
        today = datetime.now().date()
        for m in r["merchants"]:
            if m["status"] in ("canceled", "ignored") or not m["next_renewal"]:
                continue
            days = (datetime.fromisoformat(m["next_renewal"]).date()
                    - today).days
            cycle_amt = (m["yearly"] / cycles[m["cadence"]]
                         if m["cadence"] in cycles and m["yearly"] else 0)
            if not 0 <= days <= 7:
                continue
            if cycle_amt < threshold and m["cadence"] != "annual":
                continue
            if seen.get(m["id"]) == m["next_renewal"]:
                continue
            if _throttled("subs:" + m["id"]):
                continue
            label = "today" if days == 0 else f"in {days}d"
            src = " (receipt-confirmed)" if m.get("renewal_source") == "receipt" else ""
            text = (f"Vira: {m['display_name']} renews {label} "
                    f"({m['next_renewal']}) — ${cycle_amt:,.2f} per "
                    f"{m['cadence'].replace('-', ' ')} cycle{src}")
            _send(cfg["handle"], text,
                  {"person_id": "subs:" + m["id"],
                   "person_name": m["display_name"], "channel": "subs"})
            seen[m["id"]] = m["next_renewal"]
            sent += 1
        subscriptions.meta_set(conn, "subs_notified", json.dumps(seen))
        conn.commit()
        return sent
    finally:
        conn.close()


def channel_send(text, kind="reply", ref=None):
    """Say something back in the self-thread.

    The conversational counterpart to the notification senders above, and
    the ONLY path server/inbound.py answers on. Three differences from a
    notification, each deliberate:

    * The VIRA_PREFIX is enforced rather than assumed. It is half of the
      echo filter, so a reply that lost it would be read back as the owner
      talking and routed as an instruction — the runaway-loop failure.
    * It is recorded BEFORE the send, not after. The loopback row can be in
      chat.db within milliseconds; recording afterwards leaves a window in
      which Vira's own words are indistinguishable from the owner's. A
      phantom entry from a failed send is harmless (it suppresses a message
      that was never delivered and that nobody typed).
    * It bypasses the sender cooldown and the notification cap — answering
      a question the owner just asked is not a notification — and carries
      REPLY_DAILY_CAP as its own runaway backstop instead.
    """
    cfg = config()
    if not cfg["enabled"] or not cfg["handle"]:
        return False
    body = text if text.startswith(VIRA_PREFIX) else VIRA_PREFIX + text
    body = body[:1400]
    today = datetime.now().date().isoformat()
    sent = _load_log().get("sent", [])
    if sum(1 for e in sent if e.get("channel") == kind
           and (e.get("at") or "").startswith(today)) >= REPLY_DAILY_CAP:
        return False
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "person_id": f"channel:{kind}",
        "person_name": "Vira",
        "channel": kind,
        "text": body,
        "ref": ref,
        "ok": False,
    }
    _record(entry)
    from . import send
    try:
        send.send_imessage(body, handle=cfg["handle"])
    except Exception as e:  # noqa: BLE001 — never crash the reply channel
        with _lock:
            log = _load_log()
            for row in reversed(log.get("sent", [])):
                if row is not entry and row.get("at") == entry["at"] \
                        and row.get("text") == body:
                    row["error"] = str(e)[:200]
                    break
            _save_log(log)
        return False
    with _lock:
        log = _load_log()
        for row in reversed(log.get("sent", [])):
            if row.get("text") == body and row.get("channel") == kind:
                row["ok"] = True
                row["via"] = ["imessage"]
                break
        _save_log(log)
    return True


def sent_texts(window_s=1800):
    """Every message Vira has put in the thread recently, exact text.

    The precise half of the echo filter. chat.db carries NO column that
    separates a Vira send from an owner send in this thread (measured
    2026-08-28: service, account, is_sent, is_delivered and
    destination_caller_id are byte-identical across the two), so the only
    discriminator available is what Vira knows it said.
    """
    now = time.time()
    out = []
    for e in _load_log().get("sent", []):
        t = e.get("text")
        if not t:
            continue
        try:
            at = datetime.fromisoformat(e["at"]).timestamp()
        except (KeyError, ValueError):
            continue
        if now - at <= window_s:
            out.append(t)
    return out


def recent_refs(window_s=6 * 3600, kind=None):
    """What Vira has notified about recently, newest first — the context a
    bare reply like "yes" is answering. `kind` filters to one ref kind."""
    now = time.time()
    out = []
    for e in reversed(_load_log().get("sent", [])):
        ref = e.get("ref")
        if not ref or (kind and ref.get("kind") != kind):
            continue
        try:
            at = datetime.fromisoformat(e["at"]).timestamp()
        except (KeyError, ValueError):
            continue
        if now - at > window_s:
            break
        out.append({"at": e.get("at"), "text": e.get("text"), "ref": ref})
    return out


def send_test(handle=None):
    """Settings-sheet test button: sends one message synchronously."""
    from . import send
    target = (handle or config()["handle"]).strip()
    if not target:
        raise ValueError("no notify handle configured (settings > Notifications)")
    text = "Vira: test notification — the iMessage channel works."
    send.send_imessage(text, handle=target)
    _record({
        "at": datetime.now().isoformat(timespec="seconds"),
        "person_id": None,
        "person_name": "(test)",
        "channel": "test",
        "text": text,
        "ok": True,
    })
    return {"sent": True, "handle": target}

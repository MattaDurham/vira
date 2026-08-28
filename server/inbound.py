"""The reply channel: the iMessage self-thread as a two-way command line.

Vira has texted the owner since 2026-07-07 and could never hear a word
back. The gap was not the plumbing — it was that nothing read what came in.
Measured on the live thread 2026-08-28, the day this was built:

  * The self-thread LOOPS BACK. A message sent to your own number lands in
    chat.db a second time as `is_from_me = 0`, so the ordinary watcher —
    which filters on exactly that — has been fetching the owner's replies
    all along. His one reply ("Reply yes", 2m28s after an invitation ping)
    was already in the feed, filed as an inbound message from himself.
  * 332 texted messages in that thread: 316 Vira notifications, 15 Morning
    Picker texts, and exactly one from the owner. He had tried once.
  * chat.db carries NO column that separates Vira's send from the owner's.
    service, account, account_guid, is_sent, is_delivered, was_downgraded
    and destination_caller_id are byte-identical across the two. The echo
    filter therefore has to be textual; there is no cleaner signal to find.

WHAT A TEXTED INSTRUCTION MAY DO (owner's ruling, 2026-08-28): act inside
Vira, confirm outward. Answering a decision card, steering a session,
closing a loop — all reversible, all inside this machine, all immediate.
Anything that leaves the machine — an RSVP, an email, a spend — is HELD and
confirmed in one line first. The confirmation always NAMES its target
("RSVP yes to <the event>?"), which is what makes a mis-bound reply
visible before it costs anything rather than after.

THE LADDER, in order, most specific first:

  1. A held outward action + yes/no        -> do it, or drop it
  2. A decision card is waiting            -> the text IS the answer
  3. A session is parked on its reply hook -> steer it
  4. A recognised intent on a recent ping  -> act, or hold if outward
  5. Anything else                         -> a session, with the ping it
                                              answers as context

Rung 5's session is told to ask through `mcp__vira__ask_owner` when it
needs a decision, and rung 2 is what carries that question back down the
same thread — so a clarification is a text, and the answer to it is a
text. That loop is the whole point.
"""
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import calinvite, jsonstore, notify, settings

_DATA = settings.ROOT / "data"
STATE = _DATA / "inbound-state.json"
LOG = _DATA / "inbound-log.json"

# How long a held outward action stays answerable. Long enough to walk away
# from the phone, short enough that tomorrow's "yes" cannot land on it.
HOLD_MINUTES = 30
# How far back a bare "yes" will look for the ping it is answering.
BIND_HOURS = 6
# A message Vira sent within this window is its own echo, not the owner.
ECHO_WINDOW_S = 1800
LOG_KEEP = 200

# Machine senders that are NOT notify.py and so carry neither the prefix nor
# a ledger entry. The Morning Picker's daily text is sent by a TC-IL
# scheduled task straight through AppleScript; 15 of them sit in the thread.
# Without this they read as the owner talking and every morning would
# dispatch a session. Config `inbound_ignore_prefixes` extends it.
MACHINE_PREFIXES = ("Morning picker ready",)

CANCEL = {"cancel", "nevermind", "never mind", "stop", "no thanks",
          "forget it", "abort"}

_lock = threading.Lock()


def _cfg(key, default):
    try:
        return settings.get(key)
    except (KeyError, AttributeError):
        return default


def enabled():
    """Dormant unless a handle is configured and the owner has not turned it
    off — the notify/mail dormancy pattern."""
    if os.environ.get("VIRA_PASSIVE"):
        return False
    if not notify.config()["handle"]:
        return False
    return bool(_cfg("imessage_reply_enabled", True))


def _state():
    return jsonstore.read(STATE, {"held": None, "session": None,
                                  "pinged": []})


def _save(fn):
    return jsonstore.mutate(STATE, fn,
                            {"held": None, "session": None, "pinged": []})


def _log(entry):
    def fn(store):
        store.setdefault("routed", []).append(entry)
        store["routed"] = store["routed"][-LOG_KEEP:]
        return store
    jsonstore.mutate(LOG, fn, {"routed": []})


def recent(limit=50):
    return list(reversed(jsonstore.read(LOG, {"routed": []})
                         .get("routed", [])))[:limit]


# ---------- the echo filter ----------

def is_ours(text):
    """True when Vira (or another machine) put this text in the thread.

    Three rungs because each covers what the others miss: the ledger is
    exact but can race a very fast loopback, the prefix covers every
    notify.py path whether or not it reached the ledger, and the machine
    list covers senders outside this process entirely.
    """
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith(notify.VIRA_PREFIX):
        return True
    prefixes = tuple(MACHINE_PREFIXES) + tuple(
        _cfg("inbound_ignore_prefixes", []) or [])
    if t.startswith(prefixes):
        return True
    return any(t == s.strip() for s in notify.sent_texts(ECHO_WINDOW_S))


# ---------- the watcher hook ----------

def consume(items):
    """Called by imessage.Watcher with each tick's new feed items.

    Never raises: the feed must not stop because a reply could not be
    routed. Rides the existing 3s poll rather than opening a second sqlite
    cycle — these rows are already being fetched.
    """
    if not enabled():
        return
    handle = notify.config()["handle"]
    for item in items or []:
        try:
            if item.get("channel") != "imessage" or item.get("group"):
                continue
            if (item.get("handle") or "") != handle:
                continue
            text = (item.get("text") or "").strip()
            if is_ours(text):
                continue
            route(text, item)
        except Exception as e:  # noqa: BLE001 — one bad reply is not the feed
            _log({"at": datetime.now().isoformat(timespec="seconds"),
                  "text": (item or {}).get("text", "")[:200],
                  "route": "error", "detail": str(e)[:300]})


# ---------- the ladder ----------

def route(text, item=None):
    """Route one owner message. Returns the disposition dict it logged."""
    now = datetime.now(timezone.utc)
    disp = _route(text, now)
    disp.update({"at": datetime.now().isoformat(timespec="seconds"),
                 "text": text[:400]})
    _log(disp)
    return disp


def _route(text, now):
    t = text.strip()
    low = t.lower().strip(".!,")

    # 1 — a held outward action.
    held = _live_hold(now)
    if held:
        ans = calinvite.norm_answer(t)
        if low in CANCEL or ans == "no":
            _clear_hold()
            notify.channel_send("dropped it — nothing sent.", kind="reply")
            return {"route": "hold-cancelled", "action": held.get("action")}
        if ans == "yes":
            _clear_hold()
            return _perform(held["action"])
        # Anything else supersedes it: a new instruction is not an answer,
        # and leaving the hold armed would let a later stray "yes" fire it.
        _clear_hold()
        notify.channel_send(
            f"dropped the earlier ask ({held.get('summary', 'pending')}). "
            "Taking this as new.", kind="reply")

    # 2 — a decision card is waiting.
    card = _pending_card()
    if card:
        return _answer_card(card, t)

    # 3 — a session parked on its reply hook.
    jid = _bound_session()
    if jid:
        return _steer(jid, t)

    # 4 — a recognised intent against a recent ping.
    intent = _intent(t)
    if intent:
        return intent

    # 5 — anything else becomes a session.
    return _dispatch(t)


# ---------- rung 1: holding an outward action ----------

def _live_hold(now):
    held = _state().get("held")
    if not held:
        return None
    try:
        if datetime.fromisoformat(held["expires"]) < now:
            _clear_hold()
            return None
    except (KeyError, ValueError):
        _clear_hold()
        return None
    return held


def _hold(action, summary, question):
    exp = (datetime.now(timezone.utc)
           + timedelta(minutes=HOLD_MINUTES)).isoformat()

    def fn(store):
        store["held"] = {"action": action, "summary": summary,
                         "expires": exp,
                         "asked": datetime.now().isoformat(timespec="seconds")}
        return store
    _save(fn)
    notify.channel_send(question, kind="reply")
    return {"route": "held", "action": action, "summary": summary}


def _clear_hold():
    def fn(store):
        store["held"] = None
        return store
    _save(fn)


def _perform(action):
    """Do a held outward action and report the outcome in one line."""
    kind = (action or {}).get("kind")
    if kind == "rsvp":
        try:
            res = calinvite.rsvp(action["account"], action["rowid"],
                                 action["answer"])
        except calinvite.RsvpError as e:
            notify.channel_send(f"could not RSVP — {e}", kind="reply")
            return {"route": "rsvp", "ok": False, "detail": str(e)[:300]}
        notify.channel_send(
            f"RSVP'd {action['answer']} to “{res.get('event')}” "
            f"({res.get('detail')}).", kind="reply")
        return {"route": "rsvp", "ok": True, "detail": res.get("detail")}
    notify.channel_send("I no longer know how to do that.", kind="reply")
    return {"route": "unknown-action", "ok": False, "action": action}


# ---------- rung 2: decision cards ----------

def _pending_card():
    try:
        from . import session
        rows = session.sessions.pending_all()
    except Exception:  # noqa: BLE001 — no supervisor here is not an error
        return None
    return rows[0] if rows else None


def _answer_card(row, text):
    from . import session
    card = row["card"]
    try:
        if card.get("kind") == "ask":
            session.sessions.answer(row["job_id"], card["req_id"], text)
            verb = "answered"
        else:
            allow = calinvite.norm_answer(text) == "yes"
            session.sessions.permission(row["job_id"], card["req_id"], allow,
                                        reason="answered by text")
            verb = "approved" if allow else "denied"
    except Exception as e:  # noqa: BLE001
        notify.channel_send(f"couldn't answer that — {e}", kind="reply")
        return {"route": "card", "ok": False, "detail": str(e)[:300]}
    notify.channel_send(f"{verb} — carrying on.", kind="reply")
    return {"route": "card", "ok": True, "job": row["job_id"], "verb": verb}


# ---------- rung 3: steering a parked session ----------

def _bound_session():
    sess = _state().get("session")
    if not sess:
        return None
    try:
        from . import session
        snap = session.sessions.get(sess["job"])
    except Exception:  # noqa: BLE001
        return None
    if not snap or snap.get("status") != "running":
        _bind_session(None)
        return None
    return sess["job"]


def _bind_session(jid):
    def fn(store):
        store["session"] = ({"job": jid,
                             "at": datetime.now().isoformat(
                                 timespec="seconds")} if jid else None)
        return store
    _save(fn)


def _steer(jid, text):
    from . import session
    try:
        res = session.sessions.say(jid, text)
    except Exception as e:  # noqa: BLE001
        _bind_session(None)
        return {"route": "steer", "ok": False, "detail": str(e)[:300]}
    new = (res or {}).get("job") or jid
    if new != jid:
        _bind_session(new)
    return {"route": "steer", "ok": True, "job": new}


# ---------- rung 4: recognised intents ----------

INVITE_RE = re.compile(r"\b(invit|rsvp|calendar)", re.I)


def _intent(text):
    """Deterministic intents only — no model call, no guessing.

    The one that exists today is RSVP, because that is the one the owner
    asked for. An affirmation with no recent invitation to bind to is NOT
    an RSVP; it falls through to rung 5 rather than being forced into the
    nearest known action.
    """
    ans = calinvite.norm_answer(text)
    if not ans:
        return None
    for row in notify.recent_refs(BIND_HOURS * 3600, kind="email"):
        ref = row["ref"]
        subject = ref.get("subject") or ""
        if not INVITE_RE.search(subject):
            continue
        who = ref.get("person_name") or ref.get("from_addr") or "them"
        try:
            plan = calinvite.rsvp(ref["account"], ref["rowid"], ans,
                                  dry_run=True)
        except calinvite.RsvpError as e:
            notify.channel_send(
                f"can't RSVP that one — {e}", kind="reply")
            return {"route": "rsvp-unavailable", "ok": False,
                    "detail": str(e)[:300]}
        event = plan.get("event") or subject
        return _hold(
            {"kind": "rsvp", "account": ref["account"],
             "rowid": ref["rowid"], "answer": ans},
            f"RSVP {ans} to “{event}”",
            f"RSVP {ans} to “{event}” ({who})? Reply ok to send, "
            "or no to drop it.")
    return None


# ---------- rung 5: a session ----------

PROMPT = """The owner just sent you this by text message, from his phone:

    {text}

{context}
You are Vira, answering on the iMessage thread you notify him on. Rules for
this channel:

* Act freely INSIDE this machine — read anything, and use Vira's own API on
  http://localhost:8377 for things Vira already does.
* Anything that LEAVES this machine — sending mail, answering an invitation,
  posting, spending, messaging anyone else — is not yours to decide. Ask
  first with mcp__vira__ask_owner; the question is texted to him and his
  reply comes back to you.
* If you need anything clarified, ask the same way rather than guessing.
* He is reading this on a phone. Your final message is texted to him
  verbatim, so make it one or two plain sentences, no markdown, no preamble.
"""


def _dispatch(text):
    try:
        from . import session
        ctx = ""
        refs = notify.recent_refs(BIND_HOURS * 3600)
        if refs:
            ctx = ("It most likely answers the last thing you texted him:\n"
                   f"    {refs[0]['text']}\n\n")
        jid = session.sessions.launch(
            PROMPT.format(text=text, context=ctx),
            cwd=str(settings.ROOT),
            meta={"channel": "imessage", "kind": "text-reply"})
    except Exception as e:  # noqa: BLE001
        notify.channel_send(f"couldn't start on that — {e}", kind="reply")
        return {"route": "dispatch", "ok": False, "detail": str(e)[:300]}
    _bind_session(jid)
    threading.Thread(target=_follow, args=(jid,), daemon=True,
                     name="vira-inbound-follow").start()
    return {"route": "dispatch", "ok": True, "job": jid}


def _follow(jid, timeout_s=900):
    """Text back the session's answer when it lands.

    Only the FINAL text: the owner asked for a chat, not a transcript.
    """
    from . import session
    end = time.time() + timeout_s
    while time.time() < end:
        time.sleep(3)
        try:
            snap = session.sessions.get(jid)
        except Exception:  # noqa: BLE001
            return
        if not snap:
            return
        if snap.get("status") == "running":
            continue
        out = (snap.get("result_text") or "").strip()
        if out:
            notify.channel_send(out[:900], kind="reply")
        return


# ---------- decision cards raised anywhere reach the phone ----------

def ping_cards():
    """Text the owner when a session is blocked on a decision.

    Without this the reply channel can ANSWER a card he never learned
    about. Applies to every session, not only text-launched ones — a card
    raised at 2am by a routine is exactly the one worth reaching a phone.
    """
    if not enabled():
        return
    rows = _pending_card_rows()
    if not rows:
        return
    seen = set(_state().get("pinged") or [])
    for row in rows:
        card = row["card"]
        key = f"{row['job_id']}:{card.get('req_id')}"
        if key in seen:
            continue
        q = (card.get("question") or card.get("summary")
             or card.get("tool") or "a decision")
        notify.channel_send(f"waiting on you — {str(q)[:240]}", kind="reply")
        seen.add(key)

    def fn(store):
        store["pinged"] = list(seen)[-100:]
        return store
    _save(fn)


def _pending_card_rows():
    try:
        from . import session
        return session.sessions.pending_all()
    except Exception:  # noqa: BLE001
        return []


def start(interval_s=20):
    """The card pinger. The reply half needs no thread — it rides the
    message watcher's own 3s tick through consume()."""
    if not enabled():
        return None

    def loop():
        while True:
            time.sleep(interval_s)
            try:
                ping_cards()
            except Exception:  # noqa: BLE001 — never kill the pinger
                pass
    t = threading.Thread(target=loop, daemon=True, name="vira-inbound-cards")
    t.start()
    return t

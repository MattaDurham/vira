"""After a call: ask whether it covered the open loops with that person.

THE MOMENT. A phone or FaceTime call of a few minutes with someone who has
open loops is the likeliest place those loops got settled, and the minutes
after it are the only time the owner still remembers which. So when such a
call ends, Vira raises one card: "You just talked to David for 14 min. Did
you cover any of these?", the person's open loops as tap-to-close items,
and a box for anything new that was agreed. It texts the same question once
(notify.agent_ping) so it reaches the phone.

WHERE THE CALLS COME FROM. The Mac's call history (server/callhistory.py),
which the iPhone syncs into. The assistant's minute timer runs `tick`; a
call counts once it has ended, lasted at least `call_loops_min_minutes`,
and its number or email resolves to a CRM person who is not only reachable
on a shared office line. Calls that ended more than LOOKBACK ago are never
asked about: a question about a call from last week is not "right after".

WHAT IT WRITES. Its own ledger, data/call-loops.json (which calls were
seen, and each card's state), and the CRM only through data.update_loop
("close") and data.add_loop, the same writes the brief's loop buttons
make. A second call with the same person while their card is still open
updates that card rather than raising another. Nothing here calls a model.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os

from . import callhistory, jsonstore, settings
from . import data as crm

STORE = settings.ROOT / "data" / "call-loops.json"
UTC = dt.timezone.utc
# How far back a pass looks for ended calls. Long enough to cover a server
# restart or a slow iCloud sync of the iPhone's log; short enough that a
# question still reads as "you just talked".
LOOKBACK = dt.timedelta(hours=2)
# A card nobody answered stops being a question after this long; the loops
# stay open on the person, where the brief keeps showing them.
ASK_KEEP = dt.timedelta(days=3)
# Loops on one card: a phone card with more than this many tap targets is a
# list to read, not a question to answer. Owed-by-me first, oldest first.
MAX_LOOPS = 6
# Call ids remembered for de-duplication; far more than LOOKBACK can hold.
SEEN_KEEP = 400
# Answered and expired cards kept for the record, newest first.
DONE_KEEP = 100
# The text that reaches the phone; notify caps a ping at 300 characters.
LOOP_TEXT_MAX = 60
ME, THEM = "me", "them"
FINISH = ("done", "none")


def _now():
    return dt.datetime.now(UTC)


def _cfg(key, default=None):
    try:
        return settings.get(key)
    except KeyError:
        return default


def enabled():
    # A fixture or sandboxed instance never reads the real call log.
    return (_cfg("call_loops_enabled", True) is True
            and not os.environ.get("VIRA_SANDBOX") and not settings.fixture_mode())


def min_seconds():
    return max(0, int(_cfg("call_loops_min_minutes", 3) or 0)) * 60


# ------------------------------------------------------------------ store

def _empty():
    return {"asks": {}, "seen": [], "checked_at": "", "source": {"status": "", "detail": ""}}


def _read():
    state = jsonstore.read(STORE, _empty())
    base = _empty()
    if isinstance(state, dict):
        base.update({k: v for k, v in state.items() if k in base})
    return base


def _mutate(fn):
    def apply(state):
        full = _empty()
        if isinstance(state, dict):
            full.update({k: v for k, v in state.items() if k in full})
        fn(full)
        return full
    return jsonstore.mutate(STORE, apply, _empty(), indent=2, ensure_ascii=False)


def _ask_id(call_id):
    return hashlib.sha1(str(call_id).encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ reading

def _person_for(handle):
    """The CRM person a call's number or email belongs to, or None. A
    shared office line is nobody's signal, the same rule the Sync tab's
    "Same office" sets for messages."""
    if not handle or crm.handle_key(handle) in crm.lines():
        return None
    return crm.resolve_handle(handle)


def open_loops(pid):
    """A person's open loops, owed-by-me first and oldest first, capped."""
    c = crm._load()
    prof = c["profiles"].get(pid) or {}
    loops = [lp for lp in (prof.get("open_loops") or [])
             if isinstance(lp, dict) and lp.get("status") != "closed"
             and str(lp.get("what") or "").strip()]
    loops.sort(key=lambda lp: (lp.get("owed_by") != ME, str(lp.get("since") or "9999")))
    return [{"what": str(lp["what"]).strip(), "owed_by": lp.get("owed_by") or "",
             "since": str(lp.get("since") or "")[:10], "status": "open"}
            for lp in loops[:MAX_LOOPS]], len(loops)


def _name(pid):
    p = crm._load()["by_id"].get(pid) or {}
    return str(p.get("name") or "").strip() or "them"


def _first(name):
    return name.split()[0] if name and name != "them" else name


def _minutes(seconds):
    return max(1, round(seconds / 60))


def question(ask):
    who = _first(ask["person_name"])
    via = " on FaceTime" if ask["kind"].startswith("FaceTime") else ""
    return (f"You just talked to {who}{via} for {ask['minutes']} min. "
            "Did you cover any of these?")


def _ping_text(ask):
    items = []
    for i, lp in enumerate(ask["loops"], 1):
        what = lp["what"]
        if len(what) > LOOP_TEXT_MAX:
            what = what[:LOOP_TEXT_MAX - 3].rstrip() + "..."
        items.append(f"{i}) {what}")
    tail = " Tap them closed in Vira > Attention."
    text = question(ask) + " " + "; ".join(items)
    return text[:300 - len(tail)].rstrip() + tail


# ------------------------------------------------------------------ the pass

def tick(now=None):
    """One pass: read calls that ended recently, raise a card for each new
    qualifying one, text it once, and retire cards nobody answered."""
    if not enabled():
        return {"enabled": False}
    now = now or _now()
    try:
        calls = callhistory.calls_ended_since(now - LOOKBACK)
        source = {"status": "ok", "detail": ""}
    except callhistory.Unavailable as exc:
        calls, source = [], {"status": exc.status, "detail": exc.detail}
    snapshot = _read()
    seen = set(snapshot["seen"])
    floor = min_seconds()
    fresh = []
    for call in calls:
        if call["id"] in seen:
            continue
        ended = dt.datetime.fromisoformat(call["ended_at"])
        if ended > now:
            continue  # still on the call: ask once it ends
        entry = {"call": call, "pid": None}
        if call["answered"] and call["seconds"] >= floor:
            entry["pid"] = _person_for(call["handle"])
        fresh.append(entry)
    # Loops are read outside the store lock: the CRM has its own.
    for entry in fresh:
        if entry["pid"]:
            entry["loops"], entry["total"] = open_loops(entry["pid"])
            entry["name"] = _name(entry["pid"])
    raised = []

    def record(state):
        state["source"] = source
        state["checked_at"] = now.isoformat()
        for entry in fresh:
            call = entry["call"]
            state["seen"].append(call["id"])
            if not entry["pid"] or not entry.get("loops"):
                continue
            current = next((a for a in state["asks"].values()
                            if a["status"] == "open" and a["person_id"] == entry["pid"]), None)
            if current:
                # A dropped call and a call back is one conversation.
                current["minutes"] += _minutes(call["seconds"])
                current["ended_at"] = call["ended_at"]
                current["call_ids"].append(call["id"])
                continue
            ask = {"id": _ask_id(call["id"]), "person_id": entry["pid"],
                   "person_name": entry["name"], "kind": call["kind"],
                   "minutes": _minutes(call["seconds"]), "ended_at": call["ended_at"],
                   "call_ids": [call["id"]], "loops": entry["loops"],
                   "more_loops": max(0, entry["total"] - len(entry["loops"])),
                   "added": [], "status": "open", "created_at": now.isoformat(),
                   "answered_at": "", "texted": False}
            state["asks"][ask["id"]] = ask
            raised.append(ask["id"])
        state["seen"] = state["seen"][-SEEN_KEEP:]
        for ask in state["asks"].values():
            if ask["status"] == "open" and now - dt.datetime.fromisoformat(ask["created_at"]) > ASK_KEEP:
                ask["status"] = "expired"
        _prune(state)
    _mutate(record)
    texted = [ask_id for ask_id in raised if _text(ask_id, now)]
    return {"enabled": True, "source": source["status"], "raised": raised, "texted": texted}


def _prune(state):
    closed = sorted((a for a in state["asks"].values() if a["status"] != "open"),
                    key=lambda a: a.get("answered_at") or a["created_at"], reverse=True)
    for ask in closed[DONE_KEEP:]:
        state["asks"].pop(ask["id"], None)


def _text(ask_id, now):
    """Text the question once, outside quiet hours. The card is in
    Attention either way; a call at midnight is asked about there."""
    from . import executive, notify
    ask = _read()["asks"].get(ask_id)
    if not ask or ask["texted"]:
        return False
    try:
        if executive.quiet(now):
            return False
        sent = notify.agent_ping(_ping_text(ask), key="calls:" + ask_id)
    except Exception:  # noqa: BLE001 - the Attention card still carries the question
        return False
    if sent:
        _mutate(lambda s: s["asks"].get(ask_id, {}).update(texted=True))
    return bool(sent)


# ------------------------------------------------------------------ answers

def _open_ask(state, ask_id):
    ask = state["asks"].get(ask_id)
    if not ask:
        raise KeyError(f"no call question {ask_id}")
    if ask["status"] != "open":
        raise ValueError("This call question was already answered.")
    return ask


def close_loop(ask_id, index):
    """The owner tapped a loop: it was covered. Closes it on the person
    (data.update_loop), then marks it on the card. A loop the CRM no
    longer holds open is marked gone rather than failing the tap."""
    ask = _open_ask(_read(), ask_id)
    try:
        loop = ask["loops"][int(index)]
    except (IndexError, ValueError, TypeError):
        raise ValueError("No such loop on this card.") from None
    if loop["status"] != "open":
        return public(ask)
    try:
        crm.update_loop(ask["person_id"], loop["what"], "close")
        mark = "closed"
    except LookupError:
        mark = "gone"

    def apply(state):
        current = _open_ask(state, ask_id)
        current["loops"][int(index)]["status"] = mark
        if all(lp["status"] != "open" for lp in current["loops"]):
            current["status"], current["answered_at"] = "answered", _now().isoformat()
    return public(_mutate(apply)["asks"][ask_id])


def add_loop(ask_id, what, owed_by=ME):
    """Something new was agreed on the call: a new open loop on the person."""
    what = str(what or "").strip()
    if not what:
        raise ValueError("Say what was agreed.")
    owed_by = owed_by if owed_by in (ME, THEM) else ME
    ask = _open_ask(_read(), ask_id)
    crm.add_loop(ask["person_id"], what[:500], owed_by)

    def apply(state):
        _open_ask(state, ask_id)["added"].append({"what": what[:500], "owed_by": owed_by})
    return public(_mutate(apply)["asks"][ask_id])


def finish(ask_id, action):
    """`done`: the card is answered. `none`: none of these came up. Either
    way the loops not tapped stay open on the person."""
    if action not in FINISH:
        raise ValueError(f"unknown action {action!r}")

    def apply(state):
        ask = _open_ask(state, ask_id)
        ask["status"] = "answered" if action == "done" else "none"
        ask["answered_at"] = _now().isoformat()
    return public(_mutate(apply)["asks"][ask_id])


# ------------------------------------------------------------------ reads

def public(ask):
    return dict(ask, question=question(ask))


def pending():
    """Open cards, newest call first."""
    asks = [public(a) for a in _read()["asks"].values() if a["status"] == "open"]
    return sorted(asks, key=lambda a: a["ended_at"], reverse=True)


def status():
    state = _read()
    return {"enabled": enabled(), "min_minutes": min_seconds() // 60,
            "source": state["source"] if state["checked_at"] else callhistory.status(),
            "checked_at": state["checked_at"], "pending": pending(),
            "recent": [public(a) for a in sorted(
                (a for a in state["asks"].values() if a["status"] != "open"),
                key=lambda a: a.get("answered_at") or a["created_at"], reverse=True)[:10]]}

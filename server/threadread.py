"""Thread analysis: compute what a conversation actually looks like before
asking a model what to say about it.

`suggest.py` hands a model a profile blob and thirty messages and asks for
three replies that vary in tone. That produces three more decisions, which is
why the surface reads as nagging: it adds work instead of removing it.

This module does the arithmetic first. Everything here is deterministic and
computed from chat.db - who talks, who starts, how fast each side answers,
how densely messages arrive, and which asks are actually still open. The
model layer in `brief()` then reasons over those facts and returns a decision
brief rather than a menu of drafts.

The load-bearing inversion: the brief names what needs NOTHING. A burst of
eight messages usually carries one real ask, and the other seven are read as
obligations only because they arrived. Saying so out loud is the feature.

Honesty bound: chat.db cannot see an answer given in person or by phone. For
anyone the owner lives with, silence in the log is not evidence of
non-response, and every payload carries `colocation_caveat` so no surface can
quietly present it as one.
"""

import re
import statistics
from collections import Counter
from datetime import timedelta

from . import data as crm
from . import imessage

SESSION_GAP_H = 6      # silence that separates one conversation from the next
STALE_H = 12           # an ask that sat this long registers as having sat
DEFAULT_WINDOW_D = 14

# An ask that carries its own release. These are the messages that generate
# guilt without requiring anything, and naming them is most of the relief.
RELEASE = re.compile(
    r"\b(optional|no rush|no need|no reply needed|whenever|fyi|just so you know|"
    r"don'?t worry about|not urgent|no pressure|if you (?:get|have) a chance)\b", re.I)

REQUEST = re.compile(
    r"\b(can you|could you|would you|can u|could u|will you|please|plz|"
    r"i need you to|need you to|don'?t forget|remember to|remind me|"
    r"let me know|lmk|thoughts\?|what do you think|are you able)\b", re.I)


def _handles(pid):
    p = crm._load()["by_id"].get(pid)
    if not p:
        return []
    hs = set(p.get("handles", {}).get("imessage", []))
    for ph in p.get("handles", {}).get("phones10", []):
        hs.add("+1" + ph)
    return sorted(hs)


def messages(pid, days=365):
    """Full 1:1 history with attachment counts and reaction flags.

    Mirrors imessage.thread_for_person's `c.style = 45` filter, which is what
    keeps group-chat traffic out of a one-to-one count. Reactions are kept
    (flagged, not dropped) because they are real responses and excluding them
    understates how much the owner actually answers.
    """
    hs = _handles(pid)
    if not hs:
        return []
    q = ",".join("?" * len(hs))
    con = imessage._connect()
    try:
        rows = con.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                       m.associated_message_type,
                       (SELECT COUNT(*) FROM message_attachment_join maj
                         WHERE maj.message_id = m.ROWID)
                FROM message m
                JOIN handle h ON h.ROWID = m.handle_id
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE h.id IN ({q}) AND c.style = 45
                ORDER BY m.date""", hs).fetchall()
    finally:
        con.close()

    out = []
    for rowid, dt, from_me, text, blob, amt, natt in rows:
        when = imessage.apple_dt(dt)
        if not when:
            continue
        out.append({
            "rowid": rowid,
            "when": when,
            "from_me": bool(from_me),
            "text": imessage.msg_text(text, blob) or "",
            "reaction": bool(amt and amt >= 2000),
            "attachments": natt or 0,
        })
    if days:
        cut = out[-1]["when"] - timedelta(days=days) if out else None
        out = [m for m in out if m["when"] >= cut] if cut else out
    return out


def _said(msgs):
    """Messages that carry content, i.e. not bare reactions."""
    return [m for m in msgs if not m["reaction"]]


def cadence(msgs):
    """Volume, latency and initiation. Initiation is the one that matters and
    the one no inbox surfaces: it is the difference between answering someone
    and reaching for them."""
    said = _said(msgs)
    if not said:
        return {}
    mine = [m for m in said if m["from_me"]]
    theirs = [m for m in said if not m["from_me"]]

    lat = []
    for i, m in enumerate(said):
        if m["from_me"]:
            continue
        for nxt in said[i + 1:]:
            if nxt["from_me"]:
                lat.append((nxt["when"] - m["when"]).total_seconds() / 60)
                break

    starts_mine = starts_theirs = 0
    prev = None
    for m in said:
        if prev is None or (m["when"] - prev["when"]).total_seconds() > SESSION_GAP_H * 3600:
            if m["from_me"]:
                starts_mine += 1
            else:
                starts_theirs += 1
        prev = m

    total_starts = starts_mine + starts_theirs
    return {
        "mine": len(mine),
        "theirs": len(theirs),
        "my_share_pct": round(100 * len(mine) / len(said)),
        "median_reply_min": round(statistics.median(lat)) if lat else None,
        "replies_over_12h": sum(1 for x in lat if x > STALE_H * 60),
        "starts_mine": starts_mine,
        "starts_theirs": starts_theirs,
        "my_initiation_pct": round(100 * starts_mine / total_starts) if total_starts else None,
    }


def bursts(msgs, gap_s=120, min_len=4):
    """Runs of their messages arriving faster than anyone can answer. Peak
    density is reported in objects (messages plus photos) because seven photos
    in one second is what actually lands as overwhelming."""
    said = _said(msgs)
    runs, cur = [], []
    for m in said:
        if m["from_me"]:
            if len(cur) >= min_len:
                runs.append(cur)
            cur = []
            continue
        if cur and (m["when"] - cur[-1]["when"]).total_seconds() <= gap_s:
            cur.append(m)
        else:
            if len(cur) >= min_len:
                runs.append(cur)
            cur = [m]
    if len(cur) >= min_len:
        runs.append(cur)

    peak = None
    for r in runs:
        span = max((r[-1]["when"] - r[0]["when"]).total_seconds(), 1)
        objs = len(r) + sum(x["attachments"] for x in r)
        rate = objs / (span / 60)
        if not peak or rate > peak["objects_per_min"]:
            peak = {
                "at": r[0]["when"].isoformat(),
                "messages": len(r),
                "attachments": sum(x["attachments"] for x in r),
                "span_s": round(span),
                "objects_per_min": round(rate, 1),
            }
    return {"count": len(runs),
            "largest": max((len(r) for r in runs), default=0),
            "peak": peak}


def _classify(text):
    if RELEASE.search(text):
        return "released"
    if "?" in text:
        return "question"
    if REQUEST.search(text):
        return "request"
    return None


def open_asks(msgs, window_days=DEFAULT_WINDOW_D):
    """What they actually asked that is still sitting.

    `pending` is everything asked since the owner last said anything, which is
    definitionally awaiting him. `released` is the subset that carries its own
    permission to be ignored - the seven of the eight. Surfacing that split is
    the whole point: volume is not obligation.
    """
    said = _said(msgs)
    if not said:
        return {"pending": [], "released": [], "stale": []}
    cut = said[-1]["when"] - timedelta(days=window_days)
    recent = [m for m in said if m["when"] >= cut]

    last_mine = None
    for m in recent:
        if m["from_me"]:
            last_mine = m["when"]

    pending, released, stale = [], [], []
    for i, m in enumerate(recent):
        if m["from_me"] or not m["text"]:
            continue
        kind = _classify(m["text"])
        if not kind:
            continue
        item = {"rowid": m["rowid"], "when": m["when"].isoformat(),
                "text": m["text"][:400], "kind": kind,
                "attachments": m["attachments"]}
        if kind == "released":
            released.append(item)
            continue
        if last_mine is None or m["when"] > last_mine:
            pending.append(item)
        else:
            nxt = next((x for x in recent[i + 1:] if x["from_me"]), None)
            if nxt and (nxt["when"] - m["when"]).total_seconds() > STALE_H * 3600:
                item["sat_hours"] = round((nxt["when"] - m["when"]).total_seconds() / 3600, 1)
                stale.append(item)
    return {"pending": pending, "released": released, "stale": stale}


def analyze(pid, window_days=DEFAULT_WINDOW_D):
    """The computed picture. No model involved, so nothing here can be
    hallucinated - every number is arithmetic over chat.db."""
    hist = messages(pid, days=400)
    if not hist:
        return {"person_id": pid, "empty": True}
    cut = hist[-1]["when"] - timedelta(days=window_days)
    recent = [m for m in hist if m["when"] >= cut]

    base, now = cadence(hist), cadence(recent)
    deltas = {}
    for k in ("my_share_pct", "median_reply_min", "my_initiation_pct"):
        if base.get(k) is not None and now.get(k) is not None:
            deltas[k] = {"baseline": base[k], "recent": now[k],
                         "change": now[k] - base[k]}

    return {
        "person_id": pid,
        "window_days": window_days,
        "baseline": base,
        "recent": now,
        "deltas": deltas,
        "bursts": bursts(recent),
        "asks": open_asks(hist, window_days),
        "last_message_at": hist[-1]["when"].isoformat(),
        "last_from_me": hist[-1]["from_me"],
        "colocation_caveat": (
            "chat.db cannot see replies given in person or by phone. If you "
            "live with this person, silence in the log is not evidence of "
            "non-response."),
    }


BRIEF_PROMPT = """You are helping {owner} decide how to handle a conversation.
You are NOT writing a menu of drafts. {owner} is tired and does not need three
options to choose between; he needs to know what is actually being asked, what
he can ignore, and one move.

These figures are computed from the message database. They are facts, not
impressions. Do not contradict them and do not restate them all back.

{facts}

Open asks, already extracted (kind "released" means the message itself says it
needs nothing):
{asks}

Recent conversation (chronological; "me" = {owner}):
{thread}

{extra}

Rules:
- Lead with what is actually going on in one sentence. If the numbers and the
  other person's stated complaint disagree, say so plainly.
- Separate what needs a response from what does not. Be explicit about the
  count: "N messages, M actual asks" is more useful than any draft.
- "how_to_think" is strategy, not phrasing: what closing a loop does, what
  leaving one open invites, what the timing buys. Never therapy-speak.
- Exactly ONE suggested message. Short. In {owner}'s evidenced voice from the
  "me" lines. It should close as many open asks as one message honestly can.
- If the honest move is to send nothing, say that and explain what to do
  instead.
- Never invent facts not in the thread or the figures.

Return ONLY JSON:
{{"headline": "...",
  "needs_response": [{{"rowid": 0, "what": "...", "why": "..."}}],
  "needs_nothing": [{{"rowid": 0, "what": "..."}}],
  "how_to_think": ["...", "..."],
  "move": {{"text": "...", "why": "...", "closes": [0]}},
  "metric_that_explains_it": "..."}}
"""


def brief(pid, window_days=DEFAULT_WINDOW_D, extra=""):
    """Decision brief: computed facts first, model reasoning second."""
    from . import suggest

    facts = analyze(pid, window_days)
    if facts.get("empty"):
        return {"empty": True, "person_id": pid}

    msgs = imessage.thread_for_person(pid, limit=40)
    thread = "\n".join(
        f"[{m['when'][:16] if m['when'] else '?'}] "
        f"{'me' if m['from_me'] else 'them'}: {m['text']}" for m in msgs)

    import json as _json
    lean = {k: facts[k] for k in
            ("baseline", "recent", "deltas", "bursts", "colocation_caveat")}
    owner = suggest.config().get("owner_name") or "the user"
    prompt = BRIEF_PROMPT.format(
        owner=owner,
        facts=_json.dumps(lean, indent=1)[:4000],
        asks=_json.dumps(facts["asks"], indent=1)[:4000],
        thread=thread[:12000],
        extra=f"Guidance from {owner}: {extra}" if extra else "")

    text, backend = suggest._run(prompt, suggest.config())
    out = suggest._extract_json(text)
    out["facts"] = facts
    out["backend"] = backend
    return out


# ---------- HTTP surface -------------------------------------------------
# Two endpoints, deliberately split. /analyze is pure arithmetic and always
# safe to call: no model, no egress, no cost. /brief adds the reasoning layer
# and inherits suggest.py's configured backend, so it obeys the same
# "no backend configured, no model egress" contract as everything else.

from fastapi import APIRouter, HTTPException          # noqa: E402
from pydantic import BaseModel                        # noqa: E402

router = APIRouter(prefix="/api/threadread", tags=["threadread"])


class BriefReq(BaseModel):
    person_id: str
    window_days: int = DEFAULT_WINDOW_D
    extra: str = ""


@router.get("/analyze/{person_id}")
def api_analyze(person_id: str, window_days: int = DEFAULT_WINDOW_D):
    """Computed picture only. No model call."""
    try:
        return analyze(person_id, window_days)
    except KeyError:
        raise HTTPException(404, "unknown person")


@router.post("/brief")
def api_brief(req: BriefReq):
    """Computed picture plus the decision brief."""
    try:
        return brief(req.person_id, req.window_days, req.extra)
    except KeyError:
        raise HTTPException(404, "unknown person")

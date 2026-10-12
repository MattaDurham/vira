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

from . import modulemodels

import re
import statistics
from collections import Counter
from datetime import timedelta

from . import data as crm
from . import imessage, modelbudget

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


# ---------- did the last inbound message close the conversation ----------
# The waiting list used to treat "their message was last" as "the owner owes
# a reply". A tapback, "thank you, big relief", "have a great Friday" or a
# bare shared link is the other person ending the exchange, and listing it as
# owed is the nagging this module exists to remove. reply_state() reads the
# unanswered tail and says which of three it is:
#   owed      - something in the tail asks or invites a response
#   optional  - nothing asks, but nothing closes it either (a link, news)
#   ended     - the tail closes the thread (reaction, thanks, sign-off)
# Conservative by construction: any unanswered ask in the tail keeps the row
# owed even when a "thanks!" follows it, because a thanked-in-advance request
# is still a request.

# Phrases that close an exchange. A short message carrying one of these, and
# no ask, is a sign-off rather than an opening.
CLOSER = re.compile(
    r"\b(thanks|thank you|thank u|thx|tysm|ty|appreciate (?:it|you|that)|"
    r"much appreciated|big relief|what a relief|so relieved|"
    r"have (?:a |an )?(?:great|good|nice|lovely|fun|wonderful|safe|happy|"
    r"amazing|fantastic|relaxing|restful)\b[^.!?]{0,24}|"
    r"enjoy(?: it| the| your)?|take care|you too|same to you|"
    r"sounds good|sounds great|sounds perfect|got it|will do|perfect|"
    r"no worries|no problem|np|all good|all set|"
    r"talk soon|ttyl|see you(?: soon| then| there| tomorrow)?|see ya|"
    r"good ?night|night night|sleep well|love you|love ya|"
    r"congrats|congratulations|cheers|bye)\b", re.I)

# Short acknowledgements that are the whole message ("ok", "haha", "nice").
_ACK_WORD = (r"(?:ok|okay|k|kk|cool|great|awesome|nice|amazing|yes|yep|yup|"
             r"yeah|ya|sure|done|deal|for sure|of course|sweet|love it|haha+|"
             r"ha+|lol+|lmao|omg|wow|aw+|yay+|gotcha|word|bet|right|true|"
             r"indeed|exactly|agreed|night|nite|gn)")
ACK = re.compile(rf"^{_ACK_WORD}(?:[\s,.!]+{_ACK_WORD})*[\s.!]*$", re.I)

# Wording that hands the turn back without a question mark.
INVITE = re.compile(
    r"\b(guess what|big news|check (?:this|it) out|what about you|how about you|"
    r"hbu|wbu|wyd|you around|are you (?:free|around|up)|call me|text me|"
    r"hit me up|get back to me|let'?s (?:talk|chat|catch up|grab|get)|"
    r"we should)\b", re.I)

# A bare greeting opens a conversation; it is the other person's turn-taking.
GREETING = re.compile(r"^(hey|hi|hello|yo|hiya|howdy)\b[\s,!.]*\w*[\s!.]*$", re.I)

# Android/SMS tapbacks arrive as text, not as associated messages.
TEXT_TAPBACK = re.compile(
    r"^(liked|loved|laughed at|emphasized|disliked|reacted \S+ to)\s+[\"“]",
    re.I)

URL = re.compile(r"https?://\S+|\bwww\.\S+", re.I)

# A closer has to BE the message, not appear in it: "that's the perfect
# place" is a statement, "perfect, thanks!" is a sign-off. After the closing
# phrases and filler are removed, a message that OPENS with a closer may keep
# a short tail ("thanks! I'll send it tomorrow" keeps four words); one where
# the closer sits mid-sentence may keep almost nothing.
CLOSER_LEFTOVER_WORDS = 4
CLOSER_LEFTOVER_MIDDLE = 1
FILLER = {"so", "much", "very", "really", "again", "a", "lot", "and", "all",
          "guys", "man", "dude", "oh", "ok", "okay", "haha", "lol", "just",
          "too", "as", "always", "for", "everything", "it", "that", "this",
          "the", "both", "of", "you", "yall", "y'all"}


def _tail(msgs):
    """Inbound messages since the owner last did anything in the thread."""
    out = []
    for m in reversed(msgs):
        if m["from_me"]:
            break
        out.append(m)
    out.reverse()
    return out


def _closes(text):
    t = text.strip()
    if not re.search(r"[A-Za-z0-9]", t):
        return "replied with an emoji"
    if ACK.match(t):
        return "acknowledged"
    if CLOSER.search(t):
        rest = [w for w in re.findall(r"[a-z0-9']+", CLOSER.sub(" ", t.lower()))
                if w not in FILLER]
        head = t.lstrip(" \t\"'(")
        opens = bool(CLOSER.match(head) or ACK.match(re.split(r"[,.!\s]", head)[0]))
        if len(rest) <= (CLOSER_LEFTOVER_WORDS if opens else CLOSER_LEFTOVER_MIDDLE):
            return "signed off"
    return None


def reply_state(msgs):
    """owed / optional / ended for the unanswered tail of a 1:1 thread.

    Returns {"state", "why"}; `why` is a short plain phrase the waiting row
    shows, so a muted or dropped row always says what it was read as.
    """
    tail = _tail(msgs)
    if not tail:
        return {"state": "ended", "why": "you spoke last"}
    for m in tail:
        text = m["text"] or ""
        if m["reaction"] or TEXT_TAPBACK.match(text):
            continue
        kind = _classify(text) if text else None
        if kind in ("question", "request") or INVITE.search(text):
            return {"state": "owed", "why": "asks something"}
        if GREETING.match(text.strip()):
            return {"state": "owed", "why": "opened a conversation"}
    last = tail[-1]
    text = (last["text"] or "").strip()
    if last["reaction"] or TEXT_TAPBACK.match(text):
        if text.lower().startswith("questioned"):
            return {"state": "optional", "why": "questioned a message"}
        return {"state": "ended", "why": "ended with a reaction"}
    if not text:
        if last["attachments"]:
            return {"state": "optional", "why": "sent an attachment"}
        return {"state": "ended", "why": "nothing to answer"}
    if RELEASE.search(text):
        return {"state": "optional", "why": "said no reply needed"}
    bare = URL.sub("", text).strip()
    if URL.search(text) and len(bare.split()) <= 3:
        return {"state": "optional", "why": "shared a link"}
    why = _closes(text)
    if why:
        return {"state": "ended", "why": why}
    return {"state": "optional", "why": "no question asked"}


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


# HOW MANY MESSAGES the brief reads. Left where it was on purpose: unlike the
# character caps below, this one is not purely a capacity number - the facts
# block beside it describes a DEFAULT_WINDOW_D (14-day) window, and a
# transcript reaching months further back would describe a different
# conversation than the arithmetic above it. Coupled to a meaning rule, so it
# stays a literal.
BRIEF_MESSAGES = 40

# HOW THE PROMPT'S EVIDENCE BUDGET IS DIVIDED, not how big it is. The three
# blocks used to carry 4,000 / 4,000 / 12,000 characters as literals; only the
# RATIO between them was ever a judgement - the transcript is the evidence and
# the two computed blocks are its index, so the thread gets three times the
# room. The total is asked of modelbudget, because how much a prompt may carry
# is a fact about the backend answering it and not about this module.
BRIEF_SHARES = {"facts": 1, "asks": 1, "thread": 3}

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


@modulemodels.scoped("people")
def brief(pid, window_days=DEFAULT_WINDOW_D, extra=""):
    """Decision brief: computed facts first, model reasoning second."""
    from . import suggest

    facts = analyze(pid, window_days)
    if facts.get("empty"):
        return {"empty": True, "person_id": pid}

    msgs = imessage.thread_for_person(pid, limit=BRIEF_MESSAGES)
    thread = "\n".join(
        f"[{m['when'][:16] if m['when'] else '?'}] "
        f"{'me' if m['from_me'] else 'them'}: {m['text']}" for m in msgs)

    import json as _json
    lean = {k: facts[k] for k in
            ("baseline", "recent", "deltas", "bursts", "colocation_caveat")}
    owner = suggest.config().get("owner_name") or "the user"
    # "standard": a composed answer, not a card the owner is watching a
    # spinner on. Each block takes its share of what the backend can actually
    # hold; when that is small the shares shrink together, which is the point.
    unit = max(modelbudget.context_chars("standard")
               // sum(BRIEF_SHARES.values()), 1)
    prompt = BRIEF_PROMPT.format(
        owner=owner,
        facts=_json.dumps(lean, indent=1)[:unit * BRIEF_SHARES["facts"]],
        asks=_json.dumps(facts["asks"], indent=1)[:unit * BRIEF_SHARES["asks"]],
        thread=thread[:unit * BRIEF_SHARES["thread"]],
        extra=f"Guidance from {owner}: {extra}" if extra else "")

    text, backend = suggest._run(prompt, suggest.config())
    out = suggest._extract_json(text)
    out["facts"] = facts
    out["backend"] = backend
    return out



# ---------- connectors into the existing surfaces ------------------------
# Nothing below is a new surface. These feed the dossier (/api/person), the
# Daily Brief's waiting list, the reply/opener prompts in suggest.py, and the
# narration dossiers in atlas.py and reconnect.py — the same connection,
# reused, the way one measurement should light up every view that touches
# the person.

import re as _re
import time as _time

# Mirrors atlas._is_family: the person you live with is the person whose
# in-person replies the log cannot see, so their silences must never be
# scored as neglect.
_FAMILY = _re.compile(
    r"\b(family|wife|husband|spouse|partner|daughter|son|mother|father|"
    r"mom|dad|sister|brother|sibling)\b", _re.I)

_CACHE = {}
_TTL = 600


def _colocated(pid):
    c = crm._load()
    prof = c.get("profiles", {}).get(pid) or {}
    if (prof.get("relationship_class") or "").lower() == "family":
        return True
    master = c.get("master", {}).get(pid) or {}
    return bool(_FAMILY.search(str(master.get("relationship") or "")))


def enrich_person(pid):
    """The dossier's rhythm block: cached, cheap to fail, never a gate."""
    hit = _CACHE.get(pid)
    if hit and _time.time() - hit[0] < _TTL:
        return hit[1]
    a = analyze(pid, window_days=DEFAULT_WINDOW_D)
    if a.get("empty"):
        _CACHE[pid] = (_time.time(), None)
        return None
    ak = a["asks"]
    out = {
        "recent": a["recent"],
        "baseline": {k: a["baseline"].get(k) for k in
                     ("my_share_pct", "median_reply_min",
                      "my_initiation_pct")},
        "asks": {
            "pending": len(ak["pending"]),
            "released": len(ak["released"]),
            "stale": len(ak["stale"]),
            "pending_items": [
                {"when": x["when"][:16], "text": x["text"][:140]}
                for x in ak["pending"][:4]],
        },
        "burst_peak": a["bursts"]["peak"],
        "colocated": _colocated(pid),
    }
    if out["colocated"]:
        out["caveat"] = a["colocation_caveat"]
    _CACHE[pid] = (_time.time(), out)
    return out


def brief_asks(pid, hist=None):
    """The waiting row's honest count: N messages is not N obligations.
    14-day window only — the brief must stay fast across a dozen rows.
    `hist` lets a caller that already read the thread pass it in."""
    if hist is None:
        hist = messages(pid, days=DEFAULT_WINDOW_D)
    if not hist:
        return None
    ak = open_asks(hist, DEFAULT_WINDOW_D)
    pend, rel = len(ak["pending"]), len(ak["released"])
    if not pend and not rel:
        return None
    bits = []
    if pend:
        bits.append(f"{pend} real ask{'s' if pend != 1 else ''}")
    if rel:
        bits.append(f"{rel} self-released")
    return {"pending": pend, "released": rel,
            "note": " · ".join(bits),
            "colocated": _colocated(pid)}


def facts_block(pid):
    """Computed facts for the reply/opener prompts: the arithmetic the model
    reasons over instead of guessing at. Plain text, small, never raises."""
    try:
        a = analyze(pid, window_days=DEFAULT_WINDOW_D)
    except Exception:  # noqa: BLE001 — enrichment, never a gate
        return ""
    if a.get("empty"):
        return ""
    R, B, ak = a["recent"], a["baseline"], a["asks"]
    lines = ["Computed from the full message history (facts, not "
             "impressions — do not contradict them):"]
    if R.get("median_reply_min") is not None:
        lines.append(
            f"- Owner answers in median {R['median_reply_min']} min "
            f"(baseline {B.get('median_reply_min')}); owner starts "
            f"{R.get('my_initiation_pct')}% of conversations "
            f"(baseline {B.get('my_initiation_pct')}%).")
    pend, rel = ak["pending"], ak["released"]
    lines.append(f"- Open asks awaiting the owner: {len(pend)}. "
                 f"Asks whose own wording says no reply is needed: {len(rel)}.")
    for x in pend[:4]:
        lines.append(f"  - [pending {x['when'][5:16]}] {x['text'][:120]}")
    for x in rel[:2]:
        lines.append(f"  - [released {x['when'][5:16]}] {x['text'][:100]}")
    if _colocated(pid):
        lines.append("- They live together: in-person replies are invisible "
                     "here, so log silence is not evidence of neglect.")
    return "\n".join(lines)

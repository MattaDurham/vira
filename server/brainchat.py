"""Persistent, vault-grounded chat state for Find.

Find's ordinary search and one-shot Ask stay stateless.  This module owns the
other path: a session that can be resumed across browsers, plus the accumulated
Concept Cloud and Related cards derived from that same conversation.

Model calls happen outside the JSON-store lock.  The short compare-and-append
at the end rejects overlapping turns rather than silently interleaving them.
"""
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, suggest, vault

STORE = Path(__file__).resolve().parent.parent / "data" / "brain-chat.json"
MAX_SESSIONS = 20
MAX_TURNS = 40
MAX_PRIOR_CONCEPTS = 60

# THE CLASS, ONCE, FOR BOTH PROMPTS A TURN BUILDS. The answer and the concept
# pass that follows it are composed answers over retrieved material, which is
# what modelbudget's default class describes; neither is the latency-critical
# popup `interactive` is for (define.py's card is that). Sizing the two
# differently would be arbitrary -- they read the same passages in the same
# request -- so the class is named here and both ask the seam with it.
BUDGET = "standard"


class Conflict(RuntimeError):
    """The active session changed while a model answer was being produced."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _blank_store():
    return {"version": 1, "active_id": None, "sessions": {}}


def _new_session():
    now = _now()
    return {
        "id": "brain_" + secrets.token_hex(8),
        "started": now,
        "updated": now,
        "turns": [],
        "concepts": [],
        "follow_up_questions": [],
        "topic_clusters": [],
        "cited": [],
    }


def _public(session):
    """Copy the stored shape so callers cannot mutate a read result."""
    return json.loads(json.dumps(session))


def current():
    state = jsonstore.read(STORE, _blank_store())
    sid = state.get("active_id")
    session = (state.get("sessions") or {}).get(sid)
    return _public(session) if session else None


def new():
    session = _new_session()

    def update(state):
        state.setdefault("sessions", {})[session["id"]] = session
        state["active_id"] = session["id"]
        state["version"] = 1
        _prune_sessions(state)

    jsonstore.mutate(STORE, update, _blank_store(), indent=2,
                     ensure_ascii=False)
    return _public(session)


def _prune_sessions(state):
    sessions = state.get("sessions") or {}
    if len(sessions) <= MAX_SESSIONS:
        return
    oldest = sorted(sessions, key=lambda sid: sessions[sid].get("updated", ""))
    for sid in oldest[:len(sessions) - MAX_SESSIONS]:
        if sid != state.get("active_id"):
            sessions.pop(sid, None)


def _answer_question(question, prior_turns, hits):
    if not prior_turns:
        return vault.ask(question, hits=hits)
    contextual = (
        "Continue this vault-grounded conversation. Use the earlier exchange "
        "only as conversational context; support every factual claim with the "
        "retrieved vault notes and preserve [[wikilink]] citations.\n\n"
        "EARLIER EXCHANGE:\n" + _transcript(prior_turns) +
        "\n\nCURRENT QUESTION:\n" + question
    )
    return vault.ask(contextual, hits=hits)


def _transcript(prior_turns):
    """The earlier exchange, newest turn first into a budget from modelbudget.

    WAS `prior_turns[-8:]` with each question cut at 1,200 characters and
    each answer at 2,400 -- three literals typed together in one commit and
    never revisited against any window. A session STORES forty turns
    (MAX_TURNS), so a long conversation showed the model eight of them and
    truncated every one, while the backend answering it reports a million-
    token context window in its own response JSON. Nothing failed and
    nothing said anything: a model handed a conversation that appears to
    start later than it did simply answers as if it did.

    modelbudget sizes it against whatever backend will actually answer, so
    changing backends in Config re-sizes this instead of leaving a literal
    describing a machine nobody re-measured. What still does not fit is
    COUNTED in the prompt rather than dropped in silence.
    """
    from . import modelbudget
    total, per_turn = modelbudget.split(BUDGET, parts=max(len(prior_turns), 1))
    blocks, used = [], 0
    for turn in reversed(prior_turns):
        block = ("User: " + str(turn.get("question") or "")[:per_turn]
                 + "\nAssistant: " + str(turn.get("answer") or "")[:per_turn])
        # `split` FLOORS a part at a few hundred characters, so parts x
        # per_turn can exceed the total on a small window. The running total
        # is the binding constraint; the per-part figure is only a ceiling.
        if blocks and used + len(block) > total:
            break
        blocks.append(block)
        used += len(block)
    blocks.reverse()
    left = len(prior_turns) - len(blocks)
    if left:
        blocks.insert(0, f"({left} earlier turn(s) omitted -- they did not "
                         "fit this backend's context budget)")
    return "\n".join(blocks)


_CONCEPT_PROMPT = """You distill vault-chat sessions into a semantic concept cloud.

Return ONLY one JSON object with these keys:
- concepts: 8-12 concepts central to THIS turn. Each has term, weight (0..1),
  primary_path, and related_paths. Multi-word phrases are good. Avoid generic
  words. Every path must come from CHUNKS. Reuse the exact spelling of a term
  in PRIOR CONCEPTS when it is the same idea.
- follow_up_questions: exactly 3 short, concrete questions the owner might ask
  next, each at most 80 characters.
- topic_clusters: 0-3 objects with label (3-6 words) and paths. Each cluster
  needs at least 2 distinct paths from CHUNKS.

Do not invent paths. If the chunks do not support a cluster, return [].
"""


def _extract_json(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("concept model returned no JSON object")
    return json.loads(match.group(0))


def _concept_prompt(question, answer, hits, prior):
    """What the concept pass reads: the turn, and the passages that grounded it.

    WAS `hits[:MAX_CHUNKS]` with each passage cut at `MAX_CHUNK_CHARS`
    (8 x 2,400), the question at 4,000 and the answer at 8,000 -- four
    literals bounding roughly 19k characters of material, none of them ever
    compared to a window. Two were worse than merely small. `ask()`
    retrieves ten passages, so two of every ten were searched for, paid for
    and then dropped HERE with nothing said; and an answer past 8,000
    characters had its tail excluded from the very extraction it was being
    read for, which produces a thinner concept cloud rather than an error.

    modelbudget answers all four now. Question, answer and every retrieved
    passage are the parts, so the per-part figure is a ceiling and the TOTAL
    is what binds -- a short question leaves its room to the passages
    instead of wasting it.
    """
    from . import modelbudget
    total, part = modelbudget.split(BUDGET, parts=len(hits) + 2)
    q_text = question[:part]
    a_text = answer[:part]
    room = total - len(q_text) - len(a_text)
    chunks = []
    for i, hit in enumerate(hits, 1):
        heading = hit.get("heading") or hit.get("heading_path") or ""
        label = str(hit.get("path") or "(unknown)")
        if heading:
            label += " | " + str(heading)
        block = (f"--- CHUNK {i} | {label} ---\n"
                 + str(hit.get("text") or "")[:part])
        # The first passage always goes in -- an extraction pass with no
        # grounding at all is worse than one that overruns a soft ceiling.
        if chunks and len(block) > room:
            break
        chunks.append(block)
        room -= len(block)
    left = len(hits) - len(chunks)
    if left:
        chunks.append(f"({left} further passage(s) omitted -- they did not "
                      "fit this backend's context budget)")
    prior_text = ", ".join(
        f"{c.get('term')} (w={float(c.get('weight') or 0):.2f})"
        for c in prior[:MAX_PRIOR_CONCEPTS] if c.get("term")
    ) or "(none; this is the first turn)"
    return (
        _CONCEPT_PROMPT + "\nQUESTION:\n" + q_text
        + "\n\nANSWER:\n" + a_text
        + "\n\nCHUNKS:\n" + "\n\n".join(chunks)
        + "\n\nPRIOR CONCEPTS:\n" + prior_text
    )


def _validate_concepts(raw, hits):
    valid = {str(h.get("path")) for h in hits if h.get("path")}
    concepts = []
    for item in raw.get("concepts") or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        primary = str(item.get("primary_path") or "")
        weight = item.get("weight")
        if not term or primary not in valid or not isinstance(weight, (int, float)):
            continue
        related = []
        for path in item.get("related_paths") or []:
            if path in valid and path != primary and path not in related:
                related.append(path)
        concepts.append({"term": term[:120],
                         "weight": max(0.0, min(1.0, float(weight))),
                         "primary_path": primary, "related_paths": related})
    followups = [str(q).strip()[:80] for q in
                 (raw.get("follow_up_questions") or [])
                 if isinstance(q, str) and q.strip()][:3]
    clusters = []
    for item in raw.get("topic_clusters") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        paths = []
        for path in item.get("paths") or []:
            if path in valid and path not in paths:
                paths.append(path)
        if label and len(paths) >= 2:
            clusters.append({"label": label[:100], "paths": paths})
    return concepts, followups, clusters[:3]


def _merge_concepts(prior, incoming):
    out = [dict(c, related_paths=list(c.get("related_paths") or []))
           for c in prior]
    by_term = {str(c.get("term") or "").lower().strip(): c for c in out}
    for item in incoming:
        key = item["term"].lower().strip()
        old = by_term.get(key)
        if old:
            old["turns"] = int(old.get("turns") or 1) + 1
            old["weight"] = min(
                1.0, max(float(old.get("weight") or 0), item["weight"])
                + 0.05 * (old["turns"] - 1))
            for path in item.get("related_paths") or []:
                if path != old.get("primary_path") and path not in old["related_paths"]:
                    old["related_paths"].append(path)
        else:
            added = dict(item, turns=1)
            out.append(added)
            by_term[key] = added
    return out


def _merge_cited(prior, citations, turn_number):
    by_path = {c.get("path"): dict(c) for c in prior if c.get("path")}
    for cite in citations:
        path = cite.get("path")
        if not path:
            continue
        item = by_path.setdefault(path, {
            "path": path, "title": cite.get("title") or Path(path).stem,
            "count": 0, "last_cited_in_turn": turn_number,
        })
        item["count"] = int(item.get("count") or 0) + 1
        item["last_cited_in_turn"] = turn_number
        if cite.get("title"):
            item["title"] = cite["title"]
    return sorted(by_path.values(),
                  key=lambda c: c.get("last_cited_in_turn", 0), reverse=True)


def ask(question, session_id=None):
    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")

    state = jsonstore.read(STORE, _blank_store())
    sid = session_id or state.get("active_id")
    session = (state.get("sessions") or {}).get(sid)
    if not session:
        session = _new_session()
        sid = session["id"]
        expected_turns = 0
    else:
        session = _public(session)
        expected_turns = len(session.get("turns") or [])

    research_result = None
    try:
        from . import research
        if research.may_answer(question):
            research_result = research.answer_question(question)
    except Exception:  # a dormant graph must never break ordinary vault chat
        research_result = None
    if research_result:
        hits = research_result.get("hits") or []
        answer = {
            "answer": research_result.get("answer") or "",
            "citations": research_result.get("citations") or [],
            "hits": hits,
        }
    else:
        # ONE retrieval, read by both prompts. It was a bare `limit=10`
        # sitting beside a MAX_CHUNKS of 8, so the answer prompt and the
        # concept prompt read different sets and nobody could see it.
        # vault.ask_hits() is the single answer, budgeted against the
        # backend that will read it and the engine's own prompt ceiling.
        hits = vault.search(question, limit=vault.ask_hits(BUDGET))
        answer = _answer_question(question, session.get("turns") or [], hits)
    answer_text = str(answer.get("answer") or "")

    concepts, followups, clusters = [], [], []
    if answer_text.strip() and hits:
        try:
            raw = _extract_json(suggest.complete(_concept_prompt(
                question, answer_text, hits, session.get("concepts") or [])))
            concepts, followups, clusters = _validate_concepts(raw, hits)
        except Exception:  # the answer is useful even if its companions fail
            pass

    turn_number = expected_turns + 1
    turn = {
        "question": question,
        "answer": answer_text,
        "citations": answer.get("citations") or [],
        "hits": answer.get("hits") or [],
        "created": _now(),
    }
    if research_result:
        turn["research"] = {
            key: value for key, value in research_result.items()
            if key not in {"answer", "hits", "citations"}
        }
    session["turns"] = (session.get("turns") or []) + [turn]
    session["turns"] = session["turns"][-MAX_TURNS:]
    session["concepts"] = _merge_concepts(session.get("concepts") or [], concepts)
    session["follow_up_questions"] = followups
    session["topic_clusters"] = clusters
    session["cited"] = _merge_cited(session.get("cited") or [],
                                    turn["citations"], turn_number)
    session["updated"] = _now()

    def commit(latest):
        sessions = latest.setdefault("sessions", {})
        found = sessions.get(sid)
        if found is not None and len(found.get("turns") or []) != expected_turns:
            raise Conflict("another chat turn finished first")
        if found is None and expected_turns:
            raise Conflict("chat session changed while answering")
        sessions[sid] = session
        latest["active_id"] = sid
        latest["version"] = 1
        _prune_sessions(latest)

    jsonstore.mutate(STORE, commit, _blank_store(), indent=2,
                     ensure_ascii=False)
    return _public(session)

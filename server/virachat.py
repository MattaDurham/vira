"""Chat with Vira - a conversation over EVERYTHING Vira holds (2026-09-01,
branch claude/chat-with-vira).

Find's Chat used to be vault-only by construction: brainchat.py retrieved
from qocha, answered from those chunks, and validated every citation
against a vault path. That shape was written when everything in Vira was
going to be in the vault. It is not - messages, mail, shared media with
their OCR, contacts, the calendar, the brief and the ideas backlog live in
their own stores and reach a session only through the mcp__vira__* tools.
So a chat that must reach everything IS a live agent session on those
tools, and this module is the thin layer that makes a session read as a
conversation: one turn in, one answer out, what it looked at beside it.

- THE ENGINE IS THE SESSION HARNESS, unchanged. `send` launches ONE
  session per chat and every later turn is `sessions.say` into it. A
  finished turn PARKS in the reply window, which is exactly the state a
  conversation wants; a chat resumed after the window closed continues
  through `_resume_ended` by session id, so the transcript is never lost.
- IT RUNS ON THE DEFAULT RUNG, NOT READ-ONLY (owner's ruling, 2026-09-01,
  after the first live chat failed a question about his subscriptions).
  The first cut launched `read_only=True`: the vira tools are auto-allowed
  either way, so it cost no cards - what it cost was Bash and the HTTP
  API. The session could SEE that /api/subs held the full ledger and could
  not call it, so it counted from the brief's five-row slice and said so.
  Read-only is the plan session's contract, not a chat's; a chat is an
  owner session and gets what every Implement and Ask-Vira dispatch gets:
  `session_default_mode`, decided in config, never hardcoded here. cwd is
  the home directory, so branch-first placement never fires and no
  worktree is minted for a conversation.
- THE ANSWER ARRIVES AT THE TURN BOUNDARY. `_follow` polls the job
  snapshot the way inbound.py's reply follower does: settled means the
  session is parked again (or ended) and the published result differs
  from the answer this chat already holds.
- WHAT IT LOOKED AT IS DATA, NOT A GUESS. The runner records every tool
  call with the turn it belongs to (state["tools"]); `looked_at` turns
  those into cards the client can open - the find query, the note, the
  person. Nothing is inferred from the prose.
- CITATIONS ARE RESOLVED EXACTLY. A [[wikilink]] in the answer is resolved
  through vault.resolve_ref (the exact-stem rule), and one that resolves
  only by search is marked so; nothing is passed off as the link.
- The concept pass is ONE model call over the turn, the same shape
  brainchat used, with one change: a concept needs no vault path. A term
  with a cited note opens on that note; one without opens as a Find over
  everything, which is what a concept means in a chat that spans it all.

Model-call class is reply drafting, so passive instances answer too - but
a passive instance runs no supervisor, so a turn there can only report
that the session could not start.
"""
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agentbackend, jsonstore, settings

STORE = Path(__file__).resolve().parent.parent / "data" / "vira-chat.json"
MAX_SESSIONS = 20
MAX_TURNS = 60
MAX_PRIOR_CONCEPTS = 60
# A turn that has not settled by this is reported as failed rather than
# left pending forever - the compose box must never wedge shut. A real
# multi-tool turn on this machine runs 20-90s; ten minutes is far past any
# honest answer.
TURN_MAX_S = 600
POLL_S = 1.0
BUDGET = "standard"


class Busy(RuntimeError):
    """A turn is already in flight on this chat."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _blank():
    return {"version": 1, "active_id": None, "sessions": {}}


def _new_session():
    now = _now()
    return {"id": "chat_" + secrets.token_hex(8), "started": now,
            "updated": now, "job_id": "", "turns": [], "concepts": [],
            "follow_up_questions": [], "cited": []}


def _public(s):
    return json.loads(json.dumps(s))


def _load():
    return jsonstore.read(STORE, _blank())


def _prune(state):
    sessions = state.get("sessions") or {}
    if len(sessions) <= MAX_SESSIONS:
        return
    oldest = sorted(sessions, key=lambda k: sessions[k].get("updated", ""))
    for sid in oldest[:len(sessions) - MAX_SESSIONS]:
        if sid != state.get("active_id"):
            sessions.pop(sid, None)


def _mutate(fn):
    jsonstore.mutate(STORE, fn, _blank(), indent=2, ensure_ascii=False)


def summary_rows(state=None):
    """Every chat, newest first, for the picker: id, when, the first
    question, how many turns."""
    state = state or _load()
    rows = []
    for s in (state.get("sessions") or {}).values():
        turns = s.get("turns") or []
        rows.append({"id": s["id"], "started": s.get("started"),
                     "updated": s.get("updated"), "turns": len(turns),
                     "title": (turns[0]["question"] if turns else "New chat")[:80]})
    return sorted(rows, key=lambda r: r.get("updated") or "", reverse=True)


def current():
    state = _load()
    s = (state.get("sessions") or {}).get(state.get("active_id"))
    return _with_progress(_public(s)) if s else None


def new():
    s = _new_session()

    def up(state):
        state.setdefault("sessions", {})[s["id"]] = s
        state["active_id"] = s["id"]
        _prune(state)
    _mutate(up)
    return _public(s)


def switch(sid):
    def up(state):
        if sid not in (state.get("sessions") or {}):
            raise KeyError(sid)
        state["active_id"] = sid
    _mutate(up)
    return current()


# ---------- the session behind a chat ----------

def _owner():
    return settings.get("owner_name") or "the owner"


CHAT_BRIEF = """This is a CHAT with {owner} inside Vira - a conversation, not a task. \
Answer the message below directly, in plain prose, in a few sentences \
unless more is genuinely asked for.

Before answering anything about {owner}'s life or records, LOOK IT UP with \
the {tool_prefix}* tools - {tool_prefix}find first (it spans notes, media with \
OCR, people and the text of messages and mail), then the single-corpus \
tools when you know where the answer lives (calendar, daily_brief, \
crm_lookup, imessage_thread, mail_search, media_search, vault_search, \
vault_note, list_ideas). Never answer from memory what a tool can answer \
from the data. When a tool returns a SLICE (the daily brief shows five \
renewals, not the ledger), go to the whole thing: Vira's HTTP API on \
http://localhost:8377 serves every store raw and you may call it with \
Bash - GET /api/subs (the full subscriptions ledger), /api/brief, \
/api/people?q=, /api/person/<id>, /api/find?q=, /api/applications, \
/api/reading/list, /api/ideas, /api/attention. Name people, dates and \
the thing you found; when a vault note grounds a claim, cite it as a \
[[wikilink]] to its path. Never invent a fact, a date or a document. If \
nothing in the data answers, say so plainly and name what you searched. \
You can act as well as answer - draft, file, look things up, run what is \
needed - and when an action is genuinely {owner}'s call, ask with \
{tool_prefix}ask_owner rather than guessing.

Do not narrate your tool calls or your plan, and do not end with offers or \
status - the reply box under this chat stays open on its own.

{owner} says:
{question}"""


CHAT_BRIEF_HTTP = """This is a CHAT with {owner} inside Vira - a conversation, not a task. \
Answer the message below directly, in plain prose, in a few sentences \
unless more is genuinely asked for.

Before answering anything about {owner}'s life or records, LOOK IT UP - \
Vira's HTTP API on http://localhost:8377 is your data access: \
GET /api/find?q=<query> spans notes, shared media with OCR, people and the \
text of messages and mail; GET /api/brief is the calendar and who is \
waiting; GET /api/people?q=<name> and GET /api/person/<id> are the CRM. \
Never answer from memory what the data can answer. Name people, dates \
and the thing you found; never invent a fact, a date or a document. If \
nothing answers, say so plainly and name what you searched.

Do not narrate your calls or your plan, and do not end with offers or \
status - the reply box under this chat stays open on its own.

{owner} says:
{question}"""


def _launch_prompt(question, native=True, provider="anthropic"):
    brief = CHAT_BRIEF if native else CHAT_BRIEF_HTTP
    prefix = "mcp__vira__" if provider == "anthropic" else "vira."
    return brief.format(owner=_owner(), question=question.strip(),
                        tool_prefix=prefix)


def _session_snapshot(job_id):
    try:
        from . import session
        return session.sessions.get(job_id)
    except Exception:  # noqa: BLE001 - a missing registry reads as no session
        return None


def chat_provider():
    """Choose the owner's session-capable go-to with native Vira tools.

    Claude and Codex now consume the same registry, so Chat no longer has a
    reason to override an OpenAI go-to merely to gain data access. A future
    provider enters this path by declaring session quality, not by adding a
    new name check here. Returns (provider, native_tools).
    """
    from . import models
    want = str(settings.raw().get("ai_provider") or "").strip().lower()
    if want in models.PROVIDERS and models.is_disabled(want):
        # Outside the try below on purpose: the broad except is for a
        # broken PROBE, and a disabled go-to is a decision, not a failure.
        # A chat opened against a disabled go-to refuses by name (the
        # refusal becomes the turn's answer) instead of quietly running on
        # whichever other provider is connected.
        raise models.ProviderDisabled(want, role="the configured go-to")
    try:
        connected = [p.get("id") for p in models.connected()]
        ordered = ([want] if want in connected else []) + connected
        for pid in ordered:
            quality = agentbackend.sessions_quality(pid)
            if quality:
                return pid, quality == "gated"
    except Exception:  # noqa: BLE001 - a broken probe falls to the default
        pass
    return None, False


def _open_session(job_id, question):
    """Start the chat's session or continue it. Returns the job id the
    conversation lives under now (say() may resume under a NEW id)."""
    from . import session
    if not job_id:
        model = (settings.get("chat_model") or "").strip() or None
        provider, native = chat_provider()
        q = " ".join((question or "").split())
        return session.sessions.launch(
            _launch_prompt(question, native, provider or ""), model=model,
            provider=provider, meta={"kind": "chat"},
            subject=q[:140],
            about=f"A conversation with Vira, opened with: {q[:600]}")
    out = session.sessions.say(job_id, question)
    return out.get("job") or job_id


def send(question, session_id=None):
    """Append a pending turn and drive the session; the answer lands via
    `_follow` on a daemon thread. Returns the session with the turn
    pending so the client can render it and poll."""
    question = (question or "").strip()
    if not question:
        raise ValueError("empty message")
    state = _load()
    sid = session_id or state.get("active_id")
    s = (state.get("sessions") or {}).get(sid)
    if s is None:
        s = _new_session()
        sid = s["id"]
    if any(t.get("status") == "pending" for t in s.get("turns") or []):
        raise Busy("Vira is still answering the last message")
    prior = ""
    for t in reversed(s.get("turns") or []):
        if t.get("status") == "done":
            prior = t.get("answer") or ""
            break
    turn = {"question": question, "answer": "", "status": "pending",
            "created": _now(), "sent_t": time.time(),
            "looked_at": [], "citations": []}
    idx = len(s.get("turns") or [])
    job_id = s.get("job_id") or ""
    error = ""
    try:
        job_id = _open_session(job_id, question)
    except Exception as e:  # noqa: BLE001 - the refusal is the turn's answer
        error = str(e)[:400]
    if error:
        turn["status"] = "failed"
        turn["answer"] = "I could not start the conversation: " + error
    s["job_id"] = job_id
    s["turns"] = ((s.get("turns") or []) + [turn])[-MAX_TURNS:]
    s["updated"] = _now()
    idx = len(s["turns"]) - 1

    def up(st):
        st.setdefault("sessions", {})[sid] = s
        st["active_id"] = sid
        _prune(st)
    _mutate(up)
    if not error:
        threading.Thread(target=_follow,
                         args=(sid, idx, job_id, prior, turn["sent_t"]),
                         daemon=True, name="vira-chat-follow").start()
    return _with_progress(_public(s))


# ---------- following a turn to its answer ----------

def _settled(snap):
    status = snap.get("status")
    return snap.get("awaiting") in ("reply", "paused") or status != "running"


def _follow(sid, idx, job_id, prior, sent_t=0.0, max_s=TURN_MAX_S,
            poll_s=POLL_S, clock=time.time, sleep=time.sleep):
    """Watch the session until this turn's answer is published, then file
    it with what the turn looked at and the concept pass. Runs OUTSIDE
    the store lock; only the final write takes it.

    ATTRIBUTION IS BY TIME, not by the runner's turn counter. The counter
    moves when the runner DELIVERS a reply, which is after this follower
    starts, and a turn that makes no calls of its own leaves the newest
    recorded call belonging to the previous turn - on the second live turn
    that handed the new answer the old answer's seven cards. A call made
    after the message was sent belongs to this turn; nothing else does."""
    end = clock() + max_s
    saw_working = False
    answer, failed = "", ""
    while clock() < end:
        snap = _session_snapshot(job_id)
        if snap is None:
            failed = "the session is gone"
            break
        if snap.get("status") == "running" and snap.get("awaiting") not in ("reply", "paused"):
            saw_working = True
        out = (snap.get("result_text") or "").strip()
        if _settled(snap) and out and (out != prior or saw_working):
            answer = out
            break
        if snap.get("status") not in ("running", None):
            failed = snap.get("error") or f"the session ended ({snap.get('status')})"
            break
        sleep(poll_s)
    else:
        failed = f"no answer after {int(max_s)}s"
    if not answer and not failed:
        failed = "the session ended without an answer"
    snap = _session_snapshot(job_id) or {}
    looked = looked_at(snap, since_t=sent_t)
    cites = citations(answer) if answer else []
    concepts, followups = [], []
    if answer:
        try:
            concepts, followups = _concepts(sid, answer, cites)
        except Exception:  # noqa: BLE001 - the answer is useful without them
            pass
    _finish_turn(sid, idx, answer, failed, looked, cites, concepts, followups)


def _finish_turn(sid, idx, answer, failed, looked, cites, concepts, followups):
    def up(st):
        s = (st.get("sessions") or {}).get(sid)
        if not s or idx >= len(s.get("turns") or []):
            return
        t = s["turns"][idx]
        if t.get("status") != "pending":
            return
        t["answer"] = answer if answer else ("Vira could not answer: " + failed)
        t["status"] = "done" if answer else "failed"
        t["looked_at"] = looked
        t["citations"] = cites
        t["finished"] = _now()
        if answer:
            s["concepts"] = _merge_concepts(s.get("concepts") or [], concepts)
            if followups:
                s["follow_up_questions"] = followups
            s["cited"] = _merge_cited(s.get("cited") or [], cites, idx + 1)
        s["updated"] = _now()
    _mutate(up)


def _with_progress(s):
    """Live progress for a pending turn: the tool calls the session has
    made so far, as labels - read off the job, never stored."""
    if not s:
        return s
    pending = [t for t in s.get("turns") or [] if t.get("status") == "pending"]
    if pending and s.get("job_id"):
        snap = _session_snapshot(s["job_id"]) or {}
        since = float(pending[-1].get("sent_t") or 0)
        rows = [r for r in snap.get("tools") or []
                if float(r.get("t") or 0) >= since and not _harness(r)]
        s["progress"] = [_label(r) for r in rows][-6:]
        s["live"] = bool(snap)
    return s


# ---------- what the turn looked at ----------

def _label(row):
    name = (row.get("name") or "").replace("mcp__vira__", "")
    inp = row.get("input") or {}
    what = inp.get("query") or inp.get("q") or inp.get("path") \
        or inp.get("name") or inp.get("person") or ""
    return f"{name}: {what}" if what else name


# The harness's own calls - loading deferred tools, planning - are not
# something the session looked AT.
HARNESS_TOOLS = {"ToolSearch", "TodoWrite", "Task", "Read", "Glob", "Grep",
                 "WebFetch", "WebSearch"}


def _harness(row):
    name = row.get("name") or ""
    return name in HARNESS_TOOLS or name.startswith(("Read", "Glob", "Grep"))


def looked_at(snap, since_t=0.0):
    """Cards for the tool calls made SINCE the message was sent. Each names
    the surface that shows the same thing the session saw, so a reader can
    go and look - the honest form of 'sources' for a session, since what
    it READ is a fact and what it was 'grounded in' is a claim."""
    out, seen = [], set()
    for r in snap.get("tools") or []:
        if float(r.get("t") or 0) < float(since_t or 0) or _harness(r):
            continue
        card = _card(r)
        if not card:
            continue
        key = (card["kind"], card.get("query") or card.get("path")
               or card.get("pid") or card.get("label"))
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out[:12]


def _person(name):
    try:
        from . import data as crm
        hits = crm.search_people(name, limit=3) or []
        hit = next((h for h in hits
                    if (h.get("name") or "").lower() == name.lower()), None)
        hit = hit or (hits[0] if len(hits) == 1 else None)
        if hit and hit.get("id"):
            return hit["id"], hit.get("name") or name
    except Exception:  # noqa: BLE001 - a card can still say the name
        pass
    return None, name


def _card(row):
    name = (row.get("name") or "").replace("mcp__vira__", "")
    inp = row.get("input") or {}
    q = inp.get("query") or inp.get("q") or ""
    if name == "find":
        return {"kind": "find", "label": q, "query": q}
    if name == "vault_search":
        return {"kind": "find", "label": q, "query": q, "tab": "notes"}
    if name == "media_search":
        return {"kind": "find", "label": q, "query": q, "tab": "media"}
    if name == "mail_search":
        return {"kind": "find", "label": q, "query": q, "tab": "messages"}
    if name == "vault_note":
        p = inp.get("path") or ""
        return {"kind": "note", "label": Path(p).stem or p, "path": p} if p else None
    if name in ("crm_lookup", "imessage_thread"):
        who = inp.get("name") or inp.get("person") or ""
        if not who:
            return None
        pid, label = _person(who)
        return {"kind": "person", "label": label, "pid": pid,
                "detail": "messages" if name == "imessage_thread" else "profile"}
    if name in ("calendar", "daily_brief"):
        return {"kind": "brief", "label": "calendar" if name == "calendar" else "daily brief"}
    if name == "list_ideas":
        return {"kind": "queue", "label": "the ideas backlog"}
    if (row.get("name") or "").startswith("mcp__vira__"):
        return {"kind": "tool", "label": _label(row)}
    return None


# ---------- citations ----------

_WIKI = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")


def citations(answer):
    """Every [[wikilink]] in the answer, resolved EXACTLY through
    vault.resolve_ref; a search-only match is kept but marked inexact, so
    the client can say 'closest match' instead of passing it off."""
    out, seen = [], set()
    for m in _WIKI.finditer(answer or ""):
        ref = m.group(1).strip()
        if not ref or ref.lower() in seen:
            continue
        seen.add(ref.lower())
        hit = None
        try:
            from . import vault
            hit = vault.resolve_ref(ref)
        except Exception:  # noqa: BLE001 - a dormant vault cites nothing
            hit = None
        if hit and hit.get("path"):
            out.append({"ref": ref, "path": hit["path"],
                        "title": Path(hit["path"]).stem,
                        "exact": bool(hit.get("exact", True))})
        else:
            out.append({"ref": ref, "path": None, "title": ref, "exact": False})
    return out


# ---------- the concept pass ----------

_CONCEPT_PROMPT = """You distill one turn of a chat into a concept cloud.

Return ONLY one JSON object with these keys:
- concepts: 6-10 concepts central to THIS turn - the people, things, places, \
decisions and themes it is actually about. Each has term (a short phrase, the \
spelling the turn uses), weight (0..1), and note (one of NOTES verbatim when \
that note is what grounds the concept, else null). Reuse the exact spelling \
of a term in PRIOR CONCEPTS when it is the same idea.
- follow_up_questions: exactly 3 short, concrete questions the owner might \
ask next, each at most 80 characters, answerable from their own records.

Never invent a note path.
"""


def _concepts(sid, answer, cites):
    from . import modelbudget, suggest
    state = _load()
    s = (state.get("sessions") or {}).get(sid) or {}
    turns = s.get("turns") or []
    question = turns[-1]["question"] if turns else ""
    total, part = modelbudget.split(BUDGET, parts=3)
    prior = ", ".join(f"{c.get('term')} (w={float(c.get('weight') or 0):.2f})"
                      for c in (s.get("concepts") or [])[:MAX_PRIOR_CONCEPTS]
                      if c.get("term")) or "(none; this is the first turn)"
    notes = [c["path"] for c in cites if c.get("path")]
    prompt = (_CONCEPT_PROMPT + "\nQUESTION:\n" + question[:part]
              + "\n\nANSWER:\n" + answer[:part]
              + "\n\nNOTES:\n" + ("\n".join(notes) or "(none)")
              + "\n\nPRIOR CONCEPTS:\n" + prior)
    raw = suggest.complete(prompt)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return [], []
    return _validate(json.loads(m.group(0)), notes)


def _validate(raw, notes):
    valid = set(notes)
    out = []
    for item in raw.get("concepts") or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        w = item.get("weight")
        if not term or not isinstance(w, (int, float)):
            continue
        note = item.get("note")
        note = note if isinstance(note, str) and note in valid else None
        out.append({"term": term[:120], "weight": max(0.0, min(1.0, float(w))),
                    "primary_path": note})
    followups = [str(q).strip()[:80] for q in (raw.get("follow_up_questions") or [])
                 if isinstance(q, str) and q.strip()][:3]
    return out[:10], followups


def _merge_concepts(prior, incoming):
    out = [dict(c) for c in prior]
    by_term = {str(c.get("term") or "").lower().strip(): c for c in out}
    for item in incoming:
        key = item["term"].lower().strip()
        old = by_term.get(key)
        if old:
            old["turns"] = int(old.get("turns") or 1) + 1
            old["weight"] = min(1.0, max(float(old.get("weight") or 0), item["weight"])
                                + 0.05 * (old["turns"] - 1))
            if item.get("primary_path") and not old.get("primary_path"):
                old["primary_path"] = item["primary_path"]
        else:
            added = dict(item, turns=1)
            out.append(added)
            by_term[key] = added
    return out


def _merge_cited(prior, cites, turn_number):
    by_path = {c.get("path"): dict(c) for c in prior if c.get("path")}
    for c in cites:
        path = c.get("path")
        if not path:
            continue
        item = by_path.setdefault(path, {"path": path, "title": c.get("title")
                                         or Path(path).stem, "count": 0,
                                         "last_cited_in_turn": turn_number})
        item["count"] = int(item.get("count") or 0) + 1
        item["last_cited_in_turn"] = turn_number
    return sorted(by_path.values(),
                  key=lambda c: c.get("last_cited_in_turn", 0), reverse=True)

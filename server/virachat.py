"""Durable conversations backed by the native session harness.

The answer and useful provider messages are published independently of optional
source/concept decoration. Stable turn IDs fence late callbacks; ordinary reads
recover settled answers from durable runner state. History is retained in full
and paginated only at the HTTP boundary. Evidence scope and the prompt contract
are recorded per conversation, and exact citations reopen immutable read spans.
A module model change starts a new native session with bounded saved conversation
context, preserving the visible chat and the earlier job records.
Each instance runs its own supervisor; prompts and HTTP lookups stay on the
instance that owns the conversation.
"""
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agentbackend, instance, jsonstore, modelbudget, modulemodels, settings

STORE = Path(__file__).resolve().parent.parent / "data" / "vira-chat.json"
# History is never trimmed. These are response page sizes, not retention limits.
HISTORY_PAGE = 30
PROMPT_VERSION = "vira-chat-v2"
MAX_PRIOR_CONCEPTS = 60
# A turn that has not settled by this is reported as failed rather than
# left pending forever - the compose box must never wedge shut. A real
# multi-tool turn on this machine runs 20-90s; ten minutes is far past any
# honest answer.
TURN_MAX_S = 600
# Optional source/concept decoration gets its own deadline; it never owns
# the answer or the compose box. Late results are discarded after this.
ENRICHMENT_MAX_S = 45
# Held until a worker actually exits, including after its publication deadline.
_ENRICHMENT_SLOTS = threading.BoundedSemaphore(2)
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


def _public(s, *, before=None, limit=HISTORY_PAGE):
    out = json.loads(json.dumps(s))
    if not isinstance(out, dict) or "turns" not in out:
        return out
    turns = out["turns"]
    end = len(turns) if before is None else max(0, min(int(before), len(turns)))
    start = max(0, end - max(1, min(int(limit), 100)))
    out["turns"] = turns[start:end]
    out["history"] = {"total": len(turns), "start": start, "end": end,
                      "before": start if start else None}
    return out


def _load():
    return jsonstore.read(STORE, _blank())


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


def current(session_id=None, before=None, limit=HISTORY_PAGE):
    state = _load()
    s = (state.get("sessions") or {}).get(session_id or state.get("active_id"))
    if s:
        _reconcile(s)
        s = (_load().get("sessions") or {}).get(s["id"])
    return _with_progress(_public(s, before=before, limit=limit)) if s else None


def new():
    s = _new_session()

    def up(state):
        state.setdefault("sessions", {})[s["id"]] = s
        state["active_id"] = s["id"]
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


CHAT_BRIEF = """You are answering {owner} in Vira. Answer the question directly;
ordinary chat is not a request for a report, dossier, or saved artifact.
This conversation belongs to the Vira instance at {api_url}.

Evidence scope: {scope}. Use only sources enabled for model answers and within
this scope. The {tool_prefix}* tools enforce this policy. Do not bypass an
excluded source through shell, raw HTTP, another agent, or a cached summary.
Use {tool_prefix}answer_sources to discover available sources and their dates.
Start with the narrowest useful tool: calendar for dates, a person's messages
for a conversation, source search for a named document; use {tool_prefix}find
when the location is unknown. Independent lookups may run in parallel.

Choose the research depth the question needs (requested mode: {mode}):
- Lookup: read the decisive passage and answer promptly with its date.
- Synthesis: compare independent originals across the relevant period, then
  check a recent example and counterevidence. Derived summaries are leads;
  repeated copies of one event are not independent corroboration.
- Count/rank: enumerate and count the complete filtered population; relevance
  search is not a denominator. If coverage is partial, label the result a
  qualitative pattern, not a measured top three or an exact count.
- Deep research: use batched reads, source search, and continuation cursors.
  A truncated passage or a recent-message slice is not the complete record.
Distinguish event dates from file modification dates and source versions.
Read original passages before making strong claims. Treat conflicting accounts
as accounts, not established motives or diagnoses. Say what is unknown.

Cite concrete claims using the evidence_handle returned by a read, as
[[evidence:ev_HASH|short source label]]. This opens the exact version and span
read. Never invent a handle. Legacy [[path]] links are navigation only.
Every 15-30 seconds of substantial work, give a brief useful progress update
about evidence found or an unresolved gap. Publish a supported preliminary
answer when ready and mark it provisional if checks remain. Aim to answer
ordinary questions within three minutes; explain a remaining gap rather than
silently extending research. Do not expose private reasoning or raw tool logs.

You can also perform actions the owner requests, using the configured session
permissions. Ask with {tool_prefix}ask_owner when a necessary choice belongs
to the owner. Do not create or file a report unless one was requested.

{owner} says:
{question}"""

CHAT_BRIEF_HTTP = """You are answering {owner} in Vira. Answer directly; ordinary
chat is not a report request. Requested mode: {mode}. Evidence scope: {scope}.
Use Vira's model-scoped evidence API at {api_url}/api/answer:
GET /sources, POST /read (source, start, length), POST /search (source, query),
GET /evidence/<handle>. These preserve source permissions and evidence versions.
Use only allowed sources. Never bypass exclusions through raw stores or files.
Read decisive original passages; follow continuation cursors for complete
coverage. Compare independent sources, current evidence and counterexamples
for a synthesis. Use enumeration for exact counts, and label incomplete ranks
as qualitative. Distinguish event dates from modification dates and conflicting
accounts from established facts. Cite [[evidence:ev_HASH|source label]] using
returned handles. Share useful progress every 15-30 seconds and publish a
supported preliminary answer promptly if research continues. Say what remains
unknown; never invent a source. Do not file an unrequested artifact.

{owner} says:
{question}"""


def _launch_prompt(question, native=True, provider="anthropic", *,
                   mode="auto", sources=None, chat_id=None):
    brief = CHAT_BRIEF if native else CHAT_BRIEF_HTTP
    prefix = "mcp__vira__" if provider == "anthropic" else "vira."
    scope = ", ".join(sources or []) or "all sources enabled for model answers"
    return brief.format(owner=_owner(), question=question.strip(),
                        tool_prefix=prefix, mode=mode, scope=scope,
                        api_url=instance.api_url()) + (
                            "\nFor every /api/answer request include ?session_id=" + chat_id
                            if chat_id and not native else "")


def _session_snapshot(job_id):
    try:
        from . import jobfiles, session
        snap = session.sessions.get(job_id)
        if snap is not None:
            return snap
        # Finished runners can leave the in-memory registry while their
        # durable answer remains available, including after a restart.
        return jsonstore.read(jobfiles.job_dir(job_id) / "state.json", None)
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


def _chat_model():
    """The Find pick is read for every message, including existing chats."""
    from . import session
    choice = modulemodels.selection("find")
    if choice:
        provider, model = session.module_session_model(choice)
        return {"provider": provider, "model": model or "",
                "backend": choice["backend"]}
    return default_model_selection()


def default_model_selection():
    """Find's inherited choice, shared with the picker status readout."""
    model = (settings.get("chat_model") or "").strip() or None
    if model:
        provider = agentbackend.session_provider(model=model)
    else:
        provider, _native = chat_provider()
        provider = provider or agentbackend.default_session_provider()
        with modulemodels.scope(None):
            model = agentbackend.default_model(provider)
    return {"provider": provider, "model": model or "",
            "backend": "cli" if provider in ("anthropic", "openai") else "api"}


def _conversation_context(turns, route):
    """Carry saved messages across engines, with any bound stated plainly."""
    with modulemodels.scope("find"):
        remaining = modelbudget.context_chars(
            BUDGET, route["provider"], route["backend"], model=route["model"])
    pieces = []
    truncated = False
    completed = [turn for turn in turns or [] if turn.get("status") == "done"]
    for index, turn in enumerate(reversed(completed)):
        piece = ("Owner: " + (turn.get("question") or "")
                 + "\nVira: " + (turn.get("answer") or ""))
        if len(piece) > remaining:
            pieces.append("[Earlier text omitted]\n" + piece[-remaining:])
            truncated = True
            break
        pieces.append(piece)
        remaining -= len(piece) + 2
        if remaining <= 0:
            truncated = index + 1 < len(completed)
            break
    return "\n\n".join(reversed(pieces)), truncated



def _open_session(job_id, question, *, route=None, prior_route=None, turns=(),
                  mode="auto", sources=None, chat_id=None):
    """Return (job id, model changed, carried context truncated).

    An engine change starts a new run with the same evidence and answer contract.
    Resuming an expired native session can also change its job ID; that alone is
    not a model change.
    """
    from . import session
    route = route or _chat_model()
    changed = bool(job_id and prior_route is not None and route != prior_route)
    if job_id and prior_route is None and modulemodels.selection("find"):
        snap = _session_snapshot(job_id) or {}
        changed = (snap.get("provider") != route["provider"]
                   or (snap.get("model") or "") != route["model"])
    if not job_id or changed:
        provider, model = route["provider"], route["model"] or None
        native = bool(agentbackend.capabilities(provider).get("native_tools"))
        q = " ".join((question or "").split())
        message = question
        truncated = False
        if changed:
            transcript, truncated = _conversation_context(turns, route)
            message = (
                "The owner changed the model for this chat. Continue the same "
                "conversation using these saved messages as context. Prior "
                "actions are history; do not repeat them. Only the visible "
                "messages are carried across, not the prior engine's hidden "
                "state.\n\nPRIOR CONVERSATION:\n"
                + (transcript or "(no completed messages)")
                + "\n\nCURRENT MESSAGE:\n" + question)
        new_job = session.sessions.launch(
            _launch_prompt(message, native, provider or "", mode=mode, sources=sources, chat_id=chat_id), model=model,
            effort=settings.raw().get("chat_effort") or None,
            runtime={"prompt_version": PROMPT_VERSION, "chat_id": chat_id, "evidence_scope": {"sources": sources or []},
                     "answer_mode": mode, "work_class": "foreground",
                     "latency_budget": {"first_update_s": 15, "answer_s": 180, "hard_limit_s": TURN_MAX_S}},
            provider=provider, meta={"kind": "chat"},
            subject=q[:140],
            about=f"A conversation with Vira, opened with: {q[:600]}")
        return new_job, changed, truncated
    out = session.sessions.say(job_id, question)
    return out.get("job") or job_id, False, False


def send(question, session_id=None, *, mode=None, sources=None):
    """Reserve a turn atomically, then launch its session outside the lock."""
    question = (question or "").strip()
    if not question:
        raise ValueError("empty message")
    if mode is not None and mode not in ("auto", "lookup", "synthesis", "count", "deep"):
        raise ValueError("unknown answer mode")
    if sources is not None and (not isinstance(sources, list) or not all(isinstance(x, str) for x in sources)):
        raise ValueError("sources must be a list of source IDs")
    turn = {"id": "turn_" + secrets.token_hex(8), "question": question,
            "answer": "", "status": "pending", "created": _now(),
            "sent_t": time.time(), "looked_at": [], "citations": [],
            "launching": True}
    reserved = {}

    def reserve(st):
        sid = session_id or st.get("active_id")
        s = (st.get("sessions") or {}).get(sid)
        if s is None:
            s = _new_session()
            sid = s["id"]
        if any(t.get("status") == "pending" for t in s.get("turns") or []):
            raise Busy("Vira is still answering the last message")
        prior = next((t.get("answer") or "" for t in reversed(s.get("turns") or [])
                      if t.get("status") == "done"), "")
        if s.get("turns") and sources is not None and sources != s.get("sources", []):
            raise ValueError("Start a new chat to change the evidence scope")
        if s.get("turns") and mode is not None and mode != s.get("answer_mode", "auto"):
            raise ValueError("Start a new chat to change the answer depth")
        if not s.get("turns"):
            s["sources"] = sources or []
            s["answer_mode"] = mode or "auto"
        turn["answer_mode"] = mode or s.get("answer_mode", "auto")
        turn["sources"] = s.get("sources", [])
        turn["prompt_version"] = PROMPT_VERSION
        turn["prior_result"] = prior
        s["turns"] = (s.get("turns") or []) + [turn]
        s["updated"] = _now()
        st.setdefault("sessions", {})[sid] = s
        st["active_id"] = sid
        reserved.update(sid=sid, job_id=s.get("job_id") or "", prior=prior,
                        prior_route=s.get("model_selection"), history=s["turns"][:-1],
                        idx=len(s["turns"]) - 1)
    _mutate(reserve)
    sid, job_id, prior = reserved["sid"], reserved["job_id"], reserved["prior"]
    error = ""
    previous_job = job_id
    route, changed, truncated = None, False, False
    try:
        route = _chat_model()
        before = _session_snapshot(job_id) if job_id else None
        prior = (_answer_text(before or {}) or prior).strip()
        job_id, changed, truncated = _open_session(
            job_id, question, route=route, prior_route=reserved["prior_route"],
            turns=reserved["history"], mode=turn["answer_mode"],
            sources=turn["sources"], chat_id=sid)
        if changed:
            prior = ""  # a fresh engine has no parked answer to skip
    except Exception as e:  # noqa: BLE001 - the refusal is the turn's answer
        error = str(e)[:400]

    attached = []

    def launched(st):
        s, t = _find_turn(st, sid, turn["id"])
        if not t or t.get("status") != "pending":
            return
        t.pop("launching", None)
        t["job_id"] = job_id
        t["prior_result"] = prior
        if not error:
            t.update(provider=route["provider"], model=route["model"], model_changed=changed)
            if changed:
                t["context_truncated"] = truncated
            if previous_job and job_id != previous_job:
                s.setdefault("previous_jobs", []).append(previous_job)
            s["model_selection"] = route
            s["job_id"] = job_id
        attached.append(True)
    _mutate(launched)
    if error:
        _finish_turn(sid, reserved["idx"], "", "could not start the conversation: " + error,
                     [], [], [], [], turn_key=turn["id"])
    elif attached:
        threading.Thread(target=_follow,
                         args=(sid, reserved["idx"], job_id, prior, turn["sent_t"]),
                         kwargs={"turn_key": turn["id"]},
                         daemon=True, name="vira-chat-follow").start()
    s = (_load().get("sessions") or {}).get(sid)
    return _with_progress(_public(s))


# ---------- following a turn to its answer ----------

def _current_final(snap, since=0):
    items = (snap.get("execution") or {}).get("message_items") or []
    finals = [item for item in items if item.get("phase") == "final_answer"
              and item.get("text") and float(item.get("updated_t") or 0) >= since]
    return max(finals, key=lambda item: float(item.get("updated_t") or 0))["text"].strip() if finals else ""


def _answer_text(snap, since=0):
    # Typed final items retain the complete answer. The legacy result field
    # is capped for older session consumers and is only a fallback here.
    return _current_final(snap, since) or (snap.get("result_text") or "").strip()


def _settled(snap):
    status = snap.get("status")
    return snap.get("awaiting") in ("reply", "paused") or status != "running"


def _waiting_for_owner(snap):
    return (snap.get("execution") or {}).get("status") == "awaiting_input" or snap.get("awaiting") in ("ask", "permission")


def control(sid, action, text=""):
    """Control only the pending turn attached to this saved chat."""
    from . import session
    s = (_load().get("sessions") or {}).get(sid)
    if not s:
        raise KeyError(sid)
    t = next((t for t in reversed(s.get("turns") or []) if t.get("status") == "pending"), None)
    if not t or not t.get("job_id"):
        raise Busy("There is no running turn to control")
    if action == "steer":
        if t.get("stop_requested_t"):
            raise Busy("Wait for the model to acknowledge Stop before sending a follow-up")
        text = str(text).strip()
        if not text:
            raise ValueError("empty steering message")
        session.sessions.say(t["job_id"], text)
        def record(st):
            _, live = _find_turn(st, sid, _turn_key(t))
            if live:
                live.setdefault("steering", []).append({"text": text, "t": time.time()})
        _mutate(record)
    elif action == "stop":
        # The native harness owns interrupting the turn; never stop its server.
        session.sessions.interrupt(t["job_id"])
        def stopped(st):
            _, live = _find_turn(st, sid, _turn_key(t))
            if live and live.get("status") == "pending":
                live["stop_requested_t"] = time.time()
        _mutate(stopped)
    else:
        raise ValueError("unknown chat control")
    return current(sid)


def _settle_stop(sid, key, snap):
    """A stop request is not permission to enqueue into a runner still ending."""
    acknowledged = snap is None or snap.get("awaiting") == "paused" or snap.get("status") in (
        "done", "error", "failed", "interrupted", "finished", "orphaned")
    if not acknowledged:
        return False
    changed = []
    def up(st):
        _, t = _find_turn(st, sid, key)
        if t and t.get("status") == "pending" and t.get("stop_requested_t"):
            t.update(status="stopped", outcome="interrupted", finished=_now(), finished_t=time.time())
            changed.append(True)
    _mutate(up)
    return bool(changed)


def visible(sid, turn_id, stage="answer"):
    """Receipt from the browser after a useful update or answer was rendered."""
    if stage not in ("useful", "answer"):
        raise ValueError("unknown visibility stage")
    def up(st):
        _, t = _find_turn(st, sid, turn_id)
        if not t:
            raise KeyError(turn_id)
        if stage == "answer" and t.get("status") != "done":
            return
        metrics = t.setdefault("metrics", {})
        metrics.setdefault(stage + "_visible_t", time.time())
        baseline = metrics.get("answer_ready_t") or t.get("finished_t")
        if stage == "answer" and baseline:
            metrics["visible_answer_lag_s"] = max(0, metrics["answer_visible_t"] - baseline)
    _mutate(up)


def _observe(sid, key, snap):
    """Persist provider messages and timing before optional decoration."""
    execution = snap.get("execution") or {}
    _, existing = _find_turn(_load(), sid, key)
    if not existing:
        return
    since = float(existing.get("sent_t") or 0)
    messages = [r for r in execution.get("message_items") or []
                if r.get("phase") in ("commentary", "final_answer")
                and r.get("text") and float(r.get("updated_t") or 0) >= since]
    receipts = [r for r in snap.get("receipts") or [] if float(r.get("started_t") or 0) >= since]
    ready = execution.get("answer_ready_t")
    waiting = _waiting_for_owner(snap)
    if (messages == existing.get("messages", [])
            and (not snap.get("runtime") or snap["runtime"] == existing.get("runtime"))
            and (not execution.get("usage") or execution["usage"] == existing.get("usage"))
            and receipts == existing.get("receipts", [])
            and waiting == bool(existing.get("waiting_since_t"))
            and (not ready or ready < since or ready == existing.get("metrics", {}).get("answer_ready_t"))):
        return
    def up(st):
        _, t = _find_turn(st, sid, key)
        if not t:
            return
        if waiting:
            t.setdefault("waiting_since_t", time.time())
        elif t.get("waiting_since_t"):
            t["waited_s"] = t.get("waited_s", 0) + max(0, time.time() - t.pop("waiting_since_t"))
        since = float(t.get("sent_t") or 0)
        # Thinking/reasoning items and unknown-phase text are never displayed.
        messages = [r for r in execution.get("message_items") or []
                    if r.get("phase") in ("commentary", "final_answer")
                    and r.get("text") and float(r.get("updated_t") or 0) >= since]
        if messages:
            t["messages"] = messages
            metrics = t.setdefault("metrics", {})
            metrics.setdefault("first_useful_t", time.time())
            metrics["time_to_first_useful_s"] = max(0, metrics["first_useful_t"] - since)
        if snap.get("runtime"):
            t["runtime"] = snap["runtime"]
        if execution.get("answer_ready_t") and execution["answer_ready_t"] >= since:
            t.setdefault("metrics", {})["answer_ready_t"] = execution["answer_ready_t"]
        if execution.get("usage"):
            t["usage"] = execution["usage"]
        if receipts:
            # The full unbounded event journal remains with the native job.
            t["receipts"] = receipts
            t["receipt_job_id"] = t.get("job_id")
    _mutate(up)


def _turn_key(turn):
    # Older persisted chats predate UUIDs; their send timestamp is stable
    # across pagination and legacy records, unlike a displayed page index.
    return turn.get("id") or turn.get("sent_t")


def _find_turn(state, sid, key):
    s = (state.get("sessions") or {}).get(sid)
    t = next((t for t in (s or {}).get("turns") or [] if _turn_key(t) == key), None)
    return s, t


def _follow(sid, idx, job_id, prior, sent_t=0.0, max_s=TURN_MAX_S,
            poll_s=POLL_S, clock=time.time, sleep=time.sleep, turn_key=None):
    """Publish the answer at its boundary, before ANY optional enrichment.

    Source attribution uses the send time, not the runner's turn counter:
    the counter moves after dispatch and a turn may make no calls at all.
    Every asynchronous write uses the stable identity, never a list index.
    """
    if turn_key is None:
        s = (_load().get("sessions") or {}).get(sid) or {}
        turns = s.get("turns") or []
        if idx >= len(turns):
            return
        turn_key = _turn_key(turns[idx])
    end = clock() + max_s
    last_clock = clock()
    saw_working = False
    answer, failed, snap = "", "", {}
    while True:
        _, t = _find_turn(_load(), sid, turn_key)
        if not t or t.get("status") != "pending":
            return
        snap = _session_snapshot(job_id)
        if snap is None:
            if t.get("stop_requested_t") and _settle_stop(sid, turn_key, None):
                return
            failed = "the session is gone"
            break
        _observe(sid, turn_key, snap)
        if t.get("stop_requested_t"):
            if _settle_stop(sid, turn_key, snap):
                return
            sleep(poll_s)
            continue
        now = clock()
        if _waiting_for_owner(snap):
            end += max(0, now - last_clock)
        last_clock = now
        if now >= end and not _waiting_for_owner(snap):
            failed = f"no answer after {int(max_s)}s"
            _interrupt_timeout(job_id)
            break
        if snap.get("status") == "running" and snap.get("awaiting") not in ("reply", "paused"):
            saw_working = True
        out = _answer_text(snap, sent_t)
        fresh_boundary = saw_working and snap.get("awaiting") in ("reply", "paused")
        if _settled(snap) and out and (out != prior or fresh_boundary or _current_final(snap, sent_t)):
            answer = out
            break
        if snap.get("status") not in ("running", None):
            failed = snap.get("error") or f"the session ended ({snap.get('status')})"
            break
        sleep(poll_s)
    if not answer and not failed:
        failed = "the session ended without an answer"
    if _finish_turn(sid, idx, answer, failed, [], [], [], [],
                    turn_key=turn_key, enrich=bool(answer)) and answer:
        _start_enrichment(sid, turn_key, snap or {})


def _finish_turn(sid, idx, answer, failed, looked, cites, concepts, followups,
                 *, turn_key=None, enrich=False):
    changed = []

    def up(st):
        s = (st.get("sessions") or {}).get(sid)
        if not s:
            return
        turns = s.get("turns") or []
        # The index form remains for existing local repair callers. All
        # followers and reconciliation calls supply a stable turn key.
        t = (_find_turn(st, sid, turn_key)[1] if turn_key is not None
             else turns[idx] if 0 <= idx < len(turns) else None)
        if not t or t.get("status") != "pending":
            return
        t["answer"] = answer if answer else ("Vira could not answer: " + failed)
        t["status"] = "done" if answer else "failed"
        t["outcome"] = "completed" if answer else ("timed_out" if "no answer after" in failed else "failed")
        t["looked_at"] = looked
        t["citations"] = cites
        t["finished"] = _now()
        t["finished_t"] = time.time()
        if answer:
            t["enrichment"] = {"status": "pending" if enrich else "done",
                               "deadline_t": time.time() + ENRICHMENT_MAX_S}
            if not enrich and t is turns[-1]:
                s["concepts"] = _merge_concepts(s.get("concepts") or [], concepts)
                if followups:
                    s["follow_up_questions"] = followups
                s["cited"] = _merge_cited(s.get("cited") or [], cites, len(turns))
        s["updated"] = _now()
        changed.append(True)
    _mutate(up)
    return bool(changed)


def _interrupt_timeout(job_id):
    if not job_id:
        return
    try:
        from . import session
        session.sessions.interrupt(job_id)
    except Exception:
        # The failure remains durable even if the provider is unreachable.
        pass


def _reconcile(s):
    """Recover a stranded answer from the durable session on normal reads.

    A parked previous answer is not evidence for the new question. Legacy
    turns use their preceding answer as the baseline; new turns also save
    the session result seen immediately before dispatch.
    """
    turns = s.get("turns") or []
    for idx, t in enumerate(turns):
        key = _turn_key(t)
        if (t.get("status") == "pending" and t.get("launching")
                and time.time() - float(t.get("sent_t") or 0) >= TURN_MAX_S):
            _finish_turn(s["id"], idx, "", "the conversation did not finish starting",
                         [], [], [], [], turn_key=key)
            continue
        if t.get("status") == "pending" and not t.get("launching"):
            snapshot = _session_snapshot(t.get("job_id") or s.get("job_id") or "")
            snap = snapshot or {}
            _observe(s["id"], key, snap)
            t = _find_turn(_load(), s["id"], key)[1] or t
            if t.get("stop_requested_t"):
                _settle_stop(s["id"], key, snapshot)
                continue
            prior = t.get("prior_result")
            if prior is None:
                prior = next((p.get("answer") or "" for p in reversed(turns[:idx])
                              if p.get("status") == "done"), "")
            since = float(t.get("sent_t") or 0)
            out = _answer_text(snap, since)
            if _settled(snap) and out and (out != prior or _current_final(snap, since)):
                if _finish_turn(s["id"], idx, out, "", [], [], [], [],
                                turn_key=key, enrich=True):
                    _start_enrichment(s["id"], key, snap)
            elif (not _waiting_for_owner(snap)
                  and time.time() - float(t.get("sent_t") or 0) - t.get("waited_s", 0) >= TURN_MAX_S):
                _interrupt_timeout(t.get("job_id") or s.get("job_id"))
                _finish_turn(s["id"], idx, "", f"no answer after {TURN_MAX_S}s",
                             [], [], [], [], turn_key=key)
        phase = t.get("enrichment") or {}
        if phase.get("status") not in ("pending", "running"):
            continue
        if time.time() >= phase.get("deadline_t", 0):
            _finish_enrichment(s["id"], key, error="enrichment timed out")
        elif phase.get("status") == "pending":
            snap = _session_snapshot(t.get("job_id") or s.get("job_id") or "") or {}
            _start_enrichment(s["id"], key, snap)


def _start_enrichment(sid, key, snap):
    context = {}

    def claim(st):
        s, t = _find_turn(st, sid, key)
        if not t or (t.get("enrichment") or {}).get("status") != "pending":
            return
        phase = t["enrichment"]
        phase["status"] = "running"
        context.update(question=t["question"], answer=t["answer"],
                       prior=_public(s.get("concepts") or []),
                       since_t=float(t.get("sent_t") or 0),
                       until_t=t["finished_t"], deadline_t=phase["deadline_t"],
                       runtime=t.get("runtime") or snap.get("runtime") or {},
                       sources=s.get("sources") or [])
    _mutate(claim)
    if not context:
        return
    slots = _ENRICHMENT_SLOTS
    if not slots.acquire(blocking=False):
        _finish_enrichment(sid, key, error="optional source and concept workers are busy")
        return
    context["worker_slots"] = slots
    # The timer changes durable state even if a lookup or provider call
    # hangs. Python cannot cancel that call; its late callback is harmless.
    timer = threading.Timer(max(0, context["deadline_t"] - time.time()),
                            _finish_enrichment, args=(sid, key),
                            kwargs={"error": "enrichment timed out"})
    timer.daemon = True
    try:
        timer.start()
        threading.Thread(target=_enrich, args=(sid, key, snap, context, timer),
                         daemon=True, name="vira-chat-enrich").start()
    except Exception:
        timer.cancel()
        slots.release()
        _finish_enrichment(sid, key, error="optional source worker could not start")


def _enrichment_open(sid, key):
    _, t = _find_turn(_load(), sid, key)
    phase = (t or {}).get("enrichment") or {}
    return phase.get("status") == "running" and time.time() < phase.get("deadline_t", 0)


def _save_sources(sid, key, *, looked=None, cites=None):
    """Keep completed provenance even if the later concept call fails."""
    saved = []

    def up(st):
        s, t = _find_turn(st, sid, key)
        phase = (t or {}).get("enrichment") or {}
        if phase.get("status") != "running" or time.time() >= phase.get("deadline_t", 0):
            return
        if looked is not None:
            t["looked_at"] = looked
        if cites is not None:
            t["citations"] = cites
            if t is s["turns"][-1]:
                s["cited"] = _merge_cited(s.get("cited") or [], cites, len(s["turns"]))
        s["updated"] = _now()
        saved.append(True)
    _mutate(up)
    return bool(saved)


def _enrich(sid, key, snap, context, timer):
    from . import answer_runtime, retrieval
    runtime = dict(context.get("runtime") or {},
                   auxiliary_deadline_t=context["deadline_t"], work_class="auxiliary",
                   evidence_scope={"sources": context.get("sources") or []})
    try:
        with answer_runtime.scope(runtime), retrieval.source_scope(context.get("sources") or []):
            _enrich_scoped(sid, key, snap, context, timer)
    finally:
        context["worker_slots"].release()


def _enrich_scoped(sid, key, snap, context, timer):
    try:
        if not _enrichment_open(sid, key):
            return
        snap = dict(snap, tools=[r for r in snap.get("tools") or []
                                if float(r.get("t") or 0) <= context["until_t"]])
        looked = looked_at(snap, since_t=context["since_t"])
        if not _save_sources(sid, key, looked=looked):
            return
        cites = citations(context["answer"])
        if not _save_sources(sid, key, cites=cites):
            return
        concepts, followups = _concepts(context["question"], context["answer"],
                                       cites, context["prior"])
        _finish_enrichment(sid, key, looked, cites, concepts, followups)
    except Exception as exc:  # noqa: BLE001 - optional decoration never loses the answer
        _finish_enrichment(sid, key, error=str(exc)[:400] or "enrichment failed")
    finally:
        # A deadline reached between stages must still get a terminal state.
        _finish_enrichment(sid, key, error="enrichment timed out", expired_only=True)
        timer.cancel()


def _finish_enrichment(sid, key, looked=None, cites=None, concepts=None,
                       followups=None, *, error="", expired_only=False):
    def up(st):
        s, t = _find_turn(st, sid, key)
        phase = (t or {}).get("enrichment") or {}
        if phase.get("status") not in ("pending", "running"):
            return
        expired = time.time() >= phase.get("deadline_t", 0)
        if expired_only and not expired:
            return
        failure = "enrichment timed out" if expired else error
        phase["status"] = "failed" if failure else "done"
        if failure:
            phase["error"] = failure
        else:
            t["looked_at"] = looked or []
            t["citations"] = cites or []
            # Older turns may receive their own source cards, but must not
            # replace the current turn's shared suggestions or summaries.
            if t is s["turns"][-1]:
                s["concepts"] = _merge_concepts(s.get("concepts") or [], concepts or [])
                s["follow_up_questions"] = followups or []
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
        s["execution"] = {k: v for k, v in (snap.get("execution") or {}).items()
                          if k != "message_items"}
        s["admission"] = snap.get("admission") or {}
        s["runtime"] = snap.get("runtime") or {}
        s["elapsed_s"] = max(0, time.time() - since)
        s["activity_age_s"] = max(0, time.time() - float(
            (snap.get("execution") or {}).get("updated_t") or since))
        s["receipts"] = [r for r in snap.get("receipts") or []
                         if float(r.get("started_t") or 0) >= since]
        s["live"] = bool(snap)
    return s


# ---------- what the turn looked at ----------

def _label(row):
    name = (row.get("name") or "").replace("mcp__vira__", "").removeprefix("vira.")
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
    return out


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
    name = (row.get("name") or "").replace("mcp__vira__", "").removeprefix("vira.")
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
        if ref.startswith("evidence:"):
            handle = ref.removeprefix("evidence:")
            try:
                from . import answer_sources
                evidence = answer_sources.evidence(handle, for_model=True)
                out.append({"ref": ref, "evidence_handle": handle,
                            "title": evidence.get("title") or evidence.get("source_handle") or ref,
                            "path": None, "exact": True,
                            "version": evidence.get("version"), "span": evidence.get("span")})
            except (ValueError, KeyError, PermissionError, FileNotFoundError):
                out.append({"ref": ref, "path": None, "title": "Unavailable evidence", "exact": False})
            continue
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


@modulemodels.scoped("find")
def _concepts(question, answer, cites, prior_concepts):
    from . import answer_runtime, modelbudget, suggest
    total, part = modelbudget.split(BUDGET, parts=3)
    prior = ", ".join(f"{c.get('term')} (w={float(c.get('weight') or 0):.2f})"
                      for c in prior_concepts[:MAX_PRIOR_CONCEPTS]
                      if c.get("term")) or "(none; this is the first turn)"
    notes = [c["path"] for c in cites if c.get("path")]
    prompt = (_CONCEPT_PROMPT + "\nQUESTION:\n" + question[:part]
              + "\n\nANSWER:\n" + answer[:part]
              + "\n\nNOTES:\n" + ("\n".join(notes) or "(none)")
              + "\n\nPRIOR CONCEPTS:\n" + prior)
    deadline = answer_runtime.current().get("auxiliary_deadline_t", time.time() + ENRICHMENT_MAX_S)
    raw = suggest.complete(prompt, tools=[], timeout=max(0.1, deadline - time.time()),
                           work_class="auxiliary")
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

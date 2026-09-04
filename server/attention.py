"""Attention — the tier-1 aggregator: what Vira is doing RIGHT NOW and what
is waiting on the owner RIGHT NOW.

THE SPLIT THIS MODULE ENFORCES. There are two kinds of waiting and they have
opposite properties. Decisions that KEEP belong to the Attention shell's
Decide lane (server/reviewqueue.py) and are read in batches; nothing degrades
if the owner rules tomorrow. This module carries only the Now lane: sessions
working or parked on a question, decision cards, resumable dead sessions,
unlanded branches, running flows, and the small set of derived health states
that otherwise fail silently (the 2026-07-27 audit's theme). The common shell,
not this payload, joins Now with Decide and Day; that keeps the live list short
without making three cadences look like three peer products.

THE SOURCE DISCIPLINE IS reviewqueue's: every reader runs inside `_safe`,
a raised exception becomes an entry in `errors` and an empty list, and a
broken store never takes the surface down with it. This module WRITES
nothing — acting on a row happens through the surface that owns it (the
card's own answer route, the Runs list's Land/Resume, the session
terminal). Read-only end to end, so there is no passive guard on the
route; the health rows alone are skipped under VIRA_PASSIVE, because they
assert facts about workers a passive instance deliberately does not run.

EDGE-TRIGGERING IS THE CLIENT'S JOB, TOKENS ARE OURS. The attention window
auto-reopens only on genuinely NEW membership (the briefstate self-re-arming
key idea, applied to a window). The server's half of that contract is the
`trigger` token on every row: it embeds the id always, and the state ONLY
when the row needs the owner — so a working session or flow re-triggers
when it STARTS (a new id) or when it flips to needing you (state joins the
token), and never on mere progress. A stage completing inside a running
flow must not pop a window the owner just closed.
"""
from __future__ import annotations

import os
from datetime import datetime

from . import jobfiles, joblog

# The boards poller ticks every boards_poll_minutes (default 15); a snapshot
# this old means the loop is wedged or dead, which is exactly the class of
# failure that once ran silent for three days (the poll-firstseen wedge).
BOARDS_STALE_H = 4

# Dead-but-resumable sessions age out of the list: after a couple of days an
# unanswered resumable session is a decision that keeps, not a live state,
# and the compose box in its terminal still offers the resume forever.
RESUMABLE_MAX_H = 48


def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _age_days(iso):
    try:
        return round(max(0.0, (datetime.now()
                               - datetime.fromisoformat(iso)).total_seconds())
                     / 86400, 1)
    except (TypeError, ValueError):
        return None


def _epoch(value):
    """Comparable activity time from the numeric and ISO clocks our source
    stores already carry. Missing legacy timestamps sort last, never as now."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value or "").replace(
            "Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _row(rid, kind, state, needs_you, title, sub="", verb="", job_id=None,
         activity_at=0, **extra):
    """One attention row. `trigger` is the edge-token the client's reopen
    logic keys on — see the module docstring for why state only joins it
    when the row needs the owner."""
    r = {"id": rid, "kind": kind, "state": state,
         "needs_you": bool(needs_you), "title": title, "sub": sub,
         "verb": verb, "job_id": job_id,
         "activity_at": _epoch(activity_at),
         "trigger": f"{rid}@{state}" if needs_you else f"{rid}@"}
    r.update(extra)
    return r


def _safe(fn, errors, key):
    """A source that raises costs its own rows and nothing else — the
    reviewqueue / brief never-break-on-a-section contract."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — a broken store is not an outage
        errors[key] = f"{type(e).__name__}: {e}"
        return []


# ------------------------------------------------------------- sessions

def _machine_meta(spec):
    meta = spec.get("meta") or {}
    return bool(meta.get("machine") or meta.get("routine_id")
                or meta.get("circuit_run") or meta.get("judge_of"))


def _is_chat(spec):
    """A CHAT is a conversation, never work (owner's ruling, 2026-09-03).
    Chat with Vira runs as a session so it can reach the tools and so it
    shows up as a run, but a parked or dead chat is not something waiting
    on the owner and never counts as unlanded work - if a conversation
    leads to work, that work is dispatched as its own session and THAT
    session is what this surface watches."""
    return ((spec.get("meta") or {}).get("kind") == "chat")


def _session_rows(registry, names):
    """Live sessions from the supervisor's registry, state read FRESH off
    each job dir (the pending_all discipline: the cached copy is refreshed
    by the supervisor, which does not run on a passive instance, and a
    surface that silently stops updating is the one thing this must not
    be). A session whose pending list holds a card is SKIPPED here — the
    card row owns it, and one piece of work never renders twice."""
    from . import session as session_mod
    with registry.lock:
        handles = [x for x in registry.sessions.values()
                   if x.kind == "detached"]
    rows = []
    for h in handles:
        if _is_chat(h.spec):
            continue                          # a conversation, not work
        st = jobfiles.read_json(h.dir / "state.json") or {}
        status = st.get("status")
        awaiting = st.get("awaiting")
        started = h.spec.get("started") or st.get("started")
        machine = _machine_meta(h.spec)
        circuit_stage = bool((h.spec.get("meta") or {}).get("circuit_run"))
        title = names.get(h.id) or h.id
        if status == "running":
            if st.get("pending"):
                continue                      # the card row carries it
            if awaiting == "reply":
                rows.append(_row(
                    f"session:{h.id}", "reply", "reply", True, title,
                    "finished its turn and is holding open for your reply",
                    verb="reply", job_id=h.id, machine=machine,
                    activity_at=started))
            else:
                # A circuit-stage session is one dot of a flow the flows
                # source already draws; a second row would double it.
                if circuit_stage:
                    continue
                rows.append(_row(
                    f"session:{h.id}", "working", "working", False, title,
                    "working", verb="watch", job_id=h.id, machine=machine,
                    activity_at=started))
        elif status == "error":
            snap = {"status": status, "live": st.get("live"),
                    "session_id": st.get("session_id"),
                    "provider": h.spec.get("provider", "anthropic"),
                    "cwd": h.spec.get("cwd", ""),
                    "finished_by_owner": st.get("finished_by_owner")}
            fin = st.get("finished") or ""
            age = _age_days(fin)
            fresh = age is not None and age * 24 <= RESUMABLE_MAX_H
            if fresh and not machine and session_mod.resumable(snap):
                rows.append(_row(
                    f"session:{h.id}", "died", "died", True, title,
                    (st.get("error") or "ended on an error")[:140]
                    + " — the conversation is resumable",
                    verb="open", job_id=h.id, machine=machine,
                    age_days=age, activity_at=fin))
    return rows


def _card_rows(registry, names):
    """Every unanswered decision card, as attention rows AND as renderable
    card payloads — the pending_all shape the existing permission/ask card
    components already consume, so answering here and answering in the
    terminal stay the same act."""
    pending = registry.pending_all()
    rows, cards = [], []
    for p in pending:
        card = p["card"]
        title = names.get(p["job_id"]) or p["job_id"]
        kind = ("a question" if card.get("kind") == "ask" else
                "ready to land: " + (card.get("branch") or "a branch")
                if card.get("kind") == "landing" else
                "approval: " + (card.get("tool") or "a tool call"))
        rows.append(_row(
            f"card:{card['req_id']}", "card", "pending", True, title, kind,
            verb="answer", job_id=p["job_id"], req_id=card["req_id"],
            activity_at=card.get("created")))
        cards.append({**p, "title": title})
    return rows, cards


# ---------------------------------------------------------------- flows

def _stage_strip(run):
    """The minimal per-stage list the client's mini stage strip renders —
    id, name, status, judge?, grade — in TOPO order, because the strip's
    whole point is plan-vs-build-vs-judge reading left to right and a
    stages_def list is stored in authoring order, not execution order.
    Derived per read from the run's own frozen stages_def; a run stored
    before stages_def existed falls back to the stages dict's own order
    (insertion order, which start_run wrote from the defs anyway).

    Deliberately NOT part of the row's trigger token: a stage transition
    is progress, and progress must never re-pop a window the owner just
    closed — the client repaints the strip off its own render key."""
    from . import circuits
    defs = [d for d in (run.get("stages_def") or []) if d.get("id")]
    states = run.get("stages") or {}
    by_id = {d["id"]: d for d in defs}
    if defs:
        try:
            order = circuits.topo_order(defs)
        except (ValueError, KeyError, TypeError):
            order = [d["id"] for d in defs]
    else:
        order = list(states)
    strip = []
    for sid in order:
        d = by_id.get(sid) or {}
        s = states.get(sid) or {}
        item = {"id": sid, "name": d.get("name") or sid,
                "status": s.get("status") or "pending",
                "judge": circuits.norm_stage_mode(d.get("mode")) == "judge"}
        if s.get("grade"):
            item["grade"] = s["grade"]
        strip.append(item)
    return strip


def _flow_rows():
    from . import circuits
    rows = []
    for run in circuits.list_runs(20):
        if run.get("status") != "running":
            continue
        stages = run.get("stages") or {}
        done = sum(1 for s in stages.values()
                   if s.get("status") in ("done", "skipped"))
        current = next((sid for sid, s in stages.items()
                        if s.get("status") == "running"), None)
        sub = f"stage {min(done + 1, len(stages))} of {len(stages)}"
        if current:
            sub += f" — {current}"
        rows.append(_row(
            f"flow:{run['id']}", "flow", "running", False,
            run.get("circuit_name") or run["id"], sub, verb="trace",
            run_id=run["id"], stages_done=done, stages_total=len(stages),
            stages=_stage_strip(run), activity_at=run.get("started")))
    return rows


# --------------------------------------------------------------- orphans

def _orphan_rows():
    """Unlanded work, from the sweeper's own store — dismissals and the
    per-item Vira read already applied by compose(). Chronic rows re-trigger
    only when their key changes (a new commit, new dirt), which is the
    sweeper's own dismissal-re-arm rule inherited for free."""
    from . import orphanwork
    rows = []
    for it in orphanwork.compose()["items"]:
        kind = it.get("kind") or "unmerged"
        read = it.get("read") or {}
        if kind == "unpushed":
            sub = f"{it.get('ahead', 0)} commits not pushed"
        else:
            bits = []
            if it.get("dirty"):
                bits.append(f"{it['dirty']} dirty files")
            if it.get("ahead"):
                bits.append(f"{it['ahead']} unmerged commits")
            if read.get("verdict"):
                bits.append("Vira: " + read["verdict"])
            sub = " — ".join(bits) or "unlanded"
        rows.append(_row(
            f"orphan:{it['key']}", "orphan", "open", True,
            it.get("branch") or it.get("key"), sub, verb="review",
            age_days=it.get("age_days"), orphan_key=it.get("key"),
            orphan_branch=it.get("branch"),
            orphan_kind=kind, dirty=int(it.get("dirty") or 0),
            ahead=int(it.get("ahead") or 0),
            verdict=read.get("verdict") or "",
            activity_at=it.get("last_activity")))
    return rows


# ---------------------------------------------------------------- health

def _health_rows():
    """The silent-failure tier: states that already have a store recording
    them and no surface announcing them. Skipped wholesale under
    VIRA_PASSIVE — these rows assert facts about workers a passive instance
    deliberately does not run, so on a clone they could only be false
    alarms about the live machine's stores."""
    if _passive():
        return []
    rows = []
    from . import aihealth
    ai = aihealth.summary()
    if ai.get("state") == "red":
        rows.append(_row(
            "health:ai", "health", "red", True, "AI backend is down",
            (ai.get("action") or ai.get("detail") or "")[:160],
            verb="recheck", activity_at=ai.get("checked_at")))

    from . import jobboards
    bh = jobboards.health()
    if bh.get("registered"):
        fetched = bh.get("fetched") or ""
        age = _age_days(fetched)
        if fetched and age is not None and age * 24 >= BOARDS_STALE_H:
            rows.append(_row(
                "health:boards-stale", "health", f"d{int(age)}", True,
                "Job boards sweep is stale",
                f"last swept {fetched.replace('T', ' ')[:16]} — the poller "
                "may be wedged", verb="open", activity_at=fetched))
        errs = bh.get("errors") or {}
        if errs:
            bid, msg = next(iter(errs.items()))
            more = f" (+{len(errs) - 1} more)" if len(errs) > 1 else ""
            rows.append(_row(
                "health:boards-errors", "health", f"n{len(errs)}", True,
                f"{len(errs)} job board{'s' if len(errs) > 1 else ''} "
                "failing to poll", f"{bid}: {msg}"[:160] + more,
                verb="open", activity_at=fetched))
    return rows


# --------------------------------------------------------------- compose

def _names(registry):
    """Job id -> canonical session title, the same ledger naming every
    other job surface uses (joblog.name over the fuller ledger record)."""
    recs = {r["id"]: r for r in joblog.list_records()}
    idea_map = None
    out = {}
    with registry.lock:
        ids = [x.id for x in registry.sessions.values()
               if x.kind == "detached"]
    for jid in ids:
        rec = recs.get(jid) or {"id": jid}
        it = None
        if rec.get("idea_id"):
            if idea_map is None:
                from . import ideas
                idea_map = {x["id"]: x["text"] for x in ideas.list_items()}
            it = idea_map.get(rec["idea_id"])
        out[jid] = joblog.name(rec, it)
    return out


def compose(registry=None):
    """The whole attention payload, one read. `rows` is everything, sorted
    by newest activity first; `cards` is the renderable card
    payloads (pending_all shape); `tokens` is the flat trigger list the
    client's edge-triggered reopen compares against what it has seen."""
    if registry is None:
        from . import session as session_mod
        registry = session_mod.sessions
    errors = {}
    names = {}
    try:
        names = _names(registry)
    except Exception as e:  # noqa: BLE001 — naming must never break the list
        errors["names"] = f"{type(e).__name__}: {e}"

    card_rows, cards = [], []
    got = _safe(lambda: _card_rows(registry, names), errors, "cards")
    if got:
        card_rows, cards = got
    rows = list(card_rows)
    rows += _safe(lambda: _session_rows(registry, names), errors, "sessions")
    rows += _safe(_flow_rows, errors, "flows")
    rows += _safe(_orphan_rows, errors, "orphans")
    rows += _safe(_health_rows, errors, "health")

    # Now is a chronology, not a triage queue: a session that just started
    # leads immediately even when older work is waiting on the owner. That
    # visible proof of activity prevents duplicate dispatches made because
    # the new working row quietly landed below the fold.
    rows.sort(key=lambda r: r.get("activity_at") or 0, reverse=True)
    return {
        "rows": rows,
        "cards": cards,
        "errors": errors,
        "counts": {
            "needs_you": sum(1 for r in rows if r["needs_you"]),
            "working": sum(1 for r in rows if not r["needs_you"]),
        },
        "tokens": [r["trigger"] for r in rows],
        "generated_at": _now_iso(),
    }

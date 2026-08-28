"""Attention — the tier-1 aggregator: what Vira is doing RIGHT NOW and what
is waiting on the owner RIGHT NOW.

THE SPLIT THIS MODULE ENFORCES. There are two kinds of waiting and they have
opposite properties. Decisions that KEEP — lesson proposals, proposed ideas,
inbox stubs, a pending Morning Picker batch — belong to the review queue
(server/reviewqueue.py) and are read in batches; nothing degrades if the
owner rules tomorrow. This module carries only the LIVE tier: sessions
working or parked on a question, decision cards, resumable dead sessions,
unlanded branches, running flows, and the small set of derived health states
that otherwise fail silently (the 2026-07-27 audit's theme). Merging the two
tiers would make the urgent list long, which kills the one property the
owner asked for — short, clean, visible. The join between them is a
REFERENCE: `review` in the payload is a count + oldest-age line, and one
escalation rule promotes an aging review backlog into a real attention row
so the 113-rotting-proposals failure cannot recur even if the brief goes
unread.

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
import time
from datetime import datetime

from . import jobfiles, joblog

# A review item older than this stops being "a decision that keeps" and
# earns a real attention row. Weekly-bucketed in the trigger so the row
# re-announces itself once a week, not once a day.
REVIEW_ESCALATE_DAYS = 7

# The boards poller ticks every boards_poll_minutes (default 15); a snapshot
# this old means the loop is wedged or dead, which is exactly the class of
# failure that once ran silent for three days (the poll-firstseen wedge).
BOARDS_STALE_H = 4

# Dead-but-resumable sessions age out of the list: after a couple of days an
# unanswered resumable session is a decision that keeps, not a live state,
# and the compose box in its terminal still offers the resume forever.
RESUMABLE_MAX_H = 48

# reviewqueue.items() walks several stores including canon files in the
# self-record; at a 5s poll that is real work for a number that moves a few
# times a day. Cached in-process, invalidated by time alone.
_REVIEW_CACHE_S = 60
_review_cache = {"at": 0.0, "data": None}


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


def _row(rid, kind, state, needs_you, title, sub="", verb="", job_id=None,
         **extra):
    """One attention row. `trigger` is the edge-token the client's reopen
    logic keys on — see the module docstring for why state only joins it
    when the row needs the owner."""
    r = {"id": rid, "kind": kind, "state": state,
         "needs_you": bool(needs_you), "title": title, "sub": sub,
         "verb": verb, "job_id": job_id,
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
        st = jobfiles.read_json(h.dir / "state.json") or {}
        status = st.get("status")
        awaiting = st.get("awaiting")
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
                    verb="reply", job_id=h.id, machine=machine))
            else:
                # A circuit-stage session is one dot of a flow the flows
                # source already draws; a second row would double it.
                if circuit_stage:
                    continue
                rows.append(_row(
                    f"session:{h.id}", "working", "working", False, title,
                    "working", verb="watch", job_id=h.id, machine=machine))
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
                    age_days=age))
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
        kind = "a question" if card.get("kind") == "ask" else \
            "approval: " + (card.get("tool") or "a tool call")
        rows.append(_row(
            f"card:{card['req_id']}", "card", "pending", True, title, kind,
            verb="answer", job_id=p["job_id"], req_id=card["req_id"]))
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
            stages=_stage_strip(run)))
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
        if kind == "unpushed":
            sub = f"{it.get('ahead', 0)} commits not pushed"
        else:
            bits = []
            if it.get("dirty"):
                bits.append(f"{it['dirty']} dirty files")
            if it.get("ahead"):
                bits.append(f"{it['ahead']} unmerged commits")
            read = it.get("read") or {}
            if read.get("verdict"):
                bits.append("Vira: " + read["verdict"])
            sub = " — ".join(bits) or "unlanded"
        rows.append(_row(
            f"orphan:{it['key']}", "orphan", "open", True,
            it.get("branch") or it.get("key"), sub, verb="review",
            age_days=it.get("age_days"), orphan_key=it.get("key")))
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
            verb="recheck"))

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
                "may be wedged", verb="open"))
        errs = bh.get("errors") or {}
        if errs:
            bid, msg = next(iter(errs.items()))
            more = f" (+{len(errs) - 1} more)" if len(errs) > 1 else ""
            rows.append(_row(
                "health:boards-errors", "health", f"n{len(errs)}", True,
                f"{len(errs)} job board{'s' if len(errs) > 1 else ''} "
                "failing to poll", f"{bid}: {msg}"[:160] + more,
                verb="open"))
    return rows


# ---------------------------------------------------------------- review

def _review_note():
    """The tier-2 reference: a count + oldest-age line, cached because the
    queue walks canon files. Returns (note, escalation_row_or_None)."""
    now = time.time()
    if _review_cache["data"] is not None \
            and now - _review_cache["at"] < _REVIEW_CACHE_S:
        note = _review_cache["data"]
    else:
        from . import reviewqueue
        q = reviewqueue.items()
        oldest = max((r.get("age_days") or 0 for r in q["items"]),
                     default=0)
        note = {"total": q["total"], "oldest_days": round(oldest, 1),
                "counts": q["counts"]}
        _review_cache["data"] = note
        _review_cache["at"] = now
    if not note["total"]:
        return None, None
    esc = None
    if note["oldest_days"] >= REVIEW_ESCALATE_DAYS:
        week = int(note["oldest_days"] // 7)
        esc = _row(
            "review:aging", "review", f"w{week}", True,
            f"{note['total']} decisions waiting in Needs Review",
            f"the oldest has waited {int(note['oldest_days'])} days",
            verb="open review")
    return note, esc


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
    needs-you-first then newest-working; `cards` is the renderable card
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

    review, esc = None, None
    try:
        review, esc = _review_note()
    except Exception as e:  # noqa: BLE001
        errors["review"] = f"{type(e).__name__}: {e}"
    if esc:
        rows.append(esc)

    # Needs-you first, oldest waiting first inside that (the decision that
    # has blocked longest leads); working rows after, in source order.
    rows.sort(key=lambda r: (0 if r["needs_you"] else 1,
                             -(r.get("age_days") or 0)))
    return {
        "rows": rows,
        "cards": cards,
        "review": review,
        "errors": errors,
        "counts": {
            "needs_you": sum(1 for r in rows if r["needs_you"]),
            "working": sum(1 for r in rows if not r["needs_you"]),
        },
        "tokens": [r["trigger"] for r in rows],
        "generated_at": _now_iso(),
    }

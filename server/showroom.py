"""The Showroom - the organizer for many draft branches, each with its own
test instance, so the owner can keep saying "build it" without losing track.

The bottleneck this closes (owner, 2026-08-31): dispatching ideas one at a
time, then losing track of the branches - "if I have too many branches, it
gets really confusing... and I don't view the tests of all of these in an
efficient way."

WHAT THIS IS NOT (owner's correction, 2026-09-02): it is NOT a button that
builds the whole queue. "I don't think the button needs to automatically
start building every single idea. The concept is just that the showroom is
a way to organize a lot of draft branches with different test modules so
that I can keep building and I can tell you, 'Build it, build it, build
it.'" So staging is DELIBERATE and per-idea - build_queue REQUIRES the ids
it is to build and refuses an empty call by name, rather than defaulting to
everything. The parallelism is a consequence of the owner saying "build it"
several times, never a fleet the app launches on his behalf. Three pieces:

1. PER-IDEA STAGING. build_queue(ids) stages the NAMED ideas as CANDIDATES
   and the Driver launches Implement-style sessions for them a few at a
   time (showroom_max_building), through the EXISTING session engine -
   placement, the branch-first write guard, the ledger, the terminal all
   come for free because session.sessions.launch owns them.

2. A CANDIDATE LIFECYCLE. A Showroom-built branch is a candidate, not
   orphan work: orphanwork.sweep excludes candidate_branches(), so a dozen
   drafts in flight do not flood Runs/Attention with rows reading abandoned.
   The Showroom is their one surface until the owner delivers a verdict
   (land / discard), after which the ordinary machinery owns them again.

3. A VERDICT SURFACE. Each finished build is judged by a fresh session
   (judge.launch_judge - the verdict lands on the ledger row and is
   copied onto the candidate), then the card offers Try (a branch.sh
   serve --local instance, on demand - never all at once), Land
   (orphanwork._merge_sync + mark the idea done + tidy the branch),
   Iterate (a follow-up session into the same worktree carrying the
   owner's note), and Discard.

AUTO-REBASE AFTER LAND: the moment one candidate lands, every other
candidate's base is stale - and parallel UI branches collide in the three
monolithic static/ files. _rebase_survivors() rebases each remaining
candidate onto main; a conflict marks the candidate `conflict` (Iterate is
the fix path) rather than leaving it to rot silently. A candidate that is
SERVING is skipped (rebasing files under a running instance) and says so.

HONESTY NOTES the surface carries: a rebase does NOT re-run the suite (the
judge graded the pre-rebase tree; branch.sh merge still preflights), and a
build session is told to COMMIT - unlike the Queue's Implement prompt -
because a clean committed branch is what makes Land a one-click merge.

Builds do NOT pass idea_id to launch(): the runner's epilogue would mark
the idea done when the BUILD finishes, and a candidate idea is done when
it LANDS, not when it compiles. The link rides meta.showroom_idea and the
close-out happens in land().

Store data/showroom.json (jsonstore discipline). VIRA_PASSIVE refuses
every mutating operation by name: builds need the live supervisor, and
serve/land/discard shell out to scripts/branch.sh against the REAL repo -
a test clone must never mint sessions, instances, or merges.
"""
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil, joblog, jsonstore, settings

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "showroom.json"

# States a candidate moves through. `serving` is a FIELD (port), never a
# state - an instance can be up while the candidate is built or conflicted.
STATES = ("queued", "building", "built", "failed", "conflict",
          "landing", "landed", "discarded")
# States whose branch belongs to the Showroom - excluded from the
# orphan-work sweep. Terminal verdicts (landed/discarded) release the
# branch back to the ordinary machinery (both paths tidy it anyway).
ACTIVE_STATES = ("queued", "building", "built", "failed", "conflict",
                 "landing")

TICK_S = 4.0
SERVE_TIMEOUT = 600          # clone + provision + boot can take a minute+
QUICK_TIMEOUT = 120          # stop / discard
REBASE_TIMEOUT = 60
# Display order: what needs the owner's verdict first, then what is moving,
# then the tail.
_STATE_RANK = {"built": 0, "conflict": 1, "failed": 2, "landing": 3,
               "building": 4, "queued": 5, "landed": 6, "discarded": 7}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


def _refuse_if_passive(act):
    if _passive():
        raise PermissionError(
            f"this is a passive test instance - {act} runs sessions or "
            "branch.sh against the owner's real repo, so it only runs on "
            "the live Vira")


def _load():
    return jsonstore.read(STORE, {"candidates": {}})


def _mutate(fn):
    return jsonstore.mutate(STORE, fn, {"candidates": {}},
                            indent=2, ensure_ascii=False)


def _set(idea_id, **fields):
    """Update one candidate's fields under the store lock. Silently a
    no-op when the candidate is gone (a concurrent cancel)."""
    def fn(s):
        c = s["candidates"].get(idea_id)
        if c is not None:
            c.update(fields)
            c["updated"] = _now()
    _mutate(fn)


def _get(idea_id):
    c = _load()["candidates"].get(idea_id)
    if not c:
        raise KeyError(f"no showroom candidate for {idea_id}")
    return c


def _slug(candidate):
    branch = candidate.get("branch") or ""
    if "/" not in branch:
        raise ValueError(
            f"candidate {candidate.get('idea_id')} has no branch yet - "
            "its build session was never placed")
    return branch.split("/", 1)[1]


# ---------------------------------------------------------------- staging

def eligible_ideas():
    """Open Vira-project ideas with no active candidate - what the picker
    offers, NOT a work list anything dispatches on its own. The Showroom
    builds THIS repo: an idea filed under another project would be built
    against the wrong tree, so those stay on the Queue's ordinary
    dispatch (which takes a target repo)."""
    from . import ideas
    have = {cid for cid, c in _load()["candidates"].items()
            if c.get("state") in ACTIVE_STATES}
    out = []
    for it in ideas.list_items():
        if it.get("status") != "open":
            continue
        if (it.get("project") or "Vira") != "Vira":
            continue
        if it["id"] in have:
            continue
        out.append(it)
    return out


def eligible_list():
    """The picker's feed: id + text + project only, newest-updated first.
    Its own route rather than a field on compose(), because compose() is
    polled every few seconds while a build runs and the full backlog has
    no business riding that poll."""
    out = []
    for it in eligible_ideas():
        out.append({"id": it["id"], "text": it.get("text", ""),
                    "status": it.get("status", "open"),
                    "updated": it.get("updated") or it.get("created") or ""})
    out.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return {"ideas": out}


def build_queue(idea_ids, limit=None):
    """Stage exactly the NAMED ideas as candidates, refusing non-Vira or
    non-open ones by name. Nothing launches here - the Driver picks queued
    candidates up within a tick, a few at a time. Returns {staged,skipped}.

    idea_ids is REQUIRED and an empty call is refused (owner, 2026-09-02).
    An `ids or everything` default is exactly the mass dispatch this
    surface is not: a bodyless POST would build the whole backlog, which
    is a loaded gun whether or not any button currently pulls it. The
    owner names what to build, one "build it" at a time."""
    _refuse_if_passive("staging a build")
    if not idea_ids:
        raise ValueError(
            "name the idea(s) to build - the Showroom stages what you "
            "pick, never the whole queue")
    from . import ideas
    by_id = {it["id"]: it for it in ideas.list_items()}
    picks, skipped = [], []
    for iid in idea_ids:
        it = by_id.get(iid)
        if not it:
            skipped.append(f"{iid}: unknown idea")
        elif (it.get("project") or "Vira") != "Vira":
            skipped.append(f"{iid}: not a Vira idea - dispatch it from "
                           "the Queue with a target repo")
        elif it.get("status") not in ("open", "on-hold"):
            skipped.append(f"{iid}: status {it.get('status')} - only "
                           "open/on-hold ideas build")
        else:
            picks.append(it)
    if limit:
        dropped = len(picks) - int(limit)
        picks = picks[:int(limit)]
        if dropped > 0:
            skipped.append(f"{dropped} more past the limit")
    staged = []

    def fn(s):
        for it in picks:
            cur = s["candidates"].get(it["id"])
            if cur and cur.get("state") in ACTIVE_STATES:
                continue
            s["candidates"][it["id"]] = {
                "idea_id": it["id"], "text": it.get("text", ""),
                "state": "queued", "job_id": None, "branch": None,
                "worktree": None, "judge_job": None, "grade": None,
                "judge_summary": "", "error": "", "port": None,
                "serve_status": "", "note": "", "rebased": None,
                "land_output": "", "created": _now(), "updated": _now(),
            }
            staged.append(it["id"])
    _mutate(fn)
    return {"staged": len(staged), "ids": staged, "skipped": skipped}


def candidate_branches():
    """Branches the Showroom owns right now - orphanwork.sweep excludes
    these so a fleet in flight does not read as abandoned work. Never
    raises: the sweeper must not die on a broken store."""
    try:
        return {c.get("branch") for c in _load()["candidates"].values()
                if c.get("state") in ACTIVE_STATES and c.get("branch")}
    except Exception:  # noqa: BLE001 - a broken store costs the exclusion only
        return set()


# ---------------------------------------------------------------- prompts

def build_prompt(text):
    """The fleet build prompt. First line is the LABEL (joblog names the
    session, its terminal, and its branch from the prompt head - the
    prompt-slugs rule), then the idea verbatim, then the rules.

    ONE deliberate divergence from the Queue's Implement prompt: COMMIT.
    A Showroom candidate's whole point is one-click landing later, and
    orphanwork can only direct-merge a clean committed branch."""
    head = " ".join((text or "").split())[:90]
    rules = [
        "You are one of Vira's parallel Showroom build agents, working in "
        "the owner's Vira repository. You have been placed in your own "
        "worktree on your own branch - stay in it.",
        "Read the repo's agent contract (AGENTS.md, and CLAUDE.md where "
        "present) and follow it.",
        "Build the idea end to end, then verify it by exercising what you "
        "built and by running the test suite "
        "(.venv/bin/python -m unittest discover tests) - at minimum the "
        "test files covering what you touched must pass.",
        "COMMIT your work on this branch when it stands - a clean, "
        "committed branch is what lets the owner land it in one click. "
        "If you judge the work not worth keeping, say so plainly and do "
        "NOT commit.",
        "Do not merge and do not push - the owner reviews every candidate "
        "in the Showroom and decides there.",
        "The owner's Vira server is running on this machine. Never "
        "restart, stop, or kill it.",
        "If the idea is ambiguous, build the most useful honest reading "
        "of it and name the interpretation in your report rather than "
        "stalling.",
        "End with a concise report: what you built, how you verified it, "
        "and anything you deliberately did not do.",
    ]
    return "\n".join([f'Showroom build - "{head}"', "",
                      "THE IDEA (verbatim, from the owner's queue):",
                      f'"""{text}"""', ""]
                     + [f"- {r}" for r in rules])


def iterate_prompt(candidate, note):
    parts = [
        f'Showroom iteration - "{" ".join(candidate.get("text", "").split())[:80]}"',
        "",
        "You are continuing a Showroom candidate build in its existing "
        f"worktree ({candidate.get('worktree')}). An earlier session built "
        "the idea below; the owner reviewed it and wants changes.",
        "",
        "THE ORIGINAL IDEA:",
        f'"""{candidate.get("text", "")}"""',
        "",
        "THE OWNER'S NOTE (this is the instruction - it wins over the "
        "original idea where they disagree):",
        f'"""{note}"""',
        "",
        "- Read what is already on this branch before changing it.",
        "- If the branch is mid-rebase-conflict, resolve the conflict "
        "first (git status will say so).",
        "- Verify by exercising the change and running the suite for the "
        "files you touch, then COMMIT on this branch.",
        "- Do not merge, do not push, never restart the owner's server.",
        "- End with a concise report of what changed.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- driver

def _max_building():
    try:
        return max(1, int(settings.get("showroom_max_building") or 3))
    except (TypeError, ValueError):
        return 3


def _job_status(jid):
    """Live registry first, ledger fallback - the same ladder every other
    job reader walks."""
    from . import session
    snap = session.sessions.get(jid)
    if snap and snap.get("status"):
        return snap.get("status"), snap.get("error") or ""
    rec = joblog.get_record(jid) or {}
    return rec.get("status") or "running", rec.get("error") or ""


def _launch_build(candidate):
    """Launch one build; returns the job id. The launch OWNS placement
    (worktree + branch + write guard), so the branch is read back off the
    ledger row it just wrote rather than guessed."""
    from . import session
    jid = session.sessions.launch(
        build_prompt(candidate.get("text", "")), cwd=str(ROOT),
        meta={"kind": "showroom", "machine": True,
              "showroom_idea": candidate["idea_id"]})
    rec = joblog.get_record(jid) or {}
    _set(candidate["idea_id"], state="building", job_id=jid,
         branch=rec.get("branch"), worktree=rec.get("worktree"),
         error="", grade=None, judge_job=None, judge_summary="")
    return jid


def _stamp_git_facts(idea_id, worktree, branch):
    """ahead/dirty stamped at transitions (build done, rebase), never in
    compose() - polls must stay cheap."""
    ahead = dirty = None
    if worktree and Path(worktree).is_dir():
        st = gitutil.git(Path(worktree), "status", "--porcelain",
                         timeout=20)
        if st.returncode == 0:
            dirty = len([ln for ln in (st.stdout or "").splitlines()
                         if ln.strip()])
        if branch:
            ab = gitutil.git(ROOT, "rev-list", "--count",
                             f"main..{branch}", timeout=20)
            if ab.returncode == 0:
                try:
                    ahead = int((ab.stdout or "0").strip())
                except ValueError:
                    ahead = None
    _set(idea_id, ahead=ahead, dirty=dirty)


def _finish_build(candidate):
    """A build session left `running`: judge it or fail it."""
    status, err = _job_status(candidate["job_id"])
    if status == "running":
        return
    iid = candidate["idea_id"]
    if status == "done":
        _set(iid, state="built")
        _stamp_git_facts(iid, candidate.get("worktree"),
                         candidate.get("branch"))
        try:
            from . import judge
            jj = judge.launch_judge(candidate["job_id"])
            _set(iid, judge_job=jj)
        except Exception as e:  # noqa: BLE001 - a judge that cannot start
            _set(iid, note=f"judge failed to start: {str(e)[:120]}")
    else:
        _set(iid, state="failed",
             error=err or f"build session ended '{status}'")
        _stamp_git_facts(iid, candidate.get("worktree"),
                         candidate.get("branch"))


def _copy_verdict(candidate):
    rec = joblog.get_record(candidate["job_id"]) or {}
    v = rec.get("judge")
    if isinstance(v, dict) and v.get("grade"):
        _set(candidate["idea_id"], grade=v.get("grade"),
             judge_summary=str(v.get("summary") or "")[:400])


def tick():
    """One driver pass. Stateless: reads the store fresh, advances what
    it can, leaves the rest for the next tick (the circuits pattern)."""
    cands = list(_load()["candidates"].values())
    building = [c for c in cands if c.get("state") == "building"]
    for c in building:
        try:
            _finish_build(c)
        except Exception:  # noqa: BLE001 - one candidate never stops the pass
            pass
    for c in cands:
        if (c.get("state") == "built" and c.get("judge_job")
                and not c.get("grade")):
            try:
                _copy_verdict(c)
            except Exception:  # noqa: BLE001
                pass
    # Recount after finishes so freed slots refill on the same tick.
    live = len([c for c in _load()["candidates"].values()
                if c.get("state") == "building"])
    slots = _max_building() - live
    if slots <= 0:
        return
    queued = sorted((c for c in _load()["candidates"].values()
                     if c.get("state") == "queued"),
                    key=lambda c: c.get("created") or "")
    for c in queued[:slots]:
        try:
            _launch_build(c)
        except ValueError:
            break        # the session cap is full - retry next tick
        except Exception as e:  # noqa: BLE001 - a broken launch is named, kept
            _set(c["idea_id"], state="failed",
                 error=f"launch failed: {str(e)[:200]}")


class Driver:
    """The fleet driver thread (main.py starts it; VIRA_PASSIVE never
    does). Idle when nothing is queued or building - the tick is a store
    read."""

    def __init__(self, interval=TICK_S):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="vira-showroom-driver")
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                tick()
            except Exception:  # noqa: BLE001 - the driver never dies
                pass


driver = Driver()


# ---------------------------------------------------------------- actions

def _branch_sh(args, timeout):
    out = subprocess.run(
        [str(ROOT / "scripts" / "branch.sh"), *args], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False)
    return out.returncode == 0, ((out.stdout or "")
                                 + (out.stderr or "")).strip()


def serve(idea_id):
    """Spin the candidate's branch up as a passive LOCAL-ONLY test
    instance (branch.sh serve <slug> --local - a candidate snapshot holds
    personal data and is never auto-bridged to the tailnet). Async: the
    port lands on the candidate when the instance answers; poll
    compose(). Returns immediately."""
    _refuse_if_passive("serving a candidate")
    c = _get(idea_id)
    if c.get("state") in ("queued", "building", "landing"):
        raise ValueError(f"candidate is {c['state']} - nothing to serve yet")
    slug = _slug(c)
    _set(idea_id, serve_status="starting")

    def run():
        try:
            ok, text = _branch_sh(["serve", slug, "--local"], SERVE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            ok, text = False, f"serve failed to run: {e}"
        port = None
        m = (re.search(r"localhost:(\d{4})", text)
             or re.search(r"port (\d{4})", text))
        if ok and m:
            port = int(m.group(1))
        _set(idea_id, port=port,
             serve_status="up" if (ok and port) else
             f"failed: {text[-300:]}")
    threading.Thread(target=run, daemon=True,
                     name=f"vira-showroom-serve-{slug}"[:60]).start()
    return {"started": True}


def stop_serve(idea_id):
    _refuse_if_passive("stopping a candidate instance")
    c = _get(idea_id)
    try:
        _branch_sh(["stop", _slug(c)], QUICK_TIMEOUT)
    finally:
        _set(idea_id, port=None, serve_status="")
    return {"stopped": True}


def land(idea_id):
    """The verdict that finishes a candidate: merge + push (through
    orphanwork._merge_sync - branch.sh merge stays the one authority for
    preflight and refusals), mark the idea done, tidy the branch, then
    rebase every surviving candidate onto the new main. Async; the
    outcome lands on the candidate. Refusals are synchronous and named."""
    _refuse_if_passive("landing a candidate")
    c = _get(idea_id)
    if c.get("state") == "conflict":
        raise ValueError("this candidate has a rebase conflict - Iterate "
                         "to resolve it before landing")
    if c.get("state") not in ("built", "failed"):
        raise ValueError(f"candidate is {c['state']} - only a finished "
                         "build lands")
    slug = _slug(c)
    wt = c.get("worktree")
    if wt and Path(wt).is_dir():
        st = gitutil.git(Path(wt), "status", "--porcelain", timeout=20)
        if (st.stdout or "").strip():
            raise ValueError("the build left uncommitted changes - open "
                             "its session output, or Iterate to finish it")
    _set(idea_id, state="landing")

    def run():
        try:
            if c.get("port"):
                _branch_sh(["stop", slug], QUICK_TIMEOUT)
                _set(idea_id, port=None, serve_status="")
            from . import orphanwork
            ok, text = orphanwork._merge_sync(slug)
            if not ok:
                _set(idea_id, state="built", land_output=text[-2000:])
                return
            _set(idea_id, state="landed", land_output=text[-2000:])
            try:
                from . import ideas
                ideas.stamp_note(
                    idea_id, f"built and landed from the Showroom "
                             f"(job {(c.get('job_id') or '')[:8]})",
                    status="done")
            except Exception:  # noqa: BLE001 - the merge already happened
                pass
            _branch_sh(["discard", slug], QUICK_TIMEOUT)
            _rebase_survivors(exclude=idea_id)
        except Exception as e:  # noqa: BLE001 - the outcome must land
            _set(idea_id, state="built", land_output=f"landing failed: {e}")
    threading.Thread(target=run, daemon=True,
                     name=f"vira-showroom-land-{slug}"[:60]).start()
    return {"started": True}


def _rebase_survivors(exclude=None):
    """After a land, main moved: rebase every remaining candidate with a
    settled worktree. A conflict marks the candidate `conflict` (Iterate
    resolves it); a serving or busy candidate is skipped WITH the reason
    on its note - never silently. The suite is NOT re-run here and the
    judge's grade describes the pre-rebase tree; branch.sh merge still
    preflights at land time."""
    for c in _load()["candidates"].values():
        iid = c["idea_id"]
        if iid == exclude or c.get("state") not in ("built", "failed",
                                                    "conflict"):
            continue
        wt = c.get("worktree")
        if not wt or not Path(wt).is_dir():
            continue
        if c.get("port"):
            _set(iid, note="not rebased onto new main while its test "
                           "instance is serving")
            continue
        st = gitutil.git(Path(wt), "status", "--porcelain", timeout=20)
        if (st.stdout or "").strip():
            _set(iid, note="not rebased - uncommitted changes in the "
                           "worktree")
            continue
        rb = gitutil.git(Path(wt), "rebase", "main",
                         timeout=REBASE_TIMEOUT)
        if rb.returncode == 0:
            _set(iid, rebased=_now(),
                 note="rebased onto new main (suite not re-run; the "
                      "grade describes the pre-rebase build)")
            if c.get("state") == "conflict":
                _set(iid, state="built")
        else:
            gitutil.git(Path(wt), "rebase", "--abort", timeout=30)
            _set(iid, state="conflict",
                 note="rebase onto new main conflicted - Iterate to "
                      "resolve, or Discard")
        _stamp_git_facts(iid, wt, c.get("branch"))


def iterate(idea_id, note):
    """A follow-up build session into the candidate's own worktree,
    carrying the owner's note. Re-judged when it finishes."""
    _refuse_if_passive("iterating on a candidate")
    note = (note or "").strip()
    if not note:
        raise ValueError("say what should change - the note is the "
                         "instruction the session runs on")
    c = _get(idea_id)
    wt = c.get("worktree")
    if not wt or not Path(wt).is_dir():
        raise ValueError("this candidate's worktree is gone - Retry "
                         "builds it fresh instead")
    if c.get("state") in ("building", "landing"):
        raise ValueError(f"candidate is {c['state']} - wait for it")
    from . import session
    jid = session.sessions.launch(
        iterate_prompt(c, note), cwd=wt,
        meta={"kind": "showroom", "machine": True,
              "showroom_idea": idea_id})
    _set(idea_id, state="building", job_id=jid, error="", grade=None,
         judge_job=None, judge_summary="", note="")
    return {"job_id": jid}


def discard(idea_id):
    """The other verdict: tear the candidate down (branch.sh discard
    --force - a candidate by definition carries unmerged work, and this
    button sits behind an armed confirm in the client). The idea itself
    stays open with the outcome stamped - rejecting one build is not
    rejecting the idea."""
    _refuse_if_passive("discarding a candidate")
    c = _get(idea_id)
    text = ""
    if c.get("branch"):
        slug = _slug(c)
        if c.get("port"):
            _branch_sh(["stop", slug], QUICK_TIMEOUT)
        ok, text = _branch_sh(["discard", slug, "--force"], QUICK_TIMEOUT)
        if not ok:
            _set(idea_id, note=f"discard: {text[-300:]}")
    _set(idea_id, state="discarded", port=None, serve_status="")
    try:
        from . import ideas
        ideas.stamp_note(idea_id, "showroom candidate discarded",
                         append=True)
    except Exception:  # noqa: BLE001 - the teardown already happened
        pass
    return {"discarded": True, "output": text[-500:]}


def cancel(idea_id):
    """Un-stage a queued candidate before anything was spent on it."""
    c = _get(idea_id)
    if c.get("state") != "queued":
        raise ValueError(f"candidate is {c['state']} - only a queued one "
                         "cancels; use Discard")

    def fn(s):
        s["candidates"].pop(idea_id, None)
    _mutate(fn)
    return {"cancelled": True}


def retry(idea_id):
    """A failed candidate whose worktree survived iterates on the failure;
    one with no worktree re-queues for a fresh build."""
    _refuse_if_passive("retrying a candidate")
    c = _get(idea_id)
    if c.get("state") not in ("failed", "conflict"):
        raise ValueError(f"candidate is {c['state']} - retry is for "
                         "failed or conflicted builds")
    wt = c.get("worktree")
    if wt and Path(wt).is_dir():
        why = (c.get("error") or c.get("note")
               or "the previous session did not finish")
        return iterate(idea_id, "The previous build attempt stopped: "
                                f"{why}. Pick the work up and finish it.")
    _set(idea_id, state="queued", job_id=None, branch=None, worktree=None,
         error="", grade=None, judge_job=None, judge_summary="", note="")
    return {"queued": True}


def clear_settled(idea_id=None):
    """Drop landed/discarded rows from the store (one, or all settled)."""
    def fn(s):
        for iid in list(s["candidates"]):
            c = s["candidates"][iid]
            if c.get("state") not in ("landed", "discarded"):
                continue
            if idea_id and iid != idea_id:
                continue
            s["candidates"].pop(iid, None)
    _mutate(fn)
    return {"ok": True}


# ---------------------------------------------------------------- compose

def compose():
    """The Showroom payload. Cheap by construction: the store plus the
    live job-status join - git facts were stamped at transitions, never
    recomputed per poll."""
    cands = []
    for c in _load()["candidates"].values():
        row = dict(c)
        if row.get("state") == "building" and row.get("job_id"):
            try:
                status, _err = _job_status(row["job_id"])
                row["job_status"] = status
            except Exception:  # noqa: BLE001
                row["job_status"] = "unknown"
        cands.append(row)
    cands.sort(key=lambda r: (_STATE_RANK.get(r.get("state"), 9),
                              r.get("updated") or ""),
               reverse=False)
    counts = {}
    for r in cands:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    try:
        eligible = len(eligible_ideas())
    except Exception:  # noqa: BLE001 - a broken ideas store costs the count
        eligible = 0
    return {"candidates": cands,
            "fleet": {"queued": counts.get("queued", 0),
                      "building": counts.get("building", 0),
                      "max_building": _max_building()},
            "counts": counts, "eligible": eligible,
            "passive": _passive()}

"""Orphan-work sweeper — an inventory of work that never landed.

Vira's own branch-first workflow (server/worktree.py, scripts/branch.sh)
puts every dispatched session in its own worktree on its own claude/*
branch. Nothing tore down the ones a session never finished: a stalled
session leaves uncommitted edits sitting in a worktree nobody is looking
at; a session that finished cleanly but was never merged leaves a branch
with commits main never got; `worktree.tidy()` correctly REFUSES to
remove either (see its own docstring — keeping is the safe default), so
neither self-heals. This module is the other half: it finds what tidy
rightly left behind, ages it, and gives the owner one-click Resume / Merge
/ Discard instead of a manual `git worktree list` audit.

Two rungs, the house shape:

  sweep()   — deterministic. Plain git — `git worktree list`, `git branch
              --list "claude/*"`, `git status --porcelain`, `git rev-list`
              — joined against the durable job ledger for the stalled-
              session signal (a ledger row whose branch matches and whose
              status is orphaned/error). No model call, ever.
  refresh() — sweep() -> data/orphan-work.json -> ping the owner on
              genuinely NEW items (the jobboards baseline rule: the first-
              ever sweep never pings, only what appears afterwards does).

Everything here is READ-ONLY except the explicit actions (resume, merge,
discard, and land — the finish-and-merge chain), and none of those ever
reimplement branch.sh — they shell out to it and pass its stderr through
verbatim. The sweeper inventories and pings; nothing merges, discards,
resumes, or lands without an owner click ("Land" / "Land all" IS that
click: the owner deciding a row should reach main, with the finishing
session and the merge chained behind the one decision).

Store data/orphan-work.json is derived-plus-dismissals, like brief-state
and reconnect.json — NOT in the backup rotation (a lost dismissal is a
row reappearing, not lost work; the actual work lives in the worktrees
and branches themselves, which git already durably holds).

CRITICAL INTEGRATION FACT: the Resume action launches a session with cwd
already pointed at an EXISTING worktree — a leftover, not a fresh
dispatch. session.Sessions.launch() only placed a session into a NEW
worktree; re-entering an existing one left worktree/live_root unset on
the spec, which made runner._disarmed_guard fail-closed refuse to start
the session at all. session.py's branch-first block now forks on
worktree.is_worktree(root) to arm the same guard fields for this path too
— see server/session.py and tests/test_branch_guard_wiring.py's
ReentryIntoExistingWorktree.
"""
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil, jsonstore, joblog, sessiondiag, worktree

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "orphan-work.json"

STALE_AFTER_S = 6 * 3600     # a sweep older than this is served stale=true
ACTION_TIMEOUT = 600         # branch.sh merge/discard, same ceiling worktree.py uses
DIRTY_MTIME_CAP = 50         # dirty paths stat'd for the "when was this touched" signal
LAND_WAIT_S = 3 * 3600       # ceiling on waiting for a landing session to finish
LAND_POLL_S = 20             # how often the landing watcher re-reads the ledger

_actions_lock = threading.Lock()
_actions = {}                 # branch -> {name, status, output, started, finished}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- sweep

def _porcelain_worktrees():
    """[{"path": Path, "branch": "claude/x" or ""}] from `git worktree
    list --porcelain`. A detached-HEAD worktree carries branch=""."""
    out = gitutil.git(ROOT, "worktree", "list", "--porcelain", timeout=20)
    if out.returncode != 0:
        return []
    entries, cur = [], None
    for line in (out.stdout or "").splitlines():
        if line.startswith("worktree "):
            cur = {"path": Path(line[len("worktree "):].strip()), "branch": ""}
            entries.append(cur)
        elif line.startswith("branch ") and cur is not None:
            ref = line[len("branch "):].strip()
            cur["branch"] = (ref[len("refs/heads/"):]
                             if ref.startswith("refs/heads/") else "")
    return entries


def _dirty_lines(wt):
    """Non-empty `git status --porcelain` lines, or None if the read failed
    — the per-item degrade signal (never raises the whole sweep out)."""
    out = gitutil.git(wt, "status", "--porcelain", timeout=20)
    if out.returncode != 0:
        return None
    return [l for l in (out.stdout or "").splitlines() if l.strip()]


def _ahead_behind(branch):
    """(ahead, behind) of `branch` vs main, or (None, None) on failure.
    `main...branch` left-right count: left = main-only commits (behind),
    right = branch-only commits (ahead) — the same convention update.py
    uses for HEAD...@{upstream}."""
    out = gitutil.git(ROOT, "rev-list", "--left-right", "--count",
                      f"main...{branch}", timeout=20)
    if out.returncode != 0:
        return None, None
    parts = (out.stdout or "").split()
    if len(parts) != 2:
        return None, None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None
    return ahead, behind


def _tip_sha(cwd, branch):
    out = gitutil.git(cwd, "rev-parse", branch, timeout=20)
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _commit_time(branch):
    out = gitutil.git(ROOT, "log", "-1", "--format=%ct", branch, timeout=20)
    try:
        return float((out.stdout or "").strip())
    except (TypeError, ValueError):
        return None


def _dirty_mtime(wt, lines):
    """Newest mtime among up to DIRTY_MTIME_CAP dirty paths, or None. A
    deleted-file status line stats nothing and is skipped, not fatal."""
    best = None
    for line in lines[:DIRTY_MTIME_CAP]:
        rel = line[3:].strip()          # porcelain short format: "XY path"
        if " -> " in rel:               # a rename: "old -> new"
            rel = rel.split(" -> ", 1)[1]
        rel = rel.strip('"')
        if not rel:
            continue
        try:
            m = (Path(wt) / rel).stat().st_mtime
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def _job_for_branch(branch, ledger_by_branch):
    row = ledger_by_branch.get(branch)
    if not row:
        return None
    return {"id": row.get("id"), "title": joblog.name(row),
            "status": row.get("status"), "finished": row.get("finished"),
            # what was actually ASKED — the evidence a decision needs; the
            # machine preamble is squeezed so the task line survives the cap
            "prompt_head": " ".join((row.get("prompt") or "").split())[:280]}


def _porcelain_path(line):
    """One porcelain line -> the changed path. The 3-char status prefix is
    fixed-width ("XY "); a rename reads "old -> new" and the NEW path is
    the one worth showing."""
    rel = (line or "")[3:].strip()
    if " -> " in rel:
        rel = rel.split(" -> ", 1)[1]
    return rel.strip('"')


def _dirty_files(dirty_lines, limit=12):
    """Porcelain lines -> the changed paths, capped for the sweep payload.
    `context()` passes a far larger limit — a row is a summary, a decision
    is not."""
    files = []
    for line in (dirty_lines or [])[:max(limit * 2, 24)]:
        rel = _porcelain_path(line)
        if rel:
            files.append(rel)
    return files[:limit]


def _branch_commits(branch, ahead):
    """Subjects of the unmerged commits — the branch's own account of what
    it holds. Empty on any git failure (the per-item degrade rule)."""
    if not ahead:
        return []
    lg = gitutil.git(ROOT, "log", "--format=%s", f"main..{branch}",
                     "-n", "8", timeout=20)
    if lg.returncode != 0:
        return []
    return [l.strip() for l in (lg.stdout or "").splitlines() if l.strip()][:8]


def _failure_summary(branch):
    """A compact read of this branch's recorded failures for the row, or
    None. Never raises and never spends a model call — the sweep runs on
    every view open."""
    try:
        fails = sessiondiag.failures_for_branch(branch, limit=4)
    except Exception:  # noqa: BLE001 — a row must survive a bad ledger
        return None
    if not fails:
        return None
    top = fails[0]["diagnosis"]
    return {
        "count": len(fails),
        "kind": top.get("kind"),
        "harness": bool(top.get("harness")),
        "certain": bool(top.get("certain")),
        "headline": top.get("headline"),
        "why": top.get("why"),
        "fix": top.get("fix"),
        # The repeat is the actionable part: it says retrying unchanged is
        # expected to fail, which is precisely what Land used to do.
        "repeated": sessiondiag.repeated_kind(fails),
    }


def _make_item(branch, wt, dirty_lines, ledger_by_branch):
    """One inventory row, or None when there is nothing unlanded (clean
    working tree AND fully merged into main — worktree.tidy() owns
    removing that case, the sweeper never does)."""
    ahead, behind = _ahead_behind(branch)
    if ahead is None:
        return None                     # could not read — degrade away
    dirty = len(dirty_lines) if dirty_lines is not None else 0
    if dirty == 0 and ahead == 0:
        return None
    tip = _tip_sha(wt or ROOT, branch)
    ts = _commit_time(branch) or time.time()
    if dirty and wt:
        m = _dirty_mtime(wt, dirty_lines)
        if m:
            ts = max(ts, m)
    job = _job_for_branch(branch, ledger_by_branch)
    if job and job.get("status") == "running":
        # A live session owns this tree: its dirt is work IN PROGRESS, not
        # orphan work, and a row here would carry a Resume button that
        # drops a second bypassPermissions agent into a tree another agent
        # is writing (the judge's high finding). The row appears on the
        # first sweep after the session finishes, errors, or the
        # supervisor reaps it as orphaned. A parked reply-window session
        # keeps status "running" on purpose and is equally excluded — it
        # is the owner's to answer, and the decision layer surfaces it.
        return None
    return {
        "key": f"{branch}:{(tip or '')[:12]}:{dirty}",
        "branch": branch,
        "worktree": str(wt) if wt else "",
        "dirty": dirty, "ahead": ahead, "behind": behind,
        "files": _dirty_files(dirty_lines),
        "commits": _branch_commits(branch, ahead),
        "kind": "dirty" if dirty else "unmerged",
        "instance_running": bool(wt and worktree._instance_alive(wt)),
        "stalled": bool(job and job.get("status") in ("orphaned", "error")),
        # WHY it stopped, deterministically — no model call, so every row
        # carries it. This is the half that was missing when three
        # sessions died the same way on one branch and each row still
        # read as an unexplained stall: the cause was on disk the whole
        # time and nothing put it on the row.
        "failure": _failure_summary(branch),
        "job": job,
        "last_activity": ts,
        "last_activity_iso": datetime.fromtimestamp(
            ts, tz=timezone.utc).isoformat(timespec="seconds"),
        "age_days": round((time.time() - ts) / 86400, 1),
    }


def sweep():
    """Deterministic inventory: every worktree/branch carrying unlanded
    work, plus one row for unpushed commits on main. Read-only git the
    whole way; a failed sub-command degrades that one item away rather
    than raising the sweep out."""
    from . import update
    ledger_by_branch = {}
    for r in joblog.list_records():         # ascending -> last write wins == newest
        b = r.get("branch")
        if b:
            ledger_by_branch[b] = r

    items, seen = [], set()
    for wt_entry in _porcelain_worktrees():
        wt, branch = wt_entry["path"], wt_entry["branch"]
        if not worktree.is_worktree(wt):
            continue                        # the primary checkout itself
        if not branch:
            continue                        # detached HEAD — nothing to act on
        it = _make_item(branch, wt, _dirty_lines(wt), ledger_by_branch)
        if it:
            items.append(it)
        seen.add(branch)

    out = gitutil.git(ROOT, "branch", "--list", "claude/*",
                      "--format=%(refname:short)", timeout=20)
    if out.returncode == 0:
        for branch in (out.stdout or "").splitlines():
            branch = branch.strip()
            if not branch or branch in seen:
                continue
            it = _make_item(branch, None, None, ledger_by_branch)
            if it:
                items.append(it)

    st = update.status(fetch=False)
    if st.get("git") and st.get("remote") and (st.get("ahead") or 0) > 0:
        items.append({
            "key": f"unpushed-main:{st.get('sha') or ''}",
            "branch": "main", "worktree": "", "dirty": 0,
            "ahead": st["ahead"], "behind": st.get("behind", 0),
            "kind": "unpushed", "instance_running": False, "stalled": False,
            "job": None, "last_activity": time.time(),
            "last_activity_iso": _now_iso(), "age_days": 0.0,
        })
    return items


# ---------------------------------------------------------------- store

def _blank():
    return {"last_sweep": None, "items": [], "dismissed": {}, "notified": {},
            "reads": {}, "baseline_done": False}


def _read():
    s = jsonstore.read(STORE, _blank())
    if not isinstance(s, dict):
        s = _blank()
    for k, v in _blank().items():
        s.setdefault(k, v)
    return s


def refresh():
    """Regenerate the store from a fresh sweep and ping on genuinely new
    orphans. The FIRST sweep ever (baseline_done False) stamps every key
    into `notified` without pinging — announcing the whole standing
    backlog the first time this ships would bury the one ping that
    matters, the same rule jobboards.py uses for a newly-registered
    board. Safe to call from any thread."""
    items = sweep()
    now = _now_iso()
    keys = {it["key"] for it in items}
    ping = {}

    def fn(s):
        s["items"] = items
        s["last_sweep"] = now
        notified = dict(s.get("notified") or {})
        dismissed = dict(s.get("dismissed") or {})
        new_keys = sorted(k for k in keys if k not in notified)
        if not s.get("baseline_done"):
            for k in keys:
                notified[k] = now
            s["baseline_done"] = True
        elif new_keys:
            actionable = [k for k in new_keys if k not in dismissed]
            if actionable:
                oldest = min((it for it in items if it["key"] in actionable),
                            key=lambda it: it.get("last_activity") or 0.0)
                n = len(actionable)
                ping["text"] = (
                    f"Vira: {n} piece{'s' if n != 1 else ''} of unlanded "
                    f"work — oldest {oldest['branch']}, "
                    f"{oldest.get('age_days', 0):.0f}d old")
                ping["key"] = "orphanwork:" + hashlib.sha1(
                    ",".join(sorted(actionable)).encode()).hexdigest()[:16]
            for k in new_keys:
                notified[k] = now
        jsonstore.prune_oldest(notified, 200)
        jsonstore.prune_oldest(dismissed, 200)
        s["notified"] = notified
        s["dismissed"] = dismissed
        return s

    jsonstore.mutate(STORE, fn, _blank(), indent=1, ensure_ascii=False)
    if ping.get("text"):
        try:
            from . import notify
            notify.agent_ping(ping["text"], key=ping["key"])
        except Exception:  # noqa: BLE001 — a ping must never break the sweep
            pass
    _kick_assess()
    return items


def compose():
    """Every non-dismissed item, stalest-first, with any in-flight action
    state folded in. `unpushed-main` is pinned last — it has no age worth
    sorting on and it is not a worktree to resume."""
    s = _read()
    dismissed = set(s.get("dismissed") or {})
    reads = s.get("reads") or {}
    items = [dict(it) for it in (s.get("items") or [])
             if it.get("key") not in dismissed]
    with _actions_lock:
        for it in items:
            # The assessment is keyed on the item KEY, which embeds tip sha
            # + dirty count — so a new commit or edit mints a new key and
            # the stale read simply stops matching (the dismissal rule).
            r = reads.get(it.get("key") or "")
            if r:
                it["read"] = {"verdict": r.get("verdict"),
                              "why": r.get("why")}
            a = _actions.get(it.get("branch") or "")
            if a:
                it["action"] = {"name": a["name"], "status": a["status"],
                                "output": a["output"]}

    def sort_key(it):
        if it.get("kind") == "unpushed":
            return (1, 0.0)
        return (0, it.get("last_activity") or 0.0)
    items.sort(key=sort_key)

    last_sweep = s.get("last_sweep")
    dt = _parse_iso(last_sweep) if last_sweep else None
    stale = dt is None or (
        (datetime.now(timezone.utc) - dt).total_seconds() > STALE_AFTER_S)
    return {"items": items, "last_sweep": last_sweep, "stale": stale}


def dismiss(key, restore=False):
    def fn(s):
        d = dict(s.get("dismissed") or {})
        if restore:
            d.pop(key, None)
        else:
            d[key] = _now_iso()
        s["dismissed"] = d
        return s
    jsonstore.mutate(STORE, fn, _blank(), indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- actions

def _run_action(slug, argv, name, post_ok=None):
    """Run one `scripts/branch.sh <argv>` action for `slug` in a daemon
    thread, guarded so only one action runs per branch at a time. branch.sh
    is the sole authority for preflight/refusals — its combined
    stdout+stderr is kept verbatim, never second-guessed. `post_ok(text)`
    runs after a successful call and may return extra text to append (the
    post-merge push). Returns (started: bool, detail: str) immediately;
    the outcome lands on the item via compose()'s `action` field."""
    branch = f"claude/{slug}"
    with _actions_lock:
        cur = _actions.get(branch)
        if cur and cur.get("status") == "running":
            return False, "an action is already running for this branch"
        _actions[branch] = {"name": name, "status": "running", "output": "",
                            "started": _now_iso(), "finished": None}

    def run():
        text, ok = "", False
        try:
            out = subprocess.run(
                [str(ROOT / "scripts" / "branch.sh"), *argv], cwd=str(ROOT),
                capture_output=True, text=True, timeout=ACTION_TIMEOUT,
                check=False)
            text = ((out.stdout or "") + (out.stderr or "")).strip()
            ok = out.returncode == 0
            if ok and post_ok:
                extra = post_ok(text)
                if extra:
                    text = text + "\n" + extra
        except (OSError, subprocess.SubprocessError) as e:
            text = f"{name} failed to run: {e}"
        try:
            refresh()             # re-sweep BEFORE the status flips off "running" —
        except Exception:  # noqa: BLE001 — a re-sweep must never crash the thread
            pass           # else a caller polling for completion could act on a
        with _actions_lock:      # list that has not caught up with this action yet
            _actions[branch] = {
                "name": name, "status": "ok" if ok else "failed",
                "output": text[-4000:],
                "started": _actions.get(branch, {}).get("started") or _now_iso(),
                "finished": _now_iso()}

    threading.Thread(target=run, daemon=True,
                     name=f"vira-orphan-{name}-{slug}"[:60]).start()
    return True, "started"


def _push_and_note(merge_text, slug=None):
    """The post-merge epilogue: push (the standing push-by-default rule),
    name the restart if server/ changed — the server never restarts
    itself — and, once main is pushed, tear the spent branch down."""
    push = gitutil.git(ROOT, "push", timeout=60)
    pushed = push.returncode == 0
    lines = [("push: " + ((push.stdout or "") + (push.stderr or "")).strip())
             if pushed else
             ("push FAILED: "
              + ((push.stderr or "") + (push.stdout or "")).strip())]
    if "server/" in merge_text:
        lines.append(
            "server code changed — restart is the owner's: "
            "launchctl kickstart -k gui/501/nyc.durham.vira")
    # TEARDOWN IS PART OF LANDING, NOT A STEP LEFT FOR SOMEONE.
    #
    # A landing that ended at the push left the worktree, the local branch
    # and origin/<branch> behind every time — measured 2026-09-02: 26 of 30
    # worktrees on this machine were finished work nobody tore down. The
    # push is the gate, deliberately: `branch.sh discard` keeps a PR open
    # and origin/<branch> in place while main is unpushed, so running it
    # BEFORE the push would only do half the job and a failed push leaves
    # the branch for the sweeper's unpushed-main row to name.
    if pushed and slug:
        lines.append(_teardown(slug))
    return "\n".join(lines)


def _teardown(slug):
    """The last hop of a landing: `branch.sh discard <slug>` after the
    push. On a branch.sh whose merge already tore the local worktree and
    branch down this finishes the PR side (the wait for GitHub's Merged
    flip, then origin/<branch>); on one that did not, it does all of it.
    cmd_discard tolerates a worktree and a local branch that are already
    gone, which is what makes the same call right in both cases. Never a
    second implementation of teardown — branch.sh owns the rules, and a
    refusal there is passed through as a NOTE naming the manual command,
    never as a failure of work that has already landed."""
    argv = [str(ROOT / "scripts" / "branch.sh"), "discard", slug]
    try:
        out = subprocess.run(
            argv, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=ACTION_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return (f"teardown did not run ({e}) — run: "
                f"scripts/branch.sh discard {slug}")
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    if out.returncode != 0:
        return (f"teardown HELD — run: scripts/branch.sh discard {slug}\n"
                + text)
    return "teardown: " + text


def merge(slug):
    """branch.sh merge <slug>, then the push/restart/teardown epilogue."""
    return _run_action(slug, ["merge", slug], "merge",
                       post_ok=lambda text: _push_and_note(text, slug))


def discard(slug, force=False):
    argv = ["discard", slug] + (["--force"] if force else [])
    return _run_action(slug, argv, "discard")


RESUME_PROMPT = """You are resuming stalled work in a branch-first repository.

Worktree: {worktree}
Branch: {branch}
Live checkout (read-only for you — do not edit it): {live_root}

An earlier session started this work and never finished it — it sat \
uncommitted, unmerged, or the session that started it stalled. Below is \
the worktree's current state.
{job_block}
Uncommitted changes (git status --porcelain):
{status}

Commits on this branch not yet on main (git log --oneline main..branch):
{log}

Finish the work: read what is already there, understand what was being \
built, and carry it through to completion. Run the test suite. Do NOT \
merge and do NOT push — the owner decides that. When you are done (or if \
you get stuck and need the owner's judgment), end with the usual decision \
menu: merge it, spin up a test instance, or discard it.

Make that final handoff a compact review brief: lead with the outcome, name \
the workflow or before/after relationship, list verification, and state what \
remains. If this changes a visible surface, capture a representative public-\
safe screenshot or rendering through the repo's test-instance process and \
keep it in the branch with useful alt text. Never capture personal data and \
never add a decorative image merely to fill space.
"""


def _prompt_fields(item):
    """The shared evidence block both session prompts embed: worktree
    status, unmerged commits, and the originating-job line."""
    wt = item.get("worktree") or ""
    branch = item.get("branch") or ""
    status_out, log_out = "(worktree not available)", "(worktree not available)"
    if wt:
        st = gitutil.git(Path(wt), "status", "--porcelain", timeout=20)
        status_out = (st.stdout or "").strip() or "(clean)"
        lg = gitutil.git(Path(wt), "log", "--oneline", f"main..{branch}",
                         "-n", "30", timeout=20)
        log_out = (lg.stdout or "").strip() or "(no unmerged commits)"
    job = item.get("job")
    job_block = (f"\nThe originating job was: \"{job.get('title')}\" "
                f"(final status: {job.get('status')})\n") if job else ""
    return {"worktree": wt, "branch": branch, "live_root": str(ROOT),
            "job_block": job_block, "status": status_out[:4000],
            "log": log_out[:3000]}


def resume_prompt(item):
    """The composed resume prompt — also servable read-only for a passive
    instance to copy into another session (the apply-prompt pattern)."""
    return RESUME_PROMPT.format(**_prompt_fields(item))


# Row caps exist so the sweep payload stays small — every item is fetched
# on every render. They are the WRONG caps for a decision: the row shows
# 6 of 24 files and 8 commits truncated to 160 characters, which is a
# summary, and the owner is being asked to land or destroy the work behind
# it. `context()` is the unsummarized read, fetched only when he opens it.
CONTEXT_LOG = 200           # unmerged commits, with bodies
CONTEXT_STATUS = 400        # porcelain lines
VISUAL_MAX = 12             # enough for a useful contact sheet, not a wall
VISUAL_SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".webp", ".gif",
                             ".avif"))


def _branch_paths(item):
    """Every path this branch changes relative to main, plus dirty paths.

    The compact row only needs dirty paths. The foreground review needs the
    whole change map, including committed files, both to explain the work and
    to find screenshots/renderings the branch deliberately carries.
    """
    wt = item.get("worktree") or ""
    branch = item.get("branch") or ""
    paths = []
    if wt and branch and item.get("ahead"):
        diff = gitutil.git(Path(wt), "diff", "--name-only", "--diff-filter=ACMR",
                           f"main...{branch}", timeout=30)
        if diff.returncode == 0:
            paths.extend(l.strip() for l in (diff.stdout or "").splitlines()
                         if l.strip())
    if wt:
        # Expand untracked directories here: browser harnesses commonly leave
        # `.playwright-mcp/review.png` as one collapsed status row, and the
        # image inside is exactly the evidence this review is meant to find.
        st = gitutil.git(Path(wt), "status", "--porcelain",
                         "--untracked-files=all", timeout=30)
        if st.returncode == 0:
            paths.extend(_porcelain_path(l) for l in
                         (st.stdout or "").splitlines() if l.strip())
    # Git can name the same path in the branch diff and the dirty overlay.
    return list(dict.fromkeys(p for p in paths if p))


def _safe_visual_path(item, rel, allowed):
    """The filesystem half of visual_path, with an already-derived allowlist."""
    wt = item.get("worktree") or ""
    rel = str(rel or "")
    if (not wt or rel not in allowed or
            Path(rel).suffix.lower() not in VISUAL_SUFFIXES):
        return None
    try:
        root = Path(wt).resolve(strict=True)
        path = (root / rel).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _visuals(item, job_row, changed_paths):
    """Real image evidence already attached to the ask or made by the branch.

    This is discovery, not generation. A foreground card must never invent a
    screenshot merely to look richer. Idea images use their existing guarded
    endpoint; branch files use visual_path() and a separately guarded route.
    """
    rows = []
    idea_id = (job_row or {}).get("idea_id")
    if idea_id:
        try:
            from . import ideas, ideaimages
            idea = next((x for x in ideas.list_items()
                         if x.get("id") == idea_id), None)
            if idea:
                for im in ideaimages.images_of(idea):
                    if not im.get("missing"):
                        image_text = " ".join(str(im.get("text") or "").split())[:400]
                        rows.append({
                            "source": "ask", "idea_id": idea_id,
                            "id": im.get("id"), "name": im.get("name") or "Image",
                            "alt": image_text or im.get("name") or
                                   "Image attached to the original request",
                            "caption": image_text or "Original visual reference",
                        })
        except Exception:  # noqa: BLE001 -- visuals are an honest enhancement
            pass
    allowed = set(changed_paths)
    for rel in changed_paths:
        if Path(rel).suffix.lower() not in VISUAL_SUFFIXES:
            continue
        if _safe_visual_path(item, rel, allowed):
            rows.append({"source": "branch", "path": rel,
                         "name": Path(rel).name,
                         "alt": f"Visual artifact from {rel}",
                         "caption": rel})
    return rows[:VISUAL_MAX]


def visual_path(item, rel):
    """Resolve one review visual, constrained to a changed raster file.

    The route calling this receives an owner-controlled query string. It may
    serve only a path Git itself names as changed and only when the resolved
    file remains under this item's worktree. SVG is intentionally excluded:
    raster evidence cannot execute script when served back into the app.
    """
    wt = item.get("worktree") or ""
    rel = str(rel or "")
    if not wt:
        return None
    return _safe_visual_path(item, rel, set(_branch_paths(item)))


def visual_mime(path):
    """A conservative Content-Type for the raster-only review route."""
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _review_objective(job_row, branch_rows, fallback):
    """Recover the human objective even when the newest job is a Land run."""
    prompt = (job_row or {}).get("prompt") or ""
    origin = re.search(r'The originating job was:\s*"([^"]+)"', prompt)
    if origin:
        return origin.group(1).strip()
    # An idea dispatch is the strongest durable human-intent signal. Otherwise
    # the earliest branch job precedes any machine-authored Resume/Land runs.
    source = next((r for r in branch_rows if r.get("idea_id")), None)
    source = source or (branch_rows[0] if branch_rows else job_row)
    if source:
        value = source.get("command") or joblog.command(source)
        if not re.match(r"^(Finishing|Resuming|Diagnosing) stalled work\b",
                        value or "", re.I):
            return value
    return fallback


def context(item):
    """Everything known about one unlanded item, uncapped-in-practice and
    READ-ONLY: the originating job's full prompt, every unmerged commit,
    every changed file, and the exact prompt a Resume would dispatch.

    Nothing here launches, writes or sweeps — it is the review a decision
    needs, and it is deliberately safe to open on a passive instance."""
    wt = item.get("worktree") or ""
    branch = item.get("branch") or ""
    out = {"key": item.get("key") or "", "branch": branch, "worktree": wt,
           "kind": item.get("kind"),
           "prompt": "", "job": item.get("job"),
           "commits": [], "files": [], "status": "",
           "changed_files": [], "report": "", "objective": "",
           "visuals": [], "resume_prompt": "", "notes": []}

    job = item.get("job") or {}
    job_row = None
    records = joblog.list_records() if (job.get("id") or branch) else []
    branch_rows = [r for r in records if r.get("branch") == branch]
    if job.get("id"):
        # prompt_head on the row is squeezed to 280 chars for the sweep;
        # the ledger still holds what was actually asked.
        job_row = next((r for r in records
                        if r.get("id") == job["id"]), None)
        if job_row:
            out["prompt"] = job_row.get("prompt") or ""
            out["report"] = job_row.get("result") or ""
    if not out["prompt"]:
        out["prompt"] = job.get("prompt_head") or ""

    if branch and item.get("ahead"):
        lg = gitutil.git(ROOT, "log", "--format=%H%x00%s%x00%an%x00%ad%x00%b%x01",
                         "--date=short", f"main..{branch}",
                         "-n", str(CONTEXT_LOG), timeout=30)
        if lg.returncode == 0:
            for rec in (lg.stdout or "").split("\x01"):
                parts = rec.strip("\n").split("\x00")
                if len(parts) < 4 or not parts[0].strip():
                    continue
                out["commits"].append({
                    "sha": parts[0].strip()[:12], "subject": parts[1],
                    "author": parts[2], "date": parts[3],
                    "body": (parts[4].strip() if len(parts) > 4 else ""),
                })
        else:
            out["notes"].append("could not read the commit log")

    if wt:
        st = gitutil.git(Path(wt), "status", "--porcelain", timeout=30)
        if st.returncode == 0:
            lines = [l for l in (st.stdout or "").splitlines() if l.strip()]
            # The cap is REPORTED, never silent — a decision made against a
            # list that quietly stopped is the failure this view exists to
            # end (the no-silent-caps rule).
            if len(lines) > CONTEXT_STATUS:
                out["notes"].append(
                    f"{len(lines) - CONTEXT_STATUS} more changed paths not shown")
            out["status"] = "\n".join(lines[:CONTEXT_STATUS])
            out["files"] = _dirty_files(lines[:CONTEXT_STATUS], CONTEXT_STATUS)
        else:
            out["notes"].append("could not read the worktree status")
        try:
            out["resume_prompt"] = resume_prompt(item)
        except Exception:
            out["notes"].append("could not compose the resume prompt")
    elif item.get("kind") != "unpushed":
        out["notes"].append(
            "no worktree on disk — Resume cannot run until one is recreated "
            "with scripts/branch.sh")
    out["changed_files"] = _branch_paths(item)
    visual_job = next((r for r in branch_rows if r.get("idea_id")), job_row)
    out["visuals"] = _visuals(item, visual_job, out["changed_files"])
    fallback = (job.get("title") or branch.replace("claude/", "")
                or "Review this branch")
    out["objective"] = _review_objective(job_row, branch_rows, fallback)
    return out


def resume(item):
    """Dispatch a session to resume stalled work in ITEM's worktree.
    Synchronous — the launch call itself is cheap, the session runs
    detached like any other. Raises ValueError when there is no worktree
    to resume into (a branch this sweeper found with no linked worktree —
    recreating one automatically is out of scope; the owner reruns
    `branch.sh start`)."""
    wt = item.get("worktree")
    if not wt:
        raise ValueError(
            f"no worktree for {item.get('branch')} — recreate it with "
            "scripts/branch.sh before resuming")
    branch = item.get("branch") or ""
    _refuse_if_busy(branch)
    from . import session
    prompt = resume_prompt(item)
    return session.sessions.launch(
        prompt, cwd=wt, meta={"kind": "orphan-resume", "branch": item.get("branch")})


def _refuse_if_busy(branch):
    """The dispatch refusals resume() has always had, shared with land():
    checked FRESH at click time, never off the possibly day-old item row
    (the judge's high finding: sweep-time state must not authorize a
    second agent into a tree a session is writing)."""
    with _actions_lock:
        a = _actions.get(branch)
        if a and a.get("status") == "running":
            raise ValueError(
                f"an action ({a.get('name')}) is already running for "
                f"{branch} — wait for it to finish")
    newest = {}
    for r in joblog.list_records():     # ascending -> last write wins
        b = r.get("branch")
        if b:
            newest[b] = r
    row = newest.get(branch)
    if row and row.get("status") == "running":
        raise ValueError(
            f"a session is already live on {branch} "
            f"(job {row.get('id')}) — steer, answer, or finish it "
            "instead of dispatching over it")


# ---------------------------------------------------------------- landing

LAND_PROMPT = """You are finishing stalled work in a branch-first repository so it can LAND.

Worktree: {worktree}
Branch: {branch}
Live checkout (read-only for you — do not edit it): {live_root}

An earlier session started this work and never finished it. The owner has \
decided this branch should land on main. Your job is to carry the work to \
done and leave the branch READY TO MERGE — Vira merges and pushes it the \
moment you finish, so do NOT run the merge or push yourself.
{job_block}
Uncommitted changes (git status --porcelain):
{status}

Commits on this branch not yet on main (git log --oneline main..branch):
{log}

Do this, in order:
1. Read what is already there and understand what was being built.
2. Finish it — or, if it is already complete, verify it.
3. Run the test suite and fix what it catches.
4. COMMIT everything on this branch with a clear message. A clean, \
committed tree is the signal Vira merges on.
5. End with a compact review brief: lead with the outcome, name the workflow \
or before/after relationship, list verification, and state what remains. If \
this changes a visible surface, capture a representative public-safe screenshot \
or rendering through the repo's test-instance process and keep it in the branch \
with useful alt text. Never capture personal data and never add decorative filler.

If you conclude the work is WRONG — superseded, duplicated, or not worth \
landing — do NOT commit: say so plainly and stop. An uncommitted tree is \
never merged, so your refusal holds.
"""


LAND_DIAGNOSE_PROMPT = """You are diagnosing stalled work in a branch-first \
repository BEFORE any attempt to finish it.

Worktree: {worktree}
Branch: {branch}
Live checkout (read-only for you — do not edit it): {live_root}

The owner wants this branch to land, but an earlier session (or several) \
stopped without finishing. Your FIRST job is not to write code. It is to \
find out WHY it stopped, and to say so before anything else happens.
{job_block}{failure_block}
Uncommitted changes (git status --porcelain):
{status}

Commits on this branch not yet on main (git log --oneline main..branch):
{log}

DO THIS, IN ORDER:

1. DIAGNOSE. Read the failure evidence above, then confirm it against the \
worktree itself — the actual diff, the actual files. Where Vira has already \
named a cause, VERIFY it rather than assuming it; where it has not, work it \
out. Establish three things:
   - what the work was trying to do,
   - how far it actually got,
   - why it stopped, specifically enough that you could predict whether \
running the same step again would fail the same way.

2. STOP AND ASK. Do NOT start fixing. Call mcp__vira__ask_owner with your \
diagnosis as the question and concrete options. This is the whole point of \
this run: the owner decides what happens next, having read what you found. \
Your question must state, in plain language:
   - what stopped it and whether that cause is still present,
   - what is already done and what is left,
   - whether retrying unchanged would fail again.

   Offer options that fit what you actually found. Include, where each \
genuinely applies:
   - fix the cause and finish the work, then land it;
   - finish the work without touching the cause (only when the cause is \
already gone or cannot recur here — say which);
   - land what is already committed and drop the rest;
   - stop here and leave it for the owner.
   Mark the one you recommend and say why in its description.

3. ACT ON THE ANSWER, and only on the answer.
   - Told to fix and/or finish: do it, run the test suite, and COMMIT \
everything on this branch. A clean committed tree is the signal Vira \
merges on — do NOT merge or push yourself.
   - Told to stop, or to discard: do NOT commit. Leave the tree exactly as \
it is and end with your diagnosis. An uncommitted tree is never merged, so \
your refusal holds.
   - No answer arrives: stop and report. Do not guess.

Never re-run a step you have just established will fail the same way. If \
the cause is something you cannot fix from inside this worktree (a harness \
limit, a missing credential, a change needed in the live checkout), say so \
plainly in the question — that is a real finding, not a failure to deliver.

Keep the final diagnosis short and scannable for the foreground Forge card: \
outcome, workflow state, evidence, recommendation. Include real public-safe \
visual evidence when it already exists; never manufacture or capture personal data.
"""


def _failure_block(item):
    """The prior-failure read, as a prompt section. Empty when there is
    nothing recorded: a branch that merely went unfinished has no failure
    to diagnose, and a section that says "none" trains the reader to skip
    the section that matters."""
    try:
        block = sessiondiag.evidence_block(item.get("branch") or "")
    except Exception:  # noqa: BLE001 — evidence must never block a launch
        block = ""
    if not block:
        return ("\nNo failed session is recorded against this branch — it "
                "was left unfinished rather than broken. Diagnose why it "
                "stalled from the worktree itself.\n")
    return "\n" + block + "\n"


def land_prompt(item):
    return LAND_PROMPT.format(**_prompt_fields(item))


def land_diagnose_prompt(item):
    """The diagnose-first landing prompt — also servable read-only, so a
    passive instance can hand it to another session (the apply-prompt
    pattern)."""
    f = _prompt_fields(item)
    f["failure_block"] = _failure_block(item)
    return LAND_DIAGNOSE_PROMPT.format(**f)


def _job_row(jid):
    for r in joblog.list_records():
        if r.get("id") == jid:
            return r
    return None


def _set_action(branch, name, status, output):
    with _actions_lock:
        prev = _actions.get(branch) or {}
        _actions[branch] = {
            "name": name, "status": status, "output": output,
            "started": (prev.get("started")
                        if prev.get("status") == "running" else _now_iso()),
            "finished": None if status == "running" else _now_iso()}


def _merge_sync(slug):
    """branch.sh merge + the push/restart epilogue, synchronously.
    branch.sh is the sole authority for preflight/refusals — its combined
    output is passed through verbatim. Returns (ok, text)."""
    try:
        out = subprocess.run(
            [str(ROOT / "scripts" / "branch.sh"), "merge", slug],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=ACTION_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"merge failed to run: {e}"
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    if out.returncode != 0:
        return False, text
    extra = _push_and_note(text, slug)
    if extra:
        text = text + "\n" + extra
    return True, text


LAND_MODES = ("diagnose", "finish")


def norm_land_mode(mode):
    """A stored or posted mode, normalised. Anything unrecognised reads as
    DIAGNOSE — the safe direction: the worst a needless diagnosis costs is
    one decision card, while a wrong "finish" re-runs the step that just
    failed."""
    m = (mode if isinstance(mode, str) else "").strip().lower()
    return m if m in LAND_MODES else "diagnose"


def _launch_land_session(item, mode="diagnose"):
    wt = item.get("worktree")
    if not wt:
        raise ValueError(
            f"no worktree for {item.get('branch')} — recreate it with "
            "scripts/branch.sh before landing")
    mode = norm_land_mode(mode)
    prompt = (land_diagnose_prompt(item) if mode == "diagnose"
              else land_prompt(item))
    from . import session
    return session.sessions.launch(
        prompt, cwd=wt,
        meta={"kind": "orphan-land", "machine": True,
              "land_mode": mode, "branch": item.get("branch")})


def _land_tail(item, slug, branch, jid):
    """The blocking tail of a landing: wait out the finishing session
    (when there is one), re-check the tree, then merge + push. `jid` is
    None on the direct-merge path. Returns (ok, text)."""
    if jid:
        deadline = time.time() + LAND_WAIT_S
        row = _job_row(jid)
        while time.time() < deadline:
            row = _job_row(jid)
            if row and row.get("status") != "running":
                break
            time.sleep(LAND_POLL_S)
        else:
            return False, (f"landing session {jid} still running after "
                           f"{LAND_WAIT_S // 3600}h — Land again once it "
                           "finishes")
        if not row or row.get("status") != "done":
            st = (row or {}).get("status") or "gone"
            return False, (f"landing session {jid} ended '{st}' — nothing "
                           "was merged; open its terminal to see why")
        wt = item.get("worktree")
        dirty = _dirty_lines(Path(wt)) if wt else None
        if dirty:
            return False, ("landing session finished but left uncommitted "
                           "changes — it likely judged the work not worth "
                           "landing; read its summary before merging by hand")
        ahead, _behind = _ahead_behind(branch)
        if not ahead:
            return False, ("landing session finished with no commits ahead "
                           "of main — nothing to merge")
    return _merge_sync(slug)


def _land_finish(item, slug, branch, jid):
    """Run the landing tail, re-sweep BEFORE the action status flips off
    "running" (the _run_action ordering rule), then record the outcome."""
    try:
        ok, text = _land_tail(item, slug, branch, jid)
    except Exception as e:  # noqa: BLE001 — the outcome must always land
        ok, text = False, f"landing failed: {e}"
    try:
        refresh()
    except Exception:  # noqa: BLE001 — a re-sweep must never eat the outcome
        pass
    _set_action(branch, "land", "ok" if ok else "failed", (text or "")[-4000:])


def land(item, mode="diagnose"):
    """From a row to landed-on-main.

    A clean, committed branch merges + pushes directly — there is nothing
    to diagnose and nothing to finish.

    A dirty worktree gets a session dispatched into it first, and `mode`
    decides what that session is told to do:

      diagnose (DEFAULT) — establish why the earlier session stopped,
        then STOP and raise a decision card with options; it only writes
        code if the owner picks an option that says to. See
        LAND_DIAGNOSE_PROMPT.
      finish — the original behaviour: carry the work to done and commit.
        See LAND_PROMPT.

    Diagnose is the default because the alternative was measured and it
    failed: three sessions on one branch died at the identical step on
    2026-08-28, each having re-read the same code and re-started the same
    edit, and a fourth dispatched by Land would have been told only to
    "carry the work to done" — with nothing in its prompt saying three
    attempts had already died there.

    Either way the merge decision is unchanged and stays deterministic:
    this thread merges only a tree that ends CLEAN and AHEAD. A session
    that stops at the diagnosis leaves the tree dirty, so nothing merges
    — the refusal holds without needing a second mechanism.

    The wait state is in-process: a server restart mid-wait loses only
    the auto-merge hop — the session itself is detached and survives.

    Returns the session's job id (None on the direct-merge path).
    Raises ValueError on the same refusals resume() has."""
    branch = item.get("branch") or ""
    if item.get("kind") == "unpushed" or branch == "main":
        raise ValueError("main needs a push, not a landing")
    mode = norm_land_mode(mode)
    slug = branch.split("/", 1)[-1]
    _refuse_if_busy(branch)
    jid = _launch_land_session(item, mode) if item.get("dirty") else None
    _set_action(branch, "land", "running",
                ((f"diagnosing session {jid} is running — it will ask you "
                  "before changing anything" if mode == "diagnose" else
                  f"finishing session {jid} is running — merges on "
                  "completion") if jid else "merging…"))
    threading.Thread(target=_land_finish, args=(item, slug, branch, jid),
                     daemon=True, name=f"vira-orphan-land-{slug}"[:60]).start()
    return jid


def land_all(mode="diagnose"):
    """Land every non-dismissed row, SERIALLY — the merge protocol lands
    one branch at a time, and each dirty row's session runs to completion
    before the next row starts.

    `mode` is passed straight through to each row and defaults the same
    way land() does: diagnose. A sweep is exactly where the old
    behaviour was worst — it would re-dispatch into every unseen failure
    in turn — so the default here is the cautious one, and each row that
    needs a decision raises its own card rather than guessing. Pass
    "finish" to take the old straight-to-work pass.

    Returns how many rows the pass will attempt; progress rides each
    row's action field."""
    m = norm_land_mode(mode)
    todo = [it for it in compose()["items"]
            if it.get("kind") != "unpushed"
            and (it.get("branch") or "") not in ("", "main")]
    if not todo:
        return 0

    def run():
        for it in todo:
            branch = it.get("branch") or ""
            slug = branch.split("/", 1)[-1]
            try:
                _refuse_if_busy(branch)
                jid = (_launch_land_session(it, m) if it.get("dirty")
                       else None)
            except ValueError as e:
                _set_action(branch, "land", "failed", str(e))
                continue
            _set_action(branch, "land", "running",
                        ((f"diagnosing session {jid} is running — it will "
                          "ask you before changing anything"
                          if m == "diagnose" else
                          f"finishing session {jid} is running — merges on "
                          "completion") if jid else "merging…"))
            _land_finish(it, slug, branch, jid)

    threading.Thread(target=run, daemon=True,
                     name="vira-orphan-land-all").start()
    return len(todo)


# ---------------------------------------------------------------- assessment
#
# The owner's complaint that earned this (2026-08-05): "I'm looking at
# random session names and I just have to arbitrarily decide if I want to
# land it, resume it, or discard it." A row that asks for a verdict must
# carry its evidence — so every item now ships what was asked (the ledger
# prompt head), what changed (files, commit subjects), and ONE model-pass
# recommendation with the reason on the row. The recommendation is a READ,
# never an action: nothing lands or discards on its say-so.

READS_MAX = 200
VERDICTS = ("land", "resume", "discard")

ASSESS_PROMPT = """You are Vira's release reviewer. Below is every piece of UNLANDED work \
in the owner's repo: agent worktrees holding uncommitted edits, and branches \
with commits never merged into main. For each item recommend exactly one of:
- "land"    — finished, coherent work worth merging into main
- "resume"  — real work, but unfinished or unclear; a session should finish or inspect it
- "discard" — stale, duplicated, superseded, or a runaway experiment

Judge only from the evidence given: what the originating job asked (when \
known), the files changed, the commit subjects, and the age. Be decisive and \
skeptical — several parallel experiments editing the same file are usually \
duplicates; a diff far wider than its stated task is usually a runaway. When \
genuinely unsure prefer "resume" over "land": merging junk is worse than a look.

Reply with STRICT JSON only — a list of objects \
{{"key": "...", "verdict": "...", "why": "..."}} covering every item. "why" \
is ONE short plain sentence the owner reads on the row.

ITEMS:
{items}
"""

_assess_lock = threading.Lock()
_assess_running = False


def _evidence_lines(it):
    """One item's evidence block for the assessment prompt."""
    out = [f"key: {it.get('key')}",
           f"  branch: {it.get('branch')} — {it.get('age_days', 0)}d old, "
           f"{it.get('dirty', 0)} uncommitted change(s), "
           f"{it.get('ahead', 0)} unmerged commit(s)"]
    job = it.get("job") or {}
    if job.get("prompt_head"):
        out.append(f"  job asked: {job['prompt_head']}")
    elif job.get("title"):
        out.append(f"  job: {job['title']}")
    if it.get("commits"):
        out.append("  commits: " + " | ".join(it["commits"]))
    if it.get("files"):
        out.append("  files: " + ", ".join(it["files"]))
    return "\n".join(out)


def assess_missing():
    """ONE suggest.complete pass over every item whose key has no cached
    read. Grounded-or-dropped (the evidence.py discipline): a row naming
    an unknown key or an off-vocabulary verdict is discarded, never
    coerced. Returns how many reads were stored."""
    s = _read()
    reads = s.get("reads") or {}
    todo = [it for it in (s.get("items") or [])
            if it.get("kind") != "unpushed" and it.get("key") not in reads]
    if not todo:
        return 0
    from . import suggest
    prompt = ASSESS_PROMPT.format(
        items="\n".join(_evidence_lines(it) for it in todo))
    try:
        raw = suggest.complete(prompt)
        m = re.search(r"\[.*\]", raw or "", re.S)
        rows = json.loads(m.group(0)) if m else []
    except Exception:  # noqa: BLE001 — an unassessed row is the honest degrade
        return 0
    valid = {it["key"] for it in todo}
    now = _now_iso()
    accepted = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        k = r.get("key")
        v = str(r.get("verdict") or "").strip().lower()
        why = " ".join(str(r.get("why") or "").split())[:240]
        if k in valid and v in VERDICTS and why:
            accepted[k] = {"verdict": v, "why": why, "when": now}
    if not accepted:
        return 0

    def fn(store):
        cur = dict(store.get("reads") or {})
        for k, v in accepted.items():
            cur.setdefault(k, v)      # never re-adjudicate behind a cached read
        if len(cur) > READS_MAX:      # prune by stamp; values are dicts, so
            for k in sorted(cur, key=lambda x: cur[x].get("when", ""))[
                    :len(cur) - READS_MAX]:
                cur.pop(k, None)
        store["reads"] = cur
        return store

    jsonstore.mutate(STORE, fn, _blank(), indent=1, ensure_ascii=False)
    return len(accepted)


def _kick_assess():
    """Run the assessment on a daemon thread, one at a time, and never on
    a passive instance (the worker convention — a test clone's sweep must
    not spend model calls on every view open). refresh() calls this, so a
    row is assessed within one sweep of appearing and the cache makes
    every later sweep free."""
    global _assess_running
    if os.environ.get("VIRA_PASSIVE"):
        return
    with _assess_lock:
        if _assess_running:
            return
        _assess_running = True

    def run():
        global _assess_running
        try:
            assess_missing()
        except Exception:  # noqa: BLE001 — a failed pass just leaves rows unassessed
            pass
        finally:
            with _assess_lock:
                _assess_running = False

    threading.Thread(target=run, daemon=True,
                     name="vira-orphan-assess").start()

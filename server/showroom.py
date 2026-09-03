"""The Showroom - every draft branch on this machine as a card you can
test-drive, read, and rule on.

Take two (2026-09-03). The first Showroom (PR #16, closed unmerged) was a
fleet builder: it staged ideas, launched build sessions, judged them, and
showed the candidates it had itself minted. It could not show the branch
that mattered most - the one some OTHER session had already built and left
- which is exactly what the owner had: eight-odd unlanded sessions, dozens
of worktrees, and no surface that put them side by side. This module is
the viewer for ALL of it, and it builds nothing.

WHAT A CARD IS. Every `claude/*` branch git knows about (a linked worktree
or a bare local ref), banded by the one fact that decides what you can do
with it:

  session   a session is live on it right now (a parked reply window, a
            landing card waiting on you, a build still running). Nothing
            here lands or discards it - that session owns those verbs and
            the decision layer surfaces them - but its test instance can
            be launched and its work read.
  unlanded  commits ahead of main and/or uncommitted edits: the orphan-work
            sweeper's rows, joined by branch, so the card carries the
            sweeper's own evidence, Vira's land/resume/discard read and its
            recorded failures, and the verdict buttons call the sweeper's
            own routes. Never a second implementation of landing.
  landed    merged into main and clean. Under the current process this
            band should be EMPTY - a clean merge tears its worktree down
            and Land does the same after the push - so a row here is the
            pre-teardown backlog (measured 2026-09-02: 26 of 30 worktrees),
            and it exists so that cleanup is an informed click rather than
            an `rm -rf` over a directory whose name means nothing.

LAUNCH THE TEST. A card's Launch runs `branch.sh serve <slug> --local` in
a thread and the client opens a fresh tab on showroom-launch.html, which
polls this module and redirects the moment the instance answers. Several
instances may run at once (owner's call, 2026-09-03: side-by-side is the
point; disk is the cost and each card names it). `--local` is not a
default to relax: the snapshot behind a test instance is the owner's own
data, and bridging it to the tailnet needs his explicit approval per
instance (the standing branch.sh rule).

WHAT VIRA WRITES. Every card carries a title from a deterministic ladder
(the PR's title, else the ledger's name for the session, else the slug
read as words) and a blurb. A landed row's blurb is derived - merged when,
as which PR, what was left behind. A session or unlanded row gets ONE
model read per (tip, dirt, PR state) - what the branch does and what
state it is in, in plain English - cached in the store so an unchanged
branch never re-spends the call, and grounded-or-dropped: a reply naming
an unknown branch, or an empty blurb, is discarded. Until the read lands
the card shows the deterministic fallback, never a blank.

Store data/showroom.json (jsonstore discipline). Reads are open on a
passive instance - reviewing before deciding is what a test clone is for -
and every action that touches the real repo (serve, stop, cleanup)
refuses there by name, as orphanwork's do.
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil, joblog, jsonstore, orphanwork, settings, worktree

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "showroom.json"

BANDS = ("session", "unlanded", "landed")
SERVE_TIMEOUT = 600          # clone + provision + boot can take a minute or more
QUICK_TIMEOUT = 120
GH_TIMEOUT = 30
READS_MAX = 300
DESCRIBE_BATCH = 12          # branches described per model pass; the rest
                             # wait for the next sweep (never silently dropped:
                             # compose() reports how many are still pending)
BLURB_MAX = 320
PR_CACHE_S = 300             # gh pr list is one network call; cached this long

# Path -> area, so a card can say what a branch TOUCHES without a model.
# Verification and visuals win before the broad interface/engine rules -
# the same precedence the Runs view's orphanChangeAreas uses.
AREA_RULES = (
    ("tests", re.compile(r"^(tests?/)|(^|/)(test|spec)[_.-]", re.I)),
    ("visuals", re.compile(r"\.(png|jpe?g|webp|gif|avif|svg)$", re.I)),
    ("interface", re.compile(r"^(static/|templates/)|\.(css|html|js|tsx?|jsx?)$", re.I)),
    ("engine", re.compile(r"^(server/|scripts/)|\.(py|sh|ps1)$", re.I)),
    ("docs", re.compile(r"^(docs?/)|\.(md|markdown|rst)$", re.I)),
    ("config", re.compile(r"(^|/)(package|requirements|pyproject|config|settings)|\.(json|ya?ml|toml)$", re.I)),
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


def _refuse_if_passive(act):
    if _passive():
        raise PermissionError(
            f"this is a passive test instance - {act} runs branch.sh "
            "against the owner's real repo, so it only runs on the live Vira")


def _blank():
    return {"items": [], "reads": {}, "prs": {}, "prs_at": "",
            "last_sweep": None}


def _read():
    s = jsonstore.read(STORE, _blank())
    for k, v in _blank().items():
        s.setdefault(k, v)
    return s


def _mutate(fn):
    return jsonstore.mutate(STORE, fn, _blank(), indent=1,
                            ensure_ascii=False)


def _slug(branch):
    return (branch or "").split("/", 1)[-1]


def humanize(slug):
    """`attention-card-redesign` -> `Attention card redesign`; a dispatch
    slug's trailing job hash is dropped, since it names nothing."""
    words = [w for w in re.split(r"[-_]+", slug or "") if w]
    if len(words) > 1 and re.fullmatch(r"[0-9a-f]{6}", words[-1]):
        words = words[:-1]
    text = " ".join(words).strip()
    return text[:1].upper() + text[1:] if text else (slug or "")


# ---------------------------------------------------------------- pull requests

def _gh_prs():
    """Every PR on the repo, keyed by head branch, from ONE gh call.
    Best-effort: a dead or unauthenticated gh returns the cached map (or
    nothing), never raises - a card must render without GitHub."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", "300",
             "--json", "number,title,state,isDraft,headRefName,url,body,"
                       "mergedAt,updatedAt"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GH_TIMEOUT,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        rows = json.loads(out.stdout or "[]")
    except ValueError:
        return None
    prs = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        b = r.get("headRefName") or ""
        if not b:
            continue
        cur = prs.get(b)
        # Newest PR per branch wins - a branch re-PR'd after a close
        # carries the live one.
        if cur and (cur.get("number") or 0) > (r.get("number") or 0):
            continue
        prs[b] = {
            "number": r.get("number"), "title": r.get("title") or "",
            "state": (r.get("state") or "").upper(),
            "draft": bool(r.get("isDraft")), "url": r.get("url") or "",
            "body": (r.get("body") or "")[:4000],
            "merged_at": r.get("mergedAt") or "",
            "updated_at": r.get("updatedAt") or "",
        }
    return prs


def _prs(force=False):
    """The PR map, refreshed at most every PR_CACHE_S seconds."""
    s = _read()
    at = orphanwork._parse_iso(s.get("prs_at")) if s.get("prs_at") else None
    fresh = at and (datetime.now(timezone.utc) - at).total_seconds() < PR_CACHE_S
    if fresh and not force and s.get("prs"):
        return s["prs"]
    prs = _gh_prs()
    if prs is None:
        return s.get("prs") or {}

    def fn(st):
        st["prs"] = prs
        st["prs_at"] = _now_iso()
        return st
    _mutate(fn)
    return prs


# ---------------------------------------------------------------- git facts

def _local_branches():
    out = gitutil.git(ROOT, "branch", "--list", "claude/*",
                      "--format=%(refname:short)", timeout=20)
    if out.returncode != 0:
        return []
    return [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]


def _ref_facts():
    """{branch: (tip sha, commit epoch)} for every claude/* ref in ONE git
    call. The first live sweep spent ~260 subprocess spawns - two of them
    per branch just for the tip and its date - and a spawn out of the
    multi-gigabyte server process costs ~10x what it costs a bare python
    (measured 2026-09-03: 71s over HTTP against 8s in-process for the
    same sweep). Batch what git can batch."""
    out = gitutil.git(ROOT, "for-each-ref", "refs/heads/claude/",
                      "--format=%(refname:short)%09%(objectname)%09%(committerdate:unix)",
                      timeout=30)
    facts = {}
    if out.returncode != 0:
        return facts
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            facts[parts[0]] = (parts[1], float(parts[2]))
        except ValueError:
            continue
    return facts


def _merged_set():
    """The claude/* branches whose tip is reachable from main - one call.
    A branch here has ahead == 0 by definition, so its ahead/behind read
    is skipped; only unmerged branches pay for rev-list."""
    out = gitutil.git(ROOT, "branch", "--merged", "main", "--list", "claude/*",
                      "--format=%(refname:short)", timeout=30)
    if out.returncode != 0:
        return set()
    return {l.strip() for l in (out.stdout or "").splitlines() if l.strip()}


def _merge_index():
    """{branch: (merge sha, epoch)} for every --no-ff merge on main's first
    parent, from ONE log walk. branch.sh merges carry git's default
    subject, so the subject is the join."""
    out = gitutil.git(ROOT, "log", "--merges", "--first-parent",
                      "--format=%H%x09%ct%x09%s", "main", timeout=30)
    idx = {}
    if out.returncode != 0:
        return idx
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        m = re.match(r"Merge branch '([^']+)'", parts[2])
        if not m or m.group(1) in idx:
            continue                  # newest first: the first hit is the landing
        try:
            idx[m.group(1)] = (parts[0], float(parts[1]))
        except ValueError:
            continue
    return idx


def _merge_commit(branch, facts=None, merges=None):
    """(sha, epoch) of the commit that landed `branch` on main: the --no-ff
    merge branch.sh writes, else - for a branch that landed by fast-forward
    or squash and so has no merge commit - the branch's own tip, which is
    on main by definition of the landed band. Its date is when the work
    last moved; its files are the last commit's, an honest floor rather
    than nothing. `facts`/`merges` are the batched reads; a caller
    without them pays the per-branch calls."""
    if merges is None:
        merges = _merge_index()
    if branch in merges:
        return merges[branch]
    if facts is None:
        facts = _ref_facts()
    if branch in facts:
        return facts[branch]
    return "", None


def _merge_paths_many(shas):
    """{sha: [paths]} for many merge commits in ONE git show: each commit's
    block opens with a NUL-marked header line, then its --name-only paths
    against the first parent."""
    shas = [x for x in dict.fromkeys(shas) if x]
    if not shas:
        return {}
    out = gitutil.git(ROOT, "show", "--format=%x00%H", "--name-only",
                      "--first-parent", *shas, timeout=60)
    if out.returncode != 0:
        return {}
    paths, cur = {}, None
    for line in (out.stdout or "").splitlines():
        if line.startswith("\x00"):
            cur = line[1:].strip()
            paths[cur] = []
        elif cur and line.strip():
            paths[cur].append(line.strip())
    return paths


def _merge_paths(sha):
    if not sha:
        return []
    out = gitutil.git(ROOT, "show", "--format=", "--name-only",
                      "--first-parent", sha, timeout=30)
    if out.returncode != 0:
        return []
    return [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]


def areas_of(paths):
    counts = {}
    for p in paths or []:
        name = "other"
        for area, rx in AREA_RULES:
            if rx.search(p):
                name = area
                break
        counts[name] = counts.get(name, 0) + 1
    return counts


def _instance(wt):
    """{port, alive, snapshot} for a worktree's test instance, or None."""
    if not wt:
        return None
    p = Path(wt) / ".test-instance.json"
    port = None
    try:
        port = int(json.loads(p.read_text(encoding="utf-8")).get("port"))
    except (OSError, ValueError, TypeError, AttributeError):
        port = None
    alive = worktree._instance_alive(wt)
    snapshot = (Path(wt) / "data" / ".test-snapshot").exists()
    if port is None and not alive and not snapshot:
        return None
    return {"port": port if alive else None, "alive": alive,
            "snapshot": snapshot}


def _job_running(job):
    return bool(job and job.get("status") == "running")


def _make_item(branch, wt, ledger_by_branch, orphan_by_branch, prs,
               rows_by_branch=None, batch=None):
    """One card's facts, or None when git cannot read the branch. `batch`
    carries the sweep-wide reads (ref facts, the merged set, the merge
    index); a landed row's paths are left for the sweep to fill from one
    batched `git show` (`_merge_sha` on the item)."""
    batch = batch or {}
    facts = batch.get("facts")
    merged = batch.get("merged")
    if merged is not None and branch in merged:
        ahead, behind = 0, None       # reachable from main: nothing ahead
    else:
        ahead, behind = orphanwork._ahead_behind(branch)
        if ahead is None:
            return None
    dirty_lines = orphanwork._dirty_lines(wt) if wt else None
    dirty = len(dirty_lines) if dirty_lines is not None else 0
    if facts is not None and branch in facts:
        tip, ts = facts[branch]
    else:
        tip = orphanwork._tip_sha(wt or ROOT, branch)
        ts = orphanwork._commit_time(branch)
    ts = ts or time.time()
    job = orphanwork._job_for_branch(branch, ledger_by_branch)
    pr = prs.get(branch)
    if dirty and wt and dirty_lines:
        m = orphanwork._dirty_mtime(wt, dirty_lines)
        if m:
            ts = max(ts, m)
    merged_sha, merged_at = "", None
    if _job_running(job):
        band = "session"
    elif dirty or ahead:
        band = "unlanded"
    else:
        band = "landed"
        merged_sha, merged_at = _merge_commit(branch, facts, batch.get("merges"))
    # What the branch touches - the branch diff for live work; a landed
    # row's merge paths come from the sweep's one batched show.
    if band == "landed":
        paths = []                    # filled by the sweep's batched show
    else:
        paths = orphanwork._branch_paths(
            {"worktree": wt or "", "branch": branch, "ahead": ahead})
    orphan = orphan_by_branch.get(branch) or {}
    item = {
        "branch": branch, "slug": _slug(branch),
        "worktree": str(wt) if wt else "",
        "band": band, "dirty": dirty, "ahead": ahead, "behind": behind,
        "tip": (tip or "")[:12],
        "files": orphanwork._dirty_files(dirty_lines) if dirty_lines else [],
        "commits": orphanwork._branch_commits(branch, ahead),
        "areas": areas_of(paths), "paths_n": len(paths),
        "job": job,
        "pr": ({k: v for k, v in pr.items() if k != "body"} if pr else None),
        "instance": _instance(wt),
        "merged_sha": merged_sha[:12], "_merge_sha": merged_sha,
        "merged_at": (datetime.fromtimestamp(merged_at, tz=timezone.utc)
                      .isoformat(timespec="seconds") if merged_at else ""),
        "orphan_key": orphan.get("key") or "",
        "orphan_read": orphan.get("read"),
        "failure": orphan.get("failure"),
        "last_activity": ts,
        "age_days": round((time.time() - ts) / 86400, 1),
    }
    item["key"] = (f"{branch}:{item['tip']}:{dirty}:"
                   f"{(pr or {}).get('state', '')}:{band}")
    item["asked"] = _objective((rows_by_branch or {}).get(branch) or [])
    item["title"] = title_for(item)
    item["module_guess"] = module_guess(paths)
    # Thumbnail: a raster the branch itself carries (a screenshot it made),
    # served through the sweeper's guarded visual route - discovery, never
    # generation. Only an unlanded row has the key that route needs.
    item["visual"] = ""
    if band == "unlanded" and item["orphan_key"] and wt:
        try:
            vis = orphanwork._visuals(
                {"worktree": str(wt), "branch": branch, "ahead": ahead},
                None, paths)
            first = next((v for v in vis if v.get("source") == "branch"), None)
            if first:
                item["visual"] = first["path"]
        except Exception:  # noqa: BLE001 - a thumbnail is an enhancement
            pass
    return item


MACHINE_RE = re.compile(r"^(Finishing|Resuming|Diagnosing) stalled work\b", re.I)


def _objective(rows):
    """The human ask behind a branch, read off its ledger rows: an idea
    dispatch first, else the EARLIEST row that is not a machine-authored
    Land/Resume/Diagnose run. The newest row is exactly the wrong one -
    on the live ledger a branch that stalled and was landed twice carries
    "Finishing stalled work in a branch-first repository" as its newest
    name, which names the harness, not the work (the sweeper's
    _review_objective rule)."""
    if not rows:
        return ""
    source = next((r for r in rows if r.get("idea_id")), None)
    if source is None:
        source = next((r for r in rows
                       if not MACHINE_RE.match(joblog.command(r) or "")), None)
    if source is None:
        return ""
    return (joblog.command(source) or "").strip()


def module_guess(paths):
    """The module a branch is ABOUT, read off what it touches, with no
    model: the server module it changes most (`server/x.py` -> x), else
    the surface when it only touches static/, else the first path's top
    directory. The describe pass may replace it with a vocabulary tag;
    this is what a landed row and an undescribed card group under."""
    counts = {}
    for p in paths or []:
        m = re.match(r"^server/([a-z0-9_]+)\.py$", p)
        if m and m.group(1) not in ("main", "settings", "__init__"):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if counts:
        return max(sorted(counts), key=lambda k: counts[k]).replace("_", "-")
    if any(p.startswith("static/") for p in paths or []):
        return "interface"
    if any(p.startswith("scripts/") for p in paths or []):
        return "branch-tooling"
    if any(p.startswith("tests/") for p in paths or []):
        return "tests"
    return "other"


def title_for(item):
    """The deterministic title ladder: PR title, the ledger's name for the
    ORIGINATING session, humanized slug. Vira never invents a title - a
    wrong name on a card is worse than a plain one."""
    pr = item.get("pr") or {}
    if pr.get("title"):
        return pr["title"].strip()
    asked = (item.get("asked") or "").strip()
    if asked and not MACHINE_RE.match(asked) \
            and not asked.lower().startswith(("you are ", "showroom build")):
        return asked[:120]
    # A dispatch slug that is the machine preamble ("you-are-vira-s-coding-
    # agent-work-<hash>") names nothing; the branch's own first commit
    # subject is the work's account of itself.
    slug = item.get("slug") or ""
    commits = item.get("commits") or []
    if commits and re.match(r"^you-are-", slug):
        return commits[-1][:120]
    return humanize(slug or item.get("branch"))


def _when(iso):
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    return settings.strf(d, "%b %-d")


def fallback_blurb(item):
    """What the card says before (or without) a model read. States facts
    the sweep already established, never a guess about intent."""
    band = item.get("band")
    pr = item.get("pr") or {}
    if band == "landed":
        bits = ["Merged into main"]
        if item.get("merged_at"):
            bits[0] += f" on {_when(item['merged_at'])}"
        if pr.get("number"):
            bits[0] += f" as PR #{pr['number']}"
        left = ("its worktree was never torn down" if item.get("worktree")
                else "only the local branch ref is left")
        return f"{bits[0]}. Nothing unlanded remains; {left}."
    parts = []
    if item.get("ahead"):
        parts.append(f"{item['ahead']} commit{'s' if item['ahead'] != 1 else ''} ahead of main")
    if item.get("dirty"):
        parts.append(f"{item['dirty']} uncommitted change{'s' if item['dirty'] != 1 else ''}")
    head = ", ".join(parts) or "no unlanded work read yet"
    if band == "session":
        job = item.get("job") or {}
        return (f"A session is live on this branch ({job.get('title') or job.get('id') or 'unnamed'}) - "
                f"{head}. Its landing is that session's to decide.")
    commits = item.get("commits") or []
    tail = f' Latest commit: "{commits[0]}".' if commits else ""
    return f"{head[:1].upper()}{head[1:]}.{tail}"


# ---------------------------------------------------------------- sweep

ORPHAN_FRESH_S = 300        # re-run the orphan sweeper only past this age


def _orphan_fresh():
    try:
        last = orphanwork.compose().get("last_sweep")
        dt = orphanwork._parse_iso(last) if last else None
    except Exception:  # noqa: BLE001
        return False
    return bool(dt) and (datetime.now(timezone.utc) - dt).total_seconds() < ORPHAN_FRESH_S


def sweep():
    """Every claude/* branch git knows, as card facts. Runs the orphan
    sweeper first - when its store is older than ORPHAN_FRESH_S - so the
    join carries fresh verdicts without paying its ~110 git spawns on
    every open; read-only git the whole way, one branch degrading away
    rather than the sweep dying."""
    global _wt_cache
    if not _orphan_fresh():
        try:
            orphanwork.refresh()
        except Exception:  # noqa: BLE001 - the join degrades, the cards still render
            pass
    _wt_cache = orphanwork._porcelain_worktrees()
    try:
        return _sweep_items()
    finally:
        _wt_cache = None


def _sweep_items():
    try:
        orphan_by_branch = {it.get("branch"): it
                            for it in orphanwork.compose().get("items", [])
                            if it.get("branch")}
    except Exception:  # noqa: BLE001
        orphan_by_branch = {}
    ledger_by_branch, rows_by_branch = {}, {}
    for r in joblog.list_records():          # ascending -> newest wins
        b = r.get("branch")
        if b:
            ledger_by_branch[b] = r
            rows_by_branch.setdefault(b, []).append(r)
    prs = _prs()
    # Three sweep-wide reads replace ~4 git spawns per branch.
    batch = {"facts": _ref_facts(), "merged": _merged_set(),
             "merges": _merge_index()}
    items, seen = [], set()
    for ent in _worktrees():
        wt, branch = ent["path"], ent["branch"]
        if wt == ROOT or not branch or not branch.startswith("claude/"):
            continue
        if not wt.is_dir():
            wt = None                  # a registration whose directory is gone
        seen.add(branch)
        try:
            it = _make_item(branch, wt, ledger_by_branch, orphan_by_branch, prs,
                            rows_by_branch, batch)
        except Exception:  # noqa: BLE001 - one bad worktree never kills the sweep
            it = None
        if it:
            items.append(it)
    for branch in _local_branches():
        if branch in seen:
            continue
        seen.add(branch)
        try:
            # A bare ref may still have a worktree git reports as detached
            # (mid-rebase) - _worktree_of's second rung finds it.
            it = _make_item(branch, _worktree_of(branch), ledger_by_branch,
                            orphan_by_branch, prs, rows_by_branch, batch)
        except Exception:  # noqa: BLE001
            it = None
        if it:
            items.append(it)
    # Every landed row's files, from ONE git show.
    paths_by_sha = _merge_paths_many(
        [it["_merge_sha"] for it in items if it.get("band") == "landed"])
    for it in items:
        sha = it.pop("_merge_sha", "")
        if it.get("band") == "landed" and sha in paths_by_sha:
            paths = paths_by_sha[sha]
            it["areas"] = areas_of(paths)
            it["paths_n"] = len(paths)
            it["module_guess"] = module_guess(paths)
    return items


def refresh():
    items = sweep()

    def fn(s):
        s["items"] = items
        s["last_sweep"] = _now_iso()
        return s
    _mutate(fn)
    _kick_describe()
    return items


# ---------------------------------------------------------------- serving

_serves = {}                  # branch -> {status, port, text, started}
_serves_lock = threading.Lock()


def _spawn(target, name):
    """The one place a daemon thread is started - a SEAM the tests pin to
    run the target inline. Patching threading.Thread itself is not an
    option: on Windows, subprocess reads its pipes on threads, so a
    module-wide patch breaks every git call the target makes (the CI
    lesson of 2026-09-03)."""
    threading.Thread(target=target, daemon=True, name=name[:60]).start()


def _branch_sh(args, timeout):
    out = subprocess.run(
        [str(ROOT / "scripts" / "branch.sh"), *args], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False)
    return out.returncode == 0, ((out.stdout or "")
                                 + (out.stderr or "")).strip()


def _item(branch):
    for it in _read().get("items") or []:
        if it.get("branch") == branch:
            return it
    return None


def _require(branch):
    it = _item(branch)
    if not it:
        raise KeyError(f"no showroom card for {branch} - refresh first")
    return it


_wt_cache = None            # the sweep's one worktree listing; None outside a sweep


def _worktrees():
    return _wt_cache if _wt_cache is not None else orphanwork._porcelain_worktrees()


def _worktree_of(branch):
    """The worktree holding `branch`, asked of git - never of the store, so
    a launch works on a branch the last sweep has not seen. A worktree
    MID-REBASE reports a detached HEAD and no branch, while git still
    reserves the branch for it ("already used by worktree at ..."), so the
    canonical .worktrees/<slug> path is the second rung: a directory there
    that is a linked worktree holds this branch's work whatever HEAD says.
    Never a registration whose directory is gone (a prunable entry)."""
    for ent in _worktrees():
        if ent["branch"] == branch and ent["path"] != ROOT and ent["path"].is_dir():
            return ent["path"]
    canon = _primary() / ".worktrees" / _slug(branch)
    if canon.is_dir() and (canon / ".git").is_file():
        return canon
    return None


def _primary():
    """The primary checkout - where .worktrees/ lives. ROOT is this
    module's own tree, which on a branch test instance is itself a linked
    worktree, and a canonical path derived from it would nest one worktree
    inside another (found by serving from a branch instance)."""
    return worktree.primary_root(ROOT) or ROOT


def _ensure_worktree(branch):
    """A bare local ref gets a worktree so it can serve: `git worktree add`
    at the canonical .worktrees/<slug> path, then `branch.sh adopt` for
    the venv symlink and the CLAUDE.md copy. Raises ValueError with git's
    own words when it cannot."""
    wt = _worktree_of(branch)
    if wt:
        return wt
    out = gitutil.git(ROOT, "rev-parse", "--verify", "--quiet",
                      f"refs/heads/{branch}", timeout=20)
    if out.returncode != 0:
        raise ValueError(f"{branch} is not a local branch")
    dest = _primary() / ".worktrees" / _slug(branch)
    dest.parent.mkdir(parents=True, exist_ok=True)
    gitutil.git(ROOT, "worktree", "prune", timeout=30)   # dead registrations only
    add = gitutil.git(ROOT, "worktree", "add", str(dest), branch, timeout=60)
    if add.returncode != 0:
        err = (add.stderr or add.stdout or "").strip()
        m = re.search(r"already used by worktree at '([^']+)'", err)
        if m and Path(m.group(1)).is_dir():
            return Path(m.group(1))       # reserved by a mid-rebase worktree
        raise ValueError("could not create a worktree: " + err[-300:])
    _branch_sh(["adopt", _slug(branch)], QUICK_TIMEOUT)
    return dest


def serve(branch):
    """Start the branch's test instance (branch.sh serve <slug> --local),
    asynchronously; the outcome lands in `serving` on compose(). Needs no
    card in the store: the worktree is asked of git, and a branch with
    only a local ref gets one made (the whole point of a card is that the
    work can be looked at, whatever state some other session left it in).
    An instance already up is reported as up - branch.sh prints "already
    running (pid N, port P)" and exits 0."""
    _refuse_if_passive("launching a test instance")
    if not (branch or "").startswith("claude/"):
        raise ValueError(f"not a draft branch: {branch or 'unset'}")
    with _serves_lock:
        cur = _serves.get(branch)
        if cur and cur.get("status") == "starting":
            return {"started": False, "detail": "already starting"}
        _serves[branch] = {"status": "starting", "port": None, "text": "",
                           "started": _now_iso()}

    def run():
        try:
            _ensure_worktree(branch)
            ok, text = _branch_sh(["serve", _slug(branch), "--local"],
                                  SERVE_TIMEOUT)
        except ValueError as e:
            ok, text = False, str(e)
        except (OSError, subprocess.SubprocessError) as e:
            ok, text = False, f"serve failed to run: {e}"
        port = None
        m = (re.search(r"localhost:(\d{4})", text)
             or re.search(r"port (\d{4})", text))
        if ok and m:
            port = int(m.group(1))
        with _serves_lock:
            _serves[branch] = {
                "status": "up" if (ok and port) else "failed",
                "port": port, "text": text[-600:], "started": _now_iso()}
        try:
            refresh()
        except Exception:  # noqa: BLE001 - the serve outcome is already recorded
            pass
    _spawn(run, f"vira-showroom-serve-{_slug(branch)}")
    return {"started": True}


def stop(branch):
    _refuse_if_passive("stopping a test instance")
    if not (branch or "").startswith("claude/"):
        raise ValueError(f"not a draft branch: {branch or 'unset'}")
    ok, text = _branch_sh(["stop", _slug(branch)], QUICK_TIMEOUT)
    with _serves_lock:
        _serves.pop(branch, None)
    try:
        refresh()
    except Exception:  # noqa: BLE001
        pass
    return {"stopped": ok, "output": text[-400:]}


def cleanup(branch):
    """Tear a LANDED branch down - the one verb this band has. Routes
    through orphanwork.discard (branch.sh discard, no --force): a clean
    merged tree needs no force, and a tree that turns out dirty is refused
    by branch.sh itself rather than destroyed. Unlanded rows use the
    sweeper's own Discard, which carries the armed confirm."""
    _refuse_if_passive("cleaning up a branch")
    it = _require(branch)
    if it.get("band") != "landed":
        raise ValueError(f"{branch} is {it.get('band')} - only a landed, "
                         "clean branch cleans up here; use its own verdicts")
    ok, detail = orphanwork.discard(_slug(branch))
    if not ok:
        raise ValueError(detail)
    return {"started": True}


def _actions_by_branch():
    with orphanwork._actions_lock:
        return {b: dict(a) for b, a in orphanwork._actions.items()}


# ---------------------------------------------------------------- describing

DESCRIBE_PROMPT = """You are Vira's release desk. Below are draft branches in the owner's \
Vira repository, each with the evidence the sweep gathered: the title, what the \
originating session was asked, its commit subjects, which areas of the codebase \
it touches, its pull request text where one exists, and its state.

For EACH branch write a blurb of one to three plain sentences (under {cap} \
characters) that says (1) what the branch does or is meant to do, in the \
owner's terms, and (2) what state it is in - built and waiting, half-done, a \
stalled experiment, a duplicate of something that already landed. Judge only \
from the evidence given; never invent features the evidence does not name. \
Prefer the pull request text and the commit subjects over the session prompt \
when they disagree, since they describe what was actually done.

Also name the MODULE each branch is about: the one surface or engine of Vira \
it changes. Reuse a tag from the vocabulary below whenever one fits - the \
point is that five branches about one module group under ONE word - and coin \
a short lowercase hyphenated tag only when nothing fits.

MODULE VOCABULARY (tag x count): {vocab}

Reply with STRICT JSON only: a list of objects \
{{"branch": "...", "module": "...", "blurb": "..."}} covering every branch below.

BRANCHES:
{items}
"""

TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")   # ideatags' own tag shape


def _module_vocab(s):
    """The module words already in use - the Queue's converged module axis
    (ideatags.vocabulary) plus every module this store has read or
    guessed - so the describe pass reuses a word rather than minting a
    fifth spelling of one subject (the ideatags rule)."""
    counts = {}
    try:
        from . import ideatags
        for tag, n in (ideatags.vocabulary().get("module") or [])[:40]:
            counts[tag] = counts.get(tag, 0) + n
    except Exception:  # noqa: BLE001 - the Queue's vocabulary is a bonus
        pass
    for r in (s.get("reads") or {}).values():
        if r.get("module"):
            counts[r["module"]] = counts.get(r["module"], 0) + 1
    for it in s.get("items") or []:
        g = it.get("module_guess")
        if g and g != "other":
            counts[g] = counts.get(g, 0) + 1
    if not counts:
        return "(none yet)"
    return ", ".join(f"{t} x{n}" for t, n in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


_describe_lock = threading.Lock()
_describe_running = False


def _evidence(item):
    out = [f"branch: {item.get('branch')}",
           f"  title: {item.get('title')}",
           f"  state: {item.get('band')} - {item.get('age_days', 0)}d since last "
           f"activity, {item.get('ahead', 0)} commit(s) ahead of main, "
           f"{item.get('dirty', 0)} uncommitted change(s), "
           f"{item.get('behind', 0)} behind"]
    job = item.get("job") or {}
    if job.get("prompt_head"):
        out.append(f"  session asked: {job['prompt_head']}")
    if item.get("commits"):
        out.append("  commits: " + " | ".join(item["commits"]))
    if item.get("areas"):
        out.append("  touches: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(item["areas"].items(),
                                          key=lambda kv: -kv[1])))
    if item.get("files"):
        out.append("  uncommitted files: " + ", ".join(item["files"]))
    pr = item.get("pr") or {}
    if pr.get("number"):
        state = ("draft" if pr.get("draft") else pr.get("state", "").lower())
        out.append(f"  pull request #{pr['number']} ({state}): {pr.get('title', '')}")
    body = (item.get("pr_body") or "")
    if body:
        out.append("  PR text: " + " ".join(body.split())[:900])
    if item.get("orphan_read"):
        r = item["orphan_read"]
        out.append(f"  sweeper's read: {r.get('verdict')} - {r.get('why')}")
    if item.get("failure") and item["failure"].get("headline"):
        out.append(f"  last session failure: {item['failure']['headline']}")
    return "\n".join(out)


def pending_reads(s=None):
    """Items that want a model read and have none: session and unlanded
    rows only - a landed row's blurb is derived."""
    s = s or _read()
    reads = s.get("reads") or {}
    return [it for it in (s.get("items") or [])
            if it.get("band") in ("session", "unlanded")
            and it.get("key") not in reads]


def describe_missing():
    """ONE suggest.complete pass over up to DESCRIBE_BATCH undescribed
    cards. Grounded-or-dropped: an unknown branch or an empty blurb is
    discarded, never coerced. Returns how many reads were stored."""
    s = _read()
    todo = pending_reads(s)[:DESCRIBE_BATCH]
    if not todo:
        return 0
    prs = s.get("prs") or {}
    for it in todo:
        it["pr_body"] = (prs.get(it.get("branch")) or {}).get("body", "")
    from . import suggest
    prompt = DESCRIBE_PROMPT.format(
        cap=BLURB_MAX, vocab=_module_vocab(s),
        items="\n".join(_evidence(it) for it in todo))
    try:
        raw = suggest.complete(prompt)
        m = re.search(r"\[.*\]", raw or "", re.S)
        rows = json.loads(m.group(0)) if m else []
    except Exception:  # noqa: BLE001 - an undescribed card is the honest degrade
        return 0
    by_branch = {it["branch"]: it for it in todo}
    now = _now_iso()
    accepted = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        b = r.get("branch")
        blurb = " ".join(str(r.get("blurb") or "").split())[:BLURB_MAX]
        it = by_branch.get(b)
        if it and blurb:
            module = str(r.get("module") or "").strip().lower().replace(" ", "-")
            accepted[it["key"]] = {
                "blurb": blurb, "when": now,
                "module": module if TAG_RE.match(module) else ""}
    if not accepted:
        return 0

    def fn(st):
        cur = dict(st.get("reads") or {})
        for k, v in accepted.items():
            cur.setdefault(k, v)      # never re-adjudicate behind a cached read
        if len(cur) > READS_MAX:
            for k in sorted(cur, key=lambda x: cur[x].get("when", ""))[
                    :len(cur) - READS_MAX]:
                cur.pop(k, None)
        st["reads"] = cur
        return st
    _mutate(fn)
    return len(accepted)


def _kick_describe():
    """The describe pass on a daemon thread, one at a time, never on a
    passive instance (the orphanwork._kick_assess convention)."""
    global _describe_running
    if _passive():
        return
    with _describe_lock:
        if _describe_running:
            return
        _describe_running = True

    def run():
        global _describe_running
        try:
            describe_missing()
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _describe_lock:
                _describe_running = False
    _spawn(run, "vira-showroom-describe")


# ---------------------------------------------------------------- compose

def compose():
    """The cards: the cached sweep plus the live joins (an in-flight serve,
    an in-flight action, the cached blurb). Cheap by construction - no
    git on this path, so the client can poll it while an instance boots."""
    s = _read()
    reads = s.get("reads") or {}
    actions = _actions_by_branch()
    with _serves_lock:
        serves = {b: dict(v) for b, v in _serves.items()}
    items = []
    for it in s.get("items") or []:
        row = dict(it)
        r = reads.get(row.get("key") or "")
        row["blurb"] = (r or {}).get("blurb") or fallback_blurb(row)
        row["blurb_source"] = "vira" if r else "derived"
        row["module"] = (r or {}).get("module") or row.get("module_guess") or "other"
        row["serving"] = serves.get(row["branch"])
        a = actions.get(row["branch"])
        if a:
            row["action"] = {"name": a.get("name"), "status": a.get("status"),
                             "output": (a.get("output") or "")[-600:]}
        items.append(row)
    # Newest activity first, whatever the band (owner, 2026-09-03: no
    # sections by type - everything, with grouping by CONTENT on demand).
    items.sort(key=lambda r: -(r.get("last_activity") or 0.0))
    counts = {b: 0 for b in BANDS}
    modules = {}
    running = 0
    for r in items:
        counts[r.get("band")] = counts.get(r.get("band"), 0) + 1
        modules[r["module"]] = modules.get(r["module"], 0) + 1
        inst = r.get("instance") or {}
        if inst.get("alive") or (r.get("serving") or {}).get("status") == "starting":
            running += 1
    # A passive instance never runs the describe pass, so it must not
    # promise one: "Vira is reading 10" on a clone would be a count that
    # only ever grows.
    return {"items": items, "counts": counts, "running": running,
            "modules": sorted(modules.items(), key=lambda kv: (-kv[1], kv[0])),
            "describing": 0 if _passive() else len(pending_reads(s)),
            "last_sweep": s.get("last_sweep"), "passive": _passive()}


def context(branch):
    """The full read behind a card, on first expand: an unlanded row's
    complete sweeper context (prompt, every commit, every file, the
    resume prompt) plus the PR text and, for a landed row, the merge and
    the disk the worktree still holds. READ-ONLY."""
    it = _require(branch)
    out = {"branch": branch, "band": it.get("band"), "title": it.get("title")}
    prs = _read().get("prs") or {}
    pr = prs.get(branch)
    if pr:
        out["pr"] = pr
    wt = it.get("worktree")
    if wt and Path(wt).is_dir():
        try:
            du = subprocess.run(["du", "-sk", wt], capture_output=True,
                                text=True, timeout=60, check=False)
            kb = int((du.stdout or "0").split()[0])
            out["disk_mb"] = round(kb / 1024)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            out["disk_mb"] = None
    if it.get("band") == "landed" and it.get("merged_sha"):
        lg = gitutil.git(ROOT, "show", "--format=%H%n%an%n%ci%n%s%n%n%b",
                         "--no-patch", it["merged_sha"], timeout=20)
        out["merge"] = (lg.stdout or "").strip() if lg.returncode == 0 else ""
        out["merge_paths"] = _merge_paths(it["merged_sha"])[:200]
    if it.get("orphan_key"):
        try:
            orphan = next((o for o in orphanwork.compose().get("items", [])
                           if o.get("key") == it["orphan_key"]), None)
            if orphan:
                out["orphan"] = orphanwork.context(orphan)
        except Exception as e:  # noqa: BLE001 - the rest of the read stands
            out["orphan_error"] = str(e)[:200]
    job = it.get("job") or {}
    if job.get("id"):
        rec = joblog.get_record(job["id"]) or {}
        out["job"] = {"id": job["id"], "title": job.get("title"),
                      "status": rec.get("status") or job.get("status"),
                      "prompt": (rec.get("prompt") or "")[:6000],
                      "result": (rec.get("result") or "")[:4000]}
    return out

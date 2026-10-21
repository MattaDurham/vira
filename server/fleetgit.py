"""Fleet's repository reader: worktrees, branches, pull requests and what
landed, for every repository an agent session works in.

Fleet's slow tier. Git is asked every REPO_SECS, or sooner when a ref, a
worktree or packed-refs changes on disk (a cheap stat signature, checked on
every live tick); GitHub's `gh` is asked at most every GH_SECS per
repository, and skipped quietly when gh is missing or not signed in.

Read-only: only git's read commands run (worktree list, for-each-ref,
branch --merged, status --porcelain, log, show). Nothing is fetched,
pruned or checked out. Every call runs with GIT_OPTIONAL_LOCKS=0, because
a plain `git status` refreshes stale stat data by rewriting `.git/index`
under `index.lock`, and an agent's `git add` or `git commit` in the same
worktree at that moment fails with "index.lock: File exists".
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import changelog

GIT_TIMEOUT = 10
REPO_SECS = 30
GH_SECS = 300
GH_TIMEOUT = 20
# Landings shown per repository: enough for the timeline's 60 days on a
# busy repository without reading the whole history.
LANDINGS_DAYS = 60
LANDINGS_MAX = 400
# Branches listed per repository. A long-lived repository can hold
# hundreds of spent local branches; the newest are the ones worth seeing.
BRANCHES_MAX = 80
BASE_NAMES = ("main", "master", "trunk", "develop")


def git_bin():
    return shutil.which("git")


def gh_bin():
    """The gh binary, or None. VIRA_FLEET_GH points elsewhere (a missing
    path reads as "not installed")."""
    v = os.environ.get("VIRA_FLEET_GH")
    if v is not None:
        return v if v and os.path.isfile(v) else None
    return shutil.which("gh")


def _run(cmd, cwd=None, timeout=GIT_TIMEOUT):
    # No optional locks for git, or for the git that gh runs: Fleet only
    # reads, so it never takes the index lock an agent may need.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return r.returncode, r.stdout


def git(repo, *args, timeout=GIT_TIMEOUT):
    g = git_bin()
    if not g:
        return 1, ""
    return _run([g, "-C", str(repo), *args], timeout=timeout)


# ---------------------------------------------------------------- repo roots

_roots = {}


def repo_root(path):
    """The main checkout a folder belongs to, from the filesystem alone: a
    `.git` folder marks a main checkout; a `.git` file points a worktree at
    its main checkout's git folder. None outside any repository."""
    if not path:
        return None
    path = str(path)
    if path in _roots:
        return _roots[path]
    found = None
    p = Path(path)
    for anc in (p, *p.parents):
        g = anc / ".git"
        try:
            if g.is_dir():
                found = str(anc)
                break
            if g.is_file():
                text = g.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"gitdir:\s*(.+)", text)
                if m:
                    gd = Path(m.group(1).strip())
                    if not gd.is_absolute():
                        gd = (anc / gd).resolve()
                    # <main>/.git/worktrees/<name> -> <main>
                    if gd.parent.name == "worktrees":
                        found = str(gd.parent.parent.parent)
                    else:
                        found = str(anc)
                break
        except OSError:
            break
    # Resolved, so one repository reached through a symlink and through its
    # real path (a worktree's .git file names the real one) is one key.
    found = os.path.realpath(found) if found else None
    _roots[path] = found
    return found


def same_path(a, b):
    """Whether two folders are one, through symlinks (git reports resolved
    paths: /var is /private/var on macOS)."""
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def worktree_kind(path, main):
    path = str(path)
    if same_path(path, main):
        return "main"
    if "/.claude/worktrees/" in path.replace("\\", "/"):
        return "app"
    if "/.worktrees/" in path.replace("\\", "/"):
        return "branch"
    return "other"


def signature(root):
    """A cheap fingerprint of a repository's refs and worktrees: the mtimes
    of the folders git renames ref and worktree files into."""
    gd = Path(root) / ".git"
    sig = []
    for p in (gd / "packed-refs", gd / "HEAD", gd / "worktrees"):
        try:
            sig.append(p.stat().st_mtime_ns)
        except OSError:
            sig.append(0)
    heads = gd / "refs" / "heads"
    try:
        for dirpath, _dirs, _files in os.walk(heads):
            sig.append(os.stat(dirpath).st_mtime_ns)
    except OSError:
        pass
    wts = gd / "worktrees"
    try:
        for e in os.scandir(wts):
            try:
                sig.append(os.stat(os.path.join(e.path, "HEAD")).st_mtime_ns)
            except OSError:
                pass
    except OSError:
        pass
    return tuple(sig)


# ---------------------------------------------------------------- parsing

def parse_worktrees(text):
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:], "branch": None, "head": None, "detached": False}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            cur["detached"] = True
        elif line == "bare":
            cur["bare"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
    return [w for w in out if not w.get("bare")]


def pick_base(heads):
    for b in BASE_NAMES:
        if b in heads:
            return b
    return None


# Reflog lines that move a branch without adding work of its own: its
# creation, and a fast-forward onto another branch (syncing with the base).
_NO_WORK = re.compile(r"^branch: Created from |: Fast-forward$")


def never_committed(root, branch):
    """Whether a branch has had no commit of its own since it was created,
    from its reflog. `git branch --merged` lists such a branch, because its
    tip is a commit the base already holds, but nothing of it ever landed:
    a fresh agent worktree read as "merged" while its session was still
    writing. False when there is no reflog to say (expired, or turned off),
    which leaves git's answer standing."""
    p = Path(root) / ".git" / "logs" / "refs" / "heads" / branch
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    msgs = [ln.split("\t", 1)[1] for ln in lines if "\t" in ln]
    return bool(msgs) and all(_NO_WORK.search(m) for m in msgs)


def parse_heads(text, with_ab):
    heads = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if with_ab and len(parts) >= 5:
            name, sha, ct, ab, subject = parts[0], parts[1], parts[2], parts[3], "\t".join(parts[4:])
        elif len(parts) >= 4:
            name, sha, ct, subject = parts[0], parts[1], parts[2], "\t".join(parts[3:])
            ab = ""
        else:
            continue
        ahead = behind = None
        m = re.match(r"(\d+) (\d+)", ab or "")
        if m:
            ahead, behind = int(m.group(1)), int(m.group(2))
        heads[name] = {"name": name, "sha": sha, "at": int(ct) * 1000 if ct.isdigit() else None,
                       "subject": subject[:200], "ahead": ahead, "behind": behind}
    return heads


# ---------------------------------------------------------------- gh

class GhCache:
    """PRs per repository from `gh pr list`, at most every GH_SECS."""

    def __init__(self):
        self.by_repo = {}        # root -> (at, prs, note)

    def get(self, root, force=False):
        hit = self.by_repo.get(root)
        if hit and not force and time.time() - hit[0] < GH_SECS:
            return hit[1], hit[2]
        gh = gh_bin()
        if not gh:
            res = ([], "gh not found on this machine")
        else:
            rc, out = _run([gh, "pr", "list", "--state", "all", "--limit", "200",
                            "--json", "number,title,state,headRefName,mergedAt,url,createdAt"],
                           cwd=root, timeout=GH_TIMEOUT)
            if rc != 0:
                res = ([], "gh could not list pull requests here (not signed in, or no GitHub remote)")
            else:
                import json
                try:
                    rows = json.loads(out or "[]")
                except ValueError:
                    rows = []
                res = ([dict(n=r.get("number"), title=(r.get("title") or "")[:200],
                             state=(r.get("state") or "").upper(), branch=r.get("headRefName"),
                             mergedAt=r.get("mergedAt"), url=r.get("url"),
                             createdAt=r.get("createdAt"))
                        for r in rows if isinstance(r, dict)], None)
        # A failed read is not cached as a fact for the full TTL.
        self.by_repo[root] = (time.time() if res[1] is None else time.time() - GH_SECS + 60,
                              res[0], res[1])
        return res


# ---------------------------------------------------------------- scan

def scan(root, gh_cache=None, now_ms=None):
    """Everything Fleet shows about one repository, without the sessions
    (fleet.label_repo adds which sessions sit where)."""
    now_ms = now_ms or int(time.time() * 1000)
    out = {"root": root, "name": Path(root).name, "base": None, "worktrees": [],
           "branches": [], "prs": [], "landings": [], "changelog": [],
           "note": None, "prNote": None, "scannedAt": now_ms}
    if not git_bin():
        out["note"] = "git not found on this machine"
        return out
    rc, wt_text = git(root, "worktree", "list", "--porcelain")
    if rc != 0:
        out["note"] = "not a git repository any more"
        return out
    worktrees = parse_worktrees(wt_text)
    rc, names = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    heads_set = set(names.split()) if rc == 0 else set()
    base = pick_base(heads_set)
    if not base:
        main_wt = next((w for w in worktrees if worktree_kind(w["path"], root) == "main"), None)
        base = (main_wt or {}).get("branch")
    out["base"] = base
    fmt = "%(refname:short)%09%(objectname)%09%(committerdate:unix)%09"
    with_ab = bool(base)
    rc, text = (git(root, "for-each-ref", f"--format={fmt}%(ahead-behind:{base})%09%(subject)",
                    "refs/heads") if base else (1, ""))
    if rc != 0:
        # git before 2.41 has no ahead-behind atom: count per branch below.
        with_ab = False
        rc, text = git(root, "for-each-ref", f"--format={fmt}%(subject)", "refs/heads")
    heads = parse_heads(text, with_ab) if rc == 0 else {}
    merged = set()
    if base:
        rc, text = git(root, "branch", "--format=%(refname:short)", "--merged", base)
        if rc == 0:
            merged = {line.strip() for line in text.splitlines() if line.strip()}
    fresh = {n for n in merged if n != base and never_committed(root, n)}
    merged -= fresh
    if base and not with_ab:
        for h in heads.values():
            if h["name"] == base:
                continue
            rc, t = git(root, "rev-list", "--left-right", "--count", f"{h['name']}...{base}")
            m = re.match(r"(\d+)\s+(\d+)", t or "")
            if rc == 0 and m:
                h["ahead"], h["behind"] = int(m.group(1)), int(m.group(2))

    in_wt = set()
    dirty_of = {}
    for w in worktrees:
        kind = worktree_kind(w["path"], root)
        h = heads.get(w["branch"] or "") or {}
        dirty = None
        if os.path.isdir(w["path"]):
            rc, st = git(w["path"], "status", "--porcelain", timeout=GIT_TIMEOUT)
            dirty = len([ln for ln in st.splitlines() if ln.strip()]) if rc == 0 else None
        if w["branch"]:
            in_wt.add(w["branch"])
        out["worktrees"].append(dict(
            path=w["path"], name=Path(w["path"]).name, kind=kind, branch=w["branch"],
            detached=w["detached"], missing=not os.path.isdir(w["path"]),
            lastAt=h.get("at"), subject=h.get("subject"), ahead=h.get("ahead"),
            behind=h.get("behind"), merged=(w["branch"] in merged) if w["branch"] and w["branch"] != base else None,
            fresh=w["branch"] in fresh, dirty=dirty))
        if w["branch"]:
            dirty_of[w["branch"]] = dirty
    rest = [h for n, h in heads.items() if n not in in_wt and n != base]
    rest.sort(key=lambda h: -(h["at"] or 0))
    out["branchCount"] = len(rest)
    for h in rest[:BRANCHES_MAX]:
        out["branches"].append(dict(name=h["name"], lastAt=h["at"], subject=h["subject"],
                                    ahead=h["ahead"], behind=h["behind"],
                                    merged=h["name"] in merged, fresh=h["name"] in fresh))
    out["heads"] = {n: dict(merged=n in merged, ahead=h["ahead"], fresh=n in fresh,
                            dirty=dirty_of.get(n)) for n, h in heads.items()}

    prs, note = (gh_cache.get(root) if gh_cache else ([], None))
    out["prs"], out["prNote"] = prs, note

    if base:
        log, diff, current = history(root, base, (heads.get(base) or {}).get("sha"))
        out["landings"] = changelog.parse_landings(log, prs)
        if diff:
            out["changelog"] = changelog.parse_changelog_md(diff, current)
    return out


_history = {}


def history(root, base, sha):
    """The base branch's first-parent log with line counts, and the
    CHANGELOG.md diff that dates its bullets: git's text, kept until the
    base branch moves. The --shortstat log is the slowest read in a scan
    (0.6 s on a busy repository), and the base moves a few times a day."""
    key = (root, base)
    hit = _history.get(key)
    if hit and sha and hit[0] == sha:
        return hit[1]
    since = f"--since={LANDINGS_DAYS}.days"
    rc, log = git(root, "log", "--first-parent", base, since, f"-{LANDINGS_MAX}",
                  "--format=%x1e%H%x1f%ct%x1f%P%x1f%s", "--shortstat")
    diff = current = ""
    if (Path(root) / "CHANGELOG.md").exists():
        rc2, diff = git(root, "log", "--first-parent", "--reverse", "-p", "--unified=0",
                        "--format=%x1e%H%x1f%ct", base, "--", "CHANGELOG.md")
        rc3, current = git(root, "show", f"{base}:CHANGELOG.md")
        diff = diff if rc2 == 0 else ""
        current = current if rc3 == 0 else ""
    val = (log if rc == 0 else "", diff, current)
    if sha and rc == 0:
        _history[key] = (sha, val)
    return val


def merged_state(scan_row, branch):
    """(exists, merged, ahead, fresh, dirty) for a branch from a scan; None
    when unknown. fresh: no commit of its own yet. dirty: uncommitted files
    in its worktree, None without one."""
    if not scan_row:
        return None
    h = (scan_row.get("heads") or {}).get(branch)
    if h is None:
        return (False, None, None, False, None)
    return (True, h["merged"], h["ahead"], h.get("fresh", False), h.get("dirty"))

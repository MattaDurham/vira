"""The pull-request index - which PR belongs to which branch, asked of gh
ONCE for the whole repo and read back as a dict.

Every Vira coding session lives on a `claude/<slug>` branch, and since the
PR layer (2026-08-27) every one of those branches is a pull request. A PR
carries the two things a session's ledger row cannot know at launch: its
NUMBER (the label the owner tracks work by) and its TITLE (the subject the
session itself wrote, once it got far enough to open a draft). Both arrive
AFTER the launch record is written, so they are read at RENDER time from
this index rather than stamped once and left to rot.

Why one call for everything: a per-branch `gh pr view` from inside a job
read would put a network call behind every row of the Runs list. `refresh()`
runs `gh pr list --state all` once (the repo has tens of PRs, not
thousands) and writes data/pr-index.json; `lookup(branch)` reads that file
with an mtime cache, so a name lookup costs a dict access. Refresh is kicked
by the orphan-work sweep - it already runs on every Runs view open - and by
anything that just learned a PR exists. Never from a request path.

Everything here degrades to "no PR known": a dead gh, no network, a repo
with no remote. A row without a PR reads exactly as it did before this
module existed.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from . import jsonstore
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "pr-index.json"

# How long an index answers before the next sweep re-asks gh. A PR's number
# never changes and its title rarely does, so this is about NEW PRs being
# named promptly - the Runs view re-sweeps on every open anyway.
TTL_S = 600
GH_TIMEOUT = 30
LIMIT = 300

_lock = threading.Lock()
_cache = {"mtime": None, "by_branch": {}}
_inflight = threading.Lock()


def _read():
    try:
        st = STORE.stat()
    except OSError:
        _cache["mtime"], _cache["by_branch"] = None, {}
        return _cache["by_branch"]
    if _cache["mtime"] == st.st_mtime:
        return _cache["by_branch"]
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        by = data.get("by_branch") or {}
        if not isinstance(by, dict):
            by = {}
    except (OSError, ValueError, UnicodeDecodeError):
        by = {}
    _cache["mtime"], _cache["by_branch"] = st.st_mtime, by
    return by


def lookup(branch):
    """{number, url, title, state, draft} for a branch, or None. Reads the
    on-disk index only - never gh."""
    if not branch:
        return None
    return _read().get(branch) or None


def stale():
    try:
        return time.time() - STORE.stat().st_mtime > TTL_S
    except OSError:
        return True


def _gh_list(cwd):
    """The raw `gh pr list` rows, or None when gh cannot answer."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", str(LIMIT),
             "--json", "number,url,title,headRefName,state,isDraft,updatedAt"],
            cwd=str(cwd), capture_output=True, text=True, timeout=GH_TIMEOUT,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        rows = json.loads(out.stdout or "[]")
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def _shape(rows):
    by = {}
    for r in rows:
        br = (r.get("headRefName") or "").strip()
        num = r.get("number")
        if not br or not isinstance(num, int):
            continue
        rec = {"number": num, "url": r.get("url") or "",
               "title": (r.get("title") or "").strip(),
               "state": (r.get("state") or "").upper(),
               "draft": bool(r.get("isDraft"))}
        # A branch re-used across PRs: the newest number wins - gh lists
        # newest first, so the first sighting is the one to keep.
        by.setdefault(br, rec)
    return by


def refresh(cwd=None, force=False):
    """Re-ask gh and rewrite the index. Returns the by-branch map (the old
    one when gh cannot answer - a failed refresh never blanks a good
    index). Skipped while fresh unless `force`."""
    if not force and not stale():
        return _read()
    rows = _gh_list(cwd or ROOT)
    if rows is None:
        return _read()
    by = _shape(rows)
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with locked(STORE):
        jsonstore.write_atomic(
            STORE, {"when": time.time(), "by_branch": by}, indent=1)
    _cache["mtime"] = None            # force a re-read on next lookup
    return by


def refresh_async(cwd=None, force=False):
    """The sweep's entry point: one refresh at a time, on a thread, never
    blocking the caller. Passive instances still read (a PR list is public
    metadata) - there is deliberately no VIRA_PASSIVE gate here."""
    if os.environ.get("VIRA_PR_INDEX_OFF"):
        return False
    if not force and not stale():
        return False
    if not _inflight.acquire(blocking=False):
        return False

    def run():
        try:
            refresh(cwd, force=True)
        finally:
            _inflight.release()
    threading.Thread(target=run, daemon=True, name="vira-prindex").start()
    return True


def note(branch, number, url, title=""):
    """Record one PR this process just learned about (the landing card's
    `_ensure_pr`, a `branch.sh pr` run) without waiting for the next
    sweep. Merged over the on-disk map under the lock."""
    if not branch or not isinstance(number, int):
        return
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with locked(STORE):
        by = dict(_read())
        cur = dict(by.get(branch) or {})
        cur.update({"number": number, "url": url or cur.get("url", "")})
        if title:
            cur["title"] = title
        cur.setdefault("title", "")
        cur.setdefault("state", "OPEN")
        cur.setdefault("draft", True)
        by[branch] = cur
        # Keep the file's own age: a note is not a full refresh, and
        # stamping `when` here would let a stale index read as fresh.
        try:
            when = json.loads(STORE.read_text(encoding="utf-8")).get("when")
        except (OSError, ValueError, UnicodeDecodeError):
            when = None
        jsonstore.write_atomic(
            STORE, {"when": when or 0, "by_branch": by}, indent=1)
        # The mtime just moved, so stale() would read this as a refresh;
        # restore the old stamp so the next sweep still re-asks gh.
        try:
            t = when or 0
            os.utime(STORE, (t, t))
        except OSError:
            pass
    _cache["mtime"] = None

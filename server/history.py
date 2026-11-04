"""The history store: one append-only record of every event, kept forever.

Every screen that shows history (Fleet, the module windows, the idea cards)
is a view of this one record. It replaces each screen gathering its own copy
from up to nine stores when it opens, and it ends the 120-run limit on Flow
history: a run captured here stays here.

WHAT AN EVENT IS. One thing that happened, at the finest grain the sources
record: a note you told Vira, a session, one commit, one merge into main,
one Flow run and each of its steps, one Program run, a captured note, an
answer, a decision, a text Vira sent. Each event carries

    id        stable identity, built from the source's own ids
              ("session:claude:<uuid>", "run:program:<run id>")
    at, end   UTC ISO times; `end` only for things with a span
    kind      ask | session | code | run | made | answer | decide | tell
    sub       what kind of that kind ("journal", "claude", "merged", ...)
    status    the event's own word, one of STATUSES
    title     a title taken from the records (titles.py rules below)
    project   the folder's lane name, the way Fleet names it
    folder    the folder it happened in; repo: its repository root
    module    the Vira submodule its files belong to (server/moduletree),
              modules: {submodule: files} for every one it touched
    links     {session, job, idea, branch, pr, flow_run, program_run,
               note, files, commit, parent, ...}: lists of plain ids
    data      what only this kind carries (tokens, cost, counts, errors,
              log paths, line counts)

APPEND-ONLY, NEWEST LINE WINS. An event can change after it is first seen
(a session that was running ends; a branch gets merged). The change is a
new line with the same id, never an edit: the reader keeps the last line per
id, and every earlier line stays in the file. Nothing is ever deleted.

FILES. data/history/events-YYYY-MM.jsonl, one file per month of writing,
appended under a file lock (the server and the ingest process both write).
Months before the previous one are gzipped in place
(events-YYYY-MM.jsonl.gz) by `compress_old`; the reader reads both forms.
data/ belongs to one checkout, so a test instance keeps its own history.

STATUS WORDS. One set of nine for every event and every entry built from
them (STATUSES). Each says where the work ended up, never only that a
session stopped. The ninth, "Not started", is an idea in the Ideas queue
that no session has started: it is not waiting on the owner, and the list
views leave it out unless asked (UNSTARTED).

TITLES. Taken from the records in this order: the pull request title, then
the idea title, then the Flow or Program name with its counts, then the
title the app gave the session, then the first sentence of the ask.
Session ids, branch names and hashes never appear in a title (clean_title).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "history"

KINDS = ("ask", "session", "code", "run", "made", "answer", "decide", "tell")
KIND_LABELS = {"ask": "You asked", "session": "A session ran", "code": "Code moved",
               "run": "An automation ran", "made": "Something was made",
               "answer": "An answer was given", "decide": "You decided",
               "tell": "Vira told you"}

RUNNING, WAITING, NOT_MERGED, MERGED = "Running", "Waiting on you", "Not merged", "Merged"
ANSWERED, RAN, FAILED, DROPPED = "Answered", "Ran", "Failed", "Dropped"
NOT_STARTED = "Not started"
STATUSES = (RUNNING, WAITING, NOT_MERGED, MERGED, ANSWERED, RAN, FAILED, DROPPED, NOT_STARTED)
STATUS_MEANS = {
    RUNNING: "A session or run is working right now.",
    WAITING: "It needs your answer, approval or merge.",
    NOT_MERGED: "Code on a branch that has not reached main and nobody is working on it.",
    MERGED: "Code reached main.",
    ANSWERED: "A question got its answer, or you made a choice.",
    RAN: "An automation finished with no errors. Counts say what it made.",
    FAILED: "It stopped with an error.",
    DROPPED: "You set it aside: an idea dropped or deferred, a branch discarded.",
    NOT_STARTED: "An idea in your Ideas queue that no session has started. It stays in "
                 "the Ideas tab; lists leave it out unless asked.",
}
# Words the list views leave out unless a filter asks for them.
UNSTARTED = (NOT_STARTED,)
# Unfinished words: pinned at the top of today in every view.
OPEN_STATUSES = (RUNNING, WAITING)

LINK_KEYS = ("session", "job", "idea", "note", "chat", "branch", "pr", "commit",
             "flow_run", "flow", "program_run", "program", "schedule", "files",
             "notes", "doc", "parent", "person", "branches_written")

_lock = threading.Lock()
_cache = {"sig": None, "events": {}, "lines": 0}


# ---------------------------------------------------------------- time

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_ts(value):
    """Epoch seconds from what the stores hold: epoch seconds or
    milliseconds, an ISO string with an offset (UTC), or a naive ISO string
    (the journal and the notification log write local time). 0.0 when the
    value is missing or unreadable; a missing time is never invented."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return 0.0
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return to_ts(float(s))
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if d.tzinfo is None:
        d = d.astimezone()          # naive = this Mac's local time
    try:
        return d.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def iso(value):
    """UTC ISO for any time to_ts reads, or "" when there is none."""
    ts = to_ts(value)
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def local_day(ts):
    """The local calendar day of an epoch time, never a [:10] slice of UTC."""
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d") if ts else ""


# ---------------------------------------------------------------- titles

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_BRANCH = re.compile(r"\b(?:claude|codex|origin|feature|fix|idea)/[A-Za-z0-9._/-]+")
_HASH = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_IDS = re.compile(r"\b(?:run|job|idea|note|chat|rt|cir|prog|rv|turn)_[0-9a-f]{6,}\b")
_RUN_STAMP = re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b")
_SPACE = re.compile(r"\s+")
TITLE_CAP = 120


def clean_title(text, cap=TITLE_CAP):
    """A title with every session id, branch name, hash and record id taken
    out, whitespace folded and the length capped on a word boundary."""
    s = str(text or "")
    for rx in (_UUID, _BRANCH, _RUN_STAMP, _IDS, _HASH):
        s = rx.sub("", s)
    s = re.sub(r"\(\s*\)|\[\s*\]", "", s)
    s = _SPACE.sub(" ", s).strip(" -:;,·—")
    s = re.sub(r"\s+([,.;:])(?=\s|$)", r"\1", s)
    if len(s) > cap:
        cut = s[:cap].rsplit(" ", 1)[0].rstrip(" ,;:-")
        s = (cut or s[:cap]) + "..."
    return s


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n")


def first_sentence(text, cap=TITLE_CAP):
    """The first sentence of an ask, cleaned. A quoted block Vira wraps the
    owner's words in counts as the ask."""
    s = str(text or "").strip()
    m = re.search(r'"""(.+?)"""', s, re.S)
    if m:
        s = m.group(1).strip()
    s = s.lstrip("#>*- \t")
    head = _SENTENCE_END.split(s, maxsplit=1)[0] if s else ""
    return clean_title(head, cap)


def pick_title(pr_title="", idea_title="", automation="", app_title="", ask=""):
    """The title rule, in order. Each candidate is cleaned first, so a PR
    title that was only a branch name falls through to the next one."""
    for cand in (pr_title, idea_title, automation, app_title):
        t = clean_title(cand)
        if t:
            return t
    return first_sentence(ask) or ""


def automation_title(name, counts=None, errors=0):
    """A Flow or Program name with its counts: "Capture each link: 3
    captured, 1 failed". Zero counts are left out; errors are counted."""
    name = clean_title(name) or "Automation"
    bits = [f"{n} {k}" for k, n in (counts or {}).items()
            if isinstance(n, (int, float)) and n]
    if errors and not any(k.startswith(("fail", "error")) for k in (counts or {})):
        bits.append(f"{errors} error{'s' if errors != 1 else ''}")
    return f"{name}: {', '.join(bits)}" if bits else name


# ---------------------------------------------------------------- places

def is_scratch(path):
    p = str(path or "").replace("\\", "/")
    return "scratch-workspaces" in p or "/scratchpad" in p


def repo_root(path):
    from . import fleetgit
    return fleetgit.repo_root(path)


def project_of(path):
    """(lane name, repository root) for a folder, the way Fleet names it:
    the repository's folder name, "Home folder", or "No folder"."""
    if not path or is_scratch(path):
        return "No folder", None
    root = repo_root(path)
    if root:
        return Path(root).name, root
    p = Path(str(path))
    try:
        if p.resolve() == Path.home().resolve() or str(p) in ("/", "\\"):
            return "Home folder", None
    except OSError:
        pass
    return p.name or "Home folder", None


def vira_repo():
    """The main checkout this code belongs to: its files are the ones the
    module tree places."""
    return repo_root(str(ROOT)) or str(ROOT)


_tree = {"at": 0.0, "pats": None}
_WT_PREFIX = re.compile(r"^.*?/(?:\.claude/worktrees|\.worktrees)/[^/]+/")


def _patterns():
    """[(pattern, submodule id)] from this checkout's module tree, read at
    most once a minute."""
    if _tree["pats"] is None or time.time() - _tree["at"] > 60:
        from . import moduletree
        pats = []
        try:
            tree = moduletree.load()
            for sub in moduletree.submodules(tree):
                for p in sub.get("owns") or ():
                    pats.append((p, sub["id"]))
        except Exception:  # noqa: BLE001 - no tree means no module, not a crash
            pats = []
        _tree.update(at=time.time(), pats=pats)
    return _tree["pats"]


def rel_path(path, repo=None):
    """A file path relative to its checkout or worktree, posix form."""
    p = str(path or "").replace("\\", "/")
    if not p.startswith("/") and not re.match(r"^[A-Za-z]:/", p):
        return p.lstrip("./")
    m = _WT_PREFIX.match(p)
    if m:
        return p[m.end():]
    if repo:
        r = str(repo).replace("\\", "/").rstrip("/") + "/"
        if p.startswith(r):
            return p[len(r):]
    return ""


def module_for(relpath):
    """The submodule that owns one repository-relative path, or ""."""
    from . import moduletree
    if not relpath:
        return ""
    exact = [sid for p, sid in _patterns() if p == relpath]
    if exact:
        return exact[0]
    for p, sid in _patterns():
        if moduletree.matches(p, relpath):
            return sid
    return ""


def modules_of(files, repo=None):
    """({submodule: file count}, top submodule) for files in the Vira
    repository. Other repositories have no module tree: ({}, "")."""
    if not files:
        return {}, ""
    if repo and os.path.realpath(str(repo)) != os.path.realpath(vira_repo()):
        return {}, ""
    counts = {}
    for f in files:
        sid = module_for(rel_path(f, repo or vira_repo()))
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    if not counts:
        return {}, ""
    top = max(counts, key=lambda k: (counts[k], -list(counts).index(k)))
    return counts, top


# ---------------------------------------------------------------- cost

# API list prices in US dollars per million tokens: (input, output, cache
# read). Cache writes are priced from input: 1.25x for the 5-minute cache,
# 2x for the 1-hour one. These are the published first-party rates as of
# 2026-10-06; a model not in the table gets no cost (never a guess). Most
# sessions here run on a subscription, so the figure is what the same
# tokens would cost on the API, said as such in `cost_basis`.
PRICES = (
    ("claude-fable-5-1", 10.0, 50.0, 0.25), ("claude-mythos-5-1", 10.0, 50.0, 0.25),
    ("claude-fable-5", 10.0, 50.0, 1.0), ("claude-mythos-5", 10.0, 50.0, 1.0),
    ("claude-opus-5-5", 4.0, 20.0, 0.20), ("claude-opus-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-8", 5.0, 25.0, 0.50), ("claude-opus-4-7", 5.0, 25.0, 0.50),
    ("claude-opus-4-6", 5.0, 25.0, 0.50), ("claude-opus-4-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-1", 15.0, 75.0, 1.50), ("claude-opus-4", 15.0, 75.0, 1.50),
    ("claude-sonnet-5-5", 2.0, 10.0, 0.20), ("claude-sonnet-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-4-6", 3.0, 15.0, 0.30), ("claude-sonnet-4-5", 3.0, 15.0, 0.30),
    ("claude-sonnet-4", 3.0, 15.0, 0.30),
    ("claude-haiku-5-5", 0.10, 0.50, 0.01), ("claude-haiku-4-5", 1.0, 5.0, 0.10),
    ("claude-3-5-haiku", 0.80, 4.0, 0.08),
)
COST_BASIS = "API list price for these tokens"


def price_of(model):
    m = str(model or "").lower()
    if m and not m.startswith("claude-"):
        m = "claude-" + m
    for prefix, inp, out, read in PRICES:
        if m.startswith(prefix):
            return inp, out, read
    return None


def add_usage(totals, model, usage):
    """Fold one API usage object into per-model totals in place."""
    if not isinstance(usage, dict):
        return totals
    row = totals.setdefault(str(model or "unknown"), {
        "input": 0, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0})
    row["input"] += int(usage.get("input_tokens") or 0)
    row["output"] += int(usage.get("output_tokens") or 0)
    row["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    total_w = int(usage.get("cache_creation_input_tokens") or 0)
    if total_w and not (w1h or w5m):
        w5m = total_w
    row["cache_write_5m"] += w5m
    row["cache_write_1h"] += w1h
    return totals


def cost_of(totals):
    """(US dollars or None, total tokens) for per-model totals. None when
    any model with tokens has no price, so a partial figure never reads as
    the whole."""
    usd, tokens, unknown = 0.0, 0, False
    for model, t in (totals or {}).items():
        n = sum(int(v or 0) for v in t.values())
        tokens += n
        if not n:
            continue
        p = price_of(model)
        if not p:
            unknown = True
            continue
        inp, out, read = p
        usd += (t.get("input", 0) * inp + t.get("output", 0) * out
                + t.get("cache_read", 0) * read
                + t.get("cache_write_5m", 0) * inp * 1.25
                + t.get("cache_write_1h", 0) * inp * 2.0) / 1e6
    return (None if unknown or not tokens else round(usd, 4)), tokens


# ---------------------------------------------------------------- events

def event(id, kind, at, *, sub="", status="", title="", end="", project="",
          folder="", repo="", module="", modules=None, links=None, data=None,
          source=""):
    """A normalized event dict. Empty links are dropped; link values are
    de-duplicated lists of strings."""
    if kind not in KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    if status and status not in STATUSES:
        raise ValueError(f"unknown status word {status!r}")
    clean_links = {}
    for k, v in (links or {}).items():
        vals = v if isinstance(v, (list, tuple, set)) else [v]
        out = []
        for x in vals:
            x = "" if x is None else str(x)
            if x and x not in out:
                out.append(x)
        if out:
            clean_links[k] = out
    ev = {"id": str(id), "kind": kind, "sub": sub or "", "at": iso(at),
          "end": iso(end) if end else "", "status": status or "",
          "title": title or "", "project": project or "", "folder": folder or "",
          "repo": repo or "", "module": module or "", "modules": dict(modules or {}),
          "links": clean_links, "data": dict(data or {}), "source": source or ""}
    return ev


def content_hash(ev):
    body = {k: v for k, v in ev.items() if k not in ("logged", "ts", "end_ts")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------- the files

def store_dir():
    return DIR


def _segments(d=None):
    d = Path(d or store_dir())
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir()
             if p.name.startswith("events-") and (p.name.endswith(".jsonl")
                                                  or p.name.endswith(".jsonl.gz"))]
    return sorted(files, key=lambda p: p.name.replace(".gz", ""))


def signature(d=None):
    """A cheap fingerprint of the store: (name, size, mtime) per file. It
    changes on every append; the stream and the poll hook watch it."""
    out = []
    for p in _segments(d):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_size, st.st_mtime_ns))
    return tuple(out)


def revision(d=None):
    """One short string for `signature`, for clients to compare."""
    sig = signature(d)
    return hashlib.sha256(repr(sig).encode("utf-8")).hexdigest()[:16] if sig else "empty"


def iter_lines(d=None):
    """Every stored line, oldest file first, in write order. A line that
    does not parse (a torn write from a killed process) is skipped."""
    for p in _segments(d):
        opener = gzip.open if p.name.endswith(".gz") else open
        try:
            with opener(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("id"):
                        yield ev
        except (OSError, EOFError):
            continue


def load(d=None):
    """{id: newest line} for the whole store, cached on the signature, so a
    read with nothing new costs a stat per file."""
    d = Path(d or store_dir())
    sig = (str(d), signature(d))
    with _lock:
        if _cache["sig"] == sig:
            return _cache["events"]
    events, n = {}, 0
    for ev in iter_lines(d):
        n += 1
        events[ev["id"]] = ev
    with _lock:
        _cache.update(sig=sig, events=events, lines=n)
    return events


def line_count():
    return _cache.get("lines", 0)


def _current_file(d):
    return Path(d) / f"events-{datetime.now(timezone.utc).strftime('%Y-%m')}.jsonl"


def append(events, d=None):
    """Write events as new lines. Returns how many were written."""
    events = [e for e in events if e]
    if not events:
        return 0
    d = Path(d or store_dir())
    d.mkdir(parents=True, exist_ok=True)
    path = _current_file(d)
    stamp = now_iso()
    text = "".join(json.dumps(dict(e, logged=stamp), ensure_ascii=False,
                              default=str) + "\n" for e in events)
    with locked(d / "events"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
    return len(events)


def archive_lines(path, rows):
    """Append rows to a store's archive sidecar (<store>-archive.jsonl), one
    JSON line each, under the sidecar's lock. A store that keeps only its
    newest N records calls this with the records it is about to drop, so
    the history ingest still reads them. Never raises: losing the archive
    line must not fail the write that trims the store."""
    rows = [r for r in rows or () if r]
    if not rows:
        return 0
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)
        with locked(path):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
        return len(rows)
    except Exception:  # noqa: BLE001 - see the docstring
        return 0


def put(candidates, d=None, current=None):
    """Append each candidate whose content differs from the stored line for
    its id (or that is new). Returns how many lines were written."""
    current = load(d) if current is None else current
    fresh, seen = [], {}
    for ev in candidates:
        if not ev or not ev.get("id"):
            continue
        h = content_hash(ev)
        if seen.get(ev["id"]) == h:
            continue
        old = current.get(ev["id"])
        if old is not None and content_hash(old) == h:
            continue
        seen[ev["id"]] = h
        fresh.append(ev)
    return append(fresh, d)


def compress_old(d=None, keep_months=2):
    """Gzip the month files older than the newest `keep_months`. The gzip is
    written beside the file and swapped in only when complete; the plain
    file goes only after the gzip reads back with the same lines."""
    d = Path(d or store_dir())
    plain = sorted(p for p in _segments(d) if p.name.endswith(".jsonl"))
    done = []
    for p in plain[:-keep_months] if len(plain) > keep_months else []:
        gz = p.with_name(p.name + ".gz")
        tmp = p.with_name(p.name + ".gz.tmp")
        with locked(d / "events"):
            raw = p.read_bytes()
            with gzip.open(tmp, "wb") as fh:
                fh.write(raw)
            with gzip.open(tmp, "rb") as fh:
                if fh.read() != raw:
                    tmp.unlink()
                    continue
            tmp.replace(gz)
            p.unlink()
        done.append(gz.name)
    return done

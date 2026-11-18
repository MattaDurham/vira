"""The Sessions vault: every agent session on this machine kept whole, one
folder per session, in a Markdown vault Vira indexes as its own source.

WHAT A SESSION FOLDER HOLDS.

    <vault>/2026/2026-11/2026-11-09/2026-11-09 1037 project - title/
        conversation.md     every message, the readable thinking, each step
        <retro stem>.md     the retro (written by server/sessionretro.py)
        work/work.md        every step's full input and output, every record
        work/helpers/*.md   each helper agent's conversation and work
        original/           byte-for-byte copies of every file the session left
        original/manifest.json   source path, size, time and sha256 per copy
        original/versions/  an earlier copy, kept when a source was rewritten

The day folder also holds the day rollups; weeks/ holds the week rollups.
`original/` and `work/` are left out of the search index (the vault's
`exclude_dirs`); they are kept and readable.

WHERE SESSIONS COME FROM. The list is built from the files on disk, not
from the history store, because the history store lists only the sessions
the apps show (a few hundred) while the logs on disk number in the
thousands: terminal sessions, sessions Vira runs through the SDK, scheduled
runs. Three kinds of source:

- Claude Code: `<projects root>/<folder>/<id>.jsonl`, plus `<id>/` beside it
  (helper agents, saved tool output), the file backups taken before edits
  (`~/.claude/file-history/<id>`), the desktop app's record of the session,
  and the session's scratchpad folder, which the Mac clears on restart.
- Codex: `rollout-*.jsonl` (and `.jsonl.zst`, the compressed form Codex
  moves old sessions to) under a sessions root, plus the thread's row in
  Codex's own database.
- Vira: each job folder under data/jobs. A job that ran through the Claude
  runtime belongs to that Claude session's folder; a job with no transcript
  of its own is a session by itself.

`import` runs the same discovery over other roots (the old Mac's backup, a
cloud session pulled onto this Mac), so an imported session is shaped
exactly like one from this Mac.

NOTHING IS DROPPED. Every file is copied as it is: the copies are the record
of truth and the pages are made from them. A copy is refreshed when its
source changes. When the new source does not begin with the old copy's
bytes (it was rewritten, not appended to), the old copy moves to
original/versions/ first. Nothing in the vault is ever deleted by this
module. `check` compares every source on disk with the vault and lists
anything missing.

THINKING. Claude writes its thinking to the log as readable text (for
current models, a summary of the reasoning); it is shown in the
conversation. Codex stores most reasoning encrypted with a key only its
provider holds; the conversation says where each encrypted step was, and
the encrypted bytes are in original/.

Store: data/session-vault.json maps each session to its folder, so a title
that changes later never renames a folder. It can be rebuilt from the
folders (`rebuild-store`): every conversation.md carries its keys.

Runs out of process (`python -m server.sessionvault sync`), never inside
the server: a first sync reads tens of gigabytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import history, jsonstore, settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "session-vault.json"
JOBS = ROOT / "data" / "jobs"
GENERATED_BY = "vira-session-vault"
CONVERSATION = "conversation.md"
ORIGINAL = "original"
WORK = "work"
MANIFEST = "manifest.json"

# The working day starts at 04:00, the same boundary the retros and their
# rollups use, so a session after midnight files under the day it belongs to.
DAY_START_HOUR = 4
# A folder name is cut to this many characters of title: long enough to
# read, short enough for every file system's 255-byte name limit with the
# date, time and project in front.
TITLE_IN_NAME = 80
# Lines longer than this are a pasted image or a file dump; the page shows
# their size and points at the copy instead of inlining megabytes of base64.
INLINE_CAP = 200_000
# Hashing reads in blocks of this size.
BLOCK = 1 << 20

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_WRAPPER = re.compile(r"<(system-reminder|local-command-stdout|command-name|command-message|"
                      r"command-args|local-command-caveat)>.*?</\1>", re.S)
_WT = re.compile(r"^(.*?)/(?:\.claude/worktrees|\.worktrees)/[^/]+")

_override = {"root": None, "store": None, "claude": None, "codex": None, "jobs": None,
             "app": None, "file_history": None, "scratch": None, "codex_db": None,
             "cloud": None}


# ---------------------------------------------------------------- places

def vault_root():
    if _override["root"]:
        return Path(_override["root"])
    raw = str(settings.get("session_vault_root") or "").strip()
    return Path(raw).expanduser() if raw else None


def _store():
    return Path(_override["store"]) if _override["store"] else STORE


def _home():
    return Path.home()


def claude_roots():
    return [Path(_override["claude"])] if _override["claude"] else [_home() / ".claude" / "projects"]


def codex_roots():
    if _override["codex"]:
        return [Path(_override["codex"])]
    base = _home() / ".codex"
    return [base / "sessions", base / "archived_sessions"]


def jobs_root():
    return Path(_override["jobs"]) if _override["jobs"] else JOBS


def _app_root():
    if _override["app"]:
        return Path(_override["app"])
    if settings.IS_MAC:
        return _home() / "Library" / "Application Support" / "Claude"
    return None


def _file_history_root():
    return Path(_override["file_history"]) if _override["file_history"] else \
        _home() / ".claude" / "file-history"


def _scratch_roots():
    """Where the desktop app keeps each session's scratchpad (cleared when
    the Mac restarts, which is why it is copied)."""
    if _override["scratch"]:
        return [Path(_override["scratch"])]
    if not settings.IS_MAC:
        return []
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return [Path(f"/private/tmp/claude-{uid}")]


def _codex_db():
    if _override["codex_db"]:
        return Path(_override["codex_db"])
    return _home() / ".codex" / "state_5.sqlite"


# ---------------------------------------------------------------- time

def _local(ts):
    return datetime.fromtimestamp(ts).astimezone()


def work_day(ts):
    from datetime import timedelta
    return (_local(ts) - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


def day_dir(day, root=None):
    root = root or vault_root()
    return root / day[:4] / day[:7] / day


def weeks_dir(root=None):
    return (root or vault_root()) / "weeks"


# ---------------------------------------------------------------- reading

def _zstd_lines(path):
    exe = shutil.which("zstd")
    if not exe:
        raise OSError("the log is zstd-compressed and no zstd program is installed")
    proc = subprocess.Popen([exe, "-dc", str(path)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        for raw in proc.stdout:
            yield raw.decode("utf-8", "replace")
    finally:
        proc.stdout.close()
        proc.wait()


def _raw_lines(path):
    if str(path).endswith(".zst"):
        yield from _zstd_lines(path)
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        yield from fh


def rows(path, problems=None):
    """Every JSON row of a log, in order. A line that is not JSON is kept as
    {"_unparsed": text} so the work page still shows it."""
    try:
        for line in _raw_lines(path):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = {"_unparsed": line}
            if isinstance(row, dict) and "timestamp" not in row and row.get("created_at"):
                row["timestamp"] = row["created_at"]     # claude.ai's event stream
            yield row if isinstance(row, dict) else {"_unparsed": line}
    except OSError as exc:
        if problems is not None:
            problems.append(f"{Path(path).name}: {exc}")


def _text(blocks):
    if isinstance(blocks, str):
        return blocks
    out = []
    for b in blocks if isinstance(blocks, list) else []:
        if isinstance(b, str):
            out.append(b)
        elif isinstance(b, dict):
            if isinstance(b.get("text"), str):
                out.append(b["text"])
            elif isinstance(b.get("content"), (str, list)):
                out.append(_text(b["content"]))
            elif b.get("type") == "image":
                out.append("[image, kept in original/]")
    return "\n".join(out)


# ---------------------------------------------------------------- discovery

def _app_records(app_root):
    """{claude session id: path of the desktop app's record of it}."""
    out = {}
    if not app_root:
        return out
    for sub in ("claude-code-sessions", "local-agent-mode-sessions"):
        base = app_root / sub
        if not base.is_dir():
            continue
        for p in base.glob("*/*/local_*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            cid = rec.get("cliSessionId") or rec.get("unarchivedCliSessionId")
            if cid:
                out[str(cid)] = p
    return out


def _scratch_dirs(sid):
    out = []
    for root in _scratch_roots():
        if root.is_dir():
            out += [p for p in root.glob(f"*/{sid}") if p.is_dir()]
    return out


def _codex_threads(db):
    """{thread id: row} from Codex's own thread table, read-only."""
    if not db or not Path(db).is_file():
        return {}
    try:
        con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        rows_ = con.execute("SELECT * FROM threads").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {str(r["id"]): {k: r[k] for k in r.keys()} for r in rows_}


def _codex_names(index):
    """{thread id: the name Codex shows} from its session index (the last
    line for an id wins: a rename appends)."""
    out = {}
    if not index or not Path(index).is_file():
        return out
    for row in rows(index):
        if row.get("id") and row.get("thread_name"):
            out[str(row["id"])] = str(row["thread_name"])
    return out


_ASK = re.compile(r"##\s*My request:\s*(.*)", re.S)


def _the_ask(text):
    """What the person asked, without the context block an app puts in
    front of it (attached files, open pages)."""
    text = str(text or "")
    m = _ASK.search(text)
    return (m.group(1) if m else text).strip()


def _codex_parent(log):
    """The thread that started a Codex helper agent, from its first record."""
    for row in rows(log):
        p = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("type") == "session_meta":
            return str(p.get("parent_thread_id") or "")
        return ""
    return ""


def _job_links(jroot):
    """{transcript session id: [job dir]} and the jobs with none."""
    linked, alone = {}, []
    if not jroot.is_dir():
        return linked, alone
    for jdir in sorted(p for p in jroot.iterdir() if p.is_dir()):
        state = jsonstore.read(jdir / "state.json", {}) or {}
        sid = str(state.get("session_id") or "")
        if sid:
            linked.setdefault(sid, []).append(jdir)
        else:
            alone.append(jdir)
    return linked, alone


def cloud_root():
    return Path(_override["cloud"]) if _override.get("cloud") else ROOT / "data" / "claude-ai-sessions"


def discover(origin="this Mac", claude=None, codex=None, jobs=True, app_root="default",
             skip=None, cloud=None):
    """Every session the given roots hold, as source records:
    {key, tool, sid, log, extras: [(path, name in original/)], app_record,
     codex_thread, jobs: [dir], origin}."""
    skip = skip or (lambda p: False)
    app = _app_records(_app_root() if app_root == "default" else app_root)
    linked, alone = _job_links(jobs_root()) if jobs else ({}, [])
    out = []
    for root in claude if claude is not None else claude_roots():
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or skip(folder):
                continue
            for log in sorted(folder.glob("*.jsonl")):
                sid = log.stem
                rec = {"key": f"claude:{sid}", "tool": "claude", "sid": sid, "log": log,
                       "origin": origin, "extras": [], "jobs": linked.pop(sid, []),
                       "app_record": app.get(sid), "codex_thread": None}
                side = folder / sid
                if side.is_dir():
                    rec["extras"].append((side, "session-folder"))
                fh = _file_history_root() / sid
                if origin == "this Mac" and fh.is_dir():
                    rec["extras"].append((fh, "file-history"))
                if origin == "this Mac":
                    for i, sc in enumerate(_scratch_dirs(sid)):
                        rec["extras"].append((sc, "scratchpad" + (f"-{i + 1}" if i else "")))
                out.append(rec)
    threads = _codex_threads(_codex_db()) if codex is None else {}
    names = _codex_names(_codex_db().parent / "session_index.jsonl") if codex is None else {}
    for tid, name in names.items():
        threads.setdefault(tid, {})["name_shown"] = name
    for root in codex if codex is not None else codex_roots():
        if not root.is_dir():
            continue
        for log in sorted(list(root.rglob("rollout-*.jsonl")) + list(root.rglob("rollout-*.jsonl.zst"))):
            if skip(log):
                continue
            m = _UUID.findall(log.name)
            sid = m[-1] if m else log.stem
            out.append({"key": f"codex:{sid}", "tool": "codex", "sid": sid, "log": log,
                        "origin": origin, "extras": [], "jobs": linked.pop(sid, []),
                        "app_record": None, "codex_thread": threads.get(sid)})
    # A Codex log can sit in both the live and the compressed form for a
    # moment: keep the one whose name has no .zst when both are present.
    seen = {}
    for rec in out:
        prev = seen.get(rec["key"])
        if prev is None or str(prev["log"]).endswith(".zst"):
            seen[rec["key"]] = rec
    # A Codex helper agent writes a log of its own that names the thread
    # that started it: it belongs in that session's folder, as Claude's
    # helper agents do, not in a folder of its own.
    for rec in list(seen.values()):
        if rec["tool"] != "codex":
            continue
        parent = _codex_parent(rec["log"])
        if parent and f"codex:{parent}" in seen and parent != rec["sid"]:
            seen[f"codex:{parent}"]["extras"].append((rec["log"], "helpers"))
            del seen[rec["key"]]
    out = list(seen.values())
    for root in cloud if cloud is not None else ([cloud_root()] if origin == "this Mac" else []):
        if not Path(root).is_dir():
            continue
        for log in sorted(Path(root).glob("*.events.jsonl")):
            sid = log.name[:-len(".events.jsonl")]
            meta = log.with_name(f"{sid}.session.json")
            out.append({"key": f"cloud:{sid}", "tool": "cloud", "sid": sid, "log": log,
                        "origin": "claude.ai", "extras": [(meta, "claude-ai-record")] if meta.is_file() else [],
                        "jobs": [], "app_record": None, "codex_thread": None})
    if jobs:
        for jdirs in linked.values():
            alone.extend(jdirs)      # a job whose transcript is not on disk
        for jdir in alone:
            out.append({"key": f"vira:{jdir.name}", "tool": "vira", "sid": jdir.name,
                        "log": None, "origin": origin, "extras": [], "jobs": [jdir],
                        "app_record": None, "codex_thread": None})
    return out


# ---------------------------------------------------------------- facts

def _project(cwd):
    if not cwd:
        return "No folder"
    m = _WT.match(str(cwd).replace("\\", "/"))
    if m:
        return Path(m.group(1)).name or "No folder"
    if history.is_scratch(cwd):
        return "No folder"
    p = Path(str(cwd))
    try:
        if p.resolve() == _home().resolve():
            return "Home folder"
    except OSError:
        pass
    if p.exists():
        try:
            return history.project_of(str(cwd))[0]
        except Exception:  # noqa: BLE001 - a git lookup never stops a sync
            pass
    return p.name or "Home folder"


def _started_by(entry, rec):
    """you / vira / a program, from how the session was opened."""
    if entry in ("claude-desktop", "cli", "claude-vscode", "codex-app", "vscode", "cli-tui",
                 "bridge", "anthropic_cloud", "claude.ai"):
        return "you"
    if rec["jobs"] or entry == "sdk-py":
        return "vira"            # Vira runs its model sessions through the Python SDK
    if rec["tool"] == "codex":
        src = str((rec.get("codex_thread") or {}).get("source") or "").lower()
        return "you" if src in ("", "cli", "vscode", "app", "desktop") else "a program"
    return "a program" if entry else "you"


def facts(rec):
    """Metadata read from the log and its neighbours: times, folder, title,
    models, counts. Never invented: a field the record lacks stays empty."""
    f = {"start": 0.0, "end": 0.0, "cwd": "", "title": "", "models": Counter(),
         "branches": set(), "entry": "", "version": "", "you": 0, "replies": 0,
         "steps": 0, "thinking": 0, "encrypted": 0, "tokens_in": 0, "tokens_out": 0,
         "first_ask": "", "parent": "", "app_id": "", "permission": "", "problems": []}

    def stamp(ts):
        t = history.to_ts(ts)
        if t:
            f["start"] = t if not f["start"] else min(f["start"], t)
            f["end"] = max(f["end"], t)

    f["bridge"] = []
    if rec["tool"] in ("claude", "cloud") and rec["log"]:
        for row in rows(rec["log"], f["problems"]):
            stamp(row.get("timestamp"))
            f["cwd"] = f["cwd"] or str(row.get("cwd") or "")
            f["entry"] = f["entry"] or str(row.get("entrypoint") or "")
            f["version"] = str(row.get("version") or f["version"])
            if row.get("gitBranch") not in (None, "", "HEAD"):
                f["branches"].add(str(row["gitBranch"]))
            if row.get("type") == "custom-title" and row.get("customTitle"):
                f["title"] = str(row["customTitle"])
            if row.get("permissionMode"):
                f["permission"] = str(row["permissionMode"])
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            if row.get("isSidechain"):
                continue
            if row.get("type") == "user" and not row.get("isMeta"):
                c = msg.get("content")
                said = c if isinstance(c, str) else "\n".join(
                    b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text") \
                    if isinstance(c, list) else ""
                said = _WRAPPER.sub("", said or "").strip()
                if said:
                    f["you"] += 1
                    f["first_ask"] = f["first_ask"] or said
            elif row.get("type") == "assistant":
                if msg.get("model") and msg["model"] != "<synthetic>":
                    f["models"][msg["model"]] += 1
                u = msg.get("usage") or {}
                f["tokens_in"] += int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
                    + int(u.get("cache_creation_input_tokens") or 0)
                f["tokens_out"] += int(u.get("output_tokens") or 0)
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        f["replies"] += 1
                    elif b.get("type") == "tool_use":
                        f["steps"] += 1
                    elif b.get("type") == "thinking":
                        f["thinking"] += 1
                    elif b.get("type") == "redacted_thinking":
                        f["encrypted"] += 1
        if rec.get("app_record"):
            try:
                app = json.loads(Path(rec["app_record"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                app = {}
            f["title"] = str(app.get("title") or f["title"])
            f["app_id"] = str(app.get("sessionId") or "")
            f["parent"] = str(app.get("forkedFromSessionId") or "")
            f["permission"] = str(app.get("permissionMode") or f["permission"])
            f["bridge"] = [str(b) for b in app.get("bridgeSessionIds") or []]
            if app.get("model"):
                f["models"][str(app["model"])] += 0
    elif rec["tool"] == "codex" and rec["log"]:
        for row in rows(rec["log"], f["problems"]):
            stamp(row.get("timestamp"))
            p = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            kind, ptype = row.get("type"), p.get("type")
            if kind == "session_meta":
                f["cwd"] = f["cwd"] or str(p.get("cwd") or "")
                f["entry"] = str(p.get("originator") or p.get("source") or f["entry"])
                f["version"] = str(p.get("cli_version") or f["version"])
                git = p.get("git") if isinstance(p.get("git"), dict) else {}
                if git.get("branch"):
                    f["branches"].add(str(git["branch"]))
            elif kind == "turn_context":
                f["cwd"] = f["cwd"] or str(p.get("cwd") or "")
                if p.get("model"):
                    f["models"][str(p["model"])] += 1
                if p.get("approval_policy"):
                    f["permission"] = str(p.get("approval_policy"))
            elif kind == "event_msg" and ptype == "user_message":
                f["you"] += 1
                f["first_ask"] = f["first_ask"] or _the_ask(p.get("message"))
            elif kind == "event_msg" and ptype == "agent_message":
                f["replies"] += 1
                f["first_reply"] = f.get("first_reply") or str(p.get("message") or "")
            elif kind == "event_msg" and ptype == "item_completed":
                # the Codex app's newer form: one completed item per message
                item = p.get("item") if isinstance(p.get("item"), dict) else {}
                if item.get("type") == "UserMessage":
                    f["you"] += 1
                    f["first_ask"] = f["first_ask"] or _the_ask(_text(item.get("content")))
                elif item.get("type") == "AgentMessage":
                    f["replies"] += 1
                    f["first_reply"] = f.get("first_reply") or _text(item.get("content"))
            elif kind == "event_msg" and ptype == "token_count":
                info = p.get("info") if isinstance(p.get("info"), dict) else {}
                tot = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                if tot:
                    f["tokens_in"] = int(tot.get("input_tokens") or 0)
                    f["tokens_out"] = int(tot.get("output_tokens") or 0)
            elif kind == "response_item" and ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                f["steps"] += 1
            elif kind == "response_item" and ptype == "reasoning":
                if _reasoning_text(p):
                    f["thinking"] += 1
                else:
                    f["encrypted"] += 1
        th = rec.get("codex_thread") or {}
        f["title"] = str(th.get("name_shown") or th.get("name") or f["title"]
                         or _the_ask(th.get("title")))
        f["cwd"] = f["cwd"] or str(th.get("cwd") or "")
    for jdir in rec["jobs"]:
        job = jsonstore.read(Path(jdir) / "job.json", {}) or {}
        state = jsonstore.read(Path(jdir) / "state.json", {}) or {}
        stamp(job.get("started"))
        stamp(state.get("finished") or state.get("heartbeat"))
        f["title"] = f["title"] or str(job.get("subject") or "")
        f["cwd"] = f["cwd"] or str(job.get("cwd") or "")
        if job.get("model_resolved") or job.get("model"):
            f["models"][str(job.get("model_resolved") or job.get("model"))] += 0
        if rec["tool"] == "vira" and not f["first_ask"]:
            f["first_ask"] = str(job.get("prompt") or "")
            f["you"] += 1 if f["first_ask"] else 0
    if not f["start"] and rec["log"]:
        try:
            st = Path(rec["log"]).stat()
            f["start"] = f["end"] = st.st_mtime
        except OSError:
            pass
    if rec["tool"] == "cloud":
        meta = jsonstore.read(Path(rec["log"]).with_name(f"{rec['sid']}.session.json"), {}) or {}
        f["title"] = str(meta.get("title") or f["title"])
        f["entry"] = str(meta.get("environment_kind") or "claude.ai")
        f["app_id"] = ""
        stamp(meta.get("created_at"))
        if meta.get("configured_model"):
            f["models"][str(meta["configured_model"])] += 0
    f["title"] = history.clean_title(_the_ask(f["title"] or f["first_ask"] or f.get("first_reply", "")).split("\n")[0],
                                     cap=TITLE_IN_NAME) or "Untitled"
    f["project"] = _project(f["cwd"])
    f["started_by"] = _started_by(f["entry"], rec)
    return f


def _reasoning_text(p):
    parts = []
    for s in p.get("summary") or []:
        if isinstance(s, dict) and s.get("text"):
            parts.append(str(s["text"]))
    for c in p.get("content") or []:
        if isinstance(c, dict) and c.get("text"):
            parts.append(str(c["text"]))
    return "\n".join(parts).strip()


# ---------------------------------------------------------------- folders

def _safe(text):
    s = re.sub(r'[\\/:*?"<>|\n\r\t#^\[\]]+', "-", str(text)).strip(" .-")
    return re.sub(r"\s+", " ", s) or "Untitled"


def folder_name(f):
    start = _local(f["start"]) if f["start"] else None
    stamp = start.strftime("%Y-%m-%d %H%M") if start else "0000-00-00 0000"
    return f"{stamp} {_safe(f['project'])} - {_safe(f['title'])[:TITLE_IN_NAME].rstrip(' .-')}"


def _read_store():
    return jsonstore.read(_store(), {"sessions": {}}) or {"sessions": {}}


def folder_for(key, f=None, root=None, create=False):
    """The session's folder: the one already recorded for it, else a new one
    named from its start and title. A folder, once made, is never renamed."""
    root = root or vault_root()
    st = _read_store()
    row = (st.get("sessions") or {}).get(key)
    if row and row.get("folder"):
        return root / row["folder"]
    alias = (st.get("aliases") or {}).get(key)
    if alias and (st.get("sessions") or {}).get(alias, {}).get("folder"):
        return root / st["sessions"][alias]["folder"]
    if not create or f is None:
        return None
    day = work_day(f["start"]) if f["start"] else "0000-00-00"
    base = day_dir(day, root) / folder_name(f)
    path, n = base, 2
    taken = {r.get("folder") for r in (st.get("sessions") or {}).values()}
    while str(path.relative_to(root)).replace("\\", "/") in taken or \
            (path.exists() and not _owns(path, key)):
        path = base.with_name(f"{base.name} ({n})")
        n += 1
    return path


def _owns(path, key):
    conv = path / CONVERSATION
    if not conv.is_file():
        return False
    head = conv.read_text(encoding="utf-8", errors="ignore")[:4000]
    return f"session_key: {key}" in head


def _remember(key, folder, root, aliases=(), **fields):
    rel = str(Path(folder).relative_to(root)).replace("\\", "/")

    def fn(st):
        st.setdefault("sessions", {})
        row = st["sessions"].setdefault(key, {})
        row.update(fields, folder=rel)
        al = st.setdefault("aliases", {})
        for a in aliases:
            if a and a != key:
                al[a] = key
    jsonstore.mutate(_store(), fn, {"sessions": {}}, indent=1)


# ---------------------------------------------------------------- copies

def _sha256(path, limit=None):
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as fh:
        while True:
            n = BLOCK if left is None else min(BLOCK, left)
            if n <= 0:
                break
            chunk = fh.read(n)
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def _clone(src, dst):
    """Copy a file. On a Mac the copy is a clone: it shares storage with the
    original until either changes, so keeping a copy of every log costs
    almost nothing on the same disk."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".copying")
    if tmp.exists():
        tmp.unlink()
    done = False
    if settings.IS_MAC:
        r = subprocess.run(["/bin/cp", "-c", "-p", str(src), str(tmp)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        done = r.returncode == 0
    if not done:
        shutil.copy2(src, tmp)
    tmp.replace(dst)


def _files_of(rec):
    """[(source file, name under original/)] for every file the session left."""
    out = []
    if rec["log"]:
        out.append((Path(rec["log"]), Path(rec["log"]).name))
    for base, name in rec["extras"]:
        base = Path(base)
        if base.is_file():
            out.append((base, f"{name}/{base.name}"))
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.is_symlink():
                out.append((p, f"{name}/{p.relative_to(base).as_posix()}"))
    for jdir in rec["jobs"]:
        jdir = Path(jdir)
        for p in sorted(jdir.rglob("*")):
            if p.is_file() and not p.name.endswith(".lock"):
                out.append((p, f"vira-job-{jdir.name}/{p.relative_to(jdir).as_posix()}"))
    if rec.get("app_record"):
        out.append((Path(rec["app_record"]), f"app-record/{Path(rec['app_record']).name}"))
    return out


def copy_originals(rec, folder, now=None, prefix=""):
    """Bring original/ up to date with every source file. Returns counts."""
    counts = Counter()
    odir = folder / ORIGINAL
    man_path = odir / MANIFEST
    man = jsonstore.read(man_path, {"files": {}}) or {"files": {}}
    files = man.setdefault("files", {})
    stamp = datetime.fromtimestamp(now or time.time()).astimezone().strftime("%Y-%m-%dT%H%M%S")
    for src, rel in _files_of(rec):
        rel = prefix + rel
        try:
            st = src.stat()
        except OSError:
            counts["source gone"] += 1
            continue
        row = files.get(rel)
        dst = odir / rel
        if row and row.get("size") == st.st_size and row.get("mtime_ns") == st.st_mtime_ns \
                and dst.is_file():
            counts["unchanged"] += 1
            continue
        if row and dst.is_file():
            old_size = int(row.get("size") or 0)
            appended = st.st_size >= old_size and row.get("sha256") and \
                _sha256(src, old_size) == row["sha256"]
            if not appended:
                keep = odir / "versions" / stamp / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                dst.replace(keep)
                counts["earlier version kept"] += 1
        _clone(src, dst)
        files[rel] = {"source": str(src), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                      "sha256": _sha256(dst), "copied": datetime.now().astimezone().isoformat(timespec="seconds")}
        counts["copied"] += 1
    if not prefix:
        man["origin"] = rec["origin"]
        man["session_key"] = rec["key"]
    if counts["copied"] or counts["earlier version kept"] or not man_path.exists():
        jsonstore.write_atomic(man_path, man, indent=1)
    return counts


# ---------------------------------------------------------------- pages

def _fence(text):
    text = str(text)
    if len(text) > INLINE_CAP:
        return f"_[{len(text):,} characters; the full text is in original/]_"
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text}\n{ticks}"


def _quote(text):
    return "\n".join("> " + ln if ln else ">" for ln in str(text).splitlines())


def _when(ts):
    t = history.to_ts(ts)
    return _local(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def _short(inp, cap=160):
    if isinstance(inp, dict):
        for k in ("command", "file_path", "path", "pattern", "url", "query", "prompt", "description"):
            if inp.get(k):
                s = str(inp[k])
                break
        else:
            s = json.dumps(inp, ensure_ascii=False)
    else:
        s = str(inp)
    s = " ".join(s.split())
    return s[:cap] + ("..." if len(s) > cap else "")


def render_claude(path, conv, work, label="Claude", asker="You"):
    """Append a Claude Code log to the two pages: conversation lines to
    `conv`, every record to `work`."""
    step = 0
    for row in rows(path):
        if "_unparsed" in row:
            work.append(f"#### Unreadable line\n\n{_fence(row['_unparsed'])}")
            continue
        typ = row.get("type")
        when = _when(row.get("timestamp"))
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = msg.get("content")
        side = bool(row.get("isSidechain"))
        if typ == "user":
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    raw = b.get("text") or ""
                    said = _WRAPPER.sub("", raw).strip()
                    work.append(f"#### {'Injected text' if row.get('isMeta') else asker} - {when}\n\n{_fence(raw)}")
                    if said and not row.get("isMeta") and not side:
                        conv.append(f"### {asker} - {when}\n\n{said}")
                elif b.get("type") == "tool_result":
                    out = _text(b.get("content"))
                    err = " (error)" if b.get("is_error") else ""
                    work.append(f"#### Result{err} - {when}\n\n{_fence(out)}")
                elif b.get("type") == "image":
                    work.append(f"#### Image - {when}\n\n_[image, kept in original/]_")
                    if not side:
                        conv.append(f"_[You attached an image - {when}]_")
        elif typ == "assistant":
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "thinking":
                    txt = (b.get("thinking") or "").strip()
                    if txt:
                        work.append(f"#### Thinking - {when}\n\n{_quote(txt)}")
                        if not side:
                            conv.append(f"_Thinking:_\n\n{_quote(txt)}")
                    else:
                        work.append(f"#### Thinking - {when}\n\n_[stored encrypted; kept in original/]_")
                elif t == "redacted_thinking":
                    work.append(f"#### Thinking - {when}\n\n_[stored encrypted; kept in original/]_")
                elif t == "text" and (b.get("text") or "").strip():
                    work.append(f"#### {label} - {when}\n\n{b['text'].strip()}")
                    if not side:
                        conv.append(f"### {label} - {when}\n\n{b['text'].strip()}")
                elif t == "tool_use":
                    step += 1
                    name = str(b.get("name") or "tool")
                    work.append(f"#### Step {step}: {name} - {when}\n\n"
                                f"{_fence(json.dumps(b.get('input'), ensure_ascii=False, indent=1))}")
                    if not side:
                        conv.append(f"- Step {step}: {name} - {_short(b.get('input'))}")
        elif typ == "system":
            txt = str(row.get("content") or row.get("subtype") or "")
            if txt:
                work.append(f"#### System - {when}\n\n{_fence(txt)}")
        elif typ == "summary" and row.get("summary"):
            work.append(f"#### Summary - {when}\n\n{row['summary']}")
        elif typ == "attachment":
            att = row.get("attachment")
            work.append(f"#### Attachment - {when}\n\n{_fence(json.dumps(att, ensure_ascii=False, indent=1)[:INLINE_CAP])}")
    return step


def render_codex(path, conv, work):
    step = 0

    for row in rows(path):
        if "_unparsed" in row:
            work.append(f"#### Unreadable line\n\n{_fence(row['_unparsed'])}")
            continue
        p = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind, ptype = row.get("type"), p.get("type")
        when = _when(row.get("timestamp"))
        if kind == "event_msg":
            if ptype == "user_message":
                said = str(p.get("message") or "").strip()
                if said:
                    conv.append(f"### You - {when}\n\n{said}")
                    work.append(f"#### You - {when}\n\n{_fence(said)}")
            elif ptype == "agent_message":
                said = str(p.get("message") or "").strip()
                if said:
                    conv.append(f"### Codex - {when}\n\n{said}")
                    work.append(f"#### Codex - {when}\n\n{said}")

            elif ptype == "agent_reasoning":
                said = str(p.get("text") or "").strip()
                if said:
                    conv.append(f"_Thinking:_\n\n{_quote(said)}")
                    work.append(f"#### Thinking - {when}\n\n{_quote(said)}")
            elif ptype == "item_completed":
                item = p.get("item") if isinstance(p.get("item"), dict) else {}
                itype = item.get("type")
                if itype == "UserMessage":
                    said = _text(item.get("content")).strip()
                    if said:
                        conv.append(f"### You - {when}\n\n{said}")
                        work.append(f"#### You - {when}\n\n{_fence(said)}")
                elif itype == "AgentMessage":
                    said = _text(item.get("content")).strip()
                    if said:
                        conv.append(f"### Codex - {when}\n\n{said}")
                        work.append(f"#### Codex - {when}\n\n{said}")
                elif itype == "Reasoning":
                    said = "\n".join(str(x) for x in item.get("summary_text") or [] if x).strip()
                    if said:
                        conv.append(f"_Thinking:_\n\n{_quote(said)}")
                        work.append(f"#### Thinking - {when}\n\n{_quote(said)}")
                elif itype:
                    work.append(f"#### {itype} - {when}\n\n"
                                f"{_fence(json.dumps(item, ensure_ascii=False, indent=1)[:INLINE_CAP])}")
            continue
        if kind == "response_item":
            if ptype == "message":
                txt = _text(p.get("content"))
                role = p.get("role") or "message"
                # The conversation takes messages from the event stream
                # (user_message / agent_message), which carries each one
                # once; this stream repeats them with injected context, so
                # it goes to the work page whole.
                work.append(f"#### {str(role).capitalize()} - {when}\n\n{_fence(txt)}")
            elif ptype == "reasoning":
                txt = _reasoning_text(p)
                work.append(f"#### Thinking - {when}\n\n" +
                            (_quote(txt) if txt else "_[stored encrypted; kept in original/]_"))
            elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                step += 1
                name = str(p.get("name") or ptype)
                raw = p.get("input") if p.get("input") is not None else p.get("arguments") or p.get("action")
                body = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, indent=1)
                work.append(f"#### Step {step}: {name} - {when}\n\n{_fence(body)}")
                try:
                    shown = json.loads(body) if isinstance(raw, str) and body.strip().startswith("{") else raw
                except ValueError:
                    shown = body
                conv.append(f"- Step {step}: {name} - {_short(shown)}")
            elif ptype in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                out = p.get("output")
                work.append(f"#### Result - {when}\n\n{_fence(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1))}")
            else:
                work.append(f"#### {ptype or kind} - {when}\n\n{_fence(json.dumps(p, ensure_ascii=False, indent=1))}")
        elif kind in ("session_meta", "turn_context"):
            work.append(f"#### {kind} - {when}\n\n{_fence(json.dumps(p, ensure_ascii=False, indent=1))}")
    return step


def render_job(jdir, conv, work, with_conversation):
    """A Vira job's own record: the ask it was given, its thinking, messages
    and steps. Shown in the conversation only when there is no transcript."""
    jdir = Path(jdir)
    job = jsonstore.read(jdir / "job.json", {}) or {}
    work.append(f"### Vira job {jdir.name}\n\n#### The ask Vira sent\n\n{_fence(job.get('prompt') or '')}")
    if with_conversation and job.get("prompt"):
        conv.append(f"### Vira's ask - {_when(job.get('started'))}\n\n{job['prompt']}")
    for row in rows(jdir / "events.jsonl"):
        ev = row.get("event")
        when = _when(row.get("t"))
        if ev == "message_completed":
            txt = str(row.get("text") or "").strip()
            phase = row.get("phase") or ""
            if not txt:
                continue
            if phase == "thinking":
                work.append(f"#### Thinking - {when}\n\n{_quote(txt)}")
                if with_conversation:
                    conv.append(f"_Thinking:_\n\n{_quote(txt)}")
            else:
                work.append(f"#### Reply ({phase or 'message'}) - {when}\n\n{txt}")
                if with_conversation:
                    conv.append(f"### Vira session - {when}\n\n{txt}")
        elif ev in ("tool_started", "tool_completed"):
            r = row.get("receipt") or {}
            work.append(f"#### {ev.replace('_', ' ')}: {r.get('name') or 'tool'} - {when}\n\n"
                        f"{_fence(json.dumps(r, ensure_ascii=False, indent=1))}")
        else:
            work.append(f"#### {ev or 'event'} - {when}\n\n{_fence(json.dumps(row, ensure_ascii=False)[:INLINE_CAP])}")


def _fm(v):
    s = str(v)
    if s == "" or re.search(r'[:#\[\]{},&*!|>\'"%@`]|^\s|\s$', s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def front_matter(rec, f, folder, aliases):
    models = ", ".join(m for m, _ in f["models"].most_common()) or ""
    rows_ = [
        ("tags", f"[session, {rec['tool']}]"),
        ("title", _fm(f["title"])),
        ("tool", rec["tool"]),
        ("session_key", rec["key"]),
        ("session_id", rec["sid"]),
        ("also_known_as", _fm(", ".join(a for a in aliases if a))),
        ("project", _fm(f["project"])),
        ("cwd", _fm(f["cwd"])),
        ("date", _local(f["start"]).strftime("%Y-%m-%d") if f["start"] else ""),
        ("time", _fm(_local(f["start"]).strftime("%H:%M")) if f["start"] else ""),
        ("start", history.iso(f["start"])),
        ("end", history.iso(f["end"])),
        ("started_by", _fm(f["started_by"])),
        ("opened_from", _fm(f["entry"])),
        ("models", _fm(models)),
        ("branches", _fm(", ".join(sorted(f["branches"])))),
        ("permission_mode", _fm(f["permission"])),
        ("forked_from", _fm(f["parent"])),
        ("your_messages", f["you"]),
        ("replies", f["replies"]),
        ("steps", f["steps"]),
        ("thinking_steps_readable", f["thinking"]),
        ("thinking_steps_encrypted", f["encrypted"]),
        ("tokens_in", f["tokens_in"]),
        ("tokens_out", f["tokens_out"]),
        ("origin", _fm(rec["origin"])),
        ("log", _fm(str(rec["log"] or ""))),
        ("generated_by", GENERATED_BY),
    ]
    return "---\n" + "\n".join(f"{k}: {v}" for k, v in rows_) + "\n---"


def _open_link(f, rec):
    if f["app_id"]:
        return f"claude://claude.ai/epitaxy/{f['app_id']}"
    return ""


def render(rec, f, folder, aliases):
    """Write conversation.md and work/ for one session from its copies."""
    conv, work = [], []
    odir = folder / ORIGINAL
    log_copy = odir / Path(rec["log"]).name if rec["log"] else None
    if log_copy and log_copy.is_file():
        if rec["tool"] == "codex":
            render_codex(log_copy, conv, work)
        else:
            # who wrote the "user" turns: you, or Vira or a program asking
            asker = {"vira": "Vira asked", "a program": "Asked by a program"}.get(f["started_by"], "You")
            render_claude(log_copy, conv, work, asker=asker)
    for extra in sorted((odir / "claude-ai").glob("*.events.jsonl")) if (odir / "claude-ai").is_dir() else []:
        work.append(f"### The copy claude.ai kept ({extra.name})")
        render_claude(extra, [], work)
    for jdir in rec["jobs"]:
        jcopy = odir / f"vira-job-{Path(jdir).name}"
        render_job(jcopy if jcopy.is_dir() else jdir, conv, work, with_conversation=not rec["log"])
    helpers = sorted((odir / "session-folder" / "subagents").glob("*.jsonl")) \
        if (odir / "session-folder" / "subagents").is_dir() else []
    codex_helpers = sorted(p for p in (odir / "helpers").glob("*") if p.name.endswith((".jsonl", ".zst"))) \
        if (odir / "helpers").is_dir() else []
    hdir = folder / WORK / "helpers"
    for h in codex_helpers:
        hwork = []
        render_codex(h, [], hwork)
        name = _UUID.findall(h.name)
        _write(hdir / f"{name[-1] if name else h.stem}.md",
               f"# Helper agent {name[-1] if name else h.stem}\n\n" + "\n\n".join(hwork) + "\n")
    helpers_n = len(helpers) + len(codex_helpers)
    for h in helpers:
        hconv, hwork = [], []
        render_claude(h, hconv, hwork, label="Helper")
        meta = h.with_suffix(".meta.json")
        desc = ""
        if meta.is_file():
            try:
                desc = str(json.loads(meta.read_text(encoding="utf-8")).get("description") or "")
            except (OSError, ValueError):
                desc = ""
        _write(hdir / f"{h.stem}.md", f"# Helper agent {h.stem}\n\n{desc}\n\n" + "\n\n".join(hwork) + "\n")
    link = _open_link(f, rec)
    head = front_matter(rec, f, folder, aliases)
    retro = [p for p in folder.glob("*.md") if p.name != CONVERSATION]
    lines = [head, "", f"# {f['title']}", ""]
    lines.append(f"{f['project']} - {_when(f['start'])} to {_when(f['end'])} - "
                 f"{rec['tool']}, started by {f['started_by']}")
    if link:
        lines.append(f"\nOpen the session: {link}")
    if retro:
        lines.append("\nRetro: " + ", ".join(f"[[{p.stem}]]" for p in sorted(retro)))
    lines.append(f"\nEvery step's full input and output: [[{WORK}/work]]. "
                 f"The files this session left, unchanged: `{ORIGINAL}/`.")
    if helpers_n:
        lines.append(f"Helper agents: {helpers_n} (in `{WORK}/helpers/`).")
    if f["encrypted"]:
        lines.append(f"\n{f['encrypted']} thinking steps are stored encrypted by the tool's "
                     "provider; their bytes are kept in `original/` but cannot be read here.")
    if not conv:
        lines.append("\n_No conversation was recorded in this session's log._")
    lines += ["", "## Conversation", "", "\n\n".join(conv), ""]
    _write(folder / CONVERSATION, "\n".join(lines))
    _write(folder / WORK / "work.md", f"# Work - {f['title']}\n\n" + "\n\n".join(work) + "\n")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- sync

def _fingerprint(rec):
    parts = []
    for src, rel in _files_of(rec):
        try:
            st = src.stat()
        except OSError:
            continue
        parts.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:24]


def sync_one(rec, root=None, now=None):
    root = root or vault_root()
    fp = _fingerprint(rec)
    st = _read_store()
    row = (st.get("sessions") or {}).get(rec["key"]) or {}
    folder = folder_for(rec["key"], root=root)
    if folder and row.get("fp") == fp and (folder / CONVERSATION).is_file():
        return "unchanged"
    if rec["tool"] == "cloud" and folder and not _owns(folder, rec["key"]):
        # a Remote Control copy of a session that ran on a Mac: its events
        # are kept with that session, not as a second session
        counts = copy_originals(rec, folder, now=now, prefix="claude-ai/")
        _remember(rec["key"], folder, root, fp=fp, tool="cloud", origin="claude.ai",
                  attached=True)
        return "claude.ai copy kept with its session" if counts["copied"] else "unchanged"
    f = facts(rec)
    folder = folder or folder_for(rec["key"], f, root=root, create=True)
    new = not (folder / CONVERSATION).exists()
    counts = copy_originals(rec, folder, now=now)
    aliases = [f["app_id"]] + [f"vira:{Path(j).name}" for j in rec["jobs"]] + \
              [Path(j).name for j in rec["jobs"]] + [f"cloud:{b}" for b in f["bridge"]]
    render(rec, f, folder, [a for a in aliases if a])
    _remember(rec["key"], folder, root, aliases=[a for a in aliases if a] + [rec["sid"]], fp=fp,
              tool=rec["tool"], origin=rec["origin"], started_by=f["started_by"],
              start=history.iso(f["start"]), end=history.iso(f["end"]), cwd=f["cwd"],
              project=f["project"])
    if f["problems"]:
        return "copied with problems"
    return "new" if new else ("updated" if counts["copied"] else "rendered")


def sync(origin="this Mac", limit=None, only=None, say=None, **roots):
    root = vault_root()
    if not root:
        return {"error": "no Sessions vault folder is set (session_vault_root)"}
    root.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with locked(str(_store()) + ".sync"):
        recs = discover(origin=origin, **roots)
        if only:
            recs = [r for r in recs if r["key"] in only or r["sid"] in only]
        # A claude.ai copy is kept with the Mac session it mirrors, so every
        # Mac session must have its folder before the copies are placed.
        recs.sort(key=lambda r: (r["tool"] == "cloud", _mtime(r)))
        for rec in recs[:limit] if limit else recs:
            try:
                outcome = sync_one(rec, root=root)
            except OSError as exc:
                outcome = "failed"
                if say:
                    say(f"failed: {rec['key']}: {exc}"[:300])
            counts[outcome] += 1
            if say and outcome != "unchanged":
                say(f"{outcome}: {rec['key']}")
        write_readme(root)
    return dict(counts)


def _mtime(rec):
    try:
        return Path(rec["log"]).stat().st_mtime if rec["log"] else \
            (Path(rec["jobs"][0]) / "job.json").stat().st_mtime
    except (OSError, IndexError):
        return 0.0


# ---------------------------------------------------------------- retros

_SKIP_DIRS = {ORIGINAL, WORK, "Projects"}


def retro_files(pattern="*.md", root=None):
    """Every retro and rollup Markdown file in the vault whose name matches
    `pattern`: session retros in session folders, day rollups in day
    folders, week rollups in weeks/. Conversation pages are not retros."""
    root = root or vault_root()
    if not root or not root.is_dir():
        return []
    out = []
    for p in root.rglob(pattern):
        if p.name in (CONVERSATION, "README.md") or not p.is_file():
            continue
        rel = p.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel[:-1]):
            continue
        out.append(p)
    return sorted(out)


def retro_path_for(key, stem, ids=()):
    """Where a session's retro goes in the vault, or None when the session
    has no folder yet (the next sync makes one)."""
    root = vault_root()
    if not root:
        return None
    for k in [key, *ids]:
        folder = folder_for(k, root=root)
        if folder:
            return folder / f"{stem}.md"
    return None


# ---------------------------------------------------------------- adopting

_ROLLUP = re.compile(r"^(\d{4}-\d{2}-\d{2}) (day .+|cross-project|week)$")
_FRONT = re.compile(r"\A---\n(.*?)\n---", re.S)


def _front_of(path):
    try:
        head = Path(path).read_text(encoding="utf-8", errors="ignore")[:6000]
    except OSError:
        return {}
    m = _FRONT.match(head)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line and not line.startswith((" ", "-", "\t")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def _place(src, dest_dir):
    """Move one file into a folder without overwriting anything there: a
    different file of the same name is kept beside it as `(earlier)`."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        if dest.read_bytes() == src.read_bytes():
            src.unlink()
            return dest
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem} (earlier{'' if n == 1 else ' ' + str(n)}){src.suffix}"
            n += 1
    shutil.move(str(src), str(dest))
    return dest


def _by_time(st, cwd, ts):
    """The session that ran in `cwd` around `ts`, for a retro with no id."""
    best = None
    for key, row in (st.get("sessions") or {}).items():
        if not cwd or row.get("cwd") != cwd:
            continue
        a, b = history.to_ts(row.get("start")), history.to_ts(row.get("end"))
        if a and a - 600 <= ts <= (b or a) + 12 * 3600:
            if best is None or abs(a - ts) < best[0]:
                best = (abs(a - ts), key)
    return best[1] if best else None


def adopt(src_dir, dry=False):
    """Move retros and rollups from a flat retro folder (the old TC-IL
    Sessions folder) into the vault: each session's retro into its session
    folder, each rollup into its day folder or weeks/. A retro whose session
    has no log gets a folder of its own that says so. Nothing is
    overwritten. Returns counts and the list of what went where."""
    root = vault_root()
    st = _read_store()
    counts, moves = Counter(), []
    for p in sorted(Path(src_dir).glob("*.md")):
        fm = _front_of(p)
        m = _ROLLUP.match(p.stem)
        if m:
            dest = weeks_dir(root) if m.group(2) == "week" else day_dir(m.group(1), root)
            kind = "rollups"
        else:
            sid = fm.get("session_id", "")
            folder = None
            for k in ([f"claude:{sid}", f"codex:{sid}", f"vira:{sid}", sid] if sid else []):
                folder = folder_for(k, root=root)
                if folder:
                    break
            if not folder:
                from .sessionretro import _file_ts
                ts = _file_ts(fm, p)
                key = _by_time(st, fm.get("cwd", ""), ts) if ts else None
                folder = folder_for(key, root=root) if key else None
            if folder:
                dest, kind = folder, "retros into their session folder"
            else:
                dest, kind = _retro_only_folder(p, fm, root, dry), "retros whose log was not found"
        counts[kind] += 1
        moves.append((str(p), str(dest)))
        if not dry:
            _place(p, dest)
    return {"counts": dict(counts), "moves": moves}


def _retro_only_folder(p, fm, root, dry):
    """A session folder for a retro whose session left no log on disk: the
    retro is all there is, and its conversation page says so."""
    from .sessionretro import _file_ts
    ts = _file_ts(fm, p) or 0.0
    project = fm.get("project") or "No folder"
    f = {"start": ts, "project": project,
         "title": history.clean_title(_title_of(p) or p.stem, cap=TITLE_IN_NAME) or p.stem}
    key = f"retro-only:{p.stem}"
    folder = folder_for(key, root=root) or folder_for(key, f, root=root, create=True)
    if dry:
        return folder
    if not (folder / CONVERSATION).exists():
        sid = fm.get("session_id", "")
        _write(folder / CONVERSATION, "\n".join([
            "---", "tags: [session, retro-only]", f"title: {_fm(f['title'])}",
            f"session_key: {key}", f"session_id: {_fm(sid)}", f"project: {_fm(project)}",
            f"cwd: {_fm(fm.get('cwd', ''))}",
            f"date: {_local(ts).strftime('%Y-%m-%d') if ts else ''}",
            f"start: {history.iso(ts)}", "origin: retro only", f"generated_by: {GENERATED_BY}",
            "---", "", f"# {f['title']}", "",
            "The log of this session was not found on this Mac or on the backup drive "
            "when the vault was built. Its retro is the record that survives: "
            f"[[{p.stem}]].", ""]))
        _remember(key, folder, root, aliases=[sid] if sid else [], tool="retro-only",
                  origin="retro only", start=history.iso(ts), cwd=fm.get("cwd", ""),
                  project=project)
    return folder


def _title_of(p):
    try:
        text = Path(p).read_text(encoding="utf-8", errors="ignore")[:6000]
    except OSError:
        return ""
    body = _FRONT.sub("", text, count=1)
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


# ---------------------------------------------------------------- claude.ai

# Run in a signed-in claude.ai tab (the page's own session reads the
# account's sessions; nothing leaves the browser except the downloads it
# saves). It saves the account's Claude Code sessions, each with every event
# claude.ai holds, as claude-ai-sessions-<n>.jsonl files of BATCH sessions.
EXPORT_SCRIPT = r"""
(async () => {
  const BATCH = 100;
  const org = (document.cookie.match(/lastActiveOrg=([^;]+)/) || [])[1];
  const H = {credentials: 'include', headers: {'anthropic-version': '2023-06-01',
    'anthropic-beta': 'ccr-byoc-2025-07-29', 'x-organization-uuid': org}};
  const get = async (u) => { const r = await fetch(u, H); if (!r.ok) throw new Error(u + ' ' + r.status); return r.json(); };
  let sessions = [], url = '/v1/sessions?limit=100';
  for (;;) { const r = await get(url); sessions = sessions.concat(r.data || []);
    if (!r.has_more) break; url = '/v1/sessions?limit=100&after_id=' + r.last_id; }
  const save = (n, lines) => { const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([lines.join('
') + '
'], {type: 'application/jsonl'}));
    a.download = 'claude-ai-sessions-' + String(n).padStart(3, '0') + '.jsonl'; a.click(); };
  let lines = [], n = 1;
  for (const s of sessions) {
    let events = [], u = '/v1/sessions/' + s.id + '/events?limit=100';
    for (;;) { const r = await get(u); events = events.concat(r.data || []);
      if (!r.has_more) break; u = '/v1/sessions/' + s.id + '/events?limit=100&after_id=' + r.last_id; }
    lines.push(JSON.stringify({session: s, events}));
    if (lines.length === BATCH) { save(n++, lines); lines = []; }
  }
  if (lines.length) save(n, lines);
  return {sessions: sessions.length, files: n};
})()
"""


def import_cloud(src_dir, dest=None):
    """Unpack the export's batch files into one events file and one record
    per session (data/claude-ai-sessions/), rewriting only what changed.
    The next sync files each one: with the Mac session it mirrors when the
    desktop app's record names it, else as a session of its own."""
    dest = Path(dest) if dest else cloud_root()
    dest.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    for batch in sorted(Path(src_dir).glob("claude-ai-sessions-*.jsonl")):
        for row in rows(batch):
            sess = row.get("session") if isinstance(row.get("session"), dict) else None
            if not sess or not sess.get("id"):
                counts["unreadable lines"] += 1
                continue
            sid = str(sess["id"])
            ev_text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in row.get("events") or [])
            ev_path = dest / f"{sid}.events.jsonl"
            if ev_path.is_file() and ev_path.read_text(encoding="utf-8") == ev_text:
                counts["unchanged"] += 1
            else:
                counts["sessions written"] += 1
                _write(ev_path, ev_text)
            _write(dest / f"{sid}.session.json", json.dumps(sess, ensure_ascii=False, indent=1))
    return dict(counts)


# ---------------------------------------------------------------- readme

README = """# Sessions

Every agent session on this machine, one folder per session, by date:
`YYYY/YYYY-MM/YYYY-MM-DD/<date time project - title>/`.

Each session folder holds:

- `conversation.md`: every message from you and the agent, its readable
  thinking, and a line for each step. The details at the top say which
  tool, model, folder and branch it used, when it ran, and who started it.
- the retro, named `<date time project>.md`, once the session has been
  quiet for two hours.
- `work/work.md`: every step's full input and output. `work/helpers/`:
  each helper agent's work.
- `original/`: every file the session left, copied unchanged, with
  `manifest.json` (source, size, sha256). `original/versions/` keeps an
  earlier copy when a source was rewritten. Nothing here is ever deleted.

Each day folder also holds that day's rollups (one per project and one
across projects). `weeks/` holds the weekly rollups.

Search covers the conversations, retros and rollups; `work/` and
`original/` are kept and readable but not indexed.
"""


def write_readme(root):
    p = root / "README.md"
    if not p.exists():
        _write(p, README)


# ---------------------------------------------------------------- check

def check(**roots):
    """Every source file on disk against the vault: missing sessions,
    missing or out-of-date copies. Read-only."""
    root = vault_root()
    out = Counter()
    missing = []
    for rec in discover(**roots):
        folder = folder_for(rec["key"], root=root)
        if not folder or not (folder / CONVERSATION).is_file():
            out["sessions not in the vault"] += 1
            missing.append(rec["key"])
            continue
        man = (jsonstore.read(folder / ORIGINAL / MANIFEST, {}) or {}).get("files") or {}
        prefix = "claude-ai/" if rec["tool"] == "cloud" and not _owns(folder, rec["key"]) else ""
        for src, rel in _files_of(rec):
            rel = prefix + rel
            row = man.get(rel)
            try:
                size = src.stat().st_size
            except OSError:
                continue
            if not row or not (folder / ORIGINAL / rel).is_file():
                out["files not copied"] += 1
                missing.append(f"{rec['key']} {rel}")
            elif row.get("size") != size:
                out["copies behind their source"] += 1
            else:
                out["files copied"] += 1
        out["sessions in the vault"] += 1
    return {"counts": dict(out), "missing": missing[:200], "missing_total": len(missing)}


def rebuild_store():
    """Recreate data/session-vault.json from the folders' own pages."""
    root = vault_root()
    n = 0
    for conv in root.rglob(CONVERSATION) if root and root.is_dir() else []:
        head = conv.read_text(encoding="utf-8", errors="ignore")[:4000]
        m = re.search(r"^session_key: (\S+)$", head, re.M)
        if not m:
            continue
        aka = re.search(r'^also_known_as: "?([^"\n]*)"?$', head, re.M)
        aliases = [a.strip() for a in (aka.group(1).split(",") if aka else []) if a.strip()]
        _remember(m.group(1), conv.parent, root, aliases=aliases)
        n += 1
    return {"sessions recorded": n}


# ---------------------------------------------------------------- CLI

def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _done(counts):
    if counts.get("error"):
        print("FAILED: " + counts["error"])
        return 1
    print("done: " + (", ".join(f"{n} {k}" for k, n in counts.items() if n) or "nothing to do"))
    return 0


def _say(line):
    print(line, flush=True)


def _roots(spec):
    """Folders from a comma-separated list; each may be a glob pattern (the
    old Mac's Cowork sessions each keep their own projects folder)."""
    import glob as _glob
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        hits = sorted(_glob.glob(part)) if any(ch in part for ch in "*?[") else [part]
        out += [Path(h) for h in hits if Path(h).is_dir()]
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if _opt(argv, "--vault"):
        _override["root"] = _opt(argv, "--vault")
    if cmd == "sync":
        limit = _opt(argv, "--limit")
        only = _opt(argv, "--only")
        return _done(sync(limit=int(limit) if limit else None, say=_say,
                          only=set(only.split(",")) if only else None))
    if cmd == "import":
        # Another machine's or a cloud session's logs, shaped like this Mac's.
        origin = _opt(argv, "--origin", "imported")
        skip = re.compile(_opt(argv, "--skip")) if _opt(argv, "--skip") else None
        return _done(sync(origin=origin, say=_say, claude=_roots(_opt(argv, "--claude")),
                          codex=_roots(_opt(argv, "--codex")), jobs=False,
                          app_root=Path(_opt(argv, "--app")) if _opt(argv, "--app") else None,
                          skip=(lambda p: bool(skip.search(str(p)))) if skip else None))
    if cmd == "adopt":
        src = _opt(argv, "--from")
        if not src:
            print("FAILED: adopt needs --from <flat retro folder>")
            return 1
        res = adopt(Path(src).expanduser(), dry="--dry-run" in argv)
        for a_, b_ in res["moves"] if "--list" in argv else []:
            print(f"{a_} -> {b_}")
        return _done(res["counts"])
    if cmd == "export-script":
        print(EXPORT_SCRIPT.strip())
        return 0
    if cmd == "import-cloud":
        src = _opt(argv, "--from")
        if not src:
            print("FAILED: import-cloud needs --from <folder holding claude-ai-sessions-*.jsonl>")
            return 1
        counts = import_cloud(Path(src).expanduser())
        print("unpacked: " + json.dumps(counts))
        return _done(sync(say=_say))
    if cmd == "check":
        print(json.dumps(check(), indent=1))
        return 0
    if cmd == "rebuild-store":
        return _done(rebuild_store())
    st = _read_store()
    print(json.dumps({"vault": str(vault_root() or ""),
                      "sessions": len(st.get("sessions") or {}),
                      "by_tool": dict(Counter(r.get("tool") for r in (st.get("sessions") or {}).values())),
                      "by_origin": dict(Counter(r.get("origin") for r in (st.get("sessions") or {}).values()))},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Fill the history store from every store that holds history today.

One reader per source, each returning events (server/history.py shapes).
`collect` runs them all, each in its own try, so a broken source is a named
error in the report and the rest still land. `history.put` appends only the
events whose content changed, so running a pass twice writes nothing new.

Sources (every one read-only; nothing here writes a byte to them):

    vira_jobs        data/jobs-log.json and data/jobs/<id>/ (state, events,
                     control): sessions, asks, answers, landing and card
                     decisions, tokens and active time
    claude_app       the Claude desktop app's session records, and each
                     one's transcript (tokens, cost, active time, files)
    claude_cli       terminal Claude Code transcripts with no app record
    codex            Codex's thread database
    git              every repository a session worked in: each merge into
                     the base branch, each commit, each branch left not merged
                     or discarded; pull requests from gh
    branch_state     data/orphan-work.json and data/showroom.json: uncommitted
                     changes sitting in a worktree
    flows            data/circuit-runs.json and the runs captured before the
                     store evicted them (data/circuit-runs-archive.jsonl):
                     each run, each step, each approval
    programs         data/routine-runs.json: each Program run
    ideas            data/ideas.json: each idea, and the owner's approve,
                     defer and drop decisions (from status changes and the
                     notes Vira stamps)
    journal          data/brief-journal.json (+ archive): each note told to Vira
    chat             data/vira-chat.json: each question and answer
    notify           data/notify-log.json (+ archive): each text Vira sent,
                     linked to the work it names
    review           data/review-decided.json: Review choices
    branch_actions   data/branch-actions.jsonl: Merge and Discard pressed on
                     a branch (orphanwork writes it)
    contacts         data/contacts-sync.json: each sync run and each phone
                     card Vira created
    documents        data/reading-list.json (not retros) and the retro files
                     in ~/TC-IL/Sessions, each linked to its session
    inflow           captured vault notes, linked to the run that captured them

Incremental state (data/history/ingest-state.json): per-transcript byte
offsets and running totals, transcript heads, git watermarks per repository,
when gh last answered. A transcript is read once; a later pass reads only
what was appended.

CLI (out of process, never inside the server's GIL):

    python -m server.historyingest backfill   every source, everything on disk
    python -m server.historyingest tick       the same, incremental (the
                                              server runs this every minute)
    python -m server.historyingest status     counts per kind and source
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import history as H
from . import jsonstore

ROOT = H.ROOT
DATA = ROOT / "data"

# A session whose transcript or record moved this recently is running.
LIVE_S = 10 * 60
# A session that ended its turn asking the owner something stays "Waiting on
# you" this long; past it, what it left decides its word.
WAIT_KEEP_S = 2 * 24 * 3600
# Gaps between transcript lines longer than this are idle time, not work.
IDLE_GAP_S = 5 * 60
# git is re-read at most this often in a tick; gh (network) less often.
GIT_SECS = 120
GH_SECS = 600
# Files kept per session in links.files: the module comes from all of them,
# the list is for the detail panel.
FILES_KEEP = 300
BASE_NAMES = ("main", "master", "trunk", "develop")


def state_path():
    return H.store_dir() / "ingest-state.json"


def _blank_state():
    return {"version": 1, "transcripts": {}, "heads": {}, "git": {}, "gh": {},
            "ran": {}}


class Ctx:
    """What one pass shares between readers: the stored events, the
    incremental state, caches, and the errors and counts it reports."""

    def __init__(self, full=False, now=None):
        self.full = full
        self.now = now or time.time()
        self.current = H.load()
        self.state = jsonstore.read(state_path(), _blank_state())
        for k, v in _blank_state().items():
            self.state.setdefault(k, v if not isinstance(v, dict) else {})
        self.errors = {}
        self.counts = {}
        self.folders = set()       # every folder a session worked in
        self.pr_titles = {}        # "project#n" -> title
        self.pr_rows = {}          # "project#n" -> gh row
        self.ideas = {}            # idea id -> record
        self.program_flow = {}     # program run id -> flow run id
        self.flow_counts = {}      # flow run id -> merged counts
        self.vira_sessions = {}    # engine session id -> Vira job id
        self.collected = []        # events read so far in this pass
        _preload_prs(self)
        _preload_ideas(self)

    def due(self, name, every):
        last = float((self.state.get("ran") or {}).get(name) or 0)
        return self.full or self.now - last >= every

    def ran(self, name):
        self.state.setdefault("ran", {})[name] = self.now


# ---------------------------------------------------------------- helpers

def _preload_ideas(ctx):
    """Idea texts for the titles of sessions read before the ideas are."""
    from . import ideas as ideastore
    for it in (_read_json(ideastore.STORE, {"items": []}) or {}).get("items") or []:
        if isinstance(it, dict) and it.get("id"):
            ctx.ideas[it["id"]] = it


def _preload_prs(ctx):
    """Pull request titles known before this pass reads git: the last gh
    answer per repository, and Vira's own PR index."""
    for repo, cur in (ctx.state.get("gh") or {}).items():
        project = Path(repo).name
        for r in (cur or {}).get("rows") or []:
            k = _pr_key(project, r.get("number"))
            if k:
                ctx.pr_titles[k] = r.get("title") or ""
                ctx.pr_rows[k] = r
    try:
        from . import prindex
        vira = Path(H.vira_repo()).name
        for branch, p in (prindex._read() or {}).items():
            k = _pr_key(vira, (p or {}).get("number"))
            if k and k not in ctx.pr_titles:
                ctx.pr_titles[k] = p.get("title") or ""
                ctx.pr_rows[k] = {"number": p.get("number"), "title": p.get("title"),
                                  "state": p.get("state"), "headRefName": branch,
                                  "url": p.get("url")}
    except Exception:  # noqa: BLE001 - no index is no titles, not a failed pass
        pass


def _read_json(path, default):
    return jsonstore.read(path, default)


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


def _pr_key(project, number):
    try:
        return f"{project}#{int(number)}" if project and number else ""
    except (TypeError, ValueError):
        return ""


def _branch_key(project, branch):
    b = str(branch or "").removeprefix("origin/").removeprefix("refs/heads/")
    if not b or b in BASE_NAMES or b == "HEAD":
        return ""
    return f"{project}:{b}"


def _repo_name_of(slug):
    """"MattaDurham/vira" -> "vira"."""
    return str(slug or "").rsplit("/", 1)[-1]


def _rel_files(files, repo):
    """Paths inside the repository (or one of its worktrees) made relative
    to it, so a file is one name whichever checkout touched it; paths
    elsewhere stay absolute."""
    out = []
    for f in files or ():
        r = H.rel_path(f, repo) if repo else ""
        p = r or str(f)
        if p not in out:
            out.append(p)
    return out


def _place(folder):
    project, repo = H.project_of(folder)
    if folder:
        return project, repo, str(folder)
    return project, repo, ""


# ---------------------------------------------------------------- transcripts

_TS = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')
_WRITE_TOOLS = frozenset(("Edit", "Write", "MultiEdit", "NotebookEdit"))


class TStats:
    """Running totals of one Claude transcript, resumable from a byte
    offset: first and last time, active time, tokens per model (each API
    message counted once, by its message id), the first human prompt, the
    files its write tools touched, its branch and folder."""

    FIELDS = ("offset", "size", "mtime_ns", "first", "last", "active", "prev",
              "usage", "pending", "prompt", "files", "branch", "cwd", "entry",
              "prompts", "session")

    def __init__(self, d=None):
        d = d or {}
        self.offset = int(d.get("offset") or 0)
        self.size = int(d.get("size") or 0)
        self.mtime_ns = int(d.get("mtime_ns") or 0)
        self.first = float(d.get("first") or 0)
        self.last = float(d.get("last") or 0)
        self.active = float(d.get("active") or 0)
        self.prev = float(d.get("prev") or 0)
        self.usage = d.get("usage") or {}
        self.pending = d.get("pending") or None    # [msg id, model, usage]
        self.prompt = d.get("prompt") or ""
        self.files = list(d.get("files") or [])
        self.branch = d.get("branch") or ""
        self.cwd = d.get("cwd") or ""
        self.entry = d.get("entry") or ""
        self.prompts = int(d.get("prompts") or 0)
        self.session = d.get("session") or ""

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    def totals(self):
        """Committed usage plus the message still being written."""
        out = json.loads(json.dumps(self.usage))
        if self.pending:
            H.add_usage(out, self.pending[1], self.pending[2])
        return out

    def _tick(self, ts):
        if not ts:
            return
        if not self.first or ts < self.first:
            self.first = ts if not self.first else min(self.first, ts)
        if self.prev and 0 < ts - self.prev <= IDLE_GAP_S:
            self.active += ts - self.prev
        if ts >= self.prev:
            self.prev = ts
        self.last = max(self.last, ts)

    def _line(self, raw):
        m = _TS.search(raw)
        ts = H.to_ts(m.group(1).decode("utf-8", "replace")) if m else 0.0
        self._tick(ts)
        interesting = (b'"usage"' in raw or (not self.prompt and b'"user"' in raw)
                       or b'"tool_use"' in raw or not self.cwd or not self.entry
                       or (b'"gitBranch"' in raw and not self.branch))
        if not interesting:
            return
        try:
            r = json.loads(raw)
        except ValueError:
            return
        if not isinstance(r, dict):
            return
        self.cwd = self.cwd or r.get("cwd") or ""
        self.entry = self.entry or r.get("entrypoint") or ""
        self.session = self.session or r.get("sessionId") or ""
        if r.get("gitBranch") and r.get("gitBranch") != "HEAD":
            self.branch = r["gitBranch"]
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        t = r.get("type")
        if t == "user" and not r.get("isSidechain") and not r.get("isMeta"):
            from . import fleetclaude
            txt = fleetclaude.user_text(msg.get("content"))
            origin = (r.get("origin") or {}).get("kind", "human") if isinstance(r.get("origin"), dict) else "human"
            if txt and origin == "human" and r.get("promptSource") != "system" \
                    and not txt.startswith(("Caveat:", "[Request interrupted", "<")):
                self.prompts += 1
                if not self.prompt:
                    self.prompt = txt[:2000]
        if t == "assistant":
            mid, model = msg.get("id"), msg.get("model")
            usage = msg.get("usage")
            if isinstance(usage, dict) and mid and not str(model or "").startswith("<"):
                if self.pending and self.pending[0] != mid:
                    H.add_usage(self.usage, self.pending[1], self.pending[2])
                self.pending = [mid, model, usage]
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and b.get("name") in _WRITE_TOOLS:
                    fp = (b.get("input") or {}).get("file_path") \
                        if isinstance(b.get("input"), dict) else None
                    if fp and fp not in self.files and len(self.files) < FILES_KEEP:
                        self.files.append(fp)

    def scan(self, path):
        """Read what was appended since the last scan. Returns True when
        anything new was read."""
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_size == self.size and st.st_mtime_ns == self.mtime_ns:
            return False
        if st.st_size < self.offset:            # rewritten: start over
            self.__init__()
        try:
            with open(path, "rb") as fh:
                fh.seek(self.offset)
                pos = self.offset
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break                   # a line still being written
                    pos += len(raw)
                    if raw.strip():
                        self._line(raw)
                self.offset = pos
        except OSError:
            return False
        self.size, self.mtime_ns = st.st_size, st.st_mtime_ns
        return True


def _tstats(ctx, path):
    """The (possibly resumed) running totals of one transcript."""
    if not path:
        return None
    key = str(path)
    s = TStats(ctx.state["transcripts"].get(key))
    if s.scan(key):
        ctx.state["transcripts"][key] = s.to_dict()
    elif key not in ctx.state["transcripts"] and not os.path.exists(key):
        return None
    return s


def _subagent_usage(ctx, tpath, sid):
    """Tokens spent by a session's subagents, per model."""
    out, n = {}, 0
    if not tpath or not sid:
        return out, n
    sdir = Path(tpath).parent / sid / "subagents"
    if not sdir.is_dir():
        return out, n
    for f in sorted(sdir.glob("agent-*.jsonl")):
        n += 1
        s = _tstats(ctx, f)
        if s:
            for model, t in s.totals().items():
                row = out.setdefault(model, {k: 0 for k in t})
                for k, v in t.items():
                    row[k] = row.get(k, 0) + v
    return out, n


def _merge_usage(*totals):
    out = {}
    for t in totals:
        for model, row in (t or {}).items():
            cur = out.setdefault(model, {})
            for k, v in row.items():
                cur[k] = cur.get(k, 0) + int(v or 0)
    return out


def _transcript_index(ctx):
    """{session id: transcript path} for every main transcript under
    Claude Code's projects folder, plus each file's head (entrypoint,
    folder, first prompt), read once per file and kept in the state."""
    from . import fleetclaude as fc
    root = fc.projects_dir()
    idx, heads = {}, ctx.state.setdefault("heads", {})
    seen = set()
    try:
        folders = list(os.scandir(root))
    except OSError:
        return idx
    for d in folders:
        if not d.is_dir():
            continue
        try:
            files = list(os.scandir(d.path))
        except OSError:
            continue
        for f in files:
            if not f.name.endswith(".jsonl"):
                continue
            stem = f.name[:-6]
            seen.add(f.path)
            prev = idx.get(stem)
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if not prev or mt > os.path.getmtime(prev):
                idx[stem] = f.path
            h = heads.get(f.path)
            if not h or (not h.get("entrypoint") and h.get("mtime") != mt):
                head = fc.read_head(Path(f.path))
                heads[f.path] = {"mtime": mt, "entrypoint": head.get("entrypoint"),
                                 "sessionId": head.get("sessionId"), "cwd": head.get("cwd"),
                                 "at": head.get("at"), "branch": head.get("branch")}
    for k in [k for k in heads if k not in seen]:
        del heads[k]
    return idx


# ---------------------------------------------------------------- Vira jobs

# Job kinds whose output is code on a branch (workresults.CODE_KINDS).
CODE_KINDS = ("Implement", "Plan", "Resume", "Merge", "Diagnose and merge", "Land",
              "Diagnose and land")
_VERDICT_LABEL = {"merge": "Merge it", "test": "Try it first", "keep": "Keep testing",
                  "discard": "Discard"}
_VERDICT_STATUS = {"merge": H.MERGED, "discard": H.DROPPED, "test": H.ANSWERED,
                   "keep": H.ANSWERED}


def _job_usage(jdir):
    """(per-model totals, active seconds, [turn spans], [waits]) from a job
    folder's events.jsonl and state.json."""
    totals, active, turns, waits = {}, 0.0, [], []
    state = _read_json(jdir / "state.json", {}) or {}
    model = state.get("model_used") or ""
    open_turns = {}
    for ev in _read_jsonl(jdir / "events.jsonl"):
        kind, t = ev.get("event"), float(ev.get("t") or 0)
        if kind == "turn_started":
            open_turns[ev.get("turn_id")] = t
        elif kind == "turn_completed":
            st = open_turns.pop(ev.get("turn_id"), None)
            if st and t >= st:
                active += t - st
                turns.append([st, t])
        elif kind == "usage":
            H.add_usage(totals, model or "unknown", ev.get("usage"))
        elif kind == "awaiting_input":
            waits.append({"at": t, "kind": ev.get("kind") or "question",
                          "req_id": ev.get("request_id")})
    return totals, active, turns, waits, state


def _job_title(rec, ctx, pr_title=""):
    from . import joblog
    idea = ctx.ideas.get(rec.get("idea_id") or "") or {}
    automation = ""
    if joblog.kind_label(rec) == "Routine":
        meta = rec.get("meta") or {}
        automation = meta.get("routine_name") or rec.get("subject") or ""
    try:
        ask = joblog.subject(rec, idea.get("text"))
    except Exception:  # noqa: BLE001 - a label must never hide a row
        ask = rec.get("subject") or rec.get("prompt") or ""
    app_title = rec.get("title") if rec.get("title_edited") else ""
    return H.pick_title(pr_title, idea.get("text") or "", automation, app_title, ask)


def from_vira_jobs(ctx):
    from . import joblog, prindex
    try:
        records = joblog.list_records()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"jobs-log.json: {e}") from e
    try:
        prs = prindex._read()
    except Exception:  # noqa: BLE001
        prs = {}
    jobs_dir = DATA / "jobs"
    out = []
    for rec in records:
        jid = rec.get("id")
        if not jid:
            continue
        kind = joblog.kind_label(rec)
        project, repo, folder = _place(rec.get("worktree") or rec.get("cwd"))
        if folder:
            ctx.folders.add(folder)
        branch = rec.get("branch") or ""
        pr = rec.get("pr") if isinstance(rec.get("pr"), dict) else {}
        pr_row = prs.get(branch) or {}
        pr_n = pr.get("number") or pr_row.get("number")
        pr_title = pr_row.get("title") or pr.get("title") or ""
        jdir = jobs_dir / jid
        totals, active, turns, waits, state = _job_usage(jdir)
        tstats = None
        if rec.get("transcript"):
            tstats = _tstats(ctx, rec["transcript"])
            if not totals and tstats:
                totals = tstats.totals()
        files = _rel_files(tstats.files if tstats else [], repo)
        modules, module = H.modules_of(files, repo) if files else ({}, "")
        cost, tokens = H.cost_of(totals)
        st = rec.get("status")
        if st == "running":
            status = H.WAITING if (state.get("awaiting") or state.get("pending")) else H.RUNNING
        elif st in ("error", "orphaned"):
            status = H.FAILED
        elif branch:
            status = H.NOT_MERGED
        elif kind == "Routine" or (rec.get("meta") or {}).get("circuit_run"):
            status = H.RAN
        else:
            status = H.ANSWERED
        meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
        links = {"job": jid, "session": rec.get("session_id"), "idea": rec.get("idea_id"),
                 "branch": _branch_key(project, branch),
                 "pr": _pr_key(project, pr_n), "flow_run": meta.get("circuit_run"),
                 "schedule": meta.get("routine") or meta.get("routine_id"),
                 "parent": meta.get("parent_job") or meta.get("job_id"),
                 "files": files[:FILES_KEEP]}
        cards = []
        for c in state.get("cards") or []:
            if isinstance(c, dict):
                cards.append({"req_id": c.get("req_id"), "kind": c.get("kind"),
                              "at": H.iso(c.get("t")), "summary": H.clean_title(c.get("summary") or "", 200)})
        title = _job_title(rec, ctx, pr_title)
        if rec.get("session_id"):
            ctx.vira_sessions[rec["session_id"]] = jid
        out.append(H.event(
            f"session:vira:{jid}", "session", rec.get("started"), sub="vira",
            end=rec.get("finished") or "", status=status, title=title,
            project=project, folder=folder, repo=repo or "", module=module, modules=modules,
            links=links, source="jobs-log",
            data={"app": "Vira", "kind_label": kind, "model": state.get("model_used")
                  or rec.get("model_used") or rec.get("model") or "",
                  "provider": rec.get("provider") or "", "mode": rec.get("mode") or "",
                  "read_only": bool(rec.get("read_only")), "job_status": st or "",
                  "code": kind in CODE_KINDS and not rec.get("read_only"),
                  "tokens": tokens, "usage": totals, "cost_usd": cost,
                  "cost_basis": H.COST_BASIS if cost is not None else "",
                  "active_s": round(active or (tstats.active if tstats else 0), 1),
                  "turns": len(turns), "waits": [dict(w, at=H.iso(w["at"])) for w in waits],
                  "cards": cards, "transcript": rec.get("transcript") or "",
                  "result": (rec.get("result") or "")[:600],
                  "pr_url": pr.get("url") or pr_row.get("url") or "",
                  "text": str(rec.get("subject") or "")[:600],
                  "worktree": rec.get("worktree") or "", "landing": state.get("landing") or ""}))
        # The ask, when the owner typed it to this session directly.
        direct = (not rec.get("idea_id") and not meta.get("circuit_run")
                  and not meta.get("routine") and not joblog.is_continuation(rec)
                  and kind not in ("Judge", "Routine", "Journal", "Text reply",
                                   "Score roles", "Rescore roles", "Library names",
                                   "Room refresh", "System map", "Map", "Profile explore"))
        if direct:
            sub = "ask" if kind in ("Ask", "Chat") else "dispatch"
            out.append(H.event(
                f"ask:job:{jid}", "ask", rec.get("started"), sub=sub, status=H.ANSWERED,
                title=title, project=project, folder=folder, repo=repo or "",
                links={"job": jid, "session": rec.get("session_id")}, source="jobs-log",
                data={"text": (rec.get("subject") or rec.get("command") or "")[:600]}))
        if kind in ("Ask", "Chat") and st == "done" and rec.get("result"):
            out.append(H.event(
                f"answer:job:{jid}", "answer", rec.get("finished") or rec.get("started"),
                sub="session", status=H.ANSWERED, title=title, project=project,
                folder=folder, repo=repo or "", links={"job": jid}, source="jobs-log",
                data={"text": rec["result"][:600]}))
        out.extend(_job_decisions(rec, jdir, state, title, project, folder, repo, branch))
    return out


def _job_decisions(rec, jdir, state, title, project, folder, repo, branch):
    """The owner's answers to the cards this session raised: landing
    verdicts and answered questions. The control file keeps no time, so an
    answer is dated when the session resumed after it (the first event
    after the card), and a landing verdict when the session ended."""
    jid = rec["id"]
    cards = {c.get("req_id"): c for c in state.get("cards") or [] if isinstance(c, dict)}
    times = sorted(float(e.get("t") or 0) for e in _read_jsonl(jdir / "events.jsonl"))
    out = []
    for i, line in enumerate(_read_jsonl(jdir / "control.jsonl")):
        op = line.get("op")
        if op not in ("landing", "answer", "permission"):
            continue
        req = line.get("req_id") or f"n{i}"
        card = cards.get(req) or {}
        offered = float(card.get("t") or 0)
        if op == "landing":
            verdict = line.get("verdict") or "keep"
            at = rec.get("finished") if verdict in ("merge", "discard") else 0
            if not at:
                at = next((t for t in times if t > offered), offered) if offered else rec.get("finished")
            label, status = _VERDICT_LABEL.get(verdict, verdict), _VERDICT_STATUS.get(verdict, H.ANSWERED)
            sub, data = "landing", {"verdict": verdict, "offered": H.iso(offered)}
        elif op == "answer":
            at = next((t for t in times if t > offered), offered) if offered else rec.get("finished")
            label, status, sub = "Answered", H.ANSWERED, "answer"
            data = {"answer": str(line.get("answer") or "")[:400], "offered": H.iso(offered)}
        else:
            at = next((t for t in times if t > offered), offered) if offered else rec.get("finished")
            label = "Allowed" if line.get("allow") else "Denied"
            status, sub = H.ANSWERED, "permission"
            data = {"allow": bool(line.get("allow")), "scope": line.get("scope") or ""}
        out.append(H.event(
            f"decide:job:{jid}:{req}", "decide", at, sub=sub, status=status,
            title=H.clean_title(f"{label}: {title}"), project=project, folder=folder,
            repo=repo or "", links={"job": jid, "session": rec.get("session_id"),
                                    "branch": _branch_key(project, branch)},
            source="jobs/control", data=data))
    return out


# ---------------------------------------------------------------- Claude

def _claude_status(last_ts, now, summary_cat, has_code):
    if last_ts and now - last_ts < LIVE_S:
        return H.WAITING if summary_cat in ("blocked", "review_ready") else H.RUNNING
    if summary_cat in ("blocked", "review_ready") and last_ts and now - last_ts < WAIT_KEEP_S:
        return H.WAITING
    return H.NOT_MERGED if has_code else H.ANSWERED


def from_claude_app(ctx):
    from . import fleetclaude as fc
    root = fc.sessions_root()
    if not root.is_dir():
        ctx.counts["claude_app_note"] = "Claude desktop app not found on this machine"
        return []
    tindex = ctx.tindex if hasattr(ctx, "tindex") else _transcript_index(ctx)
    ctx.tindex = tindex
    out, cli_ids = [], set()
    for f in root.glob("*/*/local_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not d.get("sessionId"):
            continue
        sid, cli = d["sessionId"], d.get("cliSessionId") or ""
        cli_ids.add(cli)
        origin = d.get("originCwd") or d.get("cwd") or ""
        project, repo = H.project_of(origin)
        anchored = fc.repo_of_record(d)
        if anchored and project != "No folder":
            repo = os.path.realpath(anchored)
            project = Path(repo).name
        folder = d.get("cwd") or origin
        if folder:
            ctx.folders.add(folder)
        tpath = tindex.get(cli) if cli else None
        ts = _tstats(ctx, tpath) if tpath else None
        sub_usage, n_agents = _subagent_usage(ctx, tpath, cli)
        totals = _merge_usage(ts.totals() if ts else {}, sub_usage)
        cost, tokens = H.cost_of(totals)
        prs = fc.prs_of_record(d, d.get("sourceBranch") or "main")
        pr_keys = [_pr_key(_repo_name_of(p.get("repo")) or project, p.get("n")) for p in prs]
        files = _rel_files(ts.files if ts else [], repo)
        written = fc.written_of_record(d)
        modules, module = H.modules_of(files, repo) if files else ({}, "")
        branch = d.get("branch") or (ts.branch if ts else "")
        if branch in BASE_NAMES:
            branch = ""
        last = max(filter(None, [H.to_ts(d.get("lastActivityAt")), ts.last if ts else 0]), default=0)
        start = H.to_ts(d.get("createdAt")) or (ts.first if ts else 0)
        summary = d.get("postTurnSummary") if isinstance(d.get("postTurnSummary"), dict) else {}
        status = _claude_status(last, ctx.now, summary.get("status_category"),
                                bool(branch or prs))
        pr_title = ""
        for p in prs:
            pr_title = pr_title or ctx.pr_titles.get(_pr_key(_repo_name_of(p.get("repo")) or project, p.get("n"))) or ""
        title = H.pick_title(pr_title, "", "", d.get("title") or "", ts.prompt if ts else "")
        parent, link = fc.link_of_record(d)
        out.append(H.event(
            f"session:claude:{sid}", "session", start, sub="claude", end=last,
            status=status, title=title, project=project, folder=folder, repo=repo or "",
            module=module, modules=modules, source="claude-app",
            links={"session": [sid, cli], "branch": _branch_key(project, branch),
                   "pr": pr_keys, "parent": parent, "files": files[:FILES_KEEP],
                   "branches_written": [_branch_key(project, b) for b in written]},
            data={"app": "Claude Code", "surface": "app", "model": d.get("model") or "",
                  "effort": d.get("effort") or "", "mode": d.get("permissionMode") or "",
                  "tokens": tokens, "usage": totals, "cost_usd": cost,
                  "cost_basis": H.COST_BASIS if cost is not None else "",
                  "active_s": round(ts.active, 1) if ts else 0, "turns": d.get("completedTurns") or 0,
                  "subagents": n_agents, "archived": bool(d.get("isArchived")),
                  "summary": summary.get("status_category") or "",
                  "needs": H.clean_title(summary.get("needs_action") or "", 200),
                  "link_kind": link or "", "transcript": str(tpath or ""),
                  "open_url": f"claude://claude.ai/epitaxy/{sid}",
                  "title_source": d.get("titleSource") or "",
                  "text": (ts.prompt if ts else "")[:600]}))
    ctx.claude_app_ids = cli_ids
    return out


def from_claude_cli(ctx):
    """Terminal sessions: a transcript whose entrypoint is `cli` and that
    has no app record. Headless SDK calls are not sessions and are left out,
    as Fleet leaves them out."""
    from . import fleetclaude as fc
    tindex = ctx.tindex if hasattr(ctx, "tindex") else _transcript_index(ctx)
    ctx.tindex = tindex
    known = getattr(ctx, "claude_app_ids", set())
    vira_sids = set(ctx.vira_sessions)
    out = []
    for path, h in (ctx.state.get("heads") or {}).items():
        if h.get("entrypoint") != fc.CLI_ENTRY:
            continue
        sid = h.get("sessionId") or Path(path).stem
        if sid in known or sid in vira_sids:
            continue
        ts = _tstats(ctx, path)
        if not ts:
            continue
        folder = ts.cwd or h.get("cwd") or ""
        if folder:
            ctx.folders.add(folder)
        project, repo = H.project_of(folder)
        sub_usage, n_agents = _subagent_usage(ctx, path, sid)
        totals = _merge_usage(ts.totals(), sub_usage)
        cost, tokens = H.cost_of(totals)
        files = _rel_files(ts.files, repo)
        modules, module = H.modules_of(files, repo) if files else ({}, "")
        branch = ts.branch if ts.branch not in BASE_NAMES else ""
        status = _claude_status(ts.last, ctx.now, None, bool(branch))
        out.append(H.event(
            f"session:claude:{sid}", "session", ts.first or h.get("at"), sub="claude",
            end=ts.last, status=status, title=H.pick_title(ask=ts.prompt) or "Terminal session",
            project=project, folder=folder, repo=repo or "", module=module, modules=modules,
            source="claude-cli",
            links={"session": sid, "branch": _branch_key(project, branch), "files": files},
            data={"app": "Claude Code", "surface": "CLI", "model": "", "tokens": tokens,
                  "usage": totals, "cost_usd": cost,
                  "cost_basis": H.COST_BASIS if cost is not None else "",
                  "active_s": round(ts.active, 1), "turns": ts.prompts,
                  "subagents": n_agents, "transcript": path, "text": ts.prompt[:600]}))
    return out


# ---------------------------------------------------------------- Codex

def from_codex(ctx):
    from . import fleetcodex as fx
    threads, note = fx.read_threads(0)
    if note:
        ctx.counts["codex_note"] = note
        return []
    names = fx.thread_names()
    vira_sids = ctx.vira_sessions
    children = {}
    for th in threads:
        parent = fx._parent_of_source(th.get("source"))
        if parent:
            children.setdefault(parent, []).append(th)
    out = []
    for th in threads:
        if fx._parent_of_source(th.get("source")):
            continue
        tid = th.get("id")
        if not tid or tid in vira_sids:
            continue
        folder = th.get("cwd") or ""
        if folder:
            ctx.folders.add(folder)
        project, repo = H.project_of(folder)
        created = fx.created_ms(th)
        last = th.get("updated_at_ms") or th.get("recency_at_ms") or \
            ((th.get("updated_at") or 0) * 1000) or created
        branch = th.get("git_branch") or ""
        if branch in BASE_NAMES:
            branch = ""
        last_ts = H.to_ts(last)
        status = _claude_status(last_ts, ctx.now, None, bool(branch))
        tokens = int(th.get("tokens_used") or 0) + sum(
            int(c.get("tokens_used") or 0) for c in children.get(tid, []))
        title = H.pick_title(app_title=names.get(tid) or th.get("name") or th.get("title") or "",
                             ask=th.get("first_user_message") or th.get("preview") or "")
        out.append(H.event(
            f"session:codex:{tid}", "session", created, sub="codex", end=last,
            status=status, title=title or "Codex session", project=project, folder=folder,
            repo=repo or "", source="codex",
            links={"session": tid, "branch": _branch_key(project, branch)},
            data={"app": "Codex", "surface": fx.surface_of(th.get("source")),
                  "model": th.get("model") or "", "effort": th.get("reasoning_effort") or "",
                  "tokens": tokens, "cost_usd": None, "cost_basis": "",
                  "active_s": 0, "subagents": len(children.get(tid, [])),
                  "archived": bool(th.get("archived")), "open_url": fx.open_url(tid),
                  "transcript": th.get("rollout_path") or "",
                  "text": str(th.get("first_user_message") or "")[:600]}))
    return out


# ---------------------------------------------------------------- git

def _git(repo, *args, timeout=60):
    from . import fleetgit
    return fleetgit.git(repo, *args, timeout=timeout)


def _base_of(repo):
    rc, out = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        return out.strip().removeprefix("origin/")
    rc, out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    heads = set(out.split()) if rc == 0 else set()
    for b in BASE_NAMES:
        if b in heads:
            return b
    return ""


_LOG_FMT = "--format=%x1e%H%x1f%P%x1f%ct%x1f%s"


def _parse_log(text):
    """[{sha, parents, at, subject, files: [[path, added, deleted]]}] from
    `git log _LOG_FMT --numstat`."""
    rows = []
    for chunk in text.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, rest = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        files = []
        for line in rest.splitlines():
            bits = line.split("\t")
            if len(bits) != 3:
                continue
            a, dl, p = bits
            p = re.sub(r"\{[^{}]*? => ([^{}]*?)\}", r"\1", p).replace("//", "/")
            if " => " in p:
                p = p.split(" => ", 1)[1]
            files.append([p, int(a) if a.isdigit() else 0, int(dl) if dl.isdigit() else 0])
        rows.append({"sha": parts[0], "parents": parts[1].split(), "at": int(parts[2] or 0),
                     "subject": "\x1f".join(parts[3:]), "files": files})
    return rows


def _diff(files):
    return {"files": len(files), "added": sum(f[1] for f in files),
            "removed": sum(f[2] for f in files)}


def _merge_branch(subject):
    from . import changelog
    m = changelog._MERGE_BRANCH.match(subject or "")
    branch, number = None, None
    if m:
        branch = m.group(1) or m.group(3)
        number = int(m.group(2)) if m.group(2) else None
    else:
        sq = changelog._SQUASH_PR.search(subject or "")
        number = int(sq.group(1)) if sq else None
    return (branch or "").removeprefix("origin/"), number


def _commit_event(repo, project, c, status, branch_key="", base=""):
    paths = [f[0] for f in c["files"]]
    modules, module = H.modules_of(paths, repo)
    return H.event(
        f"code:commit:{project}:{c['sha']}", "code", c["at"], sub="commit", status=status,
        title=H.clean_title(c["subject"]) or "Commit", project=project, folder=repo,
        repo=repo, module=module, modules=modules, source="git",
        links={"commit": c["sha"], "branch": branch_key, "files": paths[:FILES_KEEP]},
        data={"diff": _diff(c["files"]), "files_changed": c["files"][:FILES_KEEP],
              "base": base})


def _gh_prs(ctx, repo, project):
    """Pull requests of one repository from gh, at most every GH_SECS; the
    last good answer is kept in the state so a pass without network still
    has titles."""
    from . import fleetgit
    gh_state = ctx.state.setdefault("gh", {})
    cur = gh_state.get(repo) or {}
    if ctx.full or ctx.now - float(cur.get("at") or 0) >= GH_SECS:
        gh = fleetgit.gh_bin()
        if gh:
            rc, out = fleetgit._run([gh, "pr", "list", "--state", "all", "--limit", "1000",
                                     "--json", "number,title,state,headRefName,mergedAt,"
                                     "closedAt,url,createdAt,isDraft"], cwd=repo, timeout=40)
            if rc == 0:
                try:
                    rows = json.loads(out or "[]")
                    cur = {"at": ctx.now, "rows": [r for r in rows if isinstance(r, dict)]}
                    gh_state[repo] = cur
                except ValueError:
                    pass
    rows = cur.get("rows") or []
    for r in rows:
        k = _pr_key(project, r.get("number"))
        if k:
            ctx.pr_titles[k] = r.get("title") or ""
            ctx.pr_rows[k] = r
    return rows


def from_git(ctx):
    """Every repository a session worked in, plus this one."""
    if not ctx.due("git", GIT_SECS):
        return []
    roots = {H.vira_repo()}
    for f in list(ctx.folders):
        r = H.repo_root(f)
        if r:
            roots.add(r)
    for e in ctx.current.values():
        if e.get("repo"):
            roots.add(e["repo"])
    out = []
    for repo in sorted(r for r in roots if r and os.path.isdir(r)):
        try:
            out.extend(_git_repo(ctx, repo))
        except Exception as e:  # noqa: BLE001 - one repository never hides the rest
            ctx.errors[f"git {Path(repo).name}"] = str(e)[:240]
    ctx.ran("git")
    return out


def _git_repo(ctx, repo):
    project = Path(repo).name
    base = _base_of(repo)
    if not base:
        return []
    st = ctx.state["git"].setdefault(repo, {})
    out = []
    # Pull requests first: their titles name the merges.
    rows = _gh_prs(ctx, repo, project)
    for r in rows:
        k = _pr_key(project, r.get("number"))
        state = (r.get("state") or "").upper()
        status = {"MERGED": H.MERGED, "CLOSED": H.DROPPED}.get(state, H.NOT_MERGED)
        out.append(H.event(
            f"code:pr:{k}", "code", r.get("createdAt"), sub="pr_opened", status=status,
            end=r.get("mergedAt") or r.get("closedAt") or "",
            title=H.clean_title(r.get("title")) or f"Pull request {r.get('number')}",
            project=project, folder=repo, repo=repo, source="gh",
            links={"pr": k, "branch": _branch_key(project, r.get("headRefName"))},
            data={"number": r.get("number"), "state": state, "url": r.get("url") or "",
                  "draft": bool(r.get("isDraft")), "merged_at": H.iso(r.get("mergedAt")),
                  "closed_at": H.iso(r.get("closedAt"))}))
    # The base branch: each merge and each direct commit, once.
    rc, tip = _git(repo, "rev-parse", base)
    tip = tip.strip() if rc == 0 else ""
    last = st.get("base_tip") if not ctx.full else None
    if tip and tip != last:
        rng = f"{last}..{base}" if last else base
        rc, text = _git(repo, "log", "--first-parent", "-m", "--numstat", _LOG_FMT, rng,
                        timeout=300)
        if rc != 0 and last:
            rc, text = _git(repo, "log", "--first-parent", "-m", "--numstat", _LOG_FMT, base,
                            timeout=300)
        for c in _parse_log(text if rc == 0 else ""):
            if len(c["parents"]) > 1:
                out.extend(_merge_events(ctx, repo, project, base, c))
            else:
                _, n = _merge_branch(c["subject"])
                ev = _commit_event(repo, project, c, H.MERGED, base=base)
                if n:
                    ev["links"]["pr"] = [_pr_key(project, n)]
                    pr_title = ctx.pr_titles.get(_pr_key(project, n))
                    if pr_title:
                        ev["title"] = H.clean_title(pr_title)
                    ev["sub"] = "merged"
                out.append(ev)
        st["base_tip"] = tip
    # Branches not in the base: each one's commits, and the branch itself.
    out.extend(_branch_events(ctx, repo, project, base))
    return out


def _merge_events(ctx, repo, project, base, c):
    branch, number = _merge_branch(c["subject"])
    out = []
    rc, text = _git(repo, "log", "--no-merges", "--numstat", _LOG_FMT,
                    f"{c['parents'][0]}..{c['parents'][1]}", timeout=120)
    commits = _parse_log(text if rc == 0 else "")
    bkey = _branch_key(project, branch)
    pr_k = _pr_key(project, number)
    if not pr_k and branch:
        for k, r in ctx.pr_rows.items():
            if k.startswith(project + "#") and r.get("headRefName") == branch \
                    and (r.get("state") or "").upper() == "MERGED":
                pr_k = k
                break
    for cm in commits:
        out.append(_commit_event(repo, project, cm, H.MERGED, bkey, base))
    paths = [f[0] for f in c["files"]]
    modules, module = H.modules_of(paths, repo)
    pr_title = ctx.pr_titles.get(pr_k, "")
    fallback = next((cm["subject"] for cm in commits), "") or c["subject"]
    title = H.pick_title(pr_title, "", "", "", "") or H.clean_title(fallback) \
        or "Merge into " + base
    out.append(H.event(
        f"code:merged:{project}:{c['sha']}", "code", c["at"], sub="merged", status=H.MERGED,
        title=title, project=project, folder=repo, repo=repo, module=module,
        modules=modules, source="git",
        links={"commit": [c["sha"]] + [cm["sha"] for cm in commits], "branch": bkey,
               "pr": pr_k, "files": paths[:FILES_KEEP]},
        data={"base": base, "diff": _diff(c["files"]), "files_changed": c["files"][:FILES_KEEP],
              "commits": len(commits), "branch_name": branch, "subject": c["subject"][:200]}))
    return out


def _branch_events(ctx, repo, project, base):
    """Each branch not merged into the base (its commits, Not merged), and
    each branch that was here before and is gone now: Merged when a merge
    or a merged PR names it, else Dropped (discarded)."""
    rc, text = _git(repo, "for-each-ref", "--format=%(refname:short)%1f%(objectname)"
                    "%1f%(committerdate:unix)", "refs/heads")
    heads = {}
    for line in text.splitlines() if rc == 0 else []:
        bits = line.split("\x1f")
        if len(bits) == 3 and bits[0] != base:
            heads[bits[0]] = (bits[1], int(bits[2] or 0))
    rc, merged_text = _git(repo, "branch", "--format=%(refname:short)", "--merged", base)
    merged = set(merged_text.split()) if rc == 0 else set()
    wts = {}
    rc, wt_text = _git(repo, "worktree", "list", "--porcelain")
    cur = None
    for line in wt_text.splitlines() if rc == 0 else []:
        if line.startswith("worktree "):
            cur = line[9:].strip()
        elif line.startswith("branch ") and cur:
            wts[line[7:].strip().removeprefix("refs/heads/")] = cur
    st = ctx.state["git"].setdefault(repo, {})
    tips = st.setdefault("branch_tips", {})
    out = []
    for name, (tip, at) in heads.items():
        bkey = _branch_key(project, name)
        if not bkey:
            continue
        if name in merged:
            # Its commits are in the base; a merge event (or a fast-forward)
            # already carries them. Never committed on: nothing to record.
            continue
        commits = []
        if ctx.full or tips.get(name) != tip or f"code:branch:{bkey}" not in ctx.current:
            rc, ltext = _git(repo, "log", "--no-merges", "--numstat", _LOG_FMT,
                             f"{base}..{name}", timeout=120)
            commits = _parse_log(ltext if rc == 0 else "")
            for cm in commits:
                out.append(_commit_event(repo, project, cm, H.NOT_MERGED, bkey, base))
            tips[name] = tip
            files = {}
            for cm in commits:
                for p, a, dl in cm["files"]:
                    row = files.setdefault(p, [p, 0, 0])
                    row[1] += a
                    row[2] += dl
            paths = list(files)
            modules, module = H.modules_of(paths, repo)
            title = H.clean_title(next((cm["subject"] for cm in commits), "")) \
                or H.clean_title(name.split("/", 1)[-1].replace("-", " "))
            out.append(H.event(
                f"code:branch:{bkey}", "code", at, sub="not_merged", status=H.NOT_MERGED,
                title=title or "Branch", project=project, folder=wts.get(name) or repo,
                repo=repo, module=module, modules=modules, source="git",
                links={"branch": bkey, "commit": [cm["sha"] for cm in commits],
                       "files": paths[:FILES_KEEP]},
                data={"branch_name": name, "tip": tip, "worktree": wts.get(name) or "",
                      "ahead": len(commits), "diff": _diff(list(files.values())),
                      "files_changed": list(files.values())[:FILES_KEEP], "base": base}))
    # Branches seen before that are gone (or merged) now.
    for eid, ev in list(ctx.current.items()):
        if not eid.startswith(f"code:branch:{project}:") or ev.get("repo") != repo \
                or ev.get("sub") != "not_merged":
            continue
        name = (ev.get("data") or {}).get("branch_name") or ""
        if name in heads and name not in merged:
            continue
        bkey = _branch_key(project, name)
        was_merged = name in merged or any(
            e.get("sub") == "merged" and bkey in (e.get("links") or {}).get("branch", [])
            for e in ctx.current.values()) or any(
            (r.get("headRefName") == name and (r.get("state") or "").upper() == "MERGED")
            for k, r in ctx.pr_rows.items() if k.startswith(project + "#"))
        new = json.loads(json.dumps(ev))
        new.pop("logged", None)
        new["sub"] = "merged_branch" if was_merged else "discarded"
        new["status"] = H.MERGED if was_merged else H.DROPPED
        new["end"] = H.iso(ctx.now)
        out.append(new)
        if not was_merged:
            for sha in (ev.get("links") or {}).get("commit", []):
                cev = ctx.current.get(f"code:commit:{project}:{sha}")
                if cev and cev.get("status") == H.NOT_MERGED:
                    c2 = json.loads(json.dumps(cev))
                    c2.pop("logged", None)
                    c2["status"] = H.DROPPED
                    out.append(c2)
        tips.pop(name, None)
    return out


def from_branch_state(ctx):
    """Uncommitted work: a worktree with changes no commit holds yet, as
    the branch sweeper (data/orphan-work.json) and the branch inventory
    (data/showroom.json) last saw it. git knows nothing of these until they
    are committed. A row that is gone later ended one of three ways: its
    branch merged (Merged), it is still on a branch not merged (Not
    merged), or neither (Dropped)."""
    from . import orphanwork, showroom
    vira = Path(H.vira_repo()).name
    seen, out = {}, []
    for it in (_read_json(orphanwork.STORE, {}) or {}).get("items") or []:
        if isinstance(it, dict) and it.get("branch") and int(it.get("dirty") or 0) > 0:
            seen[it["branch"]] = {"dirty": int(it["dirty"]), "worktree": it.get("worktree") or "",
                                  "at": it.get("last_activity_iso") or it.get("last_activity"),
                                  "running": bool(it.get("in_motion")),
                                  "files": it.get("files") or [], "source": "orphan-work"}
    for it in (_read_json(showroom.STORE, {}) or {}).get("items") or []:
        if isinstance(it, dict) and it.get("branch") and int(it.get("dirty") or 0) > 0 \
                and it.get("band") != "landed" and it["branch"] not in seen:
            seen[it["branch"]] = {"dirty": int(it["dirty"]), "worktree": it.get("worktree") or "",
                                  "at": it.get("last_activity"), "running": it.get("band") == "session",
                                  "files": [], "source": "showroom"}
    for branch, s in seen.items():
        bkey = _branch_key(vira, branch)
        files = [f if isinstance(f, str) else (f.get("path") if isinstance(f, dict) else "")
                 for f in s["files"]]
        files = [f for f in files if f]
        modules, module = H.modules_of(files, H.vira_repo()) if files else ({}, "")
        out.append(H.event(
            f"code:worktree:{bkey}", "code", s["at"], sub="worktree",
            status=H.RUNNING if s["running"] else H.NOT_MERGED,
            title=H.clean_title(f"Uncommitted changes: {branch.split('/', 1)[-1].replace('-', ' ')}"),
            project=vira, folder=s["worktree"], repo=H.vira_repo(), module=module, modules=modules,
            source=s["source"], links={"branch": bkey, "files": files[:FILES_KEEP]},
            data={"branch_name": branch, "dirty": s["dirty"], "worktree": s["worktree"]}))
    pool = dict(ctx.current)
    pool.update({e["id"]: e for e in ctx.collected})
    for eid, ev in ctx.current.items():
        if ev.get("sub") != "worktree" or ev.get("status") not in (H.NOT_MERGED, H.RUNNING):
            continue
        name = (ev.get("data") or {}).get("branch_name") or ""
        if name in seen:
            continue
        bkey = _branch_key(vira, name)
        fates = [e for e in pool.values() if e.get("kind") == "code"
                 and bkey in (e.get("links") or {}).get("branch", []) and e["id"] != eid]
        if any(e.get("sub") in ("merged", "merged_branch") for e in fates):
            word = H.MERGED
        elif any(e.get("sub") == "not_merged" and e.get("status") == H.NOT_MERGED for e in fates):
            word = H.NOT_MERGED
        else:
            word = H.DROPPED
        new = json.loads(json.dumps(ev))
        new.pop("logged", None)
        new.update(sub="worktree_cleared", status=word, end=H.iso(ctx.now))
        out.append(new)
    return out


# ---------------------------------------------------------------- Flows

_FLOW_STATUS = {"running": H.RUNNING, "pending": H.RUNNING, "waiting": H.WAITING,
                "done": H.RAN, "ok": H.RAN, "error": H.FAILED, "failed": H.FAILED,
                "canceled": H.DROPPED, "cancelled": H.DROPPED, "skipped": H.DROPPED,
                "interrupted": H.FAILED, "starting": H.RUNNING, "ready": H.RUNNING}


def _flow_runs():
    from . import circuits
    runs = {}
    archive = circuits.RUNS_ARCHIVE()
    for r in _read_jsonl(archive):
        if r.get("id"):
            runs[r["id"]] = r
    try:
        live = circuits._load_runs().get("runs") or []
    except Exception:  # noqa: BLE001
        live = []
    for r in live:
        if isinstance(r, dict) and r.get("id"):
            runs[r["id"]] = r
    return list(runs.values())


def _program_runs():
    from . import routinerun
    try:
        return routinerun._read().get("runs") or []
    except Exception:  # noqa: BLE001
        return []


def _add_counts(into, counts):
    for k, v in (counts or {}).items():
        if isinstance(v, (int, float)):
            into[k] = into.get(k, 0) + v


def from_flows(ctx):
    programs = {r.get("id"): r for r in _program_runs() if r.get("id")}
    out = []
    for run in _flow_runs():
        rid = run["id"]
        project, repo, folder = _place(run.get("cwd"))
        stages_def = {s.get("id"): s for s in run.get("stages_def") or [] if isinstance(s, dict)}
        counts, errors, step_jobs, step_programs = {}, 0, [], []
        for sid, s in (run.get("stages") or {}).items():
            if not isinstance(s, dict):
                continue
            sdef = stages_def.get(sid) or {}
            prun = s.get("command_run")
            if prun:
                ctx.program_flow[prun] = rid
                step_programs.append(prun)
                p = programs.get(prun) or {}
                _add_counts(counts, p.get("counts"))
                errors += int(p.get("error_count") or 0)
            if s.get("job_id"):
                step_jobs.append(s["job_id"])
            step_status = _FLOW_STATUS.get(s.get("status") or "", "")
            name = sdef.get("name") or sid
            pcounts = (programs.get(prun) or {}).get("counts") if prun else None
            out.append(H.event(
                f"run:step:{rid}:{sid}", "run", s.get("started") or run.get("started"),
                sub="step", end=s.get("finished") or "", status=step_status,
                title=H.automation_title(f"{run.get('circuit_name') or 'Flow'}: {name}", pcounts),
                project=project, folder=folder, repo=repo or "", source="circuit-runs",
                links={"flow_run": rid, "flow": run.get("circuit_id"), "job": s.get("job_id"),
                       "program_run": prun, "idea": run.get("idea_id")},
                data={"stage": sid, "name": name, "mode": sdef.get("mode") or "",
                      "model": sdef.get("model") or "", "attempts": s.get("attempts") or 0,
                      "grade": s.get("grade"), "verdict": s.get("verdict"),
                      "result": str(s.get("result_text") or "")[:600]}))
            dec = s.get("decision")
            if isinstance(dec, dict) and sdef.get("mode") == "approval":
                approved = bool(dec.get("approved"))
                out.append(H.event(
                    f"decide:flow:{rid}:{sid}", "decide", dec.get("at") or s.get("started"),
                    sub="approval", status=H.ANSWERED if approved else H.DROPPED,
                    title=H.clean_title(f"{'Approved' if approved else 'Sent back'}: "
                                        f"{run.get('circuit_name') or 'Flow'}, {name}"),
                    project=project, folder=folder, repo=repo or "", source="circuit-runs",
                    links={"flow_run": rid, "flow": run.get("circuit_id")},
                    data={"approved": approved, "note": str(dec.get("note") or "")[:400]}))
        ctx.flow_counts[rid] = counts
        source = str(run.get("source") or "")
        schedule = source.split(":", 1)[1] if source.startswith("routine:") else ""
        status = _FLOW_STATUS.get(run.get("status") or "", H.RAN)
        if any(isinstance(s, dict) and s.get("status") == "waiting"
               for s in (run.get("stages") or {}).values()) and status == H.RUNNING:
            status = H.WAITING
        out.append(H.event(
            f"run:flow:{rid}", "run", run.get("started"), sub="flow",
            end=run.get("finished") or "", status=status,
            title=H.automation_title(run.get("circuit_name") or "Flow", counts, errors),
            project=project, folder=folder, repo=repo or "", source="circuit-runs",
            links={"flow_run": rid, "flow": run.get("circuit_id"), "schedule": schedule,
                   "idea": run.get("idea_id"), "job": step_jobs, "program_run": step_programs},
            data={"name": run.get("circuit_name") or "", "trigger": source,
                  "counts": counts, "errors": errors, "error": str(run.get("error") or "")[:400],
                  "input": str(run.get("input") or "")[:400], "entry": run.get("entry"),
                  "stages": len(run.get("stages") or {})}))
    return out


def from_programs(ctx):
    from . import routinerun
    out = []
    for r in _program_runs():
        pid = r.get("id")
        if not pid:
            continue
        status = {"ok": H.RAN, "failed": H.FAILED, "error": H.FAILED,
                  "interrupted": H.FAILED, "running": H.RUNNING,
                  "starting": H.RUNNING}.get(r.get("status") or "", H.FAILED if r.get("error_count") else H.RAN)
        logs = [str(routinerun.RUNS / pid / "output.log")] + [
            (x.get("path") if isinstance(x, dict) else str(x)) for x in r.get("logs") or []]
        flow_run = ctx.program_flow.get(pid, "")
        specs = ctx.state.setdefault("program_cwd", {})
        if pid not in specs:
            spec = _read_json(routinerun.RUNS / pid / "spec.json", {}) or {}
            specs[pid] = spec.get("cwd") or ""
        project, repo, folder = _place(specs[pid]) if specs[pid] else ("", None, "")
        out.append(H.event(
            f"run:program:{pid}", "run", r.get("started") or r.get("queued"), sub="program",
            project=project, folder=folder, repo=repo or "",
            end=r.get("finished") or "", status=status,
            title=H.automation_title(r.get("routine") or r.get("routine_id") or "Program",
                                     r.get("counts"), r.get("error_count") or 0),
            source="routine-runs",
            links={"program_run": pid, "program": r.get("routine_id"), "flow_run": flow_run},
            data={"name": r.get("routine") or "", "trigger": r.get("trigger") or "",
                  "counts": r.get("counts") or {}, "errors": r.get("error_count") or 0,
                  "error_lines": [str(x)[:300] for x in (r.get("errors") or [])[:5]],
                  "exit_code": r.get("exit_code"), "logs": [x for x in logs if x],
                  "log_dir": str(routinerun.RUNS / pid), "note": r.get("note") or ""}))
    return out


# ---------------------------------------------------------------- ideas

_STAMP = re.compile(r"(deferred|declined|approved|dropped) by the owner (\d{4}-\d{2}-\d{2})")


def _idea_project(name, lanes):
    """An idea's project ("Vira", "Tiksi") as the lane its code lives in
    ("vira", "tiksi"): the repository folder of the same name, any case."""
    n = str(name or "").strip()
    for lane in sorted(lanes):
        if lane and lane.lower() == n.lower():
            return lane
    return n


def from_ideas(ctx):
    from . import ideas as ideastore
    s = _read_json(ideastore.STORE, {"items": []}) or {}
    items = s.get("items") or []
    lanes = {e.get("project") for e in ctx.current.values() if e.get("project")}
    lanes |= {Path(r).name for r in (H.repo_root(f) for f in ctx.folders) if r}
    lanes.add(Path(H.vira_repo()).name)
    out = []
    for it in items:
        iid = it.get("id")
        if not iid:
            continue
        ctx.ideas[iid] = it
        st = it.get("status") or ""
        note = it.get("note") or ""
        if st == "done":
            status = H.MERGED if "merged into main" in note else H.ANSWERED
        elif st in ("dropped", "deferred"):
            status = H.DROPPED
        else:
            status = H.WAITING
        project = _idea_project(it.get("project"), lanes)
        title = H.pick_title(idea_title=it.get("text") or "")
        out.append(H.event(
            f"ask:idea:{iid}", "ask", it.get("created"), sub="idea", status=status,
            title=title or "Idea", project=project, source="ideas",
            links={"idea": iid},
            data={"idea_status": st, "source": it.get("source") or "",
                  "text": (it.get("text") or "")[:1200], "note": note[:600],
                  "updated": H.iso(it.get("updated"))}))
        # Decisions Vira stamped into the note, and status changes seen
        # since the last pass (the first approve is only a status change).
        for m in _STAMP.finditer(note):
            verb, day = m.group(1), m.group(2)
            out.append(H.event(
                f"decide:idea:{iid}:{verb}:{day}", "decide", day + "T12:00:00", sub=verb,
                status=H.ANSWERED if verb == "approved" else H.DROPPED,
                title=H.clean_title(f"{verb.capitalize()}: {title}"), project=project,
                source="ideas", links={"idea": iid}, data={"day_only": True}))
        prev = ctx.current.get(f"ask:idea:{iid}")
        old = ((prev or {}).get("data") or {}).get("idea_status")
        if prev is not None and old and old != st and st in ("open", "deferred", "dropped"):
            verb = {"open": "approved", "deferred": "deferred", "dropped": "dropped"}[st]
            if not any(f"decide:idea:{iid}:{verb}:" in k for k in ctx.current):
                out.append(H.event(
                    f"decide:idea:{iid}:{verb}:{H.iso(it.get('updated'))}", "decide",
                    it.get("updated"), sub=verb,
                    status=H.ANSWERED if verb == "approved" else H.DROPPED,
                    title=H.clean_title(f"{verb.capitalize()}: {title}"), project=project,
                    source="ideas", links={"idea": iid}, data={"from": old, "to": st}))
    return out


# ---------------------------------------------------------------- journal, chat

def from_journal(ctx):
    from . import journal
    entries = {}
    for e in _read_jsonl(journal.STORE.with_name("brief-journal-archive.jsonl")):
        if e.get("id"):
            entries[e["id"]] = e
    for e in (_read_json(journal.STORE, {"entries": []}) or {}).get("entries") or []:
        if isinstance(e, dict) and e.get("id"):
            entries[e["id"]] = e
    out = []
    for e in entries.values():
        res = e.get("result") if isinstance(e.get("result"), dict) else {}
        idea_ids, job_ids = [], []
        for u in (res.get("unapplied") or []) + (res.get("actions") or []):
            if isinstance(u, dict):
                idea_ids.append(u.get("idea_id"))
                job_ids.append(u.get("job_id"))
        st = e.get("status") or ""
        status = H.ANSWERED if (res or st in ("noted", "done", "applied")) else H.WAITING
        out.append(H.event(
            f"ask:note:{e['id']}", "ask", e.get("created"), sub="journal", status=status,
            title=H.first_sentence(e.get("text")) or "Note", source="brief-journal",
            project="", links={"note": e["id"], "idea": idea_ids, "job": job_ids,
                               "person": e.get("person_id")},
            data={"text": (e.get("text") or "")[:2000], "context": e.get("context") or "",
                  "journal_status": st, "result": H.clean_title(res.get("summary") or "", 400)}))
    return out


def from_chat(ctx):
    s = _read_json(DATA / "vira-chat.json", {}) or {}
    sessions = s.get("sessions") or {}
    if isinstance(sessions, list):
        sessions = {x.get("id"): x for x in sessions if isinstance(x, dict)}
    out = []
    for cid, sess in sessions.items():
        if not isinstance(sess, dict):
            continue
        for t in sess.get("turns") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            title = H.first_sentence(t.get("question")) or "Question"
            links = {"chat": cid, "job": t.get("job_id") or sess.get("job_id")}
            out.append(H.event(
                f"ask:chat:{t['id']}", "ask", t.get("created") or sess.get("started"),
                sub="chat", status=H.ANSWERED if t.get("answer") else H.RUNNING,
                title=title, source="vira-chat", links=links,
                data={"text": (t.get("question") or "")[:1200]}))
            if t.get("answer"):
                usage = t.get("usage") if isinstance(t.get("usage"), dict) else {}
                totals = H.add_usage({}, t.get("model") or "", usage) if usage else {}
                cost, tokens = H.cost_of(totals)
                out.append(H.event(
                    f"answer:chat:{t['id']}", "answer", t.get("finished") or t.get("created"),
                    sub="chat", status=H.ANSWERED if t.get("outcome") != "failed" else H.FAILED,
                    title=title, source="vira-chat", links=links,
                    data={"text": (t.get("answer") or "")[:1200], "model": t.get("model") or "",
                          "tokens": tokens, "cost_usd": cost}))
    return out


# ---------------------------------------------------------------- notifications

_TXT_BRANCH = re.compile(r"\b((?:claude|codex)/[A-Za-z0-9._-]+)")
_TXT_PR = re.compile(r"\bPR #?(\d+)\b|\bpull request #?(\d+)\b", re.I)


def _routine_job(ctx, rid, at):
    """The Vira job a routine started at `at` (its last_run stamp)."""
    best, gap = "", 1800.0
    for e in ctx.collected:
        if e.get("kind") != "session" or e.get("sub") != "vira":
            continue
        if rid not in (e.get("links") or {}).get("schedule", []):
            continue
        d = abs(H.to_ts(e.get("at")) - at)
        if d < gap:
            best, gap = (e["links"].get("job") or [""])[0], d
    return best


def from_notify(ctx):
    from . import notify
    rows = _read_jsonl(notify.LOG.with_name("notify-log-archive.jsonl"))
    rows += (_read_json(notify.LOG, {"sent": []}) or {}).get("sent") or []
    vira = Path(H.vira_repo()).name
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict) or not r.get("at"):
            continue
        text = str(r.get("text") or "")
        nid = hashlib.sha256((str(r.get("at")) + "\n" + text).encode("utf-8")).hexdigest()[:16]
        if nid in seen:
            continue
        seen.add(nid)
        ref = r.get("ref") if isinstance(r.get("ref"), dict) else {}
        links = {"job": [ref.get("job_id"), ref.get("job")], "idea": ref.get("idea_id"),
                 "flow_run": ref.get("run_id") or ref.get("circuit_run"),
                 "person": r.get("person_id") if not str(r.get("person_id") or "").startswith(
                     ("channel:", "agent:", "subs:")) else None,
                 "branch": [_branch_key(vira, b) for b in _TXT_BRANCH.findall(text)],
                 "pr": [_pr_key(vira, a or b) for a, b in _TXT_PR.findall(text)]}
        if ref.get("branch"):
            links["branch"].append(_branch_key(vira, ref["branch"]))
        # agent_ping keeps its throttle key as the person id: it names the
        # Flow run ("circuit:<run>:<card>") or the routine and its run time
        # ("routine:<id>:<when>").
        key = str(r.get("person_id") or "")
        if key.startswith("agent:circuit:"):
            links["flow_run"] = key.split(":")[2]
        elif key.startswith("agent:routine:"):
            bits = key.split(":", 3)
            rid, when = (bits[2], bits[3]) if len(bits) == 4 else (bits[-1], "")
            links["job"].append(_routine_job(ctx, rid, H.to_ts(when) or H.to_ts(r.get("at"))))
        body = text[len("Vira: "):] if text.startswith("Vira: ") else text
        # A branch name never reaches a title; the sentence keeps its shape.
        body = _TXT_BRANCH.sub("the branch", body)
        out.append(H.event(
            f"tell:{nid}", "tell", r.get("at"), sub=r.get("channel") or "text",
            status=H.RAN if r.get("ok") else H.FAILED, title=H.first_sentence(body) or "Text",
            source="notify-log", links=links,
            data={"text": text[:800], "channel": r.get("channel") or "",
                  "via": r.get("via") or [], "ref": ref, "error": r.get("error") or ""}))
    return out


# ---------------------------------------------------------------- decisions

def from_review(ctx):
    from . import reviewqueue
    s = _read_json(reviewqueue.DECISIONS, {}) or {}
    out = []
    for source, items in s.items():
        if not isinstance(items, dict):
            continue
        for key, d in items.items():
            if not isinstance(d, dict):
                continue
            action = str(d.get("action") or "decided")
            status = H.DROPPED if action in ("dismiss", "drop", "reject", "decline", "skip") else H.ANSWERED
            out.append(H.event(
                f"decide:review:{source}:{key}", "decide", d.get("at"), sub="review",
                status=status, title=H.clean_title(f"{action.capitalize()} in Review: {source}"),
                source="review-decided", links={}, data={"source": source, "key": key,
                                                         "action": action, "mode": d.get("mode")}))
    return out


def from_branch_actions(ctx):
    from . import orphanwork
    vira = Path(H.vira_repo()).name
    merged = {}
    for r in _read_jsonl(orphanwork.ACTIONS_LOG()):
        if r.get("branch") and r.get("action") and r.get("at"):
            merged.setdefault((r["branch"], r["at"], r["action"]), {}).update(r)
    out = []
    for r in merged.values():
        branch, action = r["branch"], r["action"]
        status = {"merge": H.MERGED, "discard": H.DROPPED}.get(action, H.ANSWERED)
        label = {"merge": "Merge", "discard": "Discard", "land": "Merge"}.get(action, action.capitalize())
        out.append(H.event(
            f"decide:branch:{branch}:{r.get('at')}", "decide", r.get("at"), sub=action,
            status=status, title=H.clean_title(f"{label}: " + (r.get("title") or
                                               branch.split("/", 1)[-1].replace("-", " "))),
            project=vira, source="branch-actions",
            links={"branch": _branch_key(vira, branch), "job": r.get("job_id")},
            data={"ok": r.get("ok"), "by": r.get("by") or "", "outcome": r.get("outcome") or ""}))
    return out


# ---------------------------------------------------------------- made things

def from_contacts(ctx):
    s = _read_json(DATA / "contacts-sync.json", {}) or {}
    out, runs = [], []
    for r in s.get("log") or []:
        if not isinstance(r, dict) or not r.get("when"):
            continue
        counts = {k: r.get(k) for k in ("imported", "handles", "fills", "pushed", "created",
                                        "review") if isinstance(r.get(k), int)}
        errs = r.get("errors") or []
        rid = "contacts:" + str(r["when"])
        runs.append((H.to_ts(r["when"]), rid))
        out.append(H.event(
            f"run:{rid}", "run", r["when"], sub="contacts",
            status=H.FAILED if errs else H.RAN,
            title=H.automation_title("Contacts sync", counts, len(errs)),
            project=Path(H.vira_repo()).name,
            source="contacts-sync", links={"program": "contacts-sync", "program_run": rid},
            data={"reason": r.get("reason") or "", "counts": counts,
                  "errors": len(errs), "error_lines": [str(x)[:300] for x in errs[:5]]}))
    runs.sort()
    for c in s.get("created") or []:
        if not isinstance(c, dict) or not c.get("when"):
            continue
        at = H.to_ts(c["when"])
        run = next((rid for t, rid in reversed(runs) if t <= at + 5), "")
        out.append(H.event(
            f"made:contact:{c.get('uid') or c.get('pid')}", "made", c["when"], sub="contact",
            status=H.DROPPED if c.get("undone") else H.RAN,
            title=H.clean_title("Phone card created: " + (c.get("name") or "a contact")),
            source="contacts-sync", links={"person": c.get("pid"), "program_run": run},
            data={"undone": bool(c.get("undone"))}))
    return out


def _retro_dir():
    from . import changelog
    return changelog.SESSIONS


_RETRO_TITLE = re.compile(r"^#\s+(.+)$", re.M)


def from_documents(ctx):
    out = []
    rl = _read_json(DATA / "reading-list.json", {"items": []}) or {}
    for it in rl.get("items") or []:
        if not isinstance(it, dict) or not it.get("id") or it.get("kind") == "retro":
            continue
        ref = str(it.get("ref") or "")
        out.append(H.event(
            f"made:doc:{it['id']}", "made", it.get("added") or it.get("created"),
            sub="document", status=H.RAN, title=H.clean_title(it.get("title")) or "Document",
            source="reading-list",
            links={"doc": it["id"], "job": ref if re.fullmatch(r"[0-9a-f]{12}", ref) else ""},
            data={"doc_kind": it.get("kind") or "", "locator": it.get("locator") or "",
                  "source": it.get("source") or ""}))
    # Retros: one document each, linked to its session; never one entry per line.
    d = _retro_dir()
    st = ctx.state.setdefault("retros", {})
    try:
        files = sorted(d.glob("*.md")) if d.is_dir() else []
    except OSError:
        files = []
    for p in files:
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            continue
        hit = st.get(str(p))
        if hit and hit.get("mtime") == mt and not ctx.full and f"made:retro:{p.stem}" in ctx.current:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:20000]
        except OSError:
            continue
        fm = text.split("\n---", 1)[0] if text.startswith("---") else ""
        def field(name):
            m = re.search(rf"^{name}:\s*\"?([^\"\n]+)\"?\s*$", fm, re.M)
            return m.group(1).strip() if m else ""
        stem_day = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
        day = field("date")[:10] or (stem_day.group(1) if stem_day else "")
        tm = field("time")
        if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", tm or ""):
            tm = ""
        # A day retro's "23:59" is the writer's stamp, not when the work
        # happened: keep the day, say the time is not known.
        if tm in ("23:59", "23:59:59"):
            tm = ""
        at = f"{day}T{tm or '12:00'}" if day else p.stat().st_mtime
        m = _RETRO_TITLE.search(text)
        sid = field("session_id")
        out.append(H.event(
            f"made:retro:{p.stem}", "made", at, sub="retro", status=H.RAN,
            title=H.clean_title(m.group(1) if m else p.stem) or "Retro",
            project=field("project"), source="retros",
            links={"session": sid},
            data={"path": str(p), "day_only": not tm, "generated_by": field("generated_by")}))
        st[str(p)] = {"mtime": mt}
    return out


def from_inflow(ctx):
    """Captured vault notes, each linked to the run that captured it: the
    Program or Flow run in the vault's folder whose window holds the note's
    creation time. A note no run explains is still recorded, unlinked."""
    from . import ingestfeed
    if not ctx.due("inflow", 600):
        return []
    try:
        cards, _ = ingestfeed.scan()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Inflow scan: {e}") from e
    vault = ingestfeed.vault_root()
    windows = []
    pool = dict(ctx.current)
    pool.update({e["id"]: e for e in ctx.collected})
    for e in pool.values():
        if e.get("kind") == "run" and e.get("sub") in ("program", "flow", "step"):
            a, b = H.to_ts(e.get("at")), H.to_ts(e.get("end")) or H.to_ts(e.get("at"))
            if a:
                windows.append((a, b + 120, e["id"]))
    windows.sort()
    out = []
    for c in cards:
        rel = c.get("path") or ""
        if not rel:
            continue
        p = Path(vault) / rel if vault else None
        born = 0.0
        if p is not None:
            try:
                st = p.stat()
                born = getattr(st, "st_birthtime", 0) or st.st_mtime
            except OSError:
                born = 0.0
        at = born or H.to_ts(c.get("date"))
        run = next((rid for a, b, rid in windows if a - 5 <= born <= b), "") if born else ""
        links = {"notes": rel}
        if run:
            links["flow_run" if run.startswith("run:flow:") else "program_run"] = run.split(":", 2)[2]
        out.append(H.event(
            f"made:note:{c.get('id') or rel}", "made", at, sub="note", status=H.RAN,
            title=H.clean_title(c.get("title")) or "Captured note", project=Path(vault).name if vault else "",
            folder=str(vault or ""), source="inflow", links=links,
            data={"capture_source": c.get("source") or "", "path": rel, "url": c.get("url") or ""}))
    ctx.ran("inflow")
    return out


# ---------------------------------------------------------------- the pass

# Order matters: sessions before git (the repositories they worked in), and
# before ideas (an idea's project is named after a repository); flows before
# programs (the program -> flow link) and runs before the Inflow (a note is
# linked to the run whose window holds it). Idea texts and pull request
# titles are preloaded, so the sessions read first still get them.
SOURCES = (
    ("vira_jobs", from_vira_jobs), ("claude_app", from_claude_app),
    ("claude_cli", from_claude_cli), ("codex", from_codex), ("git", from_git),
    ("branch_state", from_branch_state), ("ideas", from_ideas),
    ("flows", from_flows), ("programs", from_programs), ("journal", from_journal),
    ("chat", from_chat), ("notify", from_notify), ("review", from_review),
    ("branch_actions", from_branch_actions), ("contacts", from_contacts),
    ("documents", from_documents), ("inflow", from_inflow),
)


def collect(ctx, only=None):
    events = []
    for name, fn in SOURCES:
        if only and name not in only:
            continue
        t0 = time.time()
        try:
            got = fn(ctx) or []
        except Exception as e:  # noqa: BLE001 - one broken source never hides the rest
            ctx.errors[name] = f"{type(e).__name__}: {e}"[:300]
            got = []
        ctx.counts[name] = len(got)
        ctx.counts[name + "_s"] = round(time.time() - t0, 2)
        events.extend(got)
        ctx.collected = events
    return events


def run(full=False, only=None):
    """One pass. Returns a report: events read per source, lines written,
    errors by source, seconds."""
    from .filelock import locked
    d = H.store_dir()
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with locked(d / "ingest"):
        ctx = Ctx(full=full)
        events = collect(ctx, only)
        written = H.put(events, current=ctx.current)
        jsonstore.write_atomic(state_path(), ctx.state, ensure_ascii=False)
        try:
            compressed = H.compress_old()
        except OSError:
            compressed = []
    return {"written": written, "read": len(events), "counts": ctx.counts,
            "errors": ctx.errors, "seconds": round(time.time() - t0, 1),
            "compressed": compressed, "at": H.now_iso()}


def status():
    events = H.load()
    kinds, subs, sources = {}, {}, {}
    for e in events.values():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        k = f"{e['kind']}/{e.get('sub')}"
        subs[k] = subs.get(k, 0) + 1
        sources[e.get("source")] = sources.get(e.get("source"), 0) + 1
    return {"events": len(events), "lines": H.line_count(), "kinds": kinds,
            "subs": dict(sorted(subs.items())), "sources": sources,
            "revision": H.revision()}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd in ("backfill", "tick"):
        only = argv[1].split(",") if len(argv) > 1 else None
        rep = run(full=(cmd == "backfill"), only=only)
        print(json.dumps(rep, indent=1))
        return 1 if rep["errors"] else 0
    if cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

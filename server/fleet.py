"""Fleet: every agent session on this machine, live, in one window.

It covers Claude Code (desktop-app sessions, terminal sessions, subagents),
Codex (the app, the VS Code extension, the CLI) and Vira's own jobs, across
every project on the machine, not only Vira. Around the sessions it shows
the repository picture: worktrees and branches, running Vira branch
instances, pull requests, what landed on the base branch beside the change
log, and the headless model calls running in the background.

It replaced two pages the owner kept outside Vira: Session Fleet (live,
Claude desktop sessions only; its collector is the logic fleetclaude.py
ports) and a frozen "Vira activity" snapshot that had the rest.

Readers, one module per source, all read-only and all optional:

    fleetclaude.py  Claude Code transcripts, app records, headless calls
    fleetcodex.py   Codex's thread database and rollout files
    fleetgit.py     git and gh, per repository
    this module     Vira's jobs, the process table, families, delivery

A source that is not on this machine reports "not found on this machine"
in `sources` and the window still works: Windows and Linux have no Claude
app folder where macOS does, CI has no Codex, and gh may be missing.

TWO SPEEDS. The live tier (processes, app records, transcript and rollout
tails, Vira jobs) runs every LIVE_SECS, and only while a Fleet window is
connected to /api/fleet/stream: with no window open it does nothing, so a
running Vira pays nothing for Fleet existing. State between ticks (byte
offsets, record mtimes, the pid-to-folder cache) keeps a warm tick cheap.
The slow tier (git every fleetgit.REPO_SECS or when refs or worktrees
change, gh every fleetgit.GH_SECS, headless-call counts, the 7-day view,
the change log) runs on the same loop but rarely.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import admission, changelog, instance, joblog, settings
from . import fleetclaude as fc
from . import fleetcodex as fx
from . import fleetgit as fg

LIVE_SECS = 2.0
# Closed sessions drawn as chips: anything active in the last 72 hours,
# plus every relative of a shown session.
CLOSED_WINDOW_MS = 72 * 3600 * 1000
GRAPH_MAX_CLOSED = 60
DAY_MS = 24 * 3600 * 1000
WEEK_MS = 7 * DAY_MS
# How often a transcript-path miss is retried by re-indexing the folders.
INDEX_SECS = 10
# Listening ports and Vira instances are re-read this often.
INSTANCE_SECS = 10
INSTANCE_PORTS = range(8377, 8400)    # live is 8377; branch instances 8378-8399
# Vira's own change log groups are rebuilt at most this often.
CHANGELOG_SECS = 300
# Repositories scanned. Sessions roam; the ones most recently worked in
# are the ones whose branches matter.
REPOS_MAX = 16
# Days of landings and change-log entries shown in the timeline.
TIMELINE_DAYS = 60
STATUS_RANK = {"Needs you": 0, "Working": 1, "Jobs running": 2, "Idle": 3, "Closed": 4}
BASE_NAMES = {"main", "master", "trunk", "develop", "HEAD"}


def log(*a):
    print("[fleet]", *a, file=sys.stderr, flush=True)


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- processes

def etime_secs(e):
    d, _, rest = e.rpartition("-")
    try:
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return int(d or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return 0


class Procs:
    """The process table, and each process's working folder (cached per
    process: lsof is the slow part of a tick)."""

    def __init__(self):
        self.cwds = {}           # pid -> (started_s, cwd)
        self.note = None

    def read(self):
        if os.name == "nt":
            self.note = "the process list is not read on Windows"
            return {}, {}
        ps = shutil.which("ps") or "/bin/ps"
        try:
            out = subprocess.run([ps, "-axo", "pid=,ppid=,etime=,%cpu=,rss=,args="],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=5, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            self.note = "could not read the process list"
            return {}, {}
        self.note = None
        procs, kids = {}, {}
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(\d+)\s+(.*)", line)
            if not m:
                continue
            pid, ppid, et, cpu, rss, args = m.groups()
            p = dict(pid=int(pid), ppid=int(ppid), secs=etime_secs(et),
                     cpu=float(cpu), rss=int(rss), args=args)
            procs[p["pid"]] = p
            kids.setdefault(p["ppid"], []).append(p["pid"])
        live = set(procs)
        for k in [k for k in self.cwds if k not in live]:
            del self.cwds[k]
        return procs, kids

    def cwd_of(self, p):
        started = time.time() - p["secs"]
        hit = self.cwds.get(p["pid"])
        if hit and abs(hit[0] - started) <= 3:
            return hit[1]
        cwd = None
        if sys.platform.startswith("linux"):
            try:
                cwd = os.readlink(f"/proc/{p['pid']}/cwd")
            except OSError:
                cwd = None
        else:
            lsof = shutil.which("lsof") or "/usr/sbin/lsof"
            try:
                r = subprocess.run([lsof, "-a", "-p", str(p["pid"]), "-d", "cwd", "-Fn"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=3, check=False).stdout
                cwd = next((ln[1:] for ln in r.splitlines() if ln.startswith("n")), None)
            except (OSError, subprocess.TimeoutExpired):
                cwd = None
        self.cwds[p["pid"]] = (started, cwd)
        return cwd


def listening_ports():
    """port -> pid for every TCP listener, or None when lsof is missing."""
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        out = subprocess.run([lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    ports, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid:
            m = re.search(r":(\d+)$", line)
            if m:
                ports.setdefault(int(m.group(1)), pid)
    return ports


def probe_instance(port, timeout=0.6):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/instance",
                                    timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- projects

def is_scratch(path):
    p = str(path or "").replace("\\", "/")
    return "scratch-workspaces" in p or "/scratchpad" in p


def project_of(path):
    """(lane name, repository root) for a session's folder."""
    if not path or is_scratch(path):
        return "No folder", None
    root = fg.repo_root(path)
    if root:
        return Path(root).name, root
    p = Path(path)
    try:
        if p.resolve() == Path.home().resolve() or str(p) in ("/", "\\"):
            return "Home folder", None
    except OSError:
        pass
    return p.name or "Home folder", None


def base_row(**kw):
    r = dict(id=None, tool=None, surface=None, project="No folder", repo=None,
             folder=None, worktree=None, branch=None, title="Untitled", openUrl=None,
             viraJob=None, status="Closed", live=False, startedAt=None, createdAt=None,
             lastAt=None, turnOpen=False, turnStartedAt=None, turnTools=0,
             turnOrigin=None, now=None, feed=[], plan=None, question=None,
             context=None, agents={"live": [], "total": 0}, shells=[], model="",
             effort="", mode="", pid=None, cpu=None, memMB=None, asked=None,
             lastText=None, prs=[], summary=None, report=None, parentId=None,
             link=None, family=None, git=None,
             _parent=None, _link=None, _sid=None, _written=[], _base=None)
    r.update(kw)
    return r


def status_of(turn_open, waiting, agents_live, shells, live):
    if not live:
        return "Closed"
    if waiting:
        return "Needs you"
    if turn_open or agents_live:
        return "Working"
    if shells:
        return "Jobs running"
    return "Idle"


# ---------------------------------------------------------------- Vira jobs

def vira_jobs():
    """(live registry rows, ledger rows) from this server's job registry.
    Read in process, never over HTTP to itself."""
    from . import session
    try:
        live = session.sessions.recent()
    except Exception:  # noqa: BLE001 - a registry that cannot answer is no jobs
        live = []
    return live, joblog.recent(300)


# ---------------------------------------------------------------- the collector

class Fleet:
    def __init__(self, jobs=None):
        self.procs = Procs()
        self.jobs = jobs or vira_jobs
        self.transcripts = {}      # path -> Transcript (live sessions)
        self.subagents = {}        # path -> Subagent
        self.records = {}          # record path -> (mtime, dict)
        self.tindex = {}           # transcript stem -> path
        self.tindex_at = 0.0
        self.lastts = {}           # path -> (size, mtime_ns, ms)
        self.rollouts = {}         # rollout path -> Rollout
        self.rollout_ts = {}       # rollout path -> (size, mtime_ns, ms)
        self.names = (None, {})    # session_index mtime, names
        self.ledger = (None, [])   # jobs-log mtime, rows
        self.calls = fc.CallIndex()
        self.call_rows = []
        self.cli_seen = []
        self.gh = fg.GhCache()
        self.scans = {}            # repo root -> scan
        self.sigs = {}             # repo root -> (signature, scanned at)
        self.roots_seen = {}       # repo root -> last session activity ms
        self.inst = ([], [], 0.0)  # instances, listener pids, read at
        self.vira_log = (0.0, None)
        self.live_lock = threading.Lock()
        self.slow_lock = threading.Lock()
        self.last_rows = []
        self.last_live = None
        self.last_repos = None
        self.notes = {}
        self._shown_closed = set()
        self._used = set()           # transcripts read this tick

    # ----- Claude Code

    def _records(self):
        root = fc.sessions_root()
        out = []
        if not root.is_dir():
            return out, False
        seen = set()
        for f in root.glob("*/*/local_*.json"):
            seen.add(f)
            try:
                m = f.stat().st_mtime_ns
            except OSError:
                continue
            cached = self.records.get(f)
            if not cached or cached[0] != m:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(d, dict) or not d.get("sessionId"):
                    continue
                cached = (m, {k: d.get(k) for k in fc.RECORD_KEYS})
                self.records[f] = cached
            if not cached[1].get("isArchived"):
                out.append(cached[1])
        for k in [k for k in self.records if k not in seen]:
            del self.records[k]
        return out, True

    def _tpath(self, cli_id):
        if not cli_id:
            return None
        p = self.tindex.get(cli_id)
        if p and os.path.exists(p):
            return Path(p)
        if time.time() - self.tindex_at > INDEX_SECS:
            self.tindex_at = time.time()
            idx = {}
            root = fc.projects_dir()
            try:
                for d in os.scandir(root):
                    if d.is_dir():
                        for f in os.scandir(d.path):
                            if f.name.endswith(".jsonl"):
                                stem = f.name[:-6]
                                prev = idx.get(stem)
                                if not prev or os.path.getmtime(f.path) > os.path.getmtime(prev):
                                    idx[stem] = f.path
            except OSError:
                pass
            self.tindex = idx
        p = self.tindex.get(cli_id)
        return Path(p) if p else None

    def _last_ts(self, path, cache=None, reader=fc.last_timestamp):
        cache = self.lastts if cache is None else cache
        try:
            st = os.stat(path)
        except OSError:
            return None
        hit = cache.get(str(path))
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2]
        ms = reader(Path(path))
        cache[str(path)] = (st.st_size, st.st_mtime_ns, ms)
        return ms

    def _transcript(self, path):
        t = self.transcripts.get(path)
        if not t:
            t = self.transcripts[path] = fc.Transcript(path)
        t.poll()
        self._used.add(path)
        return t

    def _subagents(self, tpath, sid, now_s):
        live, total = [], 0
        sdir = tpath.parent / sid / "subagents" if tpath else None
        if not sdir or not sdir.is_dir():
            return live, total
        for f in sdir.glob("agent-*.jsonl"):
            total += 1
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if now_s - mt > fc.SUBAGENT_LIVE_SECS:
                continue
            sa = self.subagents.get(f)
            if not sa:
                sa = self.subagents[f] = fc.Subagent(f)
            sa.poll()
            if sa.live():
                live.append(sa.view())
        return live, total

    def _claude(self, t_now, procs, kids, exclude):
        rows = []
        records, app_found = self._records()
        proj_found = fc.projects_dir().is_dir()
        app_by_id, app_loose, cli_procs = {}, [], []
        for p in procs.values():
            a = p["args"]
            if fc.app_process(a):
                rid = fc.resume_id(a)
                if rid:
                    app_by_id[rid] = p
                else:
                    app_loose.append(p)
            elif fc.cli_process(a):
                cli_procs.append(p)
        loose_by_cwd = {}
        for p in app_loose:
            c = self.procs.cwd_of(p)
            if c:
                loose_by_cwd.setdefault(c, []).append(p)
        now_s = t_now / 1000
        used_paths = set()
        known_ids = set()
        for d in records:
            cli_id = d.get("cliSessionId")
            known_ids.add(cli_id)
            proc = app_by_id.get(cli_id) if cli_id else None
            if not proc and loose_by_cwd.get(d.get("cwd")):
                proc = loose_by_cwd[d.get("cwd")].pop()
            tpath = self._tpath(cli_id)
            origin = d.get("originCwd") or d.get("cwd") or ""
            project, repo = project_of(origin)
            repo = fc.repo_of_record(d) or repo
            if repo and project != "No folder":
                project = Path(repo).name
            base = d.get("sourceBranch") or "main"
            prs = fc.prs_of_record(d, base)
            parent, link = fc.link_of_record(d)
            common = dict(
                id=d["sessionId"], tool="claude", surface="app", project=project, repo=repo,
                folder=d.get("cwd"), worktree=d.get("worktreeName"), branch=d.get("branch"),
                title=d.get("title") or "Untitled", openUrl=f"claude://claude.ai/epitaxy/{d['sessionId']}",
                createdAt=d.get("createdAt"), startedAt=d.get("createdAt"),
                model=(d.get("model") or "").replace("claude-", ""), effort=d.get("effort") or "",
                mode=d.get("permissionMode") or "", prs=[{k: p[k] for k in ("n", "state", "url")} for p in prs],
                summary=fc.summary_of_record(d),
                report=fc.clip(d.get("lastTurnReport") or "", 280) or None,
                _parent=parent, _link=link, _sid=cli_id, _written=fc.written_of_record(d),
                _base=base, _prs=prs)
            if not proc:
                last = d.get("lastActivityAt")
                if tpath:
                    try:
                        mt = tpath.stat().st_mtime * 1000
                    except OSError:
                        mt = 0
                    if t_now - mt < WEEK_MS:
                        last = max(filter(None, [last, self._last_ts(tpath)]), default=last)
                    else:
                        last = max(filter(None, [last, int(mt)]), default=last)
                if not last or t_now - last > WEEK_MS:
                    continue
                rows.append(base_row(**common, lastAt=last))
                continue
            t = self._transcript(tpath) if tpath else None
            if tpath:
                used_paths.add(tpath)
            live_agents, total_agents = self._subagents(tpath, cli_id, now_s)
            shells = [s for s in fc.shell_children(proc, procs, kids, t_now)
                      if "session-fleet" not in s["cmd"]]
            status = status_of(bool(t and t.turn_open), bool(t and t.waiting()),
                               live_agents, shells, True)
            model = (t.model if t and t.model else d.get("model")) or ""
            rows.append(base_row(
                **{**common, "model": model.replace("claude-", "")},
                status=status, live=True, pid=proc["pid"], cpu=proc["cpu"],
                memMB=round(proc["rss"] / 1024),
                lastAt=max(filter(None, [t.last_at if t else None, d.get("lastActivityAt")]), default=None),
                turnOpen=bool(t and t.turn_open), turnStartedAt=t.turn_started if t else None,
                turnTools=t.turn_tools if t else 0, turnOrigin=t.turn_origin if t else None,
                asked=t.asked if t else None, lastText=t.last_text if t else None,
                now=t.now_tool() if t else None, question=t.open_question() if t else None,
                plan=t.plan_view() if t else None,
                context=dict(used=t.context if t else None, limit=fc.context_limit(model),
                             compactions=t.compactions if t else 0),
                agents=dict(live=live_agents, total=total_agents), shells=shells,
                feed=t.feed_view() if t else []))

        # Terminal sessions: a `claude` process, matched to its transcript by
        # --resume, else the newest terminal transcript in its folder.
        live_cli = set()
        for p in cli_procs:
            cwd = self.procs.cwd_of(p)
            rid = fc.resume_id(p["args"])
            path = self._tpath(rid) if rid else None
            if not path and cwd:
                folder = fc.projects_dir() / fc.slug(cwd)
                cands = sorted(folder.glob("*.jsonl"), key=lambda f: -f.stat().st_mtime) if folder.is_dir() else []
                for c in cands[:5]:
                    if c in used_paths:
                        continue
                    head = self._head(c)
                    if head.get("entrypoint") == fc.CLI_ENTRY:
                        path = c
                        break
            sid = path.stem if path else f"pid-{p['pid']}"
            if sid in exclude or sid in known_ids:
                continue
            live_cli.add(sid)
            t = self._transcript(path) if path else None
            if path:
                used_paths.add(path)
            live_agents, total_agents = self._subagents(path, sid, now_s)
            shells = fc.shell_children(p, procs, kids, t_now)
            folder = (t.cwd if t else None) or cwd
            project, repo = project_of(folder)
            model = (t.model if t else "") or ""
            rows.append(base_row(
                id=sid, tool="claude", surface="CLI", project=project, repo=repo, folder=folder,
                branch=t.branch if t else None,
                title=fc.clip((t.asked if t else None) or "Terminal session", 90),
                status=status_of(bool(t and t.turn_open), bool(t and t.waiting()), live_agents, shells, True),
                live=True, pid=p["pid"], cpu=p["cpu"], memMB=round(p["rss"] / 1024),
                startedAt=t_now - p["secs"] * 1000, createdAt=t_now - p["secs"] * 1000,
                lastAt=t.last_at if t else None, turnOpen=bool(t and t.turn_open),
                turnStartedAt=t.turn_started if t else None, turnTools=t.turn_tools if t else 0,
                turnOrigin=t.turn_origin if t else None, asked=t.asked if t else None,
                lastText=t.last_text if t else None, now=t.now_tool() if t else None,
                question=t.open_question() if t else None, plan=t.plan_view() if t else None,
                context=dict(used=t.context if t else None, limit=fc.context_limit(model),
                             compactions=t.compactions if t else 0),
                agents=dict(live=live_agents, total=total_agents), shells=shells,
                model=model.replace("claude-", ""), feed=t.feed_view() if t else [], _sid=sid))
        # closed terminal sessions, from the slow tier's folder scan
        for c in self.cli_seen:
            head = c["head"]
            sid = head.get("sessionId") or Path(c["path"]).stem
            if sid in live_cli or sid in exclude or sid in known_ids:
                continue
            last = self._last_ts(c["path"]) or int(c["mtime"] * 1000)
            if t_now - last > WEEK_MS:
                continue
            project, repo = project_of(head.get("cwd"))
            rows.append(base_row(
                id=sid, tool="claude", surface="CLI", project=project, repo=repo,
                folder=head.get("cwd"), branch=head.get("branch"),
                title=fc.clip(head.get("prompt") or "Terminal session", 90),
                startedAt=head.get("at"), createdAt=head.get("at"), lastAt=last, _sid=sid))

        for k in [k for k, v in self.subagents.items() if now_s - (v.t.last_at or 0) / 1000 > 3600]:
            del self.subagents[k]
        self.notes["claudeApp"] = None if app_found else "Claude desktop app not found on this machine"
        self.notes["claude"] = None if proj_found else "Claude Code not found on this machine"
        return rows

    def _head(self, path):
        key = str(path)
        try:
            m = os.stat(path).st_mtime_ns
        except OSError:
            return {}
        hit = self.calls.heads.get(key)
        if hit and hit[1].get("entrypoint"):
            return hit[1]
        head = fc.read_head(Path(path))
        self.calls.heads[key] = (m / 1e9, head)
        return head

    # ----- Codex

    def _thread_names(self):
        p = fx.codex_home() / "session_index.jsonl"
        try:
            m = p.stat().st_mtime_ns
        except OSError:
            return {}
        if self.names[0] != m:
            self.names = (m, fx.thread_names())
        return self.names[1]

    def _rollout_last(self, path):
        return self._last_ts(path, cache=self.rollout_ts, reader=codex_last_real)

    def _codex(self, t_now, procs, vira_by_sid):
        rows = []
        threads, note = fx.read_threads(t_now - WEEK_MS, ids=list(vira_by_sid))
        self.notes["codex"] = note
        if note:
            return rows
        names = self._thread_names()
        running = fx.codex_running(procs)
        children, mains = {}, []
        for th in threads:
            if th.get("archived"):
                continue
            parent = fx._parent_of_source(th.get("source"))
            if parent:
                children.setdefault(parent, []).append(th)
            else:
                mains.append(th)
        used = set()
        for th in mains:
            path = th.get("rollout_path")
            last = self._rollout_last(path) if path else None
            created = fx.created_ms(th)
            last = last or created
            recent = bool(last and t_now - last < fx.OPEN_SECS * 1000)
            ro = None
            if path and (recent or th["id"] in vira_by_sid):
                ro = self.rollouts.get(path) or self.rollouts.setdefault(path, fx.Rollout(path))
                ro.poll()
                used.add(path)
            turn = bool(ro and ro.running(t_now))
            kids_live = []
            for ch in children.get(th["id"], []):
                cp = ch.get("rollout_path")
                cl = self._rollout_last(cp) if cp else None
                if cp and cl and t_now - cl < fc.SUBAGENT_LIVE_SECS * 1000:
                    cro = self.rollouts.get(cp) or self.rollouts.setdefault(cp, fx.Rollout(cp))
                    cro.poll()
                    used.add(cp)
                    if cro.running(t_now):
                        cur = cro.now_tool()
                        kids_live.append(dict(
                            what=ch.get("agent_nickname") or ch.get("agent_path") or "Subagent",
                            startedAt=cro.turn_started, lastAt=cro.last_at, tools=cro.turn_tools,
                            now=f"{cur['name']}: {cur['desc']}" if cur else None))
            live = turn or bool(kids_live) or (running and recent)
            if not live and (not last or t_now - last > WEEK_MS) and th["id"] not in vira_by_sid:
                continue
            waiting = bool(ro and ro.question and turn)
            status = status_of(turn, waiting, kids_live, [], live)
            project, repo = project_of(th.get("cwd"))
            title = (names.get(th["id"]) or th.get("name") or th.get("title")
                     or fc.clip(th.get("first_user_message") or th.get("preview") or "", 90) or "Untitled")
            model = (ro.model if ro and ro.model else th.get("model")) or ""
            row = base_row(
                id=th["id"], tool="codex", surface=fx.surface_of(th.get("source")),
                project=project, repo=repo, folder=th.get("cwd"), branch=th.get("git_branch"),
                title=fc.clip(title, 120), openUrl=fx.open_url(th["id"]), status=status, live=live,
                startedAt=created, createdAt=created, lastAt=last,
                turnOpen=turn, turnStartedAt=ro.turn_started if ro and turn else None,
                turnTools=ro.turn_tools if ro else 0, turnOrigin="you" if turn else None,
                asked=ro.asked if ro else None, lastText=ro.last_text if ro else None,
                now=ro.now_tool() if ro and turn else None,
                question=ro.question if waiting else None, plan=ro.plan_view() if ro else None,
                context=dict(used=ro.context if ro else None,
                             limit=(ro.context_limit if ro else None) or None,
                             compactions=ro.compactions if ro else 0),
                agents=dict(live=kids_live, total=len(children.get(th["id"], []))),
                model=model, effort=th.get("reasoning_effort") or "",
                feed=ro.feed_view() if ro else [],
                _parent=ro.forked_from if ro else None, _link="fork" if ro and ro.forked_from else None,
                _sid=th["id"])
            job = vira_by_sid.get(th["id"])
            if job:
                merge_into_job(job, row)
                continue
            rows.append(row)
        for k in [k for k in self.rollouts if k not in used]:
            del self.rollouts[k]
        return rows

    # ----- Vira

    def _vira(self, t_now):
        try:
            live_rows, ledger = self._ledger()
        except Exception as e:  # noqa: BLE001 - a broken ledger is a named note
            self.notes["vira"] = f"could not read the job ledger: {e}"
            return []
        self.notes["vira"] = None
        live_by_id = {r["id"]: r for r in live_rows}
        by_branch = joblog.by_branch_index(ledger)
        rows = []
        for rec in ledger:
            reg = live_by_id.get(rec.get("id"))
            started = _iso_ms(rec.get("started"))
            finished = _iso_ms(rec.get("finished"))
            last = finished or started
            running = bool(reg and reg.get("status") == "running")
            if not running and (not last or t_now - last > WEEK_MS):
                continue
            awaiting = (reg or {}).get("awaiting")
            if not running:
                status = "Closed"
            elif awaiting in ("ask", "permission"):
                status = "Needs you"
            elif awaiting == "agents":
                status = "Jobs running"
            elif awaiting in ("reply", "paused"):
                status = "Idle"
            else:
                status = "Working"
            try:
                desc = joblog.describe(rec, None, by_branch)
            except Exception:  # noqa: BLE001 - naming never breaks the list
                desc = {"title": rec.get("title") or "Vira job", "kind_label": "", "pr": None}
            project, repo = project_of(rec.get("cwd"))
            pr = desc.get("pr") or {}
            t = None
            tp = rec.get("transcript")
            if running and tp and os.path.exists(tp) and (rec.get("provider") or "anthropic") == "anthropic":
                t = self._transcript(Path(tp))
            model = (t.model if t and t.model else rec.get("model")) or ""
            rows.append(base_row(
                id=f"vira:{rec['id']}", tool="vira", surface=desc.get("kind_label") or "Job",
                project=project, repo=repo, folder=rec.get("cwd"),
                worktree=Path(rec["worktree"]).name if rec.get("worktree") else None,
                branch=rec.get("branch") or None, title=fc.clip(desc.get("title") or "Vira job", 140),
                viraJob=rec["id"], status=status, live=running, startedAt=started,
                createdAt=started, lastAt=max(filter(None, [t.last_at if t else None, last]), default=None),
                turnOpen=status == "Working", turnStartedAt=started if status == "Working" else None,
                turnTools=t.turn_tools if t else 0, asked=fc.clip(rec.get("command") or "", 280) or None,
                lastText=fc.clip(desc.get("outcome") or "", 280) or None,
                now=t.now_tool() if t else None, plan=t.plan_view() if t else None,
                question=(dict(kind="ask", at=last, questions=[dict(
                    question="Waiting for your answer in Vira", header="Vira", multi=False, options=[])])
                    if status == "Needs you" else None),
                context=dict(used=t.context if t else None, limit=fc.context_limit(model),
                             compactions=t.compactions if t else 0) if t else None,
                feed=t.feed_view() if t else [], model=model.replace("claude-", ""),
                effort=rec.get("effort") or "",
                prs=[dict(n=pr.get("number"), state=(pr.get("state") or "OPEN").upper(),
                          url=pr.get("url"))] if pr.get("number") else [],
                _sid=rec.get("session_id") or None, _base="main", _provider=rec.get("provider")))
        return rows

    def _ledger(self):
        try:
            m = joblog.STORE.stat().st_mtime_ns
        except OSError:
            m = None
        live_rows, ledger = None, None
        if self.jobs is vira_jobs:
            from . import session
            try:
                live_rows = session.sessions.recent()
            except Exception:  # noqa: BLE001
                live_rows = []
            if m is not None and self.ledger[0] == m:
                ledger = self.ledger[1]
            else:
                ledger = joblog.recent(300)
                self.ledger = (m, ledger)
            return live_rows, ledger
        return self.jobs()

    # ----- the live tier

    def live(self):
        with self.live_lock:
            return self._live()

    def _live(self):
        t_now = now_ms()
        self._used = set()
        procs, kids = self.procs.read()
        vira = self._vira(t_now)
        vira_by_sid = {r["_sid"]: r for r in vira if r.get("_sid")}
        claude = self._claude(t_now, procs, kids, set(vira_by_sid))
        codex = self._codex(t_now, procs, vira_by_sid)
        rows = vira + claude + codex
        for k in [k for k in self.transcripts if k not in self._used]:
            del self.transcripts[k]
        for r in rows:
            if r.get("repo") and r.get("lastAt"):
                self.roots_seen[r["repo"]] = max(self.roots_seen.get(r["repo"], 0), r["lastAt"])
        self.last_rows = rows
        instances, listener_pids = self._instances(procs)
        families = self._graph(rows)
        shown = {r["id"] for r in rows if r["live"]} | self._shown_closed
        sessions = [r for r in rows if r["live"]]
        nodes = [r for r in rows if not r["live"] and r["id"] in shown]
        sessions.sort(key=lambda s: (STATUS_RANK[s["status"]], -(s["lastAt"] or 0)))
        nodes.sort(key=lambda n: -(n["lastAt"] or 0))
        roots = {r["repo"] for r in rows if r.get("repo")} | {r["folder"] for r in rows if r.get("folder")}
        servers = self._servers(procs, roots, listener_pids)
        totals = dict(open=len(sessions), working=0, needsYou=0, jobs=0, idle=0,
                      subagents=0, shells=0, servers=len(servers), instances=len(instances),
                      all=len(rows), closed24=0)
        key = {"Needs you": "needsYou", "Working": "working", "Jobs running": "jobs", "Idle": "idle"}
        for s in sessions:
            totals[key[s["status"]]] += 1
            totals["subagents"] += len(s["agents"]["live"])
            totals["shells"] += len(s["shells"])
        closed = [n for n in rows if not n["live"] and n.get("lastAt") and t_now - n["lastAt"] < DAY_MS]
        totals["closed24"] = len(closed)
        by_tool = {}
        for s in sessions:
            by_tool.setdefault(s["tool"], dict(open=0, working=0, needsYou=0, jobs=0, idle=0))
            by_tool[s["tool"]]["open"] += 1
            by_tool[s["tool"]][key[s["status"]]] += 1
        out = dict(
            snapshotAt=t_now, totals=totals, byTool=by_tool,
            sessions=[public(s) for s in sessions], nodes=[public(n) for n in nodes],
            families=families,
            closedToday=[dict(id=n["id"], title=n["title"], project=n["project"], lastAt=n["lastAt"],
                              tool=n["tool"]) for n in sorted(closed, key=lambda n: -n["lastAt"])[:12]],
            closedTodayCount=len(closed), servers=servers, instances=instances,
            sources=self.sources(), self=self_meta())
        self.last_live = out
        return out

    def sources(self):
        n = self.notes
        return dict(
            claude=dict(found=not n.get("claude"), note=n.get("claude")),
            claudeApp=dict(found=not n.get("claudeApp"), note=n.get("claudeApp")),
            codex=dict(found=not n.get("codex"), note=n.get("codex")),
            vira=dict(found=not n.get("vira"), note=n.get("vira")),
            processes=dict(found=not self.procs.note, note=self.procs.note),
            git=dict(found=bool(fg.git_bin()), note=None if fg.git_bin() else "git not found on this machine"),
            gh=dict(found=bool(fg.gh_bin()), note=None if fg.gh_bin() else "gh not found on this machine"))

    # ----- families: linked sessions, and sessions sharing a branch or PR

    def _graph(self, rows):
        by_id = {r["id"]: r for r in rows}
        open_ids = {r["id"] for r in rows if r["live"]}
        parent_of = {}
        for r in rows:
            p = r.get("_parent")
            if p and p in by_id and p != r["id"]:
                parent_of[r["id"]] = (p, r.get("_link") or "task")
        children = {}
        for c, (p, _) in parent_of.items():
            children.setdefault(p, []).append(c)
        t_now = now_ms()
        recent = sorted((r for r in rows if not r["live"] and r.get("lastAt")
                         and t_now - r["lastAt"] < CLOSED_WINDOW_MS),
                        key=lambda r: -r["lastAt"])[:GRAPH_MAX_CLOSED]
        shown = set(open_ids) | {r["id"] for r in recent}
        frontier = list(shown)
        while frontier:
            sid = frontier.pop()
            rel = ([parent_of[sid][0]] if sid in parent_of else []) + children.get(sid, [])
            for x in rel:
                if x not in shown:
                    shown.add(x)
                    frontier.append(x)
        self._shown_closed = shown - open_ids

        uf = {s: s for s in shown}

        def find(x):
            while uf[x] != x:
                uf[x] = uf[uf[x]]
                x = uf[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                uf[rb] = ra

        for sid in shown:
            if sid in parent_of and parent_of[sid][0] in shown:
                union(sid, parent_of[sid][0])
        owner = {}
        for sid in shown:
            r = by_id[sid]
            base = r.get("_base") or "main"
            keys = [f"pr:{r.get('repo')}#{p['n']}" for p in r.get("prs") or [] if p.get("n")]
            keys += [f"br:{r.get('repo')}:{b}" for b in self._branches_of(r)
                     if b not in BASE_NAMES and b != base]
            for k in keys:
                if k in owner:
                    union(owner[k], sid)
                else:
                    owner[k] = sid

        for sid in shown:
            r = by_id[sid]
            p = parent_of.get(sid)
            r["parentId"] = p[0] if p and p[0] in shown else None
            r["link"] = p[1] if p else None
            r["family"] = find(sid)
            r["git"] = dict(branch=r["branch"] if r.get("branch") not in BASE_NAMES else None,
                            base=r.get("_base") or "main", worktree=r.get("worktree"),
                            prs=r.get("prs") or [],
                            written=[b for b in r.get("_written") or [] if b not in BASE_NAMES][:12])

        fams = {}
        for sid in shown:
            fams.setdefault(find(sid), []).append(sid)
        families = []
        for fid, members in fams.items():
            roots = [m for m in members if not (m in parent_of and parent_of[m][0] in members)]
            root = min(roots or members, key=lambda m: by_id[m].get("createdAt") or 0)
            branches = self._family_branches([by_id[m] for m in members])
            rank = {"open_pr": 0, "unmerged": 1, "unknown": 2, "deleted": 3, "merged": 4}
            bl = sorted(branches.values(), key=lambda b: (rank[b["state"]], b["name"]))
            prs = [b["pr"] for b in bl if b["pr"]]
            active = any(m in open_ids and by_id[m]["status"] in ("Working", "Needs you", "Jobs running")
                         for m in members)
            families.append(dict(
                id=fid, root=root, title=by_id[root]["title"], project=by_id[root]["project"],
                tool=by_id[root]["tool"],
                members=sorted(members, key=lambda m: by_id[m].get("createdAt") or 0),
                open=sum(1 for m in members if m in open_ids), branches=bl[:60], branchCount=len(bl),
                merged=sum(1 for b in bl if b["merged"] is True),
                prsOpen=sum(1 for p in prs if p["state"] == "OPEN"),
                prsMerged=sum(1 for p in prs if p["state"] == "MERGED"),
                needsWork=sum(1 for b in bl if b["state"] in ("open_pr", "unmerged")),
                deleted=sum(1 for b in bl if b["state"] == "deleted"),
                kind="feature" if (bl or len(members) > 1) else "session",
                state=("active" if active else "unmerged" if any(b["state"] in ("open_pr", "unmerged") for b in bl)
                       else "done" if bl else "no_code"),
                lastAt=max((by_id[m].get("lastAt") or 0) for m in members)))
        families.sort(key=lambda f: (-f["open"], -f["lastAt"]))
        return families

    def _branches_of(self, r):
        out = [r["branch"]] if r.get("branch") else []
        out += [b for b in r.get("_written") or [] if b not in out]
        return out

    def _family_branches(self, members):
        branches = {}
        for r in members:
            repo = r.get("repo")
            base = r.get("_base") or "main"
            scan = self.scans.get(repo) if repo else None
            gh_by_branch = {p["branch"]: p for p in (scan or {}).get("prs") or [] if p.get("branch")}
            for p in r.get("_prs") or [dict(n=x["n"], state=x["state"], url=x["url"], branch=r.get("branch"))
                                        for x in r.get("prs") or []]:
                b = p.get("branch") or f"PR #{p['n']}"
                cur = branches.setdefault(b, dict(name=b, pr=None, merged=None, sessions=[], repo=repo, base=base))
                if r["id"] not in cur["sessions"]:
                    cur["sessions"].append(r["id"])
                state = (gh_by_branch.get(b) or {}).get("state") or p["state"]
                if not cur["pr"] or state == "MERGED":
                    cur["pr"] = dict(n=p["n"], state=state, url=p.get("url"))
                if state == "MERGED":
                    cur["merged"] = True
            for b in self._branches_of(r):
                if b in BASE_NAMES or b == base:
                    continue
                cur = branches.setdefault(b, dict(name=b, pr=None, merged=None, sessions=[], repo=repo, base=base))
                if r["id"] not in cur["sessions"]:
                    cur["sessions"].append(r["id"])
                gp = gh_by_branch.get(b)
                if gp and not cur["pr"]:
                    cur["pr"] = dict(n=gp["n"], state=gp["state"], url=gp.get("url"))
                    if gp["state"] == "MERGED":
                        cur["merged"] = True
        for b in branches.values():
            scan = self.scans.get(b.pop("repo", None))
            if scan:
                local = fg.merged_state(scan, b["name"])
                remotes = scan.get("remotes") or {}
                if b["merged"] is None:
                    b["merged"] = local[1] if local[0] else remotes.get(b["name"])
                b["exists"] = local[0] or b["name"] in remotes
                b["ahead"] = None if b["merged"] is True or not local[0] else local[2]
            else:
                b["exists"], b["ahead"] = None, None
            if b["pr"] and b["pr"]["state"] == "OPEN":
                b["state"] = "open_pr"
            elif b["merged"] is True:
                b["state"] = "merged"
            elif b["exists"] is False:
                b["state"] = "deleted"
            elif b["merged"] is False:
                b["state"] = "unmerged"
            else:
                b["state"] = "unknown"
        return branches

    # ----- dev servers and Vira instances

    def _instances(self, procs):
        if time.time() - self.inst[2] < INSTANCE_SECS:
            return self.inst[0], self.inst[1]
        ports = listening_ports()
        me = None
        try:
            me = int(re.search(r":(\d+)$", instance.api_url()).group(1))
        except (ValueError, AttributeError):
            me = None
        cands = ([p for p in INSTANCE_PORTS if p in ports] if ports is not None else list(INSTANCE_PORTS))
        wt_by_port = {}
        for scan in list(self.scans.values()):
            for w in scan.get("worktrees") or []:
                try:
                    meta = json.loads((Path(w["path"]) / ".test-instance.json").read_text(encoding="utf-8"))
                    wt_by_port[int(meta["port"])] = (w, meta)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        out, pids = [], set()
        for port in cands:
            body = dict(instance.metadata(), self=True) if port == me else probe_instance(
                port, timeout=0.6 if ports is not None else 0.15)
            if not body:
                continue
            pid = (ports or {}).get(port)
            if port == me:
                pid = os.getpid()
            wt, meta = wt_by_port.get(port, (None, None))
            folder = wt["path"] if wt else None
            if port == me:
                folder = str(instance.ROOT)
            elif body.get("kind") == "primary":
                folder = str(instance.primary_root())
            pid = pid or (meta or {}).get("pid")
            p = procs.get(pid) if pid else None
            if pid:
                pids.add(pid)
            out.append(dict(port=port, id=body.get("id"), kind=body.get("kind"),
                            url=f"http://localhost:{port}", self=port == me, pid=pid,
                            startedAt=(now_ms() - p["secs"] * 1000) if p else None,
                            cpu=p["cpu"] if p else None, memMB=round(p["rss"] / 1024) if p else None,
                            worktree=folder, branch=wt["branch"] if wt else None))
        self.inst = (out, pids, time.time())
        return out, pids

    def _servers(self, procs, roots, skip):
        roots = [r for r in roots if r and r not in (str(Path.home()), "/")]
        out = []
        for p in procs.values():
            if p["ppid"] != 1 or p["pid"] in skip or p["pid"] == os.getpid():
                continue
            if p["args"].startswith(("/Applications/", "/System/", "/usr/libexec/", "/Library/")):
                continue
            if not re.search(r"\b(python[\d.]*|node|npm|npx|uvicorn|vite|deno|bun|ruby|go)\b", p["args"], re.I):
                continue
            cwd = self.procs.cwd_of(p) or ""
            if not any(cwd.startswith(r) for r in roots) and not any(r in p["args"] for r in roots):
                continue
            port = re.search(r"--port[= ](\d+)", p["args"]) or re.search(r":(\d{4,5})\b", p["args"])
            out.append(dict(pid=p["pid"], cmd=fc.short_cmd(p["args"]),
                            port=int(port.group(1)) if port else None,
                            folder=Path(cwd).name if cwd else None,
                            startedAt=now_ms() - p["secs"] * 1000, cpu=p["cpu"],
                            memMB=round(p["rss"] / 1024)))
        out.sort(key=lambda s: s["startedAt"])
        return out

    # ----- the slow tier

    def repo_roots(self):
        roots = dict(self.roots_seen)
        own = fg.repo_root(str(changelog.REPO))
        if own:
            roots[own] = max(roots.get(own, 0), now_ms())
        return [r for r, _ in sorted(roots.items(), key=lambda kv: -kv[1])][:REPOS_MAX]

    def repos_due(self):
        t = time.time()
        for root in self.repo_roots():
            sig, at = self.sigs.get(root, (None, 0))
            if t - at > fg.REPO_SECS or fg.signature(root) != sig:
                return True
        return self.last_repos is None

    def repos(self, force=False):
        with self.slow_lock:
            return self._repos(force)

    def _repos(self, force):
        t_now = now_ms()
        roots = self.repo_roots()
        for root in roots:
            sig, at = self.sigs.get(root, (None, 0))
            cur = fg.signature(root)
            if force or time.time() - at > fg.REPO_SECS or cur != sig or root not in self.scans:
                scan = fg.scan(root, self.gh, t_now)
                rc, remotes = fg.git(root, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
                rmerged = set()
                if scan.get("base"):
                    rc2, text = fg.git(root, "branch", "-r", "--format=%(refname:short)", "--merged", scan["base"])
                    rmerged = {x.strip() for x in text.splitlines()} if rc2 == 0 else set()
                scan["remotes"] = {x.strip().removeprefix("origin/"): (x.strip() in rmerged)
                                   for x in remotes.splitlines()
                                   if x.strip() and x.strip() != "origin/HEAD"} if rc == 0 else {}
                self.scans[root] = scan
                self.sigs[root] = (cur, time.time())
        for k in [k for k in self.scans if k not in roots]:
            del self.scans[k]
            self.sigs.pop(k, None)
        exclude = {r["_sid"] for r in self.last_rows if r.get("tool") == "vira" and r.get("_sid")}
        self.call_rows, self.cli_seen = self.calls.scan(t_now, exclude)
        return self._repos_view(t_now)

    def _repos_view(self, t_now):
        rows = self.last_rows
        live = [r for r in rows if r["live"]]
        own = fg.repo_root(str(changelog.REPO))
        repos = []
        for root, scan in list(self.scans.items()):
            repos.append(label_repo(scan, live, self.inst[0], t_now))
        repos.sort(key=lambda r: -self.roots_seen.get(r["root"], 0))
        week = [dict(id=r["id"], tool=r["tool"], title=r["title"], project=r["project"],
                     start=r.get("startedAt") or r.get("createdAt") or r.get("lastAt"),
                     end=r.get("lastAt") or r.get("startedAt"), live=r["live"], status=r["status"])
                for r in rows if (r.get("lastAt") or 0) > t_now - WEEK_MS]
        week.sort(key=lambda w: w["start"] or 0)
        return dict(at=t_now, repos=repos, calls=self.call_rows, week=week,
                    viraRepo=own, viraLog=self._vira_log(t_now))

    def _vira_log(self, t_now):
        """Vira's own change log (retros, ideas, jobs by local day), shown
        beside the landings in Vira's lane."""
        at, groups = self.vira_log
        if groups is not None and time.time() - at < CHANGELOG_SECS:
            return groups
        try:
            built = changelog.groups()
        except Exception as e:  # noqa: BLE001 - a broken source is a note, not a crash
            log("changelog failed:", repr(e))
            built = []
        cutoff = time.strftime("%Y-%m-%d", time.localtime((t_now - TIMELINE_DAYS * DAY_MS) / 1000))
        groups = []
        for g in built:
            if g["date"] < cutoff:
                continue
            # A dropped idea is a decision not to change anything; the
            # timeline is what changed.
            groups.append(dict(date=g["date"], goal=g.get("goal") or "", entries=[
                dict(text=fc.clip(e.get("text") or "", 300), kind=e.get("kind"), ts=e.get("ts"),
                     source=e.get("source")) for e in g["entries"][:40] if e.get("kind") != "dropped"]))
        self.vira_log = (time.time(), groups)
        return groups

    # ----- everything at once

    def snapshot(self):
        with admission.cpu("fleet"):
            live = self.live()
        repos = self.last_repos if self.last_repos and not self.repos_due() else self.repos()
        self.last_repos = repos
        return dict(live, repos=repos)


def label_repo(scan, live_rows, instances, t_now):
    """A scan plus who is where: the sessions in each worktree, the Vira
    instance serving it, and its status label."""
    out = {k: v for k, v in scan.items() if k not in ("heads", "remotes")}
    real = lambda p: os.path.normcase(os.path.realpath(p)).rstrip(os.sep)
    paths = [real(o["path"]) for o in scan.get("worktrees") or []]
    folders = [(r, real(r["folder"])) for r in live_rows if r.get("folder")]
    wts = []
    for w in scan.get("worktrees") or []:
        path = real(w["path"])
        # A worktree nested in another (app worktrees live inside the main
        # checkout) owns the sessions in it; the outer one does not.
        inner = [o for o in paths if o != path and o.startswith(path + os.sep)]
        here = [r for r, f in folders if (f == path or f.startswith(path + os.sep))
                and not any(f == o or f.startswith(o + os.sep) for o in inner)]
        inst = next((i for i in instances if i.get("worktree") and real(i["worktree"]) == path), None)
        if w["kind"] == "main":
            label = "Live"
        elif any(r["status"] != "Closed" for r in here):
            label = "In use"
        elif w.get("dirty"):
            label = "Uncommitted changes"
        elif w.get("merged") is False and (w.get("ahead") or 0) > 0:
            label = "Unlanded commits"
        elif w.get("merged") is True or (w.get("ahead") == 0):
            label = "Landed, can remove"
        else:
            label = "Unknown"
        wts.append(dict(w, sessions=[r["id"] for r in here], instance=inst, label=label))
    out["worktrees"] = wts
    out["branches"] = [dict(b, label=("Landed, can remove" if b.get("merged") or b.get("ahead") == 0
                                      else "Unlanded commits"))
                       for b in scan.get("branches") or []]
    return out


def codex_last_real(path):
    """The last real event's time in a rollout, skipping the reload events
    the Codex app appends to every thread."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - fc.LAST_TS_TAIL))
            data = f.read()
    except OSError:
        return None
    for line in reversed(data.split(b"\n")):
        if not line.strip() or b"thread_settings_applied" in line:
            continue
        try:
            t = fc.ts_ms(json.loads(line).get("timestamp"))
        except (ValueError, AttributeError):
            continue
        if t:
            return t
    return None


def merge_into_job(job, codex_row):
    """A Codex thread Vira itself ran is shown once, as the Vira job, with
    the thread's live detail."""
    for k in ("feed", "plan", "now", "context", "lastText", "turnTools", "model", "effort"):
        if codex_row.get(k):
            job[k] = codex_row[k]
    if job["live"] and codex_row.get("lastAt"):
        job["lastAt"] = max(job.get("lastAt") or 0, codex_row["lastAt"])
    job["codexThread"] = codex_row["id"]


def _iso_ms(iso):
    if not iso:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()     # the ledger writes local time
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def self_meta():
    try:
        return dict(instance.metadata(), pid=os.getpid())
    except ValueError:
        return dict(id=instance.id(), kind="branch" if instance.is_branch() else "primary",
                    pid=os.getpid())


def public(r):
    return {k: v for k, v in r.items() if not k.startswith("_")}


# ---------------------------------------------------------------- delivery

class Hub:
    """Pushes Fleet to every open window over /api/fleet/stream. The loop
    runs only while at least one window is connected."""

    def __init__(self, fleet):
        self.fleet = fleet
        self.subs = set()
        self.task = None
        self.live_json = None
        self.repos_json = None
        self.live_cmp = None
        self.repos_cmp = None
        self.ticks = 0
        self.busy_s = 0.0

    def subscribe(self):
        q = asyncio.Queue(maxsize=8)
        self.subs.add(q)
        if self.live_json:
            q.put_nowait(("fleet", self.live_json))
        if self.repos_json:
            q.put_nowait(("fleetrepos", self.repos_json))
        if not self.task or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self.run())
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)

    def _send(self, event, data):
        for q in list(self.subs):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                # A window that stopped reading gets the newest frame only.
                while not q.empty():
                    q.get_nowait()
                q.put_nowait((event, data))

    async def run(self):
        slow = None
        while self.subs:
            t0 = time.monotonic()
            try:
                live = await asyncio.to_thread(self._tick_live)
                if live is not None:
                    body = json.dumps(dict(live, snapshotAt=0), separators=(",", ":"))
                    if body != self.live_cmp:
                        self.live_cmp = body
                        self.live_json = json.dumps(live, separators=(",", ":"))
                        self._send("fleet", self.live_json)
                    else:
                        self._send("tick", json.dumps({"at": live["snapshotAt"]}))
                # The slow tier runs beside the live one, never in its way:
                # a first scan with gh can take seconds.
                if slow is None or slow.done():
                    slow = asyncio.get_running_loop().create_task(self._slow(self.ticks % 5 == 0))
            except Exception as e:  # noqa: BLE001 - one bad tick never ends the stream
                log("tick failed:", repr(e))
            self.ticks += 1
            self.busy_s += time.monotonic() - t0
            await asyncio.sleep(max(0.2, LIVE_SECS - (time.monotonic() - t0)))
        self.task = None

    async def _slow(self, relabel):
        try:
            repos = await asyncio.to_thread(self._tick_repos, relabel)
        except Exception as e:  # noqa: BLE001
            log("repos failed:", repr(e))
            return
        if repos is None:
            return
        body = json.dumps(dict(repos, at=0), separators=(",", ":"))
        if body != self.repos_cmp:
            self.repos_cmp = body
            self.repos_json = json.dumps(repos, separators=(",", ":"))
            self._send("fleetrepos", self.repos_json)

    def _tick_live(self):
        try:
            with admission.cpu("fleet"):
                return self.fleet.live()
        except admission.Full:
            return None

    def _tick_repos(self, relabel):
        """A rescan when git changed or REPO_SECS passed; otherwise, every
        few ticks, the same scan relabelled with who sits where now."""
        if self.fleet.repos_due():
            repos = self.fleet.repos()
        elif not relabel:
            return None
        else:
            with self.fleet.slow_lock:
                repos = self.fleet._repos_view(now_ms())
        self.fleet.last_repos = repos
        return repos


FLEET = Fleet()
HUB = Hub(FLEET)

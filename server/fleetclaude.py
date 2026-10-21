"""Fleet's Claude Code reader: what each Claude Code session on this machine
is doing, read from the files Claude Code itself writes.

Three kinds of transcript live under ~/.claude/projects/<slugged cwd>/:

- Desktop-app sessions. The app also keeps a record per session under its
  support folder (claude-code-sessions/*/*/local_*.json) with the title,
  branch, PRs, fork and spawn links, and a post-turn summary.
- Terminal (CLI) sessions: a transcript whose `entrypoint` is `cli` and no
  app record.
- Headless SDK calls (`sdk-cli`, `sdk-py`): one prompt, one answer. They are
  NOT sessions. They outnumbered real sessions about 10 to 1 when this was
  built (Vira's library tagger alone ran every ten minutes from every running
  instance), so they are kept out of the session list and counted instead
  (CallIndex below).

Ported from the session-fleet collector (~/.claude/session-fleet/fleet.py),
which drove the Session Fleet page before Fleet existed: the incremental
transcript reader, the status rules, subagents, shells, plan progress and
context use are its logic, kept close to how it was written there.

Read-only: nothing here writes a byte to Claude Code's files.

Two pitfalls this module exists to get right:

- Last activity is the timestamp of the last line that HAS one, never the
  transcript's mtime. The app appends untimestamped lines (bridge-session,
  cost-state, last-prompt) to idle sessions, and an mtime read showed an idle
  session as working.
- The app record's `lastActivityAt` is not kept current while a session runs
  (it read the creation time on a session hours old), so a closed session's
  last activity is read from its transcript tail too.
"""
import json
import os
import re
import time
from pathlib import Path

from . import settings, toolresult

# Bytes read from a transcript the first time it is seen. Sessions run to
# hundreds of megabytes; the tail holds the open turn, the plan and the
# feed, and older lines only ever changed state that a later line resets.
INITIAL_TAIL = 8_000_000
# A subagent transcript written this recently, and not finished, is running.
SUBAGENT_LIVE_SECS = 120
# Bytes read from the end of a closed session's transcript to find its last
# timestamped line. Untimestamped app lines are short; 64 KB spans many.
LAST_TS_TAIL = 65_536
# Lines read from the head of a transcript to learn what kind it is. The
# entrypoint and first prompt sit in the first few lines.
HEAD_LINES = 40
FEED_LEN = 10
# The agent's own status lines kept beside the feed ("263 tests pass.
# Running the full suite"): what it says it is doing, between its calls.
SAYS_LEN = 5
END_REASONS = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
ASK_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
APP_ENTRY = "claude-desktop"
CLI_ENTRY = "cli"


# ---------------------------------------------------------------- sources

def claude_dir():
    """Claude Code's own folder; CLAUDE_CONFIG_DIR moves it, as it does
    for Claude Code itself."""
    v = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(v).expanduser() if v else Path.home() / ".claude"


def projects_dir():
    return claude_dir() / "projects"


def app_dir():
    """The Claude desktop app's support folder. VIRA_FLEET_CLAUDE_APP_DIR
    points it elsewhere (an empty folder reads as "not found")."""
    v = os.environ.get("VIRA_FLEET_CLAUDE_APP_DIR")
    if v:
        return Path(v).expanduser()
    if settings.IS_MAC:
        return Path.home() / "Library" / "Application Support" / "Claude"
    if settings.IS_WIN:
        return Path(os.environ.get("APPDATA") or Path.home()) / "Claude"
    return Path.home() / ".config" / "Claude"


def sessions_root():
    return app_dir() / "claude-code-sessions"


# ---------------------------------------------------------------- helpers

def clip(s, n=200):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def ts_ms(iso):
    if not iso:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def context_limit(model):
    m = (model or "").lower()
    if "[1m]" in m or re.search(r"(opus|sonnet|fable|haiku)-5", m):
        return 1_000_000
    return 200_000


def slug(cwd):
    """The folder name Claude Code files a cwd's transcripts under."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd or ""))


def user_text(content):
    """The words a person typed, minus harness tags; None for tool results."""
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result"
               for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    content = re.sub(r"<system-reminder>.*?</system-reminder>", " ",
                     content, flags=re.S)
    content = re.sub(r"<[a-z_-]+(?: [^>]*)?>.*?</[a-z_-]+>", " ",
                     content, flags=re.S).strip()
    return content or None


def merge_feed(feed, says):
    """Tool calls and status lines, newest first. A line older than the
    oldest call kept has lost its context, so it is left out."""
    calls = [{k: v for k, v in f.items() if k != "id"} for f in feed]
    floor = calls[0]["at"] if len(calls) >= FEED_LEN else 0
    rows = calls + [dict(s) for s in says if s["at"] >= floor]
    rows.sort(key=lambda r: r["at"] or 0, reverse=True)
    return rows


def add_say(says, text, at):
    """Keep a status line, once: some sources write the same message as
    more than one event."""
    if not text or (says and says[-1]["say"] == text):
        return
    says.append({"say": text, "at": at or 0})
    del says[:-SAYS_LEN]


# What a tool call is for, in a word the window draws as an animated icon,
# so a glance at the board says "those three are testing, those two are
# editing". A shell command is read from the command itself, first match
# wins: running tests outranks everything a test command also does.
_TESTING = re.compile(r"\b(?:unittest|pytest|jest|vitest|mocha|tox|rspec|phpunit|preflight"
                      r"|go test|cargo test|(?:npm|yarn|pnpm) (?:run )?test|node tests?/\S+)")
_SHIPPING = re.compile(r"\bgit (?:commit|push|merge|rebase|tag|cherry-pick)\b|\bgh pr\b"
                       r"|branch\.sh (?:pr|merge|discard)\b")
_EDITING = re.compile(r"\bsed -i\b|\.write_text\(|\.write\(|\bapply_patch\b|\bpatch -p"
                      r"|>>? ?[\w./-]+\.(?:py|js|ts|tsx|css|html|md|json|sh|ya?ml|toml)\b")
_READING = re.compile(r"^\s*(?:cd \S+ && )?(?:grep|rg|ag|sed -n|cat|head|tail|less|ls|find|wc|tree|jq|file"
                      r"|git (?:log|show|diff|status|blame|grep|branch|worktree list))\b")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "str_replace_editor"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "view_image", "ReadMcpResourceTool"}
WEB_TOOLS = {"WebSearch", "WebFetch", "web_search", "web_fetch"}
AGENT_TOOLS = {"Agent", "Task", "spawn_agent"}


def tool_kind(name, inp):
    """testing, shipping, editing, reading, researching, delegating or
    running: what a call is for."""
    inp = inp if isinstance(inp, dict) else {}
    if name in EDIT_TOOLS:
        return "editing"
    if name in READ_TOOLS:
        return "reading"
    if name in WEB_TOOLS or "Browser" in name or "claude-in-chrome" in name:
        return "researching"
    if name in AGENT_TOOLS:
        return "delegating"
    if name in toolresult.SHELL_TOOLS:
        cmd = inp.get("command") or inp.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd)
        for kind, rx in (("testing", _TESTING), ("shipping", _SHIPPING),
                         ("editing", _EDITING), ("reading", _READING)):
            if rx.search(cmd):
                return kind
    return "running"


def describe_tool(name, inp):
    short = (name.replace("mcp__", "").split("__")[-1]
             if name.startswith("mcp__") else name)
    inp = inp if isinstance(inp, dict) else {}
    detail = (inp.get("description") or inp.get("command")
              or inp.get("subject") or inp.get("prompt")
              or inp.get("file_path") or inp.get("pattern")
              or inp.get("query") or inp.get("url") or inp.get("action")
              or inp.get("title") or "")
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail)[:120]
    detail = str(detail)
    if inp.get("file_path") and not inp.get("description"):
        detail = Path(str(inp["file_path"])).name
    return short, clip(detail, 140)


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):      # some records hold a repr'd list
        try:
            import ast
            x = ast.literal_eval(v)
            return x if isinstance(x, list) else []
        except (ValueError, SyntaxError):
            return []
    return []


def last_timestamp(path):
    """The timestamp (ms) of the last line in a transcript that has one."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - LAST_TS_TAIL))
            data = f.read()
    except OSError:
        return None
    for line in reversed(data.split(b"\n")):
        if b'"timestamp"' not in line:
            continue
        try:
            t = ts_ms(json.loads(line).get("timestamp"))
        except ValueError:
            continue
        if t:
            return t
    return None


def read_head(path):
    """What kind of transcript this is, from its first lines: entrypoint,
    session id, cwd, first prompt, first timestamp."""
    out = {"entrypoint": None, "sessionId": None, "cwd": None,
           "prompt": None, "at": None, "branch": None}
    try:
        with open(path, "rb") as f:
            for i, line in enumerate(f):
                if i >= HEAD_LINES:
                    break
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                out["entrypoint"] = out["entrypoint"] or r.get("entrypoint")
                out["sessionId"] = out["sessionId"] or r.get("sessionId")
                out["cwd"] = out["cwd"] or r.get("cwd")
                out["branch"] = out["branch"] or r.get("gitBranch")
                out["at"] = out["at"] or ts_ms(r.get("timestamp"))
                if out["prompt"] is None and r.get("type") == "user":
                    txt = user_text((r.get("message") or {}).get("content"))
                    if txt:
                        out["prompt"] = txt[:400]
                if out["entrypoint"] and out["prompt"] and out["at"]:
                    break
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- transcripts

class Transcript:
    """Incremental reader of one session's main transcript."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = None
        self.inode = None
        self.buf = b""
        self.reset_state()

    def reset_state(self):
        self.turn_open = False
        self.turn_started = None
        self.turn_tools = 0
        self.turn_origin = None
        self.asked = None
        self.last_text = None
        self.last_at = None
        self.context = None
        self.model = None
        self.compactions = 0
        self.pending = {}          # tool_use id -> {name, desc, at}
        self.feed = []             # recent tool calls
        self.says = []             # recent status lines from the agent
        self.plan = None           # [{content, status}]
        self.blocked = False       # wrap_up said the session waits on the person
        self.question = None       # a pending AskUserQuestion / ExitPlanMode
        self.wrap = None           # the end-of-turn ask, until answered
        self.cwd = None
        self.branch = None
        self.entrypoint = None

    def poll(self):
        try:
            st = self.path.stat()
        except OSError:
            return
        if (self.offset is None or st.st_ino != self.inode
                or st.st_size < self.offset):
            self.inode = st.st_ino
            self.reset_state()
            self.offset = max(0, st.st_size - INITIAL_TAIL)
            self.buf = b""
            skip_first = self.offset > 0
        else:
            skip_first = False
        if st.st_size == self.offset:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(st.st_size - self.offset)
        except OSError:
            return
        self.offset = st.st_size
        data = self.buf + data
        lines = data.split(b"\n")
        self.buf = lines.pop()  # partial last line, finished on a later poll
        if skip_first and lines:
            lines = lines[1:]
        for line in lines:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                self.apply(r)

    def apply(self, r):
        ts = r.get("timestamp")
        if r.get("isSidechain"):
            return
        self.cwd = r.get("cwd") or self.cwd
        self.branch = r.get("gitBranch") or self.branch
        self.entrypoint = self.entrypoint or r.get("entrypoint")
        t = r.get("type")
        msg = r.get("message") or {}
        if t == "system" and r.get("subtype") == "compact_boundary":
            self.compactions += 1
        if t == "user":
            self._on_user(r, msg, ts)
        elif t == "assistant":
            u = msg.get("usage") or {}
            ctx = ((u.get("input_tokens") or 0)
                   + (u.get("cache_read_input_tokens") or 0)
                   + (u.get("cache_creation_input_tokens") or 0))
            if ctx:
                self.context = ctx
            if msg.get("model") and not str(msg.get("model")).startswith("<"):
                self.model = msg["model"]
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    self.last_text = clip(b["text"], 280)
                    add_say(self.says, self.last_text, ts_ms(ts))
                elif bt == "tool_use":
                    self.on_tool(b, ts)
            if msg.get("stop_reason") in END_REASONS:
                self.turn_open = False
                self.pending = {k: v for k, v in self.pending.items()
                                if v["name"] in ASK_TOOLS}
        if ts:
            self.last_at = ts_ms(ts) or self.last_at

    def _on_user(self, r, msg, ts):
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    p = self.pending.pop(b.get("tool_use_id"), None)
                    if p and p["name"] in ASK_TOOLS:
                        self.question = None
                    if p:
                        for item in reversed(self.feed):
                            if item.get("id") == b.get("tool_use_id"):
                                item["ms"] = max(0, (ts_ms(ts) or 0) - item["at"])
                                toolresult.for_feed(item, item["name"], b.get("content"),
                                                    bool(b.get("is_error")))
                                break
        txt = user_text(content)
        origin = (r.get("origin") or {}).get("kind", "human")
        if not txt or r.get("isMeta"):
            return
        if txt.startswith("[Request interrupted"):
            self.turn_open = False
            self.pending.clear()
            self.question = None
        elif r.get("promptSource") == "system" or origin != "human":
            if not self.turn_open:
                self.turn_open, self.turn_started, self.turn_tools = True, ts_ms(ts), 0
                self.turn_origin = "background event"
        elif not txt.startswith("Caveat:"):
            self.turn_open, self.turn_started, self.turn_tools = True, ts_ms(ts), 0
            self.turn_origin = "you"
            self.blocked = False
            self.wrap = None
            if len(txt.split()) >= 4 or not self.asked:
                self.asked = clip(txt, 280)

    def on_tool(self, b, ts):
        name, inp, at = b.get("name", ""), b.get("input") or {}, ts_ms(ts) or 0
        if not isinstance(inp, dict):
            inp = {}
        if name == "mcp__ccd_turn__update_plan":
            self.on_plan(inp)
            return
        if name == "mcp__ccd_turn__wrap_up":
            if self.plan:
                for i in self.plan:
                    i["status"] = "completed"
            self.blocked = bool(inp.get("blocked"))
            acts = [dict(label=clip(str(a.get("label") or ""), 40),
                         message=clip(str(a.get("message") or ""), 300))
                    for a in (inp.get("actions") or [])
                    if isinstance(a, dict) and (a.get("label") or a.get("message"))]
            self.wrap = (dict(kind="wrap", at=at, ask=clip(inp.get("ask") or "", 300),
                              recap=clip(inp.get("recap") or "", 400),
                              actions=acts[:3], blocked=self.blocked)
                         if inp.get("ask") else None)
            return
        if name.startswith("mcp__ccd_turn__") or name == "ToolSearch":
            return
        if name == "TodoWrite" and isinstance(inp.get("todos"), list):
            # The CLI's own task list: the same shape the app's plan has.
            self.plan = [{"content": clip(str(i.get("content", "")), 120),
                          "status": i.get("status", "pending")}
                         for i in inp["todos"] if isinstance(i, dict)]
        if name == "AskUserQuestion":
            qs = []
            for q in (inp.get("questions") or [])[:4]:
                if isinstance(q, dict):
                    qs.append(dict(
                        question=clip(q.get("question") or "", 600),
                        header=clip(q.get("header") or "", 30),
                        multi=bool(q.get("multiSelect")),
                        options=[dict(label=clip(o.get("label") or "", 60),
                                      description=clip(o.get("description") or "", 200))
                                 for o in (q.get("options") or [])[:4]
                                 if isinstance(o, dict)]))
            self.question = dict(kind="ask", at=at, questions=qs)
        elif name == "ExitPlanMode":
            self.question = dict(kind="plan", at=at,
                                 plan=clip(inp.get("plan") or "", 1500))
        short, detail = describe_tool(name, inp)
        kind = tool_kind(name, inp)
        self.pending[b.get("id")] = {"name": short, "desc": detail, "at": at, "kind": kind}
        self.turn_tools += 1
        self.feed.append({"id": b.get("id"), "name": short, "desc": detail, "at": at, "kind": kind})
        del self.feed[:-FEED_LEN]

    def on_plan(self, inp):
        if isinstance(inp.get("items"), list):
            self.plan = [{"content": clip(str(i.get("content", "")), 120),
                          "status": i.get("status", "pending")}
                         for i in inp["items"] if isinstance(i, dict)]
            return
        if not self.plan:
            return
        if inp.get("advance"):
            cur = next((i for i in self.plan if i["status"] == "in_progress"), None)
            if cur:
                cur["status"] = "completed"
            nxt = next((i for i in self.plan if i["status"] == "pending"), None)
            if nxt:
                nxt["status"] = "in_progress"
        for n in inp.get("done") or []:
            if isinstance(n, int) and 1 <= n <= len(self.plan):
                self.plan[n - 1]["status"] = "completed"
        s = inp.get("start")
        if isinstance(s, int) and 1 <= s <= len(self.plan):
            for i in self.plan:
                if i["status"] == "in_progress":
                    i["status"] = "pending"
            self.plan[s - 1]["status"] = "in_progress"

    # ----- views shared by every session built from a transcript

    def now_tool(self):
        if not self.pending:
            return None
        p = max(self.pending.values(), key=lambda x: x["at"])
        return dict(name=p["name"], desc=p["desc"], startedAt=p["at"], kind=p.get("kind"))

    def plan_view(self):
        if not self.plan:
            return None
        return dict(done=sum(1 for i in self.plan if i["status"] == "completed"),
                    total=len(self.plan), items=self.plan)

    def feed_view(self):
        return merge_feed(self.feed, self.says)

    def waiting(self):
        """Blocked on the person right now: an open question or plan, or a
        turn that ended saying it waits on them."""
        return (any(p["name"] in ASK_TOOLS for p in self.pending.values())
                or (self.blocked and not self.turn_open))

    def open_question(self):
        if self.question:
            return self.question
        if self.wrap and not self.turn_open:
            return self.wrap
        return None


class SubTranscript(Transcript):
    """Subagent lines are marked isSidechain; read them as a main chain."""

    def apply(self, r):
        r = dict(r)
        r["isSidechain"] = False
        super().apply(r)


class Subagent:
    """Incremental reader of one subagent transcript."""

    def __init__(self, path):
        self.path = Path(path)
        self.t = SubTranscript(path)
        self.what = None
        self.started = None

    def poll(self):
        self.t.poll()
        if self.what is None:
            meta = self.path.with_suffix(".meta.json")
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                self.what = clip(m.get("description") or m.get("agentType") or "", 120) or None
            except (OSError, ValueError, AttributeError):
                pass
            if self.what is None and self.t.asked:
                self.what = clip(self.t.asked, 120)
        if self.started is None:
            try:
                with open(self.path, "rb") as f:
                    first = json.loads(f.readline())
                self.started = ts_ms(first.get("timestamp"))
            except (OSError, ValueError, AttributeError):
                try:
                    self.started = int(self.path.stat().st_ctime * 1000)
                except OSError:
                    self.started = None

    def live(self):
        return self.t.turn_open or bool(self.t.pending)

    def view(self):
        cur = next(reversed(self.t.pending.values()), None) if self.t.pending else None
        return dict(what=self.what or "Subagent", startedAt=self.started,
                    lastAt=self.t.last_at, tools=self.t.turn_tools,
                    now=(f"{cur['name']}: {cur['desc']}" if cur else None))


# ---------------------------------------------------------------- app records

RECORD_KEYS = ("sessionId", "cliSessionId", "cwd", "originCwd", "title", "model",
               "effort", "permissionMode", "createdAt", "lastActivityAt",
               "isArchived", "spawnedFrom", "forkedFromSessionId", "branch",
               "sourceBranch", "worktreeName", "worktreePath", "prs",
               "writtenBranches", "postTurnSummary", "lastTurnReport",
               "gitAnchors", "isStarred", "completedTurns")


def repo_of_record(d):
    """The repository a session works in, from the app's git anchors."""
    anchors = _as_list(d.get("gitAnchors"))
    for a in anchors:
        root = a.get("gitRoot") if isinstance(a, dict) else None
        if root and "/.claude/worktrees/" not in root and "/.worktrees/" not in root:
            return root
    for a in anchors:
        if isinstance(a, dict) and a.get("commonDir"):
            return str(Path(a["commonDir"]).parent)
    return None


def prs_of_record(d, base):
    prs = []
    for p in _as_list(d.get("prs")):
        if isinstance(p, dict) and p.get("prNumber"):
            prs.append(dict(n=p["prNumber"], state=(p.get("state") or "OPEN").upper(),
                            url=p.get("url"), repo=p.get("repo"),
                            branch=p.get("branch"), base=p.get("baseRef") or base))
    return prs


def written_of_record(d):
    written = []
    for w in _as_list(d.get("writtenBranches")):
        if isinstance(w, str) and "\x00" in w:
            b = w.split("\x00", 1)[1]
            if b and b not in written:
                written.append(b)
    return written


def link_of_record(d):
    sf = d.get("spawnedFrom") if isinstance(d.get("spawnedFrom"), dict) else {}
    parent = sf.get("sessionId") or d.get("forkedFromSessionId")
    if not parent:
        return None, None
    kind = "fork" if (sf.get("userFork") or d.get("forkedFromSessionId")) else "task"
    return parent, kind


def summary_of_record(d):
    s = d.get("postTurnSummary") if isinstance(d.get("postTurnSummary"), dict) else {}
    if not s:
        return None
    return dict(category=s.get("status_category"),
                detail=clip(s.get("status_detail") or "", 160),
                needs=clip(s.get("needs_action") or "", 200))


# ---------------------------------------------------------------- processes

def app_process(args):
    """A Claude desktop-app session process (not its disclaimer wrapper)."""
    return ("claude.app/Contents/MacOS/claude" in args
            and "--output-format stream-json" in args
            and "Helpers/disclaimer" not in args)


_CLI_BIN = re.compile(r"(?:^|/)claude(?:\.exe)?(?:\s|$)|@anthropic-ai/claude-code/cli\.js")


def cli_process(args):
    """A terminal `claude` session: the binary, not driven over stream-json
    (that is the app or an SDK call)."""
    if "--output-format stream-json" in args or "--print" in args or " -p " in f" {args} ":
        return False
    head = args.split(" --", 1)[0]
    return bool(_CLI_BIN.search(head)) and "Claude.app/Contents/Helpers" not in args


def resume_id(args):
    m = re.search(r"--(?:resume|session-id)[= ](\S+)|(?:^|\s)-r\s+(\S+)", args)
    return (m.group(1) or m.group(2)) if m else None


def shell_children(proc, procs, kids, now_ms):
    """Shell commands a session is running (the Bash tool's snapshot shells)."""
    def desc(pid):
        out, stack = [], list(kids.get(pid, []))
        while stack:
            c = stack.pop()
            out.append(c)
            stack.extend(kids.get(c, []))
        return out

    shells = []
    for c in kids.get(proc["pid"], []):
        cp = procs.get(c)
        if not cp or "shell-snapshots/snapshot-" not in cp["args"]:
            continue
        tree = [cp["pid"], *desc(cp["pid"])]
        real = [procs[x] for x in tree[1:] if x in procs and not procs[x]["args"].startswith(
            ("/bin/zsh", "sh -c", "/bin/sh", "bash -c", "/bin/bash"))]
        real.sort(key=lambda x: x["pid"])
        if real:
            cmd = short_cmd(real[0]["args"])
        else:
            m = re.search(r"eval '(.+?)' < /dev/null", cp["args"])
            cmd = short_cmd(m.group(1) if m else cp["args"])
        shells.append(dict(cmd=cmd, startedAt=now_ms - cp["secs"] * 1000,
                           cpu=round(sum(procs[x]["cpu"] for x in tree if x in procs), 1),
                           memMB=round(sum(procs[x]["rss"] for x in tree if x in procs) / 1024)))
    return shells


def short_cmd(args):
    a = re.sub(r"/opt/homebrew/\S*?/MacOS/Python", "python", args)
    a = re.sub(r"/(?:opt/homebrew|usr/local)/\S*/bin/(\w[\w.-]*)", r"\1", a)
    a = re.sub(r"/Users/[^/\s]+/", "~/", a)
    a = re.sub(r"/home/[^/\s]+/", "~/", a)
    return clip(a, 160)


# ---------------------------------------------------------------- background calls

# What a headless call was for, from the opening of its prompt. These are the
# openings of Vira's own model prompts (server/doctags.py, ideatags.py, ...),
# so the table names Vira's background work; anything else is "other".
CALL_KINDS = (
    (re.compile(r"^You are tagging DOCUMENTS", re.I), "library tagging"),
    (re.compile(r"^You are tagging items on a personal engineering backlog", re.I), "backlog tagging"),
    (re.compile(r"studying one of the social circles", re.I), "social profile study"),
    (re.compile(r"^You are reviewing a draft", re.I), "draft check"),
    (re.compile(r"release reviewer", re.I), "release review"),
    (re.compile(r"release desk", re.I), "release desk"),
    (re.compile(r"daily brief", re.I), "daily brief"),
    (re.compile(r"(naming|name) the (subjects|regions)|library.{0,40}nam", re.I), "library naming"),
    (re.compile(r"cartographer|map building|asked for a map", re.I), "map building"),
    (re.compile(r"research assistant|answer questions? by", re.I), "research answer"),
    (re.compile(r"research topics?", re.I), "research topics"),
    (re.compile(r"extracting billing evidence", re.I), "receipt extraction"),
    (re.compile(r"auditing whether session retrospectives", re.I), "retro audit"),
    (re.compile(r"drafting reply suggestions", re.I), "reply suggestions"),
    (re.compile(r"MUSE\b", re.I), "idea engine"),
    (re.compile(r"deciding which of .{0,30}cont|pairs of .{0,30}cont", re.I), "contact review"),
)


def call_kind(prompt):
    head = (prompt or "")[:400]
    for rx, label in CALL_KINDS:
        if rx.search(head):
            return label
    return "other"


class CallIndex:
    """Headless SDK calls per folder and kind: calls today, in 7 days, and
    the last one. Each transcript's head is read once; the scan only stats
    the folders after that."""

    def __init__(self):
        self.heads = {}          # path -> (mtime, head)

    def scan(self, now_ms, exclude_ids=(), days=7):
        root = projects_dir()
        out = {}
        sessions = []           # CLI sessions found on the way (not calls)
        if not root.is_dir():
            return [], sessions
        cutoff = now_ms / 1000 - days * 86400
        midnight = _local_midnight(now_ms)
        seen = set()
        try:
            folders = list(os.scandir(root))
        except OSError:
            return [], sessions
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
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m < cutoff:
                    continue
                seen.add(f.path)
                cached = self.heads.get(f.path)
                if not cached or (cached[1].get("entrypoint") is None and cached[0] != m):
                    head = read_head(Path(f.path))
                    self.heads[f.path] = cached = (m, head)
                head = cached[1]
                ep = head.get("entrypoint") or ""
                sid = head.get("sessionId") or Path(f.name).stem
                if ep == CLI_ENTRY:
                    sessions.append(dict(path=f.path, head=head, mtime=m))
                    continue
                if not ep.startswith("sdk") or sid in exclude_ids:
                    continue
                at = head.get("at") or int(m * 1000)
                folder = Path(head.get("cwd") or "").name or slug_name(d.name)
                kind = call_kind(head.get("prompt"))
                row = out.setdefault((folder, kind), dict(
                    folder=folder, cwd=head.get("cwd"), kind=kind,
                    today=0, week=0, last=0))
                row["week"] += 1
                if at >= midnight:
                    row["today"] += 1
                row["last"] = max(row["last"], at)
        for k in [k for k in self.heads if k not in seen]:
            del self.heads[k]
        rows = sorted(out.values(), key=lambda r: (-r["today"], -r["week"], -r["last"]))
        return rows, sessions


def slug_name(folder_name):
    return folder_name.rsplit("-", 1)[-1] or folder_name


def _local_midnight(now_ms):
    t = time.localtime(now_ms / 1000)
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)

"""Fleet's Codex reader: every Codex thread on this machine, from the Codex
app, the VS Code extension and the CLI, read from Codex's own files.

Codex keeps a thread list in a SQLite database under CODEX_HOME (default
~/.codex), named state_<n>.sqlite where the number moves with Codex's
schema. The newest such file is used and its columns are discovered rather
than assumed, so a schema bump degrades to fewer fields, not a crash. The
database is opened read-only (mode=ro); nothing here writes to Codex.

Each thread's real activity comes from the tail of its rollout file, never
from the database's timestamps. Two pitfalls make that necessary:

- `updated_at` is bulk-bumped: when the Codex app reloads, it moves for
  every thread at once (every thread once read the same minute).
- On reload the app also appends a `thread_settings_applied` event to each
  rollout, so even the rollout's last line can be a reload, not work. Those
  events are skipped when reading the last activity.

A turn is running (Codex's own "Working") when the last `task_started`
comes after the last `task_complete` or `turn_aborted`, and the thread did
real work within the last hour. The hour is measured from the last event,
not from `task_started`: Codex turns run for hours (one ran eight), and a
turn Codex lost simply stops writing. Codex runs every thread through one app process, so there is no
per-thread process to say a thread is open: a thread counts as open while a
Codex process is running and the thread did real work within OPEN_SECS.
"""
import glob
import json
import os
import sqlite3
from pathlib import Path

from .fleetclaude import clip, ts_ms

# The first read of a rollout file. Rollouts reach hundreds of megabytes;
# the open turn, its feed and plan sit in the tail.
INITIAL_TAIL = 8_000_000
# An open turn with no event for this long is a turn Codex lost.
TURN_STALE_SECS = 3600
# A thread that did real work this recently, with Codex running, is open.
OPEN_SECS = 3600
FEED_LEN = 10


def codex_home():
    v = os.environ.get("CODEX_HOME")
    return Path(v).expanduser() if v else Path.home() / ".codex"


def state_db():
    """The newest state_*.sqlite, or None."""
    files = glob.glob(str(codex_home() / "state_*.sqlite"))
    if not files:
        return None
    return Path(max(files, key=lambda f: os.path.getmtime(f)))


def thread_names():
    """Thread names from session_index.jsonl (the app's rename store)."""
    out = {}
    try:
        with open(codex_home() / "session_index.jsonl", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("id") and r.get("thread_name"):
                    out[r["id"]] = r["thread_name"]
    except OSError:
        pass
    return out


def _parent_of_source(source):
    """A subagent thread's parent id, from the JSON `source` column."""
    if not source or not str(source).startswith("{"):
        return None
    try:
        s = json.loads(source)
    except ValueError:
        return None
    spawn = ((s.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(s, dict) else {}
    return spawn.get("parent_thread_id")


def surface_of(source):
    s = str(source or "")
    if s == "vscode":
        return "app"
    if s.startswith("{"):
        return "subagent"
    if s in ("cli", "exec"):
        return "CLI"
    return s or "app"


def read_threads(since_ms, ids=()):
    """Threads touched since since_ms (by any of Codex's own clocks), plus
    any thread named in ids. Returns (rows, note): rows are dicts with the
    columns this schema has; note says why there are none."""
    db = state_db()
    if not db:
        return [], "Codex not found on this machine"
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as e:
        return [], f"could not open {db.name}: {e}"
    try:
        cols = {r[1] for r in con.execute("pragma table_info(threads)")}
        if not {"id", "rollout_path"} <= cols:
            return [], f"{db.name} has no threads table this reader knows"
        want = [c for c in ("id", "rollout_path", "cwd", "source", "archived",
                            "git_branch", "title", "name", "model",
                            "reasoning_effort", "tokens_used", "created_at",
                            "created_at_ms", "updated_at", "updated_at_ms",
                            "recency_at_ms", "thread_source", "agent_nickname",
                            "agent_path", "first_user_message", "preview")
                if c in cols]
        clocks = [c for c in ("recency_at_ms", "updated_at_ms", "created_at_ms") if c in cols]
        secs = [c for c in ("updated_at", "created_at") if c in cols]
        conds = [f"{c} >= ?" for c in clocks] + [f"{c} >= ?" for c in secs]
        params = [since_ms] * len(clocks) + [since_ms // 1000] * len(secs)
        ids = [i for i in ids if i]
        if ids:
            conds.append(f"id in ({','.join('?' * len(ids))})")
            params += ids
        where = " or ".join(conds) or "1"
        rows = con.execute(f"select {', '.join(want)} from threads where {where}",
                           params).fetchall()
        return [dict(zip(want, r)) for r in rows], None
    except sqlite3.Error as e:
        return [], f"could not read {db.name}: {e}"
    finally:
        con.close()


def created_ms(row):
    if row.get("created_at_ms"):
        return int(row["created_at_ms"])
    if row.get("created_at"):
        return int(row["created_at"]) * 1000
    return None


class Rollout:
    """Incremental reader of one thread's rollout file."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = None
        self.inode = None
        self.buf = b""
        self.reset_state()

    def reset_state(self):
        self.last_at = None
        self.turn_open = False
        self.turn_started = None
        self.turn_tools = 0
        self.feed = []
        self.pending = {}           # call_id -> {name, desc, at}
        self.plan = None
        self.context = None
        self.context_limit = None
        self.model = None
        self.asked = None
        self.last_text = None
        self.question = None
        self.compactions = 0
        self.forked_from = None

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
            if skip_first:
                self._read_meta()
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
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
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

    def _read_meta(self):
        """The session_meta line at the head (fork source) when only the
        tail is read."""
        try:
            with open(self.path, "rb") as f:
                r = json.loads(f.readline())
            if r.get("type") == "session_meta":
                self.forked_from = (r.get("payload") or {}).get("forked_from_id")
        except (OSError, ValueError, AttributeError):
            pass

    def apply(self, r):
        t = r.get("type")
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        pt = p.get("type")
        at = ts_ms(r.get("timestamp"))
        if t == "event_msg" and pt == "thread_settings_applied":
            return              # a reload of the app, not work
        if t == "session_meta":
            self.forked_from = p.get("forked_from_id") or self.forked_from
        elif t == "turn_context":
            self.model = p.get("model") or self.model
        elif t == "compacted":
            self.compactions += 1
        elif t == "event_msg":
            self._on_event(pt, p, at)
        elif t == "response_item":
            self._on_item(pt, p, at)
        if at:
            self.last_at = at

    def _on_event(self, pt, p, at):
        if pt == "task_started":
            self.turn_open, self.turn_started, self.turn_tools = True, at, 0
            self.question = None
            if p.get("model_context_window"):
                self.context_limit = p["model_context_window"]
        elif pt in ("task_complete", "turn_aborted"):
            self.turn_open = False
            self.pending.clear()
            self.question = None
            if p.get("last_agent_message"):
                self.last_text = clip(p["last_agent_message"], 280)
        elif pt == "token_count":
            info = p.get("info") or {}
            last = info.get("last_token_usage") or {}
            if last.get("input_tokens"):
                self.context = last["input_tokens"]
            if info.get("model_context_window"):
                self.context_limit = info["model_context_window"]
        elif pt == "user_message" and p.get("message"):
            self.asked = clip(p["message"], 280)
        elif pt == "agent_message" and p.get("message"):
            self.last_text = clip(p["message"], 280)
        elif pt and (pt.endswith("approval_request") or pt == "request_user_input"):
            q = p.get("questions") or []
            if q and isinstance(q, list):
                qs = [dict(question=clip(str(x.get("question") or ""), 600),
                           header=clip(str(x.get("header") or ""), 30), multi=False,
                           options=[dict(label=clip(str(o.get("label") or ""), 60),
                                         description=clip(str(o.get("description") or ""), 200))
                                    for o in (x.get("options") or [])[:4] if isinstance(o, dict)])
                      for x in q[:4] if isinstance(x, dict)]
                self.question = dict(kind="ask", at=at, questions=qs)
            else:
                what = p.get("command") or p.get("reason") or "Approve an action"
                if isinstance(what, list):
                    what = " ".join(str(w) for w in what)
                self.question = dict(kind="ask", at=at, questions=[dict(
                    question=clip(f"Approve: {what}", 600), header="Approval",
                    multi=False, options=[])])
        elif pt == "item_completed":
            item = p.get("item") or {}
            it = item.get("type")
            text = " ".join(str(c.get("text") or "") for c in (item.get("content") or [])
                            if isinstance(c, dict)).strip()
            if it == "UserMessage" and text:
                self.asked = clip(text, 280)
            elif it == "AgentMessage" and text:
                self.last_text = clip(text, 280)
            elif it in ("TodoList", "Plan") and isinstance(item.get("items"), list):
                self.plan = [{"content": clip(str(i.get("text") or i.get("step") or ""), 120),
                              "status": "completed" if i.get("completed") else
                              (i.get("status") or "pending")}
                             for i in item["items"] if isinstance(i, dict)]
            if self.question and it not in ("Reasoning",):
                self.question = None

    def _on_item(self, pt, p, at):
        if pt in ("function_call", "custom_tool_call", "local_shell_call"):
            name = p.get("name") or ("shell" if pt == "local_shell_call" else "tool")
            raw = p.get("arguments") if pt == "function_call" else p.get("input")
            args = {}
            if isinstance(raw, str) and raw.startswith("{"):
                try:
                    args = json.loads(raw)
                except ValueError:
                    args = {}
            if name == "update_plan" and isinstance(args.get("plan"), list):
                self.plan = [{"content": clip(str(i.get("step") or ""), 120),
                              "status": i.get("status") or "pending"}
                             for i in args["plan"] if isinstance(i, dict)]
                return
            if isinstance(args, dict) and args:
                desc = (args.get("title") or args.get("description") or args.get("cmd")
                        or args.get("command") or args.get("query") or args.get("path")
                        or args.get("code") or "")
                if isinstance(desc, list):
                    desc = " ".join(str(d) for d in desc)
            else:
                desc = raw if isinstance(raw, str) else ""
            call = p.get("call_id") or p.get("id")
            self.pending[call] = {"name": name, "desc": clip(str(desc), 140), "at": at or 0}
            self.turn_tools += 1
            self.feed.append({"id": call, "name": name, "desc": clip(str(desc), 140),
                              "at": at or 0})
            del self.feed[:-FEED_LEN]
        elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            call = p.get("call_id")
            if self.pending.pop(call, None) is not None:
                for item in reversed(self.feed):
                    if item.get("id") == call:
                        item["ms"] = max(0, (at or 0) - item["at"])
                        break
            if self.question:
                self.question = None
        elif pt == "message" and p.get("role") == "assistant":
            text = " ".join(str(c.get("text") or "") for c in (p.get("content") or [])
                            if isinstance(c, dict)).strip()
            if text:
                self.last_text = clip(text, 280)

    def running(self, now_ms):
        return bool(self.turn_open and self.last_at
                    and now_ms - self.last_at < TURN_STALE_SECS * 1000)

    def now_tool(self):
        if not self.pending:
            return None
        p = max(self.pending.values(), key=lambda x: x["at"])
        return dict(name=p["name"], desc=p["desc"], startedAt=p["at"])

    def plan_view(self):
        if not self.plan:
            return None
        return dict(done=sum(1 for i in self.plan if i["status"] == "completed"),
                    total=len(self.plan), items=self.plan)

    def feed_view(self):
        return [{k: v for k, v in f.items() if k != "id"} for f in self.feed][::-1]


def codex_running(procs):
    """Whether any Codex process (app server or CLI) is running."""
    for p in procs.values():
        a = p["args"]
        if ("codex" in a.lower() and (" app-server" in a or "/codex " in a
                                      or a.endswith("/codex") or "Codex.app" in a)):
            return True
    return False


def open_url(thread_id):
    return f"codex://threads/{thread_id}"

"""Fleet's collector (server/fleet.py and its readers) on synthetic sources.

Every source is a temp folder: a Claude Code folder with transcripts, a
Claude app folder with session records, a CODEX_HOME with a thread
database and rollouts, and an injected job registry. The process table is
a fixture too, so nothing here reads the real machine.

The pitfalls each test pins came from the real machine: untimestamped
lines the app appends to idle transcripts, the Codex app's reload events,
Codex's bulk-bumped updated_at, headless SDK calls outnumbering sessions,
and Vira's own Codex jobs showing twice.

Run: .venv/bin/python -m unittest tests.test_fleet
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from server import changelog, fleet, fleetclaude as fc, fleetcodex as fx

MIN = 60_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def now():
    return int(time.time() * 1000)


def user(text, at, **kw):
    return {"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": text}, **kw}


def tool_use(tid, name, inp, at, stop=None):
    msg = {"role": "assistant", "model": "claude-test-1",
           "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
           "usage": {"input_tokens": 1000, "cache_read_input_tokens": 9000}}
    if stop:
        msg["stop_reason"] = stop
    return {"type": "assistant", "timestamp": iso(at), "message": msg}


def tool_result(tid, at):
    return {"type": "user", "timestamp": iso(at), "message": {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def said(text, at, stop="end_turn"):
    return {"type": "assistant", "timestamp": iso(at), "message": {
        "role": "assistant", "model": "claude-test-1", "stop_reason": stop,
        "content": [{"type": "text", "text": text}]}}


class Env:
    """A machine's worth of agent files under one temp folder."""

    def __init__(self, root):
        self.root = Path(root)
        self.claude = self.root / "claude"
        self.app = self.root / "app"
        self.codex = self.root / "codex"
        self.work = self.root / "work"
        for p in (self.claude / "projects", self.app / "claude-code-sessions" / "a" / "b",
                  self.codex, self.work):
            p.mkdir(parents=True, exist_ok=True)
        self.procs = []
        self.cwds = {}

    def folder(self, name):
        p = self.work / name
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def record(self, sid, cli, cwd, **kw):
        d = {"sessionId": sid, "cliSessionId": cli, "cwd": cwd, "originCwd": cwd,
             "title": kw.pop("title", f"Session {sid}"), "model": "claude-test-1",
             "createdAt": kw.pop("createdAt", now() - 60 * MIN),
             "lastActivityAt": kw.pop("lastActivityAt", now() - 60 * MIN),
             "isArchived": False, **kw}
        p = self.app / "claude-code-sessions" / "a" / "b" / f"{sid}.json"
        p.write_text(json.dumps(d), encoding="utf-8")
        return p

    def transcript(self, cwd, cli, lines, entrypoint="claude-desktop"):
        folder = self.claude / "projects" / fc.slug(cwd)
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"{cli}.jsonl"
        out = []
        for r in lines:
            if isinstance(r, dict) and "timestamp" in r:
                r = {"sessionId": cli, "cwd": cwd, "entrypoint": entrypoint, **r}
            out.append(json.dumps(r))
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        return p

    def append(self, path, lines, cli="", cwd=""):
        with open(path, "a", encoding="utf-8") as f:
            for r in lines:
                f.write(json.dumps(r) + "\n")

    def proc(self, pid, args, cwd=None, ppid=500, secs=600):
        self.procs.append(dict(pid=pid, ppid=ppid, secs=secs, cpu=1.0, rss=10240, args=args))
        if cwd:
            self.cwds[pid] = cwd

    def app_proc(self, pid, cli=None, cwd=None):
        args = ("/x/claude-code/2.0/abc/claude.app/Contents/MacOS/claude --output-format stream-json "
                "--verbose --input-format stream-json")
        if cli:
            args += f" --resume={cli}"
        self.proc(pid, args, cwd)

    # ----- Codex

    def codex_db(self, rows):
        p = self.codex / "state_5.sqlite"
        con = sqlite3.connect(p)
        con.execute("create table if not exists threads (id text primary key, rollout_path text, "
                    "cwd text, source text, archived integer default 0, git_branch text, "
                    "title text, model text, reasoning_effort text, created_at_ms integer, "
                    "updated_at_ms integer, recency_at_ms integer, first_user_message text)")
        for r in rows:
            con.execute("insert or replace into threads (id, rollout_path, cwd, source, archived, "
                        "git_branch, title, model, reasoning_effort, created_at_ms, updated_at_ms, "
                        "recency_at_ms, first_user_message) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["rollout_path"], r.get("cwd", ""), r.get("source", "vscode"),
                         0, r.get("git_branch"), r.get("title", "A thread"), "gpt-test",
                         "high", r.get("created", now() - 120 * MIN), now(), now(), ""))
        con.commit()
        con.close()

    def rollout(self, tid, events):
        p = self.codex / "sessions" / f"rollout-{tid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return p


def ev(kind, at, **payload):
    return {"timestamp": iso(at), "type": "event_msg", "payload": {"type": kind, **payload}}


def item(kind, at, **payload):
    return {"timestamp": iso(at), "type": "response_item", "payload": {"type": kind, **payload}}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = Env(tmp.name)
        patches = [
            mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(self.env.claude),
                "VIRA_FLEET_CLAUDE_APP_DIR": str(self.env.app),
                "CODEX_HOME": str(self.env.codex),
                "VIRA_FLEET_GH": ""}),
            mock.patch.object(fleet.Procs, "read", lambda s: self._procs()),
            mock.patch.object(fleet.Procs, "cwd_of", lambda s, p: self.env.cwds.get(p["pid"])),
            mock.patch.object(fleet, "listening_ports", lambda: {}),
            mock.patch.object(fleet, "probe_instance", lambda *a, **k: None),
            mock.patch("server.prindex.lookup", lambda branch: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.jobs = ([], [])
        self.fleet = fleet.Fleet(jobs=lambda: self.jobs)

    def _procs(self):
        procs = {p["pid"]: dict(p) for p in self.env.procs}
        kids = {}
        for p in procs.values():
            kids.setdefault(p["ppid"], []).append(p["pid"])
        return procs, kids

    def live(self):
        return self.fleet.live()

    def row(self, snap, rid):
        return next((r for r in snap["sessions"] + snap["nodes"] if r["id"] == rid), None)


class ClaudeSessions(Base):
    def test_last_activity_ignores_untimestamped_tail_lines(self):
        cwd = self.env.folder("alpha")
        t = now() - 30 * MIN
        p = self.env.transcript(cwd, "c1", [user("Build the thing please now", t),
                                            said("Done.", t + MIN)])
        # The app keeps appending these to an idle session: no timestamp.
        self.env.append(p, [{"type": "bridge-session"}, {"type": "cost-state"},
                            {"type": "last-prompt", "lastPrompt": "x"}])
        self.env.record("local_1", "c1", cwd, lastActivityAt=now() - 90 * MIN)
        closed = self.row(self.live(), "local_1")
        self.assertFalse(closed["live"])
        self.assertEqual(closed["lastAt"], fc.ts_ms(iso(t + MIN)))
        self.env.app_proc(101, "c1", cwd)
        live = self.row(self.live(), "local_1")
        self.assertEqual(live["status"], "Idle")
        self.assertEqual(live["lastAt"], fc.ts_ms(iso(t + MIN)))

    def test_resume_argument_matches_the_process_to_its_session(self):
        cwd = self.env.folder("alpha")
        for n in (1, 2):
            self.env.transcript(cwd, f"c{n}", [user("Start the work on it", now() - 5 * MIN)])
            self.env.record(f"local_{n}", f"c{n}", cwd)
        self.env.app_proc(101, "c2", cwd)
        snap = self.live()
        self.assertEqual([s["id"] for s in snap["sessions"]], ["local_2"])
        s = snap["sessions"][0]
        self.assertEqual(s["status"], "Working")        # a turn is open
        self.assertEqual(s["tool"], "claude")
        self.assertEqual(s["surface"], "app")
        self.assertEqual(s["openUrl"], "claude://claude.ai/epitaxy/local_2")
        self.assertEqual(snap["totals"]["working"], 1)
        self.assertFalse(self.row(snap, "local_1")["live"])

    def test_a_process_without_resume_matches_by_folder(self):
        cwd = self.env.folder("beta")
        self.env.transcript(cwd, "c1", [said("Hi.", now() - 2 * MIN)])
        self.env.record("local_1", "c1", cwd)
        self.env.app_proc(101, None, cwd)
        self.assertTrue(self.row(self.live(), "local_1")["live"])

    def test_running_subagent_keeps_the_session_working(self):
        cwd = self.env.folder("alpha")
        t = now() - 3 * MIN
        p = self.env.transcript(cwd, "c1", [user("Research the options for this", t),
                                            said("Started an agent.", t + 1000)])
        sub = p.parent / "c1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {**user("Verify the sources please", t + 2000), "isSidechain": True},
            {**tool_use("t1", "Read", {"file_path": "/x/notes.md"}, t + 3000), "isSidechain": True},
        ]) + "\n", encoding="utf-8")
        (sub / "agent-a1.meta.json").write_text(json.dumps({"description": "Verify sources"}),
                                                encoding="utf-8")
        (sub / "agent-old.jsonl").write_text("", encoding="utf-8")
        os.utime(sub / "agent-old.jsonl", (time.time() - 3600, time.time() - 3600))
        self.env.record("local_1", "c1", cwd)
        self.env.app_proc(101, "c1", cwd)
        s = self.row(self.live(), "local_1")
        self.assertEqual(s["status"], "Working")
        self.assertEqual(s["agents"]["total"], 2)
        self.assertEqual(len(s["agents"]["live"]), 1)
        self.assertEqual(s["agents"]["live"][0]["what"], "Verify sources")
        self.assertEqual(s["agents"]["live"][0]["now"], "Read: notes.md")

    def test_plan_progress_and_an_open_question(self):
        cwd = self.env.folder("alpha")
        t = now() - 10 * MIN
        items = [{"content": f"Step {i}", "status": "in_progress" if i == 1 else "pending"}
                 for i in (1, 2, 3)]
        p = self.env.transcript(cwd, "c1", [
            user("Ship the feature end to end", t),
            tool_use("p1", "mcp__ccd_turn__update_plan", {"items": items}, t + 1000),
            tool_result("p1", t + 1500),
            tool_use("p2", "mcp__ccd_turn__update_plan", {"advance": True}, t + 2000),
            tool_result("p2", t + 2500),
            tool_use("q1", "AskUserQuestion", {"questions": [{
                "question": "Which store?", "header": "Store", "options": [
                    {"label": "SQLite", "description": "One file"},
                    {"label": "JSON", "description": "Plain"}]}]}, t + 3000, stop="tool_use"),
        ])
        self.env.record("local_1", "c1", cwd)
        self.env.app_proc(101, "c1", cwd)
        s = self.row(self.live(), "local_1")
        self.assertEqual(s["status"], "Needs you")
        self.assertEqual((s["plan"]["done"], s["plan"]["total"]), (1, 3))
        self.assertEqual(s["plan"]["items"][1]["status"], "in_progress")
        self.assertEqual(s["question"]["kind"], "ask")
        self.assertEqual([o["label"] for o in s["question"]["questions"][0]["options"]],
                         ["SQLite", "JSON"])
        self.assertEqual(s["now"]["name"], "AskUserQuestion")
        # answered, and the turn ends: back to Idle with no question
        self.env.append(p, [tool_result("q1", t + 60_000), said("Using SQLite.", t + 61_000)])
        s = self.row(self.live(), "local_1")
        self.assertEqual(s["status"], "Idle")
        self.assertIsNone(s["question"])

    def test_a_turn_that_ends_blocked_needs_the_owner(self):
        cwd = self.env.folder("alpha")
        t = now() - 4 * MIN
        self.env.transcript(cwd, "c1", [
            user("Get this ready to merge", t),
            tool_use("w1", "mcp__ccd_turn__wrap_up", {"recap": "Ready.", "ask": "Merge it?",
                                                     "blocked": True}, t + 1000),
            tool_result("w1", t + 1500), said("Waiting on you.", t + 2000)])
        self.env.record("local_1", "c1", cwd)
        self.env.app_proc(101, "c1", cwd)
        s = self.row(self.live(), "local_1")
        self.assertEqual(s["status"], "Needs you")
        self.assertEqual(s["question"]["kind"], "wrap")
        self.assertEqual(s["question"]["ask"], "Merge it?")

    def test_fork_and_spawn_links_make_one_family(self):
        cwd = self.env.folder("alpha")
        for n in (1, 2, 3):
            self.env.transcript(cwd, f"c{n}", [said("ok", now() - n * MIN)])
        self.env.record("local_1", "c1", cwd)
        self.env.record("local_2", "c2", cwd, spawnedFrom={"sessionId": "local_1"})
        self.env.record("local_3", "c3", cwd, forkedFromSessionId="local_1")
        snap = self.live()
        two, three = self.row(snap, "local_2"), self.row(snap, "local_3")
        self.assertEqual((two["parentId"], two["link"]), ("local_1", "task"))
        self.assertEqual((three["parentId"], three["link"]), ("local_1", "fork"))
        self.assertEqual(len({two["family"], three["family"], self.row(snap, "local_1")["family"]}), 1)

    def test_terminal_sessions_and_headless_calls(self):
        cwd = self.env.folder("gamma")
        self.env.transcript(cwd, "t1", [user("Fix the flaky test here", now() - MIN)],
                            entrypoint="cli")
        self.env.proc(201, "/usr/local/bin/claude", cwd)
        for n in range(3):
            self.env.transcript(cwd, f"s{n}", [user(
                "You are tagging DOCUMENTS in a personal software project's library so", now() - n * MIN)],
                entrypoint="sdk-cli")
        self.env.transcript(cwd, "job1", [user("You are Vira's coding agent", now() - MIN)],
                            entrypoint="sdk-py")
        snap = self.live()
        self.assertEqual([(s["id"], s["surface"]) for s in snap["sessions"]], [("t1", "CLI")])
        self.assertNotIn("s0", {r["id"] for r in snap["nodes"]})
        rows, cli = fc.CallIndex().scan(now(), exclude_ids={"job1"})
        self.assertEqual([(r["kind"], r["week"]) for r in rows], [("library tagging", 3)])
        self.assertEqual([Path(c["path"]).stem for c in cli], ["t1"])

    def test_headless_call_kinds(self):
        self.assertEqual(fc.call_kind("You are tagging items on a personal engineering backlog so"),
                         "backlog tagging")
        self.assertEqual(fc.call_kind("You are Vira's release desk. Below are"), "release desk")
        self.assertEqual(fc.call_kind("Summarize this please"), "other")


class CodexThreads(Base):
    def thread(self, tid, events, cwd=None, **kw):
        path = self.env.rollout(tid, events)
        self.env.codex_db([dict(id=tid, rollout_path=str(path), cwd=cwd or self.env.folder("delta"), **kw)])
        return path

    def test_reload_events_and_bumped_updated_at_are_not_activity(self):
        t = now() - 3 * 3600_000
        self.thread("th1", [
            {"timestamp": iso(t), "type": "session_meta", "payload": {"id": "th1"}},
            ev("task_started", t + 1000),
            item("function_call", t + 2000, name="exec_command", arguments='{"cmd": "ls"}', call_id="a"),
            item("function_call_output", t + 3000, call_id="a"),
            ev("task_complete", t + 4000, last_agent_message="Listed."),
            ev("thread_settings_applied", now() - 1000),
        ])
        self.env.proc(300, "/Applications/X/codex app-server --listen stdio://")
        s = self.row(self.live(), "th1")
        self.assertEqual(s["lastAt"], fc.ts_ms(iso(t + 4000)))
        self.assertEqual(s["status"], "Closed")
        self.assertEqual(s["tool"], "codex")
        self.assertEqual(s["openUrl"], "codex://threads/th1")

    def test_a_running_turn_is_working_then_idle_when_it_ends(self):
        t = now() - 30_000
        path = self.thread("th1", [
            ev("task_started", t - 600_000), ev("task_complete", t - 500_000),
            ev("task_started", t, model_context_window=200000),
            item("function_call", t + 1000, name="exec_command",
                 arguments='{"cmd": "pytest -q"}', call_id="c1"),
            ev("token_count", t + 1500, info={"last_token_usage": {"input_tokens": 50000},
                                              "model_context_window": 200000}),
        ])
        self.env.proc(300, "/Applications/X/codex app-server --listen stdio://")
        s = self.row(self.live(), "th1")
        self.assertEqual(s["status"], "Working")
        self.assertEqual(s["now"]["desc"], "pytest -q")
        self.assertEqual(s["context"], {"used": 50000, "limit": 200000, "compactions": 0})
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item("function_call_output", t + 9000, call_id="c1")) + "\n")
            f.write(json.dumps(ev("task_complete", t + 10_000)) + "\n")
        s = self.row(self.live(), "th1")
        self.assertEqual(s["status"], "Idle")
        self.env.procs.clear()
        self.assertEqual(self.row(self.live(), "th1")["status"], "Closed")

    def test_an_open_turn_that_went_quiet_is_not_working(self):
        path = self.env.rollout("q", [ev("task_started", now() - 2 * 3600_000),
                                      item("function_call", now() - 90 * MIN, name="x",
                                           arguments="{}", call_id="z")])
        ro = fx.Rollout(path)
        ro.poll()
        self.assertTrue(ro.turn_open)
        self.assertFalse(ro.running(now()))

    def test_a_long_turn_still_writing_is_working(self):
        self.thread("th1", [ev("task_started", now() - 3 * 3600_000),
                            item("function_call", now() - MIN, name="exec_command",
                                 arguments='{"cmd": "make"}', call_id="m")])
        self.env.proc(300, "/Applications/X/codex app-server --listen stdio://")
        self.assertEqual(self.row(self.live(), "th1")["status"], "Working")

    def test_vira_codex_jobs_show_once_as_the_job(self):
        t = now() - 20_000
        self.thread("th9", [ev("task_started", t),
                            item("function_call", t + 500, name="exec_command",
                                 arguments='{"cmd": "make"}', call_id="m")])
        self.jobs = ([{"id": "j1", "status": "running", "awaiting": None}],
                     [{"id": "j1", "session_id": "th9", "provider": "openai", "cwd": self.env.folder("delta"),
                       "status": "running", "started": datetime.now().isoformat(timespec="seconds"),
                       "title": "Build the widget", "pr": {"number": 7, "url": "https://example.com/7"},
                       "prompt": "Build the widget"}])
        snap = self.live()
        self.assertIsNone(self.row(snap, "th9"))
        job = self.row(snap, "vira:j1")
        self.assertEqual(job["tool"], "vira")
        self.assertEqual(job["status"], "Working")
        self.assertEqual(job["codexThread"], "th9")
        self.assertEqual(job["now"]["desc"], "make")
        self.assertEqual(job["viraJob"], "j1")

    def test_codex_subagents_count_as_agents_not_sessions(self):
        t = now() - 20_000
        self.thread("parent", [ev("task_started", t)])
        child = self.env.rollout("kid", [ev("task_started", t + 1000)])
        self.env.codex_db([dict(id="kid", rollout_path=str(child),
                                source=json.dumps({"subagent": {"thread_spawn": {
                                    "parent_thread_id": "parent"}}}))])
        snap = self.live()
        self.assertIsNone(self.row(snap, "kid"))
        self.assertEqual(self.row(snap, "parent")["agents"]["total"], 1)
        self.assertEqual(len(self.row(snap, "parent")["agents"]["live"]), 1)


class ViraJobs(Base):
    def ledger(self, awaiting, status="running"):
        self.jobs = ([{"id": "j1", "status": status, "awaiting": awaiting}],
                     [{"id": "j1", "session_id": "", "cwd": self.env.folder("vira"),
                       "status": status, "started": datetime.now().isoformat(timespec="seconds"),
                       "title": "A job", "prompt": "Do it", "provider": "anthropic"}])

    def test_job_states(self):
        for awaiting, status in ((None, "Working"), ("ask", "Needs you"), ("permission", "Needs you"),
                                 ("agents", "Jobs running"), ("reply", "Idle")):
            self.ledger(awaiting)
            self.assertEqual(self.row(self.live(), "vira:j1")["status"], status, awaiting)
        self.ledger(None, status="done")
        self.assertEqual(self.row(self.live(), "vira:j1")["status"], "Closed")


class MissingSources(Base):
    def test_every_missing_source_says_so_and_the_snapshot_still_answers(self):
        for p in (self.env.claude, self.env.app, self.env.codex):
            for f in sorted(p.rglob("*"), reverse=True):
                f.rmdir() if f.is_dir() else f.unlink()
            p.rmdir()
        snap = self.live()
        self.assertEqual(snap["sessions"], [])
        src = snap["sources"]
        for key in ("claude", "claudeApp", "codex", "gh"):
            self.assertFalse(src[key]["found"], key)
            self.assertIn("not found on this machine", src[key]["note"])
        self.assertTrue(src["vira"]["found"])


class Parsers(unittest.TestCase):
    def test_landings(self):
        log = ("\x1eaaa\x1f1700000000\x1fp1 p2\x1fMerge branch 'claude/fleet'\n\n"
               " 3 files changed, 40 insertions(+), 2 deletions(-)\n"
               "\x1ebbb\x1f1699990000\x1fp0\x1fFix a typo (#12)\n\n 1 file changed, 1 insertion(+)\n"
               "\x1eccc\x1f1699980000\x1fp8 p9\x1fMerge branch 'codex/speedup'\n"
               "\x1eddd\x1f1699970000\x1fp6 p7\x1fMerge branch codex/unquoted into main\n")
        prs = [{"n": 40, "title": "Fleet window", "state": "MERGED", "branch": "claude/fleet",
                "url": "https://example.com/40"}]
        rows = changelog.parse_landings(log, prs)
        self.assertEqual([r["tool"] for r in rows], ["claude", "direct", "codex", "codex"])
        self.assertEqual(rows[3]["branch"], "codex/unquoted")
        self.assertEqual(rows[0]["pr"]["title"], "Fleet window")
        self.assertEqual((rows[0]["files"], rows[0]["insertions"], rows[0]["deletions"]), (3, 40, 2))
        self.assertEqual(rows[1]["pr"]["n"], 12)
        self.assertEqual(rows[1]["insertions"], 1)

    def test_changelog_bullets_are_dated_by_the_commit_that_added_them(self):
        diff = ("\x1eaaa\x1f1700000000\n\ndiff --git a/CHANGELOG.md b/CHANGELOG.md\n"
                "@@ -1,0 +2,2 @@\n+- First thing. With detail\n+  continues here.\n"
                "\x1ebbb\x1f1700100000\n\n@@ -3,0 +4,1 @@\n+- Second thing shipped.\n"
                "+- First thing. With detail\n")
        current = ("# Changelog\n\n## Unreleased\n\n### Added\n\n- Second thing shipped.\n\n"
                   "### Fixed\n\n- First thing. With detail\n  continues here.\n")
        rows = changelog.parse_changelog_md(diff, current)
        self.assertEqual([(r["headline"], r["section"], r["at"]) for r in rows],
                         [("Second thing shipped.", "Added", 1700100000000),
                          ("First thing.", "Fixed", 1700000000000)])
        self.assertEqual(rows[1]["detail"], "With detail continues here.")



class Repositories(unittest.TestCase):
    """fleetgit against a real repository built in a temp folder."""

    def setUp(self):
        import shutil
        import subprocess
        if not shutil.which("git"):
            self.skipTest("git is unavailable")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "proj"
        self.root.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                   GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com",
                   GIT_CONFIG_NOSYSTEM="1", HOME=tmp.name)

        def git(*args, cwd=None):
            subprocess.run(["git", *args], cwd=cwd or self.root, env=env, check=True,
                           capture_output=True, text=True, encoding="utf-8")

        self.git = git
        git("init", "-q", "-b", "main")
        git("config", "core.hooksPath", "/dev/null")
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n### Added\n\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "start")
        # A branch that landed by merge, with a change-log bullet.
        git("checkout", "-q", "-b", "claude/landed")
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n### Added\n\n- The landed thing. It works.\n", encoding="utf-8")
        git("commit", "-q", "-am", "landed work")
        git("checkout", "-q", "main")
        git("merge", "-q", "--no-ff", "claude/landed", "-m", "Merge branch 'claude/landed'")
        # A branch with unlanded commits and no worktree.
        git("checkout", "-q", "-b", "claude/pending")
        (self.root / "p.txt").write_text("p\n", encoding="utf-8")
        git("add", "p.txt")
        git("commit", "-q", "-m", "pending work")
        git("checkout", "-q", "main")
        # Two app worktrees: one with a session in it, one with uncommitted edits.
        self.busy = self.root / ".claude" / "worktrees" / "busy"
        self.dirty = self.root / ".claude" / "worktrees" / "dirty"
        git("worktree", "add", "-q", "-b", "claude/busy", str(self.busy))
        git("worktree", "add", "-q", "-b", "claude/dirty", str(self.dirty))
        (self.dirty / "scratch.txt").write_text("x\n", encoding="utf-8")
        p = mock.patch.dict(os.environ, {"VIRA_FLEET_GH": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_worktrees_branches_and_status_labels(self):
        from server import fleetgit as fg
        self.assertEqual(Path(fg.repo_root(str(self.busy))).resolve(), self.root.resolve())
        scan = fg.scan(str(self.root), fg.GhCache())
        self.assertEqual(scan["base"], "main")
        self.assertEqual(scan["prNote"], "gh not found on this machine")
        session = {"id": "s1", "folder": str(self.busy), "status": "Working", "live": True}
        repo = fleet.label_repo(scan, [session], [], now())
        by_name = {w["name"]: w for w in repo["worktrees"]}
        self.assertEqual(by_name["proj"]["kind"], "main")
        self.assertEqual(by_name["proj"]["label"], "Live")
        self.assertEqual(by_name["busy"]["label"], "In use")
        self.assertEqual(by_name["busy"]["sessions"], ["s1"])
        self.assertEqual(by_name["proj"]["sessions"], [], "a session in a nested worktree is not the main checkout's")
        self.assertEqual(by_name["dirty"]["label"], "Uncommitted changes")
        self.assertEqual(by_name["dirty"]["dirty"], 1)
        branches = {b["name"]: b for b in repo["branches"]}
        self.assertEqual(branches["claude/landed"]["label"], "Landed, can remove")
        self.assertEqual(branches["claude/pending"]["label"], "Unlanded commits")
        self.assertEqual(branches["claude/pending"]["ahead"], 1)
        self.assertNotIn("heads", repo)

    def test_landings_and_change_log_from_history(self):
        from server import fleetgit as fg
        scan = fg.scan(str(self.root), fg.GhCache())
        merge = scan["landings"][0]
        self.assertEqual((merge["branch"], merge["tool"], merge["merge"]), ("claude/landed", "claude", True))
        self.assertEqual(merge["files"], 1)
        self.assertEqual([c["headline"] for c in scan["changelog"]], ["The landed thing."])
        self.assertEqual(scan["changelog"][0]["section"], "Added")
        # A new commit on main moves the cache.
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n### Added\n\n- Second.\n- The landed thing. It works.\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "direct")
        scan = fg.scan(str(self.root), fg.GhCache())
        self.assertEqual(scan["landings"][0]["tool"], "direct")
        self.assertEqual([c["headline"] for c in scan["changelog"]], ["Second.", "The landed thing."])

    def test_signature_moves_when_a_branch_or_worktree_does(self):
        from server import fleetgit as fg
        before = fg.signature(str(self.root))
        self.git("branch", "claude/new")
        self.assertNotEqual(fg.signature(str(self.root)), before)


class SlowTier(Base, Repositories):
    """The repository picture as the window receives it: every repository
    a session worked in, the 7-day bars, background calls, and Vira's own
    change log beside the landings."""

    def setUp(self):
        Base.setUp(self)
        Repositories.setUp(self)
        groups = [{"date": time.strftime("%Y-%m-%d"), "goal": "", "entries": [
            {"text": "Shipped it", "kind": "ship", "ts": datetime.now().isoformat(), "source": "retro"},
            {"text": "Not doing it", "kind": "dropped", "ts": datetime.now().isoformat(), "source": "idea"}]}]
        for p in (mock.patch.object(changelog, "REPO", self.root),
                  mock.patch.object(changelog, "groups", lambda: groups)):
            p.start()
            self.addCleanup(p.stop)

    def test_repos_week_calls_and_the_vira_log(self):
        self.env.transcript(str(self.busy), "c1", [user("Work in the busy tree", now() - 2 * MIN)])
        self.env.record("local_1", "c1", str(self.busy))
        self.env.app_proc(101, "c1", str(self.busy))
        self.env.transcript(str(self.root), "x1", [user("You are tagging DOCUMENTS here", now() - MIN)],
                            entrypoint="sdk-cli")
        snap = self.fleet.snapshot()
        repos = snap["repos"]
        self.assertEqual([Path(r["root"]).resolve() for r in repos["repos"]], [self.root.resolve()])
        busy = next(w for w in repos["repos"][0]["worktrees"] if w["name"] == "busy")
        self.assertEqual((busy["label"], busy["sessions"]), ("In use", ["local_1"]))
        self.assertEqual([(w["id"], w["tool"], w["live"]) for w in repos["week"]], [("local_1", "claude", True)])
        self.assertEqual([(c["kind"], c["today"]) for c in repos["calls"]], [("library tagging", 1)])
        self.assertEqual(Path(repos["viraRepo"]).resolve(), self.root.resolve())
        self.assertEqual([e["text"] for g in repos["viraLog"] for e in g["entries"]], ["Shipped it"],
                         "a dropped idea is not a change")
        self.assertFalse(self.fleet.repos_due(), "nothing moved since the scan")
        self.git("branch", "claude/another")
        self.assertTrue(self.fleet.repos_due(), "a new branch is noticed before the 30 s rescan")


class InstancesAndServers(Base):
    def test_a_branch_instance_is_shown_beside_its_worktree(self):
        wt = Path(self.env.folder("wt"))
        (wt / ".test-instance.json").write_text(json.dumps({"pid": 4242, "port": 8390}), encoding="utf-8")
        self.fleet.scans["/r"] = {"worktrees": [{"path": str(wt), "branch": "claude/a"}]}
        self.env.proc(4242, "python -m uvicorn server.main:app --port 8390", ppid=1, secs=50 * 3600)
        with mock.patch.object(fleet, "listening_ports", lambda: {8390: 4242}), \
                mock.patch.object(fleet, "probe_instance", lambda port, timeout=0: {"id": "branch:a", "kind": "branch"}):
            snap = self.live()
        inst = next(i for i in snap["instances"] if i["port"] == 8390)
        self.assertEqual(inst["worktree"], str(wt))
        self.assertEqual(inst["branch"], "claude/a")
        self.assertGreater(now() - inst["startedAt"], 49 * 3600_000)
        self.assertNotIn(4242, [s["pid"] for s in snap["servers"]], "an instance is not a stray dev server")

    def test_a_dev_server_left_running_in_a_session_folder(self):
        cwd = self.env.folder("site")
        self.env.transcript(cwd, "c1", [said("Started the server.", now() - MIN)])
        self.env.record("local_1", "c1", cwd)
        self.env.proc(700, "node node_modules/.bin/vite --port 5173", cwd=cwd, ppid=1, secs=7200)
        self.env.proc(701, "/Applications/Some.app/Contents/MacOS/x", cwd=cwd, ppid=1)
        snap = self.live()
        self.assertEqual([(s["pid"], s["port"]) for s in snap["servers"]], [(700, 5173)])


class AnalyzeRead(unittest.TestCase):
    """fleetread: one model call on request, its answer only ever data."""

    def setUp(self):
        from server import fleetread
        self.fr = fleetread
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(fleetread, "STORE", Path(tmp.name) / "fleet-read.json")
        p.start()
        self.addCleanup(p.stop)
        t = now()
        self.snap = {"snapshotAt": t, "sessions": [
            {"id": "s1", "tool": "claude", "title": "Build it", "live": True, "status": "Working",
             "lastAt": t, "asked": "x" * 900, "question": None, "summary": None}],
            "nodes": [{"id": "s2", "tool": "codex", "title": "Old", "live": False, "status": "Closed",
                       "lastAt": t - 48 * 3600_000, "summary": {"category": "blocked", "detail": "stuck",
                                                               "needs": "a decision"}}],
            "families": [
                {"id": "f1", "title": "Build it", "project": "alpha", "kind": "feature", "state": "active",
                 "members": ["s1"], "branches": [{"name": "claude/a", "state": "open_pr", "ahead": 2,
                                                  "pr": {"n": 3, "state": "OPEN"}}]},
                {"id": "f2", "title": "Old", "project": "beta", "kind": "session", "state": "no_code",
                 "members": ["s2"], "branches": []}]}

    def test_the_brief_is_compact_and_clipped(self):
        b = self.fr.brief(self.snap, now())
        s1 = b["alpha"][0]["sessions"][0]
        self.assertLessEqual(len(s1["asked"]), self.fr.ASK_CHARS)
        self.assertEqual(b["alpha"][0]["branches"][0]["pr"], "#3 OPEN")
        self.assertIn("needed: a decision", b["beta"][0]["sessions"][0]["end_summary"])

    def test_fit_drops_the_oldest_closed_feature_first_and_says_so(self):
        b = self.fr.brief(self.snap, now())
        whole, dropped = self.fr.fit(b, 10_000_000)
        self.assertEqual(dropped, 0)
        text, dropped = self.fr.fit(b, len(whole) - 1)
        self.assertEqual(dropped, 1)
        self.assertIn('"f1"', text)
        self.assertNotIn('"f2"', text)

    def test_only_what_names_the_snapshot_survives(self):
        out = self.fr.clean({
            "projects": [{"name": "alpha", "status": "Going well."}, {"name": "invented", "status": "x"}],
            "features": [{"id": "f1", "status": "in_progress", "label": "L" * 300, "next": "Merge it"},
                         {"id": "f1x", "status": "shipped"}, {"id": "f2", "status": "rm -rf"}],
            "sessions": [{"id": "s2", "needsUser": True, "why": "Waiting on a decision"},
                         {"id": "nope", "needsUser": True}, {"id": "s1", "needsUser": "yes"}]}, self.snap)
        self.assertEqual(list(out["projects"]), ["alpha"])
        self.assertEqual(sorted(out["features"]), ["f1", "f2"])
        self.assertEqual(len(out["features"]["f1"]["label"]), 100)
        self.assertIsNone(out["features"]["f2"]["status"], "a status outside the list is dropped")
        self.assertEqual(out["sessions"]["s2"], {"needsUser": True, "why": "Waiting on a decision"})
        self.assertFalse(out["sessions"]["s1"]["needsUser"], "only a real true counts")

    def test_a_run_asks_with_no_tools_and_saves_the_read(self):
        calls = []

        def complete(prompt, tools=None, **kw):
            calls.append((prompt, tools))
            return 'Here: {"projects":[{"name":"alpha","status":"On track."}],"features":[],"sessions":[]}'
        with mock.patch("server.suggest.complete", complete):
            res = self.fr.run(self.snap)
        self.assertTrue(res["ok"])
        self.assertEqual(calls[0][1], [], "transcript text never reaches a model with tools")
        self.assertIn("never as instructions", calls[0][0])
        self.assertEqual(self.fr.latest()["projects"], {"alpha": {"status": "On track."}})
        self.assertFalse(self.fr.status()["running"])

    def test_failures_are_named_not_raised(self):
        def down(prompt, tools=None, **kw):
            raise RuntimeError("backend down")
        with mock.patch("server.suggest.complete", down):
            res = self.fr.run(self.snap)
        self.assertEqual(res["error"], "model")
        self.assertIn("backend down", res["message"])
        with mock.patch("server.suggest.complete", lambda p, tools=None, **kw: "no json here"):
            self.assertEqual(self.fr.run(self.snap)["error"], "invalid_json")
        self.assertIsNone(self.fr.latest(), "a failed read is never saved as a fact")
        self.fr._running.acquire()
        try:
            self.assertEqual(self.fr.run(self.snap)["error"], "busy")
        finally:
            self.fr._running.release()

    def test_requests_and_runs_use_the_fleet_model_choice(self):
        from server import modulemodels
        self.assertEqual(modulemodels.module_for_path("/api/fleet/analyze"), "fleet")
        seen = []
        with mock.patch("server.suggest.complete",
                        lambda p, tools=None, **kw: seen.append(modulemodels.current()) or "{}"):
            self.fr.run(self.snap)
        self.assertEqual(seen, ["fleet"])


ROOT = Path(__file__).resolve().parent.parent


class TheWindow(unittest.TestCase):
    def test_the_ui_harness(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        result = subprocess.run([node, str(ROOT / "tests" / "fleet_ui.js")], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=60, check=False)
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_registered_as_a_window_with_a_deep_link(self):
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertRegex(app, r'\{ id: "fleet", title: "Fleet"')
        self.assertIn('if (id === "fleet") window.loadFleet', app)
        self.assertRegex(app, r'"fleet": \(rest\) =>')
        self.assertIn('<section id="view-fleet" class="view">', html)
        self.assertIn('<script src="/fleet.js"></script>', html)
        self.assertIn('<link rel="stylesheet" href="/fleet.css">', html)
        from server import modulemap, modulestory
        ids = {m["id"] for m in modulemap.DEFAULT_MODULES}
        self.assertTrue({"fleet-engine", "fleet-win"} <= ids)
        self.assertEqual(modulestory.WINDOWS["fleet"]["map"], "fleet-win")

    def test_nothing_is_wider_than_a_phone(self):
        css = (ROOT / "static" / "fleet.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 759px)", css)
        self.assertRegex(css, r"\.fl-search \{[^}]*font-size: 16px")


if __name__ == "__main__":
    unittest.main()

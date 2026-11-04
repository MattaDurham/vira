"""The history ingest and its joins, on a synthetic machine.

Sandbox builds every source in a temporary folder (a git repository with a
merge and two branches, a Vira job with a landing card, a Claude app
session with its transcript, a Codex thread database, a scheduled Flow and
its Program runs with one missed hour, ideas, a journal note, texts) and
pins every module path at it, so no case reads the real machine or writes
the real data/.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from server import (changelog, circuits, fleetgit, ideas, ingestfeed, joblog, journal,
                    notify, orphanwork, prindex, reviewqueue, routinerun, routines)
from server import history as H
from server import historyingest as I
from server import historyview as V

HOUR = 3600


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def git(repo, *args, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com",
             GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.com", **(env or {}))
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=e).stdout


def commit(repo, path, text, msg, when):
    p = Path(repo) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    git(repo, "add", path)
    stamp = f"@{int(when)} +0000"
    git(repo, "commit", "-q", "-m", msg, env={"GIT_AUTHOR_DATE": stamp,
                                              "GIT_COMMITTER_DATE": stamp})


class Sandbox:
    """A synthetic machine. `now` anchors every fixture time."""

    def __init__(self, case):
        self.case = case
        self.tmp = Path(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = time.time()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.repo = self.tmp / "vira"
        self.claude = self.tmp / "claude"
        self.app = self.tmp / "app"
        self.codex = self.tmp / "codex"
        for p in (self.claude / "projects", self.app / "claude-code-sessions", self.codex,
                  self.tmp / "Sessions"):
            p.mkdir(parents=True)
        d = self.data
        pins = [(H, "DIR", d / "history"), (I, "DATA", d), (joblog, "STORE", d / "jobs-log.json"),
                (prindex, "STORE", d / "pr-index.json"), (circuits, "RUNS", d / "circuit-runs.json"),
                (circuits, "DEFS", d / "circuits.json"),
                (routinerun, "STORE", d / "routine-runs.json"), (routinerun, "RUNS", d / "routine-runs"),
                (ideas, "STORE", d / "ideas.json"), (journal, "STORE", d / "brief-journal.json"),
                (notify, "LOG", d / "notify-log.json"),
                (reviewqueue, "DECISIONS", d / "review-decided.json"),
                (orphanwork, "STORE", d / "orphan-work.json"), (routines, "STORE", d / "routines.json"),
                (changelog, "SESSIONS", self.tmp / "Sessions"),
                (H, "vira_repo", lambda: str(self.repo)),
                (ingestfeed, "vault_root", lambda: None), (fleetgit, "gh_bin", lambda: None)]
        for obj, name, value in pins:
            p = mock.patch.object(obj, name, value)
            p.start()
            case.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude),
                                           "VIRA_FLEET_CLAUDE_APP_DIR": str(self.app),
                                           "CODEX_HOME": str(self.codex)})
        env.start()
        case.addCleanup(env.stop)
        tree = mock.patch.dict(H._tree, {"at": time.time() + 1e9, "pats": [
            ("server/ideas.py", "work/ideas"), ("static/*.js", "core/shell"),
            ("server/forge.py", "work/automations")]})
        tree.start()
        case.addCleanup(tree.stop)

    def ago(self, hours):
        return self.now - hours * HOUR

    def write(self, path, obj):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(obj), encoding="utf-8")

    def jsonl(self, path, rows):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    # ---------------------------------------------------------- the request
    def build_repo(self):
        r = self.repo
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        commit(r, "README.md", "x\n", "Start", self.ago(50))
        git(r, "checkout", "-q", "-b", "claude/widget")
        commit(r, "server/ideas.py", "a = 1\nb = 2\n", "Widget: draw faster", self.ago(28.5))
        git(r, "checkout", "-q", "main")
        stamp = f"@{int(self.ago(28))} +0000"
        git(r, "merge", "-q", "--no-ff", "-m", "Merge branch 'claude/widget'", "claude/widget",
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        git(r, "branch", "-q", "-d", "claude/widget")
        git(r, "checkout", "-q", "-b", "claude/stale")
        commit(r, "static/app.js", "let x = 1;\n", "Stale: a half-done change", self.ago(10))
        git(r, "checkout", "-q", "main")
        git(r, "checkout", "-q", "-b", "claude/gone")
        commit(r, "server/forge.py", "z = 1\n", "Gone: an experiment", self.ago(9))
        git(r, "checkout", "-q", "main")

    def build_request(self):
        """A journal note -> idea -> Implement session -> landing card ->
        merge of PR 7 -> a text about it."""
        self.write(ideas.STORE, {"items": [
            {"id": "idea_a", "text": "Make the widget faster", "status": "done",
             "project": "vira", "source": "journal",
             "note": "merged into main 2026-11-03 (claude/widget)",
             "created": iso(self.ago(30)), "updated": iso(self.ago(28))},
            {"id": "idea_b", "text": "Paint the shed", "status": "proposed", "project": "Vira",
             "source": "muse", "note": "", "created": iso(self.ago(5)), "updated": iso(self.ago(5))},
            {"id": "idea_c", "text": "Rename the tabs", "status": "dropped", "project": "Vira",
             "source": "muse", "note": "declined by the owner 2026-11-01",
             "created": iso(self.ago(80)), "updated": iso(self.ago(70))}], "projects": []})
        self.write(journal.STORE, {"entries": [
            {"id": "note_a", "text": "Make the widget faster. It drags.", "created":
             datetime.fromtimestamp(self.ago(30.5)).strftime("%Y-%m-%dT%H:%M:%S"),
             "status": "noted", "result": {"summary": "Saved one instruction.", "actions": [],
                                            "unapplied": [{"idea_id": "idea_a", "job_id": "j1"}]}}]})
        transcript = self.claude / "projects" / "-vira" / "s1.jsonl"
        self.jsonl(transcript, [
            {"type": "user", "timestamp": iso(self.ago(29)), "entrypoint": "sdk-py",
             "sessionId": "s1", "cwd": str(self.repo),
             "message": {"role": "user", "content": "You are Vira's coding agent."}},
            {"type": "assistant", "timestamp": iso(self.ago(29) + 30), "message": {
                "id": "m1", "model": "claude-opus-5-5", "content": [{
                    "type": "tool_use", "name": "Write",
                    "input": {"file_path": str(self.repo) + "/.worktrees/widget/server/ideas.py"}}]}}])
        self.write(joblog.STORE, {"jobs": [{
            "id": "j1", "session_id": "s1", "transcript": str(transcript), "prompt": "You are...",
            "cwd": str(self.repo), "worktree": str(self.repo) + "/.worktrees/widget",
            "model": "opus", "provider": "anthropic", "idea_id": "idea_a", "mode": "bypassPermissions",
            "read_only": False, "branch": "claude/widget", "meta": {}, "subject": "Make the widget faster",
            "kind_label": "", "pr": {"number": 7, "url": "https://example.com/pull/7"},
            "status": "done", "started": iso(self.ago(29)), "finished": iso(self.ago(28.2)),
            "result": "Merged.", "command": "Implement - Make the widget faster",
            "title": "Make the widget faster · Implement", "title_edited": False}]})
        jdir = self.data / "jobs" / "j1"
        self.jsonl(jdir / "events.jsonl", [
            {"event": "turn_started", "t": self.ago(29), "turn_id": "t1"},
            {"event": "usage", "t": self.ago(28.9), "turn_id": "t1", "usage": {
                "input_tokens": 1000, "output_tokens": 2000, "cache_read_input_tokens": 10000}},
            {"event": "turn_completed", "t": self.ago(29) + 600, "turn_id": "t1"},
            {"event": "turn_started", "t": self.ago(28.4), "turn_id": "t2"},
            {"event": "turn_completed", "t": self.ago(28.4) + 60, "turn_id": "t2"}])
        self.write(jdir / "state.json", {"model_used": "claude-opus-5-5", "landing": "merge", "cards": [
            {"req_id": "c1", "kind": "landing", "t": self.ago(28.5),
             "summary": "claude/widget is ready - what happens to it?"}]})
        self.jsonl(jdir / "control.jsonl", [{"op": "landing", "req_id": "c1", "verdict": "merge"}])
        self.write(prindex.STORE, {"when": self.now, "by_branch": {"claude/widget": {
            "number": 7, "url": "https://example.com/pull/7",
            "title": "Widget: draw in half the time", "state": "MERGED", "draft": False}}})
        self.write(notify.LOG, {"sent": [
            {"at": datetime.fromtimestamp(self.ago(28.45)).strftime("%Y-%m-%dT%H:%M:%S"),
             "person_id": "channel:reply", "person_name": "Vira", "channel": "reply",
             "text": "Vira: waiting on you — claude/widget is ready - what happens to it?",
             "ref": None, "ok": True},
            {"at": datetime.fromtimestamp(self.ago(1.9)).strftime("%Y-%m-%dT%H:%M:%S"),
             "person_id": "agent:circuit:run_a:report", "person_name": "Vira", "channel": "agent",
             "text": "Notes capture: 2 new notes", "ref": None, "ok": True}]})

    # ---------------------------------------------------------- the Flow
    def build_flow(self):
        """An hourly Notes capture: Program runs 5, 4 and 2 hours ago, none
        3 hours ago (a missed run), each inside a Flow run."""
        runs, flow_runs = [], []
        for i, h in enumerate((5, 4, 2)):
            pid = f"P{i}"
            runs.append({"id": pid, "routine_id": "prog_export", "routine": "Export notes",
                         "trigger": "schedule", "status": "ok" if h != 4 else "failed",
                         "started": iso(self.ago(h)), "finished": iso(self.ago(h) + 30),
                         "counts": {"new": 2, "updated": 1}, "errors": [] if h != 4 else ["Traceback"],
                         "error_count": 0 if h != 4 else 1, "logs": []})
            flow_runs.append({"id": f"run_{'a' if h == 2 else i}", "circuit_id": "cir_n",
                              "circuit_name": "Notes capture", "input": "go",
                              "cwd": str(self.tmp / "vault"), "source": "routine:rt_h",
                              "status": "done", "entry": ["export"], "started": iso(self.ago(h)),
                              "finished": iso(self.ago(h) + 40), "error": "",
                              "stages_def": [{"id": "export", "name": "Export notes",
                                              "mode": "program"}],
                              "stages": {"export": {"status": "done", "command_run": pid,
                                                    "started": iso(self.ago(h))}}})
        self.write(routinerun.STORE, {"runs": runs, "pruned": {}})
        # the oldest Flow run was evicted by the store and survives in the archive
        self.jsonl(circuits.RUNS_ARCHIVE(), [flow_runs[0]])
        self.write(circuits.RUNS, {"runs": flow_runs[1:]})
        self.write(routines.STORE, {"routines": [
            {"id": "rt_h", "name": "Every hour", "kind": "circuit", "circuit_id": "cir_n",
             "every_hours": 1, "enabled": True, "created": iso(self.ago(48))}]})

    # ---------------------------------------------------------- other apps
    def build_app_session(self):
        rec = self.app / "claude-code-sessions" / "a" / "b" / "local_x.json"
        self.write(rec, {"sessionId": "local_x", "cliSessionId": "s2", "cwd": str(self.tmp / "garden"),
                         "title": "Plan the garden", "model": "claude-sonnet-5-5",
                         "createdAt": int(self.ago(3) * 1000), "lastActivityAt": int(self.ago(2) * 1000),
                         "postTurnSummary": {"status_category": "completed"}})
        u = {"input_tokens": 100, "output_tokens": 1000}
        self.jsonl(self.claude / "projects" / "-garden" / "s2.jsonl", [
            {"type": "user", "timestamp": iso(self.ago(3)), "entrypoint": "claude-desktop",
             "sessionId": "s2", "cwd": str(self.tmp / "garden"),
             "message": {"role": "user", "content": "Plan the garden beds. Use the sun map."}},
            {"type": "assistant", "timestamp": iso(self.ago(3) + 60), "message": {
                "id": "m1", "model": "claude-sonnet-5-5", "usage": u, "content": []}},
            {"type": "assistant", "timestamp": iso(self.ago(3) + 90), "message": {
                "id": "m1", "model": "claude-sonnet-5-5", "usage": u, "content": []}},
            {"type": "assistant", "timestamp": iso(self.ago(2)), "message": {
                "id": "m2", "model": "claude-sonnet-5-5", "usage": u, "content": []}}])

    def build_codex(self):
        db = self.codex / "state_1.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table threads (id text, rollout_path text, cwd text, source text, "
                    "archived int, git_branch text, title text, model text, tokens_used int, "
                    "created_at_ms int, updated_at_ms int, first_user_message text)")
        con.execute("insert into threads values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("th1", "", str(self.tmp / "garden"), "vscode", 0, "", "",
                     "gpt-5-codex", 5000, int(self.ago(6) * 1000), int(self.ago(5) * 1000),
                     "Count the tomatoes. Then sort them."))
        con.commit()
        con.close()

    def build_all(self):
        self.build_repo()
        self.build_request()
        self.build_flow()
        self.build_app_session()
        self.build_codex()

    def ingest(self, full=True):
        rep = I.run(full=full)
        self.case.assertEqual(rep["errors"], {}, rep)
        return rep


class Ingest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(self)
        self.sb.build_all()
        self.rep = self.sb.ingest()
        self.ev = H.load()

    def test_a_second_pass_writes_nothing(self):
        self.assertGreater(self.rep["written"], 20)
        again = I.run(full=False)
        self.assertEqual(again["written"], 0, again)

    def test_vira_session_carries_tokens_cost_time_and_module(self):
        e = self.ev["session:vira:j1"]
        d = e["data"]
        self.assertEqual(d["tokens"], 13000)
        self.assertAlmostEqual(d["cost_usd"], (1000 * 4 + 2000 * 20 + 10000 * 0.2) / 1e6)
        self.assertEqual(d["active_s"], 660.0, "two turns: 600 s and 60 s")
        self.assertEqual(e["module"], "work/ideas", "from the file its Write tool touched")
        self.assertEqual(e["links"]["pr"], ["vira#7"])
        self.assertEqual(e["title"], "Widget: draw in half the time", "the PR title first")
        self.assertEqual(e["project"], "vira")

    def test_landing_answer_is_a_decision(self):
        d = self.ev["decide:job:j1:c1"]
        self.assertEqual((d["sub"], d["status"]), ("landing", H.MERGED))
        self.assertTrue(d["title"].startswith("Merge it:"))
        self.assertNotIn("claude/", d["title"])

    def test_git_merge_commits_and_branches(self):
        merges = [e for e in self.ev.values() if e["sub"] == "merged"]
        self.assertEqual(len(merges), 1)
        m = merges[0]
        self.assertEqual(m["links"]["pr"], ["vira#7"], "found through the PR index by branch")
        self.assertEqual(m["data"]["diff"], {"files": 1, "added": 2, "removed": 0})
        self.assertEqual(m["module"], "work/ideas")
        stale = self.ev["code:branch:vira:claude/stale"]
        self.assertEqual((stale["sub"], stale["status"]), ("not_merged", H.NOT_MERGED))
        self.assertEqual(stale["module"], "core/shell")

    def test_a_branch_deleted_without_a_merge_becomes_dropped(self):
        git(self.sb.repo, "branch", "-q", "-D", "claude/gone")
        rep = I.run(full=True)
        self.assertEqual(rep["errors"], {})
        gone = H.load()["code:branch:vira:claude/gone"]
        self.assertEqual((gone["sub"], gone["status"]), ("discarded", H.DROPPED))
        commits = [e for e in H.load().values() if e["sub"] == "commit"
                   and "vira:claude/gone" in e["links"].get("branch", [])]
        self.assertTrue(commits and all(c["status"] == H.DROPPED for c in commits))

    def test_flow_program_link_and_the_evicted_run_survives(self):
        self.assertIn("run:flow:run_0", self.ev, "the run read from the archive")
        p = self.ev["run:program:P2"]
        self.assertEqual(p["links"]["flow_run"], ["run_a"])
        self.assertEqual(p["title"], "Export notes: 2 new, 1 updated")
        f = self.ev["run:flow:run_a"]
        self.assertEqual(f["links"]["schedule"], ["rt_h"])
        self.assertEqual(f["data"]["counts"], {"new": 2, "updated": 1})
        self.assertEqual(self.ev["run:program:P1"]["status"], H.FAILED)

    def test_text_links_to_its_flow_run_and_its_branch(self):
        tells = {e["title"]: e for e in self.ev.values() if e["kind"] == "tell"}
        self.assertEqual(tells["Notes capture: 2 new notes"]["links"]["flow_run"], ["run_a"])
        branchy = [e for e in tells.values() if e["links"].get("branch")]
        self.assertEqual(branchy[0]["links"]["branch"], ["vira:claude/widget"])

    def test_app_session_counts_each_message_once(self):
        e = self.ev["session:claude:local_x"]
        self.assertEqual(e["data"]["tokens"], 2200)
        self.assertEqual(e["title"], "Plan the garden")
        self.assertEqual(e["status"], H.ANSWERED)
        self.assertAlmostEqual(e["data"]["cost_usd"], (200 * 2 + 2000 * 10) / 1e6)

    def test_codex_thread(self):
        e = self.ev["session:codex:th1"]
        self.assertEqual(e["data"]["tokens"], 5000)
        self.assertIsNone(e["data"]["cost_usd"], "no price table for this model")
        self.assertEqual(e["title"], "Count the tomatoes.")

    def test_ideas_and_their_decisions(self):
        self.assertEqual(self.ev["ask:idea:idea_a"]["status"], H.MERGED)
        self.assertEqual(self.ev["ask:idea:idea_b"]["status"], H.NOT_STARTED,
                         "no session has started on it")
        self.assertEqual(self.ev["ask:idea:idea_b"]["project"], "vira",
                         "the idea's project is the repository's lane")
        dec = [e for e in self.ev.values() if e["kind"] == "decide" and e["sub"] == "declined"]
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["status"], H.DROPPED)

    def test_an_idea_a_session_started_on_waits_on_you(self):
        s = json.loads(ideas.STORE.read_text(encoding="utf-8"))
        s["items"][0]["status"] = "open"
        ideas.STORE.write_text(json.dumps(s), encoding="utf-8")
        I.run(full=True)
        self.assertEqual(H.load()["ask:idea:idea_a"]["status"], H.WAITING,
                         "an Implement session ran on it")

    def test_an_approval_seen_as_a_status_change_is_recorded(self):
        s = json.loads(ideas.STORE.read_text(encoding="utf-8"))
        s["items"][1]["status"] = "open"
        s["items"][1]["updated"] = iso(time.time())
        ideas.STORE.write_text(json.dumps(s), encoding="utf-8")
        I.run(full=False)
        dec = [e for e in H.load().values() if e["kind"] == "decide" and e["sub"] == "approved"]
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["links"]["idea"], ["idea_b"])

    def test_no_title_carries_an_id_branch_or_hash(self):
        import re
        bad = re.compile(r"claude/|codex/|\b[0-9a-f]{12,40}\b|[0-9a-f]{8}-[0-9a-f]{4}-")
        for e in self.ev.values():
            self.assertIsNone(bad.search(e["title"]), e)

    def test_every_status_word_is_one_of_the_eight(self):
        for e in self.ev.values():
            self.assertIn(e["status"], H.STATUSES, e["id"])


class BranchState(unittest.TestCase):
    def test_uncommitted_work_is_not_merged_until_it_is_gone(self):
        sb = Sandbox(self)
        sb.repo.mkdir()
        sb.write(orphanwork.STORE, {"items": [{
            "branch": "claude/wip", "dirty": 3, "worktree": str(sb.repo) + "/.worktrees/wip",
            "last_activity_iso": iso(sb.ago(1)), "in_motion": False,
            "files": ["server/ideas.py"]}]})
        sb.ingest()
        e = H.load()["code:worktree:vira:claude/wip"]
        self.assertEqual((e["status"], e["data"]["dirty"], e["module"]),
                         (H.NOT_MERGED, 3, "work/ideas"))
        self.assertNotIn("claude/", e["title"])
        tid = V.index().resolve("branch:vira:claude/wip")
        self.assertEqual(V.index().trails[tid]["status"], H.NOT_MERGED)
        sb.write(orphanwork.STORE, {"items": []})
        sb.ingest()
        e = H.load()["code:worktree:vira:claude/wip"]
        self.assertEqual((e["sub"], e["status"]), ("worktree_cleared", H.DROPPED))


class Trails(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(self)
        self.sb.build_all()
        self.sb.ingest()
        self.idx = V.index()

    def test_one_trail_from_the_ask_to_the_merge(self):
        tid = self.idx.resolve("idea:idea_a")
        self.assertEqual(tid, "trail:ask:note:note_a", "rooted at the first ask")
        ids = set(self.idx.trails[tid]["members"])
        for eid in ("ask:note:note_a", "ask:idea:idea_a", "session:vira:j1", "decide:job:j1:c1"):
            self.assertIn(eid, ids)
        self.assertTrue(any(i.startswith("code:merged:") for i in ids))
        self.assertTrue(any(i.startswith("tell:") for i in ids), "the text joined by branch")
        t = self.idx.trails[tid]
        self.assertEqual(t["status"], H.MERGED)
        self.assertEqual(t["title"], "Widget: draw in half the time")
        self.assertEqual(t["module"], "work/ideas")

    def test_flow_run_trail_holds_its_program_run_and_text(self):
        tid = self.idx.resolve("flow_run:run_a")
        ids = set(self.idx.trails[tid]["members"])
        self.assertIn("run:program:P2", ids)
        self.assertTrue(any(i.startswith("tell:") for i in ids))
        self.assertEqual(self.idx.trails[tid]["kind"], "automation")
        self.assertEqual(self.idx.trails[tid]["status"], H.RAN)
        self.assertNotEqual(tid, self.idx.resolve("flow_run:run_1"),
                            "two runs of one Flow are two trails")

    def test_open_branch_trail_is_not_merged_and_offers_merge(self):
        tid = self.idx.resolve("branch:vira:claude/stale")
        e = V.entry_of_trail(self.idx, tid)
        self.assertEqual(e["status"], H.NOT_MERGED)
        acts = {a["action"]: a for a in e["actions"]}
        self.assertIn("merge", acts)
        self.assertEqual(acts["merge"]["confirm"], "Merge claude/stale into live main?")
        self.assertEqual(acts["merge"]["body"]["branch"], "claude/stale")

    def test_detail_has_outcome_trail_touched_cost_and_actions(self):
        d = V.detail("idea:idea_a")
        self.assertEqual(d["outcome"]["status"], H.MERGED)
        self.assertEqual(d["outcome"]["diff"]["basis"], "merged into main")
        self.assertEqual(d["outcome"]["diff"]["by_file"], [["server/ideas.py", 2, 0]])
        kinds = [x["kind"] for x in d["trail"]]
        self.assertEqual(kinds[0], "ask")
        waits = [x for x in d["trail"] if x["type"] == "wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["status"], H.ANSWERED, "closed by the landing decision")
        self.assertEqual(d["touched"]["modules"], {"work/ideas": 3})
        self.assertGreater(d["cost_time"]["cost_usd"], 0)
        self.assertEqual(d["cost_time"]["active_s"], 660.0)
        self.assertTrue(any(a["action"] == "judge" for a in d["actions"]))

    def test_list_by_day_pins_unfinished_work(self):
        rec = self.sb.app / "claude-code-sessions" / "a" / "b" / "local_x.json"
        d = json.loads(rec.read_text(encoding="utf-8"))
        d["lastActivityAt"] = int(time.time() * 1000)
        d["postTurnSummary"] = {"status_category": "blocked"}
        rec.write_text(json.dumps(d), encoding="utf-8")
        self.sb.ingest()
        out = V.list_entries("day", V.parse_filters())
        self.assertEqual(out["groups"][0]["key"], "now")
        now_titles = [e["title"] for e in out["groups"][0]["entries"]]
        self.assertEqual(now_titles, ["Plan the garden"], "the session waiting on the owner")
        self.assertNotIn("Paint the shed", now_titles, "a bare waiting idea stays on its day")
        all_titles = [e["title"] for g in out["groups"] for e in g["entries"]]
        self.assertNotIn("Notes capture: 2 new, 1 updated", all_titles,
                         "repeating runs belong to the grid")

    def test_unstarted_ideas_stay_out_of_the_lists_unless_asked(self):
        tid = self.idx.resolve("idea:idea_b")
        self.assertEqual(self.idx.trails[tid]["status"], H.NOT_STARTED)

        def titles(out):
            return [e["title"] for g in out["groups"] for e in g["entries"]]
        for group in ("day", "request", "project"):
            out = V.list_entries(group, V.parse_filters())
            self.assertNotIn("Paint the shed", titles(out), group)
            self.assertEqual(out["unstarted_left_out"], 1, group)
            self.assertEqual(out["statuses"][H.NOT_STARTED], 0)
            self.assertEqual(out["statuses"][H.WAITING], 0, "nothing else waits on the owner")
        out = V.list_entries("day", V.parse_filters(unstarted="1"))
        self.assertIn("Paint the shed", titles(out))
        self.assertEqual(out["unstarted_left_out"], 0)
        self.assertNotEqual(out["groups"][0]["key"], "now", "an unstarted idea is never pinned")
        out = V.list_entries("request", V.parse_filters(status=H.NOT_STARTED))
        self.assertEqual(titles(out), ["Paint the shed"])
        ev = V.list_entries("none", V.parse_filters(), unit="event", limit=500)
        self.assertNotIn("ask:idea:idea_b", [e["id"] for g in ev["groups"] for e in g["entries"]])
        self.assertEqual(ev["unstarted_left_out"], 1)
        fac = {s["key"]: s for s in V.facets()["statuses"]}
        self.assertEqual(fac[H.NOT_STARTED]["trails"], 0)
        self.assertTrue(fac[H.NOT_STARTED]["hidden_by_default"])
        self.assertEqual(V.facets()["unstarted_ideas"], 1)
        self.assertEqual({s["key"]: s for s in V.facets(unstarted=True)["statuses"]}
                         [H.NOT_STARTED]["trails"], 1)
        self.assertEqual(V.detail("idea:idea_b")["outcome"]["status"], H.NOT_STARTED,
                         "the idea's own history still opens")

    def test_a_session_whose_pull_requests_merged_is_merged(self):
        rec = self.sb.app / "claude-code-sessions" / "a" / "b" / "local_x.json"
        d = json.loads(rec.read_text(encoding="utf-8"))
        d["cwd"] = str(self.sb.repo)
        d["lastActivityAt"] = int(time.time() * 1000)
        d["postTurnSummary"] = {"status_category": "review_ready"}
        d["prs"] = [{"prNumber": 7, "state": "MERGED"}]
        rec.write_text(json.dumps(d), encoding="utf-8")
        self.sb.ingest()
        idx = V.index()
        e = idx.events["session:claude:local_x"]
        self.assertEqual(e["own_status"], H.WAITING, "its own word stays stored")
        self.assertEqual(e["status"], H.MERGED)
        tid = idx.resolve("session:local_x")
        self.assertEqual(tid, idx.resolve("idea:idea_a"), "joined by the pull request")
        self.assertEqual(idx.trails[tid]["status"], H.MERGED)
        out = V.list_entries("day", V.parse_filters())
        self.assertNotIn("now", [g["key"] for g in out["groups"]])
        self.assertEqual(out["statuses"][H.WAITING], 0)

    def test_filters(self):
        f = V.parse_filters(status=H.MERGED)
        rows = [e for g in V.list_entries("request", f)["groups"] for e in g["entries"]]
        self.assertEqual({r["title"] for r in rows}, {"Widget: draw in half the time", "Start"},
                         "the merge and the direct commit on main")
        self.assertTrue(all(r["status"] == H.MERGED for r in rows))
        f = V.parse_filters(module="work")
        rows = [e for g in V.list_entries("module", f)["groups"] for e in g["entries"]]
        self.assertTrue(rows and all(any(m.startswith("work") for m in r["modules"]) for r in rows))
        f = V.parse_filters(q="garden beds")
        rows = [e for g in V.list_entries("request", f)["groups"] for e in g["entries"]]
        self.assertEqual([r["title"] for r in rows], ["Plan the garden"])
        f = V.parse_filters(since=datetime.fromtimestamp(self.sb.ago(4)).strftime("%Y-%m-%d"),
                            project="vira")
        rows = [e for g in V.list_entries("request", f)["groups"] for e in g["entries"]]
        self.assertTrue(all("vira" in r["projects"] for r in rows))

    def test_every_grouping(self):
        for group in V.GROUPS:
            out = V.list_entries(group, V.parse_filters(automations="1"))
            self.assertGreater(out["total"], 0, group)
        auto = V.list_entries("automation", V.parse_filters())
        self.assertEqual({g["label"] for g in auto["groups"]}, {"Notes capture", "Export notes"})
        with self.assertRaises(ValueError):
            V.list_entries("weekday")


class Grid(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(self)
        self.sb.build_flow()
        self.sb.repo.mkdir()
        self.sb.ingest()

    def test_the_hour_with_no_run_is_missed(self):
        since = iso(self.sb.ago(7))
        g = V.grid(span="hour", since=since)
        rows = {r["id"]: r for r in g["rows"]}
        prog = rows["program:prog_export"]
        self.assertEqual([s["id"] for s in prog["schedules"]], ["rt_h"],
                         "the Program inherits its Flow's Schedule")
        def at(hours):
            ts = self.sb.ago(hours)
            return next(c["outcome"] for c in prog["cells"]
                        if H.to_ts(c["start"]) <= ts < H.to_ts(c["end"]))
        self.assertEqual(at(5), H.RAN)
        self.assertEqual(at(4), H.FAILED)
        self.assertEqual(at(3), "missed")
        self.assertEqual(at(2), H.RAN)
        self.assertEqual(at(6.5), "none", "before the first stored run nothing is missed")
        self.assertEqual(at(1), "missed", "the hour after the last run was missed too")
        self.assertGreaterEqual(prog["missed"], 2)

    def test_day_span_and_bad_span(self):
        g = V.grid(span="day")
        self.assertEqual(len(g["columns"]), 14)
        with self.assertRaises(ValueError):
            V.grid(span="week")


class TimeMap(unittest.TestCase):
    def test_lanes_hold_spans_points_and_ticks(self):
        sb = Sandbox(self)
        sb.build_all()
        sb.ingest()
        tm = V.timemap(since=iso(sb.ago(60)))
        lanes = {ln["key"]: ln for ln in tm["lanes"]}
        self.assertEqual(len(lanes["vira"]["points"]), 1, "the merge")
        self.assertEqual(len(lanes["vira"]["spans"]), 1, "the Vira session")
        self.assertTrue(lanes["vault"]["ticks"], "runs in their folder's lane")
        mod = V.timemap(since=iso(sb.ago(60)), lane="module")
        self.assertIn("work/ideas", {ln["key"] for ln in mod["lanes"]})
        with self.assertRaises(ValueError):
            V.timemap(lane="person")


class Hooks(unittest.TestCase):
    """The stores that trim their records now archive them first."""

    def setUp(self):
        self.sb = Sandbox(self)

    def test_flow_runs_evicted_by_the_store_reach_the_archive(self):
        with mock.patch.object(circuits, "RUNS_KEEP", 2), \
                mock.patch.object(circuits, "get_circuit", lambda cid: {
                    "id": cid, "name": "T", "stages": [
                        {"id": "a", "name": "A", "mode": "output", "needs": []}]}):
            for _ in range(4):
                circuits.start_run("cir_t", "hello")
                circuits._mutate_runs(lambda s: [r.update(status="done") for r in s["runs"]]
                                      or True)
        kept = circuits._load_runs()["runs"]
        archived = I._read_jsonl(circuits.RUNS_ARCHIVE())
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(archived), 2)
        self.assertEqual(len({r["id"] for r in I._flow_runs()}), 4, "no run is lost")

    def test_notification_log_trim_archives(self):
        with mock.patch.object(notify, "_save_log", wraps=notify._save_log):
            for i in range(203):
                notify._record({"at": f"2026-11-04T10:{i // 60:02d}:{i % 60:02d}", "text": f"t{i}",
                                "person_id": "channel:reply", "ok": True})
        self.assertEqual(len(json.loads(notify.LOG.read_text(encoding="utf-8"))["sent"]), 200)
        arch = I._read_jsonl(notify.LOG.with_name("notify-log-archive.jsonl"))
        self.assertEqual([r["text"] for r in arch], ["t0", "t1", "t2"])

    def test_journal_trim_archives(self):
        with mock.patch.object(journal, "MAX_ENTRIES", 2):
            for i in range(3):
                journal.add(f"note {i}", integrate=False)
        arch = I._read_jsonl(journal.STORE.with_name("brief-journal-archive.jsonl"))
        self.assertEqual([r["text"] for r in arch], ["note 0"])
        notes = [e for e in I.from_journal(I.Ctx())]
        self.assertEqual(len(notes), 3)

    def test_merge_pressed_on_a_branch_is_a_decision(self):
        done = subprocess.CompletedProcess([], 0, stdout="merged", stderr="")
        with mock.patch.object(orphanwork.subprocess, "run", return_value=done), \
                mock.patch.object(orphanwork, "refresh", lambda: None):
            ok, _ = orphanwork._run_action("widget", ["merge", "widget"], "merge")
            self.assertTrue(ok)
            for _ in range(200):
                if orphanwork._actions.get("claude/widget", {}).get("status") != "running":
                    break
                time.sleep(0.01)
        rows = I._read_jsonl(orphanwork.ACTIONS_LOG())
        self.assertEqual(len(rows), 2, "one line at the press, one with the outcome")
        evs = I.from_branch_actions(I.Ctx())
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["status"], evs[0]["data"]["ok"]), (H.MERGED, True))


if __name__ == "__main__":
    unittest.main()

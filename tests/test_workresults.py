"""Work inventory: source identity, real dates, isolated reads and detail joins."""
import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import workresults


class Inventory(unittest.TestCase):
    def test_one_branch_joins_jobs_flow_and_record_with_stable_aliases(self):
        branch = "codex/example"
        gallery = {"items": [{"branch": branch, "title": "Example", "band": "unlanded",
                              "last_activity": 100, "job": {"id": "j2"}}]}
        jobs = [{"id": "j1", "branch": branch, "session_id": "s1", "status": "done",
                 "started": "2026-01-01T12:00:00Z"},
                {"id": "j2", "branch": branch, "session_id": "s2", "status": "done"}]
        flows = [{"id": "f1", "stages": {"build": {"job_id": "j1"}}, "status": "done"}]
        groups = [{"entries": [{"kind": "job", "job_id": "j1", "retro": "source-retro"}]}]
        rows = workresults.build_inventory(gallery, jobs=jobs, flows=flows, groups=groups)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "branch:" + branch)
        self.assertCountEqual(row["aliases"], ["branch:" + branch, "job:j1", "job:j2",
                                              "flow:f1", "session:s1", "session:s2"])
        self.assertEqual(row["retros"], ["source-retro"])
        gallery["items"][0]["key"] = "new-dirty-rearm-key"
        self.assertEqual(workresults.build_inventory(gallery, jobs=jobs)[0]["id"], row["id"])

    def test_live_state_overrides_ledger_without_duplicate(self):
        jobs = [{"id": "one", "title": "Same work", "status": "done", "branch": "claude/task"}]
        rows = workresults.build_inventory(jobs=jobs, live=[{"id": "one", "status": "running", "awaiting": "permission"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "waiting")
        self.assertEqual(rows[0]["job_ids"], ["one"])

    def test_flow_owns_unbranched_stage_jobs_even_missing_from_ledger(self):
        rows = workresults.build_inventory(
            jobs=[{"id": "j1", "status": "done"}],
            flows=[{"id": "f1", "stages": {"a": {"job_id": "j1"}, "b": {"job_id": "j2"}}}])
        self.assertEqual([row["id"] for row in rows], ["flow:f1"])
        self.assertCountEqual(rows[0]["job_ids"], ["j1", "j2"])

    def test_multi_branch_flow_keeps_distinct_artifacts_and_links_them(self):
        rows = workresults.build_inventory(
            jobs=[{"id": "a", "branch": "codex/a"}, {"id": "b", "branch": "claude/b"}],
            flows=[{"id": "f", "stages": {"a": {"job_id": "a"}, "b": {"job_id": "b"}}}])
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(len(rows), 3)
        self.assertEqual(by_id["flow:f"]["related_ids"], ["branch:claude/b", "branch:codex/a"])
        self.assertEqual(by_id["branch:codex/a"]["job_ids"], ["a"])

    def test_invalid_dates_never_become_now(self):
        rows = workresults.build_inventory(jobs=[{"id": "j", "started": "invalid"}], groups=[{
            "date": "2026-01-02", "entries": [{"kind": "ship", "text": "Undated", "ts": ""}]}])
        self.assertTrue(all(row["updated_ts"] == 0 and row["updated_at"] == "" for row in rows))

    def test_sorts_real_timestamps_across_timezones(self):
        rows = workresults.build_inventory(jobs=[
            {"id": "older", "started": "2026-01-02T01:00:00+03:00"},
            {"id": "newer", "started": "2026-01-01T23:00:00Z"}])
        self.assertEqual([row["id"] for row in rows], ["job:newer", "job:older"])

    def test_preview_is_real_source_visual_not_a_generated_placeholder(self):
        rows = workresults.build_inventory({"items": [
            {"branch": "codex/a", "visual": "shots/a.png", "orphan_key": "a:abc:0"},
            {"branch": "claude/b"}]})
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["branch:codex/a"]["preview_url"],
                         "/api/orphanwork/visual?key=a%3Aabc%3A0&path=shots%2Fa.png")
        self.assertEqual(by_id["branch:claude/b"]["preview_url"], "")
        self.assertTrue(by_id["branch:codex/a"]["can_preview"])

    def test_equal_titles_do_not_merge_unrelated_work(self):
        rows = workresults.build_inventory(jobs=[{"id": "a", "title": "Same"}, {"id": "b", "title": "Same"}])
        self.assertEqual(len(rows), 2)

    def test_receipt_is_durable_distinct_source_with_original_destination(self):
        receipt = {"id": "source-1", "title": "Budget document", "status": "saved",
                   "updated_at": "2026-01-01T12:00:00Z", "vault_id": "personal",
                   "path": "raw/finances/budget.md"}
        rows = workresults.build_inventory(receipts=[receipt])
        self.assertEqual(rows[0]["id"], "receipt:source-1")
        self.assertEqual(rows[0]["receipt"]["path"], receipt["path"])


class Phases(unittest.TestCase):
    """Results lists finished work; anything waiting on the owner is in
    Attention. A finished status names where the work ended up."""

    def by_id(self, **kw):
        return {row["id"]: row for row in workresults.build_inventory(**kw)}

    def test_an_unlanded_branch_waits_in_attention_with_its_session(self):
        # The 2026-10-10 case: an Implement session left code uncommitted on
        # its branch. It read "Complete" beside an Attention row saying the
        # branch was unmerged - two cards, two answers.
        job = {"id": "impl", "title": "Learn the voice · Implement", "status": "done",
               "idea_id": "idea_1", "result": "Drafts now sound like the owner."}
        rows = self.by_id(orphans=[{"branch": "claude/voice", "kind": "dirty", "dirty": 3}],
                          jobs=[job], written={"impl": "claude/voice"})
        self.assertEqual(list(rows), ["branch:claude/voice"])
        row = rows["branch:claude/voice"]
        self.assertEqual(row["phase"], "attention")
        self.assertEqual(row["status"], "unlanded")
        self.assertEqual(row["job_ids"], ["impl"])

    def test_without_the_written_branch_the_session_is_a_second_result(self):
        job = {"id": "impl", "status": "done", "idea_id": "idea_1"}
        rows = self.by_id(orphans=[{"branch": "claude/voice", "kind": "dirty"}], jobs=[job])
        self.assertEqual(len(rows), 2)

    def test_work_in_motion_is_in_progress_not_waiting(self):
        rows = self.by_id(orphans=[{"branch": "claude/x", "kind": "dirty", "in_motion": True}])
        self.assertEqual(rows["branch:claude/x"]["phase"], "active")

    def test_merged_branch_is_shipped(self):
        rows = self.by_id(showroom_data={"items": [{"branch": "claude/a", "band": "landed"}]})
        self.assertEqual((rows["branch:claude/a"]["phase"], rows["branch:claude/a"]["status"]),
                         ("finished", "shipped"))

    def test_a_torn_down_branch_reads_its_pull_request(self):
        jobs = [{"id": "m", "branch": "claude/m", "status": "done", "idea_id": "i"},
                {"id": "c", "branch": "claude/c", "status": "done", "idea_id": "i"},
                {"id": "n", "branch": "claude/n", "status": "done", "idea_id": "i"}]
        rows = self.by_id(jobs=jobs, prs={"claude/m": {"state": "MERGED"},
                                          "claude/c": {"state": "CLOSED"}})
        self.assertEqual(rows["branch:claude/m"]["status"], "shipped")
        self.assertEqual(rows["branch:claude/c"]["status"], "discarded")
        # No PR on record and the branch is gone: say only that it ended.
        self.assertEqual(rows["branch:claude/n"]["status"], "ended")
        self.assertTrue(all(row["phase"] == "finished" for row in rows.values()))

    def test_a_finished_session_names_what_it_produced(self):
        rows = self.by_id(jobs=[
            {"id": "chat", "status": "done", "meta": {"kind": "chat"}},
            {"id": "muse", "status": "done", "meta": {"routine_id": "muse"}},
            {"id": "map", "status": "done", "branch": "claude/cartographer-1",
             "meta": {"kind": "map-build"}},
            {"id": "bad", "status": "error"}])
        self.assertEqual(rows["job:chat"]["status"], "answered")
        self.assertEqual(rows["job:muse"]["status"], "ran")
        # A tool-only job's throwaway branch is not code that went unmerged.
        self.assertEqual(rows["branch:claude/cartographer-1"]["status"], "finished")
        self.assertEqual(rows["job:bad"]["status"], "error")
        self.assertTrue(all(row["phase"] == "finished" for row in rows.values()))

    def test_live_sessions_are_active_or_waiting(self):
        rows = self.by_id(jobs=[{"id": "a", "status": "done"}, {"id": "b", "status": "done"}],
                          live=[{"id": "a", "status": "running"},
                                {"id": "b", "status": "running", "awaiting": "permission"}])
        self.assertEqual(rows["job:a"]["phase"], "active")
        self.assertEqual(rows["job:b"]["phase"], "attention")

    def test_a_flow_waiting_on_review_is_in_attention(self):
        rows = self.by_id(flows=[{"id": "f", "status": "waiting"}, {"id": "g", "status": "done"}])
        self.assertEqual(rows["flow:f"]["phase"], "attention")
        self.assertEqual(rows["flow:g"]["phase"], "finished")


class WrittenBranch(unittest.TestCase):
    """An unplaced session's branch, from where its own write tools wrote."""

    def transcript(self, *blocks):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, tmp.name)
        with tmp:
            for name, path in blocks:
                tmp.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": name, "input": {"file_path": path}}]}}) + "\n")
        return tmp.name

    def test_the_branch_with_the_most_writes_wins_and_reads_never_count(self):
        live = {"/repo/.claude/worktrees/app-one": "claude/app-one"}
        path = self.transcript(
            ("Edit", "/repo/.worktrees/voice/server/voice.py"),
            ("Write", "/repo/.worktrees/voice/tests/test_voice.py"),
            ("Read", "/repo/.worktrees/other/server/radar.py"),
            ("Read", "/repo/.worktrees/other/server/brief.py"),
            ("Read", "/repo/.worktrees/other/server/main.py"),
            ("Edit", "/repo/.claude/worktrees/app-one/README.md"))
        self.assertEqual(workresults.written_branch({"transcript": path}, live), "claude/voice")

    def test_windows_paths_match_either_separator(self):
        live = {"C:/work/vira/.claude/worktrees/app-one": "claude/app-one"}
        path = self.transcript(
            ("Edit", "C:\\work\\vira\\.claude\\worktrees\\app-one\\a.py"),
            ("Edit", "C:\\work\\vira\\.claude\\worktrees\\app-one\\b.py"),
            ("Edit", "C:\\work\\vira\\.worktrees\\gone\\c.py"))
        self.assertEqual(workresults.written_branch({"transcript": path}, live), "claude/app-one")
        gone = self.transcript(("Write", "C:\\work\\vira\\.worktrees\\gone\\c.py"))
        self.assertEqual(workresults.written_branch({"transcript": gone}, {}), "claude/gone")

    def test_main_and_unknown_folders_never_name_a_branch(self):
        path = self.transcript(("Edit", "/home/someone/notes.md"))
        self.assertEqual(workresults.written_branch({"transcript": path}, {"/repo": "main"}), "")
        self.assertEqual(workresults.written_branch({"transcript": "/missing.jsonl"}, {}), "")

    def test_only_finished_unplaced_coding_sessions_are_read(self):
        path = self.transcript(("Edit", "/repo/.worktrees/voice/a.py"))
        jobs = [{"id": "impl", "idea_id": "i", "status": "done", "transcript": path},
                {"id": "placed", "idea_id": "i", "status": "done", "transcript": path,
                 "branch": "claude/own"},
                {"id": "chat", "meta": {"kind": "chat"}, "status": "done", "transcript": path},
                {"id": "live", "idea_id": "i", "status": "running", "transcript": path}]
        with mock.patch.object(workresults.orphanwork, "_porcelain_worktrees", return_value=[]):
            self.assertEqual(workresults._written_branches(jobs), {"impl": "claude/voice"})


class ReadSurface(unittest.TestCase):
    def setUp(self):
        # All eight external stores, plus live process state and the extension
        # provider, are isolated. An empty fixture must never read owner data.
        self.patches = [
            mock.patch.object(workresults.showroom, "compose", return_value={"items": []}),
            mock.patch.object(workresults.orphanwork, "compose", return_value={"items": []}),
            mock.patch.object(workresults.joblog, "list_records", return_value=[]),
            mock.patch.object(workresults.circuits, "list_runs", return_value=[]),
            mock.patch.object(workresults.changelog, "groups", return_value=[]),
            mock.patch.object(workresults, "_live_jobs", return_value=[]),
            mock.patch.object(workresults, "receipt_provider", return_value=[]),
            mock.patch.object(workresults.prindex, "_read", return_value={}),
            mock.patch.object(workresults.orphanwork, "_porcelain_worktrees", return_value=[]),
        ]
        self.mocks = [patch.start() for patch in self.patches]
        self.addCleanup(lambda: [patch.stop() for patch in reversed(self.patches)])
        fixture = mock.patch.object(workresults.settings, "fixture_mode", return_value=False)
        fixture.start()
        self.addCleanup(fixture.stop)
        app = FastAPI()
        app.include_router(workresults.router)
        self.client = TestClient(app)

    def test_an_empty_fixture_has_no_results_or_side_effects(self):
        with mock.patch.object(workresults.showroom, "refresh") as refresh, \
                mock.patch.object(workresults.showroom, "_kick_describe") as describe:
            response = self.client.get("/api/work/results")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(response.json()["errors"], {})
        refresh.assert_not_called()
        describe.assert_not_called()

    def test_broken_source_is_named_and_other_results_survive(self):
        self.mocks[0].side_effect = OSError("branch store unreadable")
        self.mocks[2].return_value = [{"id": "j", "title": "Available"}]
        with self.assertLogs(workresults.log, level="WARNING"):
            result = self.client.get("/api/work/results").json()
        self.assertEqual(result["items"][0]["title"], "Available")
        self.assertIn("branch store unreadable", result["errors"]["branches"])

    def test_unknown_detail_has_honest_404(self):
        self.assertEqual(self.client.get("/api/work/results/detail", params={"id": "branch:codex/missing"}).status_code, 404)

    def test_job_alias_opens_same_branch_detail(self):
        self.mocks[2].return_value = [{"id": "j", "branch": "codex/a", "title": "Example"}]
        with mock.patch.object(workresults.joblog, "get_record", return_value={"id": "j", "transcript": "/synthetic/session.jsonl"}):
            result = self.client.get("/api/work/results/detail", params={"id": "job:j"}).json()
        self.assertEqual(result["item"]["id"], "branch:codex/a")
        self.assertEqual(result["jobs"][0]["transcript"], "/synthetic/session.jsonl")

    def test_inventory_is_readable(self):
        with mock.patch("server.instance.metadata", return_value={"kind": "branch", "id": "sample"}):
            self.assertEqual(self.client.get("/api/work/results").json()["instance"]["kind"], "branch")

    def test_fixture_never_reads_machine_work_or_external_provider(self):
        with mock.patch.object(workresults.settings, "fixture_mode", return_value=True):
            data = self.client.get("/api/work/results").json()
            self.assertTrue(data["fixture"])
            self.assertEqual(data["total"], 3)
            self.assertTrue(all(row["fixture"] for row in data["items"]))
            result = self.client.get("/api/work/results/detail", params={"id": data["items"][0]["id"]})
            self.assertEqual(result.status_code, 200)
        for source in self.mocks:
            source.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""The Sessions vault (server/sessionvault.py): every session copied whole,
rendered readable, filed by date, and the retro writer's files placed in
the session and day folders. Every source is synthetic and lives in a
temporary folder; nothing on the real machine is read."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from server import sessionretro, sessionvault as sv

SID = "11111111-2222-3333-4444-555555555555"
CODEX_ID = "01a1e53f-17e6-73e3-9095-8a3bd038b32c"


def _ts(day, hm):
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").astimezone().isoformat()


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        t = self.tmp
        self.over = {"root": t / "vault", "store": t / "store.json", "claude": t / "claude",
                     "codex": t / "codex", "jobs": t / "jobs", "app": t / "app",
                     "file_history": t / "fh", "scratch": t / "scratch",
                     "codex_db": t / "codexhome" / "state_5.sqlite", "cloud": t / "cloud"}
        patcher = mock.patch.dict(sv._override, {k: str(v) for k, v in self.over.items()})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("claude", "codex", "jobs", "app", "file_history", "scratch", "cloud"):
            self.over[k].mkdir(parents=True)
        self.day = "2026-11-09"
        self.log = t / "claude" / "-Users-x-proj" / f"{SID}.jsonl"
        _write_jsonl(self.log, [
            {"type": "user", "timestamp": _ts(self.day, "10:37"), "cwd": "/nowhere/proj",
             "entrypoint": "claude-desktop", "sessionId": SID,
             "message": {"role": "user", "content": "Can Taurid Capital hold the OneDrive for the new company?"}},
            {"type": "assistant", "timestamp": _ts(self.day, "10:38"),
             "message": {"model": "claude-opus-5-5", "content": [
                 {"type": "thinking", "thinking": "Check the OneDrive folders first.", "signature": "x"},
                 {"type": "text", "text": "Looking at the folders now."},
                 {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls OneDrive"}}],
                 "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "user", "timestamp": _ts(self.day, "10:39"),
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "_TC-LLC\nVendors\nExpenses"}]}},
            {"type": "assistant", "timestamp": _ts(self.day, "10:40"),
             "message": {"model": "claude-opus-5-5",
                         "content": [{"type": "text", "text": "Yes, with three changes."}]}},
        ])
        side = self.log.parent / SID / "subagents"
        _write_jsonl(side / "agent-abc.jsonl", [
            {"type": "assistant", "isSidechain": True, "timestamp": _ts(self.day, "10:38"),
             "message": {"content": [{"type": "text", "text": "helper found 3 folders"}]}}])
        (self.over["file_history"] / SID).mkdir()
        (self.over["file_history"] / SID / "abc@v1").write_text("old file body", encoding="utf-8")
        (self.over["scratch"] / "-Users-x-proj" / SID).mkdir(parents=True)
        (self.over["scratch"] / "-Users-x-proj" / SID / "notes.txt").write_text("scratch", encoding="utf-8")
        rec = self.over["app"] / "claude-code-sessions" / "a" / "b" / f"local_{SID}.json"
        rec.parent.mkdir(parents=True)
        rec.write_text(json.dumps({"sessionId": f"local_{SID}", "cliSessionId": SID,
                                   "title": "OneDrive review for the new company",
                                   "forkedFromSessionId": "local_parent",
                                   "bridgeSessionIds": ["session_01MIRROR"]}), encoding="utf-8")
        self.codex = t / "codex" / "2026" / "11" / "16" / f"rollout-2026-11-16T11-44-39-{CODEX_ID}.jsonl"
        _write_jsonl(self.codex, [
            {"type": "session_meta", "timestamp": _ts("2026-11-16", "11:44"),
             "payload": {"cwd": "/nowhere/algonquin", "originator": "codex_cli_rs"}},
            {"type": "event_msg", "timestamp": _ts("2026-11-16", "11:44"),
             "payload": {"type": "user_message",
                         "message": "# Files mentioned by the user:\n\n## a.jpg\n\n## My request:\nRender the hallway."}},
            {"type": "response_item", "timestamp": _ts("2026-11-16", "11:45"),
             "payload": {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA"}},
            {"type": "response_item", "timestamp": _ts("2026-11-16", "11:45"),
             "payload": {"type": "function_call", "name": "shell", "arguments": "{\"command\": [\"ls\"]}"}},
            {"type": "response_item", "timestamp": _ts("2026-11-16", "11:45"),
             "payload": {"type": "function_call_output", "output": "hallway.png"}},
            {"type": "event_msg", "timestamp": _ts("2026-11-16", "11:46"),
             "payload": {"type": "agent_message", "message": "Rendered."}},
        ])
        job = self.over["jobs"] / "abcdef123456"
        job.mkdir()
        (job / "job.json").write_text(json.dumps({"prompt": "Find the session", "started": 1795023563.9,
                                                  "subject": "Find the session"}), encoding="utf-8")
        (job / "state.json").write_text(json.dumps({"session_id": SID}), encoding="utf-8")
        (job / "events.jsonl").write_text(json.dumps(
            {"event": "message_completed", "t": 1795023600, "phase": "thinking", "text": "grep first"}) + "\n",
            encoding="utf-8")
        lone = self.over["jobs"] / "fedcba654321"
        lone.mkdir()
        (lone / "job.json").write_text(json.dumps({"prompt": "Summarize mail", "started": 1795023563.9}),
                                       encoding="utf-8")
        (lone / "state.json").write_text(json.dumps({"result_text": "Done"}), encoding="utf-8")
        (lone / "events.jsonl").write_text(json.dumps(
            {"event": "message_completed", "t": 1795023600, "phase": "final_answer", "text": "Mail summarized."})
            + "\n", encoding="utf-8")

    def folder(self, key):
        return sv.folder_for(key, root=self.over["root"])


class SyncTests(VaultCase):
    def test_a_session_is_filed_by_day_with_every_file_copied_unchanged(self):
        counts = sv.sync()
        self.assertEqual(counts.get("new"), 3)       # claude, codex, the lone Vira job
        f = self.folder(f"claude:{SID}")
        self.assertEqual(f.parent, sv.day_dir(self.day, self.over["root"]))
        self.assertIn("proj - OneDrive review for the new company", f.name)
        orig = f / "original"
        self.assertEqual(_sha(orig / f"{SID}.jsonl"), _sha(self.log))
        self.assertTrue((orig / "session-folder" / "subagents" / "agent-abc.jsonl").is_file())
        self.assertTrue((orig / "file-history" / "abc@v1").is_file())
        self.assertTrue((orig / "scratchpad" / "notes.txt").is_file())
        self.assertTrue((orig / "app-record" / f"local_{SID}.json").is_file())
        self.assertTrue((orig / "vira-job-abcdef123456" / "job.json").is_file())
        man = json.loads((orig / "manifest.json").read_text(encoding="utf-8"))["files"]
        self.assertEqual(man[f"{SID}.jsonl"]["sha256"], _sha(self.log))

    def test_the_conversation_holds_every_word_and_the_work_page_every_output(self):
        sv.sync()
        f = self.folder(f"claude:{SID}")
        conv = (f / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("Can Taurid Capital hold the OneDrive for the new company?", conv)
        self.assertIn("Check the OneDrive folders first.", conv)     # readable thinking
        self.assertIn("Yes, with three changes.", conv)
        self.assertIn("- Step 1: Bash - ls OneDrive", conv)
        self.assertIn("forked_from: local_parent", conv)
        self.assertIn("started_by: you", conv)
        self.assertIn("claude://claude.ai/epitaxy/local_" + SID, conv)
        work = (f / "work" / "work.md").read_text(encoding="utf-8")
        self.assertIn("_TC-LLC\nVendors\nExpenses", work)
        self.assertIn("grep first", work)                              # the Vira job's thinking
        helper = (f / "work" / "helpers" / "agent-abc.md").read_text(encoding="utf-8")
        self.assertIn("helper found 3 folders", helper)

    def test_codex_titles_come_from_the_request_and_encrypted_thinking_is_noted(self):
        sv.sync()
        f = self.folder(f"codex:{CODEX_ID}")
        self.assertIn("Render the hallway", f.name)
        conv = (f / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("thinking_steps_encrypted: 1", conv)
        self.assertIn("stored encrypted", conv)
        self.assertIn("Rendered.", conv)
        self.assertIn("hallway.png", (f / "work" / "work.md").read_text(encoding="utf-8"))

    def test_a_vira_job_without_a_transcript_is_its_own_session(self):
        sv.sync()
        f = self.folder("vira:fedcba654321")
        conv = (f / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("Summarize mail", conv)
        self.assertIn("Mail summarized.", conv)
        # the job that ran through Claude lives in that session's folder
        self.assertEqual(self.folder("vira:abcdef123456"), self.folder(f"claude:{SID}"))

    def test_appended_logs_update_the_copy_and_rewritten_logs_keep_the_old_one(self):
        sv.sync()
        f = self.folder(f"claude:{SID}")
        self.assertEqual(sv.sync().get("unchanged"), 3)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": _ts(self.day, "11:00"),
                                 "message": {"content": "One more question."}}) + "\n")
        sv.sync()
        self.assertEqual(_sha(f / "original" / f"{SID}.jsonl"), _sha(self.log))
        self.assertFalse((f / "original" / "versions").exists())
        old = (f / "original" / f"{SID}.jsonl").read_bytes()
        self.log.write_text(json.dumps({"type": "user", "timestamp": _ts(self.day, "12:00"),
                                        "message": {"content": "rewritten"}}) + "\n", encoding="utf-8")
        sv.sync()
        kept = list((f / "original" / "versions").rglob(f"{SID}.jsonl"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), old)

    def test_a_folder_is_never_renamed_when_the_title_changes(self):
        sv.sync()
        before = self.folder(f"claude:{SID}")
        rec = next((self.over["app"] / "claude-code-sessions").rglob("*.json"))
        rec.write_text(json.dumps({"sessionId": f"local_{SID}", "cliSessionId": SID,
                                   "title": "A new title"}), encoding="utf-8")
        sv.sync()
        self.assertEqual(self.folder(f"claude:{SID}"), before)
        self.assertIn("# A new title", (before / "conversation.md").read_text(encoding="utf-8"))

    def test_check_lists_a_session_not_yet_in_the_vault(self):
        res = sv.check()
        self.assertEqual(res["counts"]["sessions not in the vault"], 3)
        sv.sync()
        res = sv.check()
        self.assertNotIn("sessions not in the vault", res["counts"])
        self.assertNotIn("files not copied", res["counts"])

    def test_the_store_can_be_rebuilt_from_the_folders(self):
        sv.sync()
        f = self.folder(f"claude:{SID}")
        Path(self.over["store"]).unlink()
        sv.rebuild_store()
        self.assertEqual(self.folder(f"claude:{SID}"), f)
        self.assertEqual(self.folder(f"local_{SID}"), f)


class RetroPlacementTests(VaultCase):
    def test_retro_files_skip_conversations_work_and_copies(self):
        sv.sync()
        f = self.folder(f"claude:{SID}")
        (f / "2026-11-09 1037 proj.md").write_text("---\nsession_id: x\n---\n", encoding="utf-8")
        found = sv.retro_files(root=self.over["root"])
        self.assertEqual([p.name for p in found], ["2026-11-09 1037 proj.md"])

    def test_the_retro_writer_writes_into_the_session_and_day_folders(self):
        sv.sync()
        with mock.patch.object(sessionretro, "_dir_override", None):
            s = {"tool": "claude", "sid": SID, "ids": [f"local_{SID}"], "project": "proj",
                 "start": datetime.strptime(f"{self.day} 10:37", "%Y-%m-%d %H:%M").timestamp()}
            path = sessionretro._vault_path(s)
            self.assertEqual(path.parent, self.folder(f"claude:{SID}"))
            self.assertEqual(path.name, "2026-11-09 1037 proj.md")
            self.assertEqual(sessionretro._day_folder(self.day, Path("/flat")),
                             sv.day_dir(self.day, self.over["root"]))
            self.assertEqual(sessionretro._week_folder(Path("/flat")), self.over["root"] / "weeks")

    def test_the_retro_writer_keeps_the_flat_folder_when_there_is_no_vault(self):
        with mock.patch.dict(sv._override, {"root": None}), \
                mock.patch.object(sv.settings, "get", return_value=""):
            self.assertIsNone(sessionretro._vault())
            self.assertEqual(sessionretro._day_folder(self.day, Path("/flat")), Path("/flat"))


class CloudTests(VaultCase):
    def _export(self):
        exp = self.tmp / "export"
        exp.mkdir()
        lines = [
            {"session": {"id": "session_01MIRROR", "title": "OneDrive review", "environment_kind": "bridge",
                         "created_at": "2026-11-09T15:37:00Z"},
             "events": [{"type": "user", "created_at": "2026-11-09T15:37:01Z",
                         "message": {"role": "user", "content": "Can Taurid Capital hold the OneDrive?"}}]},
            {"session": {"id": "session_01ONLYCLOUD", "title": "Build resume page",
                         "environment_kind": "anthropic_cloud", "created_at": "2026-05-17T15:52:23Z"},
             "events": [{"type": "user", "created_at": "2026-05-17T15:52:24Z",
                         "message": {"role": "user", "content": "Build my resume page."}},
                        {"type": "assistant", "created_at": "2026-05-17T15:53:00Z",
                         "message": {"content": [{"type": "text", "text": "Built it."}]}}]},
        ]
        (exp / "claude-ai-sessions-001.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        return exp

    def test_a_remote_control_copy_is_kept_with_its_mac_session_and_a_cloud_session_stands_alone(self):
        counts = sv.import_cloud(self._export(), dest=self.over["cloud"])
        self.assertEqual(counts["sessions written"], 2)
        sv.sync()
        mac = self.folder(f"claude:{SID}")
        self.assertEqual(self.folder("cloud:session_01MIRROR"), mac)
        self.assertTrue((mac / "original" / "claude-ai" / "session_01MIRROR.events.jsonl").is_file())
        alone = self.folder("cloud:session_01ONLYCLOUD")
        self.assertNotEqual(alone, mac)
        self.assertEqual(alone.parent, sv.day_dir("2026-05-17", self.over["root"]))
        conv = (alone / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("Build my resume page.", conv)
        self.assertIn("Built it.", conv)
        self.assertNotIn("files not copied", sv.check()["counts"])

    def test_unpacking_again_rewrites_nothing_that_did_not_change(self):
        exp = self._export()
        sv.import_cloud(exp, dest=self.over["cloud"])
        self.assertEqual(sv.import_cloud(exp, dest=self.over["cloud"]).get("unchanged"), 2)


class AdoptTests(VaultCase):
    def test_old_retros_and_rollups_move_into_place_and_nothing_is_overwritten(self):
        sv.sync()
        flat = self.tmp / "flat"
        flat.mkdir()
        (flat / "2026-11-09 1037 proj.md").write_text(
            f"---\nsession_id: {SID}\nproject: proj\n---\n# retro\n", encoding="utf-8")
        (flat / "2026-11-09 day proj.md").write_text("---\ndate: 2026-11-09\n---\n", encoding="utf-8")
        (flat / "2026-11-14 week.md").write_text("week", encoding="utf-8")
        (flat / "2026-05-17 1009 oldsite.md").write_text(
            "---\nproject: oldsite\ndate: 2026-05-17\ntime: \"10:09\"\ncwd: /gone\n---\n# oldsite\n\nBuilt the page.\n",
            encoding="utf-8")
        f = self.folder(f"claude:{SID}")
        (f / "2026-11-09 1037 proj.md").write_text("a newer retro", encoding="utf-8")
        res = sv.adopt(flat)
        self.assertEqual(list(flat.iterdir()), [])
        self.assertEqual((f / "2026-11-09 1037 proj.md").read_text(encoding="utf-8"), "a newer retro")
        self.assertIn("# retro", (f / "2026-11-09 1037 proj (earlier).md").read_text(encoding="utf-8"))
        self.assertTrue((sv.day_dir("2026-11-09", self.over["root"]) / "2026-11-09 day proj.md").is_file())
        self.assertTrue((self.over["root"] / "weeks" / "2026-11-14 week.md").is_file())
        lone = self.folder("retro-only:2026-05-17 1009 oldsite")
        self.assertTrue((lone / "2026-05-17 1009 oldsite.md").is_file())
        self.assertIn("was not found", (lone / "conversation.md").read_text(encoding="utf-8"))
        self.assertEqual(res["counts"]["retros whose log was not found"], 1)


if __name__ == "__main__":
    unittest.main()

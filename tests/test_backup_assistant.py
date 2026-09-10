"""Durable assistant ledgers enter the local rotation as complete files."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import backup, calendarplan, commitments, contactintel, executive


class AssistantBackups(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.dest = self.root / "backups"
        for patcher in (mock.patch.object(backup, "DATA", self.data),
                        mock.patch.object(backup, "DEST", self.dest),
                        mock.patch.object(backup, "DIRS", ())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.names = [executive.STATE.name, commitments.STORE.name,
                      calendarplan.STORE.name, contactintel.STATE.name]

    def test_registry_covers_the_actual_assistant_store_paths(self):
        self.assertEqual(set(self.names), {"assistant-state.json", "assistant-commitments.json",
                                           "calendar-plans.json", "contact-intelligence.json"})
        for name in self.names:
            self.assertIn(name, backup.FILES)

    def test_snapshot_preserves_durable_claims_tasks_and_pending_sources(self):
        states = [
            {"reminders": {"r": {"delivery": "uncertain"}}, "calendar_planning_attempts": {"task": "2030-09-10"}},
            {"subjects": {"owner:self": {"open_loops": [{"what": "Prepare report", "status": "open"}]}}},
            {"drafts": {"cal_test": {"status": "creating", "id": "cal_test"}}},
            {"pending": {"owner:self": {"sources": [{"id": "message-test", "text": "Prepare report"}]}}},
        ]
        for name, state in zip(self.names, states):
            (self.data / name).write_text(json.dumps(state), encoding="utf-8")
        backup.snapshot()
        backup.snapshot()
        for name, state in zip(self.names, states):
            path = Path(name)
            snapshots = list(self.dest.glob(path.stem + "-*" + path.suffix))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(json.loads(snapshots[0].read_text(encoding="utf-8")), state)
        self.assertEqual(list(self.dest.glob("*.tmp")), [])

    def test_interrupted_file_copy_is_never_mistaken_for_complete_backup(self):
        name = "calendar-plans.json"
        (self.data / name).write_text('{"drafts":{"cal_test":{"status":"creating"}}}', encoding="utf-8")
        def interrupted(source, destination):
            Path(destination).write_text('{"drafts":', encoding="utf-8")
            raise OSError("copy interrupted")
        with mock.patch.object(backup, "FILES", (name,)), mock.patch.object(backup.shutil, "copy2", side_effect=interrupted):
            backup.snapshot()
        self.assertEqual(list(self.dest.glob("calendar-plans-*.json")), [])
        with mock.patch.object(backup, "FILES", (name,)):
            backup.snapshot()
        copies = list(self.dest.glob("calendar-plans-*.json"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), (self.data / name).read_text(encoding="utf-8"))
        self.assertEqual(list(self.dest.glob("*.tmp")), [])

    def test_assistant_files_use_the_existing_fourteen_snapshot_retention(self):
        name = "assistant-state.json"
        (self.data / name).write_text('{"reminders":{}}', encoding="utf-8")
        with mock.patch.object(backup, "FILES", (name,)):
            for day in range(1, 18):
                with mock.patch.object(backup, "date") as clock:
                    clock.today.return_value = date(2030, 9, day)
                    backup.snapshot()
        paths = sorted(self.dest.glob("assistant-state-*.json"))
        self.assertEqual(len(paths), backup.KEEP)
        self.assertEqual(paths[0].name, "assistant-state-2030-09-04.json")


if __name__ == "__main__":
    unittest.main()

"""A code card's code (server/cardcode.py): the file a Program card runs,
the plain description it opens with, and a typed change that backs the
file up, shows the diff once its session ends, and can be undone."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import cardcode

STEP = '''#!/usr/bin/env python3
"""Step 2: find the new uploads.

It reads the channel list and writes tonight's queue."""
print("done: 3 queued")
'''


class CardCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        self.step = self.root / "scripts" / "step.py"
        self.step.write_text(STEP, encoding="utf-8")
        for patch in (mock.patch.object(cardcode, "STORE", self.root / "changes.json"),
                      mock.patch.object(cardcode, "BACKUPS", self.root / "backups")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_file_a_command_runs_is_its_first_code_argument(self):
        found = cardcode.program_file("caffeinate -i python3 -u scripts/step.py --limit 30", str(self.root))
        self.assertEqual(found, self.step.resolve())
        self.assertIsNone(cardcode.program_file("python3 -u scripts/missing.py", str(self.root)))
        self.assertIsNone(cardcode.program_file("echo hi", str(self.root)))

    def test_the_description_is_the_docstring_or_the_opening_comments(self):
        self.assertEqual(cardcode.description(self.step, STEP),
                         "Step 2: find the new uploads.\n\nIt reads the channel list and writes tonight's queue.")
        shell = "#!/bin/bash\n# Nightly wrapper.\n# Keeps the Mac awake.\nset -u\necho hi\n"
        self.assertEqual(cardcode.description(Path("run.sh"), shell), "Nightly wrapper.\nKeeps the Mac awake.")
        self.assertEqual(cardcode.description(Path("bare.sh"), "echo hi\n"), "")

    def test_view_shows_the_description_and_the_code(self):
        shown = cardcode.view(command="python3 scripts/step.py", cwd=str(self.root))
        self.assertTrue(shown["exists"])
        self.assertEqual(shown["name"], "step.py")
        self.assertIn("find the new uploads", shown["description"])
        self.assertEqual(shown["text"], STEP)
        self.assertEqual(shown["lines"], 5)
        self.assertEqual(shown["changes"], [])

    def test_a_change_backs_up_shows_its_diff_and_undoes(self):
        with mock.patch.object(cardcode, "_launch", return_value="job1") as launch:
            rec = cardcode.change(str(self.step), "Queue at most 20.", card="List new uploads", flow="YouTube")
        self.assertEqual(rec["status"], "running")
        self.assertEqual(rec["job_id"], "job1")
        prompt = launch.call_args.args[0]
        self.assertIn(str(self.step), prompt)
        self.assertIn("Queue at most 20.", prompt)
        self.assertIn("Keep it true", prompt)
        # the session edits the file while it runs
        self.step.write_text(STEP.replace("3 queued", "20 queued"), encoding="utf-8")
        with mock.patch.object(cardcode, "_job", return_value={"status": "running"}):
            self.assertEqual(cardcode.change_status(rec["id"])["status"], "running")
        with mock.patch.object(cardcode, "_job", return_value={"status": "done", "result": "Capped the queue."}):
            done = cardcode.change_status(rec["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["summary"], "Capped the queue.")
        self.assertIn('-print("done: 3 queued")', done["diff"])
        self.assertIn('+print("done: 20 queued")', done["diff"])
        self.assertEqual(cardcode.view(path=str(self.step))["changes"][0]["status"], "done")
        cardcode.undo(rec["id"])
        self.assertEqual(self.step.read_text(encoding="utf-8"), STEP)
        kept = Path(rec["backup"]).with_name("step.py.before-undo")
        self.assertIn("20 queued", kept.read_text(encoding="utf-8"))
        self.assertEqual(cardcode.get(rec["id"])["status"], "undone")

    def test_a_session_that_changes_nothing_says_so(self):
        with mock.patch.object(cardcode, "_launch", return_value="job2"):
            rec = cardcode.change(str(self.step), "Explain the limits.")
        with mock.patch.object(cardcode, "_job", return_value={"status": "done", "result": "No change needed."}):
            out = cardcode.change_status(rec["id"])
        self.assertEqual(out["status"], "unchanged")
        self.assertEqual(out["diff"], "")

    def test_an_empty_instruction_or_a_missing_file_is_refused(self):
        with self.assertRaisesRegex(cardcode.CodeError, "type what to change"):
            cardcode.change(str(self.step), "   ")
        with self.assertRaisesRegex(cardcode.CodeError, "not a file"):
            cardcode.change(str(self.root / "nope.py"), "Do it.")
        with self.assertRaisesRegex(cardcode.CodeError, "not a file"):
            cardcode.change("scripts/step.py", "Do it.")

    def test_a_running_change_cannot_be_undone(self):
        with mock.patch.object(cardcode, "_launch", return_value="job3"):
            rec = cardcode.change(str(self.step), "Anything.")
        with self.assertRaisesRegex(cardcode.CodeError, "still changing"):
            cardcode.undo(rec["id"])


if __name__ == "__main__":
    unittest.main()

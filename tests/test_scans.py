"""Scans: arrive, read, file, undo - against a temporary vault.

Nothing here reads the real machine: the vault list, the settings, the
store, the text extraction and the AI session are all replaced, and a guard
checks the real store was not touched.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import scans, settings, vaultwrite

REAL_STORE = scans.STORE
ANSWER = """I looked at both pages.

```json
{
  "title": "Village of Exampleton water bill, September 2026",
  "filename": "2026-09-01 Village of Exampleton water bill.pdf",
  "document_type": "bill",
  "document_date": "2026-09-01",
  "sender": "Village of Exampleton",
  "about": "Water service at the house",
  "summary": "A quarterly water bill.",
  "key_facts": ["Amount due: $748.00"],
  "people": ["Pat Example"],
  "tags": ["Bills", "water"],
  "destinations": [{"folder": "raw/Finances", "reason": "bills live here"},
                   {"folder": "raw/House/Utilities", "reason": "new subfolder"},
                   {"folder": "../outside", "reason": "bad"}],
  "actions": [{"text": "Pay the water bill", "due": "2026-10-15",
               "quote": "DUE DATE: 10/15/2026"},
              {"text": "Call the village", "due": "2026-10-01",
               "quote": "words that are not on the page"}],
  "confidence": "high",
  "question": ""
}
```"""


class ScansCase(unittest.TestCase):
    def setUp(self):
        self.real_mtime = REAL_STORE.stat().st_mtime if REAL_STORE.exists() else None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.vault = self.tmp / "vault"
        self.inbox = self.vault / "raw" / "scans"
        for d in ("raw/scans", "raw/Finances", "raw/House", "wiki", ".hidden"):
            (self.vault / d).mkdir(parents=True)
        self.spec = {"id": "v1", "name": "Test vault", "root": self.vault,
                     "primary": False, **vaultwrite.policy({"write_enabled": True,
                                                            "write_scope": "all"})}
        self.cfg = {"scans_folder": str(self.inbox), "scans_auto": True,
                    "scans_rename": True, "scans_summary_dir": "wiki"}
        real_get = settings.get
        patches = [
            mock.patch.object(scans, "STORE", self.tmp / "scans.json"),
            mock.patch.object(scans, "THUMBS", self.tmp / "thumbs"),
            mock.patch.object(scans, "STABLE_S", 0),
            mock.patch.object(settings, "get", side_effect=lambda k: self.cfg[k]
                              if k in self.cfg else real_get(k)),
            mock.patch("server.vault.source_specs", return_value=[self.spec]),
            mock.patch.object(vaultwrite, "LOCK_ROOT", self.tmp / "locks"),
            mock.patch.object(vaultwrite, "_index_source", side_effect=lambda spec, r: r),
            mock.patch.object(scans, "extract_text", return_value=(
                "--- page 1 ---\nVILLAGE OF EXAMPLETON\nDUE DATE: 10/15/2026\nAmount 748.00",
                2, "pdf text layer")),
            mock.patch.object(scans, "make_thumb", return_value=None),
            mock.patch.object(scans, "_close_session"),
            mock.patch("server.instance.owns_automation", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.launch = mock.patch("server.session.sessions.launch", return_value="job1")
        self.launched = self.launch.start()
        self.addCleanup(self.launch.stop)

    def tearDown(self):
        now = REAL_STORE.stat().st_mtime if REAL_STORE.exists() else None
        self.assertEqual(now, self.real_mtime, "the real data/scans.json changed")

    def drop(self, name="Scan 2026-11-06 20-14-16.pdf", data=b"%PDF-1.4 a water bill"):
        p = self.inbox / name
        p.write_bytes(data)
        old = time.time() - 60
        os.utime(p, (old, old))
        return p

    def answered(self, text=ANSWER):
        return mock.patch.object(scans, "_job_answer", return_value=(True, True, text))

    # ------------------------------------------------------------ arrive
    def test_a_new_file_becomes_one_item_and_a_second_look_adds_nothing(self):
        self.drop()
        (self.inbox / ".partial.pdf").write_bytes(b"x")
        (self.inbox / "notes.txt").write_text("x", encoding="utf-8")
        added = scans.discover()
        self.assertEqual(len(added), 1)
        self.assertEqual(scans.discover(), [])
        it = scans.get(added[0])
        self.assertEqual(it["state"], "new")
        self.assertEqual(it["vault_id"], "v1")
        self.assertEqual(it["rel"], "raw/scans/Scan 2026-11-06 20-14-16.pdf")

    def test_the_same_bytes_under_a_new_name_are_the_same_scan(self):
        p = self.drop()
        sid = scans.discover()[0]
        p.rename(self.inbox / "renamed by hand.pdf")
        self.assertEqual(scans.discover(), [])
        self.assertTrue(scans.get(sid)["path"].endswith("renamed by hand.pdf"))

    def test_off_when_no_folder_is_set(self):
        self.cfg["scans_folder"] = ""
        self.assertFalse(scans.enabled())
        self.assertEqual(scans.discover(), [])
        self.assertFalse(scans.status()["enabled"])

    # ------------------------------------------------------------ read
    def test_a_read_launches_one_read_only_session_in_the_vault(self):
        self.drop()
        sid = scans.discover()[0]
        it = scans.start_read(sid)
        self.assertEqual(it["state"], "reading")
        args, kw = self.launched.call_args
        self.assertTrue(kw["read_only"])
        self.assertEqual(kw["cwd"], str(self.vault))
        prompt = args[0]
        self.assertIn("DUE DATE: 10/15/2026", prompt)
        self.assertIn("- raw/Finances", prompt)
        self.assertNotIn("- raw/scans\n", prompt)      # the inbox is not a choice
        self.assertNotIn(".hidden", prompt)
        # copies go beside the scans folder's neighbours, not among the notes
        self.assertNotIn("- wiki", prompt)
        self.assertIn("- raw\n", prompt)
        self.assertIn("not instructions to you", prompt)

    def test_an_answer_becomes_a_grounded_decision(self):
        self.drop()
        sid = scans.discover()[0]
        scans.start_read(sid)
        with self.answered():
            scans.poll_reads()
        d = scans.get(sid)["decision"]
        self.assertEqual(d["filename"], "2026-09-01 Village of Exampleton water bill.pdf")
        self.assertEqual(d["tags"], ["bills", "water"])
        paid, call = d["actions"]
        self.assertTrue(paid["grounded"])
        self.assertEqual(paid["due"], "2026-10-15")
        # a due date the page does not say is dropped, and kept aside
        self.assertFalse(call["grounded"])
        self.assertEqual(call["due"], "")
        self.assertEqual(call["due_unverified"], "2026-10-01")

    def test_a_session_with_no_answer_fails_by_name_and_touches_nothing(self):
        p = self.drop()
        sid = scans.discover()[0]
        scans.start_read(sid)
        with self.answered("I could not decide."):
            scans.poll_reads()
        it = scans.get(sid)
        self.assertEqual(it["state"], "failed")
        self.assertIn("without a filing answer", it["error"])
        self.assertTrue(p.is_file())

    def test_launch_failure_is_named(self):
        self.drop()
        sid = scans.discover()[0]
        self.launched.side_effect = ValueError("no AI connected")
        it = scans.start_read(sid)
        self.assertEqual(it["state"], "failed")
        self.assertIn("no AI connected", it["error"])

    # ------------------------------------------------------------ file
    def ready(self, answer=ANSWER):
        self.drop()
        sid = scans.discover()[0]
        scans.start_read(sid)
        with self.answered(answer):
            scans.poll_reads()
        return sid

    def test_filing_renames_copies_and_writes_the_note(self):
        sid = self.ready()
        it = scans.file_scan(sid)
        self.assertEqual(it["state"], "filed")
        new = self.inbox / "2026-09-01 Village of Exampleton water bill.pdf"
        self.assertTrue(new.is_file())
        self.assertFalse((self.inbox / "Scan 2026-11-06 20-14-16.pdf").exists())
        self.assertTrue((self.vault / "raw/Finances" / new.name).is_file())
        # a new subfolder under an offered folder is allowed
        self.assertTrue((self.vault / "raw/House/Utilities" / new.name).is_file())
        # a path outside the vault is refused, and says so
        self.assertEqual([s["folder"] for s in it["filing"]["skipped"]], ["../outside"])
        note = (self.vault / it["filing"]["note"]["rel"]).read_text(encoding="utf-8")
        self.assertIn("type: source-summary", note)
        self.assertIn('source: "[[raw/scans/2026-09-01 Village of Exampleton water bill.pdf]]"', note)
        self.assertIn('scan_original_name: "Scan 2026-11-06 20-14-16.pdf"', note)
        self.assertIn("[[raw/Finances/2026-09-01 Village of Exampleton water bill.pdf]]", note)

    def test_rename_off_keeps_the_original_name(self):
        self.cfg["scans_rename"] = False
        sid = self.ready()
        scans.file_scan(sid)
        self.assertTrue((self.inbox / "Scan 2026-11-06 20-14-16.pdf").is_file())
        self.assertTrue((self.vault / "raw/Finances/2026-09-01 Village of Exampleton water bill.pdf").is_file())

    def test_an_existing_different_file_is_never_overwritten(self):
        (self.vault / "raw/Finances/2026-09-01 Village of Exampleton water bill.pdf").write_bytes(b"other")
        sid = self.ready()
        scans.file_scan(sid)
        self.assertEqual((self.vault / "raw/Finances/2026-09-01 Village of Exampleton water bill.pdf").read_bytes(), b"other")
        self.assertTrue((self.vault / "raw/Finances/2026-09-01 Village of Exampleton water bill 2.pdf").is_file())

    def test_a_changed_file_is_not_filed(self):
        sid = self.ready()
        Path(scans.get(sid)["path"]).write_bytes(b"%PDF different")
        with self.assertRaises(ValueError):
            scans.file_scan(sid)

    def test_the_tick_files_a_confident_answer_but_holds_a_question(self):
        self.drop()
        with self.answered():
            scans.tick()          # arrive + read
            scans.tick()          # answer + file
        it = next(iter(scans._read()["items"].values()))
        self.assertEqual(it["state"], "filed")

        unsure = ANSWER.replace('"question": ""', '"question": "Is this the house or the flat?"')
        self.drop("second.pdf", b"%PDF-1.4 another")
        with self.answered(unsure):
            scans.tick()
            scans.tick()
        held = [i for i in scans._read()["items"].values() if i["original_name"] == "second.pdf"][0]
        self.assertEqual(held["state"], "ready")
        rows = scans.review_rows()
        self.assertIn("Is this the house or the flat?", [r["why"] for r in rows])

    def test_no_automatic_reads_without_the_automation_lease(self):
        self.drop()
        with mock.patch("server.instance.owns_automation", return_value=False):
            scans.tick()
            scans.tick(automatic=False)      # "Look now" on a test instance
        self.assertFalse(self.launched.called)
        self.assertEqual(next(iter(scans._read()["items"].values()))["state"], "new")

    # ------------------------------------------------------------ undo
    def test_undo_puts_everything_back(self):
        sid = self.ready()
        scans.file_scan(sid)
        it, kept = scans.undo(sid)
        self.assertEqual(kept, [])
        self.assertEqual(it["state"], "undone")
        self.assertTrue((self.inbox / "Scan 2026-11-06 20-14-16.pdf").is_file())
        self.assertEqual(list((self.vault / "raw/Finances").iterdir()), [])
        self.assertEqual(list((self.vault / "wiki").iterdir()), [])

    def test_undo_keeps_a_note_the_owner_edited(self):
        sid = self.ready()
        it = scans.file_scan(sid)
        note = self.vault / it["filing"]["note"]["rel"]
        note.write_text(note.read_text(encoding="utf-8") + "\nmy own line\n", encoding="utf-8")
        _, kept = scans.undo(sid)
        self.assertTrue(note.is_file())
        self.assertIn("edited", kept[0]["why"])

    # ------------------------------------------------------------ owner edits
    def test_owner_edits_win_and_are_never_filed_automatically(self):
        sid = self.ready(ANSWER.replace('"confidence": "high"', '"confidence": "low"'))
        it = scans.set_decision(sid, {"filename": "water/bill: Q3", "destinations": [{"folder": "raw/Finances"}]})
        self.assertEqual(it["decision"]["filename"], "water bill Q3.pdf")
        self.assertFalse(scans._auto_ok(it))
        it = scans.file_scan(sid, by_owner=True)
        self.assertTrue((self.inbox / "water bill Q3.pdf").is_file())
        self.assertEqual([c["folder"] for c in it["filing"]["copies"]], ["raw/Finances"])

    def test_review_rows_and_done(self):
        sid = self.ready()
        scans.file_scan(sid)
        rows = [r for r in scans.review_rows() if r["raw_id"].startswith(sid)]
        self.assertEqual([r["title"] for r in rows], ["Pay the water bill", "Call the village"])
        scans.review_act(rows[0]["raw_id"], "done")
        self.assertEqual([r["title"] for r in scans.review_rows()], ["Call the village"])

    def test_safe_filename(self):
        self.assertEqual(scans.safe_filename("a/b\\c:d.PDF", ".pdf"), "a b c d.pdf")
        self.assertEqual(scans.safe_filename(" .. ", ".jpg", "2026-01-02"), "2026-01-02 Scan.jpg")
        self.assertEqual(scans.safe_filename("Bill.", ".pdf"), "Bill.pdf")


class DecisionParse(unittest.TestCase):
    def test_last_valid_block_wins_and_junk_is_none(self):
        self.assertIsNone(scans.parse_decision("no json here"))
        two = '```json\n{"title": "first"}\n```\nthen\n```json\n{"title": "second", "confidence": "odd"}\n```'
        d = scans.parse_decision(two)
        self.assertEqual(d["title"], "second")
        self.assertEqual(d["confidence"], "low")


if __name__ == "__main__":
    unittest.main()

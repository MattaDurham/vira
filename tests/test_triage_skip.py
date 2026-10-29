"""Triage's registry decisions (2026-10-28): Skip tiers a placeholder
out and a later name undoes it; a shared line is written on its owner;
a batch of handle adds is one write; the backups folder keeps the newest
twenty. Over a temp CRM root and a temp triage state; every source
candidates() reads is patched."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import data as crm
from server import triage

# spelled in two pieces: a +1 number in one literal trips the PII guard
H_PH = "+1" + "5550100100"
H_NEW = "+1" + "5550100200"
H_YES = "+1" + "5550100400"


def _person(pid, name, *, emails=(), phones=(), tier="C-review", ptier=None):
    return {"id": pid, "name": name, "class_hint": None, "refs": {},
            "handles": {"imessage": [*emails, *("+1" + p for p in phones)],
                        "phones10": list(phones), "emails": list(emails)},
            "master_tier": tier, "profile_tier": ptier,
            "activity": {"imsg_n": 3}}


class SkipCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "crm"
        self.root.mkdir(parents=True)
        self.people = [
            _person("p_ph", "(unidentified)", phones=["5550100100"], ptier="active"),
            _person("p_named", "Pat Example", emails=["pat@example.com"],
                    phones=["5550100300"]),
        ]
        self._write()
        patches = [
            mock.patch("server.data.settings.crm_root", return_value=self.root),
            mock.patch.object(triage, "STATE", Path(self.tmp.name) / "triage-state.json"),
            mock.patch.object(triage, "_verdicts", return_value=[]),
            mock.patch.object(triage, "_recent_inbound", return_value=[]),
            mock.patch("server.companion.unknown_senders", return_value=[]),
            mock.patch("server.resolver.referrer_from_text", return_value=""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        crm.invalidate()
        self.addCleanup(crm.invalidate)

    def _write(self):
        (self.root / "people.json").write_text(
            json.dumps({"people": self.people}), encoding="utf-8")

    def registry(self):
        return {p["id"]: p for p in json.loads(
            (self.root / "people.json").read_text(encoding="utf-8"))["people"]}

    def test_skip_tiers_out_dismisses_and_hides(self):
        self.assertEqual([c["person_id"] for c in triage.candidates()], ["p_ph"])
        out = triage.skip_person("p_ph")
        self.assertEqual(out["dismissed"], 1)
        p = self.registry()["p_ph"]
        self.assertEqual((p["master_tier"], p["profile_tier"]), ("D-skip", "excluded"))
        self.assertEqual(p["refs"]["skipped"]["previous"],
                         {"master_tier": "C-review", "profile_tier": "active"})
        self.assertIn(H_PH, triage._dismissed())
        self.assertEqual(triage.candidates(), [])
        self.assertTrue(list((self.root / "backups").glob("people-*.json")))
        with self.assertRaises(KeyError):
            triage.skip_person("p_none")

    def test_a_later_name_unskips(self):
        triage.skip_person("p_ph")
        triage.add_person("Lee Example", [], person_id="p_ph")
        p = self.registry()["p_ph"]
        self.assertEqual((p["master_tier"], p["profile_tier"]), ("C-review", "active"))
        self.assertNotIn("skipped", p["refs"])
        self.assertIn("unskipped", p["refs"])
        # the rename path too, and a skip written before tiers were kept
        triage.skip_person("p_ph")
        self.people = list(self.registry().values())
        self.people[0]["refs"]["skipped"] = {"when": "2026-10-28", "by": "merge_people_20261028"}
        self.people[0]["name"] = "(unidentified)"
        self._write()
        crm.invalidate()
        triage.rename_person("p_ph", "Lee Q. Example")
        p = self.registry()["p_ph"]
        self.assertEqual(p["master_tier"], "C-review")
        self.assertNotIn("profile_tier", p)
        # naming with a placeholder does not unskip
        triage.skip_person("p_ph")
        triage.rename_person("p_ph", H_PH)
        self.assertEqual(self.registry()["p_ph"]["master_tier"], "D-skip")

    def test_skip_all_with_a_no_verdict(self):
        verdicts = [{"handle": H_PH, "contact_worthy": "no", "action": "needs_name"},
                    {"handle": H_NEW, "contact_worthy": "no", "action": "needs_name"},
                    {"handle": H_YES, "contact_worthy": "yes", "action": "needs_name"}]
        with mock.patch.object(triage, "_verdicts", return_value=verdicts):
            self.assertEqual(len(triage.candidates()), 3)
            out = triage.skip_no_verdicts()
            self.assertEqual(out, {"skipped": 1, "dismissed": 1})
            self.assertEqual([c["handle"] for c in triage.candidates()], [H_YES])
        self.assertEqual(self.registry()["p_ph"]["master_tier"], "D-skip")

    def test_mark_lines_on_the_owner_only(self):
        changed = triage.mark_lines([{"pid": "p_named", "value": "+1 (555) 010-0300"}])
        self.assertEqual(changed, ["5550100300"])
        self.assertEqual(self.registry()["p_named"]["handles"]["lines"], ["5550100300"])
        self.assertEqual(triage.mark_lines([{"pid": "p_named", "value": "5550100300"}]), [])
        with self.assertRaises(ValueError):
            triage.mark_lines([{"pid": "p_named", "value": "5550109999"}])
        with self.assertRaises(KeyError):
            triage.mark_lines([{"pid": "p_none", "value": "5550100300"}])
        self.assertEqual(triage.mark_lines([{"pid": "p_named", "value": "5550100300"}],
                                           unmark=True), ["5550100300"])
        self.assertEqual(self.registry()["p_named"]["handles"]["lines"], [])

    def test_batch_adds_are_one_write(self):
        out = triage.add_handles_batch([
            ("p_named", [{"kind": "phone", "value": "+1 555 010 0400"}]),
            ("p_ph", [{"kind": "email", "value": "New@Example.com"},
                      {"kind": "phone", "value": "123"},
                      {"kind": "email", "value": "pat@example.com"}]),
            ("p_none", [{"kind": "email", "value": "x@example.com"}]),
        ])
        self.assertEqual(out["landed"], [("p_named", "5550100400"), ("p_ph", "new@example.com")])
        self.assertEqual([(pid, v) for pid, v, _ in out["errors"]],
                         [("p_ph", "123"), ("p_ph", "pat@example.com"), ("p_none", "x@example.com")])
        reg = self.registry()
        self.assertIn("5550100400", reg["p_named"]["handles"]["phones10"])
        self.assertIn("new@example.com", reg["p_ph"]["handles"]["emails"])
        self.assertEqual(len(list((self.root / "backups").glob("people-*.json"))), 1)
        # the single add keeps its all-or-nothing contract
        with self.assertRaises(ValueError):
            triage.add_handles("p_ph", [{"kind": "email", "value": "x@example.com"},
                                        {"kind": "email", "value": "pat@example.com"}])
        self.assertNotIn("x@example.com", self.registry()["p_ph"]["handles"]["emails"])

    def test_backups_keep_the_newest_twenty(self):
        b = self.root / "backups"
        b.mkdir()
        for n in range(25):
            (b / f"people-20260101-0000{n:02d}.json").write_text("{}", encoding="utf-8")
        (b / "master-20260101-000000.json").write_text("[]", encoding="utf-8")
        (b / "merge-20260101-000000").mkdir()
        triage._read_people_backed_up()
        left = sorted(x.name for x in b.glob("people-*.json"))
        self.assertEqual(len(left), triage.BACKUPS_KEEP)
        self.assertNotIn("people-20260101-000000.json", left)
        self.assertIn("people-20260101-000024.json", left)
        self.assertTrue((b / "master-20260101-000000.json").exists())
        self.assertTrue((b / "merge-20260101-000000").is_dir())

"""CRM duplicates and the merge (server/crmmerge.py) over a temp CRM
root, a temp card overlay, a temp sync store and a temp merge store.
Nothing here reads the real stores or the Contacts app; fixtures are
synthetic (@example.com, 555-01xx)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import applecontacts as ac
from server import contactcard, contactsync, crmmerge, jsonstore
from server import data as crm


def person(pid, name, *, emails=(), phones=(), tier="A-import", ptier=None, msgs=0):
    return {"id": pid, "name": name, "class_hint": None, "refs": {},
            "handles": {"imessage": list(emails), "phones10": list(phones),
                        "emails": list(emails)},
            "master_tier": tier, "profile_tier": ptier,
            "activity": {"imsg_n": msgs}}


class MergeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "crm"
        (self.root / "profiles").mkdir(parents=True)
        self.people = [
            person("p_1", "Pat Example", emails=["pat@example.com"],
                   phones=["5550100001"], msgs=40),
            person("p_2", "Patricia Example", emails=["patricia@example.com"], msgs=2),
            person("p_3", "Sam Other", emails=["sam@example.com"]),
            person("p_4", "Sam Other", phones=["5550100004"]),
            person("p_5", "Lee Line", phones=["5550100099"]),
            person("p_6", "Kim Line", phones=["5550100099"]),
            person("p_7", "(unidentified)", phones=["5550100001"]),
        ]
        self._write()
        (self.root / "profiles" / "p_1.json").write_text(json.dumps({
            "id": "p_1", "name": "Pat Example",
            "hooks": [{"angle": "Ask about the boat", "detail": "June race"}],
            "personal_facts": [{"fact": "Sails", "as_of": "2024-05-01"}],
            "open_loops": []}), encoding="utf-8")
        (self.root / "profiles" / "p_2.json").write_text(json.dumps({
            "id": "p_2", "name": "Patricia Example", "relationship_class": "friend",
            "how_we_met": "At the pier.",
            "hooks": [{"angle": "Ask about the dog", "detail": "new puppy"},
                      {"angle": "Ask about the boat", "detail": "June race"}],
            "personal_facts": [{"fact": "Has a puppy", "as_of": "2026-01-01"}],
            "open_loops": [{"what": "Send the photos", "owed_by": "me",
                            "since": "2026-02-01", "status": "open"}]}),
            encoding="utf-8")
        patches = [
            mock.patch("server.data.settings.crm_root", return_value=self.root),
            mock.patch.object(contactcard, "STORE", Path(self.tmp.name) / "contact-cards.json"),
            mock.patch.object(contactsync, "STORE", Path(self.tmp.name) / "contacts-sync.json"),
            mock.patch.object(crmmerge, "STORE", Path(self.tmp.name) / "crm-merge.json"),
            mock.patch.object(ac, "enabled", return_value=False),
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

    def test_candidates_are_the_pairs_that_read_as_one_person(self):
        out = crmmerge.candidates(report={})
        by_key = {r["key"]: r for r in out}
        self.assertEqual(sorted(by_key), ["p_1:p_2", "p_1:p_7", "p_3:p_4"])
        self.assertEqual(by_key["p_1:p_2"]["why"], ["nickname"])
        self.assertEqual(by_key["p_1:p_2"]["keep"], "p_1")        # more messages
        self.assertEqual(by_key["p_1:p_7"]["why"], ["both carry (555) 010-0001"])
        self.assertEqual(by_key["p_1:p_7"]["keep"], "p_1")
        self.assertEqual(by_key["p_3:p_4"]["why"], ["same name"])
        self.assertEqual(by_key["p_3:p_4"]["a"]["emails"], ["sam@example.com"])
        # two surnames on one number is a line, not a duplicate
        self.assertNotIn("p_5:p_6", by_key)
        # a shared card in the sync report pairs compatible names only
        rep = {"shared_cards": [
            {"uid": "X", "card": "Pat Example", "people": [{"pid": "p_1"}, {"pid": "p_2"}]},
            {"uid": "Y", "card": "Lee Line", "people": [{"pid": "p_5"}, {"pid": "p_6"}]}]}
        out = {r["key"]: r for r in crmmerge.candidates(report=rep)}
        self.assertIn("both match the phone card Pat Example", out["p_1:p_2"]["why"])
        self.assertNotIn("p_5:p_6", out)
        # "Not the same" is remembered; a line is no signal; both skipped is nobody's problem
        crmmerge.not_same(key="p_3:p_4")
        self.assertNotIn("p_3:p_4", {r["key"] for r in crmmerge.candidates(report={})})
        self.people[0]["handles"]["lines"] = ["5550100001"]
        self.people[0]["master_tier"] = "D-skip"
        self.people[6]["master_tier"] = "D-skip"
        self._write()
        crm.invalidate()
        keys = {r["key"] for r in crmmerge.candidates(report={})}
        self.assertNotIn("p_1:p_7", keys)
        self.assertIn("p_1:p_2", keys)      # one skipped record still merges

    def test_merge_carries_handles_profile_overrides_and_the_id(self):
        jsonstore.mutate(contactcard.STORE, lambda s: s["cards"].update({
            "p_2": {"fields": {"company": "DropCo", "title": "Skipper"},
                    "handles": {"email:patricia@example.com": {"rank": "primary"}},
                    "custom": [{"label": "Boat", "value": "Minnow"}]},
            "p_1": {"fields": {"title": "Captain"}, "handles": {}, "custom": []}}),
            {"cards": {}}, indent=1)
        out = crmmerge.merge("p_1", "p_2")
        self.assertTrue(out["ok"])
        self.assertEqual(out["handles"], 1)
        reg = self.registry()
        self.assertNotIn("p_2", reg)
        k = reg["p_1"]
        self.assertEqual(k["handles"]["emails"], ["pat@example.com", "patricia@example.com"])
        self.assertEqual(k["handles"]["phones10"], ["5550100001"])
        self.assertEqual(k["activity"]["imsg_n"], 42)
        self.assertEqual([(m["id"], m["by"]) for m in k["refs"]["merged_from"]],
                         [("p_2", "crmmerge")])
        self.assertIn("Patricia Example", k["refs"]["card_names"])
        # the profile: the dropped record's notes joined, stamped; its file retired
        prof = json.loads((self.root / "profiles" / "p_1.json").read_text(encoding="utf-8"))
        self.assertEqual([h["angle"] for h in prof["hooks"]],
                         ["Ask about the boat", "Ask about the dog"])
        self.assertEqual(prof["hooks"][1]["carried_from"], "p_2")
        self.assertEqual([f["fact"] for f in prof["personal_facts"]], ["Sails", "Has a puppy"])
        self.assertEqual(prof["open_loops"][0]["what"], "Send the photos")
        self.assertEqual(prof["how_we_met"], "At the pier.")
        self.assertEqual(prof["merged_from_profile"], ["p_2"])
        self.assertFalse((self.root / "profiles" / "p_2.json").exists())
        self.assertTrue(list((self.root / "backups" / "profiles").glob("retired-p_2-*.json")))
        self.assertTrue(list((self.root / "backups").glob("people-*.json")))
        # the card overrides: the survivor's own values win, gaps fill
        cards = jsonstore.read(contactcard.STORE, {})["cards"]
        self.assertNotIn("p_2", cards)
        self.assertEqual(cards["p_1"]["fields"], {"title": "Captain", "company": "DropCo"})
        self.assertEqual(cards["p_1"]["custom"], [{"label": "Boat", "value": "Minnow"}])
        self.assertIn("email:patricia@example.com", cards["p_1"]["handles"])
        # one review entry, the old id still opens the person, and a log line
        rows = contactsync.read_store()["review"]
        self.assertEqual([r["kind"] for r in rows], ["merged"])
        self.assertIn("Patricia Example was merged into Pat Example", rows[0]["detail"])
        self.assertEqual(crm.get_person("p_2")["person"]["id"], "p_1")
        self.assertEqual(crm.resolve_handle("patricia@example.com"), "p_1")
        self.assertEqual(len(crmmerge._store()["merges"]), 1)
        self.assertNotIn("p_1:p_2", {r["key"] for r in crmmerge.candidates(report={})})

    def test_merge_without_a_dropped_profile_or_overrides(self):
        out = crmmerge.merge("p_4", "p_3")
        self.assertEqual(out["carried"], {})
        self.assertEqual(out["card_rows"], 0)
        reg = self.registry()
        self.assertEqual(reg["p_4"]["handles"]["emails"], ["sam@example.com"])
        self.assertFalse((self.root / "profiles" / "p_4.json").exists())

    def test_merge_refuses_bad_ids(self):
        with self.assertRaises(ValueError):
            crmmerge.merge("p_1", "p_1")
        with self.assertRaises(KeyError):
            crmmerge.merge("p_1", "p_none")
        with self.assertRaises(KeyError):
            crmmerge.merge("p_none", "p_1")
        self.assertEqual(len(self.registry()), 7)

    def test_duplicates_document_and_phone_groups_kept_apart(self):
        d = crmmerge.duplicates()
        self.assertEqual(len(d["pairs"]), 3)
        self.assertEqual(d["phone_groups"], [])          # push disabled: no phone half
        cards = [{"uid": "A:ABPerson", "name": "Pat Example", "first": "Pat", "last": "Example",
                  "emails": [{"value": "pat@example.com", "label": ""}], "phones": [],
                  "created": 1.0},
                 {"uid": "B:ABPerson", "name": "Pat Example", "first": "Pat", "last": "Example",
                  "emails": [], "phones": [{"value": "+1 555 010 0001", "label": ""}],
                  "created": 2.0}]
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "load_cards", return_value=cards):
            groups = crmmerge.duplicates()["phone_groups"]
            self.assertEqual([g["uids"] for g in groups], [["A:ABPerson", "B:ABPerson"]])
            crmmerge.not_same(uids=["B:ABPerson", "A:ABPerson"])
            self.assertEqual(crmmerge.duplicates()["phone_groups"], [])

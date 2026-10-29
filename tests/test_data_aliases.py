"""The CRM data layer's alias map: a person id retired by a merge
(refs.merged_from) still finds its survivor everywhere an id is looked
up, and a marked shared line resolves to its first owner. Over a temp
CRM root; nothing here reads the real stores."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import data as crm


def _person(pid, name, *, emails=(), phones=(), merged_from=None, lines=None):
    handles = {"imessage": list(emails), "phones10": list(phones), "emails": list(emails)}
    if lines:
        handles["lines"] = list(lines)
    refs = {"merged_from": merged_from} if merged_from else {}
    return {"id": pid, "name": name, "refs": refs, "handles": handles,
            "master_tier": "A-import", "activity": {}}


class AliasCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "crm"
        (self.root / "profiles").mkdir(parents=True)
        (self.root / "imessage-archive").mkdir()
        self.people = [
            _person("p_keep", "Pat Example", emails=["pat@example.com"],
                    merged_from=[
                        # the two shapes the one-off merge wrote
                        {"id": "p_old", "name": "Patricia Example",
                         "when": "20261028-191403", "by": "merge_people_20261028"},
                        {"id": "p_older", "name": "P. Example",
                         "date": "2026-10-28", "why": "owner confirmed"}]),
            _person("p_other", "Sam Example", phones=["5550100002"]),
        ]
        (self.root / "people.json").write_text(
            json.dumps({"people": self.people}), encoding="utf-8")
        (self.root / "profiles" / "p_keep.json").write_text(json.dumps(
            {"id": "p_keep", "name": "Pat Example", "personal_facts": []}),
            encoding="utf-8")
        (self.root / "imessage-archive" / "index.json").write_text(json.dumps(
            {"files": [{"file": "old.json", "chat_id": "c1", "type": "dm",
                        "participants": [{"person_id": "p_old"}]}]}),
            encoding="utf-8")
        p = mock.patch("server.data.settings.crm_root", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        crm.invalidate()
        self.addCleanup(crm.invalidate)

    def test_a_retired_id_finds_the_survivor(self):
        self.assertEqual(crm.get_person("p_old")["person"]["id"], "p_keep")
        self.assertEqual(crm.get_person("p_older")["person"]["id"], "p_keep")
        self.assertEqual(crm.canonical_id("p_old"), "p_keep")
        self.assertEqual(crm.canonical_id("p_keep"), "p_keep")
        self.assertEqual(crm.canonical_id("p_nobody"), "p_nobody")
        self.assertIsNone(crm.get_person("p_nobody"))

    def test_lookups_alias_but_iteration_does_not(self):
        c = crm._load()
        self.assertIs(c["by_id"].get("p_old"), c["by_id"]["p_keep"])
        self.assertIs(c["by_id"]["p_older"], c["by_id"]["p_keep"])
        self.assertIn("p_old", c["by_id"])
        self.assertNotIn("p_nobody", c["by_id"])
        self.assertIsNone(c["by_id"].get("p_nobody"))
        self.assertEqual(sorted(c["by_id"]), ["p_keep", "p_other"])
        self.assertEqual(len(list(c["by_id"].values())), 2)
        self.assertEqual(c["aliases"], {"p_old": "p_keep", "p_older": "p_keep"})

    def test_profiles_and_chats_follow_the_alias(self):
        c = crm._load()
        self.assertEqual(c["profiles"].get("p_old")["id"], "p_keep")
        chats = crm.get_person("p_keep")["chats"]
        self.assertEqual([x["chat_id"] for x in chats], ["c1"])
        self.assertEqual([x["chat_id"] for x in crm.get_person("p_old")["chats"]], ["c1"])

    def test_write_paths_land_on_the_survivor(self):
        crm.add_fact("p_old", "Keeps bees")
        prof = json.loads((self.root / "profiles" / "p_keep.json").read_text(encoding="utf-8"))
        self.assertEqual([f["fact"] for f in prof["personal_facts"]], ["Keeps bees"])
        self.assertFalse((self.root / "profiles" / "p_old.json").exists())

    def test_a_chain_resolves_to_the_live_record(self):
        people = [{"id": "p_live", "refs": {"merged_from": [{"id": "p_mid"}]}},
                  {"id": "p_x", "refs": {"merged_from": [{"id": "p_old"}]}}]
        # p_x listed p_old, then p_x itself was retired into p_live, which
        # only knows p_mid: a record that is gone but still named resolves on
        people[0]["refs"]["merged_from"].append({"id": "p_x"})
        people.pop(1)
        people.append({"id": "p_ghost", "refs": {"merged_from": [{"id": "p_old"}]}})
        aliases = crm._aliases(people)
        self.assertEqual(aliases["p_mid"], "p_live")
        self.assertEqual(aliases["p_x"], "p_live")
        self.assertEqual(aliases["p_old"], "p_ghost")
        # an alias naming a LIVE record is ignored: the record wins
        self.assertNotIn("p_live", crm._aliases(
            [{"id": "p_live"}, {"id": "p_b", "refs": {"merged_from": [{"id": "p_live"}]}}]))

    def test_a_line_resolves_to_its_first_owner(self):
        self.people.append(_person("p_a", "A Office", phones=["5550100009"]))
        self.people.append(_person("p_b", "B Office", phones=["5550100009"]))
        (self.root / "people.json").write_text(
            json.dumps({"people": self.people}), encoding="utf-8")
        crm.invalidate()
        self.assertEqual(crm.resolve_handle("5550100009"), "p_b")
        self.assertEqual(crm.lines(), set())
        self.people[-1]["handles"]["lines"] = ["+1 (555) 010-0009"]
        (self.root / "people.json").write_text(
            json.dumps({"people": self.people}), encoding="utf-8")
        crm.invalidate()
        self.assertEqual(crm.lines(), {"5550100009"})
        self.assertEqual(crm.resolve_handle("+1 (555) 010-0009"), "p_a")
        self.assertEqual(crm.handle_key("Pat@Example.com"), "pat@example.com")
        self.assertEqual(crm.handle_key("+1 (555) 010-0009"), "5550100009")

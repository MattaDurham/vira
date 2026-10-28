"""Contacts sync: the survey, the watermark, the inbound writes, the push,
card creation and the review list, over a temp CRM and synthetic Apple
cards (load_cards and osascript patched). Nothing here touches the real
Contacts stores, the real CRM, or the real data/ stores: every path a
module reads is pinned to the temp directory. Fixtures are synthetic
(@example.com, 555-01xx) per the PII guard."""

import json
import tempfile
import re
import unittest
from pathlib import Path
from unittest import mock

from server import applecontacts as ac
from server import contactcard, contactsync as cs, data as crm, runtimework


def card(uid, name, *, org="", title="", emails=(), phones=(), note="",
         created=None, birthday=None, addresses=()):
    first, last = ac.split_name(name)
    return {"uid": uid, "store": "s", "pk": hash(uid) & 0xffff, "first": first,
            "last": last, "name": name, "org": org, "title": title,
            "nickname": "", "note": note, "birthday": birthday,
            "created": created, "modified": created,
            "emails": [{"value": e, "label": "home"} for e in emails],
            "phones": [{"value": p, "label": "mobile"} for p in phones],
            "urls": [], "addresses": list(addresses)}


def person(pid, name, *, emails=(), phones=(), tier="A-import"):
    return {"id": pid, "name": name, "class_hint": None, "refs": {},
            "handles": {"imessage": list(emails), "phones10": list(phones),
                        "emails": list(emails)},
            "master_tier": tier, "activity": {}}


class SyncCase(unittest.TestCase):
    """A temp CRM root, a temp sync store, a temp card overlay, and a fixed
    set of Apple cards. Every seam the pass writes through is pinned."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "crm"
        (self.root / "profiles").mkdir(parents=True)
        self.people = [
            person("p_a", "Alex Example", emails=["alex@example.com"]),
            person("p_d", "Dana Example", emails=["dana@example.com"]),
            person("p_e", "Skip Example", emails=["skip@example.com"], tier="D-skip"),
            person("p_f", "Finn Example", phones=["5550105555"]),
        ]
        self.master = [{"id": "p_d", "company": "CrmCo", "title": "Partner",
                        "relationship": "college friend"}]
        (self.root / "profiles" / "p_d.json").write_text(json.dumps({
            "id": "p_d", "name": "Dana Example", "relationship_class": "friend",
            "relationship_summary": "Old sailing friend.",
            "personal_facts": [{"fact": "Owns a catboat", "as_of": "2024-05-01"}],
            "hooks": [{"angle": "Ask about the regatta", "detail": "June race"}],
            "open_loops": [{"what": "Dinner never scheduled", "owed_by": "me",
                            "since": "2024-03-01"}],
        }), encoding="utf-8")
        self._write_registry()
        self.cards = [
            card("A:ABPerson", "Alex Example", org="AppleCo", title="Maker",
                 emails=["alex@example.com"], phones=["+1 (555) 010-1234"],
                 note="met at the pier", created=1000.0,
                 addresses=[{"label": "home", "street": "", "city": "Boston",
                             "state": "MA", "zip": "", "country": ""}]),
            card("D:ABPerson", "Dana Example", org="OtherCo",
                 emails=["dana@example.com"], note="phone only text",
                 created=900.0),
            card("B:ABPerson", "Backlog Example", emails=["backlog@example.com"],
                 created=800.0),
            card("C:ABPerson", "Casey New", org="NewCo", emails=["casey@example.com"],
                 created=2000.0),
            card("N:ABPerson", "Nameless", emails=["nobody@example.com"],
                 created=2100.0),
        ]
        self.cards[-1]["name"] = ""      # no name: cannot become a person
        self.scripts = []
        patches = [
            mock.patch("server.data.settings.crm_root", return_value=self.root),
            mock.patch.object(ac, "load_cards", side_effect=lambda stores=None: list(self.cards)),
            mock.patch.object(ac, "enabled", return_value=True),
            mock.patch.object(ac, "backup_vcard", return_value=None),
            mock.patch.object(ac, "_osascript", side_effect=self._osa),
            mock.patch.object(cs, "available", return_value=(True, "")),
            mock.patch.object(cs, "STORE", Path(self.tmp.name) / "contacts-sync.json"),
            mock.patch.object(contactcard, "STORE", Path(self.tmp.name) / "contact-cards.json"),
            mock.patch("server.onboard.crm_target", return_value=self.root),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        crm.invalidate()
        self.addCleanup(crm.invalidate)

    def _write_registry(self):
        (self.root / "people.json").write_text(
            json.dumps({"people": self.people}), encoding="utf-8")
        (self.root / "master.json").write_text(
            json.dumps(self.master), encoding="utf-8")

    def _osa(self, script):
        self.scripts.append(script)
        if "make new person" in script:
            return "NEW:ABPerson"
        return "ok"

    def registry(self):
        return json.loads((self.root / "people.json").read_text(encoding="utf-8"))["people"]

    def by_name(self, name):
        return next((p for p in self.registry() if p["name"] == name), None)


class SurveyTest(SyncCase):
    def test_watermark_is_the_newest_known_card(self):
        s = cs.survey()
        # Alex's card (1000) is the newest card the CRM already knows, so
        # Casey (2000) and the nameless card (2100) are new; Backlog (800) is
        # the backlog even though nobody in the CRM matches it.
        self.assertEqual(s["since"], 1000.0)
        self.assertEqual([c["name"] for c in s["new_cards"]], ["Casey New", ""])
        self.assertEqual(s["backlog"], 1)
        self.assertEqual(s["matched"], 2)

    def test_inbound_picture(self):
        s = cs.survey()
        adds = {(h["pid"], h["kind"], h["value"]) for h in s["handle_adds"]}
        self.assertIn(("p_a", "phone", "5550101234"), adds)
        fills = {f["pid"]: f for f in s["fills"]}
        # Alex has no CRM company/title/location: filled from the phone.
        self.assertEqual(fills["p_a"]["fields"],
                         {"company": "AppleCo", "title": "Maker", "location": "Boston, MA"})
        self.assertEqual(fills["p_a"]["phone_note"], "met at the pier")
        # Dana's CRM company stands; only the phone note comes in.
        self.assertEqual(fills["p_d"]["fields"], {})
        self.assertEqual(fills["p_d"]["phone_note"], "phone only text")

    def test_outbound_picture_and_missing(self):
        s = cs.survey()
        dana = next(c for c in s["changes"] if c["pid"] == "p_d")
        self.assertIn("company", dana["changed"])
        self.assertIn("title", dana["changed"])
        self.assertIn("note", dana["changed"])
        self.assertEqual(dana["conflicts"][0],
                         {"field": "company", "crm": "CrmCo", "phone": "OtherCo"})
        self.assertTrue(dana["foreign_note"])
        # Finn has no card and is card-worthy; Skip is a skip tier.
        self.assertEqual([m["pid"] for m in s["missing"]], ["p_f"])
        self.assertEqual(s["unmatched_skipped"], 1)

    def test_public_form_caps_lists_and_drops_objects(self):
        pub = cs._public(cs.survey())
        self.assertNotIn("_plans", pub)
        self.assertEqual(pub["new_cards_total"], 2)
        self.assertEqual(pub["fills"][0]["fields"], ["company", "location", "title"])


class InboundPassTest(SyncCase):
    def test_new_card_becomes_a_person_and_backlog_does_not(self):
        out = cs.apply(inbound=True, push=False, create=False)
        self.assertTrue(out["ok"], out)
        self.assertIsNotNone(self.by_name("Casey New"))
        self.assertIsNone(self.by_name("Backlog Example"))     # before the watermark
        self.assertIsNone(self.by_name(""))                    # no name, no person
        # the watermark moved past everything the pass saw
        self.assertEqual(cs.read_store()["since"], 2100.0)
        kinds = [r["kind"] for r in cs.read_store()["review"]]
        self.assertIn("new-person", kinds)
        # nothing was written to Apple on an inbound-only pass
        self.assertEqual(self.scripts, [])

    def test_second_pass_imports_nothing_twice(self):
        cs.apply(inbound=True)
        n = len(self.registry())
        out = cs.apply(inbound=True)
        self.assertEqual(out["done"]["imported"], [])
        self.assertEqual(len(self.registry()), n)

    def test_handles_and_fills_land_without_overwriting(self):
        cs.apply(inbound=True)
        alex = self.by_name("Alex Example")
        self.assertIn("5550101234", alex["handles"]["phones10"])
        c = contactcard.compose("p_a")
        fields = {f["key"]: f for f in c["fields"]}
        self.assertEqual(fields["company"]["value"], "AppleCo")
        self.assertEqual(fields["location"]["value"], "Boston, MA")
        self.assertEqual([x["value"] for x in c["custom"] if x["label"] == cs.PHONE_NOTE_LABEL],
                         ["met at the pier"])
        d = contactcard.compose("p_d")
        self.assertEqual({f["key"]: f["value"] for f in d["fields"]}["company"], "CrmCo")
        kinds = [r["kind"] for r in cs.read_store()["review"]]
        self.assertIn("phone-note", kinds)

    def test_shared_card_is_listed_once_and_never_retried(self):
        self.people.append(person("p_z", "Zed Example", phones=["5550101234"]))
        self._write_registry()
        crm.invalidate()
        s = cs.survey()
        # Zed shares Alex's card: the card is listed once with both names,
        # each side's missing handle is a conflict, not pending work.
        self.assertEqual([(sc["uid"], [q["pid"] for q in sc["people"]]) for sc in s["shared_cards"]],
                         [("A:ABPerson", ["p_a", "p_z"])])
        self.assertEqual([(h["pid"], h["other_pid"]) for h in s["handle_conflicts"]],
                         [("p_a", "p_z"), ("p_z", "p_a")])
        self.assertFalse([h for h in s["handle_adds"] if h["pid"] in ("p_a", "p_z")])
        # and nothing would be pushed to the shared card, from either side
        self.assertNotIn("A:ABPerson", [c["uid"] for c in s["changes"]])
        cs.apply(inbound=True)
        self.assertNotIn("5550101234", self.by_name("Alex Example")["handles"]["phones10"])
        self.assertNotIn("alex@example.com", self.by_name("Zed Example")["handles"]["emails"])
        shared = [r for r in cs.read_store()["review"] if r["kind"] == "shared-card"]
        self.assertEqual(len(shared), 1)
        self.assertIn("Alex Example and Zed Example", shared[0]["detail"])
        self.assertFalse([r for r in cs.read_store()["review"] if r["kind"] == "handle-conflict"])
        # a second pass files nothing new for the same shared card
        cs.apply(inbound=True)
        self.assertEqual(len([r for r in cs.read_store()["review"] if r["kind"] == "shared-card"]), 1)
        self.assertEqual(cs.read_store()["report"]["shared_cards_total"], 1)

    def test_shared_card_wording_counts_the_people_and_stays_current(self):
        self.people.append(person("p_y", "Yara Example", phones=["5550101234"]))
        self.people.append(person("p_z", "Zed Example", phones=["5550101234"]))
        self._write_registry()
        crm.invalidate()
        cs.apply(inbound=True)
        shared = [r for r in cs.read_store()["review"] if r["kind"] == "shared-card"]
        self.assertEqual(len(shared), 1)
        m = re.match(r"Three CRM people match this phone card: (.*)\. Nothing",
                     shared[0]["detail"])
        self.assertTrue(m, shared[0]["detail"])
        head, last = m.group(1).rsplit(" and ", 1)
        self.assertEqual(sorted(head.split(", ") + [last]),
                         ["Alex Example", "Yara Example", "Zed Example"])
        # the owner reads it, then renames Zed in the CRM: the same entry
        # is kept, stays read, and tells the current names
        cs.mark_reviewed(everything=True)
        next(p for p in self.people if p["id"] == "p_z")["name"] = "Zed Q. Example"
        self._write_registry()
        crm.invalidate()
        cs.apply(inbound=True)
        shared2 = [r for r in cs.read_store()["review"] if r["kind"] == "shared-card"]
        self.assertEqual([r["id"] for r in shared2], [shared[0]["id"]])
        self.assertTrue(shared2[0]["reviewed"])
        self.assertIn("Zed Q. Example", shared2[0]["detail"])
        self.assertNotIn("Zed Example", shared2[0]["detail"])

    def test_push_skips_the_shared_card(self):
        self.people.append(person("p_z", "Zed Example", phones=["5550101234"]))
        self._write_registry()
        crm.invalidate()
        out = cs.apply(inbound=False, push=True)
        self.assertEqual({p["pid"] for p in out["done"]["pushed"]}, {"p_d"})
        self.assertFalse([x for x in self.scripts if 'person id "A:ABPerson"' in x])

    def test_legacy_conflict_rows_are_not_duplicated(self):
        self.people.append(person("p_z", "Zed Example", phones=["5550101234"]))
        self._write_registry()
        crm.invalidate()
        # an entry written before review keys existed, for the same case
        legacy = {"id": "rv_old", "kind": "handle-conflict", "pid": "p_a",
                  "name": "Alex Example", "uid": None, "when": "2026-10-28T12:32:44",
                  "detail": "The phone card carries phone 5550101234: handle "
                            "5550101234 already belongs to another CRM person",
                  "text": "", "reviewed": False}
        cs._mutate(lambda st: st["review"].append(legacy))
        cs.apply(inbound=True)
        mine = [r for r in cs.read_store()["review"]
                if r["kind"] == "handle-conflict" and r["pid"] == "p_a"]
        self.assertEqual([r["id"] for r in mine], ["rv_old"])


class PushPassTest(SyncCase):
    def test_push_writes_every_planned_card_and_files_review(self):
        out = cs.apply(inbound=False, push=True, create=False)
        self.assertTrue(out["ok"], out)
        pushed = {p["pid"] for p in out["done"]["pushed"]}
        self.assertEqual(pushed, {"p_a", "p_d"})
        dana = next(s for s in self.scripts if 'person id "D:ABPerson"' in s)
        self.assertIn('set organization of P to "CrmCo"', dana)
        self.assertIn('set job title of P to "Partner"', dana)
        self.assertIn('Previously on phone: company \\"OtherCo\\"', dana)
        self.assertIn("Phone note kept above.", dana)
        self.assertIn('"phone only text"', dana)
        self.assertIn("Owns a catboat (as of 2024-05-01)", dana)
        self.assertIn("Ask about the regatta: June race", dana)
        kinds = sorted(r["kind"] for r in cs.read_store()["review"])
        self.assertIn("field-conflict", kinds)
        self.assertIn("note-merged", kinds)
        self.assertEqual(out["done"]["created"], [])        # create was not asked

    def test_push_off_in_config_writes_nothing(self):
        with mock.patch.object(ac, "enabled", return_value=False):
            out = cs.apply(inbound=False, push=True, create=False)
        self.assertEqual(self.scripts, [])
        self.assertTrue(any("Apple push is off" in e for e in out["done"]["errors"]))

    def test_push_reports_progress_to_the_tab_and_to_fleet(self):
        seen = []

        def osa(script):
            self.scripts.append(script)
            seen.append((dict(cs._status["progress"] or {}),
                         [(w["title"], w["done"], w["total"], w["step"])
                          for w in runtimework.snapshot() if w["kind"] == "sync"]))
            return "ok"
        with mock.patch.object(ac, "_osascript", side_effect=osa):
            cs.apply(inbound=False, push=True, reason="sync tab")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0]["total"], 2)
        self.assertEqual([p["done"] for p, _ in seen], [1, 2])
        self.assertEqual({p["step"] for p, _ in seen}, {"Alex Example", "Dana Example"})
        # the same counter rides the in-process activity Fleet reads
        self.assertEqual(seen[1][1], [("Contacts sync (sync tab)", 2, 2, seen[1][0]["step"])])
        # and both are gone once the pass ends
        self.assertIsNone(cs._status["progress"])
        self.assertFalse([w for w in runtimework.snapshot() if w["kind"] == "sync"])

    def test_an_osascript_failure_is_logged_not_raised(self):
        with mock.patch.object(ac, "_osascript", side_effect=RuntimeError("-600")):
            out = cs.apply(inbound=False, push=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["done"]["pushed"], [])
        self.assertEqual(len(out["done"]["errors"]), 2)
        self.assertEqual(cs.read_store()["log"][-1]["pushed"], 0)


class CreateTest(SyncCase):
    def test_create_makes_cards_for_card_worthy_people_only(self):
        out = cs.apply(inbound=False, push=False, create=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual([c["pid"] for c in out["done"]["created"]], ["p_f"])
        made = [s for s in self.scripts if "make new person" in s]
        self.assertEqual(len(made), 1)
        self.assertIn('first name:"Finn"', made[0])
        self.assertIn('make new phone at end of phones of P', made[0])
        self.assertNotIn("delete every", made[0])
        ledger = cs.read_store()["created"]
        self.assertEqual(ledger[0]["uid"], "NEW:ABPerson")
        self.assertFalse(ledger[0]["undone"])

    def test_undo_deletes_only_what_vira_created(self):
        cs.apply(inbound=False, create=True)
        self.scripts.clear()
        out = cs.undo_created()
        self.assertEqual(out["deleted"], 1)
        self.assertIn('delete person id "NEW:ABPerson"', self.scripts[-1])
        self.assertTrue(cs.read_store()["created"][0]["undone"])
        self.assertEqual(cs.undo_created()["deleted"], 0)


class ReviewTest(SyncCase):
    def test_mark_reviewed_by_id_and_all(self):
        cs.apply(inbound=True)
        pending = [r for r in cs.read_store()["review"] if not r["reviewed"]]
        self.assertTrue(pending)
        self.assertEqual(cs.mark_reviewed([pending[0]["id"]])["marked"], 1)
        st = cs.status()
        self.assertEqual(st["review_total"], len(pending) - 1)
        cs.mark_reviewed(everything=True)
        self.assertEqual(cs.status()["review_total"], 0)

    def test_card_save_hook_files_conflicts(self):
        r = cs.push_person("p_d")
        self.assertTrue(r["ok"], r)
        kinds = [x["kind"] for x in cs.read_store()["review"]]
        self.assertIn("field-conflict", kinds)
        self.assertIn("note-merged", kinds)

    def test_busy_pass_is_reported_not_stacked(self):
        self.assertTrue(cs._run_lock.acquire(blocking=False))
        try:
            out = cs.apply(inbound=True)
        finally:
            cs._run_lock.release()
        self.assertTrue(out.get("busy"))


class GateTest(unittest.TestCase):
    def test_card_worthy_rules(self):
        self.assertTrue(cs.card_worthy(person("p1", "A", emails=["a@example.com"])))
        self.assertFalse(cs.card_worthy(person("p2", "B", emails=["b@example.com"], tier="D-skip")))
        self.assertFalse(cs.card_worthy(person("p3", "C")))          # no handle
        p = person("p4", "D", emails=["d@example.com"])
        p["profile_tier"] = "excluded"
        self.assertFalse(cs.card_worthy(p))

    def test_not_available_degrades_to_a_named_status(self):
        with mock.patch.object(cs, "available", return_value=(False, "nope")), \
             mock.patch.object(cs, "STORE", Path(tempfile.mkdtemp()) / "s.json"):
            out = cs.apply(inbound=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "nope")


if __name__ == "__main__":
    unittest.main()

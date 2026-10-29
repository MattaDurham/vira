"""Apple Contacts write-back: the plan (pure), the script, the gates,
the sqlite matcher, and the push orchestration with osascript mocked.
Fixtures are synthetic (@example.com, 555-01xx) per the PII guard."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import applecontacts as ac


def row(kind, value, rank="", use=""):
    return {"key": f"{kind}:{value}", "kind": kind, "value": value,
            "display": value, "rank": rank, "use": use, "added": False}


class PlanTest(unittest.TestCase):
    def test_rank_order_and_archived_label(self):
        rows = [row("email", "a@example.com", rank="primary", use="personal"),
                row("email", "b@example.com", rank="secondary"),
                row("email", "c@example.com"),
                row("email", "old@example.com", rank="archived", use="work")]
        plan = ac.plan_handles(rows, [], "email")
        self.assertEqual([v for v, _ in plan],
                         ["a@example.com", "b@example.com", "c@example.com",
                          "old@example.com"])
        self.assertEqual(plan[0][1], "home")       # personal -> home
        self.assertEqual(plan[-1][1], "old")       # archived -> "old"

    def test_use_maps_and_existing_label_preserved(self):
        rows = [row("email", "w@example.com", use="work"),
                row("email", "n@example.com")]
        existing = [{"value": "n@example.com", "label": "home"}]
        plan = dict(ac.plan_handles(rows, existing, "email"))
        self.assertEqual(plan["w@example.com"], "work")
        self.assertEqual(plan["n@example.com"], "home")   # kept, not invented

    def test_apple_only_address_survives(self):
        rows = [row("email", "a@example.com", rank="primary")]
        existing = [{"value": "a@example.com", "label": ""},
                    {"value": "keep@example.com", "label": "work"}]
        plan = ac.plan_handles(rows, existing, "email")
        self.assertEqual(plan[-1], ("keep@example.com", "work"))

    def test_apple_spelling_wins_for_phones(self):
        rows = [row("phone", "5550142222", rank="primary", use="personal")]
        existing = [{"value": "+1 (555) 014-2222", "label": "mobile"}]
        plan = ac.plan_handles(rows, existing, "phone")
        self.assertEqual(plan[0][0], "+1 (555) 014-2222")
        self.assertEqual(plan[0][1], "home")

    def test_same_ignores_order_and_an_empty_planned_label(self):
        existing = [{"value": "b@example.com", "label": "work"},
                    {"value": "a@example.com", "label": "home"}]
        self.assertTrue(ac._same_handles([("a@example.com", "home"), ("b@example.com", "work")],
                                         existing, "email"))
        self.assertTrue(ac._same_handles([("a@example.com", ""), ("b@example.com", "work")],
                                         existing, "email"), "no opinion on a label matches any")
        self.assertFalse(ac._same_handles([("a@example.com", "work"), ("b@example.com", "work")],
                                          existing, "email"), "a different label is a change")
        self.assertFalse(ac._same_handles([("a@example.com", "home")], existing, "email"),
                         "a missing value is a change")
        self.assertFalse(ac._same_handles([("a@example.com", "home"), ("b@example.com", "work"),
                                           ("c@example.com", "")], existing, "email"))

    def test_dedupe_and_same_check(self):
        rows = [row("email", "a@example.com", rank="primary"),
                row("email", "A@Example.com")]
        existing = [{"value": "a@example.com", "label": ""}]
        plan = ac.plan_handles(rows, existing, "email")
        self.assertEqual(len(plan), 1)
        self.assertTrue(ac._same_handles(plan, existing, "email"))
        # the planned label is empty (no use, no label at plan time), which
        # is no opinion: a card that meanwhile says "work" is not a change
        self.assertTrue(ac._same_handles(
            plan, [{"value": "a@example.com", "label": "work"}], "email"))
        self.assertFalse(ac._same_handles(
            [("a@example.com", "home")],
            [{"value": "a@example.com", "label": "work"}], "email"))


class ScriptTest(unittest.TestCase):
    def test_company_and_birthday(self):
        s = ac.build_script("UID:ABPerson", company='Ac "Me" Inc',
                            birthday=(1991, 8, 3))
        self.assertIn('set organization of P to "Ac \\"Me\\" Inc"', s)
        self.assertIn("set month of bd to August", s)
        self.assertIn("set day of bd to 3", s)
        self.assertNotIn("delete every email", s)

    def test_email_rewrite_lines(self):
        s = ac.build_script("UID:ABPerson",
                            emails=[("a@example.com", "home"),
                                    ("b@example.com", "")])
        self.assertIn("delete every email of P", s)
        self.assertIn('{label:"home", value:"a@example.com"}', s)
        self.assertIn('with properties {value:"b@example.com"}', s)
        self.assertLess(s.index("delete every email"),
                        s.index("a@example.com"))

    def test_birthday_parse(self):
        self.assertEqual(ac._birthday_parts("8/3/1991"), (1991, 8, 3))
        self.assertEqual(ac._birthday_parts("1991-08-03"), (1991, 8, 3))
        self.assertIsNone(ac._birthday_parts("next tuesday"))
        self.assertIsNone(ac._birthday_parts(""))

    def test_native_slot_lines(self):
        s = ac.build_script("UID:ABPerson",
                            related=[("Relationship", 'Old "sailing" friend')],
                            related_set=[("Relationship", "client")],
                            dates=[("When we met", (2025, 9, 25))],
                            social=[("LinkedIn", "pat-example",
                                     "https://www.linkedin.com/in/pat-example")])
        self.assertIn('make new related name at end of related names of P with '
                      'properties {label:"Relationship", value:"Old \\"sailing\\" friend"}', s)
        self.assertIn('if label of rn is "Relationship" then set value of rn to "client"', s)
        self.assertIn('make new custom date at end of custom dates of P with '
                      'properties {label:"When we met", value:cd0}', s)
        self.assertIn('make new social profile at end of social profiles of P with '
                      'properties {service name:"LinkedIn", user name:"pat-example", '
                      'url:"https://www.linkedin.com/in/pat-example"}', s)
        # a date is assembled from components, the day pinned to 1 before
        # the month moves, so the 31st cannot roll a short month over
        lines = s.splitlines()
        i = lines.index("set cd0 to current date")
        self.assertEqual(lines[i + 1:i + 5],
                         ["set day of cd0 to 1", "set year of cd0 to 2025",
                          "set month of cd0 to September", "set day of cd0 to 25"])
        self.assertNotIn("delete every", s)

    def test_social_from_url(self):
        f = ac.social_from_url
        self.assertEqual(f("https://www.linkedin.com/in/pat-example/"),
                         ("LinkedIn", "pat-example"))
        self.assertEqual(f("https://x.com/patexample"), ("Twitter", "patexample"))
        self.assertEqual(f("https://www.youtube.com/@patexample"),
                         ("YouTube", "patexample"))
        self.assertEqual(f("https://bsky.app/profile/pat.example.com"),
                         ("Bluesky", "pat.example.com"))
        self.assertIsNone(f("https://www.linkedin.com/company/example"))
        self.assertIsNone(f("https://x.com/intent/tweet?text=hi"))
        self.assertIsNone(f("https://example.com/pat"))
        self.assertIsNone(f("not a url"))


class GateTest(unittest.TestCase):
    def test_sandbox_refuse(self):
        with mock.patch.dict(os.environ, {"VIRA_SANDBOX": "1"}):
            self.assertFalse(ac.enabled())

    def test_off_mac_refuses(self):
        with mock.patch.object(ac.settings, "IS_MAC", False):
            self.assertFalse(ac.enabled())

    def test_config_off_refuses(self):
        with mock.patch.object(ac.settings, "get",
                               side_effect=lambda k: False):
            with mock.patch.object(ac.settings, "IS_MAC", True):
                with mock.patch.dict(os.environ, {}, clear=False):

                    os.environ.pop("VIRA_SANDBOX", None)
                    self.assertFalse(ac.enabled())


def _fixture_store(dirpath, slots=False):
    db = Path(dirpath) / "AddressBook-v22.abcddb"
    c = sqlite3.connect(db)
    if slots:
        c.execute("CREATE TABLE ZABCDRELATEDNAME (Z_PK INTEGER PRIMARY KEY, "
                  "ZOWNER INTEGER, ZLABEL TEXT, ZNAME TEXT)")
        c.execute("CREATE TABLE ZABCDCONTACTDATE (Z_PK INTEGER PRIMARY KEY, "
                  "ZOWNER INTEGER, ZLABEL TEXT, ZDATE REAL)")
        c.execute("CREATE TABLE ZABCDSOCIALPROFILE (Z_PK INTEGER PRIMARY KEY, "
                  "ZOWNER INTEGER, ZSERVICENAME TEXT, ZUSERNAME TEXT, ZURLSTRING TEXT)")
        c.execute("INSERT INTO ZABCDRELATEDNAME VALUES (1, 1, 'Relationship', 'college friend')")
        c.execute("INSERT INTO ZABCDCONTACTDATE VALUES (1, 1, 'When we met', 780494400)")
        c.execute("INSERT INTO ZABCDSOCIALPROFILE VALUES "
                  "(1, 1, 'LinkedIn', 'pat', 'https://www.linkedin.com/in/pat')")
    c.execute("CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, "
              "ZUNIQUEID TEXT, ZORGANIZATION TEXT)")
    c.execute("CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, "
              "ZOWNER INTEGER, ZADDRESS TEXT, ZLABEL TEXT)")
    c.execute("CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, "
              "ZOWNER INTEGER, ZFULLNUMBER TEXT, ZLABEL TEXT)")
    c.execute("INSERT INTO ZABCDRECORD VALUES (1, 'AAA:ABPerson', 'OldCo')")
    c.execute("INSERT INTO ZABCDEMAILADDRESS VALUES "
              "(1, 1, 'match@example.com', '_$!<Home>!$_')")
    c.execute("INSERT INTO ZABCDPHONENUMBER VALUES "
              "(1, 1, '+1 (555) 014-9999', '_$!<Mobile>!$_')")
    c.execute("INSERT INTO ZABCDRECORD VALUES (2, 'BBB:ABPerson', '')")
    c.execute("INSERT INTO ZABCDEMAILADDRESS VALUES "
              "(2, 2, 'other@example.com', NULL)")
    c.commit()
    c.close()
    return db


class FindCardsTest(unittest.TestCase):
    def test_match_and_label_unwrap(self):
        with tempfile.TemporaryDirectory() as td:
            db = _fixture_store(td)
            with mock.patch.object(ac, "_stores", return_value=[db]):
                cards = ac.find_cards(["match@example.com"], [])
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0]["uid"], "AAA:ABPerson")
                self.assertEqual(cards[0]["org"], "OldCo")
                self.assertEqual(cards[0]["emails"][0]["label"], "home")
                phone_hit = ac.find_cards([], ["5550149999"])
                self.assertEqual(phone_hit[0]["uid"], "AAA:ABPerson")
                self.assertEqual(ac.find_cards(["nope@example.com"], []), [])


class NoteTest(unittest.TestCase):
    def test_split_and_merge_keep_the_phone_text(self):
        block = "Name: Pat"
        merged = ac.merge_note("met at the pier", block)
        self.assertTrue(merged.startswith("met at the pier\n\n=== Vira ==="))
        foreign, inner = ac.split_note(merged)
        self.assertEqual(foreign, "met at the pier")
        self.assertEqual(inner, block)
        self.assertEqual(ac.split_note("just phone text"), ("just phone text", None))
        # a block with no end marker still yields its text
        self.assertEqual(ac.split_note("x\n=== Vira ===\nName: Pat")[1], "Name: Pat")

    def test_block_is_deterministic_and_complete(self):
        card = {"fields": [{"key": "display_name", "value": "Pat Example"},
                           {"key": "pronouns", "value": "they/them"},
                           {"key": "company", "value": "NewCo"},
                           {"key": "location", "value": "Boston, MA"}],
                "custom": [{"label": "Nickname", "value": "Patty"}], "handles": []}
        detail = {"person": {}, "profile": {
            "relationship_class": "friend", "how_we_met": "At a regatta.",
            "relationship_summary": "Old sailing friend.",
            "personal_facts": [{"fact": "Owns a catboat", "as_of": "2024-05-01"}],
            "topics": [{"topic": "sailing", "status": "active"}],
            "hooks": [{"angle": "Ask about the regatta", "detail": "June race"}],
            "open_loops": [{"what": "Dinner", "owed_by": "me", "since": "2024-03-01"}],
            "comms_style": "Warm.",
            "cadence": {"typical_gap_days": 36.5},
            "last_exchange": {"date": "2024-06-01T10:00", "channel": "imessage",
                              "summary": "Race results."}}}
        a = ac.note_block(card, detail)
        self.assertEqual(a, ac.note_block(card, detail))      # no timestamps
        for needle in ("Name: Pat Example (they/them)", "Relationship: friend",
                       "Company: NewCo", "Location: Boston, MA", "Nickname: Patty",
                       "How we met: At a regatta.", "Summary: Old sailing friend.",
                       "- Owns a catboat (as of 2024-05-01)", "Topics: sailing (active)",
                       "- Ask about the regatta: June race",
                       "- Dinner (owed by me, since 2024-03-01)", "Style: Warm.",
                       "Cadence: typically every 36 days; last exchange 2024-06-01 "
                       "via imessage: Race results."):
            self.assertIn(needle, a)
        # the phone's text is announced, and previous values carry forward
        b = ac.note_block(card, detail, foreign=True,
                          previous=['Previously on phone: company "OldCo"', 'company "OldCo"'])
        self.assertTrue(b.startswith("Phone note kept above."))
        self.assertEqual(b.count('Previously on phone: company "OldCo"'), 1)

    def test_block_is_capped_with_a_pointer(self):
        detail = {"person": {}, "profile": {
            "personal_facts": [{"fact": "x" * 200} for _ in range(60)]}}
        a = ac.note_block({"fields": [], "custom": [], "handles": []}, detail)
        self.assertLessEqual(len(a), ac.NOTE_CAP + 20)
        self.assertTrue(a.endswith("(more in Vira)"))

    def test_plan_covers_title_address_urls_and_note(self):
        card = {"fields": [{"key": "title", "value": "Partner"},
                           {"key": "location", "value": "Boston, MA"}],
                "custom": [{"label": "LinkedIn", "value": "https://example.com/pat"}],
                "handles": []}
        apple = {"uid": "A:ABPerson", "org": "", "title": "Associate",
                 "emails": [], "phones": [], "urls": [], "addresses": [],
                 "note": "phone text"}
        plan = ac.card_plan(card, {"person": {}}, apple)
        self.assertEqual(plan["changed"], ["title", "address", "urls", "note"])
        self.assertTrue(plan["foreign_note"])
        s = ac.build_script("A:ABPerson", **plan["kw"])
        self.assertIn('set job title of P to "Partner"', s)
        self.assertIn('make new address at end of addresses of P with properties '
                      '{label:"home", city:"Boston", state:"MA"}', s)
        self.assertIn('make new url at end of urls of P with properties '
                      '{label:"LinkedIn", value:"https://example.com/pat"}', s)
        self.assertIn('set note of P to "phone text" & linefeed & "" & linefeed '
                      '& "=== Vira ==="', s)
        # an existing address or url is left alone, never replaced
        apple["addresses"] = [{"label": "work", "city": "Elsewhere", "state": ""}]
        apple["urls"] = [{"value": "https://example.com/pat/", "label": ""}]
        plan = ac.card_plan(card, {"person": {}}, apple)
        self.assertNotIn("address", plan["changed"])
        self.assertNotIn("urls", plan["changed"])

    def test_plan_fills_the_native_slots(self):
        card = {"fields": [{"key": "relationship", "value": "College friend"},
                           {"key": "met", "value": "9/25/2025"}],
                "custom": [{"label": "LinkedIn",
                            "value": "https://www.linkedin.com/in/pat-example"},
                           {"label": "Blog", "value": "https://example.com/pat"}],
                "handles": []}
        apple = {"uid": "A:ABPerson", "org": "", "title": "", "emails": [],
                 "phones": [], "urls": [], "addresses": [], "note": ""}
        plan = ac.card_plan(card, {"person": {}}, apple)
        self.assertEqual(plan["changed"],
                         ["relationship", "met", "urls", "social", "note"])
        self.assertEqual(plan["kw"]["related"], [("Relationship", "College friend")])
        self.assertEqual(plan["kw"]["dates"], [("When we met", (2025, 9, 25))])
        self.assertEqual(plan["kw"]["social"],
                         [("LinkedIn", "pat-example",
                           "https://www.linkedin.com/in/pat-example")])
        # the profile link is a social profile, not also a URL row
        self.assertEqual(plan["kw"]["urls"], [("Blog", "https://example.com/pat")])
        self.assertEqual(plan["conflicts"], [])
        self.assertIn("When we met: 9/25/2025", plan["kw"]["note"])
        # a card that already has them (labels arrive lower-cased from the
        # store): the date and the profile are left alone; the relationship
        # is the one scalar, replaced and flagged with the phone's value
        apple.update({"related": [{"label": "relationship", "value": "friend"}],
                      "dates": [{"label": "when we met", "date": (2024, 1, 2)}],
                      "social": [{"service": "LinkedIn", "user": "old", "url": ""}]})
        plan = ac.card_plan(card, {"person": {}}, apple)
        self.assertNotIn("met", plan["changed"])
        self.assertNotIn("social", plan["changed"])
        self.assertNotIn("related", plan["kw"])
        self.assertEqual(plan["kw"]["related_set"], [("Relationship", "College friend")])
        self.assertEqual(plan["conflicts"], [{"field": "relationship",
                                              "crm": "College friend", "phone": "friend"}])
        self.assertIn('Previously on phone: relationship "friend"', plan["kw"]["note"])
        # the same value in another case is current: nothing to write
        apple["related"] = [{"label": "Relationship", "value": "college friend"}]
        plan = ac.card_plan(card, {"person": {}}, apple)
        self.assertNotIn("relationship", plan["changed"])
        # a date with no year stays in the note only
        card["fields"][1]["value"] = "September 2025"
        apple["dates"] = []
        plan = ac.card_plan(card, {"person": {}}, apple)
        self.assertNotIn("met", plan["changed"])
        self.assertIn("When we met: September 2025", plan["kw"]["note"])

    def test_create_script_adds_without_deleting(self):
        s = ac.build_create_script("Pat Q. Example, M.D.", company="NewCo",
                                   emails=[("pat@example.com", "home")],
                                   phones=[("5550101234", "mobile")],
                                   related=[("Relationship", "friend")],
                                   dates=[("When we met", (2025, 9, 25))],
                                   social=[("GitHub", "pat", "https://github.com/pat")],
                                   note="=== Vira ===\nName: Pat\n=== end Vira ===")
        self.assertIn('first name:"Pat Q.", last name:"Example"', s)
        self.assertNotIn("delete every", s)
        self.assertIn("return id of P", s)
        self.assertIn('make new email at end of emails of P', s)
        self.assertIn('make new related name at end of related names of P', s)
        self.assertIn('make new custom date at end of custom dates of P', s)
        self.assertIn('service name:"GitHub"', s)
        self.assertEqual(ac.split_name("Cher"), ("Cher", ""))


class LoadCardsTest(unittest.TestCase):
    def test_rich_loader_tolerates_a_minimal_store(self):
        with tempfile.TemporaryDirectory() as d:
            db = _fixture_store(d)
            cards = ac.load_cards([db])
        self.assertEqual(len(cards), 2)
        a = next(c for c in cards if c["uid"] == "AAA:ABPerson")
        self.assertEqual(a["org"], "OldCo")
        self.assertEqual(a["emails"][0]["label"], "home")
        self.assertEqual(a["note"], "")
        self.assertIsNone(a["created"])
        self.assertEqual((a["related"], a["dates"], a["social"]), ([], [], []))
        idx = ac.index_cards(cards)
        self.assertEqual([c["uid"] for c in ac.match_cards(cards, idx, [], ["5550149999"])],
                         ["AAA:ABPerson"])

    def test_native_slots_are_read(self):
        with tempfile.TemporaryDirectory() as d:
            cards = ac.load_cards([_fixture_store(d, slots=True)])
        a = next(c for c in cards if c["uid"] == "AAA:ABPerson")
        self.assertEqual(a["related"], [{"label": "relationship", "value": "college friend"}])
        self.assertEqual(a["dates"], [{"label": "when we met", "date": (2025, 9, 25)}])
        self.assertEqual(a["social"], [{"service": "LinkedIn", "user": "pat",
                                        "url": "https://www.linkedin.com/in/pat"}])
        b = next(c for c in cards if c["uid"] == "BBB:ABPerson")
        self.assertEqual((b["related"], b["dates"], b["social"]), ([], [], []))


class PushTest(unittest.TestCase):
    def _card(self):
        return {"pid": "p_x", "custom": [], "updated": None,
                "fields": [{"key": "company", "value": "NewCo"},
                           {"key": "birthday", "value": "8/3/1991"},
                           {"key": "display_name", "value": "Pat Example"}],
                "handles": [row("email", "match@example.com", rank="primary",
                                use="personal"),
                            row("email", "gone@example.com", rank="archived")]}

    def test_push_updates_matched_card(self):
        scripts = []
        apple = {"uid": "AAA:ABPerson", "store": "s", "org": "OldCo",
                 "emails": [{"value": "match@example.com", "label": "home"},
                            {"value": "extra@example.com", "label": "work"}],
                 "phones": []}
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[apple]), \
             mock.patch.object(ac, "backup_vcard", return_value=None), \
             mock.patch.object(ac, "_osascript",
                               side_effect=lambda s: scripts.append(s)), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}, "master": {}}), \
             mock.patch("server.contactcard.compose",
                        return_value=self._card()):
            out = ac.push_person("p_x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["cards"][0]["changed"],
                         ["company", "birthday", "emails", "note"])
        s = scripts[0]
        self.assertIn('set organization of P to "NewCo"', s)
        # the note carries the Vira block, and the phone's old company
        self.assertIn('"=== Vira ==="', s)
        self.assertIn('Previously on phone: company \\"OldCo\\"', s)
        self.assertEqual(out["cards"][0]["conflicts"],
                         [{"field": "company", "crm": "NewCo", "phone": "OldCo"}])
        self.assertFalse(out["cards"][0]["foreign_note"])   # no phone text
        # the Apple-only address survived the rewrite
        self.assertIn("extra@example.com", s)
        # archived sank to the CRM block's end wearing "old", before extras
        self.assertLess(s.index("match@example.com"),
                        s.index("gone@example.com"))
        self.assertIn('{label:"old", value:"gone@example.com"}', s)

    def test_no_match_is_honest(self):
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[]), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}}), \
             mock.patch("server.contactcard.compose",
                        return_value=self._card()):
            out = ac.push_person("p_x")
        self.assertFalse(out["ok"])
        self.assertIn("no Apple contact", out["detail"])

    def test_current_card_not_rewritten(self):
        apple = {"uid": "AAA:ABPerson", "store": "s", "org": "NewCo",
                 "emails": [{"value": "match@example.com", "label": "home"},
                            {"value": "gone@example.com", "label": "old"}],
                 "phones": []}
        card = self._card()
        card["fields"] = [{"key": "company", "value": "NewCo"},
                          {"key": "birthday", "value": ""}]
        # the note already holds this card's block: nothing to rewrite
        apple["note"] = ac.merge_note("", ac.note_block(card, {"person": {}}))
        ran = []
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[apple]), \
             mock.patch.object(ac, "_osascript",
                               side_effect=lambda s: ran.append(s)), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}}), \
             mock.patch("server.contactcard.compose", return_value=card):
            out = ac.push_person("p_x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["cards"][0]["changed"], [])
        self.assertEqual(ran, [])
        self.assertIn("already current", out["detail"])

    def test_osascript_failure_never_raises(self):
        apple = {"uid": "AAA:ABPerson", "store": "s", "org": "Old",
                 "emails": [{"value": "match@example.com", "label": ""}],
                 "phones": []}
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[apple]), \
             mock.patch.object(ac, "backup_vcard", return_value=None), \
             mock.patch.object(ac, "_osascript",
                               side_effect=RuntimeError("-600")), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}}), \
             mock.patch("server.contactcard.compose",
                        return_value=self._card()):
            out = ac.push_person("p_x")
        self.assertFalse(out["ok"])
        self.assertIn("-600", out["detail"])


if __name__ == "__main__":
    unittest.main()


def _dup_card(uid, name, *, emails=(), phones=(), org="", created=None):
    first, last = ac.split_name(name)
    return {"uid": uid, "store": "s", "pk": 1, "first": first, "last": last,
            "name": name, "org": org, "title": "", "nickname": "", "note": "",
            "birthday": None, "created": created, "modified": created,
            "emails": [{"value": e, "label": "home"} for e in emails],
            "phones": [{"value": p, "label": "mobile"} for p in phones],
            "urls": [], "addresses": [], "related": [], "dates": [], "social": []}


class DuplicatesTest(unittest.TestCase):
    """find_duplicates and fold_cards (2026-10-28): cards that read as one
    person, and the fold that keeps one of them."""

    def _cards(self):
        return [
            _dup_card("A", "Pat Example", emails=["pat@example.com"], created=100.0),
            _dup_card("B", "Patricia Example", phones=["+1 555 010 0100"], created=200.0),
            _dup_card("C", "Sam Other", emails=["sam@example.com"]),
            _dup_card("D", "Sam Other", phones=["5550100200"]),
            _dup_card("E", "Lee Third", phones=["5550100300"]),
            _dup_card("F", "Kim Fourth", phones=["5550100300"]),     # a line, not a twin
            _dup_card("G", "", emails=["pat@example.com"]),          # unnamed, Pat's email
            _dup_card("H", "W One", phones=["5550100400"]),
            _dup_card("I", "X Two", phones=["5550100400"]),
            _dup_card("J", "Y Three", phones=["5550100400"]),
            _dup_card("K", "Z Four", phones=["5550100400"]),         # a switchboard
            _dup_card("L", "J. Example", emails=["j@example.com"]),  # an initial: no match
        ]

    def test_find_duplicates_groups_names_and_shared_handles(self):
        groups = ac.find_duplicates(self._cards())
        self.assertEqual([set(g["uids"]) for g in groups], [{"A", "B", "G"}, {"C", "D"}])
        pat = groups[0]
        self.assertEqual(pat["keep"], "A")
        self.assertIn("Pat / Patricia", pat["why"])
        self.assertIn("share pat@example.com", pat["why"])
        self.assertEqual(groups[1]["why"], ["same name"])
        self.assertEqual([c["uid"] for c in pat["cards"]][0], "A")

    def test_a_line_is_not_a_signal(self):
        groups = ac.find_duplicates(self._cards(), lines={"pat@example.com"})
        self.assertEqual([set(g["uids"]) for g in groups], [{"A", "B"}, {"C", "D"}])

    def test_fold_pushes_the_union_then_deletes(self):
        cards = self._cards()
        scripts = []
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "backup_vcard", return_value=None), \
             mock.patch.object(ac, "_osascript", side_effect=lambda s: scripts.append(s) or "ok"):
            out = ac.fold_cards("A", ["B", "G", "A", "nope"], cards=cards)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["pushed"], ["+1 555 010 0100"])
        self.assertEqual(out["deleted"], ["B", "G"])
        self.assertEqual(out["errors"], ["nope: not on the Mac"])
        self.assertEqual(len(scripts), 3)
        self.assertIn('set P to person id "A"', scripts[0])
        self.assertIn('value:"+1 555 010 0100"', scripts[0])
        self.assertIn("delete every phone of P", scripts[0])
        self.assertNotIn("email", scripts[0])             # nothing new to add
        self.assertIn('delete person id "B"', scripts[1])
        self.assertIn('delete person id "G"', scripts[2])

    def test_fold_refuses_honestly(self):
        with mock.patch.object(ac, "enabled", return_value=False):
            self.assertFalse(ac.fold_cards("A", ["B"], cards=self._cards())["ok"])
        with mock.patch.object(ac, "enabled", return_value=True):
            out = ac.fold_cards("nope", ["B"], cards=self._cards())
            self.assertFalse(out["ok"])
            self.assertIn("not on the Mac", out["detail"])
            self.assertFalse(ac.fold_cards("A", ["A"], cards=self._cards())["ok"])

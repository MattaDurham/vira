"""Admin inbox (server/admininbox.py): institutional senders are chores, not
people waiting on a reply.

Covers the sender classifier, the rule pass (kind, chore line, due date),
collect() over a synthetic chat.db and mail feed, the brief's waiting lists
skipping those senders (the join), and the model pass keeping a due date
only when its quote is an exact span of the message. No real store, chat.db
or config is read: every source the module touches is patched.

Run: .venv/bin/python -m unittest discover tests
"""
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import admininbox, brief

APPLE_EPOCH = 978307200


def _ns(unix):
    return int((unix - APPLE_EPOCH) * 1e9)


FRIEND = {"id": "p_test0000frnd", "name": "Casey Example", "class_hint": "friend"}
SHORT = {"id": "p_test0000shrt", "name": "(unidentified)"}
PHARM = {"id": "p_test0000phrm", "name": "Example Pharmacy", "class_hint": "business"}


def _crm():
    by_id = {p["id"]: p for p in (FRIEND, SHORT, PHARM)}
    return {"by_id": by_id, "people": list(by_id.values()), "profiles": {}}


HANDLES = {"+12125550123": FRIEND["id"], "29900": SHORT["id"],
           "+12125550188": PHARM["id"]}


class Signals(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(admininbox.triage, "verdict_for", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_short_code_placeholder_is_institutional(self):
        self.assertIn("short-code sender", admininbox.signals("29900", SHORT, []))

    def test_named_friend_forwarding_a_code_stays_a_person(self):
        self.assertEqual(admininbox.signals(
            "+12125550123", FRIEND, ["Your verification code is 123456"]), [])

    def test_contact_named_like_an_organization(self):
        self.assertIn("organization name",
                      admininbox.signals("+12125550188", PHARM, ["Hi"]))

    def test_company_class_hint(self):
        self.assertIn("company contact", admininbox.signals(
            "+12125550101", {"name": "Example Bank", "class_hint": "company"}, []))

    def test_unknown_number_with_automated_content(self):
        self.assertIn("message content: automated", admininbox.signals(
            "+12125550190", None, ["Reply STOP to opt out. Msg & data rates apply."]))

    def test_unknown_plain_number_is_a_person(self):
        self.assertEqual(admininbox.signals("+12125550190", None, ["are we still on?"]), [])

    def test_noreply_mail_even_for_a_named_contact(self):
        self.assertIn("system mailbox",
                      admininbox.signals("no-reply@example.com", FRIEND, []))

    def test_role_mailbox_only_for_unnamed_senders(self):
        self.assertEqual(admininbox.signals("hello@example.com", FRIEND, []), [])
        self.assertIn("system mailbox",
                      admininbox.signals("hello@example.com", None, []))


class RulePass(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(admininbox.settings, "get", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def test_kind(self):
        self.assertEqual(admininbox._kind("Your verification code is 123456"), "code")
        self.assertEqual(admininbox._kind("Please register for the new portal"), "chore")
        self.assertEqual(admininbox._kind("Shop now: 20% off everything"), "promo")
        self.assertEqual(admininbox._kind("Your order has shipped."), "info")

    def test_chore_line_is_the_asking_sentence(self):
        text = "Hello from Example Health. Please register for our new patient portal this weekend."
        self.assertEqual(admininbox._rule_chore(text, "Example Health"),
                         "Example Health: Please register for our new patient portal this weekend.")
        self.assertEqual(admininbox._rule_chore("Example Health asks you to register.", "Example Health"),
                         "Example Health asks you to register.")
        self.assertTrue(admininbox._rule_chore("Your bill is ready.", "Example Power")
                        .startswith("Example Power: "))

    def test_due_this_weekend_is_that_sunday(self):
        when = "2026-10-07T10:00:00+00:00"   # a Wednesday
        self.assertEqual(admininbox._rule_due("Register this weekend", when), "2026-10-11")
        self.assertEqual(admininbox._rule_due("Register next weekend", when), "2026-10-18")

    def test_due_explicit_date_and_none(self):
        when = "2026-10-07T10:00:00+00:00"
        self.assertEqual(admininbox._rule_due("Payment due by October 20, 2026.", when),
                         "2026-10-20")
        self.assertIsNone(admininbox._rule_due("Your order has shipped.", when))


class Labels(unittest.TestCase):
    def test_short_code_label_and_urgency_prefix(self):
        with mock.patch.object(admininbox.triage, "business_signals", return_value=([], "")):
            self.assertEqual(admininbox.org_name("85237(smsft)", None,
                                                 ["IMPORTANT: your claim needs review"]),
                             "Short code 85237")
            self.assertEqual(admininbox.org_name("29900", None, ["Example code: 4821"]),
                             "Short code 29900")
            self.assertEqual(admininbox.org_name("29900", None, ["Example Health: visit at 3"]),
                             "Example Health")

    def test_no_doubled_label(self):
        self.assertEqual(admininbox._rule_chore("Example Health: new message in your portal",
                                                "Example Health"),
                         "Example Health: new message in your portal")

    def test_code_shapes(self):
        self.assertEqual(admininbox._kind("Example: Your code is 458666. Don't share it."), "code")
        self.assertEqual(admininbox._kind("Example code: 83243"), "code")

    def test_numeric_date(self):
        with mock.patch.object(admininbox.settings, "get", return_value=""):
            self.assertEqual(admininbox._rule_due("upgrade starts 11/02/2026 at 5 PM",
                                                  "2026-10-07T10:00:00+00:00"), "2026-11-02")
            self.assertEqual(admininbox._rule_due("renew by 1/5", "2026-12-20T10:00:00+00:00"),
                             "2027-01-05")

    def test_passed_appointment_is_spent_not_overdue(self):
        appt = {"kind": "chore", "due": "2026-10-01", "due_quote": None,
                "text": "Reminder: your appointment is on October 1, 2026 at 3 PM."}
        bill = {"kind": "chore", "due": "2026-10-01", "due_quote": None,
                "text": "Your payment of $30 is due October 1, 2026."}
        self.assertEqual(admininbox._stage(appt, "2026-10-10"), "notice")
        self.assertEqual(admininbox._stage(bill, "2026-10-10"), "overdue")
        self.assertEqual(admininbox._stage(bill, "2026-10-01"), "today")


class Collect(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.when_short = dt.datetime.fromtimestamp(now - 3600).astimezone().isoformat()
        rows = [
            ("+12125550123", _ns(now - 600), "Dinner Friday?", None),
            ("29900", _ns(now - 3600),
             "Example Health: register for the new Beacon portal this weekend. Reply STOP to opt out.", None),
            ("29900", _ns(now - 7200), "Your verification code is 482913", None),
        ]
        fresh = dt.datetime.now().astimezone().isoformat()
        self.feed = [
            {"channel": "email", "handle": "no-reply@example.com", "person_id": None,
             "person_name": "Example Carrier", "when": fresh, "account": "a",
             "subject": "Delivery attempt: signature required",
             "text": "Delivery attempt: signature required - reschedule online"},
            {"channel": "email", "handle": "casey@example.com", "person_id": FRIEND["id"],
             "person_name": FRIEND["name"], "when": fresh,
             "subject": "photos", "text": "photos - from the trip"},
        ]
        self.dismissed = set()
        patches = [
            mock.patch.object(admininbox, "_imessage_rows", return_value=rows),
            mock.patch.object(admininbox.crm, "_load", return_value=_crm()),
            mock.patch.object(admininbox.crm, "resolve_handle", side_effect=HANDLES.get),
            mock.patch.object(admininbox.triage, "_verdicts", return_value=[]),
            mock.patch.object(admininbox.settings, "get", return_value=""),
            mock.patch.object(admininbox.settings, "fixture_mode", return_value=False),
            mock.patch.object(admininbox.briefstate, "dismissed_keys",
                              side_effect=lambda: set(self.dismissed)),
            mock.patch.object(admininbox, "STORE", Path(tempfile.mkdtemp()) / "admin.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_people_out_institutions_in(self):
        got = admininbox.collect(self.feed)
        handles = [it["handle"] for it in got["items"]]
        self.assertNotIn("+12125550123", handles)
        self.assertNotIn("casey@example.com", handles)
        self.assertIn("no-reply@example.com", handles)
        portal = next(it for it in got["items"] if "Beacon portal" in it["text"])
        self.assertEqual(portal["kind"], "chore")
        self.assertEqual(portal["sender"], "Example Health")
        self.assertIsNotNone(portal["due"])
        self.assertIn(portal["stage"], ("today", "upcoming"))
        code = next(it for it in got["items"] if "482913" in it["text"])
        self.assertEqual(code["stage"], "notice")
        # chores sort ahead of notices
        stages = [it["stage"] for it in got["items"]]
        self.assertEqual(stages[-1], "notice")

    def test_done_rows_leave_and_come_back_on_request(self):
        first = admininbox.collect(self.feed)["items"][0]
        self.dismissed.add(first["key"])
        got = admininbox.collect(self.feed)
        self.assertNotIn(first["key"], [it["key"] for it in got["items"]])
        self.assertEqual(got["hidden"], 1)
        again = admininbox.collect(self.feed, include_hidden=True)
        self.assertIn(first["key"], [it["key"] for it in again["items"]])

    def test_extract_keeps_only_grounded_due_dates(self):
        got = admininbox.collect(self.feed)
        portal = next(it for it in got["items"] if "Beacon portal" in it["text"])
        carrier = next(it for it in got["items"] if it["channel"] == "email")
        reply = {"items": [
            {"key": portal["key"], "kind": "chore",
             "chore": "Register for Example Health's Beacon portal",
             "due": "2030-01-01", "due_quote": "by New Year"},       # not in text
            {"key": carrier["key"], "kind": "chore",
             "chore": "Reschedule the Example Carrier delivery",
             "due": "2026-10-12", "due_quote": "signature required"},
        ]}
        with mock.patch.object(admininbox.suggest, "complete",
                               return_value=json.dumps(reply)) as complete, \
             mock.patch.object(admininbox.modelbudget, "context_chars", return_value=10**6):
            out = admininbox.extract(self.feed)
        self.assertEqual(complete.call_args.kwargs.get("tools"), [])
        by_key = {it["key"]: it for it in out["items"]}
        self.assertEqual(by_key[portal["key"]]["chore"], "Register for Example Health's Beacon portal")
        self.assertEqual(by_key[portal["key"]]["source"], "model")
        # ungrounded model date dropped; the rule pass's weekend date stands
        self.assertEqual(by_key[portal["key"]]["due"], portal["due"])
        self.assertEqual(by_key[carrier["key"]]["due"], "2026-10-12")

    def test_extract_degrades_when_model_is_down(self):
        with mock.patch.object(admininbox.suggest, "complete", side_effect=RuntimeError("down")), \
             mock.patch.object(admininbox.modelbudget, "context_chars", return_value=10**6):
            out = admininbox.extract(self.feed)
        self.assertIn("model unavailable", out["status"]["extract"])
        self.assertTrue(out["items"])

    def test_repeat_chore_from_one_sender_is_one_row(self):
        now = time.time()
        rows = [("29900", _ns(now - 60 * k), "Example Health: you have a new message in your portal", None)
                for k in (1, 2)]
        with mock.patch.object(admininbox, "_imessage_rows", return_value=rows):
            got = admininbox.collect([])
        self.assertEqual(len(got["items"]), 1)
        self.assertEqual(len(got["items"][0]["also"]), 1)

    def test_mark_rejects_foreign_keys(self):
        with self.assertRaises(ValueError):
            admininbox.mark("wait-im:p_x:2026")


class _Con:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_a):
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class WaitingListSkipsInstitutions(unittest.TestCase):
    """The join: the brief's waiting list must not show a short code or a
    pharmacy as a person owed a reply, while a real friend stays."""

    def test_unreplied_imessages(self):
        now = time.time()
        rows = [("+12125550123", _ns(now - 600), 0, "Dinner Friday?", None),
                ("29900", _ns(now - 900), 0, "Example Health: register this weekend", None),
                ("+12125550188", _ns(now - 1200), 0, "Your prescription is ready for pickup", None)]
        with mock.patch.object(brief.imessage, "_connect", return_value=_Con(rows)), \
             mock.patch.object(brief.crm, "resolve_handle", side_effect=HANDLES.get), \
             mock.patch.object(brief.crm, "_load", return_value=_crm()), \
             mock.patch.object(admininbox.triage, "verdict_for", return_value=None), \
             mock.patch.object(brief.threadread, "brief_asks", return_value=None):
            out = brief._unreplied_imessages()
        self.assertEqual([r["person_id"] for r in out], [FRIEND["id"]])

    def test_recent_mail(self):
        fresh = dt.datetime.now().astimezone().isoformat()
        items = [
            {"channel": "email", "person_id": FRIEND["id"], "handle": "no-reply@example.com",
             "person_name": FRIEND["name"], "when": fresh, "text": "Your statement is ready"},
            {"channel": "email", "person_id": FRIEND["id"], "handle": "casey@example.com",
             "person_name": FRIEND["name"], "when": fresh, "text": "photos"},
        ]
        with mock.patch.object(brief.crm, "_load", return_value=_crm()):
            out = brief._recent_mail(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["preview"], "photos")


if __name__ == "__main__":
    unittest.main()

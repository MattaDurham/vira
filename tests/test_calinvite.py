"""The RSVP ladder.

Every case is offline: no IMAP, no SMTP, no HTTP. The one thing these must
never do is answer a real invitation, so nothing here touches a network
client — the rungs are exercised on their inputs.
"""
import os
import unittest
from unittest import mock

from server import calinvite

# A Google invitation's real shape: a VTIMEZONE whose DAYLIGHT/STANDARD
# subcomponents carry their OWN DTSTART, ahead of the VEVENT. This is the
# arrangement that made a reply carry a 1970 start date.
ICS = """BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
CALSCALE:GREGORIAN
METHOD:REQUEST
BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
DTSTART:19700308T020000
TZNAME:EDT
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
DTSTART:19701101T020000
TZNAME:EST
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260830T150000
DTEND;TZID=America/New_York:20260830T170000
UID:abc123xyz@google.com
SEQUENCE:3
ORGANIZER;CN=A Organizer:mailto:organizer@example.com
SUMMARY:Sunday planning
END:VEVENT
END:VCALENDAR
"""

HTML = (
    '<a href="https://calendar.google.com/calendar/event?action=RESPOND'
    '&amp;eid=EID&amp;rst=1&amp;tok=TOK1">Yes</a>'
    '<a href="https://calendar.google.com/calendar/event?action=RESPOND'
    '&amp;eid=EID&amp;rst=2&amp;tok=TOK2">No</a>'
    '<a href="https://calendar.google.com/calendar/event?action=RESPOND'
    '&amp;eid=EID&amp;rst=3&amp;tok=TOK3">Maybe</a>'
    '<a href="https://calendar.google.com/calendar/event?action=VIEW'
    '&amp;eid=EID">More</a>')


class Answers(unittest.TestCase):
    def test_the_words_a_person_actually_types_map_to_an_answer(self):
        for word in ("yes", "Yes", "yep", "ok", "sure", "reply yes",
                     "accept", "going"):
            self.assertEqual(calinvite.norm_answer(word), "yes", word)
        for word in ("no", "nope", "decline", "cant make it"):
            self.assertEqual(calinvite.norm_answer(word), "no", word)
        self.assertEqual(calinvite.norm_answer("maybe"), "maybe")

    def test_an_unrecognised_word_is_not_coerced_to_the_nearest_answer(self):
        # This decides an outward action. "I don't know what you meant" has
        # to be reachable, or every stray text becomes an RSVP.
        for word in ("later", "who is that", "yes but move it", "", "why"):
            self.assertIsNone(calinvite.norm_answer(word), word)


class RespondLinks(unittest.TestCase):
    def test_each_answer_picks_its_own_link(self):
        self.assertIn("rst=1", calinvite.respond_link(HTML, "yes"))
        self.assertIn("rst=2", calinvite.respond_link(HTML, "no"))
        self.assertIn("rst=3", calinvite.respond_link(HTML, "maybe"))

    def test_the_url_is_unescaped_before_use(self):
        # Fetching the escaped form sends "&amp;rst=1" and the answer is
        # silently dropped — a request that succeeds and does nothing.
        url = calinvite.respond_link(HTML, "yes")
        self.assertNotIn("&amp;", url)
        self.assertIn("&rst=1", url)

    def test_a_mail_with_no_rsvp_links_yields_nothing(self):
        self.assertEqual(calinvite.respond_link("<p>hello</p>", "yes"), "")


class BuildReply(unittest.TestCase):
    def test_the_reply_carries_the_events_own_identity(self):
        ics, org, summary = calinvite.build_reply(
            ICS, "owner@example.com", "yes", "A Owner")
        self.assertIn("METHOD:REPLY", ics)
        self.assertIn("UID:abc123xyz@google.com", ics)
        self.assertIn("SEQUENCE:3", ics)
        self.assertIn("PARTSTAT=ACCEPTED:mailto:owner@example.com", ics)
        self.assertEqual(org, "mailto:organizer@example.com")
        self.assertEqual(summary, "Sunday planning")

    def test_dtstart_comes_from_the_event_not_the_timezone_block(self):
        # The regression. A whole-file property scan returns the VTIMEZONE's
        # DST transition (19700308T020000) because it appears first, and the
        # reply then describes an event in 1970. Caught against the real
        # invitation, not by reading the code.
        ics, _, _ = calinvite.build_reply(ICS, "owner@example.com", "yes")
        self.assertIn("DTSTART;TZID=America/New_York:20260830T150000", ics)
        self.assertNotIn("19700308", ics)

    def test_each_answer_writes_its_own_partstat(self):
        for answer, partstat in (("yes", "ACCEPTED"), ("no", "DECLINED"),
                                 ("maybe", "TENTATIVE")):
            ics, _, _ = calinvite.build_reply(ICS, "owner@example.com", answer)
            self.assertIn(f"PARTSTAT={partstat}", ics)

    def test_an_invitation_with_no_uid_is_refused_not_guessed(self):
        with self.assertRaises(calinvite.RsvpError):
            calinvite.build_reply("BEGIN:VCALENDAR\nEND:VCALENDAR",
                                  "owner@example.com", "yes")

    def test_a_long_property_is_folded(self):
        long_ics = ICS.replace("UID:abc123xyz@google.com",
                               "UID:" + "u" * 200 + "@example.com")
        ics, _, _ = calinvite.build_reply(long_ics, "owner@example.com", "yes")
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 76, line[:40])


class CalendarPart(unittest.TestCase):
    def _msg(self, parts):
        from email.mime.base import MIMEBase
        from email.mime.multipart import MIMEMultipart
        m = MIMEMultipart("mixed")
        m["Subject"] = "Invitation: Sunday planning"
        for ctype, payload, fname in parts:
            maintype, subtype = ctype.split("/")
            sub = MIMEBase(maintype, subtype)
            sub.set_payload(payload.encode("utf-8"))
            sub.add_header("Content-Transfer-Encoding", "8bit")
            if fname:
                sub.add_header("Content-Disposition", "attachment",
                               filename=fname)
            m.attach(sub)
        return m

    def test_the_inline_calendar_part_is_found(self):
        m = self._msg([("text/plain", "hi", None),
                       ("text/calendar", ICS, None)])
        self.assertIn("METHOD:REQUEST", calinvite.calendar_part(m))

    def test_an_ics_attachment_is_found_when_there_is_no_inline_part(self):
        m = self._msg([("text/plain", "hi", None),
                       ("application/octet-stream", ICS, "invite.ics")])
        self.assertIn("METHOD:REQUEST", calinvite.calendar_part(m))

    def test_an_ordinary_email_yields_nothing(self):
        m = self._msg([("text/plain", "just a note", None)])
        self.assertEqual(calinvite.calendar_part(m), "")


class Refusals(unittest.TestCase):
    def test_a_passive_instance_refuses_to_answer_a_real_invitation(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(calinvite.RsvpError) as cm:
                calinvite.rsvp("a@example.com", "r", "yes")
        self.assertIn("passive", str(cm.exception))

    def test_an_unknown_answer_is_refused_before_anything_is_read(self):
        with self.assertRaises(calinvite.RsvpError):
            calinvite.rsvp("a@example.com", "r", "probably")


if __name__ == "__main__":
    unittest.main()

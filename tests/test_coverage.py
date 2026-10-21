"""Childcare coverage gaps, end to end on synthetic data.

Every store is a temp file, the clock is pinned, the Calendar.app adapter
and the iMessage ping are replaced, and school feeds are served from an
in-memory ICS body. Nothing reads the machine's calendars, messages or
config. Names and handles are synthetic (555-01xx block).
"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import calendarplan as plans
from server import coverage, notify, reviewqueue

# Tuesday 2030-09-10, 08:00 in New York.
NOW = dt.datetime(2030, 9, 10, 12, tzinfo=dt.timezone.utc)
ZONE = "America/New_York"

ROUTINE = {
    "caregivers": [{"name": "Pat", "handles": ["+15555550101"]},
                   {"name": "Robin", "handles": []}],
    "kids": [
        {"name": "Ada", "pickup": "14:55", "school_ics": "https://school.example.com/a.ics",
         "cover": {"mon": "Pat", "tue": "Pat", "wed": "Pat", "thu": "me", "fri": ""}},
        {"name": "Bea", "pickup": "15:10",
         "cover": {"mon": "Robin", "tue": "Pat", "wed": "Robin", "thu": "Robin", "fri": ""}},
    ],
}

ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0",
    "BEGIN:VEVENT", "UID:half-1", "DTSTART;VALUE=DATE:20300911",
    "SUMMARY:Early dismissal 12:00pm", "END:VEVENT",
    "BEGIN:VEVENT", "UID:closed-1", "DTSTART;VALUE=DATE:20300913",
    "SUMMARY:No school - staff development day", "END:VEVENT",
    "BEGIN:VEVENT", "UID:closed-2", "DTSTART;VALUE=DATE:20300916",
    "SUMMARY:Schools closed", "END:VEVENT",
    "BEGIN:VEVENT", "UID:fair", "DTSTART:20300917T220000Z", "DTEND:20300917T233000Z",
    "SUMMARY:Science fair", "END:VEVENT",
    "END:VCALENDAR", ""])


def message(text, when=NOW, rowid=1, handle="+15555550199", name="Sam Example"):
    return {"rowid": rowid, "when": when.isoformat(), "channel": "imessage", "text": text,
            "handle": handle, "person_name": name, "group": False}


class CoverageCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cfg = {"assistant_enabled": True, "assistant_timezone": ZONE,
                    "assistant_calendar_name": "Personal test", "coverage_enabled": True}
        self.pings = []
        for patcher in (
                mock.patch.object(coverage, "STORE", root / "coverage.json"),
                mock.patch.object(plans, "STORE", root / "calendar-plans.json"),
                mock.patch.object(coverage, "_now", return_value=NOW),
                mock.patch.object(plans, "_now", return_value=NOW),
                mock.patch.object(coverage.settings, "raw", side_effect=lambda: dict(self.cfg)),
                mock.patch.object(plans.settings, "sandboxed", return_value=False),
                mock.patch.object(plans.settings, "fixture_mode", return_value=False),
                mock.patch.object(plans.settings, "IS_MAC", True),
                mock.patch.object(plans, "_destination_cache", {"at": 0, "value": None}),
                mock.patch.object(plans, "_metadata_native", return_value={"calendars": [
                    {"id": "cal-1", "native_id": "cal-1", "name": "Personal test",
                     "writable": True, "is_default": True}], "default_id": "cal-1"}),
                mock.patch.object(coverage, "_fetch_ics", return_value=ICS),
                mock.patch.object(notify, "agent_ping",
                                  side_effect=lambda text, key=None: self.pings.append((key, text)) or True),
                mock.patch.dict(os.environ, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(plans, "_calendar_create", return_value={"uid": "event-1"})
        self.create = patcher.start()
        self.addCleanup(patcher.stop)
        coverage.save_routine(ROUTINE)

    def open_gaps(self):
        return coverage.gaps()


class RoutineTests(CoverageCase):
    def test_routine_is_normalized_and_validated(self):
        saved = coverage.routine()
        self.assertEqual(saved["kids"][0]["cover"]["thu"], "me")
        self.assertEqual(saved["kids"][0]["days"], ["mon", "tue", "wed", "thu", "fri"])
        with self.assertRaises(ValueError):
            coverage.validate_routine({"kids": [{"name": "Ada", "pickup": "3pm"}]})
        with self.assertRaises(ValueError):
            coverage.validate_routine({"caregivers": [], "kids": [
                {"name": "Ada", "pickup": "14:55", "cover": {"mon": "Stranger"}}]})
        with self.assertRaises(ValueError):
            coverage.validate_routine({"kids": [{"name": "Ada", "pickup": "14:55",
                                                 "school_ics": "file:///etc/passwd"}]})


class CancellationTests(CoverageCase):
    def test_named_caregiver_with_a_date_opens_one_gap_for_her_kids(self):
        coverage.consume([message("Heads up, Pat can't come tomorrow.")])
        gaps = self.open_gaps()
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["date"], "2030-09-11")
        self.assertEqual(gap["who"], "Pat")
        # Wednesday: Pat covers Ada; Robin covers Bea, who is not in the gap.
        self.assertEqual([p["kid"] for p in gap["pickups"]], ["Ada"])
        self.assertEqual(gap["pickups"][0]["start"], "2030-09-11T14:55:00-04:00")
        self.assertNotIn("Pat", gap["options"])
        self.assertEqual(gap["options"][0], "me")

    def test_undated_note_from_the_caregiver_means_her_next_pickup(self):
        coverage.consume([message("So sorry, I can't make it", handle="+15555550101", name="")])
        gap = self.open_gaps()[0]
        self.assertEqual(gap["date"], "2030-09-10")
        self.assertEqual(sorted(p["kid"] for p in gap["pickups"]), ["Ada", "Bea"])

    def test_after_todays_pickups_an_undated_cancellation_moves_to_the_next_day(self):
        late = dt.datetime(2030, 9, 10, 23, tzinfo=dt.timezone.utc)  # 19:00 local
        coverage.consume([message("Pat is out sick", when=late)])
        self.assertEqual(self.open_gaps()[0]["date"], "2030-09-11")

    def test_questions_and_unrelated_messages_open_nothing(self):
        coverage.consume([message("Pat can't come tomorrow?", rowid=1),
                          message("Pat can't wait for the game!", rowid=2),
                          message("Robin can't come thursday", rowid=3)])
        # Robin does not cover Ada on Thursday but does cover Bea.
        gaps = self.open_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["who"], "Robin")
        self.assertEqual([p["kid"] for p in gaps[0]["pickups"]], ["Bea"])

    def test_the_same_cancellation_twice_asks_once(self):
        coverage.consume([message("Pat can't come tomorrow", rowid=1)])
        coverage.consume([message("Pat can't come tomorrow", rowid=1)])
        coverage.consume([message("Reminder: Pat can't come tomorrow", rowid=2)])
        self.assertEqual(len(self.open_gaps()), 1)

    def test_watching_off_reads_nothing(self):
        self.cfg["coverage_enabled"] = False
        coverage.consume([message("Pat can't come tomorrow")])
        self.assertEqual(self.open_gaps(), [])


class SchoolCalendarTests(CoverageCase):
    def test_parse_ics_reads_dates_and_times(self):
        events = coverage.parse_ics(ICS)
        self.assertEqual([e["uid"] for e in events], ["half-1", "closed-1", "closed-2", "fair"])
        self.assertEqual(events[0]["start"], dt.date(2030, 9, 11))
        self.assertTrue(events[0]["all_day"])
        self.assertEqual(events[3]["start"].tzinfo, dt.timezone.utc)

    def test_half_days_and_uncovered_closures_become_gaps(self):
        coverage.tick()
        gaps = {(g["date"], g["cause"]): g for g in self.open_gaps()}
        half = gaps[("2030-09-11", "half_day")]
        self.assertEqual(half["pickups"][0]["time"], "12:00")
        self.assertEqual(half["pickups"][0]["usual"], "Pat")
        # Friday: nobody covers Ada, and the school is closed.
        closed = gaps[("2030-09-13", "closure")]
        self.assertEqual([p["kid"] for p in closed["pickups"]], ["Ada"])
        self.assertEqual(closed["pickups"][0]["start"], "2030-09-13T08:00:00-04:00")
        self.assertTrue(closed["question"].startswith("Who has Ada on Friday Sep 13?"), closed["question"])
        # Monday's closure falls on a day Pat covers; the science fair is no gap.
        self.assertEqual(set(gaps), {("2030-09-11", "half_day"), ("2030-09-13", "closure")})

    def test_a_feed_that_cannot_be_read_is_named_not_cached(self):
        coverage._fetch_ics.side_effect = OSError("offline")
        result = coverage.tick()
        self.assertIn("school feed unreadable", result["errors"][0])
        coverage._fetch_ics.side_effect = None
        coverage._fetch_ics.return_value = ICS
        coverage.tick()
        self.assertEqual(len(self.open_gaps()), 2)


class SettleTests(CoverageCase):
    def test_a_gap_gets_a_block_and_one_question(self):
        coverage.consume([message("Pat can't come tomorrow")])
        coverage._fetch_ics.return_value = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        coverage.tick()
        coverage.tick()
        gap = self.open_gaps()[0]
        self.assertEqual(len(self.pings), 1)
        self.assertIn("Who's taking Ada at 2:55pm on Wednesday", self.pings[0][1])
        draft = plans.get(gap["calendar"]["Ada"])
        self.assertEqual(draft["status"], "created")
        self.assertEqual(draft["lane"], "family")
        self.assertEqual(draft["title"], "Pickup: Ada 2:55pm (Pat can't come)")
        self.assertEqual(draft["attendees"], [])
        self.assertEqual(self.create.call_count, 1)
        self.assertIn("your standing routine", plans._description(draft))

    def test_blocks_off_keeps_a_suggestion(self):
        self.cfg["coverage_calendar_blocks"] = False
        coverage.consume([message("Pat can't come tomorrow")])
        coverage._fetch_ics.return_value = ""
        coverage.tick()
        gap = self.open_gaps()[0]
        draft = plans.get(gap["calendar"]["Ada"])
        self.assertEqual(draft["status"], "blocked")
        self.assertIn("pickup blocks are off", draft["reason"])
        self.assertIn("pickup blocks are off", gap["calendar_note"])
        self.create.assert_not_called()

    def test_answering_records_the_taker_and_closes_the_card(self):
        coverage.consume([message("Pat can't come tomorrow")])
        gap = self.open_gaps()[0]
        with self.assertRaises(ValueError):
            coverage.answer(gap["id"], "Pat")
        done = coverage.answer(gap["id"], "robin")
        self.assertEqual((done["status"], done["taker"]), ("answered", "Robin"))
        self.assertEqual(self.open_gaps(), [])
        with self.assertRaises(ValueError):
            coverage.answer(gap["id"], "me")
        day = next(d for d in coverage.week() if d["date"] == "2030-09-11")
        ada = next(p for p in day["pickups"] if p["kid"] == "Ada")
        self.assertEqual((ada["state"], ada["who"]), ("covered", "Robin"))

    def test_not_needed_withdraws_an_unwritten_block(self):
        self.cfg["coverage_calendar_blocks"] = False
        coverage.consume([message("Pat can't come tomorrow")])
        coverage._fetch_ics.return_value = ""
        coverage.tick()
        gap = self.open_gaps()[0]
        coverage.answer(gap["id"], "not needed")
        self.assertEqual(plans.get(gap["calendar"]["Ada"])["status"], "dismissed")

    def test_past_gaps_leave_the_queue(self):
        coverage.consume([message("Pat can't come tomorrow")])
        coverage._fetch_ics.return_value = ""
        coverage.tick(now=NOW + dt.timedelta(days=3))
        self.assertEqual(self.open_gaps(), [])


class ReviewQueueTests(CoverageCase):
    def test_the_gap_is_a_decide_card_answered_by_name(self):
        coverage.consume([message("Pat can't come tomorrow")])
        rows = reviewqueue.SOURCES["coverage"].read()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["actions"], ["me", "Robin", "not needed"])
        self.assertEqual(row["labels"]["me"], "I'll take it")
        self.assertEqual(row["open"], "#coverage")
        out = reviewqueue.act(row["id"], "Robin")
        self.assertEqual(out["gap"]["taker"], "Robin")
        with self.assertRaises(ValueError):
            reviewqueue.act(row["id"], "me")


if __name__ == "__main__":
    unittest.main()

"""Appointments (server/appointments.py): reminder texts and mail become one
calendar entry per appointment, with a travel buffer, in Today/Tomorrow.

Covers the deterministic parser (short-code SMS, booking-site mail, arrival
times, ranges, solicitations), the ledger's de-duplication of repeat
reminders and its cancellation/reschedule handling, the brief merge (a found
row plus a travel row, or an annotated calendar twin, and a meeting inside
the travel time flagged as a conflict), the join into brief._calendar so a
day with an appointment is never empty, and the calendar promotion through
calendarplan (one draft per appointment, the switch, a cancellation after
staging). Every source is patched: no chat.db, Calendar store or config.

Run: .venv/bin/python -m unittest tests.test_appointments
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import appointments, brief
from server import calendarplan as plans

# Fixed local dates, clear of any daylight-saving change; the machine's own
# zone is used throughout (no assistant_timezone), exactly as the brief does.
BASE = dt.datetime(2030, 10, 8, 9, 0).astimezone()          # message received
NOW = BASE.astimezone(dt.timezone.utc)


def _local(day, hour, minute=0):
    return dt.datetime(2030, 10, day, hour, minute).astimezone()


def _item(key, text, when=BASE, handle="29900", sender="Short code 29900", channel="imessage",
          subject=None):
    return {"key": key, "channel": channel, "handle": handle, "sender": sender,
            "subject": subject, "text": text, "when": when.isoformat()}


SMS_3DAY = ("Reminder: you have an appointment with Dr. Jane Roe at Example Medical Group "
            "on 10/10 at 2:30 PM. Reply C to confirm or call 212-555-0100 to cancel.")
SMS_1DAY = ("Example Medical Group: your appt with Dr. Roe is tomorrow, 10/10 at 2:30 PM. "
            "Location: 100 Example St, Suite 4.")
MAIL = ("Your appointment is confirmed. Dr. Jane Roe, Thursday, October 10 at 2:30 PM, "
        "100 Example St Suite 4. Manage it on the example booking site.")


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cfg = {"assistant_enabled": True, "assistant_calendar_name": "Personal test",
                    "appointment_calendar_entries": True, "appointment_minutes": 60,
                    "appointment_travel_minutes": 30}
        for p in (mock.patch.object(appointments, "STORE", self.dir / "appointments.json"),
                  mock.patch.object(appointments, "_now", return_value=NOW),
                  mock.patch.object(appointments.settings, "raw", side_effect=lambda: dict(self.cfg)),
                  mock.patch.object(appointments.settings, "fixture_mode", return_value=False),
                  mock.patch.object(appointments.settings, "sandboxed", return_value=False),
                  mock.patch.dict(appointments._last_refresh, {"at": 0.0, "status": None})):
            p.start()
            self.addCleanup(p.stop)

    def feed(self, *items):
        got = {"items": list(items), "status": {"imessage": "ok", "mail": "ok"}}
        with mock.patch.object(appointments.admininbox, "collect", return_value=got) as collect:
            out = appointments.scan()
        collect.assert_called_once_with(None, include_hidden=True)
        return out


class Parse(Fixture):
    def test_short_code_reminder(self):
        p = appointments.parse(SMS_3DAY, BASE.isoformat(), "Short code 29900")
        self.assertEqual(p["kind"], "booked")
        self.assertEqual(p["start"], _local(10, 14, 30))
        self.assertIsNone(p["end"])
        self.assertEqual(p["doctor"], "Dr. Jane Roe")
        self.assertEqual(p["org"], "Example Medical Group")
        self.assertIn(p["quote"], SMS_3DAY)
        self.assertIn("10/10 at 2:30 PM", p["quote"])
        self.assertFalse(p["virtual"])

    def test_booking_site_mail_with_month_name_and_no_year(self):
        p = appointments.parse(MAIL, BASE.isoformat(), "Example Booking")
        self.assertEqual(p["start"], _local(10, 14, 30))
        self.assertEqual(p["location"], "100 Example St Suite 4")
        self.assertEqual(p["org"], "Example Booking")

    def test_arrival_time_yields_to_the_visit_time(self):
        p = appointments.parse("Please arrive by 2:15 PM for your 2:30 PM appointment tomorrow.",
                               BASE.isoformat())
        self.assertEqual(p["start"], _local(9, 14, 30))
        p = appointments.parse("Reminder: arrive 15 minutes early for your 2:30 PM visit on 10/9.",
                               BASE.isoformat())
        self.assertEqual(p["start"], _local(9, 14, 30))

    def test_stated_range_gives_the_end(self):
        p = appointments.parse("Your cleaning is scheduled 10/9, 2:30 - 3:15 PM.", BASE.isoformat())
        self.assertEqual((p["start"], p["end"]), (_local(9, 14, 30), _local(9, 15, 15)))
        self.assertTrue(p["end_stated"])

    def test_solicitation_is_not_an_appointment(self):
        self.assertIsNone(appointments.parse(
            "Time to book your annual exam! Call us Mon-Fri 9am-5pm.", BASE.isoformat()))

    def test_no_time_or_no_appointment_word(self):
        self.assertIsNone(appointments.parse("Your appointment is on 10/10.", BASE.isoformat()))
        self.assertIsNone(appointments.parse("Your order ships 10/10 at 2 PM.", BASE.isoformat()))

    def test_spent_appointment_is_ignored(self):
        self.assertIsNone(appointments.parse(
            "Thanks for your visit on 10/2 at 3 PM. Rate us!", BASE.isoformat()))

    def test_video_visit_has_no_travel_buffer(self):
        p = appointments.parse("Your video visit with Dr. Roe is tomorrow at 9:00 AM.", BASE.isoformat())
        self.assertTrue(p["virtual"])
        self.assertEqual(appointments.travel_minutes(p), 0)
        self.assertEqual(appointments.travel_minutes({"virtual": False}), 30)

    def test_self_named_service_credentialed_clinician_and_stated_length(self):
        text = ("Hi, it's Example Care. You have an appointment with JANE Q ROE, MD on Thursday, "
                "October 10, 2030, at 3:30 PM. It may last up to 45 minutes.")
        p = appointments.parse(text, BASE.isoformat(), "Short code 29900")
        self.assertEqual((p["org"], p["doctor"]), ("Example Care", "Jane Q Roe, MD"))
        self.assertEqual((p["start"], p["end"]), (_local(10, 15, 30), _local(10, 16, 15)))
        self.assertEqual(appointments.title_for(p), "Appointment: Jane Q Roe, MD (Example Care)")
        p = appointments.parse("Example Care here. See you tomorrow at 3:30 PM; we will send your "
                               "visit link an hour before.", BASE.isoformat(), "Short code 29900")
        self.assertEqual(p["org"], "Example Care")
        self.assertTrue(p["virtual"])

    def test_cancellation_without_a_time(self):
        p = appointments.parse("Your appointment has been cancelled. Call to rebook.", BASE.isoformat())
        self.assertEqual(p, {"kind": "cancelled", "start": None})


class Ledger(Fixture):
    def test_repeat_reminders_and_mail_copy_are_one_appointment(self):
        out = self.feed(_item("a1", SMS_3DAY, when=BASE - dt.timedelta(days=2)),
                        _item("a2", SMS_1DAY),
                        _item("a3", MAIL, handle="no-reply@example.com", sender="Example Booking",
                              channel="email", subject="Appointment confirmed"))
        self.assertEqual(out["added"], 1)
        rows = appointments.upcoming()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual([s["key"] for s in row["sources"]], ["a1", "a2", "a3"])
        self.assertEqual(row["title"], "Appointment: Dr. Jane Roe (Example Medical Group)")
        self.assertEqual(row["location"], "100 Example St, Suite 4")
        self.assertEqual(dt.datetime.fromisoformat(row["end"]), _local(10, 15, 30))
        # A second scan over the same messages changes nothing.
        self.assertEqual(self.feed(_item("a1", SMS_3DAY), _item("a2", SMS_1DAY))["added"], 0)
        self.assertEqual(len(appointments.upcoming()[0]["sources"]), 3)

    def test_cancellation_retires_and_a_newer_confirmation_rearms(self):
        self.feed(_item("a1", SMS_3DAY, when=BASE - dt.timedelta(days=1)),
                  _item("c1", "Your appointment on 10/10 at 2:30 PM has been cancelled."))
        self.assertEqual(appointments.upcoming(), [])
        self.assertEqual(appointments.upcoming(include_closed=True)[0]["status"], "cancelled")
        self.feed(_item("a2", SMS_1DAY, when=BASE + dt.timedelta(hours=2)))
        self.assertEqual(len(appointments.upcoming()), 1)

    def test_timeless_cancellation_retires_the_senders_one_appointment(self):
        self.feed(_item("a1", SMS_3DAY, when=BASE - dt.timedelta(days=1)),
                  _item("c1", "Your appointment has been cancelled."))
        self.assertEqual(appointments.upcoming(), [])

    def test_reschedule_supersedes_the_earlier_time(self):
        self.feed(_item("a1", SMS_3DAY, when=BASE - dt.timedelta(days=1)),
                  _item("r1", "Your appointment has been rescheduled to 10/11 at 9:00 AM."))
        rows = appointments.upcoming()
        self.assertEqual([dt.datetime.fromisoformat(r["start"]) for r in rows], [_local(11, 9)])
        old = [r for r in appointments.upcoming(include_closed=True) if r["status"] == "superseded"]
        self.assertEqual(len(old), 1)

    def test_dismiss(self):
        self.feed(_item("a1", SMS_3DAY))
        appt = appointments.upcoming()[0]
        appointments.dismiss(appt["id"])
        self.assertEqual(appointments.upcoming(), [])
        self.assertEqual(appointments.merge([], _local(10, 0).replace(tzinfo=None),
                                            _local(11, 0).replace(tzinfo=None)), [])


def _event(title, start, end, **extra):
    return dict({"title": title, "calendar": "Home", "family": False, "birthday": False,
                 "all_day": False, "start": start.replace(tzinfo=None).isoformat(),
                 "end": end.replace(tzinfo=None).isoformat(), "start_hm": "", "end_hm": "",
                 "conflict": False, "remote": False}, **extra)


DAY = (_local(10, 0).replace(tzinfo=None), _local(11, 0).replace(tzinfo=None))


class Merge(Fixture):
    def setUp(self):
        super().setUp()
        self.feed(_item("a1", SMS_3DAY))

    def test_found_row_and_travel_row(self):
        rows = appointments.merge([], *DAY)
        self.assertEqual([r["title"] for r in rows], [
            "Travel to Dr. Jane Roe (Example Medical Group)",
            "Appointment: Dr. Jane Roe (Example Medical Group)"])
        travel, appt = rows
        self.assertTrue(travel["travel"])
        self.assertEqual(travel["start"], _local(10, 14).replace(tzinfo=None).isoformat())
        self.assertTrue(appt["appointment_found"])
        self.assertEqual(appt["leave_by_hm"], "2:00 PM")
        self.assertEqual(appt["busy_end"], _local(10, 16).replace(tzinfo=None).isoformat())

    def test_calendar_twin_is_annotated_not_duplicated(self):
        twin = _event("Dr Roe", _local(10, 14, 30), _local(10, 15, 30))
        rows = appointments.merge([twin], *DAY)
        appts = [r for r in rows if not r.get("travel")]
        self.assertEqual(len(appts), 1)
        self.assertTrue(appts[0]["appointment"])
        self.assertNotIn("appointment_found", appts[0])
        self.assertEqual(appts[0]["leave_by_hm"], "2:00 PM")

    def test_unrelated_event_at_the_same_time_is_not_a_twin(self):
        other = _event("Quarterly planning", _local(10, 14, 30), _local(10, 15, 30))
        rows = appointments.merge([other], *DAY)
        self.assertTrue(any(r.get("appointment_found") for r in rows))

    def test_meeting_in_the_travel_time_is_a_conflict(self):
        meeting = _event("Team sync", _local(10, 13, 45), _local(10, 14, 15))
        rows = brief._flag_conflicts(appointments.merge([meeting], *DAY))
        by_title = {r["title"]: r for r in rows}
        self.assertTrue(by_title["Team sync"]["conflict"])
        self.assertTrue(by_title["Appointment: Dr. Jane Roe (Example Medical Group)"]["conflict"])
        self.assertFalse(by_title["Travel to Dr. Jane Roe (Example Medical Group)"]["conflict"])

    def test_cancelled_appointment_tags_its_calendar_twin(self):
        self.feed(_item("c1", "Your appointment on 10/10 at 2:30 PM has been cancelled.",
                        when=BASE + dt.timedelta(hours=1)))
        twin = _event("Appointment: Dr. Jane Roe (Example Medical Group)",
                      _local(10, 14, 30), _local(10, 15, 30))
        rows = appointments.merge([twin], *DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["appointment_status"], "cancelled")


class BriefJoin(Fixture):
    """The idea's acceptance test: the brief never says nothing scheduled on
    a day whose only appointment lives in a text."""

    def test_tomorrow_shows_the_appointment(self):
        self.feed(_item("a1", SMS_3DAY))
        bounds = {0: (_local(9, 0).replace(tzinfo=None), DAY[0]), 1: DAY}
        with mock.patch.object(brief, "_day_bounds", side_effect=lambda o: bounds.get(o, DAY)), \
                mock.patch.object(brief, "_occurrences", return_value=[]), \
                mock.patch.object(brief, "_m365_events",
                                  return_value={"today": [], "tomorrow": [], "status": None}), \
                mock.patch.object(appointments, "refresh", return_value="ok"):
            cal = brief._calendar()
        self.assertEqual(cal["today"], [])
        titles = [e["title"] for e in cal["tomorrow"]]
        self.assertIn("Appointment: Dr. Jane Roe (Example Medical Group)", titles)
        self.assertEqual(cal["appointments"], "ok")


class Promotion(Fixture):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "calendar-plans.json"
        for p in (mock.patch.object(plans, "STORE", self.path),
                  mock.patch.object(plans, "_now", return_value=NOW),
                  mock.patch.object(plans.settings, "IS_MAC", True),
                  mock.patch.object(plans, "_destination_cache", {"at": 0, "value": None}),
                  mock.patch.object(plans, "_metadata_native", return_value={"calendars": [
                      {"id": "calendar-test", "native_id": "calendar-test", "name": "Personal test",
                       "writable": True, "is_default": True}], "default_id": "calendar-test"}),
                  mock.patch.object(appointments, "_on_calendar", return_value=False)):
            p.start()
            self.addCleanup(p.stop)
        self.create = mock.patch.object(plans, "_calendar_create", return_value={"uid": "evt-1"}).start()
        self.addCleanup(mock.patch.stopall)
        self.feed(_item("a1", SMS_3DAY, when=BASE - dt.timedelta(days=1)), _item("a2", SMS_1DAY))

    def tick(self):
        with mock.patch.object(appointments.admininbox, "collect",
                               return_value={"items": [], "status": {}}):
            return appointments.tick()

    def test_one_entry_for_repeat_reminders_with_travel_note(self):
        out = self.tick()
        self.assertEqual(out["created"], 1)
        self.create.assert_called_once()
        draft = self.create.call_args[0][0]
        self.assertEqual(draft["schedule_kind"], "appointment")
        self.assertEqual(draft["attendees"], [])
        self.assertEqual(dt.datetime.fromisoformat(draft["start"]), _local(10, 14, 30))
        description = plans._description(draft)
        self.assertIn("Leave by 2:00 PM (30-minute travel buffer)", description)
        self.assertIn("2 reminders for this appointment were merged", description)
        self.assertIn("no end time", description)
        # The next pass writes nothing again.
        self.assertEqual(self.tick()["created"], 0)
        self.create.assert_called_once()

    def test_switch_off_keeps_a_suggestion(self):
        self.cfg["appointment_calendar_entries"] = False
        out = self.tick()
        self.assertEqual(out["created"], 0)
        self.create.assert_not_called()
        drafts = plans.list_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "blocked")
        self.assertIn("Automatic appointment entries are off", drafts[0]["reason"])
        # An explicit owner click still creates it.
        self.assertEqual(plans.create_owner_event(drafts[0]["id"], automatic=False)["status"], "created")

    def test_cancelled_after_staging_is_never_written(self):
        self.cfg["appointment_calendar_entries"] = False
        self.tick()
        draft_id = plans.list_drafts()[0]["id"]
        self.feed(_item("c1", "Your appointment on 10/10 at 2:30 PM has been cancelled.",
                        when=BASE + dt.timedelta(hours=1)))
        out = plans.create_owner_event(draft_id, automatic=False)
        self.assertEqual(out["status"], "blocked")
        self.assertIn("cancelled", out["reason"])
        self.create.assert_not_called()

    def test_already_on_the_calendar_stages_nothing(self):
        with mock.patch.object(appointments, "_on_calendar", return_value=True):
            out = self.tick()
        self.assertEqual(out["staged"], 0)
        self.assertEqual(plans.list_drafts(include_closed=True), [])

    def test_travel_buffer_is_busy_for_work_block_planning(self):
        # A 30-minute block wanted at 13:55 cannot sit in the 14:00 travel
        # time, the 14:30-15:30 visit, or the 15:30-16:00 trip back.
        self.cfg["appointment_calendar_entries"] = False
        self.tick()
        later = _local(10, 13, 50).astimezone(dt.timezone.utc)
        with mock.patch.object(plans, "_now", return_value=later), \
                mock.patch.object(plans, "_calendar_busy", return_value=[]):
            slot = plans._free_slot(_local(10, 23))
        self.assertEqual(slot[0], _local(10, 16))


if __name__ == "__main__":
    unittest.main()

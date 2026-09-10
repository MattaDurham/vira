"""Calendar drafts and the attendee-free write boundary, using synthetic data.

Every store is temporary. The OS adapter is replaced before exercising the
writer; the JavaScript contract runs in Node with a fake Calendar object.
No test reads or writes the machine's real calendars or CRM records.
"""
import copy
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from server import calendarplan as plans


NOW = dt.datetime(2030, 9, 10, 12, tzinfo=dt.timezone.utc)
SOURCE = {
    "id": "message-1", "channel": "imessage", "when": NOW.isoformat(),
    "is_from_me": True,
    "text": "Block my calendar for writing the report on 2030-09-11 from 10:00 to 11:00.",
}
PROPOSAL = {
    "title": "Write the report", "quote": SOURCE["text"],
    "time_quote": "2030-09-11 from 10:00 to 11:00",
    "owner_only_quote": "Block my calendar", "owner_only": True,
    "start": "2030-09-11T10:00:00+00:00", "end": "2030-09-11T11:00:00+00:00",
    "attendees": [], "description": "Time to write the report.", "location": "Home",
}
LOOP = {
    "assistant_key": "task-report", "what": "Send the report", "owed_by": "me",
    "source": "vira-assistant", "status": "open", "due": "2030-09-12",
    "due_quote": "by 2030-09-12", "evidence": [{"id": "mail-1", "channel": "email",
        "when": NOW.isoformat(), "quote": "Please send the report by 2030-09-12."}],
}


class CalendarFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "calendar-plans.json"
        self.cfg = {"assistant_enabled": True, "assistant_calendar_auto_create": True,
                    "assistant_calendar_name": "Personal test", "assistant_timezone": "UTC"}
        for patcher in (
                mock.patch.object(plans, "STORE", self.path),
                mock.patch.object(plans, "_now", return_value=NOW),
                mock.patch.object(plans.settings, "raw", side_effect=lambda: self.cfg.copy()),
                mock.patch.object(plans.settings, "sandboxed", return_value=False),
                mock.patch.object(plans.settings, "fixture_mode", return_value=False),
                mock.patch.object(plans.settings, "IS_MAC", True),
                mock.patch.object(plans, "_destination_cache", {"at": 0, "value": None}),
                mock.patch.object(plans, "_metadata_native", return_value={"calendars": [
                    {"id": "calendar-test", "native_id": "calendar-test", "name": "Personal test", "writable": True, "is_default": True},
                    {"id": "calendar-two", "native_id": "calendar-two", "name": "Writable calendar", "writable": True, "is_default": False},
                ], "default_id": "calendar-test"}),
                mock.patch.dict(os.environ, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.adapter = mock.patch.object(plans, "_calendar_create", return_value={"uid": "test-event-1"})
        self.create = self.adapter.start()
        self.addCleanup(self.adapter.stop)
        self.busy_patch = mock.patch.object(plans, "_calendar_busy", return_value=[])
        self.busy = self.busy_patch.start()
        self.addCleanup(self.busy_patch.stop)
        patcher = mock.patch.object(plans, "_current_commitment", return_value=copy.deepcopy(LOOP))
        self.real_current_commitment = patcher.get_original()[0]
        self.current = patcher.start()
        self.addCleanup(patcher.stop)

    def stage(self, proposal=None, source=None):
        return plans.stage(copy.deepcopy(proposal or PROPOSAL), copy.deepcopy(source or SOURCE))

    def mutate_draft(self, draft_id, **changes):
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["drafts"][draft_id].update(changes)
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def assert_refused(self, proposal=None, source=None, reason=None, automatic=True):
        draft = self.stage(proposal, source)
        out = plans.create_owner_event(draft["id"], automatic=automatic)
        self.assertEqual(out["status"], "blocked")
        self.create.assert_not_called()
        if reason:
            self.assertIn(reason, out["reason"])
        return out


class Drafts(CalendarFixture):
    def test_stage_is_offline_evidence_linked_and_does_not_create(self):
        draft = self.stage()
        self.assertEqual(draft["status"], "suggested")
        self.assertEqual(draft["source"]["id"], SOURCE["id"])
        self.assertEqual(draft["quote"], SOURCE["text"])
        self.assertEqual(plans.get(draft["id"]), draft)
        self.assertEqual(len(plans.list_drafts()), 1)
        self.assertTrue(draft["can_create"])
        self.create.assert_not_called()
        # Store the full trusted context, but return only the visible quote.
        self.assertNotIn("text", draft["source"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))[
            "drafts"][draft["id"]]["source"]["text"], SOURCE["text"])

    def test_each_claimed_quote_must_appear_in_actual_input(self):
        for field in ("quote", "time_quote", "owner_only_quote"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "actual source"):
                self.stage(dict(PROPOSAL, **{field: "invented words from a model"}))
        self.assertFalse(self.path.exists())

    def test_reextraction_with_new_title_and_quote_cannot_duplicate_event(self):
        draft = self.stage()
        plans.create_owner_event(draft["id"])
        again = self.stage(dict(PROPOSAL, title="Report time", quote=SOURCE["text"][6:]))
        self.assertEqual(draft["id"], again["id"])
        self.assertEqual(again["status"], "created")
        plans.create_owner_event(again["id"])
        self.create.assert_called_once()

    def test_changed_model_dates_cannot_replay_a_source_request_after_uncertain_write(self):
        self.create.side_effect = RuntimeError("result lost")
        draft = self.stage()
        plans.create_owner_event(draft["id"])
        again = self.stage(dict(PROPOSAL, start="2030-09-11T10:00-04:00", end="2030-09-11T11:00-04:00"))
        self.assertEqual(again["id"], draft["id"])
        self.assertEqual(plans.create_owner_event(again["id"])["status"], "uncertain")
        self.create.assert_called_once()

    def test_incomplete_suggestion_survives_for_review_without_inventing_duration(self):
        draft = self.stage(dict(PROPOSAL, end=""))
        self.assertFalse(draft["can_export"])
        self.assertFalse(draft["can_create"])
        self.assertEqual(draft["end"], "")
        with self.assertRaises(ValueError):
            plans.ics(draft["id"])

    def test_invalid_time_shapes_and_attendee_shapes_are_rejected(self):
        for change in ({"start": "2030-09-11"}, {"start": "2030-09-11T10:00"},
                       {"start": "2030-09-11T10:00:30Z"}, {"end": PROPOSAL["start"]},
                       {"attendees": "Nobody"}, {"attendees": [""]},
                       {"owner_only": "true"}, {"title": "x\x00y"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.stage(dict(PROPOSAL, **change))
        with self.assertRaises(ValueError):
            self.stage(source=dict(SOURCE, id=None))

    def test_dismissal_is_durable_and_does_not_delete_an_event(self):
        draft = self.stage()
        self.assertEqual(plans.dismiss(draft["id"])["status"], "dismissed")
        self.assertEqual(plans.list_drafts(), [])
        self.assertEqual(len(plans.list_drafts(include_closed=True)), 1)
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "dismissed")
        self.create.assert_not_called()

    def test_missing_draft_never_calls_the_writer(self):
        self.assertIsNone(plans.get("missing"))
        for operation in (plans.dismiss, plans.create_owner_event, plans.ics):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation("missing")
        self.create.assert_not_called()


class DestinationDiscovery(CalendarFixture):
    def test_empty_configuration_uses_verified_system_default(self):
        self.cfg["assistant_calendar_name"] = ""
        found = plans.destinations()
        self.assertEqual(found["selection"], "system_default")
        self.assertEqual(found["selected"]["id"], "calendar-test")
        draft = self.stage()
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "created")
        self.assertEqual(self.create.call_args.kwargs["calendar_id"], "calendar-test")

    def test_explicit_id_wins_over_legacy_name_and_follows_calendar_rename(self):
        self.cfg["assistant_calendar_id"] = "calendar-two"
        value = copy.deepcopy(plans._metadata_native.return_value)
        value["calendars"][1]["name"] = "Renamed destination"
        plans._metadata_native.return_value = value
        found = plans.destinations()
        self.assertEqual(found["selection"], "configured_id")
        self.assertEqual(found["selected"]["name"], "Renamed destination")

    def test_missing_explicit_id_never_silently_switches_to_default(self):
        self.cfg["assistant_calendar_id"] = "missing"
        found = plans.destinations()
        self.assertIsNone(found["selected"])
        self.assertIn("missing", found["reason"])

    def test_sole_writable_fallback_is_distinguished_from_verified_default(self):
        self.cfg["assistant_calendar_name"] = ""
        plans._metadata_native.return_value = {"calendars": [{"id": "only", "native_id": "only",
            "name": "A calendar", "writable": True, "is_default": False}], "default_id": ""}
        found = plans.destinations()
        self.assertEqual(found["selection"], "only_writable")
        self.assertFalse(found["selected"]["is_default"])

    def test_no_name_heuristic_when_multiple_writable_calendars_have_no_default(self):
        self.cfg["assistant_calendar_name"] = ""
        plans._metadata_native.return_value = {"calendars": [
            {"id": "home", "native_id": "home", "name": "Home", "writable": True, "is_default": False},
            {"id": "work", "native_id": "work", "name": "Work", "writable": True, "is_default": False}], "default_id": ""}
        found = plans.destinations()
        self.assertIsNone(found["selected"])
        self.assertIn("could not be verified", found["reason"])

    def test_duplicate_names_need_explicit_native_identifier(self):
        value = copy.deepcopy(plans._metadata_native.return_value)
        value["calendars"][1]["name"] = "Personal test"
        plans._metadata_native.return_value = value
        self.assertIsNone(plans.destinations()["selected"])
        self.cfg["assistant_calendar_id"] = "calendar-two"
        self.assertEqual(plans.destinations()["selected"]["id"], "calendar-two")

    def test_readonly_selection_cannot_fall_back_to_another_writable_calendar(self):
        value = copy.deepcopy(plans._metadata_native.return_value)
        value["calendars"][0]["writable"] = False
        plans._metadata_native.return_value = value
        found = plans.destinations()
        self.assertIsNone(found["selected"])
        self.assertIn("read-only", found["reason"])

    def test_preview_gates_never_read_or_expose_cached_native_metadata(self):
        plans.destinations()
        self.assertEqual(plans._metadata_native.call_count, 1)
        for gate in (mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}),
                     mock.patch.object(plans.settings, "fixture_mode", return_value=True),
                     mock.patch.object(plans.settings, "sandboxed", return_value=True)):
            with gate:
                found = plans.destinations(refresh=True)
                self.assertFalse(found["available"])
                self.assertEqual(found["calendars"], [])
        self.assertEqual(plans._metadata_native.call_count, 1)

    def test_cache_is_refreshable_and_metadata_failure_is_visible(self):
        plans.destinations()
        plans.destinations()
        self.assertEqual(plans._metadata_native.call_count, 1)
        plans._metadata_native.side_effect = RuntimeError("Calendar access unavailable")
        found = plans.destinations(refresh=True)
        self.assertFalse(found["available"])
        self.assertIsNone(found["selected"])
        self.assertIn("Calendar access unavailable", found["error"])

    def test_destination_is_revalidated_immediately_before_creation(self):
        draft = self.stage()
        plans._metadata_native.return_value = {"calendars": [], "default_id": ""}
        out = plans.create_owner_event(draft["id"])
        self.assertEqual(out["status"], "blocked")
        self.create.assert_not_called()


class CreationBoundary(CalendarFixture):
    def test_eligible_owner_request_creates_one_event_on_configured_calendar(self):
        draft = self.stage()
        out = plans.create_owner_event(draft["id"])
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["event_uid"], "test-event-1")
        self.assertEqual(out["event_calendar"], self.cfg["assistant_calendar_name"])
        self.assertFalse(out["can_create"])
        self.assertEqual(self.create.call_args.args[1], "Personal test")
        self.assertEqual(self.create.call_args.args[0]["attendees"], [])
        self.assertEqual(plans.list_drafts(), [])
        with self.assertRaisesRegex(ValueError, "already exists"):
            plans.dismiss(draft["id"])

    def test_explicit_click_bypasses_only_the_automatic_toggle(self):
        self.cfg["assistant_calendar_auto_create"] = False
        out = self.assert_refused(reason="Automatic")
        self.assertTrue(out["can_create"])
        self.assertEqual(plans.create_owner_event(out["id"], automatic=False)["status"], "created")
        self.create.assert_called_once()

    def test_turning_assistant_off_after_staging_prevents_even_explicit_creation(self):
        draft = self.stage()
        self.cfg["assistant_enabled"] = False
        out = plans.create_owner_event(draft["id"], automatic=False)
        self.assertEqual(out["status"], "blocked")
        self.create.assert_not_called()

    def test_passive_sandbox_and_fixture_refuse_all_writes(self):
        for gate in (mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}),
                     mock.patch.object(plans.settings, "sandboxed", return_value=True),
                     mock.patch.object(plans.settings, "fixture_mode", return_value=True)):
            with gate:
                self.assert_refused(automatic=False)

    def test_platform_and_calendar_selection_must_be_ready(self):
        with mock.patch.object(plans.settings, "IS_MAC", False):
            self.assert_refused(reason="macOS")
        self.cfg["assistant_calendar_id"] = "missing-destination"
        self.assert_refused(reason="missing or ambiguous")

    def test_other_people_never_receive_calendar_invitations(self):
        self.assert_refused(dict(PROPOSAL, attendees=["person@example.com"]), reason="other people")

    def test_inbound_text_cannot_authorize_an_owner_event(self):
        self.assert_refused(source=dict(SOURCE, is_from_me=False), reason="owner's own")

    def test_unclaimed_personal_event_stays_a_suggestion(self):
        self.assert_refused(dict(PROPOSAL, owner_only=False), reason="other people")

    def test_quote_cannot_hide_negation_reported_speech_or_company(self):
        for prefix in ("Do not ", "Don't ", "Maybe ", "If possible, ", "Someone said: ",
                       "My partner asked me to ", "We could ", '"'):
            text = prefix + SOURCE["text"]
            source = dict(SOURCE, id="case-" + prefix, text=text)
            proposal = dict(PROPOSAL, quote=SOURCE["text"])
            with self.subTest(prefix=prefix):
                self.assert_refused(proposal, source, reason="clear request")

    def test_different_appointment_in_source_cannot_supply_request_times(self):
        text = "Block my calendar for a break. The appointment is on 2030-09-11 from 10:00 to 11:00."
        self.assert_refused(dict(PROPOSAL, quote=text), dict(SOURCE, text=text), reason="same event")

    def test_time_quote_cannot_omit_a_conflicting_time_in_owner_request(self):
        text = SOURCE["text"][:-1] + " or from 12:00 to 13:00."
        self.assert_refused(dict(PROPOSAL, quote=text), dict(SOURCE, text=text), reason="source must state")


class Grounding(CalendarFixture):
    def test_model_cannot_change_date_time_or_order_of_the_source_range(self):
        for change in ({"start": "2030-09-12T10:00Z", "end": "2030-09-12T11:00Z"},
                       {"start": "2030-09-11T09:00Z"},
                       {"start": "2030-09-11T11:00Z", "end": "2030-09-12T10:00Z"}):
            with self.subTest(change=change):
                self.assert_refused(dict(PROPOSAL, **change), reason="do not match")

    def test_relative_date_uses_source_date_in_owner_zone_not_today(self):
        self.cfg["assistant_timezone"] = "America/New_York"
        # Source was sent at 23:30 on Sep 10 in the owner's zone.
        text = "Block my calendar tomorrow from 10am to 11am."
        source = dict(SOURCE, when="2030-09-11T03:30:00Z", text=text)
        proposal = dict(PROPOSAL, quote=text, time_quote="tomorrow from 10am to 11am",
                        start="2030-09-11T10:00:00-04:00", end="2030-09-11T11:00:00-04:00")
        self.assertTrue(self.stage(proposal, source)["can_create"])
        self.assert_refused(dict(proposal, start="2030-09-12T10:00-04:00", end="2030-09-12T11:00-04:00"),
                            source, reason="do not match")

    def test_relative_date_without_source_offset_is_not_guessed(self):
        text = "Block my calendar tomorrow from 10am to 11am."
        self.assert_refused(dict(PROPOSAL, quote=text, time_quote="tomorrow from 10am to 11am"),
                            dict(SOURCE, text=text, when="2030-09-10T12:00"), reason="source must state")

    def test_explicit_month_date_and_meridiem_are_supported(self):
        text = "Block my calendar on September 11, 2030 from 10am to 11am."
        self.assertTrue(self.stage(dict(PROPOSAL, quote=text, time_quote="September 11, 2030 from 10am to 11am"),
                                   dict(SOURCE, text=text))["can_create"])

    def test_ambiguous_clock_without_meridiem_and_unspecified_duration_stay_drafts(self):
        for timing in ("2030-09-11 at 10", "2030-09-11 from 10 to 11", "2030-09-11 at 10am"):
            text = "Block my calendar on " + timing + "."
            with self.subTest(timing=timing):
                self.assert_refused(dict(PROPOSAL, quote=text, time_quote=timing), dict(SOURCE, text=text),
                                    reason="source must state")

    def test_invented_utc_offset_is_refused(self):
        self.cfg["assistant_timezone"] = "America/New_York"
        self.assert_refused(reason="UTC offsets")

    def test_source_timezone_overrides_configured_zone(self):
        self.cfg["assistant_timezone"] = "America/New_York"
        for zone in ("UTC", "GMT", "UTC+00:00", "Etc/UTC"):
            text = SOURCE["text"][:-1] + " " + zone + "."
            with self.subTest(zone=zone):
                self.assertTrue(self.stage(dict(PROPOSAL, quote=text, time_quote=PROPOSAL["time_quote"] + " " + zone),
                                           dict(SOURCE, id=zone, text=text))["can_create"])

    def test_abbreviated_timezone_stays_a_suggestion(self):
        text = SOURCE["text"][:-1] + " IST."
        self.assert_refused(dict(PROPOSAL, quote=text, time_quote=PROPOSAL["time_quote"] + " IST"),
                            dict(SOURCE, text=text), reason="abbreviation")

    def test_dst_gap_and_repeated_clock_time_are_not_scheduled_from_a_guess(self):
        self.cfg["assistant_timezone"] = "America/New_York"
        for date, clocks, offset in (("2031-03-09", ("02:00", "03:00"), "-05:00"),
                                     ("2030-11-03", ("01:00", "01:30"), "-04:00")):
            timing = f"{date} from {clocks[0]} to {clocks[1]}"
            text = "Block my calendar on " + timing + "."
            proposal = dict(PROPOSAL, quote=text, time_quote=timing,
                            start=date + "T" + clocks[0] + offset, end=date + "T" + clocks[1] + offset)
            with self.subTest(date=date):
                self.assert_refused(proposal, dict(SOURCE, id=date, text=text))

    def test_past_event_is_never_created_on_replay(self):
        with mock.patch.object(plans, "_now", return_value=NOW + dt.timedelta(days=2)):
            self.assert_refused(reason="past")


class DurableWrites(CalendarFixture):
    def test_claim_is_durable_before_the_first_external_call(self):
        def create(draft, _calendar, **_options):
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(state["drafts"][draft["id"]]["status"], "creating")
            return {"uid": "saved-id"}
        self.create.side_effect = create
        out = plans.create_owner_event(self.stage()["id"])
        self.assertEqual(out["event_uid"], "saved-id")

    def test_timeout_is_uncertain_and_never_automatically_or_manually_replayed(self):
        self.create.side_effect = subprocess.TimeoutExpired("osascript", 45)
        draft = self.stage()
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "uncertain")
        self.assertEqual(plans.create_owner_event(draft["id"], automatic=False)["status"], "uncertain")
        self.assertFalse(plans.get(draft["id"])["can_create"])
        self.create.assert_called_once()

    def test_interrupted_claim_remains_visible_and_cannot_be_dismissed_or_replayed(self):
        draft = self.stage()
        self.mutate_draft(draft["id"], status="creating", reason="Inspect Calendar.app before any retry.")
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "creating")
        self.assertEqual(plans.list_drafts()[0]["status"], "creating")
        with self.assertRaisesRegex(ValueError, "in progress"):
            plans.dismiss(draft["id"])
        self.create.assert_not_called()

    def test_proven_prewrite_refusal_can_retry_after_calendar_is_corrected(self):
        self.create.side_effect = plans.CalendarRefused("Configured calendar is read-only.")
        draft = self.stage()
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "blocked")
        self.create.side_effect = None
        self.cfg["assistant_calendar_name"] = "Writable calendar"
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "created")
        self.assertEqual(self.create.call_count, 2)

    def test_unreadable_or_corrupt_ledger_is_not_an_empty_cache(self):
        for content in ("{", "[]", '{"drafts": []}', '{"drafts": {"cal_x": {"status": "created"}}}'):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                for operation in (lambda: self.stage(), plans.list_drafts,
                                  lambda: plans.create_owner_event("cal_x")):
                    with self.assertRaisesRegex(ValueError, "store|ledger"):
                        operation()
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)
        self.create.assert_not_called()

    def test_failed_claim_persistence_prevents_os_call(self):
        draft = self.stage()
        with mock.patch.object(plans.jsonstore, "write_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                plans.create_owner_event(draft["id"])
        self.assertEqual(plans.get(draft["id"])["status"], "suggested")
        self.create.assert_not_called()

    def test_failure_after_calendar_write_keeps_claim_and_prevents_duplicate(self):
        draft = self.stage()
        real_write = plans.jsonstore.write_atomic
        writes = 0
        def write(path, state, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("disk full after Calendar saved")
            return real_write(path, state, **kwargs)
        with mock.patch.object(plans.jsonstore, "write_atomic", side_effect=write):
            with self.assertRaises(OSError):
                plans.create_owner_event(draft["id"])
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "creating")
        self.create.assert_called_once()

    def test_concurrent_creation_uses_one_external_write(self):
        draft = self.stage()
        barrier = threading.Barrier(2)
        outputs = []
        errors = []
        def create():
            try:
                barrier.wait(timeout=5)
                outputs.append(plans.create_owner_event(draft["id"])["status"])
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outputs, ["created", "created"])
        self.create.assert_called_once()


class PortableDrafts(CalendarFixture):
    def test_export_has_evidence_and_no_invitation_delivery_properties(self):
        draft = self.stage(dict(PROPOSAL, attendees=["guest@example.com"], owner_only=False))
        output = plans.ics(draft["id"])
        self.assertIn("BEGIN:VEVENT\r\n", output)
        self.assertIn("DTSTART:20300911T100000Z\r\n", output)
        unfolded = output.replace("\r\n ", "")
        self.assertIn("Source quote: " + SOURCE["text"], unfolded)
        self.assertIn("imessage message-1", unfolded)
        for line in output.split("\r\n"):
            self.assertFalse(line.startswith(("METHOD:", "ORGANIZER", "ATTENDEE")))
        self.assertNotIn("guest@example.com", output)
        self.create.assert_not_called()

    def test_ical_control_text_cannot_inject_properties_and_unicode_folds_by_octets(self):
        title = "Résumé, " * 20 + "\r\nATTENDEE:mailto:guest@example.com"
        draft = self.stage(dict(PROPOSAL, title=title, location="Desk;home\\office"))
        output = plans.ics(draft["id"])
        self.assertNotIn("\r\nATTENDEE:", output)
        self.assertIn("LOCATION:Desk\\;home\\\\office\r\n", output)
        self.assertIn("\\nATTENDEE:", output.replace("\r\n ", ""))
        for line in output.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)


class GeneratedWorkBlocks(CalendarFixture):
    def plan(self, loop=None):
        return plans.plan_commitment(copy.deepcopy(loop or LOOP), "person-test", "A Contact")

    def test_inbound_due_commitment_gets_a_personal_slot_without_new_owner_command(self):
        self.busy.return_value = [(NOW, NOW.replace(hour=13))]
        draft = self.plan()
        self.assertEqual(draft["start"], "2030-09-10T13:00:00+00:00")
        self.assertEqual(draft["end"], "2030-09-10T13:30:00+00:00")
        self.assertEqual(draft["time_chosen_by"], "assistant")
        self.assertEqual(draft["schedule_kind"], "commitment")
        self.assertEqual(draft["due_quote"], LOOP["due_quote"])
        self.assertIn("chosen by Vira", draft["description"])
        self.assertFalse(draft["source"]["is_from_me"])
        self.assertTrue(draft["can_create"])
        self.busy.return_value = []
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "created")
        self.create.assert_called_once()
        self.assertEqual(self.create.call_args.args[0]["attendees"], [])

    def test_busy_event_ending_between_minutes_rounds_next_block_forward(self):
        self.busy.return_value = [(NOW, NOW.replace(hour=13, second=30))]
        draft = self.plan()
        self.assertEqual(draft["start"], "2030-09-10T13:01:00+00:00")
        self.assertTrue(draft["can_create"])

    def test_generated_blocks_still_require_standing_automatic_consent(self):
        self.cfg["assistant_calendar_auto_create"] = False
        draft = self.plan()
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "blocked")
        self.create.assert_not_called()
        self.assertEqual(plans.create_owner_event(draft["id"], automatic=False)["status"], "created")

    def test_missing_due_evidence_closed_edited_and_other_peoples_tasks_are_not_planned(self):
        for changes in ({"due": ""}, {"due_quote": "by next year"}, {"evidence": []},
                        {"owed_by": "them"}, {"status": "closed"}, {"edited": True},
                        {"source": "manual"}, {"due": "2030-09-20"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.plan(dict(LOOP, **changes)))
        self.busy.assert_not_called()
        self.create.assert_not_called()

    def test_explicit_owner_deadline_correction_overrides_old_source_date(self):
        corrected = dict(LOOP, due="2030-09-13", due_updated_by_owner=NOW.isoformat())
        self.current.return_value = corrected
        draft = self.plan(corrected)
        self.assertEqual(draft["deadline_authority"], "owner")
        self.assertEqual(draft["due"], "2030-09-13")
        self.assertIn("2030-09-12", draft["quote"])
        self.assertTrue(draft["can_create"])
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "created")

    def test_grounded_relative_deadline_can_receive_a_work_block(self):
        relative = dict(LOOP, due="2030-09-13", due_quote="in 3 days",
                        evidence=[dict(LOOP["evidence"][0], quote="Please send the report in 3 days.")])
        self.current.return_value = relative
        draft = self.plan(relative)
        self.assertTrue(draft["can_create"])
        self.assertEqual(draft["due"], "2030-09-13")

    def test_passive_fixture_sandbox_and_nonmac_never_read_calendars_for_planning(self):
        for gate in (mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}),
                     mock.patch.object(plans.settings, "sandboxed", return_value=True),
                     mock.patch.object(plans.settings, "fixture_mode", return_value=True),
                     mock.patch.object(plans.settings, "IS_MAC", False)):
            with gate:
                draft = self.plan()
                self.assertFalse(draft["can_create"])
                self.assertEqual(draft["start"], "")
                self.assertIn("Free-time planning", draft["reason"])
        self.busy.assert_not_called()

    def test_closed_or_changed_task_is_rechecked_before_calendar_write(self):
        draft = self.plan()
        for changes in ({"status": "closed"}, {"due": "2030-09-13"}, {"what": "Something different"}):
            with self.subTest(changes=changes):
                self.current.return_value = dict(LOOP, **changes)
                self.assertEqual(plans.create_owner_event(draft["id"])["status"], "blocked")
        self.create.assert_not_called()

    def test_calendar_change_between_planning_and_write_prevents_insertion(self):
        draft = self.plan()
        self.busy.return_value = [(NOW, NOW + dt.timedelta(hours=2))]
        out = plans.create_owner_event(draft["id"])
        self.assertEqual(out["status"], "blocked")
        self.assertIn("now busy", out["reason"])
        self.create.assert_not_called()

    def test_failed_availability_recheck_is_a_retryable_refusal_not_uncertain_write(self):
        draft = self.plan()
        self.busy.side_effect = RuntimeError("calendar access missing")
        out = plans.create_owner_event(draft["id"])
        self.assertEqual(out["status"], "blocked")
        self.assertIn("could not be rechecked", out["reason"])
        self.create.assert_not_called()

    def test_no_free_time_or_missing_coverage_keeps_an_unscheduled_evidence_linked_draft(self):
        self.busy.return_value = [(NOW, NOW + dt.timedelta(days=3))]
        draft = self.plan()
        self.assertEqual(draft["start"], "")
        self.assertFalse(draft["can_export"])
        self.assertIn("No free", draft["reason"])
        self.assertEqual(draft["evidence"], LOOP["evidence"])

    def test_transient_calendar_failure_is_replanned_after_bounded_retry(self):
        self.busy.side_effect = RuntimeError("temporary calendar error")
        draft = self.plan()
        self.assertEqual(draft["start"], "")
        self.busy.side_effect = None
        self.plan()
        self.assertEqual(self.busy.call_count, 1)
        with mock.patch.object(plans, "_now", return_value=NOW + dt.timedelta(minutes=6)):
            recovered = self.plan()
        self.assertEqual(recovered["id"], draft["id"])
        self.assertNotEqual(recovered["start"], "")
        self.assertEqual(self.busy.call_count, 2)

    def test_busy_slot_refusal_replans_to_new_free_time_without_reserving_itself(self):
        original = self.plan()
        self.busy.return_value = [(NOW, NOW.replace(hour=14))]
        self.assertEqual(plans.create_owner_event(original["id"])["status"], "blocked")
        with mock.patch.object(plans, "_now", return_value=NOW + dt.timedelta(minutes=6)):
            updated = self.plan()
            self.assertEqual(updated["id"], original["id"])
            self.assertEqual(updated["start"], "2030-09-10T14:00:00+00:00")
            self.busy.return_value = []
            self.assertEqual(plans.create_owner_event(updated["id"])["status"], "created")
        self.create.assert_called_once()

    def test_reservations_prevent_different_tasks_choosing_same_slot(self):
        first = self.plan()
        second = self.plan(dict(LOOP, assistant_key="other-task"))
        self.assertGreaterEqual(plans._date(second["start"]), plans._date(first["end"]))

    def test_weekends_are_skipped_and_horizon_is_bounded(self):
        friday_late = dt.datetime(2030, 9, 13, 18, tzinfo=dt.timezone.utc)
        future = dict(LOOP, due="2030-09-25", due_quote="by 2030-09-25",
                      evidence=[dict(LOOP["evidence"][0], quote="Please send the report by 2030-09-25.")])
        with mock.patch.object(plans, "_now", return_value=friday_late):
            draft = self.plan(future)
        self.assertEqual(draft["start"], "2030-09-16T09:00:00+00:00")
        self.assertEqual(self.busy.call_args.args[1], friday_late + dt.timedelta(days=7))

    def test_work_window_and_duration_changes_prevent_stale_block_creation(self):
        draft = self.plan()
        self.cfg["assistant_calendar_block_minutes"] = 60
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "blocked")
        self.create.assert_not_called()

    def test_work_settings_change_replans_uncreated_future_draft_after_cooldown(self):
        original = self.plan()
        self.cfg["assistant_calendar_block_minutes"] = 60
        with mock.patch.object(plans, "_now", return_value=NOW + dt.timedelta(minutes=6)):
            updated = self.plan()
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(plans._date(updated["end"]) - plans._date(updated["start"]), dt.timedelta(hours=1))
        self.assertNotEqual(updated["end"], original["end"])
        self.create.assert_not_called()

    def test_window_ending_at_midnight_accepts_block_ending_exactly_at_midnight(self):
        self.cfg.update(assistant_calendar_work_start=22, assistant_calendar_work_end=24)
        with mock.patch.object(plans, "_now", return_value=NOW.replace(hour=23, minute=25)):
            draft = self.plan()
            self.assertEqual(draft["end"], "2030-09-11T00:00:00+00:00")
            self.assertTrue(draft["can_create"])

    def test_ambiguous_calendar_result_can_never_be_replanned_into_duplicate_work_block(self):
        draft = self.plan()
        self.create.side_effect = RuntimeError("lost response after write")
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "uncertain")
        with mock.patch.object(plans, "_now", return_value=NOW + dt.timedelta(hours=2)):
            replay = self.plan()
        self.assertEqual(replay["id"], draft["id"])
        self.assertEqual(replay["status"], "uncertain")
        self.assertEqual(plans.create_owner_event(replay["id"])["status"], "uncertain")
        self.create.assert_called_once()

    def test_inbox_to_crm_migration_preserves_draft_identity_and_current_task_lookup(self):
        from server import executive
        original = self.plan()
        migrated = dict(LOOP, assistant_key="crm-key", assistant_origin={
            "subject_key": "person-test", "assistant_key": LOOP["assistant_key"]})
        self.current.side_effect = self.real_current_commitment
        records = [{"subject_key": "known-person", "person_name": "A Contact", "loop": migrated}]
        with mock.patch.object(executive, "commitment_records", return_value=records):
            moved = plans.plan_commitment(migrated, "known-person", "A Contact")
            self.assertEqual(moved["id"], original["id"])
            self.assertEqual(moved["subject_key"], "person-test")
            self.assertTrue(moved["can_create"])
            self.assertEqual(plans.create_owner_event(moved["id"])["status"], "created")
            replay = plans.plan_commitment(migrated, "known-person", "A Contact")
            self.assertEqual(replay["id"], original["id"])
            self.assertEqual(replay["status"], "created")
        self.create.assert_called_once()


class BusyCoverage(CalendarFixture):
    def setUp(self):
        super().setUp()
        self.busy_patch.stop()
        from server import brief, channels, msgraph
        self.brief = brief
        self.sql_path = Path(self.temp.name) / "calendar.sqlite"
        with closing(sqlite3.connect(self.sql_path)) as con, con:
            con.execute("CREATE TABLE CalendarItem (all_day INTEGER)")
            con.execute("CREATE TABLE OccurrenceCache (event_id INTEGER, occurrence_start_date REAL, occurrence_end_date REAL, occurrence_date REAL)")
        for patcher in (mock.patch.object(brief, "_cal_connect", side_effect=lambda: sqlite3.connect(self.sql_path)),
                        mock.patch.object(channels, "graph_accounts", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(msgraph, "_graph_request", side_effect=AssertionError("unexpected network read"))
        self.graph = patcher.start()
        self.addCleanup(patcher.stop)

    def insert(self, start, end, all_day=0):
        with closing(sqlite3.connect(self.sql_path)) as con, con:
            cursor = con.execute("INSERT INTO CalendarItem VALUES (?)", (all_day,))
            con.execute("INSERT INTO OccurrenceCache VALUES (?, ?, ?, ?)",
                        (cursor.lastrowid, start.timestamp() - self.brief.APPLE_EPOCH,
                         end.timestamp() - self.brief.APPLE_EPOCH, start.timestamp() - self.brief.APPLE_EPOCH))

    def test_expanded_occurrences_and_overnight_events_block_time_inside_query(self):
        self.insert(NOW - dt.timedelta(hours=14), NOW + dt.timedelta(hours=1))
        self.insert(NOW + dt.timedelta(hours=2), NOW + dt.timedelta(hours=3))
        self.insert(NOW + dt.timedelta(days=10), NOW + dt.timedelta(days=11))
        busy = plans._calendar_busy(NOW, NOW + dt.timedelta(hours=4))
        self.assertEqual(len(busy), 2)
        self.assertEqual(busy[0][0], NOW - dt.timedelta(hours=14))
        self.graph.assert_not_called()

    def test_unreadable_local_calendar_is_not_a_free_schedule(self):
        with mock.patch.object(self.brief, "_cal_connect", side_effect=sqlite3.OperationalError("no access")):
            with self.assertRaisesRegex(RuntimeError, "coverage is unavailable"):
                plans._calendar_busy(NOW, NOW + dt.timedelta(days=1))

    def test_graph_busy_read_follows_all_pages_and_skips_free_cancelled_events(self):
        from server import channels
        event = {"start": {"dateTime": "2030-09-10T13:00:00", "timeZone": "UTC"},
                 "end": {"dateTime": "2030-09-10T14:00:00", "timeZone": "UTC"}, "showAs": "busy"}
        self.graph.side_effect = [
            {"value": [dict(event, showAs="free"), dict(event, isCancelled=True)],
             "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=next"},
            {"value": [event]},
        ]
        with mock.patch.object(channels, "graph_accounts", return_value=[{"email": "owner@example.com"}]):
            busy = plans._calendar_busy(NOW, NOW + dt.timedelta(days=1))
        self.assertEqual(len(busy), 1)
        self.assertEqual(busy[0][0], NOW.replace(hour=13))
        self.assertEqual(self.graph.call_count, 2)
        self.assertEqual(self.graph.call_args.args[1], "/me/calendarView?$skiptoken=next")

    def test_incomplete_graph_coverage_blocks_scheduling(self):
        from server import channels
        for result in ({"value": [], "@odata.nextLink": "https://example.com/untrusted"},
                       {"value": [{"start": {"dateTime": "2030-09-10T13:00:00", "timeZone": "Pacific Standard Time"}}]}):
            self.graph.side_effect = None
            self.graph.return_value = result
            with mock.patch.object(channels, "graph_accounts", return_value=[{"email": "owner@example.com"}]):
                with self.subTest(result=result), self.assertRaises(RuntimeError):
                    plans._calendar_busy(NOW, NOW + dt.timedelta(days=1))


class NativeAdapter(CalendarFixture):
    def setUp(self):
        super().setUp()
        self.adapter.stop()
        self.os_call = mock.patch.object(plans.subprocess, "run")
        self.run = self.os_call.start()
        self.addCleanup(self.os_call.stop)

    def test_os_receives_constant_script_and_separate_json_not_evaluated_source(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, '{"uid":"os-event-1"}', "")
        draft = self.stage(dict(PROPOSAL, title="Report ' $(ignored); \" text"))
        out = plans.create_owner_event(draft["id"])
        self.assertEqual(out["status"], "created")
        args, kwargs = self.run.call_args
        self.assertEqual(args[0][:4], ["osascript", "-l", "JavaScript", "-"])
        payload = json.loads(args[0][4])
        self.assertEqual(payload["title"], draft["title"])
        self.assertIn(SOURCE["text"], payload["description"])
        self.assertEqual(kwargs["input"], plans._SCRIPT)
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("attendees", payload)

    def test_generated_work_block_asks_native_adapter_for_final_conflict_check(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, '{"uid":"os-event-2"}', "")
        draft = plans.plan_commitment(copy.deepcopy(LOOP), "person-test")
        self.assertEqual(plans.create_owner_event(draft["id"])["status"], "created")
        payload = json.loads(self.run.call_args.args[0][4])
        self.assertTrue(payload["avoid_conflicts"])
        self.assertNotIn("attendees", payload)

    def test_os_failure_or_unparseable_result_never_becomes_success(self):
        for result in (subprocess.CompletedProcess([], 1, "", "permission denied"),
                       subprocess.CompletedProcess([], 0, "not JSON", ""),
                       subprocess.CompletedProcess([], 0, "{}", "")):
            self.run.return_value = result
            source = dict(SOURCE, id="case-" + str(self.run.call_count))
            with self.subTest(result=result):
                out = plans.create_owner_event(self.stage(source=source)["id"])
                self.assertEqual(out["status"], "uncertain")
                self.assertNotIn("event_uid", out)

    def test_structured_prewrite_refusal_preserves_retryable_draft(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, '{"refused":"Choose a unique calendar."}', "")
        out = plans.create_owner_event(self.stage()["id"])
        self.assertEqual(out["status"], "blocked")
        self.assertIn("unique calendar", out["reason"])


@unittest.skipUnless(shutil.which("node"), "Node is needed to execute the synthetic JXA contract")
class JavascriptBoundary(unittest.TestCase):
    def test_metadata_reads_native_specifier_and_confirms_last_selected_default_without_events(self):
        harness = r'''
const preferences = {CalDefaultCalendar:'UseLastSelectedAsDefaultCalendar',
  defaultCalendarID:'native-test', 'last selected calendar list item':'native-test'};
global.$ = value => value;
$.CFPreferencesCopyAppValue = key => preferences[key];
global.ObjC = {import: () => {}, castRefToObject: value => value, deepUnwrap: value => value};
global.Automation = {getDisplayString: () => 'Application("Calendar").calendars.byId("native-test")'};
const calendar = {name: () => 'Synthetic default', writable: () => true,
  calendarIdentifier: () => {throw Error('broken getter');}};
Object.defineProperty(calendar, 'events', {get: () => {throw Error('Metadata read touched events');}});
global.Application = () => ({calendars: () => [calendar]});
'''
        result = subprocess.run([shutil.which("node"), "-e", harness + plans._METADATA_SCRIPT + "\nprocess.stdout.write(run());"],
                                capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        metadata = json.loads(result.stdout)
        self.assertEqual(metadata["default_id"], "native-test")
        self.assertEqual(metadata["default_policy"], "last_selected")
        self.assertEqual(metadata["calendars"], [{"native_id": "native-test", "name": "Synthetic default", "writable": True}])

    def execute(self, case):
        # Exercise the actual script with a fake scripting dictionary. The
        # real Application bridge is never loaded and osascript never runs.
        harness = r'''
const spec = JSON.parse(process.argv[1]);
const pushed = [];
const calendar = {writable: () => spec.writable !== false,
  calendarIdentifier: () => spec.native_id || 'calendar-test',
  events: {whose: query => () => Array.from({length: query.description ? (spec.existing || 0) : (spec.conflicts || 0)},
    () => ({uid: () => 'existing-event'})), push: e => pushed.push(e)}};
const calendars = () => [calendar];
calendars.whose = query => () => {
  if (query.name !== 'Selected calendar') throw Error('Wrong calendar');
  return Array(spec.matches === undefined ? 1 : spec.matches).fill(calendar);
};
global.Application = name => ({
  calendars,
  Event: properties => ({...properties, uid: () => 'new-event'})
});
'''
        footer = r'''
try {
  const result = JSON.parse(run([JSON.stringify({calendar: 'Selected calendar',
    calendar_id: spec.calendar_id, marker: 'Vira personal event: cal_test', title: "A 'literal' title",
    start: '2030-09-11T10:00:00Z', end: '2030-09-11T11:00:00Z',
    location: 'Desk', description: 'Test description', avoid_conflicts: spec.avoid_conflicts})]));
  process.stdout.write(JSON.stringify({result, pushed}));
} catch (error) { process.stdout.write(JSON.stringify({error: error.message, pushed})); }
'''
        result = subprocess.run([shutil.which("node"), "-e", harness + plans._SCRIPT + footer, json.dumps(case)],
                                capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        return json.loads(result.stdout)

    def test_nonunique_and_readonly_calendar_refuse_before_insert(self):
        for case in ({"matches": 0}, {"matches": 2}, {"writable": False}):
            with self.subTest(case=case):
                out = self.execute(case)
                self.assertIn("refused", out["result"])
                self.assertEqual(out["pushed"], [])

    def test_existing_marker_recovers_identity_without_another_insert(self):
        out = self.execute({"existing": 1})
        self.assertEqual(out["result"], {"uid": "existing-event", "existing": True})
        self.assertEqual(out["pushed"], [])

    def test_multiple_matching_markers_are_held_for_review(self):
        out = self.execute({"existing": 2})
        self.assertIn("Multiple events", out["error"])
        self.assertEqual(out["pushed"], [])

    def test_new_native_calendar_conflict_refuses_before_generated_block_insert(self):
        out = self.execute({"avoid_conflicts": True, "conflicts": 1})
        self.assertIn("now busy", out["result"]["refused"])
        self.assertEqual(out["pushed"], [])

    def test_generated_block_with_free_native_calendar_can_be_inserted(self):
        out = self.execute({"avoid_conflicts": True})
        self.assertEqual(out["result"]["uid"], "new-event")
        self.assertEqual(len(out["pushed"]), 1)

    def test_native_identifier_selects_destination_without_a_name_lookup(self):
        out = self.execute({"calendar_id": "calendar-test"})
        self.assertEqual(out["result"]["uid"], "new-event")

    def test_missing_native_identifier_does_not_fall_back_to_another_calendar(self):
        out = self.execute({"calendar_id": "missing"})
        self.assertIn("refused", out["result"])
        self.assertEqual(out["pushed"], [])

    def test_insert_contains_only_personal_event_properties_and_durable_marker(self):
        out = self.execute({})
        self.assertEqual(out["result"], {"uid": "new-event", "existing": False})
        self.assertEqual(len(out["pushed"]), 1)
        event = out["pushed"][0]
        self.assertEqual(set(event), {"summary", "startDate", "endDate", "description", "location", "alldayEvent"})
        self.assertIn("Vira personal event: cal_test", event["description"])
        self.assertEqual(event["startDate"], "2030-09-11T10:00:00.000Z")


if __name__ == "__main__":
    unittest.main()

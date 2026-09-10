"""Assistant HTTP contracts with local synthetic state and no lifespan.

The real route and validation layers run through TestClient. Stores are
temporary; model, source, notification, and native calendar boundaries are
replaced before any request. No watcher or background worker is started.
"""
import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import calendarplan, commitments, executive, settings


NOW = datetime(2030, 9, 10, 12, tzinfo=timezone.utc)
SUBJECT = "sender:email:synthetic"
LOOP = {
    "assistant_key": "synthetic-task", "what": "Send the workshop agenda",
    "owed_by": "me", "status": "open", "source": "vira-assistant",
    "since": "2030-09-10", "due": None,
    "deadline_review": {"text": "next Friday", "proposed": "2030-09-13",
                        "reason": "Confirm the intended Friday."},
    "evidence": [{"id": "mail:synthetic", "channel": "email",
                  "when": NOW.isoformat(), "quote": "Send the workshop agenda next Friday."}],
}
SOURCE = {
    "id": "imsg:synthetic", "channel": "imessage", "when": NOW.isoformat(),
    "is_from_me": True,
    "text": "Block my calendar for writing on 2030-09-11 from 10:00 to 11:00.",
}
PROPOSAL = {
    "title": "Personal writing time", "quote": SOURCE["text"],
    "time_quote": "2030-09-11 from 10:00 to 11:00", "owner_only_quote": "Block my calendar",
    "owner_only": True, "attendees": [],
    "start": "2030-09-11T10:00:00+00:00", "end": "2030-09-11T11:00:00+00:00",
}


class AssistantAPI(unittest.TestCase):
    def test_calendar_discovery_uses_metadata_and_accepts_stable_selection(self):
        response = self.client.get("/api/assistant/calendars?refresh=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selected"]["id"], "synthetic")
        calendarplan.destinations.assert_called_with(refresh=True)
        response = self.client.post("/api/assistant/config", json={
            "assistant_calendar_id": "synthetic", "assistant_calendar_name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read_config()["assistant_calendar_id"], "synthetic")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(dict(
            executive.DEFAULT_CONFIG, assistant_enabled=True, assistant_timezone="UTC",
            assistant_calendar_name="Synthetic personal", unrelated_setting="preserved")), encoding="utf-8")
        self.commitments_path = self.root / "commitments.json"
        self.commitments_path.write_text(json.dumps({"subjects": {SUBJECT: {
            "person_name": "Synthetic inbox", "open_loops": [copy.deepcopy(LOOP)]}}}), encoding="utf-8")
        patches = [
            mock.patch.object(settings, "CONFIG_PATH", self.config_path),
            mock.patch.object(settings, "raw", side_effect=self.read_config),
            mock.patch.object(settings, "fixture_mode", return_value=False),
            mock.patch.object(settings, "sandboxed", return_value=False),
            mock.patch.object(settings, "IS_MAC", True),
            mock.patch.object(executive, "STATE", self.root / "assistant.json"),
            mock.patch.object(executive, "_now", return_value=NOW),
            mock.patch.object(executive.crm, "_load", return_value={"profiles": {}, "by_id": {}}),
            mock.patch.object(commitments, "STORE", self.commitments_path),
            mock.patch.object(calendarplan, "STORE", self.root / "calendar.json"),
            mock.patch.object(calendarplan, "destinations", create=True,
                              return_value={"available": True, "calendars": [],
                                            "selected": {"id": "synthetic", "native_id": "synthetic", "name": "Synthetic personal", "writable": True}}),
            mock.patch.object(calendarplan, "_now", return_value=NOW),
            mock.patch.object(calendarplan, "_calendar_busy", return_value=[]),
            mock.patch("server.suggest.complete", side_effect=AssertionError("unexpected model call")),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ("VIRA_PASSIVE", "VIRA_SANDBOX"):
            os.environ.pop(key, None)
        patcher = mock.patch.object(calendarplan, "_calendar_create", return_value={"uid": "synthetic-event"})
        self.native_create = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(executive.notify, "assistant_send",
                                    side_effect=AssertionError("unexpected notification"))
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        from fastapi.testclient import TestClient
        from server import main
        # Do not use a with block: TestClient's context manager starts lifespan.
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.rid = executive._key(SUBJECT, LOOP)
        self.reminder_url = "/api/assistant/reminders/" + self.rid

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def saved_loop(self):
        return commitments.snapshot(SUBJECT)["open_loops"][0]

    def stage(self):
        return calendarplan.stage(copy.deepcopy(PROPOSAL), copy.deepcopy(SOURCE))

    def test_status_is_read_only_without_starting_workers(self):
        snapshot = {"enabled": True, "active": True, "reminders": [], "calendar": {"drafts": []}}
        with mock.patch.object(executive, "status", return_value=snapshot) as status, \
             mock.patch.object(executive, "start") as start, \
             mock.patch.object(executive, "tick") as tick:
            response = self.client.get("/api/assistant")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), snapshot)
        status.assert_called_once_with()
        start.assert_not_called()
        tick.assert_not_called()
        self.send.assert_not_called()

    def test_focused_page_loads_only_the_assistant_entry_point(self):
        response = self.client.get("/assistant")
        self.assertEqual(response.status_code, 200)
        self.assertIn('src="/assistant-page.js"', response.text)
        self.assertIn('src="/assistant.js"', response.text)
        self.assertNotIn('src="/app.js"', response.text)
        self.send.assert_not_called()
        self.native_create.assert_not_called()

    def test_config_save_preserves_other_settings_without_triggering_work(self):
        response = self.client.post("/api/assistant/config", json={
            "assistant_notify": True, "mail_body_index": True,
            "assistant_calendar_work_start": 10, "assistant_calendar_work_end": 18})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["mail_body_index"])
        self.assertTrue(self.read_config()["assistant_notify"])
        self.assertEqual(self.read_config()["unrelated_setting"], "preserved")
        self.send.assert_not_called()
        self.native_create.assert_not_called()

    def test_invalid_config_is_400_and_does_not_partially_save(self):
        before = self.config_path.read_text(encoding="utf-8")
        for payload in ({"unknown": True}, {"assistant_notify": "yes"},
                        {"assistant_notify_daily_cap": 0}, {"assistant_timezone": "Missing/Zone"},
                        {"assistant_enabled": False, "assistant_calendar_work_start": 18}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/assistant/config", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIsInstance(response.json()["detail"], str)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.client.post("/api/assistant/config", json=[]).status_code, 422)

    def test_date_correction_and_done_update_the_canonical_owner_task(self):
        response = self.client.post(self.reminder_url, json={"action": "date", "due": "2030-09-20"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["due"], "2030-09-20")
        self.assertEqual(self.saved_loop()["due"], "2030-09-20")
        self.assertTrue(self.saved_loop()["due_updated_by_owner"])
        self.assertNotIn("deadline_review", self.saved_loop())
        self.assertEqual(self.saved_loop()["evidence"], LOOP["evidence"])
        response = self.client.post(self.reminder_url, json={"action": "done"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "closed")
        self.assertEqual(self.saved_loop()["status"], "closed")
        self.assertEqual(self.client.post(self.reminder_url, json={"action": "done"}).status_code, 404)
        self.send.assert_not_called()

    def test_reminder_validation_and_missing_tasks_have_actionable_errors(self):
        for payload in ({"action": "date"}, {"action": "date", "due": "2030-02-30"},
                        {"action": "date", "due": "tomorrow"}, {"action": "snooze", "hours": 0},
                        {"action": "delete"}):
            with self.subTest(payload=payload):
                response = self.client.post(self.reminder_url, json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(self.saved_loop(), LOOP)
        self.assertEqual(self.client.post(self.reminder_url, json={}).status_code, 422)
        response = self.client.post("/api/assistant/reminders/missing", json={"action": "done"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Reminder is no longer open")

    def test_calendar_create_route_uses_explicit_manual_authority(self):
        with mock.patch.object(calendarplan, "create_owner_event", return_value={"id": "draft", "status": "created"}) as create:
            response = self.client.post("/api/assistant/calendar/draft", json={"action": "create", "automatic": True})
        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with("draft", automatic=False)

    def test_manual_creation_can_follow_an_automatic_toggle_refusal(self):
        draft = self.stage()
        # Automatic creation is off in this fixture's saved config.
        blocked = calendarplan.create_owner_event(draft["id"])
        self.assertEqual(blocked["status"], "blocked")
        self.native_create.assert_not_called()
        response = self.client.post("/api/assistant/calendar/" + draft["id"], json={"action": "create"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "created")
        self.native_create.assert_called_once()
        sent_draft, calendar_name = self.native_create.call_args.args
        self.assertEqual(sent_draft["attendees"], [])
        self.assertEqual(calendar_name, "Synthetic personal")

    def test_manual_calendar_route_cannot_bypass_the_real_passive_guard(self):
        draft = self.stage()
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            response = self.client.post("/api/assistant/calendar/" + draft["id"], json={"action": "create"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "blocked")
        self.assertIn("passive", response.json()["reason"])
        self.native_create.assert_not_called()

    def test_calendar_dismiss_and_error_responses(self):
        draft = self.stage()
        path = "/api/assistant/calendar/" + draft["id"]
        response = self.client.post(path, json={"action": "dismiss"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "dismissed")
        self.assertEqual(calendarplan.list_drafts(), [])
        self.assertEqual(self.client.post(path, json={"action": "invite"}).status_code, 400)
        self.assertEqual(self.client.post(path, json={}).status_code, 422)
        with mock.patch.object(calendarplan, "create_owner_event", side_effect=KeyError("missing")):
            self.assertEqual(self.client.post(path, json={"action": "create"}).status_code, 404)
        self.native_create.assert_not_called()

    def test_ics_route_returns_downloadable_invitation_free_calendar_data(self):
        draft = self.stage()
        response = self.client.get("/api/assistant/calendar/" + draft["id"] + ".ics")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/calendar", response.headers["content-type"])
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="calendar-draft.ics"')
        self.assertIn("BEGIN:VCALENDAR\r\n", response.text)
        self.assertIn("DTSTART:20300911T100000Z", response.text)
        for forbidden in ("ATTENDEE", "ORGANIZER", "METHOD:"):
            self.assertNotIn(forbidden, response.text)
        self.native_create.assert_not_called()
        with mock.patch.object(calendarplan, "ics", side_effect=ValueError("Calendar dates need review")):
            response = self.client.get("/api/assistant/calendar/invalid.ics")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Calendar dates need review")


if __name__ == "__main__":
    unittest.main()

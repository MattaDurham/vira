"""The bounded executive calendar loop advances every eligible task fairly."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import calendarplan, contactintel, executive


NOW = datetime(2030, 9, 10, 12, tzinfo=timezone.utc)


def record(key, due="2030-09-12"):
    return {"subject_key": "person-test", "person_name": "A Contact", "loop": {
        "assistant_key": key, "what": "Prepare " + key, "source": "vira-assistant",
        "owed_by": "me", "status": "open", "due": due,
    }}


def draft(key, **extra):
    return {"id": "cal_" + key, "subject_key": "person-test", "commitment_key": key,
            "schedule_kind": "commitment", "status": "suggested", "can_create": False,
            "start": "", "end": "", **extra}


class CalendarPlanningLoop(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = dict(executive.DEFAULT_CONFIG, assistant_enabled=True, assistant_timezone="UTC",
                        assistant_calendar_name="Synthetic calendar", assistant_batch_size=1,
                        assistant_calendar_auto_create=False)
        self.records = [record("first"), record("second", "2030-09-13"), record("third", "2030-09-14")]
        self.drafts = []
        self.now = NOW
        patchers = (
            mock.patch.object(executive, "STATE", Path(temp.name) / "assistant.json"),
            mock.patch.object(executive, "_worker_error", None),
            mock.patch.object(executive, "enabled", return_value=True),
            mock.patch.object(executive, "_now", side_effect=lambda: self.now),
            mock.patch.object(executive.settings, "raw", side_effect=lambda: self.cfg.copy()),
            mock.patch.object(executive, "commitment_records", side_effect=lambda: copy.deepcopy(self.records)),
            mock.patch.object(executive, "_notify"),
            mock.patch.object(contactintel, "tick"),
            mock.patch.object(calendarplan, "list_drafts", side_effect=lambda **kw: copy.deepcopy(self.drafts)),
            mock.patch.object(calendarplan, "destinations", create=True,
                              return_value={"selected": {"id": "synthetic", "name": "Synthetic calendar"}}),
            mock.patch.object(calendarplan, "create_owner_event", side_effect=AssertionError("unexpected calendar write")),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(calendarplan, "plan_commitment", return_value=None)
        self.plan = patcher.start()
        self.addCleanup(patcher.stop)

    def attempted(self):
        return [call.args[0]["assistant_key"] for call in self.plan.call_args_list]

    def tick(self, minutes=0):
        self.now += timedelta(minutes=minutes)
        executive.tick()

    def test_tasks_declined_before_draft_exists_cannot_starve_later_deadlines(self):
        self.tick()
        self.tick(1)
        self.tick(1)
        self.assertEqual(self.attempted(), ["first", "second", "third"])
        self.tick(1)
        self.assertEqual(self.attempted(), ["first", "second", "third"])
        self.tick(3)
        self.assertEqual(self.attempted(), ["first", "second", "third", "first"])
        self.assertEqual(len(executive._state()["calendar_planning_attempts"]), 3)

    def test_resolved_system_default_plans_without_typed_calendar_name(self):
        self.cfg["assistant_calendar_name"] = ""
        self.cfg["assistant_calendar_id"] = ""
        self.tick()
        self.assertEqual(self.attempted(), ["first"])

    def test_blocked_candidate_in_module_cooldown_does_not_consume_batch(self):
        self.drafts = [draft("first", status="blocked", planned_at=NOW.isoformat(),
                             updated_at=NOW.isoformat(), reason="Calendar is busy")]
        self.tick()
        self.assertEqual(self.attempted(), ["second"])

    def test_unscheduled_suggested_draft_is_retried_after_cooldown(self):
        self.records = self.records[:1]
        self.drafts = [draft("first", updated_at=(NOW - timedelta(minutes=6)).isoformat())]
        self.tick()
        self.assertEqual(self.attempted(), ["first"])

    def test_existing_ready_or_terminal_drafts_do_not_get_replanned(self):
        statuses = ("created", "dismissed", "creating", "uncertain", "suggested")
        self.records = [record(status) for status in statuses] + [record("new")]
        self.drafts = [draft(status, status=status, can_create=status == "suggested") for status in statuses]
        self.tick()
        self.assertEqual(self.attempted(), ["new"])

    def test_migrated_task_matches_original_completed_calendar_draft(self):
        moved = record("crm-key")
        moved["subject_key"] = "known-person"
        moved["loop"]["assistant_origin"] = {"subject_key": "person-test", "assistant_key": "first"}
        self.records = [moved, record("next")]
        self.drafts = [draft("first", status="created")]
        self.tick()
        self.assertEqual(self.attempted(), ["next"])

    def test_last_attempt_controls_fairness_across_saved_worker_cycles(self):
        first_key = executive._key("person-test", self.records[0]["loop"])
        second_key = executive._key("person-test", self.records[1]["loop"])
        third_key = executive._key("person-test", self.records[2]["loop"])
        executive._mutate(lambda state: state.update(calendar_planning_attempts={
            first_key: (NOW - timedelta(minutes=10)).isoformat(),
            second_key: (NOW - timedelta(minutes=20)).isoformat(),
            third_key: (NOW - timedelta(minutes=15)).isoformat(),
        }))
        self.tick()
        self.assertEqual(self.attempted(), ["second"])

    def test_one_task_error_does_not_abort_the_remaining_batch(self):
        self.cfg["assistant_batch_size"] = 2
        self.plan.side_effect = [RuntimeError("bad source"), None]
        self.tick()
        self.assertEqual(self.attempted(), ["first", "second"])
        self.assertIn("calendar task needs attention", executive._state()["last_error"])
        self.plan.side_effect = None
        self.tick(1)
        self.assertEqual(self.attempted(), ["first", "second", "third"])

    def test_removed_tasks_do_not_accumulate_scheduler_history(self):
        executive._mutate(lambda state: state.update(calendar_planning_attempts={"removed": NOW.isoformat()}))
        self.tick()
        self.assertNotIn("removed", executive._state()["calendar_planning_attempts"])

    def test_closed_task_attempt_history_is_pruned(self):
        key = executive._key(self.records[0]["subject_key"], self.records[0]["loop"])
        executive._mutate(lambda state: state.update(calendar_planning_attempts={key: NOW.isoformat()}))
        self.records[0]["loop"]["status"] = "closed"
        self.tick()
        self.assertNotIn(key, executive._state()["calendar_planning_attempts"])

    def test_pause_during_batch_stops_following_tasks(self):
        self.cfg["assistant_batch_size"] = 3
        with mock.patch.object(executive, "enabled", side_effect=[True, True, False]):
            self.tick()
        self.assertEqual(self.attempted(), ["first"])


if __name__ == "__main__":
    unittest.main()

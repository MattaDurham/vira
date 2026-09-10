"""A synthetic day through the real assistant's persistence and action paths.

Model responses and the final sender are stubbed. Message queues, profile and
owner-task writes, reminders, delivery receipts, corrections, and completion
all use their production implementations against temporary local stores.
"""
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from server import calendarplan, commitments, contactintel, executive
from server import data as crm
from tests.test_contactintel import AssistantFixture, NOW, PID, message, ref


INSTANT = datetime.fromtimestamp(NOW, timezone.utc)


def task(source, what, due, quote):
    return {"what": what, "owed_by": "me", "due": due,
            "due_quote": quote, "evidence": [ref(source)]}


class AssistantFlowTests(AssistantFixture):
    def setUp(self):
        super().setUp()
        self.cfg = dict(executive.DEFAULT_CONFIG, **self.cfg)
        self.cfg.update(assistant_notify=True, assistant_calendar_name="",
                        assistant_calendar_auto_create=False)
        patches = [
            mock.patch.object(executive, "STATE", self.root / "delivery-state.json"),
            mock.patch.object(executive, "_worker_error", None),
            mock.patch.object(executive, "_now", return_value=INSTANT),
            mock.patch.object(executive.settings, "raw", side_effect=lambda: self.cfg.copy()),
            mock.patch.object(executive.notify, "LOG", self.root / "notify.json"),
            mock.patch.object(executive.notify, "config", return_value={"enabled": True, "handle": "owner@example.test"}),
            mock.patch.object(calendarplan, "create_owner_event", side_effect=AssertionError("No OS calendar writes in this test")),
            mock.patch.object(executive.notify, "assistant_send", return_value={"status": "sent"}),
        ]
        for patcher in patches:
            patched = patcher.start()
            self.addCleanup(patcher.stop)
            if patcher.attribute == "assistant_send":
                self.sender = patched

    def test_incoming_conversations_become_canonical_tasks_then_resolve(self):
        contact = message("I moved to Portland. Please send the revised deck by 2026-09-10.")
        service = message("The application paperwork is due before the end of next month.",
                          id="mail:paperwork", channel="email", person_id=None,
                          handle="service@example.test", person_name="Example Service")
        owner = message("I need to finish the travel checklist by 2026-09-10.",
                        id="imsg:42", person_id="me", is_from_me=True)
        self.model.side_effect = [
            json.dumps({"facts": [{"fact": "Casey moved to Portland.", "evidence": [ref(contact)]}],
                        "loops": [task(contact, "Send the revised deck", "2026-09-10", "2026-09-10")]}),
            json.dumps({"loops": [task(service, "Submit the application paperwork", "2026-10-31", "the end of next month")]}),
            json.dumps({"loops": [task(owner, "Finish the travel checklist", "2026-09-10", "2026-09-10")]}),
        ]
        contactintel.enqueue([contact, service, owner])
        self.model.assert_not_called()
        self.assertEqual(contactintel.status()["pending_messages"], 3)

        executive.tick()

        self.assertIsNone(executive._state().get("last_error"))
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        self.assertEqual(self.model.call_count, 3)
        self.assertTrue(all(call.kwargs == {"tools": []} for call in self.model.call_args_list))
        self.assertEqual([fact["fact"] for fact in self.profile()["personal_facts"]],
                         ["Prefers morning calls.", "Casey moved to Portland."])
        self.assertEqual(len(self.profile()["open_loops"]), 1)
        self.assertEqual(len(commitments.all_subjects()), 2)
        rows = {row["what"]: row for row in executive.reminders(INSTANT)}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["Send the revised deck"]["person_id"], PID)
        self.assertEqual(rows["Finish the travel checklist"]["subject_key"], "owner:self")
        paperwork = rows["Submit the application paperwork"]
        self.assertIsNone(paperwork["person_id"])
        self.assertEqual(paperwork["person_name"], "Example Service")
        self.assertEqual(paperwork["stage"], "review")
        self.assertFalse(paperwork["notify_eligible"])
        self.assertEqual(self.sender.call_count, 2)
        self.assertTrue(all("application paperwork" not in call.args[0] for call in self.sender.call_args_list))
        self.assertTrue(all(row["delivery"] == "sent" for row in executive._state()["reminders"].values()))

        # This is the production backend invoked by the reminder POST route:
        # the owner resolves the uncertain date, then marks the contact task done.
        executive.reminder_action(paperwork["id"], "date", due="2026-09-10")
        executive.reminder_action(rows["Send the revised deck"]["id"], "done")
        self.assertEqual(self.profile()["open_loops"][0]["status"], "closed")
        corrected = commitments.snapshot(paperwork["subject_key"])["open_loops"][0]
        self.assertIn("due_updated_by_owner", corrected)
        self.assertNotIn("deadline_review", corrected)
        self.assertEqual(corrected["evidence"][0]["id"], service["id"])
        executive._notify(INSTANT)
        self.assertEqual(self.sender.call_count, 3)
        self.assertIn("application paperwork", self.sender.call_args.args[0])

        # A later owner message closes the self task automatically. Re-running
        # the worker cannot restore closed tasks or repeat a delivered reminder.
        done = message("I finished the travel checklist this morning.", id="imsg:43", person_id="me",
                       is_from_me=True, when="2026-09-10T11:30:00+00:00")
        self.model.side_effect = None
        self.model.return_value = json.dumps({"closed_loops": [{"what": "Finish the travel checklist", "evidence": [ref(done)]}]})
        contactintel.enqueue([done])
        executive.tick()
        self.assertEqual(commitments.snapshot("owner:self")["open_loops"][0]["status"], "closed")
        self.assertEqual([row["what"] for row in executive.reminders(INSTANT)], ["Submit the application paperwork"])
        executive.tick()
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.model.call_count, 4)
        self.assertEqual(contactintel.status()["pending_messages"], 0)

    def test_owner_date_correction_survives_new_evidence_and_close_survives_replay(self):
        first = message("I will send the revised deck by 2026-09-10.", person_id="me", is_from_me=True)
        self.model.return_value = json.dumps({"loops": [task(first, "Send the revised deck", "2026-09-10", "2026-09-10")]})
        contactintel.enqueue([first])
        executive.tick()
        original = executive.reminders(INSTANT)[0]
        executive.reminder_action(original["id"], "date", due="2026-09-12")
        corrected = executive.reminders(INSTANT)[0]
        self.assertEqual(corrected["id"], original["id"])
        self.assertEqual(corrected["due"], "2026-09-12")

        newer = message("The revised deck is now due on 2026-09-11.", id="imsg:42", person_id="me", is_from_me=True,
                        when="2026-09-10T11:00:00+00:00")
        self.model.return_value = json.dumps({"loops": [task(newer, "Send the revised deck", "2026-09-11", "2026-09-11")]})
        contactintel.enqueue([newer])
        executive.tick()
        self.assertEqual(executive.reminders(INSTANT)[0]["due"], "2026-09-12")
        self.assertEqual(self.sender.call_count, 1)
        executive.reminder_action(original["id"], "done")
        self.assertEqual(executive.reminders(INSTANT), [])

        # A new source repeats the old task. Stable task identity preserves
        # the owner's closed state in the canonical local store.
        replay = dict(newer, id="imsg:44", when="2026-09-10T11:30:00+00:00")
        self.model.return_value = json.dumps({"loops": [task(replay, "Send the revised deck", "2026-09-11", "2026-09-11")]})
        contactintel.enqueue([replay])
        executive.tick()
        self.assertEqual(executive.reminders(INSTANT), [])
        self.assertEqual(len(commitments.snapshot("owner:self")["open_loops"]), 1)
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        self.assertEqual(self.sender.call_count, 1)


class ContactResolutionFlowTests(AssistantFlowTests):
    """A late identity match moves an existing task without losing owner state."""

    # Keep this fixture independent from the two full-day scenario tests above.
    test_incoming_conversations_become_canonical_tasks_then_resolve = None
    test_owner_date_correction_survives_new_evidence_and_close_survives_replay = None

    def seed_unknown(self):
        source = message("Please send the revised deck by 2026-09-10.", id="mail:identity", channel="email",
                         person_id=None, handle="unmatched@example.test", person_name="Unmatched sender")
        self.model.return_value = json.dumps({"loops": [task(source, "Send the revised deck", "2026-09-10", "2026-09-10")]})
        contactintel.enqueue([source])
        executive.tick()
        row = executive.reminders(INSTANT)[0]
        return source, row

    def test_identity_resolution_preserves_reminder_identity_and_delivery_history(self):
        source, row = self.seed_unknown()
        self.assertEqual(self.sender.call_count, 1)
        contactintel.enqueue([dict(source, person_id=PID)])
        executive.tick()
        rows = executive.reminders(INSTANT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row["id"])
        self.assertEqual(rows[0]["person_id"], PID)
        self.assertEqual(commitments.all_subjects(), {})
        self.assertEqual(self.profile()["open_loops"][0]["assistant_origin"]["subject_key"], row["subject_key"])
        self.assertEqual(self.sender.call_count, 1)

    def test_owner_rename_and_deadline_correction_survive_contact_assignment(self):
        source, row = self.seed_unknown()
        commitments.set_due(row["subject_key"], row["assistant_key"], "2026-09-15")
        state = commitments._read()
        state["subjects"][row["subject_key"]]["open_loops"][0].update(what="Send deck after owner review", edited="2026-09-10")
        commitments.jsonstore.write_atomic(commitments.STORE, state)
        contactintel.enqueue([dict(source, person_id=PID)])
        executive.tick()
        loops = self.profile()["open_loops"]
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["what"], "Send deck after owner review")
        self.assertEqual(loops[0]["due"], "2026-09-15")
        self.assertIn("due_updated_by_owner", loops[0])
        self.assertEqual(commitments.all_subjects(), {})
        self.assertEqual(executive.reminders(INSTANT)[0]["id"], row["id"])

    def test_closed_local_task_stays_closed_after_identity_resolution(self):
        source, row = self.seed_unknown()
        executive.reminder_action(row["id"], "done")
        contactintel.enqueue([dict(source, person_id=PID)])
        executive.tick()
        self.assertEqual(self.profile()["open_loops"][0]["status"], "closed")
        self.assertEqual(executive.reminders(INSTANT), [])
        self.assertEqual(commitments.all_subjects(), {})
        self.assertEqual(self.sender.call_count, 1)

    def test_failed_destination_write_keeps_original_task_and_queued_source(self):
        source, row = self.seed_unknown()
        original = commitments.snapshot(row["subject_key"])
        contactintel.enqueue([dict(source, person_id=PID)])
        with mock.patch.object(crm, "save_contact_intelligence", side_effect=OSError("destination unavailable")):
            executive.tick()
        self.assertEqual(commitments.snapshot(row["subject_key"]), original)
        self.assertEqual(self.profile()["open_loops"], [])
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        contactintel.tick(NOW + 121)
        self.assertEqual(commitments.all_subjects(), {})
        self.assertEqual(len(self.profile()["open_loops"]), 1)

    def test_destination_copy_retries_after_source_removal_failure(self):
        source, row = self.seed_unknown()
        contactintel.enqueue([dict(source, person_id=PID)])
        with mock.patch.object(commitments, "finish_import", side_effect=OSError("source unavailable")):
            contactintel.tick(NOW)
        self.assertEqual(len(self.profile()["open_loops"]), 1)
        self.assertEqual(len(commitments.snapshot(row["subject_key"])["open_loops"]), 1)
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        contactintel.tick(NOW + 121)
        self.assertEqual(len(self.profile()["open_loops"]), 1)
        self.assertEqual(commitments.all_subjects(), {})
        self.assertEqual(contactintel.status()["pending_messages"], 0)

    def test_pending_unidentified_source_moves_before_it_can_create_duplicate_task(self):
        source = message("Please send the revised deck by 2026-09-10.", id="mail:pending", channel="email",
                         person_id=None, handle="unmatched@example.test")
        contactintel.enqueue([source])
        contactintel.enqueue([dict(source, person_id=PID)])
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        self.model.return_value = json.dumps({"loops": [task(source, "Send the revised deck", "2026-09-10", "2026-09-10")]})
        executive.tick()
        self.assertEqual(len(self.profile()["open_loops"]), 1)
        self.assertEqual(commitments.all_subjects(), {})
        self.assertEqual(self.sender.call_count, 1)

    def test_owner_edit_during_migration_stays_local_until_replayed_safely(self):
        source, row = self.seed_unknown()
        save = crm.save_contact_intelligence
        def copy_then_owner_edit(*args, **kwargs):
            result = save(*args, **kwargs)
            commitments.set_due(row["subject_key"], row["assistant_key"], "2026-09-15")
            return result
        contactintel.enqueue([dict(source, person_id=PID)])
        with mock.patch.object(crm, "save_contact_intelligence", side_effect=copy_then_owner_edit):
            contactintel.tick(NOW)
        self.assertEqual(commitments.snapshot(row["subject_key"])["open_loops"][0]["due"], "2026-09-15")
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        contactintel.tick(NOW + 121)
        self.assertEqual(self.profile()["open_loops"][0]["due"], "2026-09-15")
        self.assertIn("due_updated_by_owner", self.profile()["open_loops"][0])
        self.assertEqual(commitments.all_subjects(), {})
        self.assertEqual(contactintel.status()["pending_messages"], 0)


if __name__ == "__main__":
    unittest.main()

"""Synthetic, tool-free assistant passes over isolated CRM and corpus stores.

These exercise ingestion through durable acknowledgement, real profile merges,
completion evidence, calendar staging failure, and restart/retry behavior.
No model, native calendar, messaging provider, or owner store is reached.
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from server import calendarplan, channels, commitments, contactcard, contactintel, notify, textindex
from server import data as crm
from server.imessage import apple_ns


NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc).timestamp()
PID = "p_casey"


def message(text="I moved to Portland last week.", **kwargs):
    row = {"id": "imsg:41", "channel": "imessage", "person_id": PID,
           "when": "2026-09-10T10:00:00+00:00", "text": text,
           "is_from_me": False}
    row.update(kwargs)
    return row


def ref(source):
    return {"id": source["id"], "quote": source["text"]}


def fact_payload(source):
    return {"facts": [{"fact": "Casey moved to Portland.", "evidence": [ref(source)]}]}


class AssistantFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.crm_root = self.root / "crm"
        (self.crm_root / "profiles").mkdir(parents=True)
        people = [{"id": PID, "name": "Casey Example", "class_hint": "business",
                   "handles": {"emails": ["casey@example.test"]}},
                  {"id": "p_company", "name": "Example Company", "class_hint": "company"}]
        (self.crm_root / "people.json").write_text(json.dumps({"people": people}), encoding="utf-8")
        (self.crm_root / "master.json").write_text("[]", encoding="utf-8")
        self.profile_path = self.crm_root / "profiles" / (PID + ".json")
        self.write_profile({"name": "Casey Example", "relationship_summary": "Casey is an old friend from a previous team.",
                            "personal_facts": [{"fact": "Prefers morning calls.", "source": "vira"}],
                            "open_loops": []})
        self.cfg = {"assistant_enabled": True, "assistant_contact_updates": True,
                    "assistant_debounce_s": 0, "assistant_batch_size": 3,
                    "assistant_catchup_days": 14, "notify_handle": "owner@example.test",
                    "assistant_timezone": "UTC", "owner_name": "Morgan Example"}
        patches = [
            mock.patch.object(contactintel, "STATE", self.root / "intelligence.json"),
            mock.patch.object(calendarplan, "STORE", self.root / "calendar.json"),
            mock.patch.object(calendarplan, "destinations", create=True,
                              return_value={"selected": None, "calendars": [], "available": True}),
            mock.patch.object(commitments, "STORE", self.root / "commitments.json"),
            mock.patch.object(notify, "LOG", self.root / "notifications.json"),
            mock.patch.object(textindex, "DB", self.root / "text.sqlite"),
            mock.patch.object(contactintel.settings, "crm_root", return_value=self.crm_root),
            mock.patch.object(contactintel.settings, "fixture_mode", return_value=False),
            mock.patch.object(contactintel.settings, "sandboxed", return_value=False),
            mock.patch.object(contactintel.settings, "get", side_effect=lambda key: self.cfg.get(key)),
            mock.patch.object(contactcard, "added_handles", return_value={}),
            mock.patch.object(contactintel.time, "time", return_value=NOW),
            mock.patch.object(contactintel.modelbudget, "context_chars", return_value=100000),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("VIRA_PASSIVE", None)
        model_patch = mock.patch.object(contactintel.suggest, "complete", return_value="{}")
        self.model = model_patch.start()
        self.addCleanup(model_patch.stop)
        calendar_patch = mock.patch.object(calendarplan, "stage", return_value={})
        self.calendar_stage = calendar_patch.start()
        self.addCleanup(calendar_patch.stop)
        crm.invalidate()
        self.addCleanup(crm.invalidate)

    def write_profile(self, profile):
        self.profile_path.write_text(json.dumps(profile), encoding="utf-8")
        crm.invalidate()

    def profile(self):
        return json.loads(self.profile_path.read_text(encoding="utf-8"))

    def run_source(self, source, payload):
        self.model.return_value = json.dumps(payload)
        contactintel.enqueue([source])
        return contactintel.tick(NOW)

    def index(self, *sources):
        con = textindex._db()
        for source in sources:
            textindex._insert(con, uid=source["id"], source=source["channel"],
                              text=source["text"], chat_pid=source.get("person_id"),
                              from_me=int(source.get("is_from_me", False)),
                              sender_handle=source.get("handle"),
                              is_group=int(source.get("group", False)),
                              date_ns=apple_ns(datetime.fromisoformat(source["when"])))
        con.commit()
        con.close()


class QueueTests(AssistantFixture):
    def test_feed_to_durable_profile_keeps_owner_facts_and_business_people(self):
        source = message()
        self.model.return_value = json.dumps(fact_payload(source))
        shared = type("Feed", (), {"lock": threading.Lock(), "feed": [],
                                    "listeners": [], "feed_size": 10})()
        channels.push_feed_item(shared, dict(source, rowid=41))
        self.model.assert_not_called()
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        contactintel.tick(NOW)
        facts = self.profile()["personal_facts"]
        self.assertEqual(facts[0], {"fact": "Prefers morning calls.", "source": "vira"})
        self.assertEqual(facts[1]["fact"], "Casey moved to Portland.")
        self.assertEqual(facts[1]["source"], "vira-assistant")
        self.assertEqual(facts[1]["evidence"][0]["id"], "imsg:41")
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        self.assertEqual(self.model.call_args.kwargs, {"tools": []})
        self.assertTrue(list((self.crm_root / "backups" / "profiles").glob("*.json")))

    def test_catchup_discovers_inbound_and_outbound_after_restart(self):
        self.index(message(), message("I will send you the revised deck.", id="mail:second",
                                      channel="email", is_from_me=True))
        contactintel.catch_up(NOW)
        self.assertEqual(contactintel.status()["pending_messages"], 2)
        # All needed work is in the fresh disk read, independent of feed RAM.
        contactintel.tick(NOW)
        prompt = self.model.call_args.args[0]
        self.assertIn('"is_from_me": true', prompt)
        self.assertIn("revised deck", prompt)
        contactintel.catch_up(NOW)
        self.assertEqual(contactintel.status()["pending_messages"], 0)

    def test_queue_failure_does_not_advance_corpus_cursor(self):
        self.index(message())
        with mock.patch.object(contactintel, "_enqueue_into", side_effect=OSError):
            with self.assertRaises(OSError):
                contactintel.catch_up(NOW)
        self.assertIsNone(contactintel._state()["cursor"])
        contactintel.catch_up(NOW)
        self.assertEqual(contactintel.status()["pending_messages"], 1)

    def test_duplicate_and_shorter_preview_do_not_requeue_or_debounce(self):
        self.run_source(message(), fact_payload(message()))
        contactintel.enqueue([message(), message("I moved to")])
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        contactintel.enqueue([message("I moved to Portland last week. My new job starts tomorrow.")])
        self.assertEqual(contactintel.status()["pending_messages"], 1)

    def test_long_bodies_are_not_silently_truncated(self):
        source = message("First part. " * 1300 + "I will send the final deck tomorrow.")
        self.run_source(source, {})
        self.assertIn("send the final deck tomorrow", self.model.call_args.args[0])

    def test_full_email_body_supersedes_even_a_longer_subject_preview(self):
        preview = message("A very long subject heading: I sent the revised deck.", is_preview=True)
        self.run_source(preview, {})
        contactintel.enqueue([message("I sent the revised deck.", is_preview=False)])
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        contactintel.tick(NOW)
        contactintel.enqueue([preview])
        self.assertEqual(contactintel.status()["pending_messages"], 0)

    def test_batch_budget_retains_unprocessed_whole_messages(self):
        one, two = message("first body " * 500), message("second body " * 500, id="imsg:42")
        detail = crm.get_person(PID)
        budget = len(contactintel._prompt(detail, [contactintel._normalize(one)])) + 100
        contactintel.enqueue([one, two])
        with mock.patch.object(contactintel.modelbudget, "context_chars", return_value=budget):
            contactintel.tick(NOW)
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        self.assertNotIn("second body", self.model.call_args.args[0])

    def test_failure_retains_evidence_and_respects_retry_time(self):
        contactintel.enqueue([message()])
        self.model.side_effect = RuntimeError("private text must not be logged")
        contactintel.tick(NOW)
        state = contactintel.status()
        self.assertEqual(state["pending_messages"], 1)
        self.assertEqual(state["errors"][0]["error"], "RuntimeError")
        self.assertNotIn("private text", json.dumps(state))
        contactintel.tick(NOW + 1)
        self.assertEqual(self.model.call_count, 1)
        self.model.side_effect = None
        contactintel.tick(NOW + 121)
        self.assertEqual(contactintel.status()["pending_messages"], 0)

    def test_midflight_enrichment_is_not_acknowledged_with_old_preview(self):
        source = message()
        contactintel.enqueue([source])
        def response(*args, **kwargs):
            contactintel.enqueue([message(source["text"] + " I also moved offices.")])
            return "{}"
        self.model.side_effect = response
        contactintel.tick(NOW)
        self.assertEqual(contactintel.status()["pending_messages"], 1)

    def test_debounce_has_maximum_wait_for_busy_contact(self):
        self.cfg["assistant_debounce_s"] = 90
        contactintel.enqueue([message()])
        contactintel.tick(NOW + 30)
        self.model.assert_not_called()
        contactintel._change(lambda s: s["pending"][PID].update(updated=NOW + 899))
        contactintel.tick(NOW + 901)
        self.model.assert_called_once()

    def test_reaction_and_old_evidence_are_not_applied(self):
        contactintel.enqueue([message(associated_message_type=2000),
                              message(when="2025-01-01T12:00:00+00:00")])
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        self.assertEqual(sum(contactintel.status()["counters"].values()), 2)

    def test_passive_sandbox_fixture_and_disabled_do_not_queue(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            contactintel.enqueue([message()])
        with mock.patch.object(contactintel.settings, "sandboxed", return_value=True):
            contactintel.enqueue([message()])
        with mock.patch.object(contactintel.settings, "fixture_mode", return_value=True):
            contactintel.enqueue([message()])
        self.cfg["assistant_enabled"] = False
        contactintel.enqueue([message()])
        self.assertFalse(contactintel.STATE.exists())

    def test_pausing_during_model_call_prevents_profile_write(self):
        source = message()
        def pause(*args, **kwargs):
            self.cfg["assistant_enabled"] = False
            return json.dumps(fact_payload(source))
        self.model.side_effect = pause
        contactintel.enqueue([source])
        contactintel.tick(NOW)
        self.assertEqual(len(self.profile()["personal_facts"]), 1)
        self.assertEqual(contactintel.status()["pending_messages"], 1)

    def test_corrupt_store_is_not_replaced_by_an_empty_queue(self):
        contactintel.STATE.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            contactintel.enqueue([message()])
        self.assertEqual(contactintel.STATE.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(contactintel.status()["last_error"], "state_needs_repair")

    def test_corpus_error_remains_visible_after_a_successful_contact(self):
        contactintel.enqueue([message()])
        with mock.patch.object(textindex, "changes_since", side_effect=OSError):
            contactintel.tick(NOW)
        self.assertEqual(contactintel.status()["last_error"], "index_scan:OSError")
        contactintel.catch_up(NOW)
        self.assertIsNone(contactintel.status()["last_error"])


class FindingsTests(AssistantFixture):
    def loop_payload(self, source, **kwargs):
        loop = {"what": "Send the revised deck", "owed_by": "me", "evidence": [ref(source)]}
        loop.update(kwargs)
        return {"loops": [loop]}

    def test_deadline_and_quote_are_verified_then_saved(self):
        source = message("I will send the revised deck by tomorrow.", is_from_me=True)
        self.run_source(source, self.loop_payload(source, due="2026-09-11", due_quote="tomorrow"))
        loop = self.profile()["open_loops"][0]
        self.assertEqual(loop["due"], "2026-09-11")
        self.assertEqual(loop["owed_by"], "me")
        self.assertEqual(loop["status"], "open")

    def test_unverified_dates_are_held_and_fabricated_quotes_are_rejected(self):
        source = message("I will send the revised deck by tomorrow.", is_from_me=True)
        cases = [self.loop_payload(source, due="2035-01-01", due_quote="tomorrow"),
                 self.loop_payload(source, due="2026-09-11T15:00:00+00:00", due_quote="tomorrow")]
        for payload in cases:
            with self.subTest(payload=payload):
                update, _ = contactintel._clean(payload, [source], crm.get_person(PID))
                self.assertIsNone(update["loops"][0]["due"])
                self.assertEqual(update["loops"][0]["deadline_review"]["text"], "tomorrow")
        for payload in (self.loop_payload(source, evidence=[{"id": source["id"], "quote": "I already sent it."}]),
                        self.loop_payload(source, due="2035-01-01", due_quote="January 2035")):
            with self.assertRaises(ValueError):
                contactintel._clean(payload, [source], crm.get_person(PID))
        self.assertEqual(self.profile()["open_loops"], [])

    def test_ambiguous_deadline_keeps_valid_task_without_poison_retries(self):
        source = message("I will send the revised deck before the end of next month.", is_from_me=True)
        self.run_source(source, self.loop_payload(source, due="2026-10-31", due_quote="the end of next month"))
        loop = self.profile()["open_loops"][0]
        self.assertIsNone(loop["due"])
        self.assertEqual(loop["deadline_review"]["proposed"], "2026-10-31")
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        self.assertEqual(contactintel.status()["counters"]["held_deadlines"], 1)
        contactintel.tick(NOW + 121)
        self.model.assert_called_once()

    def test_explicit_completion_closes_only_the_matching_machine_loop(self):
        loop = {"what": "Send the revised deck", "owed_by": "me", "source": "vira-assistant", "status": "open"}
        profile = self.profile()
        profile["open_loops"] = [loop, {"what": "Send the budget", "owed_by": "me", "status": "open"}]
        self.write_profile(profile)
        source = message("I sent the revised deck this morning.", is_from_me=True)
        self.run_source(source, {"closed_loops": [{"what": loop["what"], "evidence": [ref(source)]}]})
        self.assertEqual(self.profile()["open_loops"][0]["status"], "closed")
        self.assertEqual(self.profile()["open_loops"][1]["status"], "open")

    def test_promise_and_completion_coalesced_in_one_pass_do_not_leave_an_open_task(self):
        first = message("I will send the revised deck.", is_from_me=True)
        second = message("I sent the revised deck this morning.", id="imsg:42", is_from_me=True,
                         when="2026-09-10T11:00:00+00:00")
        payload = self.loop_payload(first)
        payload["closed_loops"] = [{"what": "Send the revised deck", "evidence": [ref(second)]}]
        self.model.return_value = json.dumps(payload)
        contactintel.enqueue([first, second])
        contactintel.tick(NOW)
        self.assertEqual(self.profile()["open_loops"][0]["status"], "closed")

    def test_owner_inbox_task_completes_and_preserves_evidence(self):
        source = message("I will send the revised deck.", person_id="me", is_from_me=True)
        self.run_source(source, self.loop_payload(source))
        second = message("I sent the revised deck this morning.", id="imsg:42", person_id="me",
                         is_from_me=True, when="2026-09-10T11:00:00+00:00")
        self.run_source(second, {"closed_loops": [{"what": "Send the revised deck", "evidence": [ref(second)]}]})
        loop = commitments.snapshot("owner:self")["open_loops"][0]
        self.assertEqual(loop["status"], "closed")
        self.assertEqual(loop["evidence"][0]["id"], source["id"])
        self.assertEqual(loop["closed_evidence"][0]["id"], second["id"])

    def test_negated_future_unrelated_and_edited_completions_are_held(self):
        profile = self.profile()
        profile["open_loops"] = [{"what": "Send the revised deck", "owed_by": "me", "source": "vira-assistant", "status": "open"}]
        self.write_profile(profile)
        for text in ("I have not sent the revised deck.", "I will have sent the revised deck tomorrow.", "I sent the budget this morning."):
            source = message(text, is_from_me=True)
            # Deliberately quote only the affirmative fragment of the negation.
            quote = "sent the revised deck" if "not sent" in text else text
            raw = {"closed_loops": [{"what": "Send the revised deck", "evidence": [{"id": source["id"], "quote": quote}]}]}
            with self.subTest(text=text), self.assertRaises(ValueError):
                contactintel._clean(raw, [source], crm.get_person(PID))
        profile["open_loops"][0]["edited"] = "2026-09-10"
        self.write_profile(profile)
        source = message("I sent the revised deck this morning.", is_from_me=True)
        with self.assertRaises(ValueError):
            contactintel._clean({"closed_loops": [{"what": "Send the revised deck", "evidence": [ref(source)]}]}, [source], crm.get_person(PID))

    def test_duplicate_machine_findings_do_not_reopen_closed_tasks(self):
        source = message("I will send the revised deck by tomorrow.", is_from_me=True)
        payload = self.loop_payload(source)
        update, _ = contactintel._clean(payload, [source], crm.get_person(PID))
        crm.save_contact_intelligence(PID, update)
        crm.update_loop(PID, "Send the revised deck", "close")
        crm.save_contact_intelligence(PID, update)
        self.assertEqual(len(self.profile()["open_loops"]), 1)
        self.assertEqual(self.profile()["open_loops"][0]["status"], "closed")

    def test_concurrent_owner_summary_and_fact_edits_survive(self):
        source = message()
        old = self.profile()["relationship_summary"]
        update, _ = contactintel._clean(fact_payload(source), [source], crm.get_person(PID))
        update.update(relationship_summary="Casey moved to Portland and is adjusting to a new city.", summary_evidence=[ref(source)])
        profile = self.profile()
        profile["relationship_summary"] = "The owner wrote this newer relationship description."
        profile["personal_facts"].append({"fact": "Owner's newer note.", "source": "vira"})
        self.write_profile(profile)
        crm.save_contact_intelligence(PID, update, expected_summary=old)
        self.assertEqual(self.profile()["relationship_summary"], profile["relationship_summary"])
        self.assertEqual(len(self.profile()["personal_facts"]), 3)

    def test_calendar_failure_keeps_sources_until_draft_is_durable(self):
        source = message("Let's meet to discuss the report tomorrow.")
        payload = {"calendar_proposals": [{"source_id": source["id"], "title": "Discuss report", "quote": source["text"]}]}
        self.calendar_stage.side_effect = OSError("disk unavailable")
        self.run_source(source, payload)
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        self.calendar_stage.side_effect = None
        contactintel.tick(NOW + 121)
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        self.assertEqual(self.calendar_stage.call_args.args[1]["text"], source["text"])

    def test_self_scheduling_source_stages_calendar_and_owner_task_without_profile_edit(self):
        source = message("Remind me to work on the report tomorrow.", person_id="me", is_from_me=True)
        payload = fact_payload(source)
        payload["loops"] = [{"what": "Work on the report", "owed_by": "me", "evidence": [ref(source)]}]
        payload["calendar_proposals"] = [{"source_id": source["id"], "title": "Work on report", "quote": source["text"]}]
        before = self.profile()
        self.run_source(source, payload)
        self.calendar_stage.assert_called_once()
        self.assertTrue(self.calendar_stage.call_args.args[1]["is_from_me"])
        self.assertEqual(self.profile(), before)
        self.assertIn("outside a human contact dossier", self.model.call_args.args[0])
        self.assertEqual(commitments.snapshot("owner:self")["open_loops"][0]["what"], "Work on the report")

    def test_company_and_unknown_sender_obligations_live_outside_crm(self):
        before = self.profile()
        for index, pid in enumerate(("p_company", None)):
            source = message("Your appointment is confirmed for tomorrow; bring the signed form.",
                             id=f"mail:appointment{index}", channel="email", person_id=pid,
                             handle=f"service{index}@example.test", person_name="Example Service")
            payload = fact_payload(source)
            payload["loops"] = [{"what": "Bring the signed form to the appointment", "owed_by": "me",
                                 "due": "2026-09-11", "due_quote": "tomorrow", "evidence": [ref(source)]}]
            self.run_source(source, payload)
        subjects = commitments.all_subjects()
        self.assertEqual(len(subjects), 2)
        self.assertTrue(all(row["open_loops"][0]["due"] == "2026-09-11" for row in subjects.values()))
        self.assertEqual(self.profile(), before)
        self.assertFalse((self.crm_root / "profiles" / "p_company.json").exists())

    def test_paused_contact_updates_still_extracts_commitments_and_calendar(self):
        self.cfg["assistant_contact_updates"] = False
        source = message("I will send the revised deck by tomorrow.", is_from_me=True)
        payload = fact_payload(source)
        payload.update(self.loop_payload(source, due="2026-09-11", due_quote="tomorrow"))
        payload["calendar_proposals"] = [{"source_id": source["id"], "title": "Prepare deck", "quote": source["text"]}]
        self.run_source(source, payload)
        self.assertEqual(len(self.profile()["personal_facts"]), 1)
        self.assertEqual(len(self.profile()["open_loops"]), 1)
        self.calendar_stage.assert_called_once()

    def test_group_tasks_require_explicit_owner_attribution_and_never_edit_dossiers(self):
        cases = [("I'll send the revised deck by tomorrow.", True, True),
                 ("Morgan Example, please send the revised deck by tomorrow.", False, True),
                 ("Can someone send the revised deck by tomorrow?", False, False),
                 ("Can someone send the revised deck by tomorrow?", True, False)]
        before = self.profile()
        for number, (body, outgoing, accepted) in enumerate(cases):
            source = message(body, id=f"imsg:{100 + number}", is_from_me=outgoing,
                             group=True, chat_id=number + 1)
            payload = self.loop_payload(source, due="2026-09-11", due_quote="tomorrow")
            payload["facts"] = fact_payload(source)["facts"]
            self.run_source(source, payload)
            subject = contactintel._normalize(source)["subject_key"]
            with self.subTest(body=body, outgoing=outgoing):
                self.assertEqual(bool(commitments.snapshot(subject)["open_loops"]), accepted)
        self.assertEqual(self.profile(), before)

    def test_group_calendar_proposal_cannot_claim_to_be_owner_only(self):
        source = message("Let's meet to discuss the report tomorrow.", group=True, chat_id=5, is_from_me=True)
        self.run_source(source, {"calendar_proposals": [{"source_id": source["id"], "title": "Discuss report",
                                                       "quote": source["text"], "owner_only": True}]})
        self.assertFalse(self.calendar_stage.call_args.args[0]["owner_only"])

    def test_revised_due_requires_newer_evidence_and_unchanged_snapshot(self):
        source = message("I will send the revised deck by tomorrow.", is_from_me=True)
        self.run_source(source, self.loop_payload(source, due="2026-09-11", due_quote="tomorrow"))
        expected = self.profile()
        later = message("The revised deck is now due on 2026-09-15.", id="imsg:42", is_from_me=True,
                        when="2026-09-10T11:00:00+00:00")
        update, _ = contactintel._clean(self.loop_payload(later, due="2026-09-15", due_quote="2026-09-15"),
                                        [later], crm.get_person(PID))
        counts = crm.save_contact_intelligence(PID, update, expected_profile=expected)
        self.assertEqual(counts["revised"], 1)
        self.assertEqual(self.profile()["open_loops"][0]["due"], "2026-09-15")
        # Replaying a stale inference cannot move the deadline back.
        original, _ = contactintel._clean(self.loop_payload(source, due="2026-09-11", due_quote="tomorrow"),
                                          [source], crm.get_person(PID))
        self.assertEqual(crm.save_contact_intelligence(PID, original, expected_profile=self.profile())["revised"], 0)
        self.assertEqual(self.profile()["open_loops"][0]["due"], "2026-09-15")
        # Editing the task between inference and merge likewise wins.
        crm.update_loop(PID, "Send the revised deck", "edit", "Send deck after owner review")
        counts = crm.save_contact_intelligence(PID, update, expected_profile=expected)
        self.assertEqual(counts["revised"], 0)
        self.assertEqual(counts["loops"], 0)
        self.assertEqual(len(self.profile()["open_loops"]), 1)


class CorpusChangesTests(AssistantFixture):
    def test_missing_index_remains_missing(self):
        self.assertFalse(textindex.changes_since()["available"])
        self.assertFalse(textindex.DB.exists())

    def test_date_window_then_sequence_preserves_delayed_mail(self):
        self.index(message(id="old", when="2020-01-01T00:00:00+00:00"), message())
        first = textindex.changes_since(since="2026-09-01", limit=1)
        self.assertEqual([s["id"] for s in first["items"]], ["imsg:41"])
        self.index(message(id="delayed", channel="email", when="2026-09-02T00:00:00+00:00"))
        second = textindex.changes_since(first["cursor"], since="2026-09-10")
        self.assertEqual([s["id"] for s in second["items"]], ["delayed"])

    def test_reset_index_sequence_reenters_recent_window(self):
        self.index(message())
        out = textindex.changes_since(100, since="2026-09-01")
        self.assertEqual([s["id"] for s in out["items"]], ["imsg:41"])


class DeadlineWritesTests(AssistantFixture):
    def seed(self, *, owner=False, text="I will send the revised deck before the end of next month.", due="2026-10-31", due_quote="the end of next month"):
        source = message(text, person_id="me" if owner else PID, is_from_me=True)
        payload = {"loops": [{"what": "Send the revised deck", "owed_by": "me", "due": due,
                              "due_quote": due_quote, "evidence": [ref(source)]}]}
        self.run_source(source, payload)
        profile = commitments.snapshot("owner:self") if owner else self.profile()
        return source, profile["open_loops"][0]

    def test_owner_corrected_crm_deadline_preserves_evidence_and_blocks_future_inference(self):
        source, loop = self.seed()
        corrected = crm.set_loop_due(PID, loop["assistant_key"], "2026-10-30")
        self.assertEqual(corrected["due"], "2026-10-30")
        self.assertIn("due_updated_by_owner", corrected)
        self.assertNotIn("deadline_review", corrected)
        self.assertNotIn("edited", corrected)
        self.assertEqual(corrected["evidence"], loop["evidence"])
        later = message("The revised deck is due on 2026-10-25.", id="imsg:42", is_from_me=True,
                        when="2026-09-10T11:00:00+00:00")
        update, _ = contactintel._clean({"loops": [{"what": loop["what"], "owed_by": "me", "due": "2026-10-25",
                                                     "due_quote": "2026-10-25", "evidence": [ref(later)]}]},
                                        [later], crm.get_person(PID))
        self.assertEqual(crm.save_contact_intelligence(PID, update, expected_profile=self.profile())["revised"], 0)
        self.assertEqual(self.profile()["open_loops"][0]["due"], "2026-10-30")

    def test_owner_corrected_inbox_deadline_preserves_evidence_and_blocks_future_inference(self):
        source, loop = self.seed(owner=True)
        corrected = commitments.set_due("owner:self", loop["assistant_key"], "2026-10-30")
        self.assertEqual(corrected["evidence"], loop["evidence"])
        self.assertNotIn("deadline_review", corrected)
        self.assertIn("due_updated_by_owner", corrected)
        candidate = dict(loop, due="2026-10-25", evidence=[dict(loop["evidence"][0], when="2026-09-11T11:00:00+00:00")])
        counts = commitments.merge("owner:self", "Owner", {"loops": [candidate]}, expected=commitments.snapshot("owner:self"))
        self.assertEqual(counts["revised"], 0)
        self.assertEqual(commitments.snapshot("owner:self")["open_loops"][0]["due"], "2026-10-30")

    def test_deadline_updates_refuse_missing_keys_and_closed_tasks(self):
        _, contact_loop = self.seed()
        _, owner_loop = self.seed(owner=True)
        with self.assertRaises(LookupError):
            crm.set_loop_due(PID, "missing", "2026-10-30")
        with self.assertRaises(LookupError):
            commitments.set_due("owner:self", "missing", "2026-10-30")
        crm.update_loop(PID, contact_loop["what"], "close")
        commitments.close("owner:self", owner_loop["assistant_key"])
        with self.assertRaises(LookupError):
            crm.set_loop_due(PID, contact_loop["assistant_key"], "2026-10-30")
        with self.assertRaises(LookupError):
            commitments.set_due("owner:self", owner_loop["assistant_key"], "2026-10-30")

    def test_deadline_writer_waits_for_other_writer_and_preserves_its_changes(self):
        _, loop = self.seed()
        attempted, finished = threading.Event(), threading.Event()
        errors = []
        def change_due():
            attempted.set()
            try:
                crm.set_loop_due(PID, loop["assistant_key"], "2026-10-30")
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        with crm.locked(self.profile_path):
            worker = threading.Thread(target=change_due)
            worker.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(finished.wait(0.05))
            latest = self.profile()
            latest["external_refresh_note"] = "Written by the other profile writer."
            self.write_profile(latest)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.profile()["external_refresh_note"], "Written by the other profile writer.")
        self.assertEqual(self.profile()["open_loops"][0]["due"], "2026-10-30")

    def test_local_task_can_be_created_and_closed_in_the_same_pass(self):
        first = message("I will send the revised deck.", person_id="me", is_from_me=True)
        done = message("I sent the revised deck this morning.", person_id="me", is_from_me=True,
                       id="imsg:42", when="2026-09-10T11:00:00+00:00")
        self.model.return_value = json.dumps({"loops": [{"what": "Send the revised deck", "owed_by": "me", "evidence": [ref(first)]}],
                                             "closed_loops": [{"what": "Send the revised deck", "evidence": [ref(done)]}]})
        contactintel.enqueue([first, done])
        contactintel.tick(NOW)
        self.assertEqual(commitments.snapshot("owner:self")["open_loops"][0]["status"], "closed")

    def test_local_rename_does_not_resurrect_an_older_task_wording(self):
        _, loop = self.seed(owner=True)
        state = commitments._read()
        edited = state["subjects"]["owner:self"]["open_loops"][0]
        edited.update(what="Send the deck after owner review", edited="2026-09-10")
        commitments.jsonstore.write_atomic(commitments.STORE, state)
        counts = commitments.merge("owner:self", "Owner", {"loops": [loop]}, expected=commitments.snapshot("owner:self"))
        self.assertEqual(counts["loops"], 0)
        self.assertEqual(len(commitments.snapshot("owner:self")["open_loops"]), 1)

    def test_new_unresolved_local_deadline_replaces_old_due_with_review(self):
        first, loop = self.seed(owner=True, text="I will send the revised deck by tomorrow.",
                                due="2026-09-11", due_quote="tomorrow")
        newer = message("The revised deck is now due before the end of next month.", person_id="me", is_from_me=True,
                        id="imsg:42", when="2026-09-10T11:00:00+00:00")
        self.run_source(newer, {"loops": [{"what": loop["what"], "owed_by": "me", "due": "2026-10-31",
                                          "due_quote": "the end of next month", "evidence": [ref(newer)]}]})
        current = commitments.snapshot("owner:self")["open_loops"][0]
        self.assertIsNone(current["due"])
        self.assertEqual(current["deadline_review"]["proposed"], "2026-10-31")
        self.assertEqual(current["evidence"][0]["id"], first["id"])
        self.assertEqual(contactintel.status()["counters"]["held_deadlines"], 1)

    def test_store_refuses_a_closure_older_than_the_current_commitment(self):
        source, loop = self.seed(owner=True)
        closing = {"what": loop["what"], "evidence": [dict(loop["evidence"][0], when="2026-09-09T09:00:00+00:00")]}
        counts = commitments.merge("owner:self", "Owner", {"closed_loops": [closing]}, expected=commitments.snapshot("owner:self"))
        self.assertEqual(counts["closed"], 0)
        self.assertEqual(commitments.snapshot("owner:self")["open_loops"][0]["status"], "open")

    def test_inbox_errors_do_not_expose_invalid_contact_links(self):
        self.model.side_effect = RuntimeError("example provider error")
        self.run_source(message(person_id="me", is_from_me=True), {})
        error = contactintel.status()["errors"][0]
        self.assertIsNone(error["person_id"])
        self.assertEqual(error["person_name"], "Owner")


if __name__ == "__main__":
    unittest.main()

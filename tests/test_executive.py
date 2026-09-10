"""Commitment reminders: local dates, owner controls, and honest delivery."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import executive
from server.notify import assistant_send as real_assistant_send


UTC = timezone.utc
NOW = datetime(2026, 9, 10, 16, tzinfo=UTC)


def commitment(**extra):
    return {
        "what": "Send the revised outline", "owed_by": "me", "status": "open",
        "since": "2026-09-06", "due": "2026-09-10", "assistant_key": "outline",
        "source": "vira-assistant",
        "evidence": [{"id": "imsg:9", "quote": "Please send the revised outline by September 10.",
                      "channel": "imessage", "when": "2026-09-06T12:00:00+00:00"}],
        **extra,
    }


class ExecutiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = dict(executive.DEFAULT_CONFIG, assistant_enabled=True,
                        assistant_notify=True, assistant_timezone="UTC")
        self.corpus = {"by_id": {"p_test": {"id": "p_test", "name": "Casey Example"}},
                       "profiles": {"p_test": {"open_loops": [commitment()]}}}
        patches = [
            mock.patch.object(executive, "STATE", self.root / "assistant.json"),
            mock.patch.object(executive, "_worker_error", None),
            mock.patch.object(executive.notify, "LOG", self.root / "notify.json"),
            mock.patch.object(executive.commitments, "STORE", self.root / "commitments.json"),
            mock.patch.object(executive.settings, "CONFIG_PATH", self.root / "config.json"),
            mock.patch.object(executive.settings, "raw", side_effect=lambda: self.cfg.copy()),
            mock.patch.object(executive.settings, "fixture_mode", return_value=False),
            mock.patch("server.mail.accounts_view", return_value={"accounts": []}),
            mock.patch("server.calendarplan.destinations", create=True,
                       side_effect=lambda: {"selected": {"id": "example", "name": "Personal"}
                                            if self.cfg.get("assistant_calendar_name") or self.cfg.get("assistant_calendar_id")
                                            else None, "calendars": [], "available": True}),
            mock.patch.object(executive.crm, "_load", side_effect=lambda: copy.deepcopy(self.corpus)),
            mock.patch.object(executive.notify, "config", return_value={"enabled": True, "handle": "+15555550123"}),
            mock.patch.object(executive.notify, "assistant_send", return_value={"status": "sent"}),
            mock.patch.object(executive, "_now", return_value=NOW),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in patches:
            value = p.start()
            self.addCleanup(p.stop)
            if getattr(p, "attribute", None) == "assistant_send":
                self.send = value
        for key in ("VIRA_PASSIVE", "VIRA_SANDBOX"):
            os.environ.pop(key, None)

    def test_date_only_due_ends_in_owner_timezone(self):
        self.cfg["assistant_timezone"] = "America/New_York"
        row = executive.reminders(datetime(2026, 9, 11, 2, tzinfo=UTC))[0]
        self.assertEqual(row["stage"], "due")
        row = executive.reminders(datetime(2026, 9, 11, 5, tzinfo=UTC))[0]
        self.assertEqual(row["stage"], "overdue")

    def test_coverage_and_attention_expose_failed_sources_and_unscheduled_work(self):
        from server import calendarplan, contactintel, mail
        drafts = [{"id": "unknown-write", "title": "Personal block", "status": "uncertain"},
                  {"id": "no-slot", "title": "Prepare outline", "status": "suggested",
                   "schedule_kind": "commitment", "start": "", "reason": "No free time"}]
        with mock.patch.object(calendarplan, "list_drafts", return_value=drafts), \
             mock.patch.object(contactintel, "status", return_value={"errors": [{"error": "retry"}]}), \
             mock.patch.object(mail, "accounts_view", return_value={"accounts": [{"state": "ok", "stale": True}]}):
            snap = executive.status()
            self.assertTrue(any("mailbox" in message for message in snap["coverage"]))
            self.assertTrue(any("calendar write" in message for message in snap["coverage"]))
            self.assertTrue(any("no calendar block" in message for message in snap["coverage"]))
            ids = {row["id"] for row in executive.attention_rows()}
            self.assertIn("health:assistant", ids)
            self.assertIn("assistant:calendar:unknown-write", ids)
            self.assertIn("assistant:calendar:no-slot", ids)

    def test_undated_stale_and_them_owed_have_different_phone_policy(self):
        self.corpus["profiles"]["p_test"]["open_loops"] = [
            commitment(due=None), commitment(assistant_key="other", owed_by="them")]
        rows = {r["owed_by"]: r for r in executive.reminders(NOW)}
        self.assertEqual(rows["me"]["stage"], "stale")
        self.assertTrue(rows["me"]["notify_eligible"])
        self.assertFalse(rows["them"]["notify_eligible"])

    def test_legacy_loops_are_visible_without_new_phone_storm(self):
        lp = self.corpus["profiles"]["p_test"]["open_loops"][0]
        lp.pop("evidence")
        lp.pop("source")
        self.assertEqual(len(executive.reminders(NOW)), 1)
        executive._notify(NOW)
        self.send.assert_not_called()

    def test_closed_loops_and_companies_are_excluded(self):
        self.corpus["profiles"]["p_test"]["open_loops"][0]["status"] = "closed"
        self.assertEqual(executive.reminders(NOW), [])
        self.corpus["profiles"]["p_test"]["open_loops"][0]["status"] = "open"
        self.corpus["by_id"]["p_test"]["class_hint"] = "company"
        self.assertEqual(executive.reminders(NOW), [])

    def test_snooze_survives_fresh_read_and_expires(self):
        rid = executive.reminders(NOW)[0]["id"]
        executive.reminder_action(rid, "snooze", 24)
        self.assertEqual(executive.reminders(NOW), [])
        self.assertEqual(len(executive.reminders(NOW + timedelta(hours=25))), 1)
        executive._notify(NOW)
        self.send.assert_not_called()

    def test_reminder_identity_follows_task_when_sender_becomes_contact(self):
        local = commitment()
        moved = dict(local, assistant_origin={"subject_key": "sender:example", "assistant_key": "outline"})
        self.assertEqual(executive._key("sender:example", local), executive._key("p_test", moved))

    def test_done_closes_canonical_crm_loop(self):
        rid = executive.reminders(NOW)[0]["id"]
        with mock.patch.object(executive.crm, "update_loop") as close:
            result = executive.reminder_action(rid, "done")
        close.assert_called_once_with("p_test", "Send the revised outline", "close")
        self.assertEqual(result["status"], "closed")

    def test_quiet_hours_across_midnight_and_equal_disabled(self):
        for hour, expected in ((23, True), (7, True), (8, False), (21, False)):
            self.assertEqual(executive.quiet(NOW.replace(hour=hour)), expected)
        self.cfg.update(assistant_quiet_start=8, assistant_quiet_end=8)
        self.assertFalse(executive.quiet(NOW.replace(hour=8)))

    def test_success_deduplicates_same_stage_across_ticks(self):
        executive._notify(NOW)
        executive._notify(NOW + timedelta(minutes=10))
        self.assertEqual(self.send.call_count, 1)
        entry = next(iter(executive._state()["reminders"].values()))
        self.assertEqual(entry["delivery"], "sent")
        self.assertEqual(len(entry["notified"]), 1)

    def test_deadline_changed_during_claim_cancels_stale_notification(self):
        mutate = executive._mutate
        def claim_then_correct(fn):
            result = mutate(fn)
            if fn.__name__ == "claim":
                self.corpus["profiles"]["p_test"]["open_loops"][0]["due"] = "2026-11-10"
            return result
        with mock.patch.object(executive, "_mutate", side_effect=claim_then_correct):
            executive._notify(NOW)
        self.send.assert_not_called()
        self.assertEqual(executive._state()["deliveries"], [])

    def test_failed_delivery_is_not_marked_notified_and_retries_later(self):
        self.send.return_value = {"status": "failed"}
        executive._notify(NOW)
        entry = next(iter(executive._state()["reminders"].values()))
        self.assertEqual(entry["delivery"], "failed")
        self.assertFalse(entry.get("notified"))
        self.send.return_value = {"status": "sent"}
        executive._notify(NOW + timedelta(minutes=10))
        self.assertEqual(self.send.call_count, 1)
        executive._notify(NOW + timedelta(hours=1))
        self.assertEqual(self.send.call_count, 2)

    def test_unknown_send_outcome_is_held_across_restart(self):
        rid = executive.reminders(NOW)[0]["id"]
        executive._mutate(lambda s: s["reminders"].update({rid: {"delivery": "sending"}}))
        executive._notify(NOW)
        self.send.assert_not_called()

    def test_daily_cap_limits_attempts_not_only_successes(self):
        self.cfg["assistant_notify_daily_cap"] = 1
        self.corpus["profiles"]["p_test"]["open_loops"].append(
            commitment(what="Send second outline", assistant_key="second"))
        self.send.return_value = {"status": "failed"}
        executive._notify(NOW)
        self.assertEqual(self.send.call_count, 1)

    def test_passive_fixture_disabled_and_quiet_prevent_send(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            executive._notify(NOW)
        with mock.patch.object(executive.settings, "fixture_mode", return_value=True):
            executive._notify(NOW)
        self.cfg["assistant_enabled"] = False
        executive._notify(NOW)
        self.cfg["assistant_enabled"] = True
        executive._notify(NOW.replace(hour=23))
        self.send.assert_not_called()

    def test_config_validates_before_writing_and_preserves_other_keys(self):
        path = executive.settings.CONFIG_PATH
        path.write_text(json.dumps({"unrelated": "keep"}), encoding="utf-8")
        for value in ({"unexpected": True}, {"assistant_notify": "yes"},
                      {"assistant_quiet_start": 24}, {"assistant_stale_days": True},
                      {"assistant_timezone": "Missing/Zone"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                executive.save_config(value)
        executive.save_config({"assistant_notify": True, "assistant_timezone": "UTC"})
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["unrelated"], "keep")
        self.assertTrue(stored["assistant_notify"])

    def test_unknown_reminder_cannot_close_an_unrelated_loop(self):
        with mock.patch.object(executive.crm, "update_loop") as close:
            with self.assertRaises(KeyError):
                executive.reminder_action("missing", "done")
        close.assert_not_called()

    def test_corrupt_delivery_ledger_is_not_reset_and_cannot_resend(self):
        executive.STATE.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            executive._notify(NOW)
        self.send.assert_not_called()
        self.assertEqual(executive.STATE.read_text(encoding="utf-8"), "{broken")

    def test_restricted_claude_extract_removes_tools_and_mcp(self):
        from server import suggest
        result = mock.Mock(returncode=0, stdout='{"result":"{}"}', stderr="")
        with mock.patch.object(suggest.subprocess, "run", return_value=result) as run, \
                mock.patch.object(suggest, "_learn_from_cli"):
            suggest._call_cli("untrusted source", "model", 5, tools=[])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--safe-mode", cmd)
        self.assertNotIn("untrusted source", cmd)
        self.assertEqual(run.call_args.kwargs["input"], "untrusted source")

    def test_restricted_codex_extract_disables_action_tools_and_uses_clean_cwd(self):
        from server import models, suggest
        result = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(models, "find_binary", return_value="codex"), \
                mock.patch.object(suggest.subprocess, "run", return_value=result) as run:
            suggest._call_codex_cli("untrusted source", "model", 5, restricted=True)
        cmd = run.call_args.args[0]
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn('approval_policy="never"', cmd)
        for feature in ("apps", "plugins", "shell_tool", "multi_agent", "hooks", "browser_use", "computer_use"):
            self.assertTrue(any(cmd[i:i + 2] == ["--disable", feature] for i in range(len(cmd))))
        self.assertNotIn("untrusted source", cmd)
        self.assertEqual(run.call_args.kwargs["input"], "untrusted source")
        self.assertNotEqual(run.call_args.kwargs["cwd"], str(executive.settings.ROOT))

    def test_fixture_day_never_reads_os_sources_or_generates_a_model_narrative(self):
        from server import brief
        with mock.patch.object(brief.settings, "fixture_mode", return_value=True), \
                mock.patch.object(brief, "_calendar", side_effect=AssertionError("OS calendar read")), \
                mock.patch.object(brief, "_unreplied_imessages", side_effect=AssertionError("OS messages read")), \
                mock.patch.object(brief, "_drafts_queued", side_effect=AssertionError("mailbox read")), \
                mock.patch.object(brief, "_subs_section", side_effect=AssertionError("private ledger read")), \
                mock.patch.object(brief.suggest, "complete", side_effect=AssertionError("model call")):
            result = brief.compose()
            narrative = brief.generate_narrative(force=True)
        self.assertEqual(result["calendar"]["today"], [])
        self.assertEqual(narrative["status"], "fixture")

    def test_disabling_texts_during_a_batch_stops_remaining_sends(self):
        self.corpus["profiles"]["p_test"]["open_loops"].append(commitment(what="Another task", assistant_key="two"))
        def pause(*args, **kwargs):
            self.cfg["assistant_notify"] = False
            return {"status": "sent"}
        self.send.side_effect = pause
        executive._notify(NOW)
        self.assertEqual(self.send.call_count, 1)

    def test_uncertain_sender_result_is_not_automatically_retried(self):
        self.send.return_value = {"status": "uncertain", "detail": "timeout"}
        executive._notify(NOW)
        executive._notify(NOW + timedelta(hours=2))
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(next(iter(executive._state()["reminders"].values()))["delivery"], "uncertain")

    def test_ambiguous_deadline_is_visible_without_phone_alert(self):
        self.corpus["profiles"]["p_test"]["open_loops"][0].update(
            due=None, deadline_review={"text": "after the conference", "reason": "Ambiguous"})
        [row] = executive.reminders(NOW)
        self.assertEqual((row["stage"], row["priority"]), ("review", "high"))
        executive._notify(NOW)
        self.send.assert_not_called()

    def test_calendar_filters_eligible_drafts_before_the_batch_limit(self):
        from server import calendarplan, contactintel
        self.cfg["assistant_calendar_auto_create"] = True
        drafts = [{"id": str(i), "status": "suggested", "can_create": False} for i in range(10)]
        drafts.append({"id": "eligible", "status": "suggested", "can_create": True})
        with mock.patch.object(contactintel, "tick"), \
                mock.patch.object(calendarplan, "list_drafts", return_value=drafts), \
                mock.patch.object(calendarplan, "create_owner_event") as create, \
                mock.patch.object(executive, "_notify"):
            executive.tick()
        create.assert_called_once_with("eligible")

    def test_extraction_and_calendar_failure_do_not_hide_known_reminders(self):
        from server import calendarplan, contactintel
        self.cfg["assistant_calendar_auto_create"] = True
        with mock.patch.object(contactintel, "tick", side_effect=ValueError("bad queue")), \
                mock.patch.object(calendarplan, "list_drafts", side_effect=ValueError("bad calendar")), \
                mock.patch.object(contactintel, "status", return_value={"index_available": True, "mail_body_index": True}):
            executive.tick()
            snap = executive.status()
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(len(snap["reminders"]), 1)
        self.assertIn("error", snap["calendar"])
        self.assertIn("Message processing", snap["last_error"])

    def test_real_sender_honors_definite_rejection_and_uncertain_timeout(self):
        import subprocess
        from server import send
        with mock.patch.object(send, "send_message", return_value={"ok": False, "note": "No SMS route"}):
            result = real_assistant_send("Synthetic reminder")
        self.assertEqual(result["status"], "failed")
        with mock.patch.object(send, "send_message", side_effect=subprocess.TimeoutExpired("osascript", 20)):
            result = real_assistant_send("Synthetic reminder")
        self.assertEqual(result["status"], "uncertain")

    def test_manual_deadline_correction_targets_exact_canonical_task(self):
        [row] = executive.reminders(NOW)
        with mock.patch.object(executive.crm, "set_loop_due") as save:
            result = executive.reminder_action(row["id"], "date", due="2026-09-14")
        save.assert_called_once_with("p_test", "outline", "2026-09-14")
        self.assertEqual(result["due"], "2026-09-14")


if __name__ == "__main__":
    unittest.main()

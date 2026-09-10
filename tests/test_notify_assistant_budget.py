"""Executive reminder deliveries cannot spend either legacy notify budget.

All delivery records are synthetic, the log is temporary, and the clock
and native message adapter are replaced. No real message can be sent.
"""
from unittest import mock

from server import notify, send, settings
from tests.test_notify_budgets import Base, _entry


class AssistantBudgetIsolation(Base):
    def test_assistant_delivery_log_does_not_block_people_or_agent_pings(self):
        self.write([_entry("channel:assistant", "assistant", i)
                    for i in range(max(notify.DAILY_CAP, notify.AGENT_DAILY_CAP))])
        self.assertIsNone(notify._throttled("p_contact"))
        self.assertIsNone(notify._throttled("agent:routine:new"))

    def test_exact_assistant_channel_or_key_is_excluded(self):
        # Either stable marker can identify an assistant entry. In
        # particular, a generic agent key cannot make it spend that cap.
        for key, channel in (("legacy-key", "assistant"),
                             ("channel:assistant", "notify"),
                             ("agent:legacy", "assistant")):
            with self.subTest(key=key, channel=channel):
                self.write([_entry(key, channel, i)
                            for i in range(max(notify.DAILY_CAP, notify.AGENT_DAILY_CAP))])
                self.assertIsNone(notify._throttled("p_contact"))
                self.assertIsNone(notify._throttled("agent:routine:new"))

    def test_similar_names_keep_their_existing_human_or_agent_budget(self):
        for channel in ("assistant-extra", "notify"):
            with self.subTest(channel=channel):
                self.write([_entry("channel:assistant-extra", channel, i)
                            for i in range(notify.DAILY_CAP)])
                self.assertEqual(notify._throttled("p_contact"), "daily cap reached")
                self.assertIsNone(notify._throttled("agent:routine:new"))
        self.write([_entry("agent:assistant", "agent", i)
                    for i in range(notify.AGENT_DAILY_CAP)])
        self.assertEqual(notify._throttled("agent:routine:new"), "daily cap reached")
        self.assertIsNone(notify._throttled("p_contact"))

    def test_real_assistant_receipt_leaves_both_other_budgets_available(self):
        rows = [_entry(f"p_contact_{i}", "email", 60 + i)
                for i in range(notify.DAILY_CAP - 1)]
        rows += [_entry(f"agent:routine:{i}", "agent", 60 + i)
                 for i in range(notify.AGENT_DAILY_CAP - 1)]
        self.write(rows)
        with mock.patch.dict(notify.os.environ, {}, clear=True), \
             mock.patch.object(settings, "fixture_mode", return_value=False), \
             mock.patch.object(settings, "sandboxed", return_value=False), \
             mock.patch.object(notify, "config", return_value={"enabled": True, "handle": "+15555550123"}), \
             mock.patch.object(send, "send_message", return_value={"ok": True}) as native:
            receipt = notify.assistant_send("Synthetic reminder")
        self.assertEqual(receipt["status"], "sent")
        native.assert_called_once()
        self.assertEqual(notify.recent(1)[0]["channel"], "assistant")
        self.assertIsNone(notify._throttled("p_contact_new"))
        self.assertIsNone(notify._throttled("agent:routine:new"))
        # Ordinary cooldowns and each budget still apply to their own rows.
        self.assertEqual(notify._throttled("p_contact_0"), "sender cooldown")
        self.assertEqual(notify._throttled("agent:routine:0"), "sender cooldown")
        notify._record(_entry("p_contact_last", "email", 5))
        self.assertEqual(notify._throttled("p_contact_new"), "daily cap reached")
        self.assertIsNone(notify._throttled("agent:routine:new"))
        notify._record(_entry("agent:routine:last", "agent", 5))
        self.assertEqual(notify._throttled("agent:routine:new"), "daily cap reached")

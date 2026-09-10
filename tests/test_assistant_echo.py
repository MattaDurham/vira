"""Outgoing and looped-back assistant messages cannot generate new work."""
from unittest import mock

from server import commitments, contactintel, notify
from tests.test_contactintel import AssistantFixture, NOW, message


class AssistantEchoTests(AssistantFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(notify, "LOG", self.root / "notify.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_live_and_reconciled_own_texts_are_ignored_in_both_directions(self):
        for outbound in (True, False):
            contactintel.enqueue([message(
                "Vira: Due soon: Send the revised outline (due 2026-09-10).",
                person_id="me", is_from_me=outbound)])
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        contactintel.tick(NOW)
        self.model.assert_not_called()
        self.assertEqual(commitments.all_subjects(), {})

    def test_pending_echo_is_discarded_before_any_model_call(self):
        echo = message("Vira: Please check your open task.", person_id="me", is_from_me=True)
        with mock.patch.object(contactintel, "_is_echo", return_value=False):
            contactintel.enqueue([echo])
        self.assertEqual(contactintel.status()["pending_messages"], 1)
        contactintel.tick(NOW)
        self.assertEqual(contactintel.status()["pending_messages"], 0)
        self.model.assert_not_called()

    def test_exact_delivery_receipt_blocks_unprefixed_legacy_echo(self):
        body = "Please send the revised outline by tomorrow."
        with mock.patch.object(notify, "sent_texts", return_value=[body]):
            contactintel.enqueue([message(body, person_id="me", is_from_me=True)])
        self.assertEqual(contactintel.status()["pending_messages"], 0)

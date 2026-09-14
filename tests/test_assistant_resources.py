"""Action navigation comes from exact source records, never model guesses."""
import copy
import os
import unittest
from unittest import mock

from server import assistantresources as resources, contactintel

ACCOUNT = {"email": "owner@example.com", "host": "imap.gmail.com"}
MID = "<invoice-42@example.com>"
SOURCE_ID = "mail:" + MID
QUOTE = "Please pay invoice 42 by September 20."


def reminder(**extra):
    return {"id": "bill", "what": "Pay invoice 42", "evidence": [
        {"id": SOURCE_ID, "channel": "email", "quote": QUOTE}], **extra}


class Resources(unittest.TestCase):
    def setUp(self):
        self.patchers = [
            mock.patch.object(resources.settings, "fixture_mode", return_value=False),
            mock.patch.object(resources.settings, "sandboxed", return_value=False),
            mock.patch.object(resources.channels, "mail_accounts", return_value=[ACCOUNT]),
            mock.patch.object(resources.textindex, "lookup_sources", create=True, return_value={}),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("VIRA_PASSIVE", None)

    def test_legacy_evidence_recovers_exact_email_account_and_body_links(self):
        resources.textindex.lookup_sources.return_value = {SOURCE_ID: {
            "id": SOURCE_ID, "channel": "email", "account": ACCOUNT["email"],
            "message_id": MID, "subject": "Invoice 42",
            "text": QUOTE + " Pay at https://billing.example.com/pay/42?token=abc%2B123"}}
        row = reminder()
        before = copy.deepcopy(row["evidence"])
        resources.enrich([row])
        actions = row["resources"]
        email = next(action for action in actions if action["kind"] == "email" and not action.get("url"))
        self.assertEqual(email["account"], ACCOUNT["email"])
        self.assertEqual(email["message_id"], MID)
        self.assertTrue(any(action.get("label") == "Find email in Gmail" for action in actions))
        self.assertTrue(any(action.get("url", "").endswith("token=abc%2B123") for action in actions))
        self.assertEqual(row["evidence"], before)
        resources.textindex.lookup_sources.assert_called_once_with([SOURCE_ID])

    def test_live_feed_supplies_locator_before_the_index_catches_up(self):
        row = reminder()
        resources.enrich([row], [{"channel": "email", "message_id": MID,
                                  "account": ACCOUNT["email"], "rowid": "mail-owner@example.com-17",
                                  "text": QUOTE}])
        email = next(action for action in row["resources"] if not action.get("url"))
        self.assertEqual(email["rowid"], "mail-owner@example.com-17")

    def test_retained_source_account_wins_over_an_indexed_second_copy(self):
        resources.channels.mail_accounts.return_value += [{"email": "second@example.com", "type": "graph"}]
        resources.textindex.lookup_sources.return_value = {SOURCE_ID: {
            "id": SOURCE_ID, "account": "second@example.com", "message_id": MID}}
        row = reminder()
        row["evidence"][0].update(account=ACCOUNT["email"])
        resources.enrich([row])
        self.assertEqual(row["resources"][0]["account"], ACCOUNT["email"])

    def test_provider_ids_and_signed_links_never_cross_mailbox_copies(self):
        resources.channels.mail_accounts.return_value = [
            {"email": "first@example.com", "type": "graph"},
            {"email": "second@example.com", "type": "graph"}]
        resources.textindex.lookup_sources.return_value = {SOURCE_ID: {
            "id": SOURCE_ID, "account": "second@example.com", "message_id": MID,
            "graph_id": "second-only-id", "web_link": "https://outlook.example.com/second-only-id",
            "rowid": "mail-second@example.com-17", "text": "Pay https://billing.example.com/second-only-id"}}
        row = reminder()
        row["evidence"][0].update(account="first@example.com")
        # Legacy deadline evidence can precede its account-bearing sibling.
        row["evidence"].insert(0, {"id": SOURCE_ID, "channel": "email", "quote": "September 20"})
        resources.enrich([row])
        email = next(action for action in row["resources"] if not action.get("url"))
        self.assertEqual(email["account"], "first@example.com")
        self.assertEqual(email["message_id"], MID)
        for key in ("graph_id", "web_link", "rowid"):
            self.assertNotIn(key, email)
        self.assertNotIn("second-only-id", str(row["resources"]))

        # The matching live copy can provide a locator while another
        # account owns this Message-ID's existing index row.
        feed = [
            {"channel": "email", "account": "second@example.com", "message_id": MID,
             "graph_id": "second-feed-id", "web_link": "https://outlook.example.com/second-feed-id"},
            {"channel": "email", "account": "first@example.com", "message_id": MID,
             "graph_id": "first-feed-id", "web_link": "https://outlook.example.com/first-feed-id"}]
        resources.enrich([row], feed)
        email = next(action for action in row["resources"] if not action.get("url"))
        self.assertEqual(email["graph_id"], "first-feed-id")
        self.assertIn("https://outlook.example.com/first-feed-id", str(row["resources"]))
        self.assertNotIn("second-", str(row["resources"]))

    def test_new_deadline_evidence_retains_its_original_mailbox(self):
        source = {"id": SOURCE_ID, "channel": "email", "when": "2030-09-10T12:00:00+00:00",
                  "text": "Please pay invoice 42 by 2030-09-20.", "account": ACCOUNT["email"],
                  "message_id": MID, "graph_id": "original-mailbox-id", "is_from_me": False}
        detail = {"person": {"name": "Example Services"}, "profile": {}}
        for due in ("2030-09-20", "2035-01-01"):
            with self.subTest(due=due), mock.patch.object(contactintel, "_cfg", return_value="UTC"):
                update, _ = contactintel._clean({"loops": [{
                    "what": "Pay invoice 42", "owed_by": "me", "due": due, "due_quote": "2030-09-20",
                    "evidence": [{"id": SOURCE_ID, "quote": source["text"]}]}]}, [source], detail)
                loop = update["loops"][0]
                self.assertEqual(loop["due_evidence"]["account"], ACCOUNT["email"])
                self.assertEqual(loop["due_evidence"]["graph_id"], "original-mailbox-id")
                self.assertTrue(all(ref["account"] == ACCOUNT["email"] for ref in loop["evidence"]))

    def test_legacy_source_does_not_mix_feed_and_index_mailboxes(self):
        resources.textindex.lookup_sources.return_value = {SOURCE_ID: {
            "id": SOURCE_ID, "account": ACCOUNT["email"], "message_id": MID,
            "text": "View https://documents.example.com/statement.pdf"}}
        row = reminder()
        resources.enrich([row], [{"channel": "email", "account": "second@example.com",
                                  "message_id": MID, "graph_id": "second-feed-id"}])
        self.assertEqual(row["resources"][0]["account"], ACCOUNT["email"])
        self.assertNotIn("second-feed-id", str(row["resources"]))
        self.assertIn("https://documents.example.com/statement.pdf", str(row["resources"]))

    def test_tracked_html_payment_button_is_not_lost_when_url_alone_is_a_tracker(self):
        row = reminder()
        signed = "https://click.example.com/track?t=abc%2Bdef&invoice=42"
        row["evidence"][0].update(account=ACCOUNT["email"], message_id=MID, links=[
            {"label": "Pay invoice", "kind": "payment", "url": signed, "domain": "forged.example.com"},
            {"label": "Bad", "url": "javascript:alert(1)"}])
        resources.enrich([row])
        payment = next(action for action in row["resources"] if action["kind"] == "payment")
        self.assertEqual(payment["url"], signed)
        self.assertEqual(payment["domain"], "click.example.com")
        self.assertFalse(any(action.get("url", "").startswith("javascript:") for action in row["resources"]))

    def test_opaque_legacy_index_identity_does_not_guess_an_inbox_uid(self):
        row = reminder(evidence=[{"id": "mail:owner@example.com:99", "account": ACCOUNT["email"],
                                  "channel": "email", "quote": QUOTE}])
        resources.enrich([row])
        self.assertFalse(any(action["kind"] == "email" and not action.get("url") for action in row["resources"]))
        self.assertTrue(any(action["kind"] == "mailbox" for action in row["resources"]))
        self.assertIn("exact email", row["resources_note"])

    def test_disconnected_account_is_not_offered_as_a_working_email_reader(self):
        resources.channels.mail_accounts.return_value = []
        row = reminder()
        row["evidence"][0].update(account=ACCOUNT["email"], message_id=MID)
        resources.enrich([row])
        self.assertEqual(row["resources"], [])
        self.assertIn("exact email", row["resources_note"])

    def test_preview_never_reads_accounts_index_or_live_feed(self):
        for patcher in (mock.patch.object(resources.settings, "fixture_mode", return_value=True),
                        mock.patch.object(resources.settings, "sandboxed", return_value=True),
                        mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"})):
            with patcher:
                row = reminder()
                resources.enrich([row], [{"channel": "email", "message_id": MID,
                                          "account": "private@example.com", "text": QUOTE}])
                self.assertEqual(row["resources"], [])
        resources.channels.mail_accounts.assert_not_called()
        resources.textindex.lookup_sources.assert_not_called()

    def test_index_failure_keeps_direct_evidence_links_and_task(self):
        resources.textindex.lookup_sources.side_effect = RuntimeError("unavailable")
        row = reminder(evidence=[{"id": "imsg:7", "channel": "imessage",
                                  "quote": "Review https://documents.example.com/contract.pdf"}])
        resources.enrich([row])
        self.assertEqual(row["what"], "Pay invoice 42")
        self.assertEqual(row["resources"][0]["kind"], "document")
        self.assertIn("temporarily unavailable", row["resources_note"])

    def test_source_lookups_are_deduplicated_and_batched(self):
        rows = [reminder(id=str(i), evidence=[{"id": "imsg:" + str(i), "channel": "imessage"}]) for i in range(205)]
        resources.enrich(rows)
        self.assertEqual([len(call.args[0]) for call in resources.textindex.lookup_sources.call_args_list], [100, 100, 5])

    def test_navigation_survives_grounding_but_is_not_added_to_model_context(self):
        source = {"id": SOURCE_ID, "channel": "email", "when": "2030-09-10T12:00:00+00:00",
                  "text": QUOTE, "subject": "Invoice 42", "account": ACCOUNT["email"],
                  "message_id": MID, "graph_id": "private-routing-id", "rowid": "mail-owner@example.com-17",
                  "links": [{"label": "Pay invoice", "kind": "payment", "url": "https://billing.example.com/pay?secret=hidden"}]}
        evidence = contactintel._evidence([{"id": SOURCE_ID, "quote": QUOTE}], {SOURCE_ID: source})
        self.assertEqual(evidence[0]["account"], ACCOUNT["email"])
        self.assertEqual(evidence[0]["links"][0]["url"], source["links"][0]["url"])
        detail = {"person": {"name": "Example Services"}, "profile": {"open_loops": [{"what": "Pay invoice", "evidence": evidence}]}}
        with mock.patch.object(contactintel, "_cfg", return_value=""):
            prompt = contactintel._prompt(detail, [source])
        for hidden in (ACCOUNT["email"], "private-routing-id", "secret=hidden"):
            self.assertNotIn(hidden, prompt)
        self.assertIn("Invoice 42", prompt)
        self.assertIn(QUOTE, prompt)


if __name__ == "__main__":
    unittest.main()

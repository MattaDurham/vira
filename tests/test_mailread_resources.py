"""Source identity and action links from fake IMAP and Graph message reads."""
import email.message
import unittest
from unittest import mock

from server import mailread


IMAP = {"email": "owner@example.com", "host": "imap.gmail.com"}
GRAPH = {"email": "work@example.com", "type": "graph"}


def message(mid, text="Original source"):
    msg = email.message.EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = IMAP["email"]
    msg["Message-ID"] = mid
    msg["Subject"] = "Synthetic bill"
    msg.set_content(text)
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self, messages, searches):
        self.messages = messages
        self.searches = searches
        self.calls = []
        self.box = None

    def login(self, *args):
        pass

    def select(self, box, readonly=False):
        self.box = box.strip('"')
        self.calls.append(("select", self.box, readonly))

    def uid(self, operation, *args):
        self.calls.append((operation, self.box, args))
        if operation == "search":
            return "OK", [self.searches.get(self.box, b"")]
        body = self.messages.get((self.box, str(args[0])))
        return "OK", [(b"", body)] if body else [None]

    def logout(self):
        self.calls.append(("logout",))


class ImapSourceIdentity(unittest.TestCase):
    def fetch(self, fake, mid, rowid="mail-owner@example.com-5"):
        with mock.patch.object(mailread, "_accounts", return_value=[IMAP]), \
                mock.patch.object(mailread.mailmod, "keychain_password", return_value="synthetic"), \
                mock.patch.object(mailread.mailmod.channels, "imap_special_folder", return_value="All Mail"), \
                mock.patch.object(mailread.imaplib, "IMAP4_SSL", return_value=fake):
            return mailread.get_message(IMAP["email"], rowid, mid)

    def test_reused_inbox_uid_falls_back_to_exact_archived_message(self):
        mid = "<original@example.com>"
        fake = FakeIMAP({("INBOX", "5"): message("<other@example.com>"),
                         ("All Mail", "20"): message(mid, "Pay at https://billing.example.com/pay")},
                        {"All Mail": b"20"})
        found = self.fetch(fake, mid)
        self.assertEqual(found["message_id"], mid)
        self.assertEqual(found["imap_uid"], 20)
        self.assertEqual(found["links"][0]["kind"], "payment")
        self.assertEqual(found["mail_actions"][0]["label"], "Find email in Gmail")
        self.assertIn(("select", "All Mail", True), fake.calls)
        self.assertEqual(fake.calls[-1], ("logout",))

    def test_header_substring_matches_are_checked_before_returning(self):
        mid = "<original@example.com>"
        fake = FakeIMAP({("INBOX", "8"): message("<not-original@example.com>"),
                         ("INBOX", "7"): message(mid)}, {"INBOX": b"7 8"})
        found = self.fetch(fake, mid, rowid=None)
        self.assertEqual(found["imap_uid"], 7)

    def test_wrong_source_is_never_returned_as_requested_message(self):
        fake = FakeIMAP({("INBOX", "5"): message("<other@example.com>")}, {"INBOX": b"5"})
        with self.assertRaisesRegex(RuntimeError, "message not found"):
            self.fetch(fake, "<original@example.com>")

    def test_control_characters_are_rejected_before_any_mailbox_access(self):
        with mock.patch.object(mailread, "_accounts", return_value=[IMAP]), \
                mock.patch.object(mailread.mailmod, "keychain_password") as password, \
                mock.patch.object(mailread.imaplib, "IMAP4_SSL") as connection:
            for mid in ('<bad\r\n@example.com>', "bad\x00value"):
                with self.assertRaisesRegex(ValueError, "Message-ID"):
                    mailread.get_message(IMAP["email"], message_id=mid)
            password.assert_not_called()
            connection.assert_not_called()

    def test_imap_search_quotes_and_backslashes_are_escaped(self):
        fake = FakeIMAP({}, {})
        with self.assertRaises(RuntimeError):
            self.fetch(fake, '<part"with\\slash@example.com>', rowid=None)
        searches = [call for call in fake.calls if call[0] == "search"]
        self.assertEqual(searches[0][2][-1], '(HEADER Message-ID "<part\\"with\\\\slash@example.com>")')


class GraphResources(unittest.TestCase):
    def test_explicit_full_id_is_encoded_and_weblink_is_retained(self):
        full_id = "AA/full+id=="
        original_link = "https://outlook.office.com/owa/?ItemID=original%2Bitem"
        response = {"id": full_id, "webLink": original_link,
                    "body": {"contentType": "html", "content":
                             '<a href="https://billing.example.com/statement.pdf">View statement</a>'}}
        with mock.patch.object(mailread, "_accounts", return_value=[GRAPH]), \
                mock.patch.object(mailread.msgraph, "_graph_request", return_value=response) as request:
            result = mailread.get_message(GRAPH["email"], graph_id=full_id)
        self.assertEqual(result["graph_id"], full_id)
        self.assertTrue(request.call_args.args[1].startswith("/me/messages/AA%2Ffull%2Bid%3D%3D?$select="))
        self.assertIn("webLink", request.call_args.args[1])
        self.assertEqual(result["web_link"], original_link)
        self.assertEqual(result["mail_actions"][0]["url"], original_link)
        self.assertEqual(result["links"][0]["label"], "View statement")
        self.assertNotIn("method", request.call_args.kwargs)

    def test_truncated_feed_id_remains_unusable(self):
        with mock.patch.object(mailread, "_accounts", return_value=[GRAPH]), \
                mock.patch.object(mailread.msgraph, "_graph_request") as request:
            with self.assertRaises(RuntimeError):
                mailread.get_message(GRAPH["email"], rowid="mail-work@example.com-last24characters")
            request.assert_not_called()

    def test_message_id_remains_preferred_over_an_explicit_graph_id(self):
        with mock.patch.object(mailread, "_accounts", return_value=[GRAPH]), \
                mock.patch.object(mailread, "_graph_lookup", return_value={"id": "resolved"}) as lookup, \
                mock.patch.object(mailread.msgraph, "_graph_request") as direct:
            mailread.get_message(GRAPH["email"], message_id="<exact@example.com>", graph_id="older-id")
        lookup.assert_called_once_with(GRAPH["email"], "<exact@example.com>")
        direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()

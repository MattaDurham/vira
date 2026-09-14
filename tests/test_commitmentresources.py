"""Useful source links, exercised with synthetic mail and no external reads."""
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from server import commitmentresources as resources


class LinkExtraction(unittest.TestCase):
    def test_html_preserves_signed_destination_and_human_label(self):
        url = "https://billing.example.com/pay?token=signed%2Bvalue&invoice=123"
        found = resources.extract_links("", '<a href="' + url.replace("&", "&amp;") +
                                        '">View <b>bill</b> and pay</a>')
        self.assertEqual(found, [{"kind": "payment", "label": "View bill and pay",
                                 "url": url, "domain": "billing.example.com"}])

    def test_plain_urls_handle_punctuation_and_balanced_parentheses(self):
        links = resources.extract_links(
            "Statement: https://example.com/report(2026).pdf.\n"
            "Schedule: (https://calendar.example.com/book?id=1).")
        self.assertEqual([link["url"] for link in links],
                         ["https://example.com/report(2026).pdf", "https://calendar.example.com/book?id=1"])
        self.assertEqual([link["kind"] for link in links], ["document", "scheduling"])

    def test_junk_links_and_remote_assets_do_not_become_actions(self):
        html = ('<a href="https://example.com/unsubscribe?id=1">Stop email</a>'
                '<a href="https://example.com/preferences">Manage email preferences</a>'
                '<a href="https://example.com/track/click?x=1">Click here</a>'
                '<a href="https://example.com/pixel.gif">Open</a>'
                '<img src="https://example.com/open.gif">'
                '<script>https://example.com/script</script>'
                '<a href="https://example.com/statement.pdf">Statement</a>')
        self.assertEqual([r["url"] for r in resources.extract_links("", html)],
                         ["https://example.com/statement.pdf"])

    def test_useful_tracking_redirect_is_not_unwrapped_or_fetched(self):
        url = "https://click.example.com/click?target=https%3A%2F%2Fbank.example.com&signature=original"
        found = resources.extract_links("", '<a href="' + url + '">Pay bill</a>')
        self.assertEqual(found[0]["url"], url)
        self.assertEqual(found[0]["domain"], "click.example.com")

    def test_source_label_takes_priority_over_billing_hostname(self):
        found = resources.extract_links("", '<a href="https://billing.example.com/doc/1">View statement</a>')
        self.assertEqual(found[0]["kind"], "document")

    def test_duplicate_representations_keep_the_html_label_and_original_url(self):
        found = resources.extract_links("https://example.com/statement.pdf?utm_source=text",
            '<a href="https://example.com/statement.pdf?utm_source=html">September statement</a>')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["label"], "September statement")
        self.assertTrue(found[0]["url"].endswith("utm_source=html"))

    def test_different_signed_tokens_remain_distinct(self):
        found = resources.extract_links("https://example.com/pay?token=first\nhttps://example.com/pay?token=second")
        self.assertEqual(len(found), 2)

    def test_only_safe_absolute_http_urls_survive(self):
        bad = ["javascript:alert(1)", "data:text/html,test", "file:///tmp/a", "//example.com/a",
               "/relative", "https://good.example.com@evil.example.com/pay", "http://",
               "https://example.com\\@evil.example.com", "https://example.com:bad/pay",
               "https://example.com/%0aheader", "https://exa\nmple.com", "https://example.com/%00"]
        for url in bad:
            with self.subTest(url=url):
                self.assertIsNone(resources.safe_http_url(url))
        self.assertEqual(resources.extract_links("", "".join('<a href="' + u + '">Pay</a>' for u in bad)), [])

    def test_image_label_entities_and_hidden_anchors(self):
        found = resources.extract_links("", '<a href="https://example.com/support"><img alt="Help &amp; support"></a>'
            '<a href="https://example.com/hidden" style="display: none">Hidden</a>')
        self.assertEqual(found[0]["label"], "Help & support")
        self.assertEqual(found[0]["kind"], "help")
        self.assertEqual(len(found), 1)

    def test_empty_and_bounded_inputs(self):
        self.assertEqual(resources.extract_links(None, html=None), [])
        found = resources.extract_links("\n".join(f"https://example.com/path/{i}" for i in range(50)))
        self.assertEqual(len(found), resources.LINK_MAX)
        self.assertEqual(resources.extract_links(" " * resources.BODY_MAX + "https://example.com/pay"), [])

    def test_account_and_help_links_are_useful_without_inventing_a_merchant_url(self):
        found = resources.extract_links("Log in: https://example.com/account\nHelp: https://example.com/support")
        self.assertEqual([r["kind"] for r in found], ["account", "help"])
        self.assertEqual(resources.extract_links("Your utility bill is due; visit your account."), [])


class MailActions(unittest.TestCase):
    def test_custom_domain_gmail_is_recognized_by_configured_host(self):
        address = "owner@example.com"
        actions = resources.email_actions({"account": address, "message_id": "<message+1@example.com>"},
                                          [{"email": address, "host": "imap.gmail.com"}])
        self.assertEqual([a["kind"] for a in actions], ["email", "mailbox"])
        self.assertEqual(actions[0]["label"], "Find email in Gmail")
        url = urlsplit(actions[0]["url"])
        self.assertEqual(parse_qs(url.query), {"authuser": [address]})
        self.assertEqual(unquote(url.fragment), "search/in:anywhere rfc822msgid:message+1@example.com")

    def test_graph_uses_actual_weblink_not_reconstructed_id(self):
        url = "https://outlook.office.com/owa/?ItemID=original%2Bid&viewmodel=ReadMessageItem"
        actions = resources.email_actions({"account": "work@example.com", "graph_id": "different", "web_link": url},
                                          [{"email": "work@example.com", "type": "graph"}])
        self.assertEqual(actions[0]["url"], url)
        self.assertEqual(actions[0]["label"], "Open email in Outlook")

    def test_missing_exact_reference_is_honest_mailbox_fallback(self):
        actions = resources.email_actions({"account": "work@example.com", "graph_id": "full-id"},
                                          [{"email": "work@example.com", "type": "graph"}])
        self.assertEqual([a["kind"] for a in actions], ["mailbox"])

    def test_unknown_or_ambiguous_account_never_guesses_provider(self):
        locator = {"account": "owner@example.com", "web_link": "https://example.com/email"}
        account = {"email": "owner@example.com", "host": "imap.gmail.com"}
        for accounts in ([], [dict(account, email="other@example.com")], [account, account]):
            self.assertEqual(resources.email_actions(locator, accounts), [])
        self.assertEqual(resources.email_actions(locator, [{"email": locator["account"], "host": "gmail.attacker.example.com"}]), [])

    def test_explicit_webmail_url_is_used_for_unrecognized_provider(self):
        actions = resources.email_actions({"account": "owner@example.com"},
            [{"email": "owner@example.com", "host": "imap.example.com", "webmail_url": "https://mail.example.com/"}])
        self.assertEqual(actions[0]["url"], "https://mail.example.com/")
        self.assertEqual(actions[0]["kind"], "mailbox")

    def test_invalid_provider_urls_and_message_ids_are_not_action_links(self):
        actions = resources.email_actions({"account": "work@example.com", "web_link": "javascript:alert(1)"},
                                          [{"email": "work@example.com", "type": "graph"}])
        self.assertEqual([a["kind"] for a in actions], ["mailbox"])
        for mid in ("<message@example.com> OR from:someone", "bad\n@example.com"):
            actions = resources.email_actions({"account": "owner@example.com", "message_id": mid},
                [{"email": "owner@example.com", "host": "imap.gmail.com"}])
            self.assertEqual([a["kind"] for a in actions], ["mailbox"])


if __name__ == "__main__":
    unittest.main()

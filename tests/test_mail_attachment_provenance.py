"""Exact attachment manifests never call a partial preservation complete."""
import base64
import copy
from contextlib import closing
from email.message import EmailMessage
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from server import mailindex, mediaindex, msgraph


ACCOUNT = "owner@example.invalid"
MESSAGE_ID = "<agenda@example.invalid>"
GRAPH_MESSAGE = {"id": "message-one", "internetMessageId": MESSAGE_ID,
                 "receivedDateTime": "2030-09-10T12:00:00Z", "subject": "Workshop agenda",
                 "from": {"emailAddress": {"address": "sender@example.invalid"}},
                 "toRecipients": [{"emailAddress": {"address": ACCOUNT}}]}


def graph_file(ident="file-one", **changes):
    return {"@odata.type": "#microsoft.graph.fileAttachment", "id": ident,
            "name": ident + ".pdf", "contentType": "application/pdf", "size": 8,
            "contentBytes": base64.b64encode(b"document").decode("ascii"), **changes}


class AttachmentProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patch in (
            mock.patch.object(mediaindex, "_DATA", self.root),
            mock.patch.object(mediaindex, "DB", self.root / "media.sqlite"),
            mock.patch.object(mailindex, "ATTACH_DIR", self.root / "attachments"),
            mock.patch.object(mailindex, "_my_addresses", return_value={ACCOUNT}),
            mock.patch.object(mailindex.crm, "resolve_handle", return_value=None),
            mock.patch.object(msgraph, "connected", return_value=True),
            mock.patch.object(msgraph, "get_bytes", side_effect=AssertionError("unexpected network fetch")),
            mock.patch.object(mailindex, "_graph_messages", return_value=[copy.deepcopy(GRAPH_MESSAGE)]),
            mock.patch.object(mailindex.mailmod, "keychain_password", return_value="synthetic"),
            mock.patch.object(mailindex, "_all_mail_folder", return_value="INBOX"),
            mock.patch.object(mailindex, "_imap_search", return_value=[7]),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def manifest(self):
        with closing(sqlite3.connect(mediaindex.DB)) as con:
            row = con.execute("SELECT source_id,account,attachment_ids,complete FROM mail_attachment_sources").fetchone()
            rows = con.execute("SELECT id,path FROM items").fetchall()
        self.assertEqual(row[:2], ("mail:" + MESSAGE_ID, ACCOUNT))
        return {"ids": json.loads(row[2]), "complete": bool(row[3]), "files": dict(rows)}

    def graph(self, pages):
        with mock.patch.object(msgraph, "_graph_request", side_effect=pages) as request:
            mailindex._run_graph(ACCOUNT, "full", None, None, lambda *args: None)
            calls = request.call_args_list
        return self.manifest(), calls

    def imap(self, message):
        connection = mock.Mock()
        connection.uid.return_value = ("OK", [(b"metadata", message.as_bytes())])
        with mock.patch.object(mailindex.imaplib, "IMAP4_SSL", return_value=connection):
            mailindex._run_imap({"email": ACCOUNT, "host": "imap.example.invalid"},
                               "full", None, None, lambda *args: None)
        return self.manifest()

    def message(self):
        message = EmailMessage()
        message["From"] = "sender@example.invalid"
        message["To"] = ACCOUNT
        message["Message-ID"] = MESSAGE_ID
        message["Subject"] = "Workshop agenda"
        message["Date"] = "Tue, 10 Sep 2030 12:00:00 +0000"
        message.set_content("Please review the attached workshop agenda.")
        return message

    def test_graph_follows_all_pages_for_same_account_and_message(self):
        manifest, calls = self.graph([
            {"value": [graph_file()], "@odata.nextLink": msgraph.GRAPH + "/me/messages/message-one/attachments?$skiptoken=next"},
            {"value": [graph_file("file-two")]},
        ])
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(manifest["ids"]), 2)
        self.assertEqual(len(manifest["files"]), 2)
        self.assertEqual(calls[1].args, (ACCOUNT, "/me/messages/message-one/attachments?$skiptoken=next"))
        for path in manifest["files"].values():
            self.assertEqual(Path(path).read_bytes(), b"document")

    def test_graph_relative_continuation_stays_on_same_collection(self):
        manifest, calls = self.graph([
            {"value": [], "@odata.nextLink": "/me/messages/message-one/attachments?$skip=1"},
            {"value": [graph_file()]},
        ])
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(calls), 2)

    def test_graph_rejects_foreign_account_message_and_host_continuations(self):
        invalid = ["https://example.invalid/steal", "https://graph.microsoft.com.evil.invalid/v1.0/me/messages/message-one/attachments",
                   msgraph.GRAPH + "/users/other/messages/message-one/attachments",
                   msgraph.GRAPH + "/me/messages/other/attachments",
                   "//graph.microsoft.com/v1.0/me/messages/message-one/attachments",
                   msgraph.GRAPH + "/me/messages/message-one/attachments#fragment"]
        for url in invalid:
            with self.subTest(url=url):
                manifest, calls = self.graph([{ "value": [graph_file()], "@odata.nextLink": url }])
                self.assertFalse(manifest["complete"])
                self.assertEqual(len(calls), 1)

    def test_graph_failed_later_page_retains_partial_files_without_complete_claim(self):
        manifest, _ = self.graph([
            {"value": [graph_file()], "@odata.nextLink": msgraph.GRAPH + "/me/messages/message-one/attachments?$skip=1"},
            TimeoutError("synthetic page timeout"),
        ])
        self.assertFalse(manifest["complete"])
        self.assertEqual(len(manifest["files"]), 1)

    def test_graph_looping_or_malformed_page_is_incomplete(self):
        for pages in ([{"value": [], "@odata.nextLink": "/me/messages/message-one/attachments"}],
                      [{"wrong": []}], [{"value": [None]}]):
            with self.subTest(pages=pages):
                manifest, calls = self.graph(pages)
                self.assertFalse(manifest["complete"])
                self.assertEqual(len(calls), 1)

    def test_graph_skipped_and_unsupported_parts_mark_manifest_incomplete(self):
        cases = [graph_file(isInline=True), graph_file(name="calendar.ics"),
                 graph_file(name="small.png", contentType="image/png", size=10),
                 {"@odata.type": "#microsoft.graph.itemAttachment", "id": "message-att"}]
        for attachment in cases:
            with self.subTest(attachment=attachment):
                manifest, _ = self.graph([{"value": [attachment]}])
                self.assertFalse(manifest["complete"])
                self.assertEqual(manifest["ids"], [])

    def test_graph_decode_failure_is_not_an_empty_complete_manifest(self):
        manifest, _ = self.graph([{"value": [graph_file(contentBytes="!not-base64!")]}])
        self.assertFalse(manifest["complete"])
        self.assertEqual(len(manifest["ids"]), 1)
        self.assertEqual(manifest["files"], {})

    def test_graph_duplicate_pages_do_not_duplicate_manifest_identity(self):
        manifest, _ = self.graph([
            {"value": [graph_file()], "@odata.nextLink": "/me/messages/message-one/attachments?$skip=1"},
            {"value": [graph_file()]},
        ])
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(manifest["ids"]), 1)

    def test_imap_complete_files_keep_exact_source_association(self):
        message = self.message()
        message.add_attachment(b"agenda", maintype="application", subtype="pdf", filename="agenda.pdf")
        manifest = self.imap(message)
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(manifest["ids"]), 1)
        self.assertEqual(Path(next(iter(manifest["files"].values()))).read_bytes(), b"agenda")

    def test_imap_skipped_small_or_inline_files_are_incomplete(self):
        for name, maintype, subtype, disposition in (("tiny.png", "image", "png", "attachment"),
                ("inline.pdf", "application", "pdf", "inline"), ("event.ics", "text", "calendar", "attachment")):
            with self.subTest(name=name):
                message = self.message()
                message.add_attachment(b"small", maintype=maintype, subtype=subtype,
                                       filename=name, disposition=disposition)
                manifest = self.imap(message)
                self.assertFalse(manifest["complete"])
                self.assertEqual(manifest["ids"], [])

    def test_imap_bad_base64_part_marks_coverage_incomplete(self):
        message = self.message()
        message.add_attachment(b"agenda", maintype="application", subtype="pdf", filename="agenda.pdf")
        part = list(message.iter_attachments())[0]
        part.set_payload("!bad-base64!")
        manifest = self.imap(message)
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["ids"], [])

    def test_imap_decode_exception_cannot_disappear_from_coverage(self):
        part = mock.Mock()
        part.get_content_maintype.return_value = "application"
        part.get_filename.return_value = "agenda.pdf"
        part.get.return_value = "attachment"
        part.get_payload.side_effect = ValueError("synthetic decoding error")
        message = mock.Mock()
        message.walk.return_value = [part]
        coverage = {"complete": True}
        self.assertEqual(list(mailindex._msg_attachments(message, coverage)), [])
        self.assertFalse(coverage["complete"])

    def test_imap_zero_byte_file_is_preserved(self):
        message = self.message()
        message.add_attachment(b"", maintype="application", subtype="octet-stream", filename="empty.bin")
        manifest = self.imap(message)
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(manifest["ids"]), 1)
        self.assertEqual(Path(next(iter(manifest["files"].values()))).read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()

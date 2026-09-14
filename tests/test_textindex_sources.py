"""Exact local message references and additive email resource indexing.

Every index and mailbox here is synthetic. Provider calls are mocked and
fixture/sandbox cases assert that SQLite is never opened.
"""
from contextlib import closing
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import msgraph, textindex
from server.imessage import apple_ns
from tests.test_textindex import FakeIMAP


ACCOUNT = "owner@example.test"
STAMP = apple_ns(datetime(2026, 9, 10, 14, tzinfo=timezone.utc))
PAY_URL = "https://billing.example.test/pay?invoice=abc&signature=xyz"


class SourceIndexFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "text-index.sqlite"
        self.account = {"email": ACCOUNT, "host": "imap.example.test"}
        for patcher in (
            mock.patch.object(textindex, "DB", self.path),
            mock.patch.object(textindex.settings, "fixture_mode", return_value=False),
            mock.patch.object(textindex.settings, "sandboxed", return_value=False),
            mock.patch.object(textindex.crm, "resolve_handle", return_value=None),
            mock.patch.object(textindex.channels, "mail_accounts", return_value=[self.account]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def insert(self, uid, *, source="email", metadata=None, text="Please review this message."):
        con = textindex._db()
        try:
            added = textindex._insert(
                con, uid=uid, source=source, account=ACCOUNT, text=text,
                date_ns=STAMP, sender_handle="sender@example.test",
                chat_pid="p_example", chat_id=7, subject="Account notice",
                source_meta=metadata)
            con.commit()
            return added
        finally:
            con.close()

    def legacy_index(self):
        con = sqlite3.connect(self.path)
        con.executescript("""
            CREATE TABLE items(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE,
              source TEXT, account TEXT, chat_id INTEGER, is_group INTEGER,
              from_me INTEGER, sender_pid TEXT, chat_pid TEXT, sender_handle TEXT,
              date_ns INTEGER, subject TEXT, text TEXT, pending INTEGER DEFAULT 1);
            CREATE VIRTUAL TABLE fts USING fts5(text, subject);
            CREATE TABLE vec_text(seq INTEGER PRIMARY KEY, v BLOB);
            CREATE TABLE state(key TEXT PRIMARY KEY, val TEXT);
        """)
        ids = ["mail:<legacy@example.test>", "mail:opaque-provider-id",
               f"mail:{ACCOUNT}:42", "imsg:91"]
        for uid in ids:
            source = "imessage" if uid.startswith("imsg:") else "email"
            cur = con.execute(
                "INSERT INTO items(uid,source,account,date_ns,subject,text,pending) "
                "VALUES(?,?,?,?,?,?,0)", (uid, source, ACCOUNT, STAMP, "Legacy", "legacy body"))
            con.execute("INSERT INTO fts(rowid,text,subject) VALUES(?,?,?)",
                        (cur.lastrowid, "legacy body", "Legacy"))
        con.execute("INSERT INTO vec_text(seq,v) VALUES(1,?)", (b"unchanged",))
        con.execute("INSERT INTO state VALUES('wm_mail:owner@example.test','42')")
        con.commit()
        con.close()
        return ids


class SourceIndexTests(SourceIndexFixture):
    def test_lookup_is_exact_and_returns_full_body_with_identity(self):
        source_id = "mail:<message-1@example.test>"
        body = "Full source body. " * 100
        self.insert(source_id, text=body)
        self.insert("mail:<message-10@example.test>")
        found = textindex.lookup_sources([source_id, "mail:<message-", "Account notice", "' OR 1=1 --"])
        self.assertEqual(list(found), [source_id])
        row = found[source_id]
        self.assertEqual(row["id"], source_id)
        self.assertEqual(row["text"], body)
        self.assertEqual(row["message_id"], "<message-1@example.test>")
        self.assertEqual(row["account"], ACCOUNT)
        self.assertEqual(row["handle"], "sender@example.test")
        self.assertEqual(row["person_id"], "p_example")
        self.assertEqual(row["channel"], "email")
        self.assertEqual(row["subject"], "Account notice")
        self.assertEqual(row["chat_id"], 7)
        self.assertIsNotNone(row["when"])

    def test_legacy_lookup_is_read_only_and_reconstructs_only_unambiguous_ids(self):
        ids = self.legacy_index()
        before = self.path.read_bytes()
        with mock.patch.object(textindex, "_db", side_effect=AssertionError("write connection")):
            rows = textindex.lookup_sources(ids)
            delta = textindex.changes_since()["items"]
        self.assertEqual(rows[ids[0]]["message_id"], "<legacy@example.test>")
        for uid in ids[1:3]:
            self.assertNotIn("message_id", rows[uid])
            self.assertNotIn("graph_id", rows[uid])
            self.assertNotIn("rowid", rows[uid])
        self.assertEqual(rows["imsg:91"]["rowid"], 91)
        self.assertEqual({row["id"]: row for row in delta}, rows)
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertNotIn("source_meta", {r[1] for r in con.execute("PRAGMA table_info(items)")})

    def test_migration_keeps_old_rows_fts_vectors_and_watermarks(self):
        ids = self.legacy_index()
        with closing(sqlite3.connect(self.path)) as con:
            old_rows = con.execute("SELECT * FROM items ORDER BY seq").fetchall()
        new_id = "mail:<new@example.test>"
        self.assertEqual(self.insert(new_id, metadata={"graph_id": "new-full-id"}), 1)
        with closing(sqlite3.connect(self.path)) as con:
            rows = con.execute("SELECT * FROM items ORDER BY seq").fetchall()
            self.assertEqual([row[:-1] for row in rows[:-1]], old_rows)
            self.assertTrue(all(row[-1] is None for row in rows[:-1]))
            self.assertEqual(con.execute("SELECT v FROM vec_text WHERE seq=1").fetchone()[0], b"unchanged")
            self.assertEqual(con.execute("SELECT val FROM state").fetchone()[0], "42")
            self.assertEqual(con.execute("SELECT count(*) FROM fts WHERE fts MATCH 'legacy'").fetchone()[0], 4)
        # Normal overlap must not update previously indexed source records.
        self.assertEqual(self.insert(ids[0], metadata={"graph_id": "invented"}, text="changed"), 0)
        legacy = textindex.lookup_sources([ids[0]])[ids[0]]
        self.assertEqual(legacy["text"], "legacy body")
        self.assertNotIn("graph_id", legacy)

    def test_metadata_is_additive_and_cannot_override_source_identity(self):
        uid = "mail:full-graph-id"
        self.insert(uid, metadata={"graph_id": "full-graph-id", "id": "wrong",
                                   "text": "wrong", "account": "wrong@example.test",
                                   "rowid": f"mail-{ACCOUNT}-42", "links": []})
        row = textindex.lookup_sources([uid])[uid]
        self.assertEqual(row["graph_id"], "full-graph-id")
        self.assertEqual(row["id"], uid)
        self.assertEqual(row["account"], ACCOUNT)
        self.assertNotEqual(row["text"], "wrong")
        self.assertNotIn("rowid", row)
        self.assertNotIn("message_id", row)

    def test_malformed_metadata_does_not_hide_source_or_guess_graph_identity(self):
        self.insert("mail:opaque-id")
        for bad in ("{bad json", '[]', '"wrong type"'):
            with closing(sqlite3.connect(self.path)) as con:
                con.execute("UPDATE items SET source_meta=?", (bad,))
                con.commit()
            row = textindex.lookup_sources(["mail:opaque-id"])["mail:opaque-id"]
            self.assertEqual(row["text"], "Please review this message.")
            self.assertEqual(row["links"], [])
            self.assertNotIn("graph_id", row)

    def test_missing_index_does_not_create_a_directory_or_file(self):
        missing = Path(self.tmp.name) / "absent" / "text-index.sqlite"
        with mock.patch.object(textindex, "DB", missing), \
             mock.patch.object(textindex.sqlite3, "connect", side_effect=AssertionError("opened index")):
            self.assertEqual(textindex.lookup_sources(["imsg:1"]), {})
            self.assertFalse(textindex.changes_since()["available"])
        self.assertFalse(missing.parent.exists())

    def test_fixture_and_sandbox_never_open_existing_index(self):
        self.insert("imsg:1", source="imessage")
        for mode in ("fixture_mode", "sandboxed"):
            with self.subTest(mode=mode), \
                 mock.patch.object(textindex.settings, mode, return_value=True), \
                 mock.patch.object(textindex.sqlite3, "connect", side_effect=AssertionError("opened index")):
                self.assertEqual(textindex.lookup_sources(["imsg:1"]), {})
                self.assertEqual(textindex.changes_since(cursor=7),
                                 {"items": [], "cursor": 7, "available": False})

    def test_empty_or_oversized_lookup_does_not_open_index(self):
        self.insert("imsg:1", source="imessage")
        with mock.patch.object(textindex.sqlite3, "connect", side_effect=AssertionError("opened index")):
            self.assertEqual(textindex.lookup_sources([]), {})
            with self.assertRaises(ValueError):
                textindex.lookup_sources([f"imsg:{i}" for i in range(textindex.SOURCE_LOOKUP_BATCH + 1)])
            with self.assertRaises(ValueError):
                textindex.lookup_sources([None])

    def test_readonly_uri_escapes_path_characters(self):
        filename = "index #.sqlite" if os.name == "nt" else "index ?#.sqlite"
        with mock.patch.object(textindex, "DB", Path(self.tmp.name) / filename):
            self.insert("imsg:1", source="imessage")
            self.assertEqual(textindex.lookup_sources(["imsg:1"])["imsg:1"]["rowid"], 1)


class EmailResourceIndexTests(SourceIndexFixture):
    def graph_message(self, mid="<invoice@example.test>"):
        return {"id": "full-graph-object-id", "internetMessageId": mid,
                "webLink": "https://outlook.office.com/mail/id/full-graph-object-id",
                "subject": "Invoice ready", "receivedDateTime": "2026-09-10T14:00:00Z",
                "from": {"emailAddress": {"address": "billing@example.test"}},
                "toRecipients": [], "body": {"contentType": "html", "content":
                    '<p>Your invoice is ready.</p><a href="' + PAY_URL.replace("&", "&amp;")
                    + '">Pay invoice</a>'}}

    def test_graph_retains_full_ids_html_links_and_delta_metadata(self):
        msg = self.graph_message()
        with mock.patch.object(msgraph, "_graph_request", return_value={"value": [msg]}) as provider:
            self.assertEqual(textindex.backfill_graph(ACCOUNT, log=lambda *a: None), 1)
        self.assertIn("webLink", provider.call_args.args[1])
        uid = "mail:<invoice@example.test>"
        with mock.patch.object(msgraph, "_graph_request", side_effect=AssertionError("network read")):
            row = textindex.lookup_sources([uid])[uid]
            delta = textindex.changes_since()["items"][0]
        self.assertEqual(row, delta)
        self.assertEqual(row["message_id"], msg["internetMessageId"])
        self.assertEqual(row["graph_id"], msg["id"])
        self.assertEqual(row["web_link"], msg["webLink"])
        self.assertNotIn("rowid", row)
        self.assertNotIn(PAY_URL, row["text"])
        self.assertEqual(row["links"], [{"kind": "payment", "label": "Pay invoice",
                                        "url": PAY_URL, "domain": "billing.example.test"}])
        with closing(sqlite3.connect(self.path)) as con:
            metadata = json.loads(con.execute("SELECT source_meta FROM items").fetchone()[0])
            self.assertNotIn("html", metadata)
            self.assertEqual(con.execute("SELECT val FROM state").fetchone()[0], "2026-09-10T14:00:00Z")

    def test_graph_without_message_id_keeps_explicit_full_graph_id(self):
        msg = self.graph_message(mid="")
        with mock.patch.object(msgraph, "_graph_request", return_value={"value": [msg]}):
            textindex.backfill_graph(ACCOUNT, log=lambda *a: None)
        row = textindex.lookup_sources(["mail:full-graph-object-id"])["mail:full-graph-object-id"]
        self.assertEqual(row["graph_id"], "full-graph-object-id")
        self.assertNotIn("message_id", row)

    def test_graph_plain_text_links_are_retained_before_body_truncation(self):
        msg = self.graph_message()
        msg["body"] = {"contentType": "text", "content": "x" * textindex.MAIL_BODY_MAX
                       + "\nPay your invoice at " + PAY_URL}
        with mock.patch.object(msgraph, "_graph_request", return_value={"value": [msg]}):
            textindex.backfill_graph(ACCOUNT, log=lambda *a: None)
        row = textindex.lookup_sources(["mail:<invoice@example.test>"])["mail:<invoice@example.test>"]
        self.assertEqual(len(row["text"]), textindex.MAIL_BODY_MAX)
        self.assertEqual(row["links"][0]["url"], PAY_URL)

    def imap_message(self, mid=None):
        msg = EmailMessage()
        msg["From"] = "billing@example.test"
        msg["To"] = ACCOUNT
        msg["Subject"] = "Invoice ready"
        msg["Date"] = "Thu, 10 Sep 2026 14:00:00 +0000"
        if mid:
            msg["Message-ID"] = mid
        msg.set_content("Your invoice is ready.")
        msg.add_alternative('<p>Your invoice is ready.</p><a href="' + PAY_URL.replace("&", "&amp;")
                            + '">Pay invoice</a>', subtype="html")
        msg.add_attachment(b'<a href="https://unrelated.example.test/pay">Pay</a>',
                           maintype="text", subtype="html", filename="attachment.html")
        return msg.as_bytes()

    def test_imap_keeps_html_link_and_mid_without_inventing_inbox_rowid(self):
        mailbox = {41: self.imap_message("<imap-invoice@example.test>"),
                   42: self.imap_message()}
        with mock.patch.object(textindex.imaplib, "IMAP4_SSL", return_value=FakeIMAP(mailbox)), \
             mock.patch.object(textindex.channels, "imap_special_folder", return_value="[Example]/All Mail"), \
             mock.patch.object(textindex.mailmod, "keychain_password", return_value="synthetic-password"):
            self.assertEqual(textindex.backfill_imap(self.account, log=lambda *a: None), 2)
        ids = ["mail:<imap-invoice@example.test>", f"mail:{ACCOUNT}:42"]
        rows = textindex.lookup_sources(ids)
        self.assertEqual(rows[ids[0]]["message_id"], "<imap-invoice@example.test>")
        self.assertNotIn("message_id", rows[ids[1]])
        for uid, number in zip(ids, (41, 42)):
            row = rows[uid]
            self.assertNotIn("rowid", row)
            self.assertEqual(row["imap_uid"], number)
            self.assertEqual(row["mailbox"], "[Example]/All Mail")
            self.assertEqual([link["url"] for link in row["links"]], [PAY_URL])
            self.assertEqual(row["text"], "Your invoice is ready.")
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT val FROM state").fetchone()[0], "42")


if __name__ == "__main__":
    unittest.main()

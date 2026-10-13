"""Index from the mail archive: one download per message.

A 319,576-message Gmail history under a daily download cap cannot be
walked three times (bodies, attachments, archive). The archive fetches
each message once; the text index and the attachment index read its
files for the accounts it covers. A tmp archive, a tmp text index and a
tmp media index; every provider call for a covered account is a mock
that fails the test if reached.
"""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import data as crm  # noqa: E402
from server import mailarchive, mailindex, mediaindex, textindex  # noqa: E402

GMAIL = "owner@example.test"
WORK = "owner@work.example.test"
PAY_URL = "https://billing.example.test/pay?invoice=1"


def raw_message(mid, body="Your invoice is ready.", attach=True,
                date="Thu, 10 Sep 2026 14:00:00 +0000"):
    msg = EmailMessage()
    msg["From"] = "billing@example.test"
    msg["To"] = GMAIL
    msg["Subject"] = "Invoice ready"
    msg["Date"] = date
    if mid:
        msg["Message-ID"] = mid
    msg.set_content(body + "\nPay at " + PAY_URL)
    if attach:
        msg.add_attachment(b"%PDF-1.4 synthetic invoice " * 300, maintype="application",
                           subtype="pdf", filename="invoice.pdf")
    return msg.as_bytes()


def archive(account, raws, start_uid=1):
    """Store raw messages the way a sweep would, in order, and mark the
    account covered."""
    con = mailarchive._db()
    try:
        store = mailarchive._Store(con)
        for i, raw in enumerate(raws, start_uid):
            store.put(account=account, raw=raw, fallback_key=f"{account}|uid:1:{i}")
        mailarchive.set_state(con, f"wm:{account}", start_uid + len(raws) - 1)
        mailarchive.set_state(con, f"low:{account}", 1)
        con.commit()
    finally:
        con.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.enabled = True
        self.accounts = [{"email": GMAIL, "host": "imap.gmail.com"},
                         {"email": WORK, "type": "graph"}]
        self.graph_calls = []
        patches = [
            mock.patch.object(mailarchive, "root", lambda: self.dir / "archive"),
            mock.patch.object(mailarchive, "enabled", lambda: self.enabled),
            mock.patch.object(textindex, "DB", self.dir / "text-index.sqlite"),
            mock.patch.object(textindex.channels, "mail_accounts", lambda *a, **k: self.accounts),
            mock.patch.object(textindex, "_my_addresses", lambda: {GMAIL, WORK}),
            mock.patch.object(textindex.imaplib, "IMAP4_SSL",
                              side_effect=AssertionError("provider fetch for an archived account")),
            mock.patch.object(textindex, "backfill_graph",
                              lambda account, **kw: self.graph_calls.append(account) or 0),
            mock.patch.object(textindex.mailmod, "keychain_password", lambda addr: "synthetic"),
            mock.patch.object(mediaindex, "_DATA", self.dir),
            mock.patch.object(mediaindex, "DB", self.dir / "media.sqlite"),
            mock.patch.object(mailindex, "ATTACH_DIR", self.dir / "attachments"),
            mock.patch.object(mailindex, "_accounts", lambda: self.accounts),
            mock.patch.object(mailindex, "_my_addresses", lambda: {GMAIL, WORK}),
            mock.patch.object(mailindex.imaplib, "IMAP4_SSL",
                              side_effect=AssertionError("provider fetch for an archived account")),
            mock.patch.object(mailindex, "_run_graph",
                              lambda addr, *a, **k: self.graph_calls.append(addr) or 0),
            mock.patch.object(mailindex.mailmod, "keychain_password", lambda addr: "synthetic"),
            mock.patch.object(crm, "resolve_handle", lambda h: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def text_rows(self):
        with closing(sqlite3.connect(textindex.DB)) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(
                "SELECT uid, account, text, subject, source_meta FROM items ORDER BY seq")]

    def text_state(self, key):
        with closing(sqlite3.connect(textindex.DB)) as con:
            row = con.execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


class ArchiveFeed(_Base):
    def test_the_first_shape_is_migrated_to_carry_a_sequence(self):
        root = self.dir / "archive"
        root.mkdir()
        with closing(sqlite3.connect(root / "index.sqlite")) as con:
            con.executescript("""
                CREATE TABLE messages(key TEXT PRIMARY KEY, account TEXT NOT NULL,
                  message_id TEXT, sha TEXT NOT NULL, size INTEGER, date TEXT, stored TEXT);
                CREATE TABLE state(key TEXT PRIMARY KEY, val TEXT);
                INSERT INTO messages VALUES ('a|<2>', 'a', '<2>', 'ff', 1, '2026-01-02', 'x');
                INSERT INTO messages VALUES ('a|<1>', 'a', '<1>', 'ee', 1, '2026-01-01', 'x');
            """)
        con = mailarchive._db()
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
            self.assertEqual(cols[:2], ["seq", "key"])
            self.assertEqual([tuple(r) for r in con.execute(
                "SELECT seq, key FROM messages ORDER BY seq")], [(1, "a|<2>"), (2, "a|<1>")])
        finally:
            con.close()

    def test_covers_and_the_feed_follow_a_cursor(self):
        self.assertFalse(mailarchive.covers(GMAIL))
        a, b = raw_message("<a@example.test>"), raw_message("<b@example.test>")
        archive(GMAIL, [a, b])
        self.assertTrue(mailarchive.covers(GMAIL))
        self.assertFalse(mailarchive.covers(WORK))
        rows = list(mailarchive.iter_archived(account=GMAIL))
        self.assertEqual([(r["seq"], r["message_id"]) for r in rows],
                         [(1, "<a@example.test>"), (2, "<b@example.test>")])
        self.assertEqual(mailarchive.message_bytes(rows[0]), a)
        self.assertEqual([r["seq"] for r in mailarchive.iter_archived(account=GMAIL, after_seq=1)], [2])
        self.assertEqual([r["seq"] for r in mailarchive.iter_archived(limit=1)], [1])
        self.enabled = False
        self.assertFalse(mailarchive.covers(GMAIL))


class TextIndexFromArchive(_Base):
    def test_bodies_come_from_the_archive_and_the_provider_is_never_asked(self):
        archive(GMAIL, [raw_message(f"<{i}@example.test>") for i in range(1, 4)])
        said = []
        self.assertEqual(textindex.backfill_mail(log=said.append), 3)
        rows = self.text_rows()
        self.assertEqual([r["uid"] for r in rows],
                         ["mail:<1@example.test>", "mail:<2@example.test>", "mail:<3@example.test>"])
        meta = __import__("json").loads(rows[0]["source_meta"])
        self.assertEqual(meta["message_id"], "<1@example.test>")
        self.assertEqual(meta["archive_key"], f"{GMAIL}|<1@example.test>")
        self.assertEqual(len(meta["archive_sha"]), 64)
        self.assertIs(meta["body_complete"], True)
        self.assertIs(meta["has_attachments"], True)
        self.assertEqual(meta["links"][0]["url"], PAY_URL)
        self.assertNotIn("imap_uid", meta)
        self.assertIn("Your invoice is ready.", rows[0]["text"])
        self.assertEqual(self.text_state(f"archive_seq:{GMAIL}"), "3")
        self.assertEqual(self.graph_calls, [WORK])            # Graph keeps its own walk
        self.assertFalse(any("provider fetch" in m for m in said), said)
        self.assertIn(f"archive_seq:{GMAIL}", textindex._mail_watermarks())
        # Nothing new: nothing indexed. One more archived: one more row.
        self.assertEqual(textindex.backfill_mail(log=lambda *a: None), 0)
        archive(GMAIL, [raw_message("<4@example.test>")], start_uid=4)
        self.assertEqual(textindex.backfill_mail(log=lambda *a: None), 1)
        self.assertEqual(self.text_state(f"archive_seq:{GMAIL}"), "4")

    def test_a_message_without_an_id_gets_the_uid_the_provider_walk_gives(self):
        archive(GMAIL, [raw_message(None)], start_uid=7)
        self.assertEqual(textindex.backfill_mail_from_archive(log=lambda *a: None), 1)
        self.assertEqual(self.text_rows()[0]["uid"], f"mail:{GMAIL}:7")

    def test_the_tick_takes_archived_mail_in_bounded_passes(self):
        archive(GMAIL, [raw_message(f"<{i}@example.test>") for i in range(1, 6)])
        self.assertEqual(textindex.backfill_mail(log=lambda *a: None, archive_limit=2), 2)
        self.assertEqual(self.text_state(f"archive_seq:{GMAIL}"), "2")
        self.assertEqual(textindex.backfill_mail(log=lambda *a: None, archive_limit=2), 2)
        self.assertEqual(textindex.backfill_mail(log=lambda *a: None, archive_limit=2), 1)

    def test_an_uncovered_imap_account_still_walks_the_provider(self):
        self.enabled = False
        said = []
        textindex.backfill_mail(log=said.append)
        self.assertTrue(any("provider fetch" in m for m in said), said)


class AttachmentsFromArchive(_Base):
    def manifest(self):
        with closing(sqlite3.connect(mediaindex.DB)) as con:
            return {r[0]: (r[1], bool(r[2])) for r in con.execute(
                "SELECT source_id, attachment_ids, complete FROM mail_attachment_sources")}

    def media_state(self, key):
        with closing(sqlite3.connect(mediaindex.DB)) as con:
            row = con.execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def test_attachments_come_from_the_archive_with_stable_ids(self):
        archive(GMAIL, [raw_message("<a@example.test>"), raw_message("<b@example.test>", attach=False),
                        raw_message("<c@example.test>")])
        said = []
        self.assertEqual(mailindex.backfill(log=said.append), 2)
        self.assertFalse(any("provider fetch" in m for m in said), said)
        self.assertEqual(self.graph_calls, [WORK])
        manifest = self.manifest()
        self.assertEqual(set(manifest), {"mail:<a@example.test>", "mail:<b@example.test>",
                                         "mail:<c@example.test>"})
        self.assertTrue(all(complete for _, complete in manifest.values()))
        self.assertEqual(manifest["mail:<b@example.test>"][0], "[]")
        files = list((self.dir / "attachments").rglob("*.pdf"))
        self.assertEqual(len(files), 2)
        self.assertEqual(self.media_state(f"archive_seq:{GMAIL}"), "3")
        # A rerun and the incremental tick find nothing new; ids are stable.
        self.assertEqual(mailindex.backfill(log=lambda *a: None), 0)
        self.assertEqual(mailindex.incremental(log=lambda *a: None), 0)
        with closing(sqlite3.connect(mediaindex.DB)) as con:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM items WHERE source='email'").fetchone()[0], 2)

    def test_since_skips_older_archived_mail(self):
        archive(GMAIL, [raw_message("<old@example.test>", date="Mon, 1 Oct 2012 09:00:00 +0000"),
                        raw_message("<new@example.test>")])
        self.assertEqual(mailindex.backfill(since="2020-01-01", log=lambda *a: None), 1)
        self.assertEqual(set(self.manifest()), {"mail:<new@example.test>"})

    def test_an_uncovered_imap_account_still_walks_the_provider(self):
        self.enabled = False
        said = []
        mailindex.backfill(log=said.append)
        self.assertTrue(any("provider fetch" in m for m in said), said)


if __name__ == "__main__":
    unittest.main()

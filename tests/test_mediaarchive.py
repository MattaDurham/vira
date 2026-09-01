"""Vira's own copy of the attachments macOS evicts.

Everything here is rooted at ONE tmp archive and ONE synthetic chat.db.
The module reads three things outside its own store — the config, the
archive root, and chat.db — so `test_an_empty_fixture_archives_nothing`
is the isolation guard: a source added later that reaches the real
machine instead of the fixture fails it on sight.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import mediaarchive  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "archive"
        self.src = self.dir / "src"
        self.src.mkdir()
        self.chat = self.dir / "chat.db"
        self._make_chatdb()
        self.cap = 0
        patches = [
            mock.patch.object(mediaarchive, "root", lambda: self.root),
            mock.patch.object(mediaarchive, "enabled", lambda: True),
            mock.patch.object(mediaarchive, "max_bytes", lambda: self.cap),
            mock.patch.object(mediaarchive, "_connect", self._connect),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        os.environ.pop("VIRA_PASSIVE", None)

    def _make_chatdb(self):
        con = sqlite3.connect(self.chat)
        con.execute("CREATE TABLE attachment(ROWID INTEGER PRIMARY KEY, "
                    "filename TEXT, mime_type TEXT, transfer_name TEXT)")
        con.commit()
        con.close()

    def _connect(self):
        return sqlite3.connect(self.chat)

    def add(self, att_id, name, body=b"bytes", mime="image/jpeg",
            on_disk=True):
        """Register an attachment; optionally write its file."""
        p = self.src / name
        if on_disk:
            p.write_bytes(body)
        con = sqlite3.connect(self.chat)
        con.execute("INSERT INTO attachment(ROWID, filename, mime_type, "
                    "transfer_name) VALUES (?,?,?,?)",
                    (att_id, str(p), mime, name))
        con.commit()
        con.close()
        return p


class Isolation(_Base):
    def test_an_empty_fixture_archives_nothing(self):
        """The guard. No attachments in the fixture chat.db and no files in
        the fixture tree means zero archived — if this ever passes a nonzero
        count, something is reading the real machine."""
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 0)
        self.assertEqual(mediaarchive.stats()["files"], 0)

    def test_the_real_archive_root_is_never_touched(self):
        self.add(1, "a.jpg")
        mediaarchive.sweep(log=lambda *a: None)
        # everything landed inside the fixture, nothing beside it
        self.assertTrue((self.root / "index.sqlite").exists())
        self.assertTrue(str(self.root).startswith(str(self.dir)))


class StoreAndServe(_Base):
    def test_a_stored_attachment_comes_back(self):
        p = self.add(1, "photo.jpg", b"hello")
        sha = mediaarchive.store(1, p, "image/jpeg", "photo.jpg")
        got, mime, name = mediaarchive.lookup(1)
        self.assertIsNotNone(got)
        self.assertEqual(got.read_bytes(), b"hello")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(name, "photo.jpg")
        self.assertEqual(len(sha), 64)

    def test_identical_bytes_are_stored_once(self):
        """One photo sent to three people is one blob — the whole reason the
        archive is content-addressed rather than keyed by attachment."""
        a = self.add(1, "a.jpg", b"same")
        b = self.add(2, "b.jpg", b"same")
        s1 = mediaarchive.store(1, a)
        s2 = mediaarchive.store(2, b)
        self.assertEqual(s1, s2)
        blobs = list((self.root / "blobs").rglob("*"))
        self.assertEqual(len([x for x in blobs if x.is_file()]), 1)
        # ...and both attachments still resolve to it
        self.assertEqual(mediaarchive.lookup(1)[0], mediaarchive.lookup(2)[0])

    def test_storing_twice_is_idempotent(self):
        p = self.add(1, "a.jpg", b"x")
        first = mediaarchive.store(1, p)
        second = mediaarchive.store(1, p)
        self.assertEqual(first, second)
        self.assertEqual(mediaarchive.stats()["files"], 1)

    def test_an_unarchived_attachment_resolves_to_nothing(self):
        self.assertEqual(mediaarchive.lookup(999), (None, None, None))
        self.assertIsNone(mediaarchive.file_for(999))

    def test_no_temp_file_survives_a_store(self):
        p = self.add(1, "a.jpg", b"x")
        mediaarchive.store(1, p)
        leftovers = list((self.root / "tmp").glob("*")) \
            if (self.root / "tmp").exists() else []
        self.assertEqual(leftovers, [])


class Containment(_Base):
    def test_a_non_hash_addresses_nothing(self):
        """blob_path validates rather than trusts: the index is a file on
        disk, and a poisoned row must not address anything outside the
        archive."""
        for bad in ("../../etc/passwd", "", "zz" * 32, "abc", None):
            self.assertIsNone(mediaarchive.blob_path(bad))

    def test_a_valid_hash_stays_under_the_root(self):
        sha = "a" * 64
        p = mediaarchive.blob_path(sha)
        self.assertIsNotNone(p)
        p.resolve().relative_to(self.root.resolve())   # raises if it escapes

    def test_a_poisoned_row_is_refused_not_served(self):
        outside = self.dir / "secret.txt"
        outside.write_bytes(b"nope")
        con = mediaarchive._db()
        con.execute("INSERT INTO blobs(att_id, sha, size, mime, name, stored) "
                    "VALUES (?,?,?,?,?,?)",
                    (7, "../../../secret.txt", 4, "text/plain", "x", "now"))
        con.commit()
        con.close()
        self.assertEqual(mediaarchive.lookup(7), (None, None, None))


class HaveMany(_Base):
    def test_it_reports_only_what_is_held(self):
        a = self.add(1, "a.jpg", b"a")
        self.add(2, "b.jpg", b"b")
        mediaarchive.store(1, a)
        self.assertEqual(mediaarchive.have_many([1, 2, 3]), {1})

    def test_empty_input_asks_nothing(self):
        self.assertEqual(mediaarchive.have_many([]), set())

    def test_it_chunks_past_the_sqlite_variable_limit(self):
        """The busiest conversation here carries ~4,500 attachments; sqlite caps
        host variables, so the query is chunked."""
        p = self.add(1, "a.jpg", b"a")
        mediaarchive.store(1, p)
        self.assertEqual(mediaarchive.have_many(range(1, 3000)), {1})


class Sweep(_Base):
    def test_it_archives_what_is_on_disk(self):
        self.add(1, "a.jpg", b"a")
        self.add(2, "b.jpg", b"b")
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 2)
        self.assertEqual(mediaarchive.have_many([1, 2]), {1, 2})

    def test_it_skips_what_macos_already_evicted(self):
        """The evicted file is the whole problem — there is nothing to copy,
        and it must not read as an error."""
        self.add(1, "gone.jpg", on_disk=False)
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 0)

    def test_it_skips_zero_byte_placeholders(self):
        self.add(1, "stub.jpg", b"")
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 0)

    def test_it_skips_plugin_payloads(self):
        """3,687 of this machine's attachments (0.66 GB) are sticker and
        plugin payloads — noise the media surfaces already drop."""
        self.add(1, "x.pluginPayloadAttachment", b"junk")
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 0)

    def test_a_second_pass_has_nothing_to_do(self):
        self.add(1, "a.jpg", b"a")
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 1)
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 0)

    def test_an_unreadable_file_does_not_stop_the_pass(self):
        self.add(1, "a.jpg", b"a")
        p = self.add(2, "b.jpg", b"b")
        real = mediaarchive._copy_in
        calls = []

        def flaky(src):
            calls.append(src)
            if Path(src).name == "a.jpg":
                raise OSError("unreadable")
            return real(src)

        with mock.patch.object(mediaarchive, "_copy_in", flaky):
            n = mediaarchive.sweep(log=lambda *a: None)
        self.assertEqual(n, 1)
        self.assertEqual(len(calls), 2)     # it kept going

    def test_the_limit_reports_what_it_left(self):
        """No silent caps: the sweep says how many it did not take."""
        for i in range(1, 4):
            self.add(i, f"{i}.jpg", bytes([i]))
        said = []
        n = mediaarchive.sweep(log=said.append, limit=2)
        self.assertEqual(n, 2)
        self.assertTrue(any("left for the next pass" in m for m in said), said)


class Cap(_Base):
    def test_a_reached_cap_is_reported_not_silent(self):
        self.cap = 1               # 1 byte: anything already held fills it
        first = self.add(1, "a.jpg", b"aaaa")
        mediaarchive.store(1, first)
        second = self.add(2, "b.jpg", b"bbbb")
        with self.assertRaises(mediaarchive.ArchiveFull):
            mediaarchive.store(2, second)

    def test_the_sweep_names_what_it_could_not_take(self):
        self.cap = 1
        self.add(1, "a.jpg", b"aaaa")
        mediaarchive.sweep(log=lambda *a: None)
        self.add(2, "b.jpg", b"bbbb")
        said = []
        mediaarchive.sweep(log=said.append)
        self.assertTrue(any("not archived" in m for m in said), said)

    def test_deduped_bytes_do_not_count_against_the_cap(self):
        """What `new` in _copy_in is actually for. Content addressing makes
        the dedupe structural — the path IS the hash — so the flag's only
        job is the budget: the same photo sent to three people must cost
        the cap once, not three times."""
        self.cap = 300
        for i in range(1, 4):
            self.add(i, f"{i}.jpg", b"x" * 100)      # identical bytes
        self.add(9, "big.jpg", b"y" * 250)
        n = mediaarchive.sweep(log=lambda *a: None)
        # 3 duplicates cost 100 bytes total, leaving room for the 250
        self.assertEqual(n, 4)
        self.assertEqual(mediaarchive.stats()["blobs"], 2)
        self.assertEqual(mediaarchive.stats()["bytes"], 350)

    def test_no_cap_is_the_default(self):
        self.assertEqual(self.cap, 0)
        for i in range(1, 6):
            self.add(i, f"{i}.jpg", bytes([i]) * 100)
        self.assertEqual(mediaarchive.sweep(log=lambda *a: None), 5)


class Passive(_Base):
    """A test clone must never grow a duplicate archive, and with the root
    pointed at an external drive it would write into the owner's real one —
    the plans.py boundary. Reads stay open, which is what makes the surface
    testable on a branch."""

    def setUp(self):
        super().setUp()
        p = self.add(1, "a.jpg", b"a")
        mediaarchive.store(1, p)          # seed while still live
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(lambda: os.environ.pop("VIRA_PASSIVE", None))

    def test_store_refuses(self):
        p = self.add(2, "b.jpg", b"b")
        with self.assertRaises(PermissionError):
            mediaarchive.store(2, p)

    def test_the_sweep_is_a_no_op_and_says_so(self):
        self.add(2, "b.jpg", b"b")
        said = []
        self.assertEqual(mediaarchive.sweep(log=said.append), 0)
        self.assertTrue(any("passive" in m for m in said), said)

    def test_reads_still_work(self):
        self.assertIsNotNone(mediaarchive.file_for(1))


class UnreachableRoot(unittest.TestCase):
    """An archive on an unmounted external drive degrades to 'no copy'.
    Honest, and never an exception into a media listing."""

    def setUp(self):
        os.environ.pop("VIRA_PASSIVE", None)
        gone = Path("/nonexistent-volume-xyz/archive")
        p = mock.patch.object(mediaarchive, "root", lambda: gone)
        p.start()
        self.addCleanup(p.stop)

    def test_reads_degrade_quietly(self):
        self.assertEqual(mediaarchive.lookup(1), (None, None, None))
        self.assertEqual(mediaarchive.have_many([1, 2]), set())
        self.assertFalse(mediaarchive.stats()["available"])

    def test_the_sweep_says_why_it_did_nothing(self):
        with mock.patch.object(mediaarchive, "enabled", lambda: True):
            said = []
            self.assertEqual(mediaarchive.sweep(log=said.append), 0)
        self.assertTrue(any("unreachable" in m for m in said), said)


class Disabled(_Base):
    def test_it_archives_nothing_when_switched_off(self):
        self.add(1, "a.jpg", b"a")
        with mock.patch.object(mediaarchive, "enabled", lambda: False):
            said = []
            self.assertEqual(mediaarchive.sweep(log=said.append), 0)
        self.assertTrue(any("disabled" in m for m in said), said)


class Contracts(unittest.TestCase):
    def test_skip_names_match_the_media_surfaces(self):
        """media.py imports this module for the eviction fallback, so the
        skip list is duplicated rather than imported back. Pinned here so
        the two cannot drift."""
        from server import media
        self.assertEqual(mediaarchive.SKIP_NAMES, media.SKIP_NAMES)

    def test_every_config_key_it_reads_has_a_default(self):
        """settings.get raises KeyError on a key with no DEFAULTS entry (the
        mail_body_index incident)."""
        from server import settings
        for key in ("media_archive_enabled", "media_archive_root",
                    "media_archive_max_gb", "media_archive_interval_min"):
            self.assertIn(key, settings.DEFAULTS)

    def test_the_archiver_is_built_and_started(self):
        src = (Path(__file__).resolve().parent.parent
               / "server" / "main.py").read_text(encoding="utf-8")
        self.assertIn("mediaarchive.Archiver(", src)
        self.assertIn("media_archiver.start()", src)


if __name__ == "__main__":
    unittest.main()


class MediaSurfaces(_Base):
    """The join, not each half. The branch-first write guard was fully
    tested on both sides and silently disarmed for four days because the
    spec never travelled between them — so these drive media.py's real
    functions against a real archive."""

    def setUp(self):
        super().setUp()
        from server import media
        self.media = media
        # media.py resolves through chat.db and enforces its own containment
        # against ATTACH_ROOT, so the fixture's attachments must live there.
        self.attach = self.dir / "Attachments"
        self.attach.mkdir()
        for p in (mock.patch.object(media, "ATTACH_ROOT", self.attach),
                  mock.patch.object(media, "_connect", self._connect),
                  mock.patch.object(media, "THUMBS", self.dir / "thumbs")):
            p.start()
            self.addCleanup(p.stop)

    def _attach(self, att_id, name, body=b"\xff\xd8\xff data", mime="image/jpeg"):
        p = self.attach / name
        p.write_bytes(body)
        con = sqlite3.connect(self.chat)
        con.execute("INSERT INTO attachment(ROWID, filename, mime_type, "
                    "transfer_name) VALUES (?,?,?,?)",
                    (att_id, str(p), mime, name))
        con.commit()
        con.close()
        return p

    def test_an_archived_attachment_still_serves_after_eviction(self):
        """The whole point. macOS takes the original away; the viewer keeps
        working because Vira kept a copy."""
        p = self._attach(1, "photo.jpg")
        mediaarchive.store(1, p, "image/jpeg", "photo.jpg")
        p.unlink()                              # macOS evicts it
        got, mime, name = self.media.preview_file(1)
        self.assertIsNotNone(got, "evicted-but-archived served nothing")
        self.assertEqual(got.read_bytes(), b"\xff\xd8\xff data")
        self.assertEqual(mime, "image/jpeg")

    def test_an_unarchived_eviction_still_reports_honestly(self):
        p = self._attach(2, "gone.jpg")
        p.unlink()
        self.assertEqual(self.media.preview_file(2), (None, None, None))

    def test_the_original_still_wins_while_it_is_there(self):
        """The archive is a fallback, never a redirect: an attachment macOS
        still holds is served from where it lives."""
        p = self._attach(3, "here.jpg")
        mediaarchive.store(3, p, "image/jpeg", "here.jpg")
        got, _m, _n = self.media.preview_file(3)
        self.assertEqual(got, p.resolve())

    def test_bytes_path_prefers_disk_then_archive_then_nothing(self):
        p = self._attach(4, "x.jpg")
        self.assertEqual(self.media._bytes_path(4, p), p)
        mediaarchive.store(4, p)
        p.unlink()
        self.assertEqual(self.media._bytes_path(4, p).read_bytes(),
                         b"\xff\xd8\xff data")
        self.assertIsNone(self.media._bytes_path(999, self.attach / "nope"))


class EvictedFlag(_Base):
    """`evicted` drives the "in iCloud, not on this Mac" line in the viewer
    and on the person page. An archived attachment is fully serveable, so
    flagging it would be the surface lying about itself. Driven through the
    real media_for_chats rather than through have_many alone."""

    def setUp(self):
        super().setUp()
        from server import media
        self.media = media
        self.attach = self.dir / "Attachments"
        self.attach.mkdir()
        self._join_schema()
        for p in (mock.patch.object(media, "ATTACH_ROOT", self.attach),
                  mock.patch.object(media, "_connect", self._connect)):
            p.start()
            self.addCleanup(p.stop)

    def _join_schema(self):
        con = sqlite3.connect(self.chat)
        con.executescript("""
          ALTER TABLE attachment ADD COLUMN total_bytes INTEGER DEFAULT 0;
          ALTER TABLE attachment ADD COLUMN hide_attachment INTEGER DEFAULT 0;
          ALTER TABLE attachment ADD COLUMN is_sticker INTEGER DEFAULT 0;
          CREATE TABLE message(ROWID INTEGER PRIMARY KEY, is_from_me INTEGER,
                               date INTEGER, text TEXT, attributedBody BLOB,
                               payload_data BLOB, associated_message_type INT
                               DEFAULT 0, balloon_bundle_id TEXT,
                               cache_has_attachments INTEGER DEFAULT 1,
                               handle_id INTEGER DEFAULT 0);
          CREATE TABLE message_attachment_join(message_id INT, attachment_id INT);
          CREATE TABLE chat_message_join(chat_id INT, message_id INT);
        """)
        con.commit()
        con.close()

    def _shared(self, att_id, name, body=b"pix"):
        """An attachment shared in chat 1, wired through the join tables."""
        path = self.attach / name
        path.write_bytes(body)
        con = sqlite3.connect(self.chat)
        con.execute("INSERT INTO attachment(ROWID, filename, mime_type, "
                    "transfer_name, total_bytes) VALUES (?,?,?,?,?)",
                    (att_id, str(path), "image/jpeg", name, len(body)))
        con.execute("INSERT INTO message(ROWID, is_from_me, date, text) "
                    "VALUES (?,1,?,'')", (att_id, 700000000 * 10**9))
        con.execute("INSERT INTO message_attachment_join VALUES (?,?)",
                    (att_id, att_id))
        con.execute("INSERT INTO chat_message_join VALUES (1,?)", (att_id,))
        con.commit()
        con.close()
        return path

    def _photos(self):
        return {p["id"]: p for p in
                self.media.media_for_chats([1])["photos"]}

    def test_an_archived_attachment_is_not_flagged_evicted(self):
        kept = self._shared(1, "kept.jpg")
        mediaarchive.store(1, kept, "image/jpeg", "kept.jpg")
        kept.unlink()                       # macOS takes the original
        self.assertNotIn("evicted", self._photos()[1])

    def test_an_unarchived_eviction_is_still_flagged(self):
        gone = self._shared(2, "gone.jpg")
        gone.unlink()
        self.assertTrue(self._photos()[2].get("evicted"))

    def test_a_file_still_on_disk_is_not_flagged(self):
        self._shared(3, "here.jpg")
        self.assertNotIn("evicted", self._photos()[3])

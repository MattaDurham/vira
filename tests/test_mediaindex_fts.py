"""The media index's FTS5 table stays sound under every writer Vira has,
and heals itself when it is not.

Two "corruptions" of data/media-index.sqlite (2026-10-16 and 2026-10-23)
were one SQLite build reading an index another build wrote: SQLite 3.54.0's
unicode61 tokenizer treats U+FFFE and U+FFFF as token characters, 3.53.4
and macOS's /usr/bin/sqlite3 (3.51.0) treat them as separators, and eight
rows carry a message context of exactly U+FFFE. An FTS5 table that stores
its own content deletes a row by re-tokenizing that content with the
build doing the delete, so a refresh from the other build leaves the
tokens only its sibling knew about behind, and every integrity check from
the other build disagrees with the writer. A suite runs on one build, so
the cross-build half is stood in for by changing a row's stored text under
FTS5: the same thing a different tokenizer does to a delete.

Run: .venv/bin/python -m unittest tests.test_mediaindex_fts
"""
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import mediaindex  # noqa: E402

NONCHAR = "￾"      # the character the builds disagree on


def add_item(con, seq, name, context, kind="photo"):
    con.execute("INSERT INTO items(seq,kind,id,name) VALUES(?,?,?,?)",
                (seq, kind, seq, name))
    con.execute("INSERT INTO content(seq,context) VALUES(?,?)", (seq, context))
    mediaindex.refresh_fts(con, seq)


def other_build_refresh(con, seq, stored_text):
    """What refresh_fts does when the build running it tokenizes the stored
    text differently from the build that indexed it: FTS5 reads its own
    copy of the row to find the tokens to delete, so a changed copy stands
    in for a changed tokenizer. Column c3 of the shadow table is `context`."""
    con.execute("UPDATE fts_content SET c3=? WHERE id=?", (stored_text, seq))
    mediaindex.refresh_fts(con, seq)
    con.commit()


class _Index(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.object(mediaindex, "_DATA", self.root),
                  mock.patch.object(mediaindex, "DB", self.root / "media.sqlite"),
                  mock.patch.object(mediaindex, "_fts_verified", set())):
            p.start()
            self.addCleanup(p.stop)

    def log_text(self):
        p = self.root / "media-index.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""


class TwoWritersOneBuild(_Index):
    def test_the_server_and_the_cli_writing_at_once_leave_the_index_sound(self):
        """The in-server Indexer (content stages: UPDATE content, refresh,
        commit every 25) and a CLI backfill (new items, refresh, commit per
        message) on their own connections, interleaved, as on 2026-10-15
        and 2026-10-17. One build: the index agrees with its content."""
        with closing(mediaindex._db()) as con:
            for seq in range(1, 201):
                add_item(con, seq, f"photo-{seq}.jpg",
                         NONCHAR if seq % 50 == 0 else f"shared note {seq}")
            con.commit()
        errors = []

        def server():
            try:
                with closing(mediaindex._db()) as con:
                    n = 0
                    for seq in range(1, 201):
                        con.execute("UPDATE content SET ocr=? WHERE seq=?",
                                    (f"ocr text {seq} invoice", seq))
                        mediaindex.refresh_fts(con, seq)
                        n += 1
                        if n % 25 == 0:
                            con.commit()
                    con.commit()
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(("server", repr(e)))

        def cli():
            try:
                with closing(mediaindex._db()) as con:
                    for seq in range(201, 401):
                        add_item(con, seq, f"attachment-{seq}.pdf",
                                 NONCHAR if seq % 50 == 0 else f"subject {seq}",
                                 kind="doc")
                        con.commit()
            except Exception as e:  # noqa: BLE001
                errors.append(("cli", repr(e)))

        threads = [threading.Thread(target=server), threading.Thread(target=cli)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertEqual(errors, [])
        with closing(mediaindex._db()) as con:
            self.assertIsNone(mediaindex.fts_check(con))
            self.assertEqual(con.execute("SELECT count(*) FROM fts").fetchone()[0], 400)
            hits = {r[0] for r in con.execute(
                "SELECT rowid FROM fts WHERE fts MATCH 'invoice'")}
            self.assertEqual(hits, set(range(1, 201)))
            self.assertEqual(con.execute(
                "SELECT count(*) FROM fts WHERE fts MATCH 'attachment'").fetchone()[0], 200)
            self.assertEqual(con.execute(      # four carry the noncharacter instead
                "SELECT count(*) FROM fts WHERE fts MATCH 'subject'").fetchone()[0], 196)


class CrossBuildRefresh(_Index):
    def test_a_refresh_that_tokenizes_the_row_differently_leaves_the_index_malformed(self):
        """The reproduction: a row indexed with one token set, refreshed by a
        build that reads a different token set from the same stored text.
        The delete misses the tokens only the writer knew; the integrity
        check then reports the index malformed, which is what PRAGMA
        quick_check printed on 2026-10-16 and 2026-10-23."""
        with closing(mediaindex._db()) as con:
            for seq in range(1, 11):
                add_item(con, seq, f"photo-{seq}.jpg", f"context {seq} noncharacter")
            con.commit()
            self.assertIsNone(mediaindex.fts_check(con))
            other_build_refresh(con, 7, "context 7")
            err = mediaindex.fts_check(con)
            self.assertIsNotNone(err)
            self.assertIn("malformed", err)


class SelfHeal(_Index):
    def corrupt(self):
        with closing(mediaindex._db(verify=False)) as con:
            for seq in range(1, 11):
                add_item(con, seq, f"photo-{seq}.jpg", f"context {seq} noncharacter")
            con.commit()
            other_build_refresh(con, 3, "context 3")
            mediaindex.set_state(con, mediaindex.FTS_SQLITE_KEY, "3.51.0")
            self.assertIn("malformed", mediaindex.fts_check(con))

    def test_opening_the_index_checks_it_and_rebuilds_a_malformed_one(self):
        self.corrupt()
        mediaindex._fts_verified.clear()         # a new process
        with closing(mediaindex._db()) as con:
            self.assertIsNone(mediaindex.fts_check(con))
            self.assertEqual(mediaindex.get_state(con, mediaindex.FTS_SQLITE_KEY),
                             sqlite3.sqlite_version)
            # every row is searchable by its current text again
            self.assertEqual([r[0] for r in con.execute(
                "SELECT rowid FROM fts WHERE fts MATCH 'noncharacter'")],
                list(range(1, 11)))
        log = self.log_text()
        self.assertIn("malformed", log)
        self.assertIn("3.51.0", log)               # the build that last verified it
        self.assertIn(sqlite3.sqlite_version, log)   # and the one that rebuilt it
        self.assertIn("rebuilt 10 rows", log)

    def test_a_sound_index_is_recorded_as_verified_by_this_build(self):
        with closing(mediaindex._db()) as con:
            self.assertEqual(mediaindex.get_state(con, mediaindex.FTS_SQLITE_KEY),
                             sqlite3.sqlite_version)
        self.assertNotIn("malformed", self.log_text())

    def test_a_build_change_is_logged_even_when_the_index_passes(self):
        with closing(mediaindex._db(verify=False)) as con:
            mediaindex.set_state(con, mediaindex.FTS_SQLITE_KEY, "3.53.4")
        with closing(mediaindex._db()) as con:
            self.assertEqual(mediaindex.get_state(con, mediaindex.FTS_SQLITE_KEY),
                             sqlite3.sqlite_version)
        self.assertIn("3.53.4", self.log_text())

    def test_the_check_runs_once_per_process_per_index(self):
        with mock.patch.object(mediaindex, "verify_fts",
                               wraps=mediaindex.verify_fts) as v:
            mediaindex._db().close()
            mediaindex._db().close()
            self.assertEqual(v.call_count, 1)
            with mock.patch.object(mediaindex, "DB", self.root / "other.sqlite"):
                mediaindex._db().close()
            self.assertEqual(v.call_count, 2)

    def test_a_locked_index_is_checked_at_the_next_open_instead(self):
        with closing(mediaindex._db(verify=False)) as con:
            add_item(con, 1, "a.jpg", "x")
            con.commit()
        holder = sqlite3.connect(mediaindex.DB)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(mediaindex, "FTS_CHECK_BUSY_MS", 50):
                mediaindex._db().close()
            self.assertIn("skipped", self.log_text())
            self.assertEqual(mediaindex._fts_verified, set())
        finally:
            holder.rollback()
            holder.close()
        with closing(mediaindex._db()) as con:
            self.assertEqual(mediaindex.get_state(con, mediaindex.FTS_SQLITE_KEY),
                             sqlite3.sqlite_version)

    def test_a_rebuild_that_fails_is_logged_and_does_not_block_the_open(self):
        with closing(mediaindex._db(verify=False)) as con:
            add_item(con, 1, "a.jpg", "x")
            con.commit()
        with mock.patch.object(mediaindex, "fts_check", return_value="malformed inverted index"), \
                mock.patch.object(mediaindex, "fts_rebuild",
                                  side_effect=sqlite3.DatabaseError("database disk image is malformed")):
            mediaindex._db().close()
        self.assertIn("rebuild failed", self.log_text())
        self.assertIn(str(mediaindex.DB), mediaindex._fts_verified)


class RepairCommands(_Index):
    def test_fts_check_reports_without_healing_and_fts_rebuild_heals(self):
        with closing(mediaindex._db(verify=False)) as con:
            for seq in range(1, 6):
                add_item(con, seq, f"p{seq}.jpg", f"context {seq} token")
            con.commit()
            other_build_refresh(con, 2, "context 2")
        report = mediaindex.fts_report()
        self.assertFalse(report["ok"])
        self.assertIn("malformed", report["error"])
        self.assertEqual(report["sqlite"], sqlite3.sqlite_version)
        self.assertEqual(report["rows"], 5)
        report = mediaindex.fts_report()            # reporting did not heal
        self.assertFalse(report["ok"])
        done = mediaindex.fts_repair()
        self.assertTrue(done["ok"])
        self.assertEqual(done["rows"], 5)
        self.assertTrue(mediaindex.fts_report()["ok"])


class MixedBuildGate(_Index):
    def test_a_cli_refuses_to_write_beside_a_server_indexing_on_another_build(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        with closing(mediaindex._db()) as con:
            self.assertIsNone(mediaindex.mixed_build_refusal(con, now=now))
            mediaindex.set_state(con, mediaindex.INDEXER_SQLITE_KEY, "3.53.4")
            mediaindex.set_state(con, mediaindex.INDEXER_SEEN_KEY,
                                 (now - timedelta(seconds=40)).isoformat())
            why = mediaindex.mixed_build_refusal(con, now=now)
            self.assertIn("3.53.4", why)
            self.assertIn(sqlite3.sqlite_version, why)
            self.assertIn("restart", why.lower())
            # a server that stopped ticking is not writing
            mediaindex.set_state(con, mediaindex.INDEXER_SEEN_KEY,
                                 (now - timedelta(minutes=20)).isoformat())
            self.assertIsNone(mediaindex.mixed_build_refusal(con, now=now))
            # the server's own heartbeat is this build: no refusal
            mediaindex.record_indexer_heartbeat(con)
            self.assertIsNone(mediaindex.mixed_build_refusal(con))

    def test_the_heartbeat_records_build_and_time(self):
        with closing(mediaindex._db()) as con:
            mediaindex.record_indexer_heartbeat(con)
            self.assertEqual(mediaindex.get_state(con, mediaindex.INDEXER_SQLITE_KEY),
                             sqlite3.sqlite_version)
            seen = datetime.fromisoformat(mediaindex.get_state(con, mediaindex.INDEXER_SEEN_KEY))
            self.assertLess(abs((datetime.now(timezone.utc) - seen).total_seconds()), 60)

    def test_the_backfill_entry_points_stop_on_the_refusal(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        with closing(mediaindex._db()) as con:
            mediaindex.set_state(con, mediaindex.INDEXER_SQLITE_KEY, "3.53.4")
            mediaindex.set_state(con, mediaindex.INDEXER_SEEN_KEY, now.isoformat())
        with mock.patch.object(mediaindex, "_utcnow", return_value=now):
            self.assertFalse(mediaindex.cli_may_write(log=lambda *a: None))
        with closing(mediaindex._db()) as con:
            mediaindex.record_indexer_heartbeat(con)
        self.assertTrue(mediaindex.cli_may_write(log=lambda *a: None))


if __name__ == "__main__":
    unittest.main()

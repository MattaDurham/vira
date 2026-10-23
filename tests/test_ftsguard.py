"""The text and CRM indexes' FTS5 tables heal themselves the way the media
index's does, through the one helper all three call.

An FTS5 table that stores its own content deletes a row by re-tokenizing
that content with the SQLite build doing the delete, and checks itself by
re-tokenizing every row with the build doing the check. unicode61 is not
identical across builds (3.54.0 tokenizes U+FFFE and U+FFFF; 3.53.4 and
macOS's /usr/bin/sqlite3 3.51.0 do not), so one build's refresh leaves
another build's tokens behind. A suite runs on one build, so the
cross-build half is stood in for by changing a row's stored text under
FTS5 before the refresh: the same thing a different tokenizer does.

Run: .venv/bin/python -m unittest tests.test_ftsguard
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import crmindex, ftsguard, textindex
from server import data as crm
from server import retrieval, settings


class _Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = mock.patch.object(ftsguard, "_verified", set())     # a new process
        p.start()
        self.addCleanup(p.stop)

    def log_text(self, name):
        p = self.root / name
        return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------- the helper on a bare table ----------

class BareTable(_Store):
    """verify on a table of any name, with the module's own accessors."""

    def setUp(self):
        super().setUp()
        self.db = self.root / "bare.sqlite"
        self.lines = []
        self.state = {}

    def con(self):
        con = sqlite3.connect(self.db, timeout=5)
        con.executescript("CREATE VIRTUAL TABLE IF NOT EXISTS idx USING fts5(body);")
        return con

    def kw(self):
        return dict(table="idx", log=self.lines.append,
                    get_state=lambda con, k, d=None: self.state.get(k, d),
                    set_state=lambda con, k, v: self.state.__setitem__(k, v))

    def test_a_sound_table_is_recorded_as_verified_by_this_build(self):
        with closing(self.con()) as con:
            con.execute("INSERT INTO idx(rowid, body) VALUES(1, 'alpha beta')")
            con.commit()
            self.assertEqual(ftsguard.verify(con, **self.kw()), "ok")
        self.assertEqual(self.state[ftsguard.STATE_KEY], sqlite3.sqlite_version)
        self.assertEqual(self.lines, [])

    def test_a_malformed_table_is_rebuilt_and_both_builds_are_named(self):
        self.state[ftsguard.STATE_KEY] = "3.51.0"
        with closing(self.con()) as con:
            con.execute("INSERT INTO idx(rowid, body) VALUES(1, 'alpha beta gamma')")
            con.commit()
            con.execute("UPDATE idx_content SET c0='alpha' WHERE id=1")
            con.execute("DELETE FROM idx WHERE rowid=1")
            con.execute("INSERT INTO idx(rowid, body) VALUES(1, 'alpha beta gamma')")
            con.commit()
            self.assertIn("malformed", ftsguard.check(con, "idx"))
            self.assertEqual(ftsguard.verify(con, **self.kw()), "rebuilt")
            self.assertIsNone(ftsguard.check(con, "idx"))
        self.assertEqual(self.state[ftsguard.STATE_KEY], sqlite3.sqlite_version)
        text = "\n".join(self.lines)
        self.assertIn("3.51.0", text)
        self.assertIn(sqlite3.sqlite_version, text)
        self.assertIn("rebuilt 1 rows", text)

    def test_the_connection_keeps_its_own_busy_timeout_after_the_check(self):
        with closing(self.con()) as con:
            ftsguard.verify(con, busy_ms=50, **self.kw())
            self.assertEqual(con.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_a_rebuild_that_fails_is_logged_and_reported(self):
        with closing(self.con()) as con, \
                mock.patch.object(ftsguard, "check", return_value="malformed inverted index"), \
                mock.patch.object(ftsguard, "rebuild",
                                  side_effect=sqlite3.DatabaseError("database disk image is malformed")):
            self.assertEqual(ftsguard.verify(con, **self.kw()), "failed")
        self.assertIn("rebuild failed", "\n".join(self.lines))
        self.assertNotIn(ftsguard.STATE_KEY, self.state)


# ---------- the text index ----------

def add_message(con, seq, text):
    textindex._insert(con, uid=f"m{seq}", source="imessage", text=text,
                      date_ns=seq * 1_000_000_000)


def other_build_refresh_text(con, seq, stored_text):
    """What a delete-and-reinsert of one row does when the build running
    it tokenizes the stored text differently from the build that indexed
    it: FTS5 reads its own copy to find the tokens to delete, so a changed
    copy stands in for a changed tokenizer. Column c0 is `text`."""
    con.execute("UPDATE fts_content SET c0=? WHERE id=?", (stored_text, seq))
    con.execute("DELETE FROM fts WHERE rowid=?", (seq,))
    con.execute("INSERT INTO fts(rowid, text, subject) "
                "SELECT seq, text, coalesce(subject, '') FROM items WHERE seq=?", (seq,))
    con.commit()


class TextIndex(_Store):
    def setUp(self):
        super().setUp()
        self.db = self.root / "text-index.sqlite"
        p = mock.patch.object(textindex, "DB", self.db)
        p.start()
        self.addCleanup(p.stop)

    def fill(self, n=10):
        with closing(textindex._db(verify=False)) as con:
            for seq in range(1, n + 1):
                add_message(con, seq, f"message {seq} noncharacter")
            con.commit()

    def corrupt(self):
        self.fill()
        with closing(textindex._db(verify=False)) as con:
            self.assertIsNone(ftsguard.check(con))
            other_build_refresh_text(con, 3, "message 3")
            textindex.set_state(con, ftsguard.STATE_KEY, "3.51.0")
            con.commit()
            self.assertIn("malformed", ftsguard.check(con))

    def test_a_refresh_that_tokenizes_the_row_differently_leaves_the_index_malformed(self):
        self.corrupt()

    def test_opening_the_index_checks_it_and_rebuilds_a_malformed_one(self):
        self.corrupt()
        with closing(textindex._db()) as con:
            self.assertIsNone(ftsguard.check(con))
            self.assertEqual(textindex.get_state(con, ftsguard.STATE_KEY),
                             sqlite3.sqlite_version)
            self.assertEqual([r[0] for r in con.execute(
                "SELECT rowid FROM fts WHERE fts MATCH 'noncharacter'")],
                list(range(1, 11)))
        log = self.log_text("text-index.log")
        self.assertIn("malformed", log)
        self.assertIn("3.51.0", log)                 # the build that last verified it
        self.assertIn(sqlite3.sqlite_version, log)   # and the one that rebuilt it
        self.assertIn("rebuilt 10 rows", log)

    def test_a_sound_index_is_recorded_as_verified_by_this_build(self):
        self.fill(3)
        with closing(textindex._db()) as con:
            self.assertEqual(textindex.get_state(con, ftsguard.STATE_KEY),
                             sqlite3.sqlite_version)
        self.assertNotIn("malformed", self.log_text("text-index.log"))

    def test_the_check_runs_once_per_process_per_index(self):
        with mock.patch.object(ftsguard, "verify", wraps=ftsguard.verify) as v:
            textindex._db().close()
            textindex._db().close()
            self.assertEqual(v.call_count, 1)
            with mock.patch.object(textindex, "DB", self.root / "other.sqlite"):
                textindex._db().close()
            self.assertEqual(v.call_count, 2)

    def test_a_locked_index_is_checked_at_the_next_open_instead(self):
        self.fill(1)
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(ftsguard, "BUSY_MS", 50):
                textindex._db().close()
            self.assertIn("skipped", self.log_text("text-index.log"))
            self.assertEqual(ftsguard._verified, set())
        finally:
            holder.rollback()
            holder.close()
        with closing(textindex._db()) as con:
            self.assertEqual(textindex.get_state(con, ftsguard.STATE_KEY),
                             sqlite3.sqlite_version)

    def test_fts_check_reports_without_healing_and_fts_rebuild_heals(self):
        self.fill(5)
        with closing(textindex._db(verify=False)) as con:
            other_build_refresh_text(con, 2, "message 2")
        report = textindex.fts_report()
        self.assertFalse(report["ok"])
        self.assertIn("malformed", report["error"])
        self.assertEqual(report["rows"], 5)
        self.assertFalse(textindex.fts_report()["ok"])      # reporting did not heal
        json.dumps(report)                                  # what the command prints
        done = textindex.fts_repair()
        self.assertTrue(done["ok"])
        self.assertEqual(done["rows"], 5)
        self.assertTrue(textindex.fts_report()["ok"])
        self.assertIn("(fts-rebuild)", self.log_text("text-index.log"))


class TextIndexMixedBuildGate(TextIndex):
    """The CLI backfill refuses to write beside a server indexing on
    another SQLite build, as the media index's does (MixedBuildGate in
    test_mediaindex_fts): the in-server Indexer's tick leaves the
    heartbeat the gate reads."""

    def test_a_cli_refuses_to_write_beside_a_server_indexing_on_another_build(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        with closing(textindex._db()) as con:
            self.assertIsNone(textindex.mixed_build_refusal(con, now=now))
            textindex.set_state(con, ftsguard.INDEXER_SQLITE_KEY, "3.53.4")
            textindex.set_state(con, ftsguard.INDEXER_SEEN_KEY,
                                (now - timedelta(seconds=40)).isoformat())
            why = textindex.mixed_build_refusal(con, now=now)
            self.assertIn("3.53.4", why)
            self.assertIn(sqlite3.sqlite_version, why)
            self.assertIn("restart", why.lower())
            # a server that stopped ticking is not writing
            textindex.set_state(con, ftsguard.INDEXER_SEEN_KEY,
                                (now - timedelta(minutes=20)).isoformat())
            self.assertIsNone(textindex.mixed_build_refusal(con, now=now))
            # the server's own heartbeat is this build: no refusal
            textindex.record_indexer_heartbeat(con)
            self.assertIsNone(textindex.mixed_build_refusal(con))

    def test_the_indexer_tick_records_build_and_time(self):
        """Indexer.run, one tick: the heartbeat lands before the corpora
        are touched, and an indexing failure does not skip it."""
        idx = textindex.Indexer(every=0, boot_wait=0)

        def failing_tick():
            idx.stop()
            raise RuntimeError("indexing failed")

        with mock.patch.object(textindex, "incremental", side_effect=failing_tick), \
                mock.patch.object(textindex.crmindex, "refresh"), \
                mock.patch.object(textindex.crmindex, "embed_pending"):
            idx.run()
        with closing(textindex._db()) as con:
            self.assertEqual(textindex.get_state(con, ftsguard.INDEXER_SQLITE_KEY),
                             sqlite3.sqlite_version)
            seen = datetime.fromisoformat(
                textindex.get_state(con, ftsguard.INDEXER_SEEN_KEY))
            self.assertLess(abs((datetime.now(timezone.utc) - seen).total_seconds()), 60)

    def test_the_backfill_entry_point_stops_on_the_refusal(self):
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        lines = []
        with closing(textindex._db()) as con:
            textindex.set_state(con, ftsguard.INDEXER_SQLITE_KEY, "3.53.4")
            textindex.set_state(con, ftsguard.INDEXER_SEEN_KEY, now.isoformat())
            con.commit()
        with mock.patch.object(ftsguard, "_utcnow", return_value=now):
            self.assertFalse(textindex.cli_may_write(log=lines.append))
        self.assertEqual(len(lines), 1)
        self.assertIn("refusing to write the text index", lines[0])
        self.assertIn("3.53.4", lines[0])
        with closing(textindex._db()) as con:
            textindex.record_indexer_heartbeat(con)
        self.assertTrue(textindex.cli_may_write(log=lines.append))
        self.assertEqual(len(lines), 1)


# ---------- the CRM index ----------

PEOPLE = [
    {"id": "p_broker", "name": "Bob Grant",
     "handles": {"emails": ["bob@example.com"], "phones10": ["5550100"]}},
    {"id": "p_sponsor", "name": "Sam Muhs", "handles": {}},
    {"id": "p_quiet", "name": "Dana Okonkwo", "handles": {}},
]
MASTER = [
    {"id": "p_broker", "full_name": "Bob Grant", "company": "NFP",
     "title": "Account Executive", "relationship": "insurance broker",
     "evidence": "handled the property insurance renewals"},
    {"id": "p_sponsor", "full_name": "Sam Muhs", "company": "Sotelo Capital",
     "title": "Principal", "relationship": "friend and sponsor",
     "evidence": "pitches multifamily deals"},
]


class CrmIndex(_Store):
    def setUp(self):
        super().setUp()
        crm_root = self.root / "crm"
        (crm_root / "profiles").mkdir(parents=True)
        (crm_root / "people.json").write_text(json.dumps({"people": PEOPLE}), encoding="utf-8")
        (crm_root / "master.json").write_text(json.dumps(MASTER), encoding="utf-8")
        self.db = self.root / "crm-index.sqlite"
        for p in (mock.patch.object(settings, "crm_root", lambda: crm_root),
                  mock.patch.object(crmindex, "DB", self.db),
                  mock.patch.object(crmindex, "_matrices", retrieval.MatrixCache(
                      {"text": ("SELECT COUNT(*) FROM vecs", "SELECT seq, v FROM vecs")}))):
            p.start()
            self.addCleanup(p.stop)
        crm._cache.clear()
        self.addCleanup(crm._cache.clear)
        crmindex.invalidate()

    def seq_of(self, con, pid):
        return con.execute("SELECT seq FROM people WHERE pid=?", (pid,)).fetchone()[0]

    def corrupt(self):
        """refresh() empties fts and refills it; a row whose stored copy a
        second build would tokenize differently is left malformed by that
        delete. Column c1 of the shadow table is `text`."""
        crmindex.refresh(force=True)
        with closing(crmindex._con(verify=False)) as con:
            self.assertIsNone(ftsguard.check(con))
            seq = self.seq_of(con, "p_broker")
            con.execute("UPDATE fts_content SET c1='Bob Grant' WHERE id=?", (seq,))
            crmindex.set_state(con, ftsguard.STATE_KEY, "3.51.0")
            con.commit()
        crmindex.refresh(force=True)
        with closing(crmindex._con(verify=False)) as con:
            self.assertIn("malformed", ftsguard.check(con))

    def test_a_refresh_that_tokenizes_a_row_differently_leaves_the_index_malformed(self):
        self.corrupt()

    def test_opening_the_index_checks_it_and_rebuilds_a_malformed_one(self):
        self.corrupt()
        ftsguard._verified.clear()                  # a new process
        with closing(crmindex._con()) as con:
            self.assertIsNone(ftsguard.check(con))
            self.assertEqual(crmindex.get_state(con, ftsguard.STATE_KEY),
                             sqlite3.sqlite_version)
            self.assertEqual(con.execute(
                "SELECT count(*) FROM fts WHERE fts MATCH 'insurance'").fetchone()[0], 1)
        log = self.log_text("crm-index.log")
        self.assertIn("malformed", log)
        self.assertIn("3.51.0", log)
        self.assertIn(sqlite3.sqlite_version, log)
        self.assertIn("rebuilt 3 rows", log)

    def test_a_sound_index_is_recorded_as_verified_by_this_build(self):
        crmindex.refresh(force=True)
        with closing(crmindex._con()) as con:
            self.assertEqual(crmindex.get_state(con, ftsguard.STATE_KEY),
                             sqlite3.sqlite_version)
        self.assertNotIn("malformed", self.log_text("crm-index.log"))

    def test_the_check_runs_once_per_process_per_index(self):
        with mock.patch.object(ftsguard, "verify", wraps=ftsguard.verify) as v:
            crmindex._con().close()
            crmindex.refresh(force=True)
            crmindex.status()
            self.assertEqual(v.call_count, 1)

    def test_a_locked_index_is_checked_at_the_next_open_instead(self):
        crmindex.refresh(force=True)
        ftsguard._verified.clear()
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(ftsguard, "BUSY_MS", 50):
                crmindex._con().close()
            self.assertIn("skipped", self.log_text("crm-index.log"))
            self.assertEqual(ftsguard._verified, set())
        finally:
            holder.rollback()
            holder.close()
        with closing(crmindex._con()) as con:
            self.assertEqual(crmindex.get_state(con, ftsguard.STATE_KEY),
                             sqlite3.sqlite_version)

    def test_fts_check_reports_without_healing_and_fts_rebuild_heals(self):
        self.corrupt()
        report = crmindex.fts_report()
        self.assertFalse(report["ok"])
        self.assertIn("malformed", report["error"])
        self.assertEqual(report["rows"], 3)
        self.assertFalse(crmindex.fts_report()["ok"])
        done = crmindex.fts_repair()
        self.assertTrue(done["ok"])
        self.assertEqual(done["rows"], 3)
        self.assertTrue(crmindex.fts_report()["ok"])


if __name__ == "__main__":
    unittest.main()

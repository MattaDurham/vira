"""Existing local retrieval vectors drive deterministic World coordinates."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import retrieval, worldlayout


@unittest.skipIf(worldlayout.np is None, "numpy is an optional dependency")
class WorldLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault_db = self.root / "vault.sqlite"
        self.crm_db = self.root / "crm.sqlite"
        self._make_vault()
        self._make_crm()
        worldlayout._vector_cache.update(
            fingerprint=None, vectors={}, dimensions=0)
        worldlayout._layout_cache.update(
            key=None, positions={}, meta={})
        self.patches = [
            mock.patch.object(worldlayout.vault, "source_specs", return_value=[{
                "id": "primary", "db": self.vault_db,
            }]),
            mock.patch.object(worldlayout.crmindex, "DB", self.crm_db),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _blob(self, values):
        return retrieval.pack_vec(worldlayout.np.asarray(
            values, dtype="float32"))

    def _make_vault(self):
        con = sqlite3.connect(self.vault_db)
        con.executescript(
            "CREATE TABLE chunks(id INTEGER PRIMARY KEY, path TEXT, seq INT);"
            "CREATE TABLE vecs(chunk_id INTEGER PRIMARY KEY, vec BLOB);")
        rows = [(1, "alpha.md", 0, [1, 0, 0, 0]),
                (2, "alpha.md", 1, [0.9, 0.1, 0, 0]),
                (3, "beta.md", 0, [0, 1, 0, 0])]
        for chunk_id, path, seq, vector in rows:
            con.execute("INSERT INTO chunks VALUES(?,?,?)",
                        (chunk_id, path, seq))
            con.execute("INSERT INTO vecs VALUES(?,?)",
                        (chunk_id, self._blob(vector)))
        con.commit()
        con.close()

    def _make_crm(self):
        con = sqlite3.connect(self.crm_db)
        con.executescript(
            "CREATE TABLE people(seq INTEGER PRIMARY KEY, pid TEXT);"
            "CREATE TABLE vecs(seq INTEGER PRIMARY KEY, v BLOB);")
        con.execute("INSERT INTO people VALUES(1, 'p1')")
        con.execute("INSERT INTO vecs VALUES(1, ?)",
                    (self._blob([0, 0, 1, 0]),))
        con.commit()
        con.close()

    def test_vectors_place_notes_people_and_derived_neighbors(self):
        alpha = worldlayout._stable_id("note", "primary:alpha.md")
        beta = worldlayout._stable_id("note", "primary:beta.md")
        nodes = [{"id": alpha}, {"id": beta}, {"id": "p1"},
                 {"id": "topic"}]
        edges = [{"a": "topic", "b": alpha}]
        positions, meta = worldlayout.positions(nodes, edges, {})
        self.assertEqual(set(positions), {alpha, beta, "p1", "topic"})
        self.assertEqual(meta["basis"], "local-embedding-randomized-pca")
        self.assertEqual(meta["dimensions"], 4)
        self.assertEqual(meta["vector_nodes"], 3)
        self.assertEqual(meta["neighbor_nodes"], 1)
        self.assertEqual(meta["semantic_nodes"], 4)
        self.assertEqual(meta["fallback_nodes"], 0)
        self.assertEqual(meta["semantic_coverage"], 1.0)
        self.assertEqual(meta["placed_nodes"], 4)
        again, again_meta = worldlayout.positions(nodes, edges, {})
        self.assertEqual(positions, again)
        self.assertEqual(meta, again_meta)


if __name__ == "__main__":
    unittest.main()

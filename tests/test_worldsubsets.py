"""World subsets: saved recipes, a layout fitted to the subset, its clusters."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import retrieval, worldlayout, worldsubsets


def _graph(nodes, edges):
    return {"generated": "2026-10-08T12:00:00+00:00", "nodes": nodes,
            "edges": edges}


class SubsetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "world-subsets.json"
        patch = mock.patch.object(worldsubsets, "STORE", self.store)
        patch.start()
        self.addCleanup(patch.stop)

    def test_create_list_rename_and_delete_round_trip(self):
        recipe = {"steps": [{"query": "tag:ai-safety  -source:reports/",
                             "kinds": ["Note", "concept", "note"]}]}
        row = worldsubsets.create("  AI   safety ", recipe,
                                  {"items": 307, "links": 1232})
        self.assertEqual(row["name"], "AI safety")
        step = row["recipe"]["steps"][0]
        self.assertEqual(step["query"], "tag:ai-safety -source:reports/")
        self.assertEqual(step["kinds"], ["concept", "note"])
        self.assertEqual(step["hops"], 0)
        self.assertIsNone(step["start_kinds"])
        self.assertEqual(row["stats"]["items"], 307)
        started = worldsubsets.create("People out", {"steps": [
            {"start_kinds": ["Person"], "hops": 1}]})
        self.assertEqual(started["recipe"]["steps"][0]["start_kinds"],
                         ["person"])
        worldsubsets.delete(started["id"])
        on_disk = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["subsets"][0]["id"], row["id"])

        renamed = worldsubsets.update(row["id"], name="Safety")
        self.assertEqual(renamed["name"], "Safety")
        self.assertEqual(renamed["recipe"], row["recipe"])
        self.assertEqual([r["name"] for r in worldsubsets.list_all()],
                         ["Safety"])
        self.assertEqual(worldsubsets.delete(row["id"]), [])
        with self.assertRaises(KeyError):
            worldsubsets.delete(row["id"])
        with self.assertRaises(KeyError):
            worldsubsets.update("missing", name="x")

    def test_a_recipe_must_narrow_and_stay_in_bounds(self):
        for bad in (None, {}, {"steps": []}, {"steps": [{}]},
                    {"steps": [{"query": "  "}]},
                    {"steps": [{"kinds": []}]},
                    {"steps": [{"start_kinds": []}]},
                    {"steps": [{"start_kinds": "person"}]},
                    {"steps": [{"seeds": ["a"], "hops": 4}]},
                    {"steps": [{"seeds": ["a"], "hops": "two"}]},
                    {"steps": [{"query": "x" * 501}]},
                    {"steps": [{"query": "x"}] * 9},
                    {"steps": ["tag:x"]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    worldsubsets.create("name", bad)
        with self.assertRaises(ValueError):
            worldsubsets.create("   ", {"steps": [{"query": "x"}]})
        self.assertFalse(self.store.exists())

    def test_seeds_are_deduplicated_and_capped(self):
        row = worldsubsets.create("Around", {"steps": [
            {"seeds": ["a", "b", "a", " "], "hops": 2}]})
        self.assertEqual(row["recipe"]["steps"][0]["seeds"], ["a", "b"])
        with mock.patch.object(worldsubsets, "MAX_SEEDS", 2):
            with self.assertRaises(ValueError):
                worldsubsets.create("Too many", {"steps": [
                    {"seeds": ["a", "b", "c"]}]})

    def test_the_saved_list_is_capped(self):
        with mock.patch.object(worldsubsets, "MAX_SUBSETS", 2):
            worldsubsets.create("one", {"steps": [{"query": "a"}]})
            worldsubsets.create("two", {"steps": [{"query": "b"}]})
            with self.assertRaises(ValueError):
                worldsubsets.create("three", {"steps": [{"query": "c"}]})
        self.assertEqual(len(worldsubsets.list_all()), 2)


class SubsetClusterTests(unittest.TestCase):
    def _two_triangles(self):
        nodes = {}
        for node_id, tags in (("a1", ["safety"]), ("a2", ["safety"]),
                              ("a3", ["safety", "shared"]),
                              ("b1", ["music"]), ("b2", ["music"]),
                              ("b3", ["music", "shared"]),
                              ("lone", ["shared"])):
            nodes[node_id] = {"id": node_id, "name": node_id.upper(),
                              "kind": "note", "tags": tags}
        edges = [{"a": "a1", "b": "a2"}, {"a": "a2", "b": "a3"},
                 {"a": "a1", "b": "a3"}, {"a": "b1", "b": "b2"},
                 {"a": "b2", "b": "b3"}, {"a": "b1", "b": "b3"},
                 {"a": "a3", "b": "b3"}]
        return nodes, edges

    def test_clusters_split_by_links_and_take_their_specific_tag(self):
        nodes, edges = self._two_triangles()
        lens = worldsubsets.cluster_lens(sorted(nodes), nodes, edges)
        self.assertEqual(lens["id"], "subset-clusters")
        self.assertEqual(lens["total"], 7)
        by_label = {band["label"]: band for band in lens["bands"]}
        self.assertEqual(set(by_label), {"safety", "music", "Unlinked"})
        self.assertTrue(by_label["Unlinked"]["muted"])
        band = lens["node_band"]
        self.assertEqual(band["a1"], band["a2"])
        self.assertEqual(band["a2"], band["a3"])
        self.assertEqual(band["b1"], band["b3"])
        self.assertNotEqual(band["a1"], band["b1"])
        self.assertEqual(band["lone"], "cluster:loose")
        # a tag the whole subset shares never names one cluster of it
        self.assertNotIn("shared", by_label)

    def test_clusters_are_repeatable(self):
        nodes, edges = self._two_triangles()
        first = worldsubsets.cluster_lens(sorted(nodes), nodes, edges)
        again = worldsubsets.cluster_lens(sorted(nodes, reverse=True),
                                          nodes, list(reversed(edges)))
        self.assertEqual(first["node_band"], again["node_band"])

    def test_an_untagged_cluster_is_named_for_its_best_connected_member(self):
        nodes = {node_id: {"id": node_id, "name": f"Item {node_id}",
                           "kind": "note"} for node_id in "hwxyz"}
        edges = [{"a": "h", "b": other} for other in "wxyz"]
        lens = worldsubsets.cluster_lens(sorted(nodes), nodes, edges)
        self.assertEqual(lens["bands"][0]["label"], "Item h")
        self.assertEqual(lens["bands"][0]["hub"], "h")

    def test_a_generic_page_never_names_a_cluster(self):
        nodes = {node_id: {"id": node_id, "name": f"Item {node_id}",
                           "kind": "note"} for node_id in "wxyz"}
        nodes["h"] = {"id": "h", "name": "Index", "kind": "note"}
        edges = [{"a": "h", "b": other} for other in "wxyz"]
        edges.append({"a": "w", "b": "x"})
        lens = worldsubsets.cluster_lens(sorted(nodes), nodes, edges)
        self.assertNotIn("Index", [band["label"] for band in lens["bands"]])

    @unittest.skipIf(worldlayout.np is None, "numpy is optional")
    def test_meaning_splits_what_one_hub_tag_would_swallow(self):
        """Eight notes hang off one tag and have no other links. By links
        alone they are one blob; their nearest neighbours by meaning split
        them into the two subjects they are about."""
        np = worldlayout.np
        nodes = {"hub": {"id": "hub", "name": "hub", "kind": "topic"}}
        vectors, edges = {}, []
        for prefix, axis in (("a", 0), ("b", 1)):
            for i in range(4):
                node_id = f"{prefix}{i}"
                nodes[node_id] = {"id": node_id, "name": node_id,
                                  "kind": "note"}
                vector = np.zeros(4, dtype="float32")
                vector[axis] = 1.0
                vector[2] = 0.05 * (i + 1)
                vectors[node_id] = vector / np.linalg.norm(vector)
                edges.append({"a": "hub", "b": node_id})
        with mock.patch.object(worldsubsets, "KNN", 3):
            alone = worldsubsets.cluster_lens(sorted(nodes), nodes, edges)
            split = worldsubsets.cluster_lens(sorted(nodes), nodes, edges,
                                              vectors)
        self.assertEqual(len({alone["node_band"][f"a{i}"] for i in range(4)}
                             | {alone["node_band"][f"b{i}"] for i in range(4)}),
                         1)
        band = split["node_band"]
        self.assertEqual(len({band[f"a{i}"] for i in range(4)}), 1)
        self.assertEqual(len({band[f"b{i}"] for i in range(4)}), 1)
        self.assertNotEqual(band["a0"], band["b0"])

    def test_small_groups_fold_into_other_groups(self):
        nodes = {node_id: {"id": node_id, "name": node_id, "kind": "note"}
                 for node_id in ("p", "q")}
        lens = worldsubsets.cluster_lens(["p", "q"], nodes,
                                         [{"a": "p", "b": "q"}])
        self.assertEqual([band["id"] for band in lens["bands"]],
                         ["cluster:other"])
        self.assertTrue(lens["bands"][0]["muted"])


@unittest.skipIf(worldlayout.np is None, "numpy is an optional dependency")
class SubsetLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault_db = root / "vault.sqlite"
        self.crm_db = root / "crm.sqlite"
        con = sqlite3.connect(self.vault_db)
        con.executescript(
            "CREATE TABLE chunks(id INTEGER PRIMARY KEY, path TEXT, seq INT);"
            "CREATE TABLE vecs(chunk_id INTEGER PRIMARY KEY, vec BLOB);")
        rows = [("one.md", [1, 0, 0, 0]), ("two.md", [0, 1, 0, 0]),
                ("three.md", [0, 0, 1, 0]), ("four.md", [0, 0, 0, 1]),
                ("far.md", [0.5, 0.5, 0.5, 0.5])]
        for i, (path, vector) in enumerate(rows, start=1):
            con.execute("INSERT INTO chunks VALUES(?,?,0)", (i, path))
            con.execute("INSERT INTO vecs VALUES(?,?)", (i, retrieval.pack_vec(
                worldlayout.np.asarray(vector, dtype="float32"))))
        con.commit()
        con.close()
        sqlite3.connect(self.crm_db).close()
        worldlayout._vector_cache.update(
            fingerprint=None, vectors={}, dimensions=0)
        worldlayout._layout_cache.update(
            key=None, positions={}, meta={}, page_to_node={})
        for patch in (
                mock.patch.object(worldlayout.vault, "source_specs",
                                  return_value=[{"id": "v",
                                                 "db": self.vault_db}]),
                mock.patch.object(worldlayout.crmindex, "DB", self.crm_db)):
            patch.start()
            self.addCleanup(patch.stop)
        self.ids = {name: worldlayout._stable_id("note", f"v:{name}.md")
                    for name in ("one", "two", "three", "four", "far")}

    def _layout(self, ids, nodes, edges):
        with mock.patch.object(worldsubsets.worldgraph, "current",
                               return_value=_graph(nodes, edges)):
            return worldsubsets.layout(ids)

    def test_vectors_place_members_and_links_place_the_rest(self):
        one, two, three = (self.ids[k] for k in ("one", "two", "three"))
        nodes = [{"id": node_id, "name": node_id, "kind": "note"}
                 for node_id in (one, two, three, "tag", "stray")]
        nodes.append({"id": self.ids["far"], "name": "far", "kind": "note"})
        edges = [{"a": "tag", "b": one}, {"a": "tag", "b": two},
                 {"a": one, "b": self.ids["far"]}]     # far is not a member
        out = self._layout([one, two, three, "tag", "stray"], nodes, edges)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["count"], 5)
        self.assertEqual(out["links"], 2)
        meta = out["layout"]
        self.assertEqual(meta["basis"], "subset-embedding-pca")
        self.assertEqual(meta["vector_nodes"], 3)
        self.assertEqual(meta["neighbor_nodes"], 1)
        self.assertEqual(meta["loose_nodes"], 1)
        self.assertEqual(meta["unknown_nodes"], 0)
        self.assertEqual(list(out["positions"]),
                         [one, two, three, "tag", "stray"])
        pos = out["positions"]
        midpoint = [(pos[one][i] + pos[two][i]) / 2 for i in range(3)]
        jitter = meta["radius"] * 0.03 + 1e-3
        for i in range(3):
            self.assertLessEqual(abs(pos["tag"][i] - midpoint[i]), jitter)
        # the unplaced item sits in its own cloud below the main one
        lowest = min(pos[node_id][1] for node_id in (one, two, three, "tag"))
        self.assertLess(pos["stray"][1], lowest)
        self.assertEqual(out["lens"]["node_band"]["stray"], "cluster:loose")

    def test_the_projection_is_fitted_to_the_subset(self):
        """Two items the full projection keeps close get the subset's whole
        spread once they are the only things in it."""
        pair = [self.ids["three"], self.ids["four"]]
        nodes = [{"id": node_id, "name": node_id, "kind": "note"}
                 for node_id in self.ids.values()]
        out = self._layout(pair, nodes, [])
        a, b = (out["positions"][node_id] for node_id in pair)
        spread = sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5
        self.assertGreater(spread, out["layout"]["radius"])

    def test_page_folding_from_the_full_layout_is_honoured(self):
        """A vault page folded onto a CRM person lends that person its
        vector, exactly as in the full layout."""
        page = self.ids["one"]
        nodes = [{"id": "p_person", "name": "Person", "kind": "person"},
                 {"id": self.ids["two"], "name": "two", "kind": "note"}]
        worldlayout.positions(nodes, [], {page: "p_person"})
        out = self._layout(["p_person", self.ids["two"]], nodes, [])
        self.assertEqual(out["layout"]["vector_nodes"], 2)
        self.assertEqual(out["layout"]["loose_nodes"], 0)

    def test_empty_unknown_and_oversized_requests_say_so(self):
        self.assertEqual(worldsubsets.layout([])["status"], "empty")
        with mock.patch.object(worldsubsets, "SUBSET_MAX", 2):
            out = worldsubsets.layout(["a", "b", "c"])
        self.assertEqual(out, {"status": "too_large", "count": 3, "limit": 2})
        out = self._layout(["ghost"], [], [])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["layout"]["unknown_nodes"], 1)
        self.assertEqual(out["layout"]["basis"], "subset-links-only")
        self.assertEqual(len(out["positions"]["ghost"]), 3)

    def test_radius_grows_with_the_cube_root_and_has_a_floor(self):
        self.assertEqual(worldsubsets.radius_for(1), worldsubsets.MIN_RADIUS)
        self.assertAlmostEqual(worldsubsets.radius_for(1000),
                               worldsubsets.SPACING * 10, places=6)


class SubsetWiringTests(unittest.TestCase):
    def test_the_store_is_backed_up(self):
        from server import backup
        self.assertIn("world-subsets.json", backup.FILES)

    def test_the_api_saves_lists_lays_out_and_deletes(self):
        from fastapi.testclient import TestClient
        from server import main
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        nodes = [{"id": n, "name": n, "kind": "note"} for n in "abcd"]
        edges = [{"a": "a", "b": "b"}, {"a": "b", "b": "c"},
                 {"a": "a", "b": "c"}, {"a": "c", "b": "d"}]
        for patch in (
                mock.patch.object(worldsubsets, "STORE",
                                  Path(tmp.name) / "world-subsets.json"),
                mock.patch.object(worldsubsets.worldgraph, "current",
                                  return_value=_graph(nodes, edges)),
                mock.patch.object(worldsubsets.worldlayout, "subset_vectors",
                                  return_value={}),
                mock.patch.object(worldsubsets.worldlayout, "subset_positions",
                                  side_effect=lambda ids, *_rest: (
                                      {i: [0.0, 0.0, 0.0] for i in ids},
                                      {"basis": "subset-links-only"}))):
            patch.start()
            self.addCleanup(patch.stop)
        client = TestClient(main.app)
        made = client.post("/api/world/subsets", json={
            "name": "Triangle", "recipe": {"steps": [{"query": "kind:note"}]}})
        self.assertEqual(made.status_code, 200)
        sid = made.json()["subset"]["id"]
        self.assertEqual(client.post("/api/world/subsets", json={
            "name": "Nothing", "recipe": {"steps": [{}]}}).status_code, 400)
        listed = client.get("/api/world/subsets").json()["subsets"]
        self.assertEqual([row["id"] for row in listed], [sid])
        renamed = client.put(f"/api/world/subsets/{sid}",
                             json={"name": "Three", "stats": {"items": 4}})
        self.assertEqual(renamed.json()["subset"]["name"], "Three")
        self.assertEqual(renamed.json()["subset"]["stats"]["items"], 4)
        self.assertEqual(client.put("/api/world/subsets/nope",
                                    json={"name": "x"}).status_code, 404)
        laid = client.post("/api/world/subsets/layout",
                           json={"ids": ["a", "b", "c", "d"]}).json()
        self.assertEqual(laid["status"], "ok")
        self.assertEqual(laid["links"], 4)
        self.assertEqual(set(laid["positions"]), set("abcd"))
        self.assertEqual(laid["lens"]["node_band"]["a"],
                         laid["lens"]["node_band"]["c"])
        self.assertEqual(client.delete(
            f"/api/world/subsets/{sid}").json()["subsets"], [])
        self.assertEqual(client.delete(
            f"/api/world/subsets/{sid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class SubsetClientContractTests(unittest.TestCase):
    """The galaxy builds recipes; the server validates them. The two must
    name the same step fields and the same hop ceiling, or a recipe saved
    from the panel would lose a field on its way through the store."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.atlas = (root / "static" / "atlas.js").read_text(encoding="utf-8")
        cls.html = (root / "static" / "index.html").read_text(encoding="utf-8")
        cls.renderer = (root / "static" / "atlas3d.js").read_text(
            encoding="utf-8")

    def test_client_steps_carry_exactly_the_fields_the_server_keeps(self):
        import re
        block = re.search(r"const BLANK_STEP = \{(.*?)\};", self.atlas, re.S)
        self.assertIsNotNone(block)
        client = set(re.findall(r"(\w+):", block.group(1)))
        server = set(worldsubsets._clean_step({"query": "x"}))
        self.assertEqual(client, server)
        hops = re.search(r"const MAX_HOPS = (\d+);", self.atlas)
        self.assertEqual(int(hops.group(1)), worldsubsets.MAX_HOPS)

    def test_panel_bar_and_routes_are_wired(self):
        for needle in ('id="atlas-subsets"', 'id="atlas-subset-shown"',
                       'id="atlas-subset-around"', 'id="atlas-subset-list"',
                       'id="atlas-subset-bar"'):
            self.assertIn(needle, self.html)
        for needle in ('"/api/world/subsets/layout"', '"/api/world/subsets"',
                       "function subsetMembers(recipe)",
                       "function openSubset(sub)", "function closeSubset()",
                       "S.world = g;", "g.default_lens ||"):
            self.assertIn(needle, self.atlas)
        # each saved subset keeps its own camera pose
        self.assertIn("function camKey()", self.renderer)
        self.assertIn("camScope:", self.atlas)

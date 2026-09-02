"""Temporal World graph: native composition over CRM + Markdown sources."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import worldgraph


def _atlas_graph():
    return {
        "status": "ok",
        "generated": "2026-09-02T12:00:00+00:00",
        "owner": {"name": "Owner Example", "pid": "owner"},
        "nodes": [{"id": "p_alice", "name": "Alice Example", "tier": "A",
                   "company": "Acme", "title": "Engineer", "degree": 1,
                   "cluster": "c1", "face": None, "act": 20}],
        "edges": [], "ego_edges": [{"a": "ego", "b": "p_alice",
                                      "weight": 1, "signals": []}],
    }


class WorldGraphTests(unittest.TestCase):
    def setUp(self):
        worldgraph._page_cache.update(
            fingerprint=None, pages=[], total=0, sources=0)
        worldgraph._graph_cache.update(key=None, result=None)
        worldgraph._json_cache.update(graph=None, payload=None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "wiki").mkdir()
        (self.root / "Projects").mkdir()
        (self.root / "wiki" / "alice.md").write_text(
            """---
type: person
title: Alice Example
first_met: 2021-04
recorded_at: 2024-01-03
tags: [knowledge graph]
---
Works with [[Acme Lab]].
""", encoding="utf-8")
        (self.root / "wiki" / "acme-lab.md").write_text(
            """---
type: organization
title: Acme Lab
valid_from: 2020
recorded_at: 2024-02-10
tags: [knowledge graph]
---
Employs [[Alice]].
""", encoding="utf-8")
        (self.root / "Projects" / "world-map.md").write_text(
            """---
type: project
title: World Map
valid_from: 2025-03-01
recorded_at: 2025-03-02
tags:
  - knowledge graph
---
Built with [[Acme Lab]].
""", encoding="utf-8")
        self.spec = {"id": "primary", "name": "Test vault",
                     "root": self.root, "dirs": ["wiki", "Projects"],
                     "primary": True}
        self.patches = [
            mock.patch.object(worldgraph.atlas, "compose",
                              side_effect=lambda vault=False: _atlas_graph()),
            mock.patch.object(worldgraph.crm, "_load", return_value={
                "by_id": {"p_alice": {"id": "p_alice"}},
                "master": {}, "profiles": {}}),
            mock.patch.object(worldgraph.vault, "source_specs",
                              return_value=[self.spec]),
            mock.patch.object(
                worldgraph.worldlayout, "positions",
                side_effect=lambda nodes, edges, page_to_node: (
                    {node["id"]: [float(i), 0.0, 0.0]
                     for i, node in enumerate(nodes)},
                    {"basis": "test", "dimensions": 3,
                     "vector_nodes": len(nodes),
                     "neighbor_nodes": 0,
                     "semantic_nodes": len(nodes), "fallback_nodes": 0,
                     "placed_nodes": len(nodes),
                     "total_nodes": len(nodes), "coverage": 1.0,
                     "semantic_coverage": 1.0})),
            mock.patch.object(worldgraph.settings, "get",
                              side_effect=lambda key: "Owner Example"
                              if key == "owner_name" else ""),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_composes_typed_nodes_and_folds_person_identity(self):
        graph = worldgraph.compose()
        self.assertEqual(graph["schema"], "vira.world.v1")
        by_name = {node["name"]: node for node in graph["nodes"]}
        self.assertEqual(by_name["Alice Example"]["id"], "p_alice")
        self.assertEqual(by_name["Alice Example"]["kind"], "person")
        self.assertEqual(by_name["Alice Example"]["note_ref"],
                         "wiki/alice.md")
        self.assertEqual(by_name["Acme Lab"]["kind"], "organization")
        self.assertEqual(by_name["World Map"]["kind"], "project")
        self.assertEqual({row["id"] for row in graph["kinds"]},
                         {"person", "organization", "project", "topic"})
        self.assertEqual(graph["timeline"]["valid"]["min"],
                         "2020-01-01T00:00:00+00:00")
        self.assertEqual(graph["timeline"]["recorded"]["min"],
                         "2024-01-03T00:00:00+00:00")
        self.assertEqual(graph["layout"]["basis"], "test")
        self.assertTrue(all(len(node["position"]) == 3
                            for node in graph["nodes"]))

    def test_wikilinks_and_tags_keep_receipts(self):
        graph = worldgraph.compose()
        wiki = [edge for edge in graph["edges"]
                if edge["relation"] == "wikilink"]
        # The two notes that point at each other are one visual relation with
        # two receipts, not two perfectly overlapping lines.
        self.assertEqual(len(wiki), 2)
        self.assertIn(2, [len(edge["receipts"]) for edge in wiki])
        self.assertTrue(all(edge["receipts"][0]["ref"].endswith(".md")
                            for edge in wiki))
        self.assertTrue(all(edge["receipts"][0]["line"] > 1
                            for edge in wiki))
        tagged = [edge for edge in graph["edges"]
                  if edge["relation"] == "tagged"]
        self.assertEqual(len(tagged), 3)
        topic = next(node for node in graph["nodes"]
                     if node["kind"] == "topic" and
                     node["name"] == "knowledge graph")
        # A derived shared tag becomes known at the second supporting note,
        # not at the latest note that happened to reuse it.
        self.assertEqual(topic["recorded_at"],
                         "2024-02-10T00:00:00+00:00")

    def test_valid_time_replay_and_kind_filter(self):
        graph = worldgraph.compose(at="2022-01-01", axis="valid")
        names = {node["name"] for node in graph["nodes"]}
        self.assertIn("Alice Example", names)
        self.assertIn("Acme Lab", names)
        self.assertNotIn("World Map", names)
        people = worldgraph.compose(kinds=["person"])
        self.assertEqual([node["name"] for node in people["nodes"]],
                         ["Alice Example"])
        self.assertEqual(people["edges"], [])

    def test_world_includes_people_outside_the_legacy_atlas_slice(self):
        cache = {
            "people": [{"id": "p_alice", "name": "Alice Example"},
                       {"id": "p_bob", "name": "Bob Example",
                        "activity": {"imsg_n": 2}}],
            "by_id": {"p_alice": {"id": "p_alice"},
                      "p_bob": {"id": "p_bob"}},
            "master": {}, "profiles": {},
        }
        with mock.patch.object(worldgraph.crm, "_load", return_value=cache):
            graph = worldgraph.compose()
        people = {node["name"] for node in graph["nodes"]
                  if node["kind"] == "person"}
        self.assertEqual(people, {"Alice Example", "Bob Example"})

    def test_recorded_time_is_independent_from_valid_time(self):
        graph = worldgraph.compose(at="2023-12-31", axis="recorded")
        names = {node["name"] for node in graph["nodes"]}
        # Unknown CRM record time remains visible; every vault assertion was
        # recorded later and therefore has not entered the system yet.
        self.assertEqual(names, {"Alice Example"})
        self.assertEqual(graph["replay"],
                         {"at": "2023-12-31", "axis": "recorded"})

    def test_node_detail_names_the_other_endpoint(self):
        graph = worldgraph.compose()
        acme = next(node for node in graph["nodes"]
                    if node["name"] == "Acme Lab")
        detail = worldgraph.node_detail(acme["id"])
        linked = {edge["name"] for edge in detail["edges"]}
        self.assertIn("Alice Example", linked)
        self.assertIn("World Map", linked)

    def test_invalid_replay_axis_and_date_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "axis"):
            worldgraph.compose(axis="transaction")
        with self.assertRaisesRegex(ValueError, "invalid at date"):
            worldgraph.compose(at="some day")

    def test_world_does_not_stop_at_the_old_four_hundred_note_boundary(self):
        for index in range(405):
            (self.root / "wiki" / f"bulk-{index:03}.md").write_text(
                f"# Bulk {index}\n", encoding="utf-8")
        graph = worldgraph.compose()
        self.assertGreaterEqual(graph["scope"]["shown_notes"], 408)
        self.assertEqual(graph["scope"]["shown_notes"],
                         graph["scope"]["total_notes"])
        self.assertFalse(graph["scope"]["truncated"])

    def test_full_response_bytes_are_reused_for_the_same_graph(self):
        first = worldgraph.encoded()
        second = worldgraph.encoded()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()

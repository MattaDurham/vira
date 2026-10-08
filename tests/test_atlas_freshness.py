"""Automatic contact graph recovery through real connection/read/build joins."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from server import (atlas, contactcard, crmindex, data as crm,
                    dataconnections, jsonstore, mediaindex, photos, settings, vault)


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.crm = self.root / "crm"
        self.seed(self.crm)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"crm_root": str(self.crm),
                               "fixture_mode": False, "owner_name": "Owner",
                               "notify_handle": ""}), encoding="utf-8")
        self.pending = []
        patches = [
            mock.patch.object(settings, "CONFIG_PATH", self.config),
            mock.patch.object(crm, "_cache", {"loaded_at": 0}),
            mock.patch.object(contactcard, "STORE", self.root / "cards.json"),
            mock.patch.object(crmindex, "DB", self.root / "crm-index.sqlite"),
            mock.patch.object(atlas, "GRAPH", self.root / "graph.json"),
            mock.patch.object(atlas, "GROUPS", self.root / "groups.json"),
            mock.patch.object(atlas, "_building", threading.Event()),
            mock.patch.object(atlas, "_retry_at", 0.0),
            mock.patch.object(atlas, "_build_error", None),
            mock.patch.object(atlas, "_spawn_refresh", side_effect=self.pending.append),
            mock.patch.object(atlas, "_after_build"),
            mock.patch.object(mediaindex, "DB", self.root / "no-media.sqlite"),
            mock.patch.object(vault, "DB_PATH", self.root / "no-vault.sqlite"),
            mock.patch.object(photos, "photo_path", return_value=None),
            mock.patch("server.brief._live_imsg_last", return_value={}),
            mock.patch("server.atlaslens._ab_index", return_value={}),
            mock.patch("server.circles.apply", side_effect=lambda g: g),
            mock.patch.object(dataconnections, "_idle"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        writer = jsonstore.write_atomic

        def guarded_write(path, *args, **kwargs):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root.resolve()),
                            "Test attempted a real-store write")
            return writer(path, *args, **kwargs)

        patch = mock.patch.object(jsonstore, "write_atomic", side_effect=guarded_write)
        patch.start()
        self.addCleanup(patch.stop)

    def seed(self, root, extra=False):
        root.mkdir(exist_ok=True)
        (root / "profiles").mkdir(exist_ok=True)
        people = [{"id": "p_example", "name": "Casey Example", "profile_tier": "B",
                   "handles": {"emails": ["casey@example.com"]},
                   "activity": {"imsg_n": 5, "imsg_last": "2026-01-01"}}]
        if extra:
            people.append({"id": "p_second", "name": "Drew Sample", "master_tier": "B",
                           "handles": {}, "activity": {"email_n": 2}})
        (root / "people.json").write_text(json.dumps({"people": people}), encoding="utf-8")
        (root / "master.json").write_text("[]", encoding="utf-8")

    def finish(self):
        self.assertEqual(len(self.pending), 1)
        atlas._run_refresh(self.pending.pop())
        self.assertFalse(atlas._building.is_set())

    def test_first_read_builds_without_another_window_or_startup(self):
        self.assertTrue(atlas.compose()["building"])
        atlas.compose()
        self.assertEqual(len(self.pending), 1)  # concurrent polls coalesce
        self.finish()
        graph = atlas.compose()
        self.assertFalse(graph["stale"])
        self.assertEqual([n["id"] for n in graph["nodes"]], ["p_example"])
        self.assertEqual(self.pending, [])  # unchanged reads never rebuild

    def test_real_reconnection_replaces_a_legacy_graph(self):
        atlas.build_graph()
        old = atlas._read()
        old.pop("source")  # cache shipped before freshness tracking
        atlas._write(old)
        new = self.root / "reconnected"
        self.seed(new, extra=True)
        before = {p: p.read_bytes() for p in new.rglob("*.json")}
        plan = dataconnections.preview({"kind": "crm", "path": str(new)})
        self.assertTrue(plan["valid"], plan)
        dataconnections.connect(plan["request"], plan["revision"])
        self.assertEqual(len(crm.search_people()), 2)
        self.assertTrue(atlas.compose()["stale"])
        self.finish()
        graph = atlas.compose()
        self.assertEqual({n["id"] for n in graph["nodes"]}, {"p_example", "p_second"})
        self.assertFalse(graph["stale"])
        self.assertEqual({p: p.read_bytes() for p in new.rglob("*.json")}, before)

    def test_import_into_same_root_bypasses_people_cache_ttl(self):
        atlas.build_graph()
        self.seed(self.crm, extra=True)
        self.assertEqual(len(crm.search_people()), 1)  # reader still cached
        self.assertTrue(atlas.compose()["building"])
        self.finish()
        self.assertEqual(len(atlas.compose()["nodes"]), 2)

    def test_demo_to_real_transition_is_detected(self):
        demo = self.root / "demo"
        self.seed(demo)
        with mock.patch.object(crm, "_crm", return_value=demo):
            atlas.build_graph()
        self.assertTrue(atlas.compose()["building"])
        self.finish()
        self.assertFalse(atlas.compose()["stale"])

    def test_profile_edit_and_parameter_changes_trigger_refresh(self):
        atlas.build_graph()
        (self.crm / "profiles" / "p_example.json").write_text(
            '{"relationship_class":"family"}', encoding="utf-8")
        self.assertTrue(atlas.compose()["stale"])
        self.finish()
        self.assertEqual(atlas.compose()["nodes"][0]["relationship_class"], "family")
        from server import onboard
        onboard.config_set(atlas_min_edge_weight=0.3)
        self.assertTrue(atlas.compose()["stale"])
        self.finish()
        self.assertFalse(atlas.compose()["stale"])

    def test_failure_preserves_graph_and_retries_after_backoff(self):
        atlas.build_graph()
        before = atlas.GRAPH.read_bytes()
        (self.crm / "people.json").write_text("{", encoding="utf-8")
        atlas.compose()
        with mock.patch.object(atlas._log, "exception"):
            self.finish()
        self.assertEqual(atlas.GRAPH.read_bytes(), before)
        self.assertIn("error", atlas.compose())
        self.assertEqual(self.pending, [])
        self.seed(self.crm, extra=True)
        with mock.patch.object(atlas.time, "monotonic", return_value=atlas._retry_at + 1):
            self.assertTrue(atlas.compose()["building"])
        self.finish()
        self.assertEqual(len(atlas.compose()["nodes"]), 2)
        self.assertNotIn("error", atlas.compose())

    def test_source_change_during_build_is_never_marked_fresh(self):
        original = atlas.build_edges

        def changed(*args):
            self.seed(self.crm, extra=True)
            return original(*args)

        with mock.patch.object(atlas, "build_edges", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "CRM changed"):
                atlas.build_graph()
        self.assertFalse(atlas.GRAPH.exists())


if __name__ == "__main__":
    unittest.main()

"""Synthetic research workflow joins; never use owner sources or stores."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import circuits, instance, readingroom, research, researchtopics as topics, routines, settings, vaultwrite, viratools


def result():
    source = {"source_id": "root", "title": "Example lecture", "url": "https://example.com/watch?v=one",
              "canonical_url": "https://example.com/watch?v=one", "event_id": "lecture-one",
              "publication_date": "2026-01-01", "speaker_name": "Example Speaker", "speaker_verified": True,
              "scope": "primary", "coverage": "MISSING", "relationship": "original",
              "verified_excerpts": [{"text": "Start small and test", "locator": "02:10"}],
              "verification": {"status": "verified", "checked_at": "2026-01-02T00:00:00Z", "basis": "Fetched the original recording"}}
    return {"summary": "Evidence favors small experiments.", "sources": [source],
            "claims": [{"claim_id": "experiments", "claim_label": "Test with small experiments", "category": "method",
                        "evidence": [{"source_id": "root", "text": "Start small and test", "locator": "02:10"}]}],
            "limitations": ["One lecture"], "terms": [], "timeline": [], "gaps": [], "review_queue": []}


class ResearchTopicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        spec = {"id": "primary", "name": "Example vault", "root": self.vault, "write_enabled": True,
                "read_enabled": True, "model_exposure": True, "write_scope": "all", "protected_dirs": [],
                "capture_dir": "inbox", "primary": True, "policy_explicit": True}
        for target, value in [
            ("server.vaultwrite.LOCK_ROOT", self.root / "data/vault-locks"),
            ("server.researchtopics.ROOT", self.root), ("server.researchtopics.STORE", self.root / "data/topics.json"),
            ("server.circuits.DEFS", self.root / "data/circuits.json"), ("server.circuits.RUNS", self.root / "data/runs.json"),
            ("server.routines.ROOT", self.root), ("server.routines.STORE", self.root / "data/routines.json"),
            ("server.routines.SEEDS", []), ("server.readingroom.ROOT", self.root),
            ("server.readingroom.ROOMS_DIR", self.root / "data/reading/rooms"),
            ("server.readingroom.PAGES_DIR", self.root / "static/reading"),
        ]:
            patch = mock.patch(target, value); patch.start(); self.addCleanup(patch.stop)
        for target, kwargs in [
            ("server.vaultwrite._specs", {"return_value": [spec]}),
            ("server.vaultwrite._index_source", {"side_effect": lambda spec, receipt: receipt}),
            ("server.modulemodels.selection", {"return_value": None}),
            ("server.instance.id", {"return_value": "test-instance"}),
            ("server.instance.api_url", {"return_value": "http://localhost:8399"}),
            ("server.instance.owns", {"return_value": True}),
            ("server.settings.get", {"side_effect": lambda key: copy.deepcopy(settings.DEFAULTS[key])}),
            ("server.settings.raw", {"return_value": {}}),
            ("server.readingroom._ping_additions", {}),
        ]:
            patch = mock.patch(target, **kwargs); obj = patch.start(); self.addCleanup(patch.stop)
            if target.endswith("_ping_additions"): self.ping = obj
        self.addCleanup(self._guard)

    def _guard(self):
        self.assertTrue(str(topics.STORE).startswith(str(self.root)))
        self.assertTrue(str(readingroom.ROOMS_DIR).startswith(str(self.root)))

    def create(self):
        public = topics.create("How do small experiments work?", "primary")
        return topics.get(public["id"])

    def publish(self, topic, payload=None):
        payload = payload or result()
        for stage in ("scope", "inventory", "discovery", "analyze"):
            topics.save_packet(topic["id"], topic["generation"], stage, {"complete": True})
        topics.save_packet(topic["id"], topic["generation"], "verify", {"sources": payload["sources"]})
        return topics.publish(topic["id"], topic["generation"], payload)

    def test_create_dispatches_parallel_discovery_then_independent_audit(self):
        topic = self.create()
        run = circuits.get_run(topic["run_id"])
        stages = {s["id"]: s for s in run["stages_def"]}
        self.assertEqual(stages["inventory"]["needs"], ["scope"])
        self.assertEqual(stages["discovery"]["needs"], ["scope"])
        self.assertEqual(set(stages["verify"]["needs"]), {"inventory", "discovery"})
        self.assertEqual(stages["publish"]["needs"], ["analyze"])
        self.assertEqual(topics.launch(topic["id"])["run_id"], run["id"])
        self.assertEqual(len(circuits.list_runs()), 1)
        self.assertFalse(list(self.vault.rglob("*.md")))
        self.assertFalse(routines.list_routines())

    def test_publication_joins_graph_library_reader_and_refresh(self):
        topic = self.create()
        self.publish(topic)
        saved = topics.get(topic["id"])
        self.assertEqual(saved["status"], "ready")
        note = self.vault / saved["vault_relative"]
        self.assertIn("https://example.com/watch?v=one", note.read_text(encoding="utf-8"))
        room = readingroom.load_room(saved["room"])
        self.assertEqual(len(room["items"]), 1)
        self.assertTrue(routines.get_routine(saved["routine_id"])["enabled"])
        overview = research.overview(topic["id"])
        self.assertEqual(overview["claims"][0]["organization_rollup"]["distinct_event_count"], 1)
        claim = research.claim_detail("experiments", topic["id"])
        self.assertEqual(claim["evidence"][0]["source"]["source_id"], "root")
        self.assertEqual(research.source_detail("root", topic["id"])["external_url"], result()["sources"][0]["url"])
        self.ping.assert_not_called()

    def test_repost_does_not_inflate_speakers_events_or_utterances(self):
        payload = result()
        copy_source = {**copy.deepcopy(payload["sources"][0]), "source_id": "copy", "url": "https://example.com/repost", "relationship": "repost"}
        payload["sources"].append(copy_source)
        payload["claims"][0]["evidence"].append({"source_id": "copy", "text": "Start small and test", "locator": "02:10"})
        checked = topics.validate_result(payload)
        rollup = topics._rollup(topics._evidence(checked, checked["claims"][0]))
        self.assertEqual(rollup["distinct_speaker_count"], 1)
        self.assertEqual(rollup["distinct_event_count"], 1)
        self.assertEqual(rollup["utterance_count"], 1)
        self.assertEqual(rollup["appearance_count"], 2)
        copy_source["event_id"] = "invented-second-event"
        with self.assertRaisesRegex(ValueError, "share its event_id"):
            topics.validate_result(payload)

    def test_unverified_and_unlocated_claims_never_publish(self):
        for failure in ("unverified", "locator"):
            with self.subTest(failure=failure):
                payload = result()
                if failure == "unverified": payload["sources"][0]["verification"]["status"] = "inaccessible"
                else: payload["claims"][0]["evidence"][0].pop("locator")
                with self.assertRaises(ValueError): topics.validate_result(payload)
        payload = result()
        payload["claims"][0]["evidence"] = [{"source_id": "root", "locator": "a", "text": " ".join(["first"] * 13)},
                                           {"source_id": "root", "locator": "b", "text": " ".join(["second"] * 13)}]
        payload["sources"][0]["verified_excerpts"] = [{"text": e["text"], "locator": e["locator"]} for e in payload["claims"][0]["evidence"]]
        with self.assertRaisesRegex(ValueError, "24 total words"):
            topics.validate_result(payload)

    def test_stale_generations_and_unaudited_sources_are_refused(self):
        topic = self.create()
        with self.assertRaisesRegex(ValueError, "stale"):
            topics.save_packet(topic["id"], "old", "scope", {})
        with self.assertRaisesRegex(ValueError, "complete research packets"):
            topics.publish(topic["id"], topic["generation"], result())
        for stage in ("scope", "inventory", "discovery", "analyze"):
            topics.save_packet(topic["id"], topic["generation"], stage, {"complete": True})
        checked = result()["sources"]
        checked[0]["source_id"] = "different"
        topics.save_packet(topic["id"], topic["generation"], "verify", {"sources": checked})
        with self.assertRaisesRegex(ValueError, "match the independent"):
            topics.publish(topic["id"], topic["generation"], result())
        self.assertFalse(list(self.vault.rglob("*.md")))

    def test_owner_edit_blocks_refresh_without_overwriting_or_losing_result(self):
        topic = self.create(); self.publish(topic)
        topic = topics.get(topic["id"])
        note = self.vault / topic["vault_relative"]
        note.write_text("Owner's edited note\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "note changed"):
            self.publish(topic)
        self.assertEqual(note.read_text(encoding="utf-8"), "Owner's edited note\n")
        self.assertEqual(topics.get(topic["id"])["result"]["summary"], result()["summary"])

    def test_refresh_preserves_older_sources_and_reader_items(self):
        topic = self.create(); self.publish(topic)
        updated = result()
        updated["sources"][0].update(source_id="new", url="https://example.com/new", canonical_url="https://example.com/new", event_id="new-event")
        updated["sources"][0]["verified_excerpts"] = [{"text": "Measure results", "locator": "Conclusion"}]
        updated["claims"][0].update(claim_id="new-claim", evidence=[{"source_id": "new", "text": "Measure results", "locator": "Conclusion"}])
        self.publish(topics.get(topic["id"]), updated)
        saved = topics.get(topic["id"])
        self.assertEqual(len(saved["result"]["sources"]), 2)
        self.assertEqual(len(saved["result"]["claims"]), 2)
        self.assertEqual(len(saved["history"]), 1)
        self.assertEqual(len(readingroom.load_room(saved["room"])["items"]), 2)
        self.ping.assert_not_called()
        topics.configure_refresh(topic["id"], 0)
        self.assertFalse(routines.get_routine(saved["routine_id"])["enabled"])

    def test_routine_refresh_runs_the_same_workflow(self):
        topic = self.create(); self.publish(topic)
        saved = topics.get(topic["id"])
        routines_record = routines.get_routine(saved["routine_id"])
        circuits.cancel_run(saved["run_id"])
        with mock.patch("server.routines._ai_ready", return_value=True):
            launched = routines.dispatch(routines_record)
        self.assertNotEqual(launched["run_id"], saved["run_id"])
        self.assertEqual(circuits.get_run(launched["run_id"])["circuit_id"], "research-anything-v1")
        self.assertEqual(routines.get_routine(saved["routine_id"])["last_run_id"], launched["run_id"])

    def test_republication_preserves_a_paused_routine_and_latest_cadence(self):
        topic = self.create(); self.publish(topic)
        saved = topics.get(topic["id"])
        routine = routines.get_routine(saved["routine_id"])
        routines.save_routine({**routine, "enabled": False}, rid=routine["id"])
        self.publish(topics.get(topic["id"]))
        self.assertFalse(routines.get_routine(routine["id"])["enabled"])
        self.assertFalse(topics.public(topics.get(topic["id"]))["refresh_enabled"])
        topics.configure_refresh(topic["id"], 24)
        self.publish(topics.get(topic["id"]))
        self.assertEqual(routines.get_routine(routine["id"])["every_hours"], 24)
        self.assertTrue(routines.get_routine(routine["id"])["enabled"])

    def test_all_existing_sources_still_get_a_linked_reader_room(self):
        topic = self.create()
        payload = result(); payload["sources"][0]["coverage"] = "HAVE"
        self.publish(topic, payload)
        saved = topics.get(topic["id"])
        item = readingroom.load_room(saved["room"])["items"][0]
        self.assertEqual(item["status"], "HAVE")
        self.assertEqual(item["research_graph"], topic["id"])
        self.assertEqual(item["research_source_id"], "root")

    def test_primary_evidence_counts_do_not_depend_on_discovery_order(self):
        payload = result()
        context = {**copy.deepcopy(payload["sources"][0]), "source_id": "context", "scope": "context"}
        payload["sources"].insert(0, context)
        payload["claims"][0]["evidence"].insert(0, {"source_id": "context", "text": "Start small and test", "locator": "02:10"})
        evidence = topics._evidence(topics.validate_result(payload), payload["claims"][0])
        self.assertEqual(topics._rollup(evidence)["distinct_event_count"], 1)
        self.assertEqual(evidence[0]["source"]["source_id"], "root")

    def test_writes_are_absent_from_read_only_native_sessions(self):
        exposed = viratools.dynamic_tool_specs(read_only=True)[0]["tools"]
        names = {t["name"] for t in exposed}
        self.assertIn("research_topic", names)
        self.assertNotIn("save_research_packet", names)
        self.assertNotIn("publish_research_topic", names)

    def test_disconnected_and_protected_destinations_create_nothing(self):
        with mock.patch("server.vaultwrite._specs", return_value=[]):
            with self.assertRaises(ValueError): topics.create("Example", "primary")
        self.assertFalse(topics.STORE.exists())
        for hours in (float("nan"), float("inf"), -1, 1):
            with self.assertRaises(ValueError): topics.create("Example", "primary", hours)
        self.assertFalse(topics.STORE.exists())


if __name__ == "__main__":
    unittest.main()

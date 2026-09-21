"""Definitions, Reader ingestion and person stubs obey one destination policy."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import define, fullingest, readingroom, roomvault, vault, vaultpeople, vaultwrite
from tests.test_define import CARD
from tests.test_fullingest import item
from tests.vault_fixture import isolate


class Destinations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.primary = self.root / "research"
        self.extra = self.root / "household"
        (self.primary / "wiki").mkdir(parents=True)
        self.extra.mkdir()
        self.config = isolate(self, self.primary)
        self.extra_config = {
            "id": "household", "name": "Household", "root": str(self.extra),
            "dirs": ["."], "read_enabled": True, "write_enabled": True,
            "model_exposure": True, "capture_dir": "notes/inbox",
            "write_dirs": ["notes/inbox"], "protected_dirs": ["canon", "raw"],
        }
        self.config.update(vault_default_destination="primary",
                           vault_sources=[self.extra_config])
        self.rooms = self.root / "rooms"
        self.rooms.mkdir()
        for patch in (
            mock.patch.object(define, "STORE", self.root / "glossary.json"),
            mock.patch.object(define, "LOCK", self.root / "glossary.build"),
            mock.patch.object(define.atlasterms, "lookup", return_value=None),
            mock.patch.object(readingroom, "ROOT", self.root),
            mock.patch.object(readingroom, "ROOMS_DIR", self.rooms),
            mock.patch.object(roomvault, "LINKS_PATH", self.root / "links.json"),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        fullingest._summaries_cache.clear()
        roomvault._notes_cache.clear()

    def room(self):
        readingroom.build("garden", "Garden", "", [item()])

    def test_same_term_is_banked_and_reopened_in_its_own_namespace(self):
        primary = define.save(CARD, destination="primary")
        private_card = dict(CARD, rows=[{"key": "plain_definition",
                                       "label": "Plain definition",
                                       "value": "The household sense."}])
        extra = define.save(private_card, destination="household")
        self.assertEqual(extra["path"], "@household/notes/inbox/definitions/byzantine-fault-tolerance.md")
        self.assertEqual(extra["note"], extra["path"])
        self.assertEqual(primary["path"], "wiki/byzantine-fault-tolerance.md")
        with mock.patch.object(define, "_compose") as compose:
            back = define.lookup(CARD["term"], destination="household")
            other = define.lookup(CARD["term"], destination="primary")
        compose.assert_not_called()
        self.assertEqual(back["rows"][0]["value"], "The household sense.")
        self.assertEqual(other["rows"][0]["value"], CARD["rows"][0]["value"])
        self.assertEqual(len(define.index()["terms"]), 2)

    def test_legacy_cache_never_reuses_another_roots_absolute_pointer(self):
        path = self.extra / "secret.md"
        path.write_text(define.note_text(CARD, {}), encoding="utf-8")
        define.STORE.write_text(json.dumps({"terms": {
            define._norm(CARD["term"]): {"path": str(path)}}}), encoding="utf-8")
        self.assertIsNone(define.entry(CARD["term"], "primary"))

    def test_protected_canon_is_not_a_definition_update_target(self):
        canon = self.extra / "canon" / "terms.md"
        canon.parent.mkdir()
        original = define.note_text(CARD, {})
        canon.write_text(original, encoding="utf-8")
        saved = define.save(CARD, destination="household")
        self.assertTrue(saved["path"].startswith("@household/notes/inbox/definitions/"))
        self.assertEqual(canon.read_text(encoding="utf-8"), original)
        self.assertFalse((self.primary / "wiki/byzantine-fault-tolerance.md").exists())

    def test_invalid_read_only_and_ambiguous_writes_do_not_fall_back(self):
        for destination in ("missing", "household"):
            self.extra_config["write_enabled"] = False
            with self.assertRaises(define.DefineError):
                define.save(CARD, destination=destination)
        self.extra_config["write_enabled"] = True
        self.config["vault_default_destination"] = ""
        with self.assertRaises(define.DefineError):
            define.save(CARD)
        self.assertEqual(list(self.primary.rglob("*.md")), [])

    def test_context_and_model_exposure_are_enforced_before_composition(self):
        with mock.patch.object(define, "_compose") as compose:
            with self.assertRaises(define.DefineError):
                define.lookup("quorum", destination="primary", source={
                    "path": "@household/notes/inbox/thought.md", "text": "Private context."})
            self.extra_config["model_exposure"] = False
            with self.assertRaises(define.DefineError):
                define.lookup("quorum", destination="household")
        compose.assert_not_called()
        with self.assertRaises(define.DefineError):
            define.save(dict(CARD, context_sources=["household"]), destination="primary")

    def test_retrieved_context_stays_within_destination(self):
        spec = define._destination("primary")
        with mock.patch.object(vault, "search", return_value=[
            {"vault_id": "household", "path": "@household/a.md", "text": "Private"},
            {"vault_id": "primary", "path": "wiki/a.md", "text": "Research"},
        ]) as search:
            context = define._context("quorum", spec=spec)
        self.assertEqual([c["text"] for c in context], ["Research"])
        self.assertTrue(search.call_args.kwargs["for_model"])

    def test_excluded_folder_never_enters_pinned_model_context_or_source_job(self):
        saved = define.save(CARD, destination="household")
        self.extra_config["model_exclude_dirs"] = ["notes/inbox"]
        with self.assertRaises(define.DefineError):
            define._context("quorum", {"path": saved["path"], "text": "Confidential."},
                            spec=define._destination("household"))
        with self.assertRaises(define.DefineError):
            define.lookup(CARD["term"], destination="household", for_model=True)
        with self.assertRaises(define.DefineError):
            define.source_prompt(CARD["term"], destination="household")
        # Local viewing remains possible under its separate read permission.
        self.assertTrue(define.lookup(CARD["term"], destination="household")["cached"])

    def test_source_upgrade_names_governed_destination_path_and_hash(self):
        saved = define.save(CARD, destination="household")
        prompt = define.source_prompt(CARD["term"], destination="household")
        self.assertIn("vault_update", prompt)
        self.assertIn(saved["path"], prompt)
        self.assertIn('destination "household"', prompt)
        self.assertIn("expected_hash", prompt)
        self.assertIn("Do not write files with shell", prompt)

    def test_a_stale_definition_card_cannot_replace_an_owner_edit(self):
        card = define.save(CARD, destination="household")
        path = self.extra / card["path"].split("/", 1)[1]
        path.write_text(path.read_text(encoding="utf-8") + "\nOwner addition.\n", encoding="utf-8")
        revised = dict(card, rows=[{"key": "plain_definition",
                                  "label": "Plain definition", "value": "Revised."}])
        with self.assertRaises(define.DefineError):
            define.save(revised, destination="household")
        self.assertIn("Owner addition.", path.read_text(encoding="utf-8"))

    def test_definition_created_during_model_call_is_not_overwritten(self):
        def compose(term, context):
            owner = dict(CARD, term=term, rows=[{"key": "plain_definition",
                                               "label": "Plain definition", "value": "Owner version."}])
            define.save(owner, destination="household")
            return dict(CARD, term=term)
        with mock.patch.object(define, "_compose", side_effect=compose), \
             mock.patch.object(define, "_context", return_value=[]):
            result = define.lookup("quorum", destination="household")
        self.assertIn("already exists", result["write_error"])
        self.assertIn("Owner version.", (self.extra / "notes/inbox/definitions/quorum.md").read_text(encoding="utf-8"))

    def test_default_destination_change_during_model_call_does_not_reroute(self):
        def compose(term, context):
            self.config["vault_default_destination"] = "household"
            return dict(CARD, term=term)
        with mock.patch.object(define, "_compose", side_effect=compose), \
             mock.patch.object(define, "_context", return_value=[]):
            result = define.lookup("quorum")
        self.assertEqual(result["source_id"], "primary")
        self.assertTrue((self.primary / "wiki/quorum.md").is_file())
        self.assertEqual(list(self.extra.rglob("*.md")), [])

    def test_ingest_and_rebuild_keep_the_original_destination(self):
        self.room()
        fullingest.set_destination("garden", "household")
        with mock.patch.object(fullingest, "fetch_article", return_value=("Article", "a" * 500)):
            result = fullingest.stage("garden")
        self.assertEqual(result["source_id"], "household")
        captured = list((self.extra / "notes/inbox/reading-room").glob("*.md"))
        self.assertEqual(len(captured), 1)
        original = captured[0].read_text(encoding="utf-8")
        readingroom.build("garden", "Garden revised", "", [item()])
        self.assertEqual(readingroom.load_room("garden")["vault_destination"], "household")
        with mock.patch.object(fullingest, "fetch_article") as fetch:
            repeated = fullingest.stage("garden")
        fetch.assert_not_called()
        self.assertEqual(repeated["counts"], {"already": 1})
        self.assertEqual(captured[0].read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.primary.rglob("*.md")), [])

    def test_room_hub_and_summary_reopen_with_source_qualified_paths(self):
        self.room()
        fullingest.set_destination("garden", "household")
        result = roomvault.ingest("garden")
        self.assertEqual(result["hub"], "@household/notes/inbox/rooms/garden-reading-room.md")
        wiki = self.extra / "wiki"
        wiki.mkdir()
        summary = wiki / "garden.md"
        summary.write_text("---\ntype: source-summary\nroom_item_id: " + item()["id"] + "\n---\nGarden\n",
                           encoding="utf-8")
        fullingest.reconcile("garden")
        room = readingroom.load_room("garden")
        self.assertEqual(room["items"][0]["vault"], "@household/wiki/garden.md")
        annotated = roomvault.resolve("garden", room["items"])
        self.assertEqual(annotated[0]["vault_note"], "@household/wiki/garden.md")

    def test_explicit_different_room_destination_cannot_drift_or_copy(self):
        self.room()
        fullingest.set_destination("garden", "household")
        with self.assertRaises(fullingest.StageError):
            fullingest.stage("garden", destination="primary")
        self.extra_config["write_enabled"] = False
        with self.assertRaises(fullingest.StageError):
            fullingest.stage("garden")
        self.assertEqual(list(self.primary.rglob("*.md")), [])

    def test_selected_ingest_inherits_room_destination_and_refuses_root_override(self):
        self.room()
        fullingest.set_destination("garden", "household")
        with mock.patch.object(fullingest, "fetch_article", return_value=("Article", "a" * 500)):
            result = fullingest.stage_items([item()], "garden")
        self.assertEqual(result["source_id"], "household")
        self.assertTrue(result["outcomes"][0]["path"].startswith("@household/notes/inbox/reading-room/"))
        with self.assertRaises(fullingest.StageError):
            fullingest.stage_items([item()], "garden", root=self.primary)
        self.assertEqual(list(self.primary.rglob("*.md")), [])

    def test_symlink_gates_apply_to_direct_ingest(self):
        (self.extra / "notes").mkdir()
        try:
            (self.extra / "notes/inbox").symlink_to(self.primary, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(fullingest.StageError):
            fullingest.stage_item(item(), "garden", self.extra)
        self.assertEqual(list(self.primary.rglob("*.md")), [])

    def test_retirement_keeps_an_owner_edit_that_races_the_archive_copy(self):
        self.room()
        fullingest.set_destination("garden", "primary")
        summary = self.primary / "wiki/summary.md"
        summary.write_text("---\ntype: source-summary\nroom_item_id: " + item()["id"] + "\n---\nSummary\n",
                           encoding="utf-8")
        pointer = self.primary / "wiki/rooms/pointer.md"
        pointer.parent.mkdir()
        pointer.write_text("---\nroom_item_id: " + item()["id"] + "\n---\nPointer\n", encoding="utf-8")
        write = vaultwrite.write_note

        def racing_write(spec, rel, text, **kwargs):
            receipt = write(spec, rel, text, **kwargs)
            if rel.startswith("pending-user-deletion/"):
                pointer.write_text("Owner replacement\n", encoding="utf-8")
            return receipt

        with mock.patch.object(vaultwrite, "write_note", side_effect=racing_write):
            result = fullingest.reconcile("garden")
        self.assertEqual(result["retired"], 0)
        self.assertEqual(result["retirement_skipped"], 1)
        self.assertEqual(pointer.read_text(encoding="utf-8"), "Owner replacement\n")
        self.assertTrue((self.primary / "pending-user-deletion/rooms/pointer.md").is_file())

    def test_person_stubs_are_scoped_and_cannot_reuse_default_on_failure(self):
        (self.extra / "wiki").mkdir()
        with self.assertRaises(ValueError):
            vaultpeople.create_stub("Example Person", destination="household")
        self.extra_config["write_dirs"].append("wiki")
        result = vaultpeople.create_stub("Example Person", destination="household")
        self.assertEqual(result["ref"], "@household/wiki/example-person.md")
        self.assertFalse((self.primary / "wiki/example-person.md").exists())

    def test_definition_source_job_receives_the_frozen_destination(self):
        from fastapi.testclient import TestClient
        from server import main
        define.save(CARD, destination="household")
        with mock.patch.object(main.jobs, "launch", return_value="test-job") as launch:
            response = TestClient(main.app).post("/api/define/source", json={
                "term": CARD["term"], "destination": "household", "context": "family"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(launch.call_args.kwargs["vault_destination"], "household")
        self.assertEqual(launch.call_args.kwargs["vault_context"], "family")
        self.assertIn("@household/notes/inbox/definitions/", launch.call_args.args[0])

    def test_room_api_persists_destination_and_ingests(self):
        from fastapi.testclient import TestClient
        from server import main
        self.room()
        client = TestClient(main.app)
        response = client.post("/api/reading/rooms/garden/destination", json={"destination": "household"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source_id"], "household")
        ingested = client.post("/api/reading/rooms/garden/ingest", json={"destination": "household"})
        self.assertEqual(ingested.status_code, 200, ingested.text)
        self.assertTrue(list(self.extra.rglob("*.md")))


if __name__ == "__main__":
    unittest.main()

"""Real connection joins against isolated, synthetic stores only."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import (contactcard, contactintel, crmindex, dataconnections as connections,
                    data as crm, instance, joblog, jsonstore, onboard,
                    profilerefresh, readinglist, session, settings, viratools)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cfg = self.root / "config.json"
        self.initial = self.root / "initial"
        self.defaults = {**settings.DEFAULTS, "crm_root": str(self.initial)}
        self.config({"owner_name": "Example", "vault_sources": [], "reader_sources": []})
        patches = [mock.patch.object(settings, "CONFIG_PATH", self.cfg),
                   mock.patch.object(settings, "DEFAULTS", self.defaults),
                   mock.patch.object(Path, "home", return_value=self.root),
                   mock.patch.object(settings, "FIXTURE_CRM", self.root / "fixture"),
                   mock.patch.object(contactcard, "STORE", self.root / "contact-cards.json"),
                   mock.patch.object(joblog, "STORE", self.root / "jobs-log.json"),
                   mock.patch.object(crmindex, "DB", self.root / "crm-index.sqlite"),
                   mock.patch.object(crm, "_cache", {"loaded_at": 0}),
                   mock.patch.object(onboard, "_build", {"running": False}),
                   mock.patch.object(session.sessions, "recent", return_value=[])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        writer = jsonstore.write_atomic

        def guarded_write(path, *args, **kwargs):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), "Test attempted a real-store write")
            return writer(path, *args, **kwargs)

        patch = mock.patch.object(jsonstore, "write_atomic", side_effect=guarded_write)
        patch.start()
        self.addCleanup(patch.stop)

    def config(self, value):
        self.cfg.write_text(json.dumps(value), encoding="utf-8")

    def stored(self):
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def seed(self, name="crm", pid="p_example", email="casey@example.com"):
        root = self.root / name
        (root / "profiles").mkdir(parents=True)
        person = {"id": pid, "name": "Casey Example", "handles": {"emails": [email], "phones10": [], "imessage": []}}
        (root / "people.json").write_text(json.dumps({"people": [person]}), encoding="utf-8")
        (root / "master.json").write_text(json.dumps([{"id": pid, "company": "Example Co"}]), encoding="utf-8")
        (root / "profiles" / f"{pid}.json").write_text(json.dumps({"id": pid, "relationship_summary": "Reviewed evidence"}), encoding="utf-8")
        return root

    def apply(self, request):
        plan = connections.preview(request)
        self.assertTrue(plan["valid"], plan)
        return connections.connect(plan["request"], plan["revision"])

    def test_existing_crm_preserves_bytes_ids_and_unrelated_configuration(self):
        root = self.seed()
        before = {p: p.read_bytes() for p in root.rglob("*.json")}
        request = {"kind": "crm", "path": str(root)}
        plan = connections.preview(request)
        self.assertTrue(plan["valid"])
        self.assertEqual(plan["summary"]["profiles"], 1)
        self.assertEqual(self.stored()["reader_sources"], [])
        self.apply(request)
        self.assertEqual(self.stored()["owner_name"], "Example")
        self.assertEqual(self.stored()["self_record"], str(self.initial / "self"))
        self.assertEqual(crm.search_people()[0]["id"], "p_example")
        self.assertEqual(crm.get_person("p_example")["profile"]["relationship_summary"], "Reviewed evidence")
        self.assertEqual({p: p.read_bytes() for p in root.rglob("*.json")}, before)

    def test_bad_paths_duplicate_ids_and_corrupt_files_cannot_change_config(self):
        root = self.seed()
        before = self.cfg.read_bytes()
        for path in ("", ".", str(self.root / "missing")):
            self.assertFalse(connections.preview({"kind": "crm", "path": path})["valid"])
        registry = root / "people.json"
        doc = json.loads(registry.read_text(encoding="utf-8"))
        doc["people"].append(doc["people"][0])
        registry.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(connections.preview({"kind": "crm", "path": str(root)})["valid"])
        doc["people"].pop()
        registry.write_text(json.dumps(doc), encoding="utf-8")
        (root / "master.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "master.json"):
            connections.connect({"kind": "crm", "path": str(root)}, "obsolete")
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_folder_and_configuration_changes_invalidate_preview(self):
        root = self.seed()
        request = {"kind": "crm", "path": str(root)}
        plan = connections.preview(request)
        profile = root / "profiles" / "p_example.json"
        profile.write_text('{"relationship_summary":"Changed"}', encoding="utf-8")
        before = self.cfg.read_bytes()
        with self.assertRaisesRegex(ValueError, "Inspect again"):
            connections.connect(request, plan["revision"])
        self.assertEqual(self.cfg.read_bytes(), before)
        plan = connections.preview(request)
        self.config({"crm_root": str(self.initial), "owner_name": "New choice"})
        with self.assertRaisesRegex(ValueError, "Inspect again"):
            connections.connect(request, plan["revision"])
        self.assertEqual(self.stored()["owner_name"], "New choice")

    def test_revalidation_inside_config_lock_rejects_changed_files(self):
        root = self.seed()
        request = {"kind": "crm", "path": str(root)}
        plan = connections.preview(request)
        original = onboard.config_set

        def race(**kwargs):
            (root / "master.json").write_text("[]", encoding="utf-8")
            return original(**kwargs)

        before = self.cfg.read_bytes()
        with mock.patch.object(onboard, "config_set", side_effect=race), self.assertRaisesRegex(ValueError, "Inspect again"):
            connections.connect(request, plan["revision"])
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_self_record_follow_and_explicit_output_overrides(self):
        root = self.seed()
        cfg = {"crm_root": str(self.initial), "self_record": str(self.root / "independent"),
               "applications_universe": str(self.root / "analysis"),
               "applications_packages_root": str(self.root / "packages")}
        self.config(cfg)
        request = {"kind": "crm", "path": str(root), "self_choice": "follow"}
        plan = connections.preview(request)
        outputs = plan["summary"]["self_record_after"]
        self.assertEqual(outputs["root"], str(root / "self"))
        self.assertEqual(outputs["analysis"], cfg["applications_universe"])
        self.apply(request)
        self.assertEqual(self.stored()["self_record"], "")
        self.assertEqual(self.stored()["applications_packages_root"], cfg["applications_packages_root"])

    def test_self_readiness_is_independent_of_connection_and_brain_permissions(self):
        root = self.root / "self-record"
        root.mkdir()
        request = {"kind": "self", "path": str(root)}
        plan = connections.preview(request)
        self.assertTrue(plan["valid"])
        self.assertFalse(plan["summary"]["career_ready"])
        self.apply(request)
        self.assertEqual(self.stored()["vault_sources"], [])
        (root / "canon").mkdir()
        (root / "canon" / "MASTER_HISTORY.md").write_text("# Example career", encoding="utf-8")
        self.assertTrue(connections.status()["self_record"]["career_ready"])

    def test_following_crm_self_record_is_ready_in_applications_front_door(self):
        from server import frontdoor
        root = self.seed()
        (root / "self" / "canon").mkdir(parents=True)
        (root / "self" / "canon" / "MASTER_HISTORY.md").write_text("# Example evidence", encoding="utf-8")
        self.config({"crm_root": str(root), "lab_root": str(self.root / "roles")})
        self.assertTrue(frontdoor._applications_state()["ready"])

    def test_fresh_target_stays_demo_until_import_and_cannot_replace_populated_crm(self):
        fresh = self.root / "fresh"
        self.apply({"kind": "crm", "mode": "fresh", "path": str(fresh)})
        self.assertFalse(fresh.exists(), "Connecting must not create or copy CRM content")
        self.assertTrue(settings.fixture_mode())
        onboard.import_google_csv("Name,E-mail 1 - Value\nCasey Example,casey@example.com\n")
        self.assertFalse(settings.fixture_mode())
        self.assertEqual(crm.search_people()[0]["name"], "Casey Example")
        self.assertFalse(connections.preview({"kind": "crm", "mode": "fresh", "path": str(fresh)})["valid"])

    def test_demo_default_self_location_is_shown_separately_from_configured_root(self):
        from server import applications
        state = connections.status()["self_record"]
        self.assertEqual(state["root"], str(self.initial / "self"))
        self.assertEqual(state["effective_root"], str(applications.self_record()))
        self.assertEqual(state["analysis"], str(applications.universe_dir()))

    def test_switch_preserves_current_ids_and_refuses_id_reassignment(self):
        old = self.seed("old")
        self.config({"crm_root": str(old)})
        other = self.seed("other", "p_other")
        self.assertFalse(connections.preview({"kind": "crm", "path": str(other)})["valid"])
        collision = self.seed("collision", email="different@example.com")
        self.assertFalse(connections.preview({"kind": "crm", "path": str(collision)})["valid"])
        compatible = self.seed("compatible")
        self.apply({"kind": "crm", "path": str(compatible)})
        self.assertEqual(crm.search_people()[0]["id"], "p_example")

    def test_offline_old_crm_cannot_strand_saved_person_references(self):
        contactcard.STORE.write_text('{"cards":{"p_previous":{"fields":{"company":"Example"}}}}', encoding="utf-8")
        incoming = self.seed()
        request = {"kind": "crm", "path": str(incoming)}
        self.assertFalse(connections.preview(request)["valid"])
        compatible = self.seed("restored", "p_previous")
        self.apply({"kind": "crm", "path": str(compatible)})
        self.assertEqual(contactcard.raw("p_previous")["fields"]["company"], "Example")

    def test_running_sessions_detached_jobs_and_builds_refuse_switch(self):
        root = self.seed()
        request = {"kind": "crm", "path": str(root)}
        plan = connections.preview(request)
        before = self.cfg.read_bytes()
        with mock.patch.object(session.sessions, "recent", return_value=[{"status": "running"}]), self.assertRaisesRegex(ValueError, "sessions"):
            connections.connect(request, plan["revision"])
        onboard._build["running"] = True
        with self.assertRaisesRegex(ValueError, "profile"):
            connections.connect(request, plan["revision"])
        onboard._build["running"] = False
        joblog.STORE.write_text(json.dumps({"jobs": [{"status": "running", "instance_id": instance.id()}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "detached"):
            connections.connect(request, plan["revision"])
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_busy_contact_maintenance_refuses_without_waiting(self):
        root = self.seed()
        request = {"kind": "crm", "path": str(root)}
        plan = connections.preview(request)
        contactintel._tick_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "maintenance"):
                connections.connect(request, plan["revision"])
        finally:
            contactintel._tick_lock.release()

    def test_bulk_rescore_and_network_refresh_refuse_switch(self):
        from server import atlas, jobrescore
        root = self.seed()
        request = {"kind": "self", "path": str(root)}
        plan = connections.preview(request)
        with mock.patch.object(jobrescore, "_bulk", {"running": True, "current": [], "errors": [], "moved": []}), self.assertRaisesRegex(ValueError, "rescore"):
            connections.connect(request, plan["revision"])
        atlas._refresh_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "Network refresh"):
                connections.connect(request, plan["revision"])
        finally:
            atlas._refresh_lock.release()

    def test_reader_connect_scan_and_disconnect_never_change_external_documents(self):
        docs = self.root / "documents"
        docs.mkdir()
        html = docs / "example.html"
        html.write_text("<title>Example document</title><p>Evidence</p>", encoding="utf-8")
        self.apply({"kind": "reader", "path": str(docs), "label": "Examples"})
        self.assertEqual(readinglist._connected_documents()[0]["title"], "Example document")
        self.assertIsNotNone(readinglist._connected_file(str(html)))
        self.assertFalse(connections.preview({"kind": "reader", "path": str(docs)})["valid"])
        content = html.read_bytes()
        self.apply({"kind": "reader", "path": str(docs), "mode": "disconnect"})
        self.assertEqual(self.stored()["reader_sources"], [])
        self.assertIsNone(readinglist._connected_file(str(html)))
        self.assertEqual(html.read_bytes(), content)

    def test_reader_rejects_unsafe_patterns_and_can_disconnect_unavailable_folder(self):
        docs = self.root / "documents"
        docs.mkdir()
        self.assertFalse(connections.preview({"kind": "reader", "path": str(docs), "glob": "../*.html"})["valid"])
        self.apply({"kind": "reader", "path": str(docs)})
        docs.rmdir()
        self.assertFalse(connections.status()["reader_sources"][0]["available"])
        self.apply({"kind": "reader", "path": str(docs), "mode": "disconnect"})

    def test_root_changes_reload_cached_people_and_index_stamp(self):
        first = self.seed("first")
        second = self.seed("second", "p_second", "drew@example.com")
        self.config({"crm_root": str(first)})
        self.assertEqual(crm.search_people()[0]["id"], "p_example")
        first_stamp = crmindex._stamp()
        self.config({"crm_root": str(second)})
        self.assertEqual(crm.search_people()[0]["id"], "p_second")
        self.assertNotEqual(first_stamp, crmindex._stamp())

    def test_model_refresh_cannot_write_to_replaced_crm(self):
        first = self.seed("first")
        second = self.seed("second")
        self.config({"crm_root": str(second)})
        profile = second / "profiles" / "p_example.json"
        before = profile.read_bytes()
        with self.assertRaisesRegex(ValueError, "CRM changed"):
            crm.save_profile_refresh("p_example", "New summary", expected_root=first)
        self.assertEqual(profile.read_bytes(), before)

    def test_import_preserves_master_and_backs_up_registry_and_exposes_import_facts(self):
        root = self.seed()
        self.config({"crm_root": str(root)})
        master = (root / "master.json").read_bytes()
        people = (root / "people.json").read_bytes()
        onboard.import_google_csv("Name,E-mail 1 - Value,Organization 1 - Name\nDrew Sample,drew@example.com,Example Rockets\n")
        self.assertEqual((root / "master.json").read_bytes(), master)
        self.assertEqual(next((root / "backups").glob("people-*.json")).read_bytes(), people)
        person = next(p for p in crm.search_people() if p["name"] == "Drew Sample")
        self.assertEqual(crm.get_person(person["id"])["master"]["company"], "Example Rockets")
        (root / "people.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            onboard.import_google_csv("Name,E-mail 1 - Value\nCasey Example,casey@example.com\n")
        self.assertEqual((root / "people.json").read_text(encoding="utf-8"), "{")

    def test_routes_and_native_tools_share_validation_and_write_classification(self):
        from fastapi.testclient import TestClient
        from server import main
        client = TestClient(main.app)  # no lifespan; never starts background workers
        root = self.seed()
        request = {"kind": "crm", "path": str(root)}
        response = client.post("/api/data/connections/preview", json={"request": request})
        self.assertEqual(response.status_code, 200)
        plan = response.json()
        response = client.post("/api/data/connections", json={"request": request, "revision": plan["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(client.get("/api/data/connections").json()["crm"]["root"], str(root))
        reply = asyncio.run(viratools.invoke("data_connections", {"request": {"kind": "crm", "path": "relative"}}))
        self.assertFalse(json.loads(reply["content"][0]["text"])["valid"])
        self.assertIn("mcp__vira__connect_data", viratools.WRITE_TOOLS)
        names = {t["name"] for t in viratools.function_tool_specs(read_only=True)}
        self.assertNotIn("connect_data", names)
        self.assertIn("data_connections", names)

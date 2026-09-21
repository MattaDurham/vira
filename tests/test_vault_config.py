"""Editable vault policy, legacy migration, and destination configuration."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import onboard, settings, vault


class VaultPolicyConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.primary = self.root / "research"
        self.extra = self.root / "journal"
        self.primary.mkdir()
        self.extra.mkdir()
        self.cfg = self.root / "config.json"
        self.write_config({"vault_root": str(self.primary), "vault_dirs": ["wiki"]})
        patch = mock.patch.object(settings, "CONFIG_PATH", self.cfg)
        patch.start()
        self.addCleanup(patch.stop)

    def write_config(self, value):
        self.cfg.write_text(json.dumps(value), encoding="utf-8")

    def config(self):
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def case_alias(self, path):
        alias = path.with_name(path.name.upper())
        if not alias.exists() or not alias.samefile(path):
            self.skipTest("fixture filesystem is case-sensitive")
        return alias

    def test_case_alias_of_primary_or_child_cannot_be_a_second_source(self):
        alias = self.case_alias(self.primary)
        (self.primary / "notes").mkdir()
        before = self.cfg.read_bytes()
        for path in (alias, alias / "notes"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "overlap"):
                onboard.vault_source_set(str(path), "Duplicate",
                                         write_enabled=True, write_dirs=["inbox"])
            self.assertEqual(self.cfg.read_bytes(), before)

    def test_preexisting_case_aliases_cannot_override_source_policy(self):
        alias = self.case_alias(self.extra)
        self.write_config({"vault_root": str(self.primary), "vault_sources": [
            {"id": "journal", "root": str(self.extra),
             "write_enabled": False, "model_exposure": False},
            {"id": "alias", "root": str(alias),
             "write_enabled": True, "model_exposure": True},
        ]})
        specs = vault.source_specs()
        self.assertEqual([s["id"] for s in specs], ["primary", "journal"])
        self.assertFalse(specs[1]["write_enabled"])
        self.assertFalse(specs[1]["model_exposure"])

    def test_connect_only_never_upserts_or_expands_existing_permissions(self):
        onboard.vault_source_set(str(self.extra), "Journal", write_enabled=False,
                                 read_enabled=False, model_exposure=False,
                                 write_dirs=["inbox"], protected_dirs=["reference"])
        (self.extra / "notes").mkdir()
        before = self.cfg.read_bytes()
        for path in (self.primary, self.extra, self.extra / "notes", self.root):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "already connected"):
                onboard.vault_source_set(
                    str(path), "Duplicate", connect_only=True, write_scope="all",
                    write_enabled=True, read_enabled=True, model_exposure=True)
            self.assertEqual(self.cfg.read_bytes(), before)

    def test_connect_only_refuses_legacy_sources_without_persisting_migration(self):
        self.write_config({"vault_root": str(self.primary),
                           "vault_dirs": ["wiki", str(self.extra)]})
        before = self.cfg.read_bytes()
        with self.assertRaisesRegex(ValueError, "already connected"):
            onboard.vault_source_set(str(self.extra), "Duplicate", connect_only=True,
                                     write_enabled=True, write_scope="all")
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_connect_only_refuses_case_alias_without_widening_permissions(self):
        alias = self.case_alias(self.extra)
        onboard.vault_source_set(str(self.extra), "Journal", model_exposure=False)
        before = self.cfg.read_bytes()
        with self.assertRaisesRegex(ValueError, "already connected"):
            onboard.vault_source_set(str(alias), "Duplicate", connect_only=True,
                                     write_enabled=True, write_scope="all")
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_case_alias_read_scopes_keep_their_confined_relative_paths(self):
        primary_alias = self.case_alias(self.primary)
        extra_alias = self.case_alias(self.extra)
        (self.primary / "wiki").mkdir()
        (self.extra / "reference").mkdir()
        self.write_config({"vault_root": str(self.primary),
                           "vault_primary": {"write_enabled": False},
                           "vault_dirs": [str(primary_alias / "wiki")],
                           "vault_sources": [{"id": "journal", "root": str(self.extra),
                                              "dirs": [str(extra_alias / "reference")]}]})
        specs = vault.source_specs()
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0]["dirs"], ["wiki"])
        self.assertEqual(specs[1]["dirs"], ["reference"])
        self.assertEqual(vault._relative_inside(primary_alias / "future" / "notes",
                                                self.primary), Path("future/notes"))

    def test_policy_survives_rename_and_reopens_independently(self):
        row = onboard.vault_source_set(str(self.extra), "Journal")
        onboard.vault_source_set(
            str(self.extra), "Life notes", row["id"], read_enabled=False,
            model_exposure=False, write_enabled=True, purpose="Personal ideas",
            contexts=["SELF", "family"], capture_dir="inbox/notes",
            write_dirs=["inbox", "wiki"], protected_dirs=["wiki/canon"],
            model_exclude_dirs=["confidential", "raw/private"],
            default_destination=True)
        onboard.vault_source_set(str(self.extra), "My journal", row["id"])
        spec = next(s for s in vault.source_specs() if s["id"] == row["id"])
        self.assertEqual(spec["name"], "My journal")
        self.assertFalse(spec["read_enabled"])
        self.assertFalse(spec["model_exposure"])
        self.assertTrue(spec["write_enabled"])
        self.assertEqual(spec["write_scope"], "selected")
        self.assertEqual(spec["write_dirs"], ["inbox", "wiki"])
        self.assertEqual(spec["capture_dir"], "inbox/notes")
        self.assertEqual(spec["contexts"], ["self", "family"])
        self.assertEqual(spec["protected_dirs"], ["wiki/canon"])
        self.assertEqual(spec["model_exclude_dirs"], ["confidential", "raw/private"])
        self.assertEqual(self.config()["vault_default_destination"], row["id"])

    def test_whole_vault_access_needs_no_writable_folder_list(self):
        row = onboard.vault_source_set(
            str(self.extra), "Journal", write_enabled=True, write_scope="all",
            protected_dirs=["reference"], capture_dir="ideas")
        self.assertEqual(row["write_scope"], "all")
        onboard.vault_source_set(str(self.extra), "Renamed", row["id"])
        spec = next(s for s in vault.source_specs() if s["id"] == row["id"])
        self.assertEqual(spec["write_scope"], "all")
        self.assertEqual(spec["write_dirs"], [])
        self.assertEqual(spec["protected_dirs"], ["reference"])

    def test_first_folder_connection_sets_primary_and_indexes_the_selected_folder(self):
        self.write_config({})
        row = onboard.vault_source_set(str(self.extra), "Journal",
                                       write_enabled=True, write_scope="all", connect_only=True)
        self.assertEqual(row["id"], "primary")
        self.assertTrue(row["primary"])
        self.assertEqual(Path(self.config()["vault_root"]), self.extra.resolve())
        self.assertEqual(self.config()["vault_sources"], [])
        self.assertIn(".", vault.source_specs()[0]["dirs"])
        next_row = onboard.vault_source_set(str(self.primary), "Research",
                                            write_enabled=True, write_scope="all", connect_only=True)
        self.assertNotEqual(next_row["id"], "primary")
        self.assertEqual(len(vault.source_specs()), 2)

    def test_missing_primary_does_not_reassign_an_existing_extra_vault(self):
        self.write_config({"vault_sources": [{"id": "journal", "root": str(self.extra)}]})
        row = onboard.vault_source_set(str(self.primary), "Research",
                                       write_enabled=True, write_scope="all")
        self.assertFalse(row["primary"])
        self.assertFalse(self.config().get("vault_root"))
        self.assertEqual({s["id"] for s in vault.source_specs()},
                         {"primary", "journal", row["id"]})

    def test_legacy_primary_keeps_write_and_read_scopes_when_renamed(self):
        row = onboard.vault_source_set(str(self.primary), "Renamed", "primary")
        self.assertEqual(row["write_scope"], "selected")
        self.assertEqual(vault.source_specs()[0]["write_dirs"],
                         ["inbox", "plans", "wiki", "raw"])
        self.assertEqual(self.config()["vault_dirs"], ["wiki"])

    def test_enabling_all_does_not_change_existing_selected_folders_or_read_scope(self):
        row = onboard.vault_source_set(str(self.extra), "Journal", write_enabled=True,
                                       write_dirs=["inbox"], dirs=["reference"])
        onboard.vault_source_set(str(self.extra), "Journal", row["id"], write_scope="all")
        saved = self.config()["vault_sources"][0]
        self.assertEqual(saved["write_dirs"], ["inbox"])
        self.assertEqual(saved["dirs"], ["reference"])
        onboard.vault_source_set(str(self.extra), "Journal", row["id"], write_scope="selected")
        self.assertEqual(self.config()["vault_sources"][0]["write_dirs"], ["inbox"])

    def test_primary_can_be_renamed_and_disabled_without_switching_roots(self):
        onboard.vault_source_set(str(self.primary), "Reference", "primary",
                                 read_enabled=True, write_enabled=False,
                                 model_exposure=False)
        spec = vault.source_specs()[0]
        self.assertEqual(spec["name"], "Reference")
        self.assertFalse(spec["write_enabled"])
        self.assertFalse(spec["model_exposure"])
        self.assertEqual(Path(self.config()["vault_root"]), self.primary.resolve())

    def test_legacy_save_preserves_id_citation_and_read_only_until_enabled(self):
        note = self.extra / "same.md"
        note.write_text("# Unchanged\n", encoding="utf-8")
        self.write_config({"vault_root": str(self.primary),
                           "vault_dirs": ["wiki", str(self.extra)]})
        before = vault.source_specs()[1]
        onboard.vault_source_set(str(self.extra), "Renamed", before["id"])
        specs = vault.source_specs()
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[1]["id"], before["id"])
        self.assertFalse(specs[1]["write_enabled"])
        self.assertEqual(specs[1]["write_scope"], "selected")
        self.assertNotIn(str(self.extra), self.config()["vault_dirs"])
        self.assertEqual(vault.note_text("@" + before["id"] + "/same.md"), "# Unchanged\n")
        self.assertEqual(note.read_text(encoding="utf-8"), "# Unchanged\n")

    def test_legacy_disconnect_does_not_resurrect_or_delete_files(self):
        note = self.extra / "keep.md"
        note.write_text("# Keep\n", encoding="utf-8")
        self.write_config({"vault_root": str(self.primary),
                           "vault_dirs": ["wiki", str(self.extra)]})
        sid = vault.source_specs()[1]["id"]
        onboard.vault_source_remove(sid)
        self.assertEqual(len(vault.source_specs()), 1)
        self.assertTrue(note.exists())
        self.assertEqual(self.config()["vault_dirs"], ["wiki"])

    def test_duplicate_legacy_alias_is_removed_on_disconnect(self):
        self.write_config({"vault_root": str(self.primary),
                           "vault_dirs": ["wiki", str(self.extra)],
                           "vault_sources": [{"id": "notes", "root": str(self.extra),
                                              "dirs": ["inbox"]}]})
        onboard.vault_source_remove("notes")
        self.assertEqual(len(vault.source_specs()), 1)

    def test_disconnect_primary_keeps_legacy_sources_and_files(self):
        self.write_config({"vault_root": str(self.primary),
                           "vault_dirs": ["wiki", str(self.extra)],
                           "vault_default_destination": "primary"})
        sid = vault.source_specs()[1]["id"]
        onboard.vault_source_remove("primary")
        remaining = [s for s in vault.source_specs() if not s["primary"]]
        self.assertEqual([s["id"] for s in remaining], [sid])
        self.assertEqual(self.config()["vault_root"], "")
        self.assertEqual(self.config()["vault_default_destination"], "primary")
        self.assertTrue(self.primary.is_dir())

    def test_rejects_invalid_policy_without_partial_config_write(self):
        row = onboard.vault_source_set(str(self.extra), "Journal")
        before = self.cfg.read_bytes()
        cases = [
            {"write_enabled": True},
            {"write_enabled": True, "write_dirs": ["wiki"], "capture_dir": "inbox"},
            {"capture_dir": "../escape"},
            {"capture_dir": " inbox"},
            {"capture_dir": "inbox "},
            {"protected_dirs": ["notes/trailing "]},
            {"protected_dirs": ["NUL"]},
            {"protected_dirs": ["notes:today"]},
            {"write_dirs": ["/outside"]},
            {"write_dirs": ["C:\\outside"]},
            {"model_exclude_dirs": ["../private"]},
            {"write_enabled": True, "write_dirs": ["inbox"], "protected_dirs": ["inbox"]},
            {"write_scope": "unknown"},
            {"write_enabled": True, "write_scope": "all", "protected_dirs": ["INBOX"]},
            {"write_enabled": True, "write_scope": "all", "capture_dir": "cafe\u0301",
             "protected_dirs": ["caf\u00e9"]},
            {"default_destination": True},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                onboard.vault_source_set(str(self.extra), "Journal", row["id"], **changes)
            self.assertEqual(self.cfg.read_bytes(), before)

    def test_default_stays_fail_closed_when_source_disabled_or_disconnected(self):
        row = onboard.vault_source_set(str(self.extra), "Journal", write_enabled=True,
                                       write_dirs=["inbox"], default_destination=True)
        onboard.vault_source_set(str(self.extra), "Journal", row["id"], write_enabled=False)
        self.assertEqual(self.config()["vault_default_destination"], row["id"])
        onboard.vault_source_set(str(self.extra), "Journal", row["id"], write_enabled=True,
                                 default_destination=True)
        onboard.vault_source_remove(row["id"])
        self.assertEqual(self.config()["vault_default_destination"], row["id"])
        onboard.vault_default_set("")
        self.assertEqual(self.config()["vault_default_destination"], "")

    def test_unknown_id_cannot_reconnect_disconnected_source(self):
        with self.assertRaisesRegex(ValueError, "unknown vault source"):
            onboard.vault_source_set(str(self.extra), "Unknown", "removed")

    def test_existing_index_scopes_survive_policy_and_name_changes(self):
        self.write_config({"vault_root": str(self.primary),
                           "vault_sources": [{"id": "notes", "root": str(self.extra),
                                              "dirs": ["reference"]}]})
        onboard.vault_source_set(str(self.extra), "Reference archive", "notes",
                                 model_exclude_dirs=["reference/confidential"])
        self.assertEqual(self.config()["vault_sources"][0]["dirs"], ["reference"])

    def test_default_picker_rejects_unavailable_source_without_changing_selection(self):
        onboard.vault_default_set("primary")
        with self.assertRaisesRegex(ValueError, "connected and writable"):
            onboard.vault_default_set("missing")
        self.assertEqual(self.config()["vault_default_destination"], "primary")

    def test_missing_source_can_be_disabled_but_not_chosen_as_default(self):
        row = onboard.vault_source_set(str(self.extra), "Journal")
        self.extra.rmdir()
        onboard.vault_source_set(str(self.extra), "Journal", row["id"], write_enabled=False)
        with self.assertRaisesRegex(ValueError, "connected and writable"):
            onboard.vault_source_set(str(self.extra), "Journal", row["id"],
                                     write_enabled=True, write_dirs=["inbox"],
                                     default_destination=True)


class VaultPolicyRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    @mock.patch("server.main.onboard.status", return_value={"vault": {
        "policy_version": 2, "sources": [], "default_destination": ""}})
    def test_sources_advertise_policy_editor_support_even_without_connections(self, status):
        response = self.client.get("/api/vault/sources")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"policy_version": 2,
                                          "sources": [], "default_destination": ""})

    @mock.patch("server.main.onboard.vault_source_set", return_value={"id": "notes"})
    def test_route_carries_independent_policy_fields(self, save):
        changes = {"read_enabled": True, "write_enabled": True,
                   "model_exposure": True, "model_exclude_dirs": ["confidential"],
                   "capture_dir": "inbox/notes", "write_dirs": ["inbox"],
                   "protected_dirs": ["canon"], "contexts": ["self"],
                   "purpose": "Ideas and preferences", "default_destination": False}
        response = self.client.post("/api/vault/sources", json={
            "path": "/fixture/notes", "name": "Notes", "id": "notes", **changes})
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with("/fixture/notes", "Notes", "notes", **changes)

    def test_scope_round_trip_through_routes_preserves_legacy_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            primary, extra = base / "research", base / "journal"
            primary.mkdir()
            extra.mkdir()
            cfg = base / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            with (mock.patch.object(settings, "CONFIG_PATH", cfg),
                  mock.patch.object(onboard, "crm_target", return_value=base / "crm"),
                  mock.patch.object(onboard.sources, "chatdb_state", return_value="unavailable"),
                  mock.patch.object(onboard.sources, "addressbook_dbs", return_value=[]),
                  mock.patch.object(onboard.sources, "discover", return_value={}),
                  mock.patch("server.mail.summary", return_value={"accounts": 0})):
                added = self.client.post("/api/vault/sources", json={
                    "path": str(primary), "name": "Research", "write_enabled": True,
                    "write_scope": "all", "write_dirs": [], "protected_dirs": ["reference"],
                    "connect_only": True})
                self.assertEqual(added.status_code, 200, added.text)
                self.assertEqual(added.json()["write_scope"], "all")
                self.assertEqual(added.json()["id"], "primary")
                legacy = onboard.vault_source_set(str(extra), "Journal", write_enabled=True,
                                                  write_dirs=["inbox"])
                renamed = self.client.post("/api/vault/sources", json={
                    "path": str(extra), "id": legacy["id"], "name": "Renamed"})
                self.assertEqual(renamed.status_code, 200, renamed.text)
                self.assertEqual(renamed.json()["write_scope"], "selected")
                state = self.client.get("/api/vault/sources").json()
                self.assertEqual(state["policy_version"], 2)
                by_id = {row["id"]: row for row in state["sources"]}
                self.assertEqual(by_id["primary"]["write_scope"], "all")
                self.assertEqual(by_id["primary"]["protected_dirs"], ["reference"])
                self.assertEqual(by_id[legacy["id"]]["write_scope"], "selected")
                self.assertEqual(by_id[legacy["id"]]["write_dirs"], ["inbox"])
                before = cfg.read_bytes()
                duplicate = self.client.post("/api/vault/sources", json={
                    "path": str(extra), "connect_only": True, "write_scope": "all",
                    "write_enabled": True, "model_exposure": True})
                self.assertEqual(duplicate.status_code, 400, duplicate.text)
                self.assertIn("already connected", duplicate.json()["detail"])
                self.assertEqual(cfg.read_bytes(), before)
                refused = self.client.post("/api/vault/sources", json={
                    "path": str(primary), "id": "primary", "write_scope": "invalid"})
                self.assertEqual(refused.status_code, 400)
                self.assertEqual(cfg.read_bytes(), before)

    @mock.patch("server.main.onboard.vault_default_set",
                return_value={"default_destination": ""})
    def test_owner_can_explicitly_clear_unavailable_default(self, save):
        response = self.client.post("/api/vault/default-destination", json={"source_id": ""})
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with("")


if __name__ == "__main__":
    unittest.main()

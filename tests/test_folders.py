"""Folder browsing and direct creation operate only on temporary folders."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import folders


class Folders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "Vault"
        self.vault.mkdir()
        self.environ = patch.dict(os.environ, {"VIRA_PASSIVE": "", "VIRA_SANDBOX": ""})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def browse(self, path=None, **kwargs):
        return folders.browse(str(path or self.vault), str(self.vault), **kwargs)

    def test_lists_only_folders_with_unicode_and_commas_intact(self):
        for name in ["Zulu", "alpha", "Notes, ideas", "Café"]:
            (self.vault / name).mkdir()
        (self.vault / "not a folder.md").write_text("private content", encoding="utf-8")
        rows = self.browse()["folders"]
        self.assertEqual([row["name"] for row in rows], ["alpha", "Café", "Notes, ideas", "Zulu"])
        self.assertEqual(rows[2]["relative"], "Notes, ideas")

    def test_root_defaults_to_selected_vault_and_cannot_navigate_up(self):
        result = folders.browse(root=str(self.vault))
        self.assertEqual(result["path"], str(self.vault))
        self.assertEqual(result["relative"], ".")
        self.assertIsNone(result["parent"])
        self.assertEqual(result["ancestors"], result["places"])
        self.assertEqual(len(result["ancestors"]), 1)

    def test_nested_breadcrumbs_and_relative_paths(self):
        child = self.vault / "Inbox" / "Notes"
        child.mkdir(parents=True)
        result = self.browse(child)
        self.assertEqual(result["parent"], str(child.parent))
        self.assertEqual(result["relative"], "Inbox/Notes")
        self.assertEqual([row["relative"] for row in result["ancestors"]], [".", "Inbox", "Inbox/Notes"])

    def test_default_location_is_home_and_places_are_clickable(self):
        (self.root / "Documents").mkdir()
        with patch.object(Path, "home", return_value=self.root):
            result = folders.browse()
        self.assertEqual(result["path"], str(self.root))
        self.assertIsNone(result["relative"])
        places = {row["name"]: row["path"] for row in result["places"]}
        self.assertEqual(places["Home"], str(self.root))
        self.assertEqual(places["Documents"], str(self.root / "Documents"))

    def test_hidden_folders_are_opt_in(self):
        (self.vault / ".settings").mkdir()
        self.assertEqual(self.browse()["folders"], [])
        self.assertEqual(self.browse(show_hidden=True)["folders"][0]["name"], ".settings")

    def test_scope_refuses_parent_and_sibling_prefix_escape(self):
        sibling = self.root / "Vault-other"
        sibling.mkdir()
        for path in [self.root, sibling, self.vault / ".."]:
            with self.subTest(path=path), self.assertRaises(folders.FolderError) as caught:
                self.browse(path)
            self.assertEqual(caught.exception.status_code, 403)

    def test_symlinks_outside_scope_are_hidden_and_refused(self):
        inside = self.vault / "Real"
        inside.mkdir()
        try:
            (self.vault / "Outside").symlink_to(self.root, target_is_directory=True)
            (self.vault / "Inside").symlink_to(inside, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        self.assertEqual([row["name"] for row in self.browse()["folders"]], ["Inside", "Real"])
        self.assertEqual(self.browse(self.vault / "Inside")["path"], str(inside))
        with self.assertRaises(folders.FolderError):
            self.browse(self.vault / "Outside")

    def test_missing_file_relative_and_null_paths_report_useful_errors(self):
        file = self.vault / "note.md"
        file.write_text("note", encoding="utf-8")
        for value in [str(self.vault / "Missing"), str(file), "relative/path", "\x00"]:
            with self.subTest(value=value), self.assertRaises(folders.FolderError):
                folders.browse(value)

    def test_permission_error_is_returned_without_hiding_siblings(self):
        with patch.object(folders.os, "scandir", side_effect=PermissionError):
            with self.assertRaises(folders.FolderError) as caught:
                self.browse()
        self.assertEqual(caught.exception.status_code, 403)

    def test_create_returns_selected_new_folder(self):
        result = folders.create(str(self.vault), "Café, notes", str(self.vault))
        self.assertTrue((self.vault / "Café, notes").is_dir())
        self.assertEqual(result["relative"], "Café, notes")
        self.assertEqual(result["folders"], [])
        self.assertTrue(result["can_create"])

    def test_create_rejects_traversal_control_characters_and_overwrite(self):
        for name in ["", " ", ".", "..", "../escape", "child/nested", "child\\nested", "null\x00", "line\nfeed", " padded "]:
            with self.subTest(name=name), self.assertRaises(folders.FolderError):
                folders.create(str(self.vault), name, str(self.vault))
        (self.vault / "Existing").mkdir()
        with self.assertRaises(folders.FolderError) as caught:
            folders.create(str(self.vault), "Existing", str(self.vault))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse((self.root / "escape").exists())

    def test_create_refuses_parent_outside_root(self):
        with self.assertRaises(folders.FolderError) as caught:
            folders.create(str(self.root), "Outside", str(self.vault))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertFalse((self.root / "Outside").exists())

    def test_windows_names_cannot_change_drives_or_become_reserved_devices(self):
        with patch.object(folders.settings, "IS_WIN", True):
            for name in ["C:", "A:notes", "CON", "nul.txt", "COM1", "LPT9.md", "trailing.", "question?"]:
                with self.subTest(name=name), self.assertRaises(folders.FolderError):
                    folders.create(str(self.vault), name, str(self.vault))
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_scoped_existing_unsupported_names_are_navigable_but_not_selectable(self):
        names = [" Leading", "Trailing ", "Notes: today", "CON", "LPT1.md", "Legal\\draft"]
        for name in names:
            with self.subTest(name=name):
                child = self.vault / name
                try:
                    child.mkdir()
                except OSError:
                    # Some of these names cannot exist on Windows at all.
                    continue
                if not child.is_dir() or not any(p.name == name for p in self.vault.iterdir()):
                    continue
                selected = self.browse(child)
                self.assertFalse(selected["selectable"])
                self.assertTrue(selected["selection_disabled_reason"])
                row = next(row for row in self.browse()["folders"] if row["name"] == name)
                self.assertFalse(row["selectable"])
                self.assertEqual(selected["relative"], name)
                (child / "Child").mkdir()
                self.assertEqual(len(self.browse(child)["folders"]), 1)

    def test_scoped_creation_refuses_unrepresentable_names_before_mkdir(self):
        for name in [" Leading", "Trailing ", "Notes: today", "CON", "LPT1.md", "Ends.", "@source"]:
            with self.subTest(name=name), self.assertRaises(folders.FolderError):
                folders.create(str(self.vault), name, str(self.vault))
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_unscoped_root_and_scoped_root_remain_selectable(self):
        self.assertTrue(self.browse()["selectable"])
        if not folders.settings.IS_WIN:
            special = self.vault / "Notes: today"
            special.mkdir()
            self.assertTrue(folders.browse(str(special))["selectable"])
            self.assertTrue(folders.browse(str(special), str(special))["selectable"])

    def test_creation_validates_entire_scoped_destination(self):
        if folders.settings.IS_WIN:
            self.skipTest("Colon folder names cannot exist on Windows")
        unsupported = self.vault / "Notes: today"
        unsupported.mkdir()
        with self.assertRaises(folders.FolderError):
            folders.create(str(unsupported), "Ordinary name", str(self.vault))
        self.assertFalse((unsupported / "Ordinary name").exists())

    def test_passive_and_sandbox_browse_but_do_not_create(self):
        for flag in ["VIRA_PASSIVE", "VIRA_SANDBOX"]:
            with self.subTest(flag=flag), patch.dict(os.environ, {flag: "1"}):
                result = self.browse()
                self.assertFalse(result["can_create"])
                self.assertTrue(result["create_disabled_reason"])
                with self.assertRaises(folders.FolderError) as caught:
                    folders.create(str(self.vault), "Blocked", str(self.vault))
                self.assertEqual(caught.exception.status_code, 403)
                self.assertFalse((self.vault / "Blocked").exists())


class FolderRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        # No lifespan context: these HTTP tests never start app workers.
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "Vault"
        self.vault.mkdir()
        env = patch.dict(os.environ, {"VIRA_PASSIVE": "", "VIRA_SANDBOX": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_get_root_and_hidden_query_use_real_folder_listing(self):
        (self.vault / ".config").mkdir()
        response = self.client.get("/api/folders", params={"root": str(self.vault)})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["path"], str(self.vault))
        self.assertEqual(body["root"], str(self.vault))
        self.assertEqual(body["relative"], ".")
        self.assertIsNone(body["parent"])
        self.assertEqual(body["folders"], [])
        response = self.client.get("/api/folders", params={
            "path": str(self.vault), "root": str(self.vault), "show_hidden": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["folders"][0]["relative"], ".config")

    def test_get_reports_scope_and_missing_errors_as_detail(self):
        response = self.client.get("/api/folders", params={
            "path": str(self.root), "root": str(self.vault)})
        self.assertEqual(response.status_code, 403)
        self.assertIn("inside this vault", response.json()["detail"])
        response = self.client.get("/api/folders", params={"path": str(self.root / "Missing")})
        self.assertEqual(response.status_code, 404)
        self.assertIsInstance(response.json()["detail"], str)

    def test_post_json_creates_one_folder_and_returns_selectable_result(self):
        response = self.client.post("/api/folders", json={
            "parent": str(self.vault), "name": "Café, notes", "root": str(self.vault)})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["relative"], "Café, notes")
        self.assertEqual(Path(body["path"]), self.vault / "Café, notes")
        self.assertTrue(Path(body["path"]).is_dir())
        follow = self.client.get("/api/folders", params={"path": body["path"], "root": body["root"]})
        self.assertEqual(follow.status_code, 200)
        self.assertEqual(follow.json(), body)

    def test_post_never_overwrites_and_rejects_invalid_input(self):
        existing = self.vault / "Existing"
        existing.mkdir()
        sentinel = existing / "keep.md"
        sentinel.write_text("keep this", encoding="utf-8")
        for name, code in [("Existing", 409), ("../Outside", 400)]:
            response = self.client.post("/api/folders", json={
                "parent": str(self.vault), "name": name, "root": str(self.vault)})
            self.assertEqual(response.status_code, code, response.text)
            self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep this")
        self.assertFalse((self.root / "Outside").exists())
        response = self.client.post("/api/folders", json={"parent": str(self.vault)})
        self.assertEqual(response.status_code, 422)

    def test_passive_http_listing_remains_usable_and_creation_is_forbidden(self):
        with patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            response = self.client.get("/api/folders", params={"root": str(self.vault)})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["can_create"])
            reason = response.json()["create_disabled_reason"]
            response = self.client.post("/api/folders", json={
                "parent": str(self.vault), "name": "Blocked", "root": str(self.vault)})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["detail"], reason)
        self.assertFalse((self.vault / "Blocked").exists())

    def test_scoped_create_http_rejects_nonportable_names_without_writing(self):
        response = self.client.post("/api/folders", json={
            "parent": str(self.vault), "name": "Reserved: name", "root": str(self.vault)})
        self.assertEqual(response.status_code, 400)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(list(self.vault.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

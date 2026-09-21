"""Synthetic destinations exercise routing, actual writes, and tool gates."""
import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from server import settings, vault, vaultwrite, viratools


class VaultWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roots = {n: self.root / n for n in ("research", "profile", "family")}
        for root in self.roots.values():
            root.mkdir()
        self.config = {
            "vault_root": str(self.roots["research"]), "vault_dirs": ["wiki"],
            "vault_sources": [
                {"id": "profile", "name": "Profile", "root": str(self.roots["profile"]),
                 "write_enabled": True, "capture_dir": "inbox/notes",
                 "write_dirs": ["inbox/notes"], "protected_dirs": ["canon", "raw"],
                 "contexts": ["self"]},
                {"id": "family", "name": "Family", "root": str(self.roots["family"]),
                 "write_enabled": True, "capture_dir": "inbox",
                 "write_dirs": ["inbox"], "protected_dirs": ["raw", "confidential"],
                 "contexts": ["family"]},
            ],
        }
        self.cfg = self.root / "config.json"
        self.save_config()
        for patcher in (
                mock.patch.object(settings, "CONFIG_PATH", self.cfg),
                mock.patch.object(vault, "DB_PATH", self.root / "index.sqlite"),
                mock.patch.object(vaultwrite, "LOCK_ROOT", self.root / "locks"),
                mock.patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("VIRA_PASSIVE", None)
        self.addCleanup(lambda: vault._active.update(key=None, vault=None, rows=[]))

    def save_config(self):
        self.cfg.write_text(json.dumps(self.config), encoding="utf-8")

    def test_context_captures_are_separate_and_immediately_reopenable(self):
        own = vaultwrite.capture("A preference", "Prefer quiet rooms.", context="self")
        family = vaultwrite.capture("A weekend idea", "Visit a garden.", context="family")
        self.assertEqual(own["source_id"], "profile")
        self.assertTrue(own["relative_path"].startswith("inbox/notes/"))
        self.assertEqual(family["source_id"], "family")
        self.assertIn("quiet rooms", vault.note_text(own["path"]))
        self.assertEqual(vault.search("quiet rooms")[0]["path"], own["path"])
        self.assertFalse(list(self.roots["research"].rglob("*.md")))

    def test_explicit_destination_wins_and_default_does_not_override_context(self):
        self.config["vault_default_destination"] = "primary"
        self.save_config()
        self.assertEqual(vaultwrite.resolve_destination("family", "self")["id"], "family")
        self.assertEqual(vaultwrite.resolve_destination(context="self")["id"], "profile")
        self.assertEqual(vaultwrite.resolve_destination()["id"], "primary")

    def test_ambiguous_unknown_readonly_and_disconnected_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "choose"):
            vaultwrite.capture("Title", "Text")
        for dest in ("missing", "@missing/file.md"):
            with self.assertRaises(ValueError):
                vaultwrite.capture("Title", "Text", destination=dest)
        self.config["vault_sources"][0]["write_enabled"] = False
        self.config["vault_default_destination"] = "profile"
        self.save_config()
        with self.assertRaisesRegex(ValueError, "read-only"):
            vaultwrite.capture("Title", "Text")
        self.config["vault_sources"][0]["write_enabled"] = True
        self.save_config()
        self.roots["profile"].rmdir()
        with self.assertRaisesRegex(ValueError, "disconnected"):
            vaultwrite.capture("Title", "Text")
        self.assertFalse(list(self.roots["research"].rglob("*.md")))

    def test_hash_updates_preserve_source_and_refuse_stale_content(self):
        receipt = vaultwrite.capture("Same title", "Original", destination="profile")
        updated = vaultwrite.update(receipt["path"], "# Updated\nNew text\n", receipt["sha256"])
        self.assertEqual(updated["source_id"], "profile")
        with self.assertRaisesRegex(ValueError, "changed"):
            vaultwrite.update(receipt["path"], "stale", receipt["sha256"])
        with self.assertRaisesRegex(ValueError, "sha256"):
            vaultwrite.update(receipt["path"], "no hash", None)
        with self.assertRaisesRegex(ValueError, "disagree"):
            vaultwrite.update(receipt["path"], "misroute", updated["sha256"], "primary")
        self.assertIn("New text", vault.note_text(receipt["path"]))

    def test_confinement_scopes_protection_and_symlinks(self):
        spec = vaultwrite.resolve_destination("profile")
        for rel in ("../outside.md", "/outside.md", "inbox/notes/../../escape.md",
                    "canon/facts.md", "raw/source.md", "inbox/notes/../escape.md",
                    "C:\\escape.md", "inbox/notes/bad.txt"):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                vaultwrite.write_note(spec, rel, "text")
        (self.roots["profile"] / "inbox").mkdir()
        try:
            (self.roots["profile"] / "inbox/notes").symlink_to(self.roots["family"], target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            vaultwrite.write_note(spec, "inbox/notes/escape.md", "text")
        self.assertFalse((self.roots["family"] / "escape.md").exists())

    def test_whole_vault_creates_captures_updates_and_deletes_without_an_allowlist(self):
        source = self.config["vault_sources"][0]
        source.update(write_scope="all", write_dirs=[])
        self.save_config()
        spec = vaultwrite.resolve_destination("profile")
        root_note = vaultwrite.write_note(spec, "new-note.md", "# Starting here\n")
        nested = vaultwrite.write_note(spec, "projects/future/plan.md", "# Orchard plan\n")
        capture = vaultwrite.capture("An idea", "Plant an orchard.", "profile")
        self.assertIn("Plant an orchard", vault.note_text(capture["path"]))
        self.assertEqual(vault.search("Orchard plan")[0]["path"], nested["path"])
        changed = vaultwrite.update(root_note["path"], "# Updated\n", root_note["sha256"])
        self.assertEqual(vault.note_text(changed["path"]), "# Updated\n")
        vaultwrite.delete_text(spec, nested["relative_path"], nested["sha256"])
        self.assertFalse((self.roots["profile"] / nested["relative_path"]).exists())

    def test_whole_vault_still_protects_folders_and_refuses_invalid_or_linked_paths(self):
        source = self.config["vault_sources"][0]
        source.update(write_scope="all", write_dirs=[], protected_dirs=["canon", "caf\u00e9"])
        self.save_config()
        spec = vaultwrite.resolve_destination("profile")
        for rel in ("canon/facts.md", "CANON/facts.md", "cafe\u0301/notes.md",
                    "../outside.md", "/outside.md", "new/../../escape.md",
                    "C:\\escape.md", "new/CON.md", "new/name. /note.md"):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                vaultwrite.write_note(spec, rel, "blocked")
        for rel in ("canon/image.png", "CANON/image.png"):
            with self.subTest(rel=rel), self.assertRaisesRegex(ValueError, "protected"):
                vaultwrite.write_bytes(spec, rel, b"blocked")
        try:
            (self.roots["profile"] / "linked").symlink_to(self.roots["family"],
                                                           target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            vaultwrite.write_note(spec, "linked/escape.md", "blocked")
        self.assertFalse((self.roots["family"] / "escape.md").exists())

    def test_all_scope_requires_explicit_selection_and_is_rechecked_before_writing(self):
        source = self.config["vault_sources"][0]
        spec = vaultwrite.resolve_destination("profile")
        self.assertEqual(spec["write_scope"], "selected")
        with self.assertRaisesRegex(ValueError, "writable folders"):
            vaultwrite.write_note(spec, "projects/plan.md", "blocked")
        source.update(write_scope="all", write_dirs=[])
        self.save_config()
        spec = vaultwrite.resolve_destination("profile")
        source["write_scope"] = "selected"
        self.save_config()
        with self.assertRaisesRegex(ValueError, "writable folders"):
            vaultwrite.write_note(spec, "projects/plan.md", "blocked")
        source["write_scope"] = "all"
        source["write_enabled"] = False
        self.save_config()
        with self.assertRaisesRegex(ValueError, "read-only"):
            vaultwrite.write_note(spec, "projects/plan.md", "blocked")

    def test_policy_is_rechecked_at_write_and_read_disable_does_not_remap_primary(self):
        spec = vaultwrite.resolve_destination("profile")
        self.config["vault_sources"][0]["write_enabled"] = False
        self.save_config()
        with self.assertRaises(ValueError):
            vaultwrite.write_note(spec, "inbox/notes/race.md", "text")
        self.config["vault_primary"] = {"read_enabled": False}
        self.save_config()
        with self.assertRaisesRegex(ValueError, "primary"):
            vault.note_text("wiki/example.md")

    def test_parallel_creates_and_updates_do_not_clobber(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: vaultwrite.capture(
                "Same title", "Independent", "profile"), range(8)))
        self.assertEqual(len({r["path"] for r in receipts}), 8)
        receipt = receipts[0]

        def update(_):
            try:
                vaultwrite.update(receipt["path"], "Changed", receipt["sha256"])
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(update, range(4))), 1)

    def test_passive_and_provider_neutral_readonly_gates(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaisesRegex(ValueError, "passive"):
                vaultwrite.capture("Title", "Text", "profile")
        args = {"title": "Title", "text": "Text", "destination": "profile"}
        result = asyncio.run(viratools.invoke("vault_capture", args, read_only=True))
        self.assertIn("read-only", result["content"][0]["text"])
        for specs in (viratools.function_tool_specs(True),
                      viratools.dynamic_tool_specs(True)[0]["tools"]):
            self.assertNotIn("vault_capture", [t["name"] for t in specs])
            self.assertNotIn("vault_update", [t["name"] for t in specs])
        result = asyncio.run(viratools.invoke("vault_capture", {"title": "Job note", "text": "Routed"},
                                             vault_destination="profile"))
        self.assertEqual(json.loads(result["content"][0]["text"])["source_id"], "profile")

    def test_model_exposure_is_separate_from_local_reading(self):
        receipt = vaultwrite.capture("Private", "Private orchard account", "family")
        self.config["vault_sources"][1]["model_exposure"] = False
        self.save_config()
        self.assertIn("orchard", vault.note_text(receipt["path"]))
        self.assertEqual(len(vault.search("orchard")), 1)
        self.assertFalse(vault.search("orchard", for_model=True))
        with self.assertRaisesRegex(ValueError, "model access"):
            vault.note_text(receipt["path"], for_model=True)
        with vault.model_access():
            self.assertFalse(vault.grep_notes("orchard"))
            self.assertFalse(vault.search_filtered("orchard"))
        self.assertNotIn("family", [r["id"] for r in vaultwrite.destinations(for_model=True)])
        with mock.patch.object(vault._vault(), "ask", return_value={}) as answer:
            vault.ask("orchard?", hits=[{"path": receipt["path"], "text": "private"}])
            self.assertEqual(answer.call_args.kwargs["hits"], [])

    def test_hidden_model_folders_stay_locally_readable(self):
        secret = self.roots["family"] / "confidential" / "journal.md"
        secret.parent.mkdir()
        secret.write_text("# Journal\nA hidden orchard record\n", encoding="utf-8")
        allowed = vaultwrite.capture("Garden", "A visible orchard idea", "family")
        self.config["vault_sources"][1]["model_exclude_dirs"] = ["confidential"]
        self.save_config()
        vault.scan_once()
        self.assertIn("hidden", vault.note_text("@family/confidential/journal.md"))
        self.assertFalse(vault.model_path_allowed("@family/CONFIDENTIAL/journal.md"))
        self.assertEqual([h["path"] for h in vault.search("orchard", for_model=True)],
                         [allowed["path"]])
        with self.assertRaisesRegex(ValueError, "model access"):
            vault.note_text("@family/confidential/journal.md", for_model=True)
        with vault.model_access():
            self.assertFalse(vault.grep_notes("hidden orchard"))
            self.assertIsNone(vault.resolve_ref("journal"))
        alias = self.roots["family"] / "alias.md"
        try:
            alias.symlink_to(secret)
        except OSError:
            return
        self.assertFalse(vault.model_path_allowed("@family/alias.md"))

    def test_explicit_tool_context_and_path_override_inherited_job_route(self):
        captured = asyncio.run(viratools.invoke("vault_capture", {
            "title": "Context wins", "text": "Family note", "context": "family"},
            vault_destination="primary"))
        receipt = json.loads(captured["content"][0]["text"])
        self.assertEqual(receipt["source_id"], "family")
        updated = asyncio.run(viratools.invoke("vault_update", {
            "path": receipt["path"], "text": "# Updated family note\n",
            "expected_hash": receipt["sha256"]}, vault_destination="primary"))
        self.assertEqual(json.loads(updated["content"][0]["text"])["source_id"], "family")

    def test_truncated_native_note_cannot_be_replaced(self):
        receipt = vaultwrite.capture("Long note", "Text " * 100, "family")
        with mock.patch.object(viratools, "_text_cap", return_value=300):
            excerpt = viratools._vault_note_text(receipt["path"])
            self.assertNotIn("sha256:", excerpt)
            result = asyncio.run(viratools.invoke("vault_update", {
                "path": receipt["path"], "text": "Partial replacement",
                "expected_hash": receipt["sha256"]}))
        self.assertIn("complete-read budget", result["content"][0]["text"])
        self.assertIn("Text Text", vault.note_text(receipt["path"]))


class VaultMutationRouteTests(VaultWriteTests):
    def test_capture_update_reopen_and_passive(self):
        from fastapi.testclient import TestClient
        from server import main
        client = TestClient(main.app)
        created = client.post("/api/vault/capture", json={
            "title": "A thought", "text": "A synthetic note", "context": "self"})
        self.assertEqual(created.status_code, 200, created.text)
        result = created.json()
        reopened = client.get("/api/vault/note", params={"path": result["path"]}).json()
        self.assertEqual(reopened["sha256"], result["sha256"])
        edited = client.post("/api/vault/update", json={"path": result["path"],
                              "text": "# Revised\n", "expected_hash": result["sha256"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            refused = client.post("/api/vault/capture", json={
                "title": "No write", "text": "Text", "destination": "family"})
            self.assertEqual(refused.status_code, 400)


if __name__ == "__main__":
    unittest.main()

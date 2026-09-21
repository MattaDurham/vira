"""Filesystem races and alias serialization for confined synthetic writes."""
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from server import settings, vault, vaultwrite


class VaultWriteRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "vault"
        (self.root / "inbox").mkdir(parents=True)
        self.values = {"vault_root": str(self.root), "vault_primary": {
            "write_dirs": ["inbox"], "capture_dir": "inbox"}}
        for patch in (
            mock.patch.object(settings, "get", side_effect=self.values.get),
            mock.patch.object(vault, "_vault_rows", return_value=[]),
            mock.patch.object(vaultwrite, "LOCK_ROOT", self.base / "locks"),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.spec = vaultwrite.resolve_destination("primary")

    def move_parent(self):
        moved = self.base / "moved"
        (self.root / "inbox").rename(moved)
        (self.root / "inbox").symlink_to(moved, target_is_directory=True)
        return moved

    @unittest.skipUnless(vaultwrite._DIR_FD, "requires descriptor-relative filesystem calls")
    def test_create_rolls_back_when_parent_moves_during_commit(self):
        original = os.link
        def swap(source, target, **kwargs):
            self.move_parent()
            return original(source, target, **kwargs)
        with mock.patch.object(vaultwrite.os, "link", side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                vaultwrite.write_note(self.spec, "inbox/new.md", "new")
        self.assertEqual(list((self.base / "moved").iterdir()), [])

    @unittest.skipUnless(vaultwrite._DIR_FD, "requires descriptor-relative filesystem calls")
    def test_update_restores_original_when_parent_moves_during_replace(self):
        (self.root / "inbox/note.md").write_text("original", encoding="utf-8")
        original = os.replace
        swapped = False
        def swap(source, target, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                self.move_parent()
            return original(source, target, **kwargs)
        with mock.patch.object(vaultwrite.os, "replace", side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                vaultwrite.write_note(self.spec, "inbox/note.md", "new",
                                     expected_hash=vaultwrite.digest("original"), create_only=False)
        moved = self.base / "moved"
        self.assertEqual((moved / "note.md").read_text(encoding="utf-8"), "original")
        self.assertEqual([p.name for p in moved.iterdir()], ["note.md"])

    @unittest.skipUnless(vaultwrite._DIR_FD, "requires descriptor-relative filesystem calls")
    def test_delete_restores_original_when_parent_moves_during_unlink(self):
        (self.root / "inbox/note.md").write_text("original", encoding="utf-8")
        original = os.unlink
        swapped = False
        def swap(name, **kwargs):
            nonlocal swapped
            if name == "note.md" and not swapped:
                swapped = True
                self.move_parent()
            return original(name, **kwargs)
        with mock.patch.object(vaultwrite.os, "unlink", side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                vaultwrite.delete_text(self.spec, "inbox/note.md")
        moved = self.base / "moved"
        self.assertEqual((moved / "note.md").read_text(encoding="utf-8"), "original")
        self.assertEqual([p.name for p in moved.iterdir()], ["note.md"])

    def test_case_and_unicode_aliases_share_the_same_lock(self):
        self.assertEqual(vaultwrite._lock_path(self.root / "inbox/CAFÉ.md"),
                         vaultwrite._lock_path(self.root / "inbox/cafe\u0301.md"))

    def test_resolver_alias_keeps_confined_create_update_and_delete_working(self):
        if os.name == "nt":
            root = str(self.root)
            alias = Path("\\\\?\\UNC\\" + root[2:] if root.startswith("\\\\")
                         else "\\\\?\\" + root)
        else:
            alias = self.root.with_name(self.root.name.upper())
            if not alias.exists() or not alias.samefile(self.root):
                self.skipTest("requires a filesystem path alias")
        original = Path.resolve
        rel = "inbox/future/note.md"

        def resolve(path, *args, **kwargs):
            if path.name == "note.md":
                return alias / rel
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve):
            receipt = vaultwrite.write_note(self.spec, rel, "original")
            changed = vaultwrite.write_note(self.spec, rel, "updated",
                                           expected_hash=receipt["sha256"], create_only=False)
            self.assertEqual((self.root / rel).read_text(encoding="utf-8"),
                             "updated")
            vaultwrite.delete_text(self.spec, rel, changed["sha256"])
        self.assertFalse((self.root / rel).exists())

    def test_resolver_alias_to_another_directory_still_refuses_write(self):
        outside = self.base / "outside"
        outside.mkdir()
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path.name == "note.md":
                return outside / "note.md"
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(ValueError, "leaves the vault"):
                vaultwrite.write_note(self.spec, "inbox/note.md", "blocked")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / "inbox/note.md").exists())

    def test_parallel_case_alias_updates_accept_only_one_original_hash(self):
        target = self.root / "inbox/Note.md"
        target.write_text("original", encoding="utf-8")
        if not (self.root / "inbox/note.md").exists():
            self.skipTest("case-sensitive filesystem")
        original = vaultwrite._Parent.temp
        def slow_temp(parent, text, **kwargs):
            result = original(parent, text, **kwargs)
            time.sleep(0.025)
            return result
        def update(rel):
            try:
                vaultwrite.write_note(self.spec, rel, "updated " + rel,
                                     expected_hash=vaultwrite.digest("original"), create_only=False)
                return True
            except ValueError:
                return False
        with mock.patch.object(vaultwrite._Parent, "temp", slow_temp):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(update, ("inbox/Note.md", "inbox/note.md")))
        self.assertEqual(sum(results), 1)

    def test_portable_fallback_keeps_hash_and_symlink_gates(self):
        with mock.patch.object(vaultwrite, "_DIR_FD", False):
            created = vaultwrite.write_note(self.spec, "inbox/note.md", "original")
            changed = vaultwrite.write_note(self.spec, "inbox/note.md", "new",
                                           expected_hash=created["sha256"], create_only=False)
            with self.assertRaisesRegex(ValueError, "changed"):
                vaultwrite.write_note(self.spec, "inbox/note.md", "stale",
                                     expected_hash=created["sha256"], create_only=False)
            vaultwrite.delete_text(self.spec, "inbox/note.md")
            vaultwrite.delete_text(self.spec, "inbox/note.md", changed["sha256"])
            self.assertFalse((self.root / "inbox/note.md").exists())
            try:
                (self.root / "inbox/link").symlink_to(self.base, target_is_directory=True)
            except OSError:
                return  # Windows may not permit symlink creation.
            with self.assertRaisesRegex(ValueError, "symlink"):
                vaultwrite.write_note(self.spec, "inbox/link/outside.md", "blocked")

    def test_update_preserves_existing_mode_and_normalizes_newlines(self):
        target = self.root / "inbox/note.md"
        target.write_bytes(b"one\r\ntwo\r\n")
        target.chmod(0o640)
        before = target.stat().st_mode
        receipt = vaultwrite.write_note(self.spec, "inbox/note.md", "new\r\nline\r\n",
                                       expected_hash=vaultwrite.digest("one\ntwo\n"), create_only=False)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\nline\n")
        self.assertEqual(receipt["sha256"], vaultwrite.digest("new\nline\n"))
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode, before)


if __name__ == "__main__":
    unittest.main()

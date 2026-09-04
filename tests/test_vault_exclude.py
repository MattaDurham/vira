"""vault_exclude_dirs: take a tree, skip one branch of it.

The live case this exists for: TC-IL's `raw/` holds 1,007 loose full
transcripts that exist nowhere else, and `raw/instagram`, whose 5,401
clippings are already carried IN FULL by their wiki notes (measured: the
wiki note is 1.41x the size of the raw). qocha rglobs a listed dir, so
without a name-level exclusion the only way to reach the transcripts was
to take the clippings too.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import settings, vault


class VaultExcludeDirsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.primary = root / "vault"
        (self.primary / "raw").mkdir(parents=True)
        (self.primary / "raw" / "clips").mkdir()
        (self.primary / "raw" / ".venv" / "lib").mkdir(parents=True)
        (self.primary / "raw" / "talk.md").write_text(
            "# Talk\nA loose full transcript of the keynote.\n",
            encoding="utf-8")
        (self.primary / "raw" / "clips" / "one.md").write_text(
            "# Clip\nA clipping already summarised elsewhere.\n",
            encoding="utf-8")
        (self.primary / "raw" / ".venv" / "lib" / "LICENSE.md").write_text(
            "# Vendored\nA vendored package licence.\n", encoding="utf-8")

        self.excluded = ["clips"]
        original_get = settings.get

        def configured(key):
            if key == "vault_exclude_dirs":
                return self.excluded
            if key == "vault_sources":
                return []
            return original_get(key)

        for patcher in (
                mock.patch.object(vault, "DB_PATH", root / "primary.sqlite"),
                mock.patch.object(vault, "vault_root",
                                  return_value=self.primary),
                mock.patch.object(vault, "vault_dirs", return_value=["raw"]),
                mock.patch.object(settings, "get", side_effect=configured)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        vault._active.update(key=None, vault=None, rows=[])
        vault._vec_state.update(gen=-1, ids=None, mat=None)
        vault._extra_vec_states.clear()
        vault._stem_cache["key"] = None
        vault._extra_stem_caches.clear()

    def _paths(self):
        """Indexed paths, found by each file's own distinctive wording.

        grep_notes("") matches nothing, so an empty query would make every
        assertion below pass vacuously.
        """
        vault.scan_once()
        found = set()
        for term in ("transcript of the keynote", "clipping already",
                     "vendored package licence"):
            found |= {h["path"] for h in vault.grep_notes(term)}
        return found

    def test_the_listed_tree_is_taken_minus_the_excluded_branch(self):
        paths = self._paths()
        self.assertIn("raw/talk.md", paths)
        self.assertNotIn("raw/clips/one.md", paths)

    def test_the_exclusion_reaches_the_engine_and_is_not_merely_stored(self):
        # The join, not the accessor: a config key nothing passes down is the
        # reader-with-no-writer shape this repo keeps re-learning.
        self.excluded = []
        self._reset()
        self.assertIn("raw/clips/one.md", self._paths())

    def test_engine_defaults_survive_an_owner_exclusion(self):
        # Owner names are ADDED to qocha's own set, never substituted for it,
        # so a config key can never switch .venv/.git/.obsidian back on.
        self.assertNotIn("raw/.venv/lib/LICENSE.md", self._paths())

    def test_blank_and_whitespace_names_are_dropped(self):
        self.excluded = ["  ", "", "clips  "]
        self._reset()
        paths = self._paths()
        self.assertIn("raw/talk.md", paths)
        self.assertNotIn("raw/clips/one.md", paths)

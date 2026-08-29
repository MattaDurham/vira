"""Design Studio save machinery: theme-block token rewriting (value-only,
comment-preserving), studio-addition appends for un-overridden tokens,
change validation, commit-message shape, and the git commit path against
a throwaway repo (push expected to fail without a remote).

Run: .venv/bin/python -m unittest tests.test_designstudio
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import designstudio as ds

THEME = """/* header comment */
:root[data-theme="taurid"] {
  --bg: #1a1d1e;            /* Graphite */
  --accent: #8a8478;   /* Bone */
  --accent-soft: rgba(138, 132, 120, 0.12);
  --radius-sm: 2px;
}

[data-theme="taurid"] h1 { color: #d4d0c6; }
"""


class RewriteTests(unittest.TestCase):
    def test_existing_token_value_swaps_comment_survives(self):
        out = ds.rewrite_theme(THEME, "taurid", {"bg": "#101314"})
        self.assertIn("--bg: #101314;            /* Graphite */", out)
        self.assertNotIn("#1a1d1e", out)

    def test_rgba_value_with_commas(self):
        out = ds.rewrite_theme(THEME, "taurid",
                               {"accent-soft": "rgba(1, 2, 3, 0.5)"})
        self.assertIn("--accent-soft: rgba(1, 2, 3, 0.5);", out)

    def test_new_token_appends_under_marker_inside_block(self):
        out = ds.rewrite_theme(THEME, "taurid", {"space-4": "20px"})
        self.assertIn(ds.ADD_MARK, out)
        self.assertIn("--space-4: 20px;", out)
        # inside the block: addition comes before the closing brace,
        # and the supplemental rule after the block is untouched
        self.assertLess(out.index("--space-4"), out.index("}\n\n[data-theme"))
        self.assertIn('[data-theme="taurid"] h1 { color: #d4d0c6; }', out)

    def test_marker_not_duplicated_on_second_append(self):
        once = ds.rewrite_theme(THEME, "taurid", {"space-4": "20px"})
        twice = ds.rewrite_theme(once, "taurid", {"space-5": "28px"})
        self.assertEqual(twice.count(ds.ADD_MARK.strip()), 1)
        self.assertIn("--space-5: 28px;", twice)

    def test_untouched_lines_identical(self):
        out = ds.rewrite_theme(THEME, "taurid", {"radius-sm": "6px"})
        for line in ("/* header comment */",
                     "  --accent: #8a8478;   /* Bone */"):
            self.assertIn(line, out)

    def test_missing_theme_block_raises(self):
        with self.assertRaises(ValueError):
            ds.rewrite_theme(":root { --bg: #000; }", "taurid", {"bg": "#111"})


class ValidateTests(unittest.TestCase):
    def test_good_changes_pass(self):
        ds.validate_changes("taurid", {"bg": "#101314", "track-display": "0.4em"})

    def test_bad_token_name(self):
        with self.assertRaises(ValueError):
            ds.validate_changes("taurid", {"BG;drop": "#000"})

    def test_bad_value_characters(self):
        for bad in ("#000; } body { color: red", "url(x)\n", "a{b}"):
            with self.assertRaises(ValueError):
                ds.validate_changes("taurid", {"bg": bad})

    def test_bad_theme_name(self):
        with self.assertRaises(ValueError):
            ds.validate_changes("../taurid", {"bg": "#000"})

    def test_empty_changes(self):
        with self.assertRaises(ValueError):
            ds.validate_changes("taurid", {})


class FilesPathTests(unittest.TestCase):
    def test_whitelist_accepts_known_paths(self):
        ds.validate_files("taurid", {
            "foundation/tokens.css": ":root { --bg: #000; }",
            "themes/taurid/theme.css": THEME,
        })

    def test_whitelist_rejects_unknown_and_traversal(self):
        for bad in ("server/main.py", "../secrets", "themes/other/theme.css",
                    "foundation/tokens.css/../../x"):
            with self.assertRaises(ValueError):
                ds.validate_files("taurid", {bad: "x { color: red; }"})

    def test_rejects_oversize_and_binary(self):
        with self.assertRaises(ValueError):
            ds.validate_files("taurid", {"foundation/base.css": "x" * (ds.MAX_FILE_BYTES + 1)})
        with self.assertRaises(ValueError):
            ds.validate_files("taurid", {"foundation/base.css": "a\x00b"})

    def test_files_commit_message_names_token_diff(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "themes/taurid").mkdir(parents=True)
            (repo / "themes/taurid/theme.css").write_text(THEME)
            new_theme = THEME.replace("#8a8478", "#a58a5f").replace("2px", "4px")
            msg = ds.files_commit_message(
                "taurid", repo, {"themes/taurid/theme.css": new_theme})
            self.assertIn("accent", msg)
            self.assertIn("radius-sm", msg)
            self.assertTrue(msg.startswith("taurid: adjust"))

    def test_files_commit_message_names_other_files(self):
        with tempfile.TemporaryDirectory() as td:
            msg = ds.files_commit_message(
                "taurid", Path(td), {"foundation/components.css": ".btn { color: red; }"})
            self.assertIn("edit components.css", msg)


VIRA_STYLE = """:root {
  --bg: #1a1d1e;            /* Graphite */
  --accent: #8a8478;        /* Bone */
  --radius: 2px;
}

body { background: var(--bg); }
"""


class ViraTargetTests(unittest.TestCase):
    def test_vira_whitelist(self):
        ds.validate_files("taurid", {"static/style.css": VIRA_STYLE}, target="vira")
        with self.assertRaises(ValueError):
            ds.validate_files("taurid", {"server/main.py": "x"}, target="vira")
        with self.assertRaises(ValueError):
            ds.validate_files("taurid", {"foundation/tokens.css": ":root{}"}, target="vira")

    def test_unknown_target_rejected(self):
        with self.assertRaises(ValueError):
            ds.validate_files("taurid", {"static/style.css": VIRA_STYLE}, target="crm")

    def test_vira_commit_message_diffs_root_block(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "static").mkdir()
            (repo / "static/style.css").write_text(VIRA_STYLE)
            new = VIRA_STYLE.replace("#8a8478", "#d9a441").replace("2px", "6px")
            msg = ds.files_commit_message(
                "taurid", repo, {"static/style.css": new}, target="vira")
            self.assertIn("accent", msg)
            self.assertIn("radius", msg)
            self.assertTrue(msg.startswith("design: adjust"))

    def test_app_root_is_a_vira_checkout(self):
        self.assertTrue((ds.APP_ROOT / "static/style.css").is_file())
        self.assertTrue((ds.APP_ROOT / "server").is_dir())


class CommitTests(unittest.TestCase):
    def test_commit_message_caps_names(self):
        msg = ds.commit_message("taurid", ["a", "b", "c", "d", "e", "f", "g", "h"])
        self.assertIn("a, b, c, d, e, f", msg)
        self.assertIn("+2 more", msg)
        self.assertTrue(msg.startswith("taurid: adjust"))

    def test_commit_and_push_in_tmp_repo(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "-C", td, "init", "-q"], check=True)
            subprocess.run(["git", "-C", td, "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", td, "config", "user.name", "t"], check=True)
            rel = "themes/taurid/theme.css"
            (repo / "themes/taurid").mkdir(parents=True)
            (repo / rel).write_text(THEME)
            out = ds.commit_and_push(repo, rel, "taurid: adjust bg")
            self.assertTrue(out["committed"])
            self.assertTrue(out["sha"])
            self.assertFalse(out["pushed"])  # no remote in the tmp repo
            # nothing staged -> second commit reports not-committed
            again = ds.commit_and_push(repo, rel, "taurid: adjust bg")
            self.assertFalse(again["committed"])


class ViraSaveSyncsTheBaseSkin(unittest.TestCase):
    """THE JOIN. skins.sync_base_from_style is unit-tested on its own, but the
    defect it fixes was that nothing CALLED it: the studio wrote style.css and
    the base manifest was left to a documented manual step. So this drives the
    real _save_files and asserts both files were written and both were handed
    to the commit."""
    def setUp(self):
        from server import skins
        self.skins = skins
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.tmp / "static" / "skins").mkdir(parents=True)
        (self.tmp / "static/style.css").write_text(VIRA_STYLE, encoding="utf-8")
        real = Path(skins.APP_ROOT) / "static" / "skins"
        shutil.copy(real / f"{skins.BASE_ID}.json",
                    self.tmp / "static" / "skins" / f"{skins.BASE_ID}.json")
        # point skins at the throwaway tree, and record what gets committed
        self.enterContext(mock.patch.object(skins, "SKINS_DIR",
                                            self.tmp / "static" / "skins"))
        self.committed = []
        def fake_commit(repo, relpath, message):
            self.committed.extend(relpath if isinstance(relpath, list) else [relpath])
            return {"committed": True, "sha": "deadbee", "pushed": False}
        self.enterContext(mock.patch.object(ds, "commit_and_push", fake_commit))

    def _base(self):
        return json.loads((self.tmp / "static" / "skins" /
                           f"{self.skins.BASE_ID}.json").read_text(encoding="utf-8"))["tokens"]

    def test_a_vira_save_writes_and_commits_both_files(self):
        self.assertEqual(self._base()["--radius"], "13px")   # the shipped value
        new = VIRA_STYLE.replace("--radius: 2px;", "--radius: 21px;")
        out = ds._save_files(self.tmp, "taurid", {"static/style.css": new}, target="vira")
        self.assertTrue(out["ok"])
        self.assertEqual(self._base()["--radius"], "21px")   # manifest followed
        self.assertIn("static/style.css", self.committed)
        self.assertIn(f"static/skins/{self.skins.BASE_ID}.json", self.committed)

    def test_the_saved_sheet_and_the_base_agree_afterwards(self):
        # the invariant ShippedStateInvariant enforces, checked on the pair
        # this save actually produced
        new = VIRA_STYLE.replace("--accent: #8a8478;", "--accent: #d9a441;")
        ds._save_files(self.tmp, "taurid", {"static/style.css": new}, target="vira")
        written = (self.tmp / "static/style.css").read_text(encoding="utf-8")
        self.assertEqual(self.skins._rewrite_root(written, self._base()), written)

    def test_a_foundation_save_never_touches_the_base(self):
        before = (self.tmp / "static" / "skins" /
                  f"{self.skins.BASE_ID}.json").read_bytes()
        repo = self.tmp / "fnd"
        (repo / "foundation").mkdir(parents=True)
        (repo / "foundation/base.css").write_text("x { color: red; }", encoding="utf-8")
        ds._save_files(repo, "taurid", {"foundation/base.css": "x { color: blue; }"})
        self.assertEqual((self.tmp / "static" / "skins" /
                          f"{self.skins.BASE_ID}.json").read_bytes(), before)
        self.assertNotIn(f"static/skins/{self.skins.BASE_ID}.json", self.committed)



if __name__ == "__main__":
    unittest.main()

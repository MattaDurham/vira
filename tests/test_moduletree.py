"""The module tree: every tracked file placed exactly once, every README's
generated block current, every link resolving. The first class runs
`moduletree.check` on this checkout; the rest exercise the tool on a tiny
synthetic tree so a failure names its cause."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import moduletree


class ThisCheckout(unittest.TestCase):
    def test_check_finds_no_errors(self):
        problems = moduletree.check()
        errors = [m for sev, m in problems if sev == "error"]
        self.assertEqual(errors, [], "\n".join(
            ["the module tree is out of step with the code:"] + errors +
            ["", "place a new file in one modules/*/*/module.json, then run",
             "python -m server.moduletree index --write"]))

    def test_every_page_has_a_breadcrumb_and_a_generated_block(self):
        tree = moduletree.load()
        pages = [moduletree.TREE / "README.md"]
        for mod in tree["modules"]:
            pages.append(mod["dir"] / "README.md")
            for sub in mod["submodules"]:
                pages.append(sub["dir"] / "README.md")
        for page in pages:
            with self.subTest(page=page.relative_to(moduletree.ROOT).as_posix()):
                text = page.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("# "), "starts with its title")
                self.assertIn("Vira > ", text.splitlines()[2],
                              "the third line is the breadcrumb")
                self.assertIn(moduletree.BEGIN, text)
                self.assertIn(moduletree.END, text)

    def test_every_submodule_has_a_summary_and_a_neighbour(self):
        tree = moduletree.load()
        for sub in moduletree.submodules(tree):
            with self.subTest(sub=sub["id"]):
                self.assertTrue(sub.get("summary"))
                self.assertTrue(sub.get("reads") or sub.get("feeds"),
                                "an island is a sign the manifest was not "
                                "thought through")


class Globs(unittest.TestCase):
    def test_star_stays_inside_one_segment(self):
        self.assertTrue(moduletree.matches("static/aquarium-*",
                                           "static/aquarium-fins.js"))
        self.assertFalse(moduletree.matches("static/aquarium-*",
                                            "static/aquarium/koi.glb"))
        self.assertTrue(moduletree.matches("tests/test_branch_*.py",
                                           "tests/test_branch_pr.py"))
        self.assertFalse(moduletree.matches("tests/test_branch_*.py",
                                            "tests/test_branch.py"))

    def test_double_star_is_everything_under_a_folder(self):
        self.assertTrue(moduletree.matches("bridge/**", "bridge/whatsapp/a.js"))
        self.assertTrue(moduletree.matches("bridge/**", "bridge/x"))
        self.assertFalse(moduletree.matches("bridge/**", "bridges/x"))

    def test_a_plain_path_matches_itself_only(self):
        self.assertTrue(moduletree.matches("server/main.py", "server/main.py"))
        self.assertFalse(moduletree.matches("server/main.py", "server/main.pyc"))

    def test_route_prefix_and_exact(self):
        self.assertTrue(moduletree.route_matches("/api/brief", "/api/brief/note"))
        self.assertTrue(moduletree.route_matches("/api/brief", "/api/brief"))
        self.assertFalse(moduletree.route_matches("/api/brief$", "/api/brief/note"))
        self.assertTrue(moduletree.route_matches("/api/brief$", "/api/brief"))
        self.assertFalse(moduletree.route_matches("/api/brie", "/api/brief"))


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class SyntheticTree(unittest.TestCase):
    """A two-module tree in a temp root, with the file list passed in, so
    nothing here touches git or the real checkout."""

    FILES = ["server/alpha.py", "server/beta.py", "static/beta.js",
             "tests/test_alpha.py", "scripts/loose.sh"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        _write(root / "server" / "alpha.py",
               '"""Alpha - the first thing. It does one job.\n\nMore.\n"""\n')
        _write(root / "server" / "beta.py", '"""Beta reads alpha."""\n')
        _write(root / "static" / "beta.js", "/* Beta's window. */\n")
        _write(root / "tests" / "test_alpha.py", '"""Alpha tests."""\n')
        _write(root / "scripts" / "loose.sh", "#!/bin/sh\n# An orphan.\n")
        tree = root / "modules"
        _write(tree / "README.md", "# Vira modules\n\nVira > Modules\n\nprose\n")
        _write(tree / "one" / "module.json", json.dumps({
            "id": "one", "name": "One", "group": "operate", "rank": 10,
            "summary": "The first module.", "windows": ["one"],
            "submodules": ["alpha"]}))
        _write(tree / "one" / "README.md",
               "# One\n\nVira > [Modules](../README.md) > One\n\nprose\n")
        _write(tree / "one" / "alpha" / "module.json", json.dumps({
            "id": "one/alpha", "name": "Alpha", "summary": "Alpha.",
            "owns": ["server/alpha.py", "tests/test_alpha.py"],
            "routes": [], "stores": ["data/alpha.json"],
            "reads": [], "feeds": ["two/beta"], "registry": []}))
        _write(tree / "one" / "alpha" / "README.md",
               "# Alpha\n\nVira > [Modules](../../README.md) > [One](../README.md) > Alpha\n\nprose\n")
        _write(tree / "two" / "module.json", json.dumps({
            "id": "two", "name": "Two", "group": "know", "rank": 10,
            "summary": "The second module.", "windows": [],
            "submodules": ["beta"]}))
        _write(tree / "two" / "README.md",
               "# Two\n\nVira > [Modules](../README.md) > Two\n\nprose\n")
        _write(tree / "two" / "beta" / "module.json", json.dumps({
            "id": "two/beta", "name": "Beta", "summary": "Beta.",
            "owns": ["server/beta.py", "static/beta.js"],
            "routes": [], "stores": [], "reads": ["one/alpha"], "feeds": [],
            "registry": []}))
        _write(tree / "two" / "beta" / "README.md",
               "# Beta\n\nVira > [Modules](../../README.md) > [Two](../README.md) > Beta\n\nprose\n")
        self.root = root
        patches = [mock.patch.object(moduletree, "ROOT", root),
                   mock.patch.object(moduletree, "TREE", tree)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_index_writes_then_reports_current(self):
        stale = moduletree.index(write=True, files=self.FILES)
        self.assertEqual(len(stale), 5, "root, two modules, two submodules")
        self.assertEqual(moduletree.index(write=False, files=self.FILES), [])
        page = (self.root / "modules/one/alpha/README.md").read_text(encoding="utf-8")
        self.assertIn("prose", page, "the hand-written half survives")
        self.assertIn("| `server/alpha.py` | Alpha - the first thing. |", page)
        self.assertIn("[Two / Beta](../../two/beta/README.md)", page)
        self.assertIn("- `data/alpha.json`", page)

    def test_check_names_the_orphan_and_the_double_claim(self):
        moduletree.index(write=True, files=self.FILES)
        problems = moduletree.check(files=self.FILES)
        errors = [m for sev, m in problems if sev == "error"]
        self.assertEqual(errors, ["unplaced: scripts/loose.sh (add it to a "
                                  "module.json 'owns' list)"])
        mj = self.root / "modules/two/beta/module.json"
        d = json.loads(mj.read_text(encoding="utf-8"))
        d["owns"].append("server/alpha.py")
        mj.write_text(json.dumps(d), encoding="utf-8")
        errors = [m for sev, m in moduletree.check(files=self.FILES)
                  if sev == "error"]
        self.assertIn("server/alpha.py: claimed by one/alpha, two/beta", errors)
        self.assertTrue(any(m.endswith("generated block is stale; run "
                                       "`python -m server.moduletree index "
                                       "--write`") for m in errors),
                        "a manifest edit without a re-render is reported")

    def test_check_names_a_dangling_link_and_an_unlisted_folder(self):
        moduletree.index(write=True, files=self.FILES)
        mj = self.root / "modules/one/alpha/module.json"
        d = json.loads(mj.read_text(encoding="utf-8"))
        d["feeds"] = ["two/gamma"]
        mj.write_text(json.dumps(d), encoding="utf-8")
        _write(self.root / "modules/two/gamma/module.json", json.dumps({
            "id": "two/gamma", "name": "Gamma", "summary": "Gamma.",
            "owns": [], "reads": [], "feeds": []}))
        errors = [m for sev, m in moduletree.check(files=self.FILES)
                  if sev == "error"]
        self.assertIn("two: submodule folder 'gamma' is not listed in "
                      "module.json", errors)
        self.assertIn("two/gamma: README.md is missing", errors)
        self.assertNotIn("one/alpha: feeds unknown id 'two/gamma'", errors,
                         "a present folder is a known id even when unlisted")

    def test_brief_carries_the_full_docstring(self):
        moduletree.index(write=True, files=self.FILES)
        tree = moduletree.load()
        text = moduletree.brief(tree, moduletree.find(tree, "one/alpha"),
                                files=self.FILES)
        self.assertIn("# Alpha", text)
        self.assertIn("### server/alpha.py", text)
        self.assertIn("More.", text, "the whole docstring, not its first line")
        whole = moduletree.brief(tree, moduletree.find(tree, "two"),
                                 files=self.FILES)
        self.assertIn("Beta reads alpha.", whole)

    def test_plan_lists_a_git_mv_per_file(self):
        tree = moduletree.load()
        text = moduletree.plan(moduletree.find(tree, "two/beta"),
                               files=self.FILES)
        self.assertIn("git mv server/beta.py modules/two/beta/server/beta.py",
                      text)
        self.assertIn("git mv static/beta.js modules/two/beta/static/beta.js",
                      text)

    def test_where(self):
        tree = moduletree.load()
        claims = moduletree.ownership(tree, self.FILES)
        self.assertEqual(claims["server/beta.py"], ["two/beta"])
        self.assertNotIn("scripts/loose.sh", claims)


if __name__ == "__main__":
    unittest.main()

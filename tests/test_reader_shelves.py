"""Reader > Documents, Shelves view (static/reader-shelves.js).

Two halves. The contract with app.js: the view registers itself in
RDOC_VIEWS, rdocBar turns every registered view into a button on the
List/Grid switch, and renderReaderDocs hands registered views the same groups
the other views draw. And the grouping itself, run for real in node: each
"Group by" group splits into shelves on a second axis, biggest first,
untagged last, each newest first.
"""

from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
JS = (STATIC / "reader-shelves.js").read_text(encoding="utf-8")
CSS = (STATIC / "reader-shelves.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


class ShelvesContract(unittest.TestCase):

    def test_it_registers_as_a_reader_view(self):
        self.assertIn('window.RDOC_VIEWS[VIEW] = { label: "Shelves", render }', JS)
        self.assertIn('const VIEW = "shelves"', JS)

    def test_app_js_turns_registered_views_into_buttons_and_renderers(self):
        self.assertIn('...Object.entries(window.RDOC_VIEWS || {})', APP)
        self.assertIn("(window.RDOC_VIEWS || {})[rdocView]", APP)

    def test_the_choice_lives_in_the_readers_own_setting(self):
        # The view button saves rdocView under vira-reader-view; Shelves keeps
        # no setting of its own for which view is showing.
        self.assertIsNone(re.search(r"ls(Get|Set)\(\s*\"vira-reader-view", JS))
        self.assertIn('lsSet("vira-reader-view", v)', APP)

    def test_loaded_after_app_js(self):
        app = HTML.index('<script src="/app.js"></script>')
        mine = HTML.index('<script src="/reader-shelves.js"></script>')
        self.assertLess(app, mine)
        self.assertIn('<link rel="stylesheet" href="/reader-shelves.css">', HTML)

    def test_strips_snap_and_motion_respects_the_setting(self):
        self.assertRegex(CSS, r"scroll-snap-type:\s*x mandatory")
        self.assertRegex(CSS, r"scroll-snap-align:\s*start")
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertIn("prefers-reduced-motion", JS)

    def test_nothing_waits_on_an_observer(self):
        # Content hidden until an observer fires is how the retired Grid view
        # once showed an empty page (2026-08-11).
        self.assertFalse("new IntersectionObserver" in JS)
        # The one thing hidden is a picture still loading, over a grey
        # placeholder; its load or error event always uncovers the tile.
        hidden = re.findall(r"([^{}]+)\{[^}]*opacity:\s*0\s*;", CSS)
        self.assertEqual([h.strip() for h in hidden], [".shv-face.loading img"])
        self.assertIn('img.addEventListener("load"', JS)
        self.assertIn('img.addEventListener("error"', JS)


HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const src = fs.readFileSync(process.argv[1], "utf8");
const UNT = " untagged";
const ctx = {
  window: { __VIRA_TEST__: true },
  document: { addEventListener() {} },
  RDOC_UNTAGGED: UNT,
  READER_KIND: { plan: "plan", retro: "retro", walkthrough: "walkthrough" },
  rdocGroup: "module",
  rdocTagsOf: (it, ax) => ((it.tags || {})[ax] || []),
  rdocSortNew: (xs) => xs.slice().sort(
    (a, b) => String(b.created).localeCompare(String(a.created))),
  rdocGroupLabel: (k) => k,
  readerKindPlural: (k) => k.charAt(0).toUpperCase() + k.slice(1) + "s",
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const T = ctx.window.__VIRA_SHELVES_TESTS__;
const out = {};
const items = [
  { id: "a", kind: "plan", created: "2026-11-01", tags: { module: ["Reader"] } },
  { id: "b", kind: "plan", created: "2026-11-03", tags: { module: ["Reader"] } },
  { id: "c", kind: "retro", created: "2026-11-02", tags: { module: ["Work"] } },
  { id: "d", created: "2026-11-04", tags: {} },
  { id: "e", kind: "plan", created: "2026-10-01", tags: { module: ["Work"] } },
];
out.byKind = T.shelvesOf({ key: "Reader", items }).map(
  (s) => [s.key, s.label, s.items.map((i) => i.id)]);
ctx.rdocGroup = "kind";
out.axisWhenKind = T.subAxis();
out.byModule = T.shelvesOf({ key: "plan", items }).map(
  (s) => [s.key, s.items.map((i) => i.id)]);
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ShelvesGrouping(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        r = subprocess.run(
            [shutil.which("node"), "-e", HARNESS,
             str(STATIC / "reader-shelves.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_shelves_split_by_kind_biggest_first_untagged_last(self):
        self.assertEqual(self.out["byKind"], [
            ["plan", "Plans", ["b", "a", "e"]],
            ["retro", "Retros", ["c"]],
            [" untagged", "Not yet tagged", ["d"]],
        ])

    def test_grouped_by_source_the_shelves_split_by_topic_then_feature(self):
        # Captures carry topics (the theme axis); documents without one
        # fall back to their feature tag.
        self.assertEqual(self.out["axisWhenKind"], "topic")
        self.assertEqual(self.out["byModule"], [
            ["Reader", ["b", "a"]],
            ["Work", ["c", "e"]],
            [" untagged", ["d"]],
        ])


if __name__ == "__main__":
    unittest.main()

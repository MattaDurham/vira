"""Reader > Documents, Explore view (static/reader-explore.js).

The view lives in its own files and joins the List/Grid switch through the
RDOC_VIEWS registry (one line in rdocBar, one in renderReaderDocs, shared
with the Shelves view), so the contract is mostly about that join: the page
loads the file after app.js, the switch lists what the registry holds, and
renderReaderDocs hands a registered view the list and the groups. The tile rule (one tall tile per
block of five, alternating sides) is run in Node so a change to it is caught
by its effect, not its spelling.
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
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "reader-explore.js").read_text(encoding="utf-8")
CSS = (STATIC / "reader-explore.css").read_text(encoding="utf-8")


NODE_PROBE = r"""
global.window = {};
global.matchMedia = () => ({ matches: false });
global.rdocTagsOf = (it, ax) => ((it.tags || {})[ax] || []);
const rx = require(process.argv[1]);
const views = {};
for (const [k, v] of Object.entries(window.RDOC_VIEWS || {}))
  views[k] = { label: v.label, render: typeof v.render };
const out = { views, shapes: [] };
for (let i = 0; i < 20; i++) out.shapes.push(rx.rxShape(i));
out.titles = [
  rx.rxTitle({ title: "2026-09-20 day thedurham-nyc" }),
  rx.rxTitle({ title: "Durham Design Build Launch" }),
  rx.rxTitle({ title: "2026-09-20" }),
];
out.tones = [rx.rxTone({ kind: "retro", tags: { module: ["reader"] } }),
             rx.rxTone({ kind: "plan", tags: { module: ["reader"] } })];
console.log(JSON.stringify(out));
"""


class ExploreIsWired(unittest.TestCase):
    def test_the_page_loads_the_view_after_app_js(self):
        scripts = re.findall(r'<script src="/([^"]+)"', INDEX)
        self.assertIn("reader-explore.js", scripts)
        self.assertLess(scripts.index("app.js"), scripts.index("reader-explore.js"),
                        "the view wraps renderReaderDocs, so app.js must load first")
        self.assertIn('<link rel="stylesheet" href="/reader-explore.css">', INDEX)

    def test_the_switch_and_the_render_read_the_registry(self):
        self.assertIn('[["list", "List"], ["grid", "Grid"], ...Object.entries('
                      'window.RDOC_VIEWS || {}).map(([v, x]) => [v, x.label])]', APP)
        self.assertIn("window.RDOC_VIEWS[rdocView].render(list, groups)", APP)
        # Same saved setting as List and Grid.
        self.assertIn('lsSet("vira-rdoc-view", v)', APP)

    def test_it_uses_the_grids_list_and_the_readers_open_path(self):
        for name in ("rdocSortNew", "openReaderItem", "enterFocus"):
            self.assertIn(name, JS)

    def test_no_reveal_hides_a_tile(self):
        # tests/test_reader_grid_reveal.py pins the observer count; the wall
        # loads by a scroll listener and never hides a tile until it is seen.
        self.assertNotIn("IntersectionObserver", JS)
        self.assertIn("prefers-reduced-motion", CSS)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ExploreRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        res = subprocess.run(
            ["node", "-e", NODE_PROBE, str(STATIC / "reader-explore.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        if res.returncode:
            raise AssertionError(res.stderr)
        cls.out = json.loads(res.stdout.strip().splitlines()[-1])

    def test_it_registers_one_explore_option(self):
        self.assertEqual(self.out["views"],
                         {"explore": {"label": "Explore", "render": "function"}})

    def test_every_block_of_five_has_one_tall_tile_on_alternating_sides(self):
        shapes = self.out["shapes"]
        for b in range(4):
            block = shapes[b * 5:b * 5 + 5]
            tall = [s for s in block if s]
            self.assertEqual(len(tall), 1, block)
            self.assertEqual(tall[0], "tall-r" if b % 2 == 0 else "tall-l")

    def test_a_dated_title_drops_its_date_but_never_goes_blank(self):
        self.assertEqual(self.out["titles"],
                         ["day thedurham-nyc", "Durham Design Build Launch",
                          "2026-09-20"])

    def test_documents_on_one_topic_share_a_tone(self):
        a, b = self.out["tones"]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()

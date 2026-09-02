"""Orbits is a VIEW of the World window, not a dock window (2026-09-02,
owner's call: "another view in the same module - the World window has the
galaxy (3D) and the Orbits").

Source contracts over the shipped files, comments stripped so a guard
surviving only as the sentence explaining it cannot pass. What they pin:
the retired section is gone and the Orbits stage sits inside #view-atlas;
WINDOWS carries no `orbits` entry; the #orbits deep link and the palette
row both land on World with the Orbits view selected; opening World loads
whichever view is up; and the CSS hides exactly one stage.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _strip(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


class OrbitsIsAViewOfWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _strip((ROOT / "static" / "app.js").read_text(encoding="utf-8"))
        cls.html = re.sub(r"<!--.*?-->", "",
                          (ROOT / "static" / "index.html").read_text(encoding="utf-8"),
                          flags=re.S)
        cls.css = _strip((ROOT / "static" / "orbits.css").read_text(encoding="utf-8"))

    def test_no_orbits_window_and_no_orbits_section(self):
        self.assertNotIn('id: "orbits"', self.app)
        self.assertNotIn('id="view-orbits"', self.html)
        self.assertNotIn("#win-orbits", self.css)

    def test_the_orbits_stage_lives_inside_the_world_section(self):
        start = self.html.index('id="view-atlas"')
        end = self.html.index("</section>", start)
        body = self.html[start:end]
        self.assertIn('id="orbits-stage"', body)
        self.assertIn('id="atlas-stage"', body)
        self.assertIn('id="world-views"', body)
        # exactly one stage is up at rest: Orbits ships hidden
        self.assertRegex(body, r'id="orbits-stage"\s+hidden')

    def test_the_view_switch_hides_one_stage_and_shows_the_other(self):
        m = re.search(r"function setWorldView\([^)]*\)\s*\{(.*?)\n\}", self.app, re.S)
        self.assertIsNotNone(m, "setWorldView missing")
        body = m.group(1)
        self.assertIn("stage.hidden = orbits", body)
        self.assertIn("ostage.hidden = !orbits", body)
        self.assertIn('lsSet("vira-world-view", v)', body)
        # a hidden attribute is inert without a rule: style.css has none
        self.assertIn(".orbits-stage[hidden], .atlas-stage[hidden] { display: none !important; }",
                      self.css)

    def test_opening_world_loads_the_view_that_is_up(self):
        self.assertIn('if (id === "atlas") worldViewLoad();', self.app)
        m = re.search(r"function worldViewLoad\(\)\s*\{(.*?)\n\}", self.app, re.S)
        self.assertIsNotNone(m)
        self.assertIn('import("/orbits.js")', m.group(1))
        self.assertIn("window.atlasLoad?.()", m.group(1))

    def test_deep_link_and_palette_land_on_world_with_orbits_up(self):
        self.assertIn('"orbits": () => { setWorldView("orbits", { load: false }); openApp("atlas"); }',
                      self.app)
        self.assertIn('run: () => { setWorldView("orbits", { load: false }); openWindow("atlas"); }',
                      self.app)


if __name__ == "__main__":
    unittest.main()

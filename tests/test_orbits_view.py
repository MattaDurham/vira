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


class OrbitsInertia(unittest.TestCase):
    """A released spin coasts and winds down (2026-09-02). Source contracts:
    the fling is read off the drag's trailing samples, applied in the frame
    loop with exponential decay, killed by the next press, and never
    produced under reduced motion."""

    def setUp(self):
        self.src = (ROOT / "static" / "orbits.js").read_text(encoding="utf-8")

    def test_release_hands_over_a_velocity(self):
        self.assertIn("S.spinV = flingVelocity(d.samples)", self.src)

    def test_the_coast_decays_in_the_frame_loop(self):
        self.assertIn("S.spin += S.spinV * dt", self.src)
        self.assertIn("S.spinV *= Math.exp(-FLING_DECAY * dt)", self.src)

    def test_a_press_stops_the_record(self):
        i_down = self.src.index("S.spinV = 0;                            // a hand on the record stops it")
        i_move = self.src.index('cv.addEventListener("pointermove"')
        self.assertLess(i_down, i_move)

    def test_reduced_motion_never_flings(self):
        body = self.src[self.src.index("function flingVelocity"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("if (S.reduced", body)


class GrabbingTheSunPansTheView(unittest.TestCase):
    """A press on the sun MOVES the sky; every other press spins it.

    Source contracts over static/orbits.js with comments stripped, so a
    guard surviving only as the sentence explaining it cannot pass.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _strip((ROOT / "static" / "orbits.js").read_text(encoding="utf-8"))

    def test_the_press_records_whether_it_landed_on_the_sun(self):
        self.assertRegex(self.js, r"pan:\s*onSun\(e\.clientX, e\.clientY\)")

    def test_the_sun_hit_test_uses_the_radius_the_sun_is_drawn_at(self):
        self.assertRegex(self.js, r"function onSun\b")
        self.assertRegex(self.js, r"Math\.hypot\(px - sx\(0\), py - sy\(0\)\)\s*<=\s*22 \* S\.cur\.k")
        self.assertIn("const sunR = 22 * k;", self.js)

    def test_a_sun_drag_moves_the_camera_and_never_turns_the_sky(self):
        move = self.js[self.js.index("if (S.drag.moved && S.drag.pan)"):]
        move = move[:move.index("if (S.drag.moved) {")]
        self.assertIn("S.cam.x = S.drag.pan.x - dx / k", move)
        self.assertIn("S.cur.x = S.cam.x", move)   # follows the hand, never eased
        self.assertNotIn("S.spin", move)
        self.assertIn("return;", move)             # the spin branch is never reached

    def test_a_pan_never_flings(self):
        self.assertIn("if (d.moved && d.pan) return;", self.js)

    def test_the_sun_advertises_itself_as_grabbable(self):
        self.assertRegex(self.js, r'onSun\(e\.clientX, e\.clientY\) \? "grab"')

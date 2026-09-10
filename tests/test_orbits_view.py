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
        # the hand's screen delta is read in canvas pixels (the window's
        # content zoom, z) before it moves the sky - see toCanvas()
        self.assertIn("S.cam.x = S.drag.pan.x - dx / z / k", move)
        self.assertIn("S.cur.x = S.cam.x", move)   # follows the hand, never eased
        self.assertNotIn("S.spin", move)
        self.assertIn("return;", move)             # the spin branch is never reached

    def test_a_pan_never_flings(self):
        self.assertIn("if (d.moved && d.pan) return;", self.js)

    def test_the_sun_advertises_itself_as_grabbable(self):
        self.assertRegex(self.js, r'onSun\(e\.clientX, e\.clientY\) \? "grab"')


class TheSunStaysOnTheStageAndCardsStayClickable(unittest.TestCase):
    """Owner's call, 2026-09-10: the sun (him) never leaves the stage - it is
    the handle the whole picture is moved by - and contacts may overlap but
    never bury each other, so the one behind is always clickable.

    Source contracts over static/orbits.js (comments stripped) pin WHERE the
    two rules bind; tests/orbits_layout_harness.mjs runs the real module in
    node and measures that they HOLD.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _strip((ROOT / "static" / "orbits.js").read_text(encoding="utf-8"))

    def _fn(self, name):
        i = self.js.index("function " + name + "(")
        j = self.js.index("\n}\n", i)
        return self.js[i:j]

    # -- the sun --
    def test_the_clamp_runs_in_the_frame_loop_before_every_draw(self):
        frame = self._fn("frame")
        clamp = frame.index("if (keepSun()) moving = true;")
        draw = frame.index("if (moving || S.dirty) { draw(); S.dirty = false; }")
        self.assertLess(clamp, draw)
        # after the ease, so the eased camera is what gets clamped, not only the target
        self.assertLess(frame.index('for (const key of ["x", "y", "k"])'), clamp)

    def test_the_clamp_binds_target_and_eased_camera_alike(self):
        body = self._fn("keepSun")
        self.assertIn("clampToSun(S.cam, S.W, S.H)", body)
        self.assertIn("clampToSun(S.cur, S.W, S.H)", body)

    def test_the_pad_keeps_a_grab_sized_piece_of_the_sun_inside(self):
        # the sun is drawn at 22 * k; the pad tracks that with a floor and a ceiling
        self.assertIn("const sunPad = (k) => clamp(22 * k + 6, 16, 48);", self.js)
        self.assertIn("const sunR = 22 * k;", self.js)

    def test_flying_to_a_card_caps_the_zoom_by_the_sun(self):
        body = self._fn("flyTo")
        self.assertIn("kk = Math.min(kk, sunZoomCap(node, shift, kk, S.W, S.H))", body)
        # the cap runs BEFORE the camera target is written from it
        self.assertLess(body.index("sunZoomCap("), body.index("S.cam.k = kk;"))

    def test_fly_and_follow_share_one_panel_shift(self):
        # two copies of "how far does the panel push the card" were what let
        # the phone's bottom sheet be shifted as if it docked at the side
        self.assertIn("const shift = opts.clearCard ? cardShift() : { x: 0, y: 0 };", self._fn("flyTo"))
        self.assertIn("const sh = cardShift();", self._fn("frame"))
        self.assertNotIn("S.cardEl.offsetWidth", self._fn("frame"))
        self.assertNotIn("S.cardEl.offsetWidth", self._fn("flyTo"))
        body = self._fn("cardShift")
        self.assertIn("w >= 0.8 * S.W ? { x: 0, y: h / 2 } : { x: w / 2, y: 0 }", body)

    def test_every_pointer_path_reads_the_canvas_through_one_conversion(self):
        # a floating window's body is CSS-zoomed; pointer events arrive in
        # screen pixels while the canvas is laid out in its own. Measured at
        # zoom 0.6: the drawn sun was not grabbable and a click on a card
        # selected its neighbour. toCanvas() is the one conversion.
        conv = self._fn("toCanvas")
        self.assertIn("r.width / S.W", conv)
        for name in ("onSun", "sunAngle", "hit", "showTip"):
            self.assertIn("toCanvas(", self._fn(name), name + " must convert the pointer")
        pointer = self._fn("bindPointer")
        self.assertIn("zoomAt(p.x, p.y, Math.exp(-e.deltaY * 0.0016))", pointer)   # the wheel
        self.assertIn("zoomAt(m.x, m.y, d / S.pinch.d)", pointer)                  # the pinch
        # no pointer path subtracts the rect itself any more
        for name in ("onSun", "sunAngle", "hit"):
            self.assertNotIn("getBoundingClientRect", self._fn(name))

    # -- the cards --
    def test_the_layout_relaxes_the_deal_and_holds_a_rotation_proof_separation(self):
        self.assertIn("wedges.relax = relaxCards(nodes, per * A_SLACK);", self._fn("arrange"))
        body = self._fn("minSep")
        self.assertIn("Math.SQRT2 * (e + (lg.h - sm.h) / 2)", body)
        self.assertIn("Math.max(EXPOSE_MIN, EXPOSE * sm.w)", body)

    def test_a_card_never_leaves_its_bounds_between_sweeps(self):
        body = self._fn("relaxCards")
        self.assertIn("r = clamp(r, c.n.r - rSlack, c.n.r + rSlack);", body)
        self.assertIn("clamp(wrap(ang - mid), w.a0 - aSlack - mid, w.a1 + aSlack - mid)", body)
        # the angular slack stays inside the wedge gap: a bled card sits in
        # the gap, never over the neighbouring wedge's arc
        self.assertIn("const WEDGE_GAP = 2.2;", self.js)
        m = re.search(r"A_SLACK = ([\d.]+)", self.js)
        self.assertLess(float(m.group(1)), 2.2 / 2)

    def test_the_hit_test_reads_the_paint_order_from_the_top(self):
        hit = self._fn("hit")
        self.assertIn("const order = paintOrder();", hit)
        self.assertIn("for (let i = order.length - 1; i >= 0; i--)", hit)
        self.assertNotIn("bestD", hit)          # nearest-centre is gone
        draw = self._fn("draw")
        self.assertIn("const order = paintOrder();", draw)

    # -- the measurement --
    def test_the_rules_hold_when_the_real_module_runs(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed here; the harness needs it")
        result = subprocess.run([node, "tests/orbits_layout_harness.mjs"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("orbits layout harness: ok", result.stdout)

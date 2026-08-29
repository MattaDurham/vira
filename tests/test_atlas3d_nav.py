"""The Visual Network's navigation is the Image Atlas's navigation.

The ask was exact: rotate, pan and scroll in the Visual Network must be the
same gesture, with the same feel, as the Image Atlas at
thedurham.nyc/lab/atlas. That viewer ships in the chaska package as
`chaska/viewer/index.html`, so this file holds the two together the only
way a Python suite can hold a frontend claim: as a SOURCE contract.

Two halves, and the second is the one that survives time.

1. The literal contract - static/atlas3d.js declares every camera value in
   one NAV block and actually applies it. That is what the module promises.

2. The PARITY contract - when the Image Atlas viewer is on this machine,
   the same values are read out of IT and compared. A test that only pinned
   our own numbers would pass forever while the thing being copied moved.
   `test_the_parity_scan_really_reads_the_viewer` guards the guard, because
   a regex sweep that silently matched nothing passes every assertion above
   it (the reach-pin discipline).

Mutation-checked when written: changing any NAV value, dropping the
middle/right TRUCK rebinding, dropping the shift rebinding, un-capturing
the pointerdown listener, or pointing the imports at a CDN each fails at
least one case.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "static" / "atlas3d.js"
ATLAS = ROOT / "static" / "atlas.js"
SRC = MOD.read_text(encoding="utf-8")


def _viewer_source():
    """The Image Atlas viewer, if this machine has chaska installed."""
    try:
        import chaska
    except Exception:
        return None
    p = Path(chaska.__file__).resolve().parent / "viewer" / "index.html"
    return p.read_text(encoding="utf-8") if p.is_file() else None


VIEWER = _viewer_source()
needs_viewer = unittest.skipIf(
    VIEWER is None, "the Image Atlas viewer (chaska) is not on this machine")


def nav(key):
    """One value out of atlas3d.js's NAV block."""
    m = re.search(r"export const NAV = \{(.*?)\n\};", SRC, re.S)
    assert m, "NAV block not found"
    v = re.search(rf"\b{key}:\s*([^,\n]+)", m.group(1))
    assert v, f"NAV.{key} not declared"
    return v.group(1).strip()


class NavIsDeclaredAndApplied(unittest.TestCase):
    """The numbers are stated once and actually reach the controls."""

    def test_the_camera_values_are_the_viewers(self):
        self.assertEqual(nav("fov"), "55")
        self.assertEqual(nav("nearFactor"), "0.004")
        self.assertEqual(nav("farFactor"), "40")
        self.assertEqual(nav("smoothTime"), "0.28")
        self.assertEqual(nav("draggingSmoothTime"), "0.14")
        self.assertEqual(nav("minDistanceFactor"), "0.015")
        self.assertEqual(nav("maxDistanceFactor"), "8")
        self.assertEqual(nav("dollyToCursor"), "true")
        self.assertEqual(nav("driftRate"), "0.12")
        self.assertEqual(nav("driftAfter"), "4.0")

    def test_every_nav_value_is_read_somewhere(self):
        # a declared constant nothing consumes is the reader-with-no-writer
        # shape inverted, and just as silent
        block = re.search(r"export const NAV = \{(.*?)\n\};", SRC, re.S).group(1)
        keys = re.findall(r"^\s*(\w+):", block, re.M)
        self.assertGreaterEqual(len(keys), 10)
        body = SRC[SRC.index("export function create"):]
        for k in keys:
            self.assertIn(f"NAV.{k}", body, f"NAV.{k} is declared and unused")

    def test_the_bounds_are_multiples_of_the_graphs_own_radius(self):
        self.assertRegex(SRC, r"minDistance\s*=\s*b\.radius \* NAV\.minDistanceFactor")
        self.assertRegex(SRC, r"maxDistance\s*=\s*b\.radius \* NAV\.maxDistanceFactor")
        self.assertRegex(SRC, r"b\.radius \* NAV\.nearFactor")
        self.assertRegex(SRC, r"b\.radius \* NAV\.farFactor")

    def test_the_buttons_carry_the_viewers_actions(self):
        # left keeps camera-controls' ROTATE default; the viewer rebinds the
        # other two to TRUCK so middle-drag and right-drag both pan
        self.assertRegex(
            SRC, r"mouseButtons\.middle = CameraControls\.ACTION\.TRUCK")
        self.assertRegex(
            SRC, r"mouseButtons\.right = CameraControls\.ACTION\.TRUCK")

    def test_shift_swaps_the_left_button_to_pan(self):
        self.assertRegex(
            SRC,
            r'(?s)key === "Shift".{0,120}mouseButtons\.left = '
            r'CameraControls\.ACTION\.TRUCK')
        self.assertRegex(
            SRC,
            r'(?s)key === "Shift".{0,120}mouseButtons\.left = '
            r'CameraControls\.ACTION\.ROTATE')

    def test_the_press_re_anchors_the_orbit_in_the_capture_phase(self):
        # capture: the anchor must be set before camera-controls' own
        # pointerdown snapshots the drag, or it is ignored mid-gesture
        m = re.search(r'canvas\.addEventListener\("pointerdown",(.*?)\}, true\);',
                      SRC, re.S)
        self.assertIsNotNone(m, "pointerdown is not registered in the capture phase")
        self.assertIn("anchorOrbit(x, y)", m.group(1))

    def test_the_libraries_are_vendored_not_fetched(self):
        self.assertIn('from "./vendor/three.module.js"', SRC)
        self.assertIn('from "./vendor/camera-controls.module.js"', SRC)
        self.assertNotRegex(SRC, r'from "https?://')
        for name in ("three.module.js", "three.core.js", "camera-controls.module.js"):
            self.assertTrue((ROOT / "static" / "vendor" / name).is_file(),
                            f"{name} is not vendored")


class ParityWithTheImageAtlas(unittest.TestCase):
    """Read the viewer itself, so the copy cannot drift away from it."""

    @needs_viewer
    def test_the_parity_scan_really_reads_the_viewer(self):
        # the reach pin: a sweep that matched nothing would pass every case
        # below it while proving nothing at all
        self.assertIn("new CameraControls(", VIEWER)
        self.assertRegex(VIEWER, r"controls\.smoothTime\s*=")

    @needs_viewer
    def test_the_smoothing_matches(self):
        self.assertEqual(
            re.search(r"controls\.smoothTime\s*=\s*([\d.]+)", VIEWER).group(1),
            nav("smoothTime"))
        self.assertEqual(
            re.search(r"controls\.draggingSmoothTime\s*=\s*([\d.]+)", VIEWER).group(1),
            nav("draggingSmoothTime"))

    @needs_viewer
    def test_the_distance_bounds_match(self):
        self.assertEqual(
            re.search(r"controls\.minDistance\s*=\s*b\.radius\*([\d.]+)",
                      VIEWER).group(1), nav("minDistanceFactor"))
        self.assertEqual(
            re.search(r"controls\.maxDistance\s*=\s*b\.radius\*([\d.]+)",
                      VIEWER).group(1), nav("maxDistanceFactor"))

    @needs_viewer
    def test_the_lens_matches(self):
        m = re.search(r"new THREE\.PerspectiveCamera\(\s*([\d.]+),[^,]+,"
                      r"\s*b\.radius\*([\d.]+),\s*b\.radius\*([\d.]+)\)", VIEWER)
        self.assertIsNotNone(m, "the viewer's camera construction moved")
        self.assertEqual(m.group(1), nav("fov"))
        self.assertEqual(m.group(2), nav("nearFactor"))
        self.assertEqual(m.group(3), nav("farFactor"))

    @needs_viewer
    def test_the_idle_drift_matches(self):
        self.assertEqual(
            re.search(r"controls\.azimuthAngle \+= ([\d.]+) \* dt",
                      VIEWER).group(1), nav("driftRate"))
        self.assertEqual(
            re.search(r"idleT > ([\d.]+)", VIEWER).group(1), nav("driftAfter"))

    @needs_viewer
    def test_dolly_to_cursor_matches(self):
        self.assertRegex(VIEWER, r"controls\.dollyToCursor\s*=\s*true")
        self.assertEqual(nav("dollyToCursor"), "true")

    @needs_viewer
    def test_the_button_rebinding_matches(self):
        for button in ("middle", "right"):
            self.assertRegex(
                VIEWER,
                rf"controls\.mouseButtons\.{button} = CameraControls\.ACTION\.TRUCK")
        self.assertRegex(
            VIEWER, r"mouseButtons\.left = CameraControls\.ACTION\.TRUCK")
        self.assertRegex(
            VIEWER, r"mouseButtons\.left = CameraControls\.ACTION\.ROTATE")

    @needs_viewer
    def test_the_same_camera_controls_build_is_vendored(self):
        # a different build could change what an ACTION means underneath us
        theirs = (Path(__import__("chaska").__file__).resolve().parent
                  / "viewer" / "vendor" / "camera-controls.module.js")
        ours = ROOT / "static" / "vendor" / "camera-controls.module.js"
        self.assertEqual(theirs.read_bytes(), ours.read_bytes())


class TheFlatFallbackSurvives(unittest.TestCase):
    """A browser with no WebGL still gets the module, in two dimensions."""

    def setUp(self):
        self.src = ATLAS.read_text(encoding="utf-8")

    def test_the_renderer_is_optional_at_every_painting_seam(self):
        for fn in ("draw", "wake", "resize", "centerOn"):
            m = re.search(rf"function {fn}\(([^)]*)\) \{{\n(.*?)\n", self.src)
            self.assertIsNotNone(m, f"{fn}() moved")
            self.assertIn("R3", m.group(2),
                          f"{fn}() does not delegate to the 3D renderer")

    def test_a_renderer_that_cannot_start_leaves_the_flat_canvas_up(self):
        # create() returns null without WebGL; only a live renderer hides the
        # 2D canvas, so the failure mode is flat, never blank
        self.assertRegex(
            self.src,
            r"if \(r\) \{\s*\n\s*R3 = r;\s*\n\s*canvas\.style\.display = \"none\"")

    def test_what_a_hit_means_has_one_implementation(self):
        for fn in ("hitHover", "hitSelect", "hitOpen", "hitEmpty", "hitContext"):
            self.assertIn(f"function {fn}(", self.src)
            self.assertIn(f"on{fn[3:]}: {fn}", self.src.replace("onHover", "onHover"))


if __name__ == "__main__":
    unittest.main()


def _body(name, src=SRC):
    """One function's body, comments stripped.

    Comments are stripped because every guard here is explained by a comment
    directly above it, and a scan that matched the explanation instead of the
    code would pass against a file that had lost the code (the turn-closeout
    trap this repo has been bitten by).
    """
    start = src.index(f"function {name}(")
    depth, i, opened = 0, src.index("{", start), False
    while i < len(src):
        if src[i] == "{":
            depth, opened = depth + 1, True
        elif src[i] == "}":
            depth -= 1
            if opened and depth == 0:
                break
        i += 1
    body = src[start:i + 1]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in body.splitlines())


class TheOrbitAnchorIsGuarded(unittest.TestCase):
    """Two properties the first transcription of anchorOrbit lost.

    Both were measured on the real 200-node graph before they were restored,
    and both present as the same thing to the eye: every name plate on the
    stage flying to a new place on a press that should not have moved the
    camera at all.
    """

    def test_a_live_selection_is_never_re_anchored_away(self):
        # The viewer's own `if (anchorId !== null) return;`, whose comment
        # says re-anchoring "would swing the pinned image off its spot on the
        # very next drag". Measured without it: a 60px drag on empty sky moved
        # the selected person 303px across the screen. With it: 4.9px.
        body = _body("anchorOrbit")
        head = body[:body.index("pickAt")]
        self.assertRegex(
            head, r"if\s*\(\s*S\.sel\.size\s*\)\s*return",
            "anchorOrbit must leave the orbit point alone while a selection "
            "owns it, before it goes looking for a new one")

    def test_empty_sky_leaves_the_orbit_point_alone(self):
        # A graph is a ball surrounded by empty sky, so the viewer's
        # ray-plane fallback anchors the orbit far OUTSIDE the graph and the
        # next drag swings the whole thing through an enormous arc. Measured:
        # three presses on empty sky walked the orbit point to 1,120 units
        # from a centre of radius 380 and inflated the camera distance from
        # 774 to 1,524, each press compounding the last.
        body = _body("anchorOrbit")
        self.assertNotIn(
            "intersectPlane", body,
            "anchorOrbit must not fall back to a point on a plane through the "
            "graph - off the graph that point is unbounded")
        self.assertEqual(
            1, body.count("setOrbitPoint"),
            "the only thing that may become the orbit point is a node")

    def test_the_only_anchor_is_a_picked_node(self):
        # Guards the two cases above against a future rewrite that keeps the
        # spellings but reaches setOrbitPoint by some other route.
        body = _body("anchorOrbit")
        anchor_line = next(ln for ln in body.splitlines()
                           if "setOrbitPoint" in ln)
        self.assertRegex(
            anchor_line, r"if\s*\(\s*p\s*\)",
            "setOrbitPoint must be reached only when pickAt returned a node")


class TheLoopSurvivesAnEarlyStart(unittest.TestCase):
    """setRunning(true) can beat setGraph(), and did.

    The IntersectionObserver starts the loop on its own schedule while
    atlasLoad() is still awaiting; camera and controls are built later, in
    setGraph(). Observed before the guard: frame() threw on controls.update()
    every frame and the graph stayed black behind a stage of console errors.
    """

    def test_frame_checks_controls_before_using_them(self):
        body = _body("frame")
        guard = re.search(r"if\s*\(\s*!controls\s*\|\|\s*!camera\s*\)\s*return",
                          body)
        self.assertIsNotNone(
            guard, "frame() must bail out before it touches controls/camera")
        first_use = re.search(r"\bcontrols\.", body)
        self.assertLess(
            guard.start(), first_use.start(),
            "the guard has to come before the first controls deref")

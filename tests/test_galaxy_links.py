"""The galaxy's links and gestures (2026-09-02, branch claude/galaxy-links).

Owner's rulings, as source contracts over the two renderers, the controls
markup and the side card:

  * a tie's colour BLENDS from one node's colour to the other's; a toggle
    paints every tie white; a slider scales every tie's opacity;
  * a PRESS is always the camera's - no node drag, no single-click select -
    and a DOUBLE-click is the only way to select a node;
  * the full document opens from the SIDE CARD (name + an Open button),
    never from the canvas.

Comments are stripped before scanning, so a rule surviving only as the
comment explaining it does not pass.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R3 = (ROOT / "static" / "atlas3d.js").read_text(encoding="utf-8")
FLAT = (ROOT / "static" / "atlas.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _strip(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


def _listener(src, event, capture):
    """The body of canvas.addEventListener(event, ...) in one phase."""
    tail = r"\}, true\);" if capture else r"\}\);"
    pat = (r'canvas\.addEventListener\("' + event + r'",(.*?)' + tail)
    hits = [m.group(1) for m in re.finditer(pat, _strip(src), flags=re.S)
            if ("}, true);" in m.group(0)) == capture]
    return hits


def _body(src, name):
    s = _strip(src)
    start = s.index(f"function {name}(")
    depth, i, opened = 0, s.index("{", start), False
    while i < len(s):
        if s[i] == "{":
            depth, opened = depth + 1, True
        elif s[i] == "}":
            depth -= 1
            if opened and depth == 0:
                break
        i += 1
    return s[start:i + 1]


class LinksBlendWhitenAndDim(unittest.TestCase):
    def test_the_controls_exist(self):
        self.assertIn('id="atlas-link-opacity"', HTML)
        self.assertIn('id="atlas-white-links"', HTML)

    def test_the_state_is_loaded_clamped_and_bound(self):
        flat = _strip(FLAT)
        self.assertIn("saved.display?.linkOpacity, 0, 3, 1", flat)
        self.assertIn("saved.display?.whiteLinks === true", flat)
        self.assertIn('"#atlas-link-opacity": S.display.linkOpacity * 100',
                      flat)
        self.assertIn('"#atlas-white-links": S.display.whiteLinks', flat)
        self.assertIn('$("#atlas-link-opacity")?.addEventListener("input"',
                      flat)
        self.assertIn('$("#atlas-white-links")?.addEventListener("change"',
                      flat)

    def test_the_3d_edges_blend_per_vertex_along_the_chord(self):
        body = _body(R3, "paintEdges")
        self.assertIn("nodeTint(A)", body)
        self.assertIn("nodeTint(B)", body)
        # the lerp parameter is the chord's own t, so a five-chord arc
        # blends along its length rather than flipping colour mid-way
        self.assertIn("(segment + (k < 2 ? 0 : 1)) / edgeSegments", body)
        self.assertIn("(tb.r - ta.r) * t", body)

    def test_the_3d_edges_honor_white_and_opacity(self):
        body = _body(R3, "paintEdges")
        self.assertIn("S.display.whiteLinks", body)
        self.assertIn("S.display.linkOpacity", body)
        self.assertIn("cr = cg = cb = 1", body)
        self.assertIn("st[3] * opacity", body)

    def test_the_flat_fallback_uses_the_same_three_rules(self):
        body = _body(FLAT, "flatEdgeStroke")
        self.assertIn("S.display.linkOpacity", body)
        self.assertIn("S.display.whiteLinks", body)
        self.assertIn("createLinearGradient", body)
        draw = _body(FLAT, "draw")
        self.assertIn("flatEdgeStroke(e.an, e.bn", draw)


class APressIsAlwaysTheCameras(unittest.TestCase):
    def test_the_3d_capture_press_only_anchors_the_orbit(self):
        downs = _listener(R3, "pointerdown", capture=True)
        self.assertEqual(len(downs), 1)
        body = downs[0]
        self.assertIn("anchorOrbit(x, y)", body)
        for forbidden in ("setPointerCapture", "onSelect", "controls.enabled",
                          "dragNode", "pin = true"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_the_3d_renderer_has_no_node_drag(self):
        s = _strip(R3)
        for gone in ("pointOnDragPlane", "drag3", "finishNodeDrag",
                     "S.dragNode = p"):
            self.assertNotIn(gone, s, gone)

    def test_a_single_click_never_selects_in_3d(self):
        ups = _listener(R3, "pointerup", capture=False)
        self.assertEqual(len(ups), 1)
        self.assertNotIn("onSelect", ups[0])
        self.assertIn("host.onEmpty()", ups[0])

    def test_double_click_is_what_selects_in_3d(self):
        dbl = _listener(R3, "dblclick", capture=False)
        self.assertEqual(len(dbl), 1)
        self.assertIn("host.onSelect(p)", dbl[0])
        self.assertNotIn("onOpen", dbl[0])

    def test_the_flat_fallback_matches(self):
        s = _strip(FLAT)
        self.assertNotIn("S.dragNode = p", s)
        dbl = _listener(FLAT, "dblclick", capture=False)
        self.assertEqual(len(dbl), 1)
        self.assertIn("hitSelect(p)", dbl[0])
        ups = _listener(FLAT, "pointerup", capture=False)
        self.assertEqual(len(ups), 1)
        self.assertNotIn("hitSelect", ups[0])

    def test_the_hint_copy_matches_the_gesture(self):
        self.assertIn("Double-click a node to select it", HTML)
        self.assertNotIn("Drag a node to pull", HTML)


class TheSideCardOpensTheDocument(unittest.TestCase):
    def test_the_name_and_an_open_button_both_open_the_node(self):
        body = _body(FLAT, "renderCard")
        self.assertIn('nm.addEventListener("click", () => openWorldNode(d.node))',
                      body)
        self.assertIn('openBtn.addEventListener("click", () => openWorldNode(d.node))',
                      body)
        self.assertIn('"fchip sm atlas-open"', body)

    def test_the_open_label_names_what_opens(self):
        body = _body(FLAT, "openLabel")
        self.assertIn('"Open profile"', body)
        self.assertIn('"Open note"', body)


if __name__ == "__main__":
    unittest.main()

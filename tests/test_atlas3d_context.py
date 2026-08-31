"""A name is never drawn over a canvas nothing is drawing.

The Visual Network's circles are WebGL and its name cards are DOM, so the
two fail apart. A browser may take the graphics context away at any time -
GPU memory pressure, waking from sleep, or the case this app invites, more
live WebGL contexts than a tab is allowed (this graph, the Image Atlas
viewer and the Flows board can all be open at once, and Chrome evicts the
least recently used WITHOUT telling the page).

`renderer.render()` then does nothing at all: no throw, no console error.
The canvas holds its last frame while the card pass keeps re-projecting
every name against a camera that is still drifting, so within seconds each
name has walked off the circle it belongs to. Measured on the real graph:
180-361px after six seconds of idle auto-orbit, which is precisely the
"names floating in the air" this module's whole label contract forbids.

Held as a SOURCE contract, the way test_atlas3d_nav.py holds the camera:
a Python suite cannot run the renderer, but it can pin that the handlers
exist, that they do the four things that matter, and that the class the
script sets is the class the stylesheet actually styles.

Mutation-checked when written: dropping the preventDefault, the label
drop, the paintLabels guard, the `false` on the restore's clearScene, or
the CSS rule each fails at least one case.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "static" / "atlas3d.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

LOST_CLASS = "atlas-lost"


def _strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"^\s*//.*$", "", s, flags=re.M)


def _listener(event):
    """The body of the canvas listener for `event`, comments stripped.

    Comments are stripped because each guard below is explained by a
    comment directly above it, and a scan matching the explanation would
    pass against a file that had lost the code.
    """
    start = SRC.index(f'canvas.addEventListener("{event}"')
    depth, i, opened = 0, start, False
    while i < len(SRC):
        if SRC[i] == "{":
            depth, opened = depth + 1, True
        elif SRC[i] == "}":
            depth -= 1
            if opened and depth == 0:
                return _strip_comments(SRC[start:i + 1])
        i += 1
    raise AssertionError(f"unbalanced listener for {event}")


def _body(name):
    start = SRC.index(f"function {name}(")
    depth, i, opened = 0, SRC.index("{", start), False
    while i < len(SRC):
        if SRC[i] == "{":
            depth, opened = depth + 1, True
        elif SRC[i] == "}":
            depth -= 1
            if opened and depth == 0:
                return _strip_comments(SRC[start:i + 1])
        i += 1
    raise AssertionError(f"unbalanced function {name}")


class TheLossIsHandled(unittest.TestCase):
    def test_the_module_listens_for_the_loss_at_all(self):
        self.assertIn('canvas.addEventListener("webglcontextlost"', SRC)

    def test_it_prevents_default_so_the_context_can_come_back(self):
        # without preventDefault the browser never fires contextrestored and
        # the view is dead until the window is rebuilt by hand
        self.assertIn("preventDefault()", _listener("webglcontextlost"))

    def test_it_stops_the_loop_and_drops_every_name(self):
        body = _listener("webglcontextlost")
        self.assertIn("setRunning(false)", body)
        self.assertIn("dropLabels()", body,
                      "the circles are gone, so their names must go too")

    def test_it_marks_the_stage_so_the_view_says_what_happened(self):
        self.assertIn(LOST_CLASS, _listener("webglcontextlost"))

    def test_drop_labels_really_empties_the_registry(self):
        body = _body("dropLabels")
        self.assertIn("labels.clear()", body)
        self.assertIn("lastCards = []", body,
                      "cards() must not report a layout that is gone")


class TheCardPassRefusesToPaintOverADeadCanvas(unittest.TestCase):
    def test_paint_labels_returns_early_when_the_context_is_gone(self):
        body = _body("paintLabels")
        head = body[:body.index("syncCamera()")]
        self.assertIn("if (contextLost) return;", head,
                      "the guard has to run BEFORE any card is laid out")


class TheRestoreRebuildsWithoutDeletingDeadObjects(unittest.TestCase):
    def test_the_module_listens_for_the_restore(self):
        self.assertIn('canvas.addEventListener("webglcontextrestored"', SRC)

    def test_it_clears_the_lost_mark_and_resumes(self):
        body = _listener("webglcontextrestored")
        self.assertIn(LOST_CLASS, body)
        self.assertIn("setRunning(true)", body)

    def test_it_does_not_ask_the_new_context_to_delete_the_old_ones(self):
        # those objects died WITH the context; deleting them is an
        # INVALID_OPERATION per object (184 console warnings, measured)
        body = _listener("webglcontextrestored")
        self.assertIn("clearScene(false)", body)
        self.assertIn("stars = null", body,
                      "buildStars disposes the old field on the way in")

    def test_clear_scene_can_be_told_not_to_free(self):
        body = _body("clearScene")
        self.assertRegex(body, r"function clearScene\(free = true\)")
        self.assertIn("if (free)", body)

    def test_the_restore_keeps_the_layout_and_the_camera(self):
        # re-seeding would scatter the graph and rebuilding the camera would
        # throw away where the owner was looking
        body = _listener("webglcontextrestored")
        self.assertNotIn("seed()", body)
        self.assertNotIn("buildCamera()", body)


class TheStylesheetStylesTheClassTheScriptSets(unittest.TestCase):
    def test_the_lost_state_is_visible(self):
        self.assertIn(f".atlas-stage.{LOST_CLASS}", CSS,
                      "the script marks the stage; the stylesheet must "
                      "actually say something, or the mark is invisible")

    def test_it_says_what_happened_rather_than_only_dimming(self):
        rule = CSS[CSS.index(f".atlas-stage.{LOST_CLASS}::after"):]
        self.assertIn("content:", rule[:400])


class ProjectionUsesTheCameraAsItIsNow(unittest.TestCase):
    """camera-controls' update() moves the camera but only refreshes
    matrixWorldInverse when a focal offset is set, which this module never
    uses - and .project() reads exactly that matrix. Anything projected
    before renderer.render() is otherwise a frame behind what it draws."""

    def test_the_card_pass_syncs_first(self):
        self.assertIn("syncCamera()", _body("paintLabels"))

    def test_picking_syncs_too(self):
        # the same staleness lands a click on the node a moving graph left
        self.assertIn("syncCamera()", _body("pickAt"))

    def test_sync_camera_actually_updates_the_matrix(self):
        self.assertIn("camera.updateMatrixWorld()", _body("syncCamera"))


if __name__ == "__main__":
    unittest.main()

"""The flow-trace source contracts.

The suite cannot execute the frontend, so the joins that would otherwise
drift silently are pinned as SOURCE contracts (the drag-handle /
reveal-observer discipline): the stage-status vocabulary must be ONE
vocabulary across server/circuits.py, the client's copy in static/app.js,
and the stylesheet rules that tone a dot or a board card — and the trace
entry point app.js calls must actually be exposed by forge.js (the
reader-with-no-writer shape, which shipped dark twice before).

Mutation-checked when written: removing a status from the app.js table,
deleting a `.ss-dot.is-*` rule, deleting a `.forge-node.run-*` rule, or
dropping `traceRun` off the Forge export each fails exactly one case.
"""
import re
import unittest
from pathlib import Path

from server import circuits

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
FORGE_JS = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
FORGE_CSS = (ROOT / "static" / "forge.css").read_text(encoding="utf-8")


def _app_stage_statuses():
    m = re.search(r"const STAGE_STATUSES = \[([^\]]+)\]", APP_JS)
    if not m:
        return set()
    return set(re.findall(r'"(\w+)"', m.group(1)))


class StatusVocabulary(unittest.TestCase):

    def test_circuits_declares_a_nonempty_vocabulary(self):
        # The reach pin for everything below: a scan or a parse that
        # silently matched nothing must not pass the file vacuously.
        self.assertGreaterEqual(len(circuits.STAGE_STATUSES), 5)
        self.assertIn("running", circuits.STAGE_STATUSES)
        self.assertIn("done", circuits.STAGE_STATUSES)

    def test_every_stage_status_the_driver_writes_is_declared(self):
        # Scan circuits.py for status literals written into stage or run
        # state. "canceled" is the one RUN-level literal the scan also
        # sees (a canceled run's unfinished stages read "skipped"); a run
        # is also "running"/"done"/"error", which share stage spellings.
        src = (ROOT / "server" / "circuits.py").read_text(encoding="utf-8")
        found = set(re.findall(r'["\']status["\']\s*[:\]]+\s*=?\s*["\'](\w+)["\']',
                               src))
        self.assertTrue(found >= {"pending", "running", "waiting",
                                  "skipped"},
                        f"the scan lost its reach — found only {found}")
        stray = found - set(circuits.STAGE_STATUSES) - {"canceled"}
        self.assertFalse(
            stray,
            f"circuits.py writes status literal(s) {stray} that "
            f"STAGE_STATUSES does not declare — declare them AND give the "
            f"client table + stylesheets their rules")

    def test_the_client_copy_matches_the_server_vocabulary(self):
        client = _app_stage_statuses()
        self.assertTrue(client, "app.js no longer carries STAGE_STATUSES — "
                        "the strip has lost its vocabulary")
        self.assertEqual(client, set(circuits.STAGE_STATUSES))

    def test_every_status_has_a_strip_dot_rule(self):
        for status in circuits.STAGE_STATUSES:
            self.assertIn(f".ss-dot.is-{status}", STYLE_CSS,
                          f"style.css has no .ss-dot.is-{status} rule — "
                          f"a {status} stage would render as an unstyled dot")

    def test_every_status_has_a_board_overlay_rule(self):
        for status in circuits.STAGE_STATUSES:
            self.assertIn(f".forge-node.run-{status}", FORGE_CSS,
                          f"forge.css has no .forge-node.run-{status} rule — "
                          f"a traced {status} stage would wear no state")


class TraceWiring(unittest.TestCase):
    """app.js calls Forge.traceRun; forge.js must export it — and the two
    strip surfaces must route through the one trace gesture."""

    def test_forge_exports_the_trace_entry_point(self):
        m = re.search(r"window\.Forge = \{([^}]+)\}", FORGE_JS)
        self.assertIsNotNone(m, "forge.js no longer exports window.Forge")
        self.assertIn("traceRun", m.group(1))

    def test_app_calls_the_entry_point_it_expects(self):
        self.assertIn("window.Forge?.traceRun?.(", APP_JS)

    def test_both_strip_surfaces_trace(self):
        # The strip IS the trace affordance: the shared element helper
        # routes its click through traceFlowRun, and the attention verb
        # for a flow row does the same — never a second implementation.
        self.assertIn("traceFlowRun(runId)",
                      APP_JS[APP_JS.index("function stageStripEl"):])
        verb = APP_JS[APP_JS.index("function attnVerb"):]
        self.assertIn('traceFlowRun(r.run_id)', verb[:600])

    def test_the_run_overlay_respects_reduced_motion(self):
        # The pulse is animation-only; the state stays readable statically.
        reduced = FORGE_CSS[FORGE_CSS.rindex("prefers-reduced-motion"):]
        self.assertIn(".forge-wire.is-run-live", reduced)
        reduced_app = STYLE_CSS[STYLE_CSS.index("ssPulse"):]
        self.assertIn("prefers-reduced-motion", reduced_app)


if __name__ == "__main__":
    unittest.main()

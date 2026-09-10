"""Approve & build is ONE Implement session on its own branch.

Until 2026-09-10 the Queue's "Approve & build" started the plan-build-judge
Flow. Traced on the owner's ask ("confirm it branches and lands in the
Showroom, not a merge"): the Build step WAS placed on a branch and nothing
merged, but the judge diffed the run's cwd - the live checkout - rather
than the build's worktree, and a judge-triggered retry relaunched through
session.launch and so minted a SECOND worktree from main. Owner's ruling:
until Flows are fixed, just build it - one session, on a branch, reviewed
in the Showroom with a test instance a click away, and if Vira already
wrote a plan for the idea the build uses it.

Three contracts, each held at the join it belongs to:
- the ROUTE only approves; a `build` flag is refused by name, never
  silently ignored (the branch.sh serve-flags rule) - a real TestClient
  against the real app, with the store rooted at tmp and the Flow launcher
  patched so a regression would be a recorded call, not a real run;
- the BUTTON dispatches through the same function the Implement sheet
  uses, with no repo prompt and no Flow (a comment-stripped scan of the
  approval bar, since the renderer cannot run here);
- the HELPERS behave (tests/approve_builds_ui.js runs the real functions
  under node): the newest plan link wins, the build prompt carries the plan
  between the idea and the owner's extra instructions, a missing plan file
  degrades to the idea alone and says so, a planning pass is never fed the
  plan it replaces, and the image paths still ride the build.

Mutation-checked when written: dropping ideaPlanBlock from ideaTaskLines,
restoring `build: true` on the button, and removing the route's refusal
each fail at least one case.
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import circuits, ideas, ideatags, main

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def _strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"^\s*//.*$", "", s, flags=re.M)


def _block(marker):
    """Balanced body starting at `marker`, comments stripped (the
    test_omni_palette scanner: {} and [] counted together)."""
    start = SRC.index(marker)
    depth, i, opened = 0, start, False
    while i < len(SRC):
        if SRC[i] in "{[":
            depth, opened = depth + 1, True
        elif SRC[i] in "}]":
            depth -= 1
            if opened and depth == 0:
                return _strip_comments(SRC[start:i + 1])
        i += 1
    raise AssertionError(f"unbalanced block at {marker!r}")


class TheRouteOnlyApproves(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        store = root / "ideas.json"
        store.write_text(json.dumps({"items": [], "projects": []}),
                         encoding="utf-8")
        for p in (mock.patch.object(ideas, "STORE", store),
                  mock.patch.object(ideatags, "STORE", root / "idea-index.json")):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def status_of(self, idea_id):
        return next(i for i in ideas.list_items() if i["id"] == idea_id)["status"]

    def test_approve_flips_proposed_to_open_and_starts_nothing(self):
        it = ideas.add("a proposal", status="proposed")
        with mock.patch.object(circuits, "start_run") as run:
            r = self.client.post(f"/api/ideas/{it['id']}/approve", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["idea"]["status"], "open")
        self.assertNotIn("run", r.json())
        run.assert_not_called()

    def test_no_body_at_all_still_approves(self):
        it = ideas.add("a proposal", status="proposed")
        r = self.client.post(f"/api/ideas/{it['id']}/approve")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.status_of(it["id"]), "open")

    def test_a_build_flag_is_refused_by_name_before_anything_changes(self):
        it = ideas.add("a proposal", status="proposed")
        with mock.patch.object(circuits, "start_run") as run:
            r = self.client.post(f"/api/ideas/{it['id']}/approve",
                                 json={"build": True})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("/api/actions/run", r.json()["detail"])
        run.assert_not_called()
        self.assertEqual(self.status_of(it["id"]), "proposed",
                         "a refused request must not half-apply")

    def test_an_unknown_idea_is_404(self):
        r = self.client.post("/api/ideas/idea_nope/approve", json={})
        self.assertEqual(r.status_code, 404)


class TheButtonIsOneImplementSession(unittest.TestCase):
    """The approval bar, read the only way a Python suite can read it."""

    MARK = 'if (it.status === "proposed")'

    def setUp(self):
        # reach guard: a scan over a missing bar passes every negative below
        self.assertIn(self.MARK, SRC)
        self.bar = _block(self.MARK)
        self.assertIn('"Approve & build"', self.bar)

    def test_it_dispatches_through_the_shared_implement_path(self):
        self.assertIn('dispatchIdeaRun(it, "implement"', self.bar)

    def test_it_never_starts_a_flow_or_asks_for_a_repo(self):
        for bad in ("build: true", 'openApp("circuits")', "loadCircuits",
                    "prompt(", "start_run"):
            self.assertNotIn(bad, self.bar, bad)

    def test_both_buttons_approve_with_no_build_flag(self):
        self.assertEqual(self.bar.count("/approve`, {})"), 2)

    def test_the_bar_and_the_sheet_share_one_cwd_ladder(self):
        self.assertIn("cwd: ideaRunCwd(it)", self.bar)
        self.assertIn("ideaRunCwd(it)", _block("function openIdeaRun("))

    def test_the_build_carries_the_plan_and_the_plan_pass_does_not(self):
        d = _block("async function dispatchIdeaRun(")
        self.assertIn('mode === "plan" ? null : await ideaPlanFor(it)', d)
        self.assertIn("ideaPlanBlock(plan)", _block("function ideaTaskLines("))
        self.assertIn("ideaTaskLines(it, extra, fold, plan)",
                      _block("function ideaImplementPrompt("))

    def test_the_dispatch_is_its_own_review(self):
        # The sheet reviewed the settings; the bar is the owner's explicit
        # consent. Neither routes through the launch-review sheet, which is
        # for a prompt that had no surface of its own.
        self.assertIn("reviewed: true", _block("async function dispatchIdeaRun("))


class TheHelpersRunUnderNode(unittest.TestCase):
    def test_prompt_and_dispatch_behavior_in_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        result = subprocess.run([node, "tests/approve_builds_ui.js"], cwd=ROOT,
                                capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("passed", result.stdout)


if __name__ == "__main__":
    unittest.main()

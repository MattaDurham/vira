"""The landing card: the HARNESS asks merge / keep playing / discard.

Owner, 2026-09-02: a coding session that ends on the prose question "merge
it, spin up a test instance, or discard?" is exactly how a branch drifts
into the orphan sweeper, and a test instance is not an ANSWER - it is
served before the question. So runner.offer_landing serves the branch
(branch.sh serve --local), opens the required draft PR when the session
did not, and raises a decision card; session.answer routes the verdict; and
orphanwork.land_session acts on it after the session ends.

Run: .venv/bin/python -m unittest tests.test_landing_card
"""
import asyncio
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import jobfiles, orphanwork, session, viratools
from server import runner as runner_mod
from tests.test_orphanwork import _BranchShCase
from tests.test_runner import RunnerCase
from tests.test_session import make_detached, make_registry

ROOT = Path(__file__).resolve().parent.parent

FAKE_BRANCH_SH = '''#!/bin/sh
echo "$*" >> "$(dirname "$0")/../calls.log"
case "$1" in
  serve) echo "test instance up:  http://localhost:8391  (passive, LOCAL ONLY)";;
  pr)    echo "https://github.com/example/vira/pull/77";;
esac
exit 0
'''


class _Placed(RunnerCase):
    """A runner whose spec says branch-first placed it, over a fake live
    root carrying a stand-in scripts/branch.sh that records its argv."""

    def setUp(self):
        super().setUp()
        self.live = Path(self.tmp.name) / "live"
        (self.live / "scripts").mkdir(parents=True)
        sh = self.live / "scripts" / "branch.sh"
        sh.write_text(FAKE_BRANCH_SH, encoding="utf-8")
        sh.chmod(0o755)
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir()

    def placed(self, **over):
        over.setdefault("worktree", str(self.wt))
        over.setdefault("branch", "claude/t")
        over.setdefault("live_root", str(self.live))
        over.setdefault("cwd", str(self.wt))
        return self.make_runner(**over)

    def calls(self):
        p = self.live / "calls.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def offer(self, r, work=(2, 1), pr=""):
        with mock.patch.object(r, "_branch_work", lambda: work), \
             mock.patch.object(r, "_pr_url", lambda: pr):
            return asyncio.run(r.offer_landing())

    def card(self, r):
        cards = [p for p in r.state["pending"] if p.get("kind") == "landing"]
        return cards[0] if cards else None


class Eligibility(_Placed):
    def test_a_placed_owner_session_is_eligible(self):
        self.assertTrue(self.placed().landing_eligible())

    def test_an_unplaced_session_is_not(self):
        self.assertFalse(self.make_runner().landing_eligible())

    def test_a_plan_session_is_not(self):
        self.assertFalse(self.placed(publish_plan=True).landing_eligible())

    def test_a_machine_dispatch_is_not(self):
        for meta in ({"machine": True}, {"routine_id": "r"},
                     {"circuit_run": "c"}, {"judge_of": "j"}):
            self.assertFalse(self.placed(meta=meta).landing_eligible(), meta)

    def test_a_showroom_candidate_is_not(self):
        # the Showroom is that branch's own verdict surface
        self.assertFalse(
            self.placed(meta={"showroom_idea": "idea_1"}).landing_eligible())

    def test_the_config_switch_is_read_off_the_spec(self):
        self.assertFalse(self.placed(landing_card=False).landing_eligible())
        self.assertTrue(self.placed(landing_card=True).landing_eligible())


class TheCard(_Placed):
    def test_no_card_over_nothing(self):
        """A session that read and reported has nothing to land; a card
        offering to merge nothing is what would teach the owner to dismiss
        these."""
        r = self.placed()
        self.assertTrue(self.offer(r, work=(0, 0)))
        self.assertIsNone(self.card(r))
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(os.name == "posix",
                         "drives a stand-in scripts/branch.sh - a shell script "
                         "Windows CreateProcess cannot run (WinError 193), and "
                         "branch.sh is Mac-side tooling a Windows install never "
                         "runs; the card's own logic is covered by the other cases")
    def test_the_card_carries_the_served_instance_and_the_pr(self):
        r = self.placed()
        self.assertTrue(self.offer(r, work=(2, 1)))
        c = self.card(r)
        self.assertIsNotNone(c)
        self.assertEqual(c["test_url"], "http://localhost:8391")
        self.assertEqual(c["pr_url"], "https://github.com/example/vira/pull/77")
        self.assertEqual(c["branch"], "claude/t")
        self.assertEqual((c["dirty"], c["ahead"]), (2, 1))
        self.assertEqual([o["label"] for o in c["options"]],
                         ["Merge it", "Keep playing", "Discard"])
        self.assertFalse(c["allow_text"])
        # the serve is LOCAL ONLY - a personal snapshot is never auto-bridged
        self.assertIn("serve t --local", self.calls())
        self.assertIn("pr t", self.calls())
        out = self.output(r)
        self.assertIn("http://localhost:8391", out)
        self.assertIn("pull/77", out)
        # and the state the surfaces poll carries it
        self.assertEqual(self.state(r)["pending"][0]["kind"], "landing")

    def test_an_existing_pr_is_not_reopened(self):
        r = self.placed()
        self.offer(r, pr="https://github.com/example/vira/pull/5")
        self.assertNotIn("pr t", self.calls())
        self.assertEqual(self.card(r)["pr_url"],
                         "https://github.com/example/vira/pull/5")

    def test_auto_serve_off_still_raises_the_card(self):
        r = self.placed(auto_serve=False)
        self.offer(r)
        self.assertNotIn("serve t --local", self.calls())
        c = self.card(r)
        self.assertIsNotNone(c)
        self.assertEqual(c["test_url"], "")

    @unittest.skipUnless(os.name == "posix",
                         "drives a stand-in scripts/branch.sh - a shell script "
                         "Windows CreateProcess cannot run (WinError 193), and "
                         "branch.sh is Mac-side tooling a Windows install never "
                         "runs; the card's own logic is covered by the other cases")
    def test_a_dead_branch_sh_is_a_note_on_the_card_never_no_card(self):
        (self.live / "scripts" / "branch.sh").write_text(
            "#!/bin/sh\necho 'error: no free port' >&2\nexit 1\n",
            encoding="utf-8")
        r = self.placed()
        self.assertTrue(self.offer(r))
        c = self.card(r)
        self.assertIsNotNone(c)
        self.assertEqual(c["test_url"], "")
        self.assertIn("no free port", c["serve_note"])
        self.assertIn("no PR", c["pr_note"])

    def test_an_interrupted_turn_gets_no_card(self):
        # a Stop-paused turn is not delivered work
        r = self.placed()
        r.interrupted = True
        self.assertTrue(self.offer(r))
        self.assertIsNone(self.card(r))

    def test_branch_work_reads_git_not_memory(self):
        """One real repo: a branch one commit ahead with one dirty path in
        its worktree reads (1, 1); main itself reads (0, 0)."""
        live = Path(self.tmp.name) / "repo"
        live.mkdir()
        g = lambda *a, cwd=live: subprocess.run(  # noqa: E731
            ["git", "-C", str(cwd), *a], check=True, capture_output=True,
            text=True)
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
        (live / "a.txt").write_text("a", encoding="utf-8")
        g("add", "."); g("commit", "-q", "-m", "base")
        wt = Path(self.tmp.name) / "repo-wt"
        g("worktree", "add", "-q", "-b", "claude/t", str(wt))
        (wt / "b.txt").write_text("b", encoding="utf-8")
        g("add", ".", cwd=wt); g("commit", "-q", "-m", "work", cwd=wt)
        (wt / "c.txt").write_text("c", encoding="utf-8")
        r = self.placed(worktree=str(wt), live_root=str(live))
        self.assertEqual(r._branch_work(), (1, 1))
        r2 = self.placed(worktree=str(live), live_root=str(live), branch="main")
        self.assertEqual(r2._branch_work(), (0, 0))


class TheVerdict(_Placed):
    def landing(self, r, verdict, work=(0, 1)):
        with mock.patch.object(r, "_branch_work", lambda: work):
            asyncio.run(r.handle_landing({"op": "landing", "verdict": verdict}))

    def raised(self, r, work=(2, 1)):
        self.offer(r, work=work)
        r.awaiting_reply = True          # parked, as the loop would have it
        return r

    def test_keep_drops_the_card_and_stays_parked(self):
        r = self.raised(self.placed())
        self.landing(r, "keep")
        self.assertIsNone(self.card(r))
        self.assertTrue(r.inbox.empty())
        self.assertIsNone(r.landing)
        self.assertIn("keeping the branch", self.output(r))

    def test_a_reply_under_the_card_is_keep(self):
        r = self.raised(self.placed())
        asyncio.run(r.handle({"op": "say", "text": "also rename the button"}))
        self.assertIsNone(self.card(r))
        self.assertEqual(r.inbox.get_nowait(), "also rename the button")

    def test_discard_ends_the_session_by_the_owner(self):
        r = self.raised(self.placed())
        self.landing(r, "discard")
        self.assertEqual(r.landing, {"verdict": "discard"})
        self.assertTrue(r.finished_by_owner)
        self.assertIs(r.inbox.get_nowait(), runner_mod._END)
        self.assertEqual(self.state(r)["landing"], "discard")
        self.assertIsNone(self.card(r))

    def test_merge_over_a_clean_tree_ends_the_session(self):
        r = self.raised(self.placed(), work=(0, 1))
        self.landing(r, "merge", work=(0, 1))
        self.assertEqual(r.landing, {"verdict": "merge"})
        self.assertTrue(r.finished_by_owner)
        self.assertIs(r.inbox.get_nowait(), runner_mod._END)

    def test_merge_over_a_dirty_tree_steers_a_commit_then_finishes(self):
        """The Implement prompt says never commit, so delivered work is
        usually uncommitted - and branch.sh merge refuses a dirty tree. The
        session that wrote it commits it; the harness never invents the
        message. The verdict is HELD so the commit turn finishes instead of
        raising the card again."""
        r = self.raised(self.placed(), work=(2, 0))
        self.landing(r, "merge", work=(2, 0))
        self.assertEqual(r.inbox.get_nowait(), runner_mod.COMMIT_STEER)
        self.assertEqual(r.landing, {"verdict": "merge"})
        self.assertFalse(r.finished_by_owner)   # not yet - the turn runs
        # ...the commit turn ends and parks again:
        with mock.patch.object(r, "_branch_work", lambda: (0, 1)):
            park = asyncio.run(r.offer_landing())
        self.assertFalse(park)                  # finish, do not park
        self.assertTrue(r.finished_by_owner)
        self.assertTrue(r.finished_cleanly)
        self.assertIsNone(self.card(r))
        self.assertIn("verdict on file: merge", self.output(r))

    def test_an_unknown_verdict_is_ignored(self):
        r = self.raised(self.placed())
        self.landing(r, "yolo")
        self.assertIsNotNone(self.card(r))
        self.assertIsNone(r.landing)
        self.assertTrue(r.inbox.empty())

    def test_deny_pending_tolerates_a_landing_card(self):
        # no future backs a landing card; ending the session must not trip
        r = self.raised(self.placed())
        r.deny_pending("session ended")


class TheParkSitesCallIt(unittest.TestCase):
    """Both engines park through offer_landing - the JOIN, pinned as a
    source contract because the SDK loop needs a real client to drive. A
    card a runner never offers is the reader-with-no-writer shape."""

    def _body(self, rel):
        src = (ROOT / rel).read_text(encoding="utf-8")
        return re.sub(r"(?m)^\s*#.*$", "", src)

    def test_the_sdk_loop_offers_before_parking(self):
        s = self._body("server/runner.py")
        i = s.index("park = self.should_park(ok)")
        self.assertIn("park = await self.offer_landing()", s[i:i + 200])
        self.assertIn("reply = await self.await_reply() if park else None",
                      s[i:i + 300])

    def test_the_provider_loop_offers_before_parking(self):
        s = self._body("server/agentbackend.py")
        i = s.index("park = ok and runner.parks_at_turn_end()")
        self.assertIn("park = await runner.offer_landing()", s[i:i + 200])
        self.assertIn("reply = await runner.await_reply() if park else None",
                      s[i:i + 300])


class TheAnswerRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.acted = []

    def _card(self):
        return {"req_id": "L1", "kind": "landing", "branch": "claude/t",
                "question": "claude/t is ready - what happens to it?",
                "options": runner_mod.LANDING_OPTIONS, "allow_text": False,
                "summary": "", "created": 1.0}

    def _reg(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, pending=[self._card()])
        return reg, h

    def test_merge_writes_the_verdict_and_starts_the_landing(self):
        reg, h = self._reg()
        with mock.patch.object(orphanwork, "land_session",
                               lambda *a: self.acted.append(a)):
            v = reg.answer(h.id, "L1", "Merge it")
        self.assertEqual(v, "merge")
        _, cmds = jobfiles.read_control(h.dir, 0)
        self.assertEqual(cmds[-1], {"op": "landing", "req_id": "L1",
                                    "verdict": "merge"})
        self.assertEqual(self.acted, [(h.id, "claude/t", "merge")])

    def test_keep_writes_the_verdict_and_acts_on_nothing(self):
        reg, h = self._reg()
        with mock.patch.object(orphanwork, "land_session",
                               lambda *a: self.acted.append(a)):
            v = reg.answer(h.id, "L1", "Keep playing")
        self.assertEqual(v, "keep")
        _, cmds = jobfiles.read_control(h.dir, 0)
        self.assertEqual(cmds[-1]["verdict"], "keep")
        self.assertEqual(self.acted, [])

    def test_discard_and_a_typed_number(self):
        reg, h = self._reg()
        with mock.patch.object(orphanwork, "land_session",
                               lambda *a: self.acted.append(a)):
            self.assertEqual(reg.answer(h.id, "L1", "Discard"), "discard")
            self.assertEqual(reg.answer(h.id, "L1", "1"), "merge")
        self.assertEqual([a[2] for a in self.acted], ["discard", "merge"])

    def test_anything_unrecognised_reads_as_keep(self):
        # a mis-parsed answer must never merge or delete a branch
        for t in ("maybe later", "The owner skipped this question. Do NOT guess",
                  "", "2"):
            self.assertEqual(session.landing_verdict(t), "keep", t)

    def test_an_ask_card_still_answers_the_model(self):
        reg = make_registry()
        h = make_detached(reg, self.tmp.name, pending=[
            {"req_id": "q1", "kind": "ask", "question": "Fold or keep?",
             "options": ["Fold", "Keep"], "summary": "", "created": 1.0}])
        self.assertIsNone(reg.answer(h.id, "q1", "Fold"))
        _, cmds = jobfiles.read_control(h.dir, 0)
        self.assertEqual(cmds[-1]["op"], "answer")


class LandSession(_BranchShCase):
    """orphanwork.land_session waits for the SESSION to end, then acts."""

    def setUp(self):
        super().setUp()
        fast = mock.patch.object(orphanwork, "SESSION_END_POLL_S", 0.01)
        fast.start()
        self.addCleanup(fast.stop)

    def _rows(self, running_polls, worktree):
        seen = {"n": 0}
        def job_row(jid):
            seen["n"] += 1
            st = "running" if seen["n"] <= running_polls else "done"
            return {"id": jid, "status": st, "branch": "claude/t",
                    "worktree": str(worktree), "cwd": str(worktree)}
        return job_row, seen

    def test_merge_waits_for_the_session_to_end(self):
        wt = self.root
        job_row, seen = self._rows(3, wt)
        merged = []
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_dirty_lines", lambda p: []), \
             mock.patch.object(orphanwork, "_ahead_behind", lambda b: (1, 0)), \
             mock.patch.object(orphanwork, "_merge_sync",
                               lambda slug: (merged.append((slug, seen["n"]))
                                             or (True, "merged"))), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            self.assertTrue(orphanwork.land_session("j" * 12, "claude/t", "merge"))
            a = self._wait("claude/t")
        self.assertEqual(a["name"], "land")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0], "t")
        # the merge ran AFTER the row left running, never while it was live
        self.assertGreater(merged[0][1], 3)

    def test_a_session_that_ends_dirty_merges_nothing(self):
        job_row, _ = self._rows(0, self.root)
        merged = []
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_dirty_lines",
                               lambda p: ["?? x.py"]), \
             mock.patch.object(orphanwork, "_merge_sync",
                               lambda slug: merged.append(slug)), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session("j" * 12, "claude/t", "merge")
            a = self._wait("claude/t")
        self.assertEqual(a["status"], "failed")
        self.assertIn("uncommitted", a["output"])
        self.assertEqual(merged, [])

    def test_discard_runs_branch_sh_discard_after_the_end(self):
        job_row, _ = self._rows(2, self.root)
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session("j" * 12, "claude/t", "discard")
            a = self._wait("claude/t")
        self.assertEqual(a["name"], "discard")
        self.assertEqual(a["status"], "ok")
        self.assertIn("branch.sh discard t", a["output"])

    def test_refusals_are_named(self):
        with self.assertRaises(ValueError):
            orphanwork.land_session("j" * 12, "main", "merge")
        with self.assertRaises(ValueError):
            orphanwork.land_session("j" * 12, "claude/t", "keep")
        orphanwork._set_action("claude/t", "land", "running", "busy")
        with self.assertRaises(ValueError):
            orphanwork.land_session("j" * 12, "claude/t", "merge")


class ThePreamble(unittest.TestCase):
    def test_a_placed_session_is_told_not_to_ask_the_landing_question(self):
        kw = dict(worktree_path="/tmp/wt", branch="claude/x",
                  live_root="/tmp/live")
        for native in (True, False):
            p = viratools.preamble(native, **kw)
            self.assertIn("VIRA HANDLES THE LANDING", p)
            self.assertIn("Do NOT ask whether to merge, test or discard", p)
        self.assertNotIn("VIRA HANDLES THE LANDING", viratools.preamble(True))


class TheSurfaces(unittest.TestCase):
    """Every pending-card surface renders a landing card as a picker. The
    fork is ONE predicate; a raw `kind === "ask" ? askCard` left anywhere
    would render the landing card as a permission card on that surface."""

    def setUp(self):
        self.js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_no_surface_forks_on_ask_by_hand(self):
        self.assertNotIn('p.kind === "ask" ? askCard', self.js)
        self.assertGreaterEqual(self.js.count("isChoiceCard(p)"), 4)

    def test_the_landing_card_shows_the_instance_and_the_pr(self):
        i = self.js.index("function landingCard(sid, p)")
        body = self.js[i:i + 2500]
        for key in ("p.test_url", "p.pr_url", "p.serve_note", "p.pr_note",
                    "askCard(sid, p)"):
            self.assertIn(key, body)

    def test_the_landing_card_has_no_skip(self):
        self.assertIn('if (p.kind !== "landing") actions.appendChild(skip);',
                      self.js)

    def test_attention_names_the_card(self):
        src = (ROOT / "server" / "attention.py").read_text(encoding="utf-8")
        self.assertIn('"ready to land: "', src)

    def test_config_carries_both_switches(self):
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertTrue(cfg["session_landing_card"])
        self.assertTrue(cfg["session_auto_serve"])
        self.assertTrue(session.SESSION_DEFAULTS["session_landing_card"])
        self.assertTrue(session.SESSION_DEFAULTS["session_auto_serve"])


if __name__ == "__main__":
    unittest.main()

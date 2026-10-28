"""The landing card: the HARNESS asks merge / try it first / keep playing /
discard.

Owner, 2026-09-02: a coding session that ends on the prose question "merge
it, spin up a test instance, or discard?" is exactly how a branch drifts
into the orphan sweeper. So runner.offer_landing opens the required draft
PR when the session did not and raises a decision card; session.answer
routes the verdict; and orphanwork.land_session acts on it after the
session ends.

Owner, 2026-10-12: the test instance is served only when asked for. It
used to be served before every card, a minute paid whether or not the
owner meant to look, and a plain `serve` reused an instance however old.
Try it first serves a FRESH local instance and brings the card back with
its link.

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
  serve) echo "test instance up:  http://localhost:8391  (branch instance, LOCAL ONLY)";;
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
        # The card stamps the PR it found on the ledger and the PR index
        # (2026-09-03); both are cross-process stores under data/, so a
        # fixture must root them here or a fake PR #77 lands in the real
        # index.
        from server import joblog, prindex
        for mod, name in ((joblog, "STORE"), (prindex, "STORE")):
            pp = mock.patch.object(mod, name,
                                   Path(self.tmp.name) / (name + ".json"))
            pp.start()
            self.addCleanup(pp.stop)
        prindex._cache["mtime"] = None

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

    def try_it(self, r):
        """Try it first, and wait for the serve task it starts."""
        async def go():
            await r.handle_landing({"op": "landing", "verdict": "test"})
            await r._serving
        asyncio.run(go())


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
    def test_the_card_carries_the_pr_and_offers_the_instance(self):
        r = self.placed()
        self.assertTrue(self.offer(r, work=(2, 1)))
        c = self.card(r)
        self.assertIsNotNone(c)
        self.assertEqual(c["pr_url"], "https://github.com/example/vira/pull/77")
        self.assertEqual(c["branch"], "claude/t")
        self.assertEqual((c["dirty"], c["ahead"]), (2, 1))
        self.assertEqual([o["label"] for o in c["options"]],
                         ["Merge it", "Try it first", "Keep playing", "Discard"])
        self.assertFalse(c["allow_text"])
        # Nothing is served at park (owner, 2026-10-12): a merge the owner
        # is sure of must not wait a minute for a clone nobody looks at.
        self.assertEqual(c["test_url"], "")
        self.assertEqual(c["serve_note"], "")
        self.assertFalse([x for x in self.calls() if x.startswith("serve")])
        self.assertIn("pr t", self.calls())
        out = self.output(r)
        self.assertNotIn("http://localhost:8391", out)
        self.assertIn("pull/77", out)
        # and the state the surfaces poll carries it
        self.assertEqual(self.state(r)["pending"][0]["kind"], "landing")

    def test_an_existing_pr_is_not_reopened(self):
        r = self.placed()
        self.offer(r, pr="https://github.com/example/vira/pull/5")
        self.assertNotIn("pr t", self.calls())
        self.assertEqual(self.card(r)["pr_url"],
                         "https://github.com/example/vira/pull/5")

    def test_an_instance_already_up_rides_the_card_as_a_link(self):
        """Served by an earlier Try it first, or by the session itself: the
        link is carried, and still nothing is served."""
        (self.wt / ".test-instance.json").write_text(
            json.dumps({"pid": 0, "port": 8395, "label": "x"}), encoding="utf-8")
        r = self.placed()
        with mock.patch.object(runner_mod.worktree, "_instance_alive",
                               return_value=True):
            self.offer(r)
        c = self.card(r)
        self.assertEqual(c["test_url"], "http://localhost:8395")
        self.assertFalse([x for x in self.calls() if x.startswith("serve")])
        self.assertIn("already up: http://localhost:8395", self.output(r))

    def test_a_dead_instance_file_is_no_link(self):
        (self.wt / ".test-instance.json").write_text(
            json.dumps({"pid": 0, "port": 8395, "label": "x"}), encoding="utf-8")
        with mock.patch.object(runner_mod.worktree, "_instance_alive",
                               return_value=False):
            r = self.placed()
            self.offer(r)
            self.assertEqual(self.card(r)["test_url"], "")
            self.assertIsNone(runner_mod.worktree.instance_port(str(self.wt)))

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
        self.assertEqual(c["serve_note"], "")
        self.assertIn("no PR", c["pr_note"])
        # ...and when Try it first cannot serve, the card comes back saying so
        r.awaiting_reply = True
        self.try_it(r)
        c = self.card(r)
        self.assertIsNotNone(c)
        self.assertEqual(c["test_url"], "")
        self.assertIn("no free port", c["serve_note"])
        self.assertIn("could not start", self.output(r))

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

    @unittest.skipUnless(os.name == "posix",
                         "drives a stand-in scripts/branch.sh - a shell script "
                         "Windows CreateProcess cannot run (WinError 193)")
    def test_try_it_first_serves_fresh_and_brings_the_card_back(self):
        """The one verdict that serves: a FRESH instance (--fresh stops a
        stale one and re-clones), still local only, then the same card
        again with the link - the session stays parked, no verdict is on
        file, and nothing ends."""
        r = self.raised(self.placed())
        self.try_it(r)
        self.assertIn("serve t --local --fresh", self.calls())
        c = self.card(r)
        self.assertIsNotNone(c)
        self.assertEqual(c["test_url"], "http://localhost:8391")
        self.assertEqual(c["pr_url"], "https://github.com/example/vira/pull/77")
        self.assertEqual((c["dirty"], c["ahead"]), (2, 1))
        self.assertIsNone(r.landing)
        self.assertTrue(r.inbox.empty())
        self.assertFalse(r.finished_by_owner)
        out = self.output(r)
        self.assertIn("serving a fresh test instance", out)
        self.assertIn("test instance: http://localhost:8391", out)

    @unittest.skipUnless(os.name == "posix",
                         "drives a stand-in scripts/branch.sh - a shell script "
                         "Windows CreateProcess cannot run (WinError 193)")
    def test_try_it_first_after_the_park_ended_only_posts_the_link(self):
        """A reply typed while the clone ran means the session is working
        again; a card raised over a running turn would be the wrong card.
        The link still lands in the feed."""
        r = self.raised(self.placed())
        r.awaiting_reply = False
        self.try_it(r)
        self.assertIsNone(self.card(r))
        self.assertIn("test instance: http://localhost:8391", self.output(r))

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

    def test_merge_over_a_clean_tree_still_steers(self):
        """A clean tree is no sign main stood still while the card waited
        (2026-10-09: a committed branch went 78 commits stale and its
        merge stopped at a conflict nobody read). The session catches up
        first, whatever the tree looks like."""
        r = self.raised(self.placed(), work=(0, 1))
        self.landing(r, "merge", work=(0, 1))
        self.assertEqual(r.landing, {"verdict": "merge"})
        self.assertFalse(r.finished_by_owner)   # not yet - the turn runs
        self.assertEqual(r.inbox.get_nowait(), runner_mod.MERGE_STEER)

    def test_merge_off_the_parked_loop_ends_the_session(self):
        """No turn to steer: the verdict ends the session and Vira merges."""
        r = self.raised(self.placed(), work=(0, 1))
        r.awaiting_reply = False
        self.landing(r, "merge", work=(0, 1))
        self.assertTrue(r.finished_by_owner)
        self.assertIs(r.inbox.get_nowait(), runner_mod._END)

    def test_the_merge_steer_is_the_known_landing_minus_the_merge(self):
        """It points at the landing every session already has, asks for the
        catch-up with main, and keeps the merge itself with the harness."""
        s = runner_mod.MERGE_STEER
        self.assertIn("AGENTS.local.md section 3", s)
        self.assertIn("catch this branch up with main", s)
        self.assertIn("Do NOT run branch.sh merge", s)
        self.assertIn("do NOT push main", s)

    def test_the_merge_steer_says_how_to_end(self):
        """Told three things not to do, a session ends on the three things
        it did not do ("Not done, as instructed: I did not run branch.sh
        merge, push main, or touch the live checkout"), which the owner
        read as a refusal of the very thing they had just asked for
        (2026-10-28). The steer names the ending instead."""
        s = runner_mod.MERGE_STEER
        self.assertIn("Ready for Vira to merge", s)
        self.assertIn("Do not list what you left to Vira", s)
        self.assertIn("in this transcript", s)

    def test_merge_over_a_dirty_tree_steers_then_finishes(self):
        """The Implement prompt says never commit, so delivered work is
        usually uncommitted - and branch.sh merge refuses a dirty tree. The
        session that wrote it commits it; the harness never invents the
        message. The verdict is HELD so the steered turn finishes instead
        of raising the card again."""
        r = self.raised(self.placed(), work=(2, 0))
        self.landing(r, "merge", work=(2, 0))
        self.assertEqual(r.inbox.get_nowait(), runner_mod.MERGE_STEER)
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
        i = s.index("park = runner.should_park(ok)")
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

    def test_try_it_first_writes_the_verdict_and_acts_on_nothing(self):
        """Serving is the RUNNER's act (it owns the card); the server
        neither lands nor touches the branch."""
        reg, h = self._reg()
        with mock.patch.object(orphanwork, "land_session",
                               lambda *a: self.acted.append(a)):
            self.assertEqual(reg.answer(h.id, "L1", "Try it first"), "test")
            self.assertEqual(reg.answer(h.id, "L1", "2"), "test")
        _, cmds = jobfiles.read_control(h.dir, 0)
        self.assertEqual(cmds[-1], {"op": "landing", "req_id": "L1",
                                    "verdict": "test"})
        self.assertEqual(self.acted, [])

    def test_typed_numbers_follow_the_cards_order(self):
        labels = [o["label"] for o in runner_mod.LANDING_OPTIONS]
        self.assertEqual(labels, ["Merge it", "Try it first", "Keep playing",
                                  "Discard"])
        for n, label in enumerate(labels, 1):
            self.assertEqual(session.landing_verdict(str(n)),
                             session.landing_verdict(label), label)

    def test_anything_unrecognised_reads_as_keep(self):
        # a mis-parsed answer must never merge or delete a branch
        for t in ("maybe later", "The owner skipped this question. Do NOT guess",
                  "", "3", "5"):
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

    JID = "j" * 12

    def setUp(self):
        super().setUp()
        fast = mock.patch.object(orphanwork, "SESSION_END_POLL_S", 0.01)
        fast.start()
        self.addCleanup(fast.stop)
        # The session's own transcript, which the landing writes under.
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jdir = self.jobs / self.JID
        self.jdir.mkdir(parents=True)
        self.log = self.jdir / "output.log"
        self.log.write_text("Ready for Vira to merge.\n", encoding="utf-8")
        mock.patch.object(jobfiles, "JOBS_DIR", self.jobs).start()
        self.pokes = []
        mock.patch.object(orphanwork, "_poke_session",
                          self.pokes.append).start()
        self.addCleanup(mock.patch.stopall)

    def transcript(self):
        return self.log.read_text(encoding="utf-8")

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

    def test_merge_runs_the_merge_push_and_teardown(self):
        """The whole landing after the session ends: merge, push, and the
        teardown that leaves nothing for someone to discard."""
        wt = self.make_worktree("clean1", commits=1)
        job_row = lambda jid: {"id": jid, "status": "done",  # noqa: E731
                               "branch": "claude/clean1", "worktree": str(wt)}
        with mock.patch.object(orphanwork, "_job_row", job_row):
            orphanwork.land_session("j" * 12, "claude/clean1", "merge")
            a = self._wait("claude/clean1")
        self.assertEqual(a["status"], "ok")
        self.assertIn("branch.sh merge clean1", a["output"])
        self.assertIn("push:", a["output"])
        self.assertIn("branch.sh discard clean1", a["output"])

    def test_nothing_ahead_merges_nothing(self):
        wt = self.make_worktree("d4", commits=0)
        job_row = lambda jid: {"id": jid, "status": "done",  # noqa: E731
                               "branch": "claude/d4", "worktree": str(wt)}
        merged = mock.MagicMock(return_value=(True, "x"))
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_merge_sync", merged), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session("j" * 12, "claude/d4", "merge")
            a = self._wait("claude/d4")
        self.assertEqual(a["status"], "failed")
        self.assertIn("no commits ahead", a["output"])
        merged.assert_not_called()

    def test_the_wait_times_out_honestly(self):
        job_row, _ = self._rows(10 ** 6, self.root)
        merged = mock.MagicMock(return_value=(True, "x"))
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_merge_sync", merged), \
             mock.patch.object(orphanwork, "LAND_WAIT_S", 0), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session("j" * 12, "claude/t", "merge")
            a = self._wait("claude/t")
        self.assertEqual(a["status"], "failed")
        self.assertIn("still running", a["output"])
        merged.assert_not_called()

    def test_discard_runs_branch_sh_discard_after_the_end(self):
        job_row, _ = self._rows(2, self.root)
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session("j" * 12, "claude/t", "discard")
            a = self._wait("claude/t")
        self.assertEqual(a["name"], "discard")
        self.assertEqual(a["status"], "ok")
        self.assertIn("branch.sh discard t", a["output"])

    # ----- the landing reads on from the session's last words -----
    #
    # Owner, 2026-10-28: the merge ran in a server thread whose only trace
    # was an in-memory action on an unlanded row, and that row vanished the
    # moment the branch merged. The transcript ended on the session saying
    # it had NOT merged, which read as a session that silently ended.

    def test_the_landing_writes_under_the_sessions_last_turn(self):
        job_row, _ = self._rows(2, self.root)
        text = ("merging claude/t into main...\nMerge made by the 'ort' "
                "strategy.\npush: To origin\nteardown: branch.sh discard t")
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_dirty_lines", lambda p: []), \
             mock.patch.object(orphanwork, "_ahead_behind", lambda b: (1, 0)), \
             mock.patch.object(orphanwork, "_merge_sync",
                               lambda slug: (True, text)), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session(self.JID, "claude/t", "merge")
            self._wait("claude/t")
        log = self.transcript()
        # in order: the session's words, the start, the verdict, the words
        i_start = log.index("[vira] landing: merging claude/t into main")
        i_done = log.index("[vira] landed: claude/t merged into main, "
                           "pushed, branch and worktree removed")
        self.assertLess(log.index("Ready for Vira"), i_start)
        self.assertLess(i_start, i_done)
        self.assertIn("  push: To origin\n", log)       # branch.sh's words, indented
        self.assertIn("  teardown: branch.sh discard t\n", log)
        self.assertEqual(self.pokes, [self.JID, self.JID])

    def test_the_transcript_is_complete_before_the_action_reports_done(self):
        """A reader polling the action for completion (the Runs list, a
        test) must find the transcript already written: the note goes in
        BEFORE the status flips, like the re-sweep."""
        job_row, _ = self._rows(0, self.root)
        seen = []
        real = orphanwork._set_action

        def spy(branch, name, status, output):
            if status != "running":
                seen.append(self.transcript())
            real(branch, name, status, output)
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_dirty_lines", lambda p: []), \
             mock.patch.object(orphanwork, "_ahead_behind", lambda b: (1, 0)), \
             mock.patch.object(orphanwork, "_merge_sync",
                               lambda slug: (True, "merged")), \
             mock.patch.object(orphanwork, "_set_action", spy), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session(self.JID, "claude/t", "merge")
            self._wait("claude/t")
        self.assertEqual(len(seen), 1)
        self.assertIn("[vira] landed:", seen[0])

    def test_a_refused_landing_says_so_in_the_transcript(self):
        job_row, _ = self._rows(0, self.root)
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_dirty_lines",
                               lambda p: ["?? x.py"]), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session(self.JID, "claude/t", "merge")
            self._wait("claude/t")
        log = self.transcript()
        self.assertIn("[vira] landing failed: claude/t was NOT merged", log)
        self.assertIn("uncommitted changes", log)

    def test_the_notes_the_owner_must_act_on_get_their_own_lines(self):
        """The restart when server/ changed, a failed push, a held
        teardown: each is a [vira] line of its own, not a line buried in
        branch.sh's block. And a merge that did not push is not "landed,
        pushed": main is on this machine only, and the head says so."""
        job_row, _ = self._rows(0, self.root)
        text = ("merging claude/t into main...\n"
                "push FAILED: fatal: could not read from remote\n"
                "server code changed \u2014 restart is the owner's: "
                "launchctl kickstart -k gui/501/nyc.durham.vira")
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_dirty_lines", lambda p: []), \
             mock.patch.object(orphanwork, "_ahead_behind", lambda b: (1, 0)), \
             mock.patch.object(orphanwork, "_merge_sync",
                               lambda slug: (True, text)), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session(self.JID, "claude/t", "merge")
            self._wait("claude/t")
        log = self.transcript()
        self.assertIn("[vira] landed: claude/t merged into main, push FAILED "
                      "(main is local only)", log)
        self.assertNotIn("worktree removed", log)
        self.assertIn("[vira] push FAILED: fatal", log)
        self.assertIn("[vira] server code changed", log)
        self.assertIn("launchctl kickstart", log)

    def test_discard_writes_its_outcome_too(self):
        job_row, _ = self._rows(1, self.root)
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session(self.JID, "claude/t", "discard")
            self._wait("claude/t")
        log = self.transcript()
        self.assertIn("[vira] landing: discarding claude/t", log)
        self.assertIn("[vira] discarded: claude/t", log)
        self.assertIn("  branch.sh discard t\n", log)

    def test_a_long_block_keeps_its_tail(self):
        """The end of a merge is where the refusal or the push is; a
        preflight that printed a suite is trimmed from the front."""
        text = "\n".join(f"line {n}" for n in range(3000))
        block = orphanwork._landing_block(text)
        # the tail plus two spaces of indent per line and the trim note
        self.assertLessEqual(len(block), orphanwork.TRANSCRIPT_TAIL
                             + 2 * block.count("\n") + 40)
        self.assertIn("(earlier output trimmed)", block)
        self.assertIn("  line 2999\n", block)
        self.assertNotIn("line 0\n", block)

    def test_a_missing_transcript_never_fails_the_landing(self):
        mock.patch.object(jobfiles, "JOBS_DIR",
                          Path(self.tmp.name) / "nowhere").start()
        job_row, _ = self._rows(0, self.root)
        with mock.patch.object(orphanwork, "_job_row", job_row), \
             mock.patch.object(orphanwork, "_dirty_lines", lambda p: []), \
             mock.patch.object(orphanwork, "_ahead_behind", lambda b: (1, 0)), \
             mock.patch.object(orphanwork, "_merge_sync",
                               lambda slug: (True, "merged")), \
             mock.patch.object(orphanwork, "refresh", lambda: None):
            orphanwork.land_session(self.JID, "claude/t", "merge")
            a = self._wait("claude/t")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(self.pokes, [])
        self.assertFalse((Path(self.tmp.name) / "nowhere").exists())

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
            self.assertIn("served only when the owner picks Try it first", p)
        self.assertNotIn("VIRA HANDLES THE LANDING", viratools.preamble(True))

    def test_a_session_that_changes_no_file_is_not_promised_a_landing(self):
        """offer_landing raises no card over a branch with nothing on it
        (TheCard, work=(0, 0)), and a map build is placed like any writing
        session while its only output is save_map. Promising every placed
        session a test instance and a card contradicted the map prompt."""
        kw = dict(worktree_path="/tmp/wt", branch="claude/x",
                  live_root="/tmp/live")
        for native in (True, False):
            p = viratools.preamble(native, **kw)
            self.assertIn("If you changed files on this branch", p)
            self.assertIn("there is nothing to land: no card goes up", p)


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

    def test_config_carries_the_card_switch_and_not_the_retired_serve_one(self):
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertTrue(cfg["session_landing_card"])
        self.assertTrue(session.SESSION_DEFAULTS["session_landing_card"])
        # session_auto_serve served an instance before every card; retired
        # 2026-10-12 in favour of Try it first on the card itself.
        self.assertNotIn("session_auto_serve", cfg)
        self.assertNotIn("session_auto_serve", session.SESSION_DEFAULTS)
        self.assertIn("Try it first", cfg["_session_landing_card"])


if __name__ == "__main__":
    unittest.main()

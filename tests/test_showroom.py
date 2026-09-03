"""The Showroom - the organizer for draft branches the owner stages one at
a time.

Everything is rooted at ONE tmp fixture: the showroom store, the ideas
store, the session launcher, the judge, branch.sh, and git are all pinned
at their seams, because this module's whole job is to drive machinery
that acts on the real repo. test_an_empty_fixture_touches_nothing is the
isolation guard (the readinglist/JournalBase lesson).
"""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import ideas, joblog, showroom


class FakeGit:
    """gitutil.git stand-in: scripted per (args[0]) verb."""

    def __init__(self):
        self.calls = []
        self.status_out = ""          # git status --porcelain
        self.rebase_rc = 0
        self.ahead_out = "1"

    def __call__(self, cwd, *args, **kw):
        self.calls.append((str(cwd), args))
        r = mock.Mock()
        r.returncode = 0
        r.stdout, r.stderr = "", ""
        verb = args[0] if args else ""
        if verb == "status":
            r.stdout = self.status_out
        elif verb == "rebase" and "--abort" not in args:
            r.returncode = self.rebase_rc
        elif verb == "rev-list":
            r.stdout = self.ahead_out
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (root / "data").mkdir()

        def pin(target, attr, value):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

        pin(showroom, "STORE", root / "data" / "showroom.json")
        pin(ideas, "STORE", root / "data" / "ideas.json")
        pin(joblog, "STORE", root / "data" / "jobs-log.json")
        # No real subprocesses, no real git, no real sessions.
        self.git = FakeGit()
        pin(showroom, "gitutil",
            mock.Mock(git=self.git))
        self.branch_sh = mock.Mock(return_value=(True, "ok"))
        pin(showroom, "_branch_sh", self.branch_sh)
        self.launched = []
        self._ledger = {}

        def fake_launch(prompt, cwd=None, **kw):
            jid = f"job{len(self.launched)}"
            self.launched.append({"prompt": prompt, "cwd": cwd, **kw})
            self._ledger[jid] = {"id": jid, "status": "running",
                                 "branch": f"claude/showroom-x-{jid}",
                                 "worktree": str(Path(self.tmp.name)
                                                 / f"wt-{jid}")}
            return jid
        self.fake_launch = fake_launch
        sess = mock.Mock()
        sess.sessions.launch.side_effect = fake_launch
        sess.sessions.get.side_effect = lambda jid: self._ledger.get(jid)
        # showroom imports session/judge/ideas/orphanwork lazily by name,
        # so the pin has to land on the modules it will import.
        import server.session as real_session
        pin(real_session.sessions, "launch",
            mock.Mock(side_effect=fake_launch))
        pin(real_session.sessions, "get",
            mock.Mock(side_effect=lambda jid: self._ledger.get(jid)))
        import server.judge as real_judge
        self.judged = []
        pin(real_judge, "launch_judge",
            mock.Mock(side_effect=lambda jid: (self.judged.append(jid)
                                               or f"judge-{jid}")))
        pin(joblog, "get_record",
            mock.Mock(side_effect=lambda jid: self._ledger.get(jid)))
        # settings.get must not read the machine's config for our key.
        real_get = showroom.settings.get

        def fake_get(key):
            if key == "showroom_max_building":
                return 2
            return real_get(key)
        pin(showroom.settings, "get", fake_get)
        # env: never passive unless a test says so
        self.env = mock.patch.dict("os.environ", {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        import os
        os.environ.pop("VIRA_PASSIVE", None)

    def idea(self, text, status="open", project="Vira"):
        return ideas.add(text, status=status, project=project)

    def stage_all(self, **kw):
        """Stage every currently-eligible idea by NAMING them. There is no
        build-everything call to lean on (owner, 2026-09-02) - the ids are
        required - so a test that means 'stage what I just made' says so
        explicitly, through the same path the picker uses."""
        return showroom.build_queue(
            [it["id"] for it in showroom.eligible_ideas()], **kw)

    def worktree_for(self, iid):
        wt = Path(showroom._get(iid)["worktree"])
        wt.mkdir(parents=True, exist_ok=True)
        return wt


class Staging(Base):
    def test_an_empty_fixture_touches_nothing(self):
        # The isolation guard: with nothing staged, no launch, no
        # branch.sh call, no git call, and an empty compose.
        showroom.tick()
        out = showroom.compose()
        self.assertEqual(out["candidates"], [])
        self.assertEqual(self.launched, [])
        self.assertEqual(self.branch_sh.call_count, 0)
        self.assertEqual(self.git.calls, [])

    def test_build_queue_stages_open_vira_ideas_only(self):
        a = self.idea("build the widget")
        self.idea("a proposed one", status="proposed")
        self.idea("a done one", status="done")
        self.idea("another repo", project="TC-IL")
        out = self.stage_all()
        self.assertEqual(out["staged"], 1)
        self.assertEqual(out["ids"], [a["id"]])

    def test_explicit_ids_refuse_non_vira_by_name(self):
        other = self.idea("elsewhere", project="TC-IL")
        out = showroom.build_queue(idea_ids=[other["id"]])
        self.assertEqual(out["staged"], 0)
        self.assertIn("not a Vira idea", out["skipped"][0])

    def test_an_active_candidate_is_never_restaged(self):
        # Named EXPLICITLY the second time, not left to the eligible
        # filter: the guard being pinned lives inside build_queue, and a
        # double-click on "Build it" reaches it with the id in hand.
        a = self.idea("one")
        self.stage_all()
        out = showroom.build_queue([a["id"]])
        self.assertEqual(out["staged"], 0)
        self.assertEqual(showroom._get(a["id"])["state"], "queued")
        self.assertEqual(len(showroom._load()["candidates"]), 1)

    def test_limit_reports_the_overflow(self):
        for i in range(3):
            self.idea(f"idea {i}")
        out = self.stage_all(limit=1)
        self.assertEqual(out["staged"], 1)
        self.assertTrue(any("more past the limit" in s
                            for s in out["skipped"]))

    def test_passive_refuses_build_queue(self):
        import os
        os.environ["VIRA_PASSIVE"] = "1"
        with self.assertRaises(PermissionError):
            self.stage_all()

    def test_an_empty_call_is_refused_never_read_as_everything(self):
        """The owner's 2026-09-02 correction, pinned: the Showroom stages
        what he names. An `ids or everything` default would make a
        bodyless POST build the whole backlog, so the refusal is the
        feature - and it must fire with real eligible ideas sitting there,
        or the test passes for the wrong reason."""
        self.idea("one")
        self.idea("two")
        for empty in (None, [], ()):
            with self.assertRaises(ValueError) as cm:
                showroom.build_queue(empty)
            self.assertIn("never the whole queue", str(cm.exception))
        self.assertEqual(showroom.compose()["candidates"], [])
        self.assertEqual(self.launched, [])

    def test_the_picker_feed_offers_only_eligible_ideas(self):
        a = self.idea("build the widget")
        self.idea("done already", status="done")
        self.idea("other repo", project="TC-IL")
        ids = [r["id"] for r in showroom.eligible_list()["ideas"]]
        self.assertEqual(ids, [a["id"]])

    def test_a_staged_idea_leaves_the_picker_feed(self):
        # Otherwise "build it" twice in a row would stage the same idea
        # again from a list still offering it.
        a = self.idea("one")
        showroom.build_queue([a["id"]])
        self.assertEqual(showroom.eligible_list()["ideas"], [])


class DriverTick(Base):
    def test_the_driver_launches_up_to_the_cap(self):
        for i in range(4):
            self.idea(f"idea {i}")
        self.stage_all()
        showroom.tick()
        self.assertEqual(len(self.launched), 2)     # showroom_max_building=2
        states = [c["state"]
                  for c in showroom._load()["candidates"].values()]
        self.assertEqual(sorted(states), ["building", "building",
                                          "queued", "queued"])

    def test_a_launch_records_branch_and_worktree_from_the_ledger(self):
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        c = showroom._get(a["id"])
        self.assertTrue(c["branch"].startswith("claude/"))
        self.assertTrue(c["worktree"])

    def test_the_session_cap_leaves_the_rest_queued(self):
        import server.session as real_session
        real_session.sessions.launch.side_effect = ValueError("cap full")
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        self.assertEqual(showroom._get(a["id"])["state"], "queued")

    def test_a_finished_build_is_judged(self):
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        jid = showroom._get(a["id"])["job_id"]
        self._ledger[jid]["status"] = "done"
        showroom.tick()
        c = showroom._get(a["id"])
        self.assertEqual(c["state"], "built")
        self.assertEqual(self.judged, [jid])
        self.assertEqual(c["judge_job"], f"judge-{jid}")

    def test_the_judge_verdict_is_copied_onto_the_candidate(self):
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        jid = showroom._get(a["id"])["job_id"]
        self._ledger[jid]["status"] = "done"
        showroom.tick()
        self._ledger[jid]["judge"] = {"grade": "A-",
                                      "summary": "solid work"}
        showroom.tick()
        c = showroom._get(a["id"])
        self.assertEqual(c["grade"], "A-")
        self.assertEqual(c["judge_summary"], "solid work")

    def test_a_failed_build_reads_failed_with_the_error(self):
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        jid = showroom._get(a["id"])["job_id"]
        self._ledger[jid]["status"] = "error"
        self._ledger[jid]["error"] = "it broke"
        showroom.tick()
        c = showroom._get(a["id"])
        self.assertEqual(c["state"], "failed")
        self.assertIn("it broke", c["error"])

    def test_builds_never_pass_idea_id_to_launch(self):
        # The runner's epilogue marks a launch's idea done when the BUILD
        # finishes; a candidate idea is done when it LANDS. The link rides
        # meta only.
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        self.assertIsNone(self.launched[0].get("idea_id"))
        self.assertEqual(self.launched[0]["meta"]["showroom_idea"], a["id"])
        self.assertTrue(self.launched[0]["meta"]["machine"])


class Prompts(Base):
    def test_the_build_prompt_carries_the_contract(self):
        p = showroom.build_prompt("make the thing")
        self.assertTrue(p.startswith('Showroom build - "make the thing"'))
        self.assertIn('"""make the thing"""', p)
        self.assertIn("COMMIT your work", p)
        self.assertIn("Do not merge and do not push", p)
        self.assertIn("Never\nrestart" if False else "restart", p)
        self.assertIn("unittest discover tests", p)

    def test_the_iterate_prompt_leads_with_the_owners_note(self):
        c = {"text": "the idea", "worktree": "/tmp/wt"}
        p = showroom.iterate_prompt(c, "make it blue")
        self.assertIn('"""make it blue"""', p)
        self.assertIn("it wins over the", p)
        self.assertIn("COMMIT on this branch", p)


class Verdicts(Base):
    def _built(self, text="one"):
        a = self.idea(text)
        showroom.build_queue(idea_ids=[a["id"]])
        showroom.tick()
        jid = showroom._get(a["id"])["job_id"]
        self._ledger[jid]["status"] = "done"
        showroom.tick()
        self.worktree_for(a["id"])
        return a["id"]

    def _wait_state(self, iid, want, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if showroom._get(iid)["state"] == want:
                return
            time.sleep(0.02)
        self.fail(f"candidate never reached {want}: "
                  f"{showroom._get(iid)['state']}")

    def test_land_merges_marks_the_idea_done_and_tidies(self):
        iid = self._built()
        with mock.patch("server.orphanwork._merge_sync",
                        return_value=(True, "merged")) as ms:
            showroom.land(iid)
            self._wait_state(iid, "landed")
        ms.assert_called_once()
        it = next(i for i in ideas.list_items() if i["id"] == iid)
        self.assertEqual(it["status"], "done")
        self.assertIn("landed from the Showroom", it["note"])
        discards = [c for c in self.branch_sh.call_args_list
                    if c.args[0][0] == "discard"]
        self.assertEqual(len(discards), 1)

    def test_a_failed_merge_reverts_to_built_with_the_output(self):
        iid = self._built()
        with mock.patch("server.orphanwork._merge_sync",
                        return_value=(False, "preflight refused")):
            showroom.land(iid)
            self._wait_state(iid, "built")
        self.assertIn("preflight refused", showroom._get(iid)["land_output"])
        it = next(i for i in ideas.list_items() if i["id"] == iid)
        self.assertEqual(it["status"], "open")

    def test_land_refuses_a_dirty_worktree_by_name(self):
        iid = self._built()
        self.git.status_out = " M static/app.js\n"
        with self.assertRaises(ValueError) as cm:
            showroom.land(iid)
        self.assertIn("uncommitted", str(cm.exception))

    def test_land_refuses_a_conflicted_candidate(self):
        iid = self._built()
        showroom._set(iid, state="conflict")
        with self.assertRaises(ValueError) as cm:
            showroom.land(iid)
        self.assertIn("rebase conflict", str(cm.exception))

    def test_landing_rebases_the_survivors(self):
        iid = self._built("landing one")
        other = self._built("surviving one")
        with mock.patch("server.orphanwork._merge_sync",
                        return_value=(True, "merged")):
            showroom.land(iid)
            self._wait_state(iid, "landed")
        c = showroom._get(other)
        self.assertIsNotNone(c["rebased"])
        self.assertIn("suite not re-run", c["note"])
        rebases = [(cwd, a) for cwd, a in self.git.calls
                   if a and a[0] == "rebase"]
        self.assertEqual(len(rebases), 1)

    def test_a_conflicted_rebase_aborts_and_marks_conflict(self):
        iid = self._built("landing one")
        other = self._built("surviving one")
        self.git.rebase_rc = 1
        with mock.patch("server.orphanwork._merge_sync",
                        return_value=(True, "merged")):
            showroom.land(iid)
            self._wait_state(iid, "landed")
        c = showroom._get(other)
        self.assertEqual(c["state"], "conflict")
        aborts = [a for _cwd, a in self.git.calls
                  if a and a[0] == "rebase" and "--abort" in a]
        self.assertEqual(len(aborts), 1)

    def test_a_serving_survivor_is_skipped_with_the_reason(self):
        iid = self._built("landing one")
        other = self._built("serving one")
        showroom._set(other, port=8381)
        with mock.patch("server.orphanwork._merge_sync",
                        return_value=(True, "merged")):
            showroom.land(iid)
            self._wait_state(iid, "landed")
        c = showroom._get(other)
        self.assertIsNone(c["rebased"])
        self.assertIn("while its test", c["note"])

    def test_discard_forces_the_teardown_and_keeps_the_idea_open(self):
        iid = self._built()
        showroom.discard(iid)
        c = showroom._get(iid)
        self.assertEqual(c["state"], "discarded")
        call = next(c for c in self.branch_sh.call_args_list
                    if c.args[0][0] == "discard")
        self.assertIn("--force", call.args[0])
        it = next(i for i in ideas.list_items() if i["id"] == iid)
        self.assertEqual(it["status"], "open")
        self.assertIn("discarded", it["note"])

    def test_iterate_relaunches_into_the_same_worktree(self):
        iid = self._built()
        wt = showroom._get(iid)["worktree"]
        out = showroom.iterate(iid, "make it blue")
        self.assertTrue(out["job_id"])
        self.assertEqual(self.launched[-1]["cwd"], wt)
        c = showroom._get(iid)
        self.assertEqual(c["state"], "building")
        self.assertIsNone(c["grade"])

    def test_iterate_refuses_an_empty_note(self):
        iid = self._built()
        with self.assertRaises(ValueError):
            showroom.iterate(iid, "  ")

    def test_serve_parses_the_port_and_stop_clears_it(self):
        iid = self._built()
        self.branch_sh.return_value = (
            True, "test instance up:  http://localhost:8381  (passive, "
                  "LOCAL ONLY)")
        showroom.serve(iid)
        deadline = time.time() + 3
        while time.time() < deadline:
            if showroom._get(iid).get("port"):
                break
            time.sleep(0.02)
        c = showroom._get(iid)
        self.assertEqual(c["port"], 8381)
        self.assertEqual(c["serve_status"], "up")
        serve_call = next(c for c in self.branch_sh.call_args_list
                          if c.args[0][0] == "serve")
        self.assertIn("--local", serve_call.args[0])
        showroom.stop_serve(iid)
        self.assertIsNone(showroom._get(iid)["port"])

    def test_cancel_unstages_a_queued_candidate_only(self):
        a = self.idea("one")
        self.stage_all()
        showroom.cancel(a["id"])
        self.assertNotIn(a["id"], showroom._load()["candidates"])
        iid = self._built("two")
        with self.assertRaises(ValueError):
            showroom.cancel(iid)

    def test_retry_requeues_when_the_worktree_is_gone(self):
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        jid = showroom._get(a["id"])["job_id"]
        self._ledger[jid]["status"] = "error"
        showroom.tick()
        import shutil
        wt = showroom._get(a["id"])["worktree"]
        shutil.rmtree(wt, ignore_errors=True)
        out = showroom.retry(a["id"])
        self.assertTrue(out.get("queued"))
        self.assertEqual(showroom._get(a["id"])["state"], "queued")

    def test_every_mutating_action_refuses_passive_by_name(self):
        iid = self._built()
        import os
        os.environ["VIRA_PASSIVE"] = "1"
        for fn in (lambda: showroom.serve(iid),
                   lambda: showroom.land(iid),
                   lambda: showroom.discard(iid),
                   lambda: showroom.iterate(iid, "x"),
                   lambda: showroom.retry(iid)):
            with self.assertRaises(PermissionError):
                fn()


class OrphanExclusion(Base):
    def test_candidate_branches_reports_active_states_only(self):
        a = self.idea("one")
        self.stage_all()
        showroom.tick()
        br = showroom._get(a["id"])["branch"]
        self.assertIn(br, showroom.candidate_branches())
        showroom._set(a["id"], state="landed")
        self.assertNotIn(br, showroom.candidate_branches())

    def test_a_broken_store_costs_the_exclusion_only(self):
        showroom.STORE.parent.mkdir(exist_ok=True)
        showroom.STORE.write_text("not json", encoding="utf-8")
        self.assertEqual(showroom.candidate_branches(), set())


class Compose(Base):
    def test_compose_orders_verdicts_first_and_counts_honestly(self):
        a = self.idea("built one")
        b = self.idea("queued one")
        showroom.build_queue(idea_ids=[a["id"]])
        showroom.tick()
        jid = showroom._get(a["id"])["job_id"]
        self._ledger[jid]["status"] = "done"
        showroom.tick()
        showroom.build_queue(idea_ids=[b["id"]])
        out = showroom.compose()
        self.assertEqual(out["candidates"][0]["idea_id"], a["id"])
        self.assertEqual(out["counts"], {"built": 1, "queued": 1})
        self.assertEqual(out["fleet"]["max_building"], 2)
        self.assertEqual(out["eligible"], 0)

    def test_compose_never_runs_git(self):
        # Polls must stay cheap: git facts are stamped at transitions,
        # never recomputed per compose.
        self.idea("one")
        self.stage_all()
        before = len(self.git.calls)
        showroom.compose()
        self.assertEqual(len(self.git.calls), before)


class RouteLayer(Base):
    """The JOIN, not each half: the real routes against the real engine,
    with only the machinery Base already pins stubbed. _showroom_call maps
    every refusal the engine raises onto a status code, and nothing else
    tests that mapping - the branch-guard lesson (both halves covered, the
    join silently broken for four days)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)

    def test_get_shape(self):
        r = self.client.get("/api/showroom")
        self.assertEqual(r.status_code, 200)
        for k in ("candidates", "fleet", "counts", "eligible", "passive"):
            self.assertIn(k, r.json())

    def test_build_requires_ids_and_a_bodyless_post_is_rejected(self):
        """The mass path is gone at the SCHEMA too: idea_ids has no
        default, so an empty body is a 422 before the engine is reached,
        and an explicit empty list is the engine's own named 409."""
        self.idea("one")
        self.assertEqual(self.client.post("/api/showroom/build").status_code,
                         422)
        r = self.client.post("/api/showroom/build", json={"idea_ids": []})
        self.assertEqual(r.status_code, 409)
        self.assertIn("never the whole queue", r.json()["detail"])
        self.assertEqual(showroom.compose()["candidates"], [])

    def test_build_stages_the_named_idea(self):
        a = self.idea("build the widget")
        r = self.client.post("/api/showroom/build",
                             json={"idea_ids": [a["id"]]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["staged"], 1)
        self.assertEqual(showroom._get(a["id"])["state"], "queued")

    def test_the_eligible_route_feeds_the_picker(self):
        a = self.idea("pick me")
        r = self.client.get("/api/showroom/eligible")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([i["id"] for i in r.json()["ideas"]], [a["id"]])

    def test_an_unknown_candidate_is_404(self):
        self.assertEqual(
            self.client.post("/api/showroom/nope/serve").status_code, 404)

    def test_a_state_refusal_is_409(self):
        a = self.idea("one")
        self.stage_all()                      # queued - nothing to serve
        r = self.client.post(f"/api/showroom/{a['id']}/serve")
        self.assertEqual(r.status_code, 409)
        self.assertIn("nothing to serve yet", r.json()["detail"])

    def test_an_empty_iterate_note_is_409(self):
        a = self.idea("one")
        self.stage_all()
        r = self.client.post(f"/api/showroom/{a['id']}/iterate",
                             json={"note": "   "})
        self.assertEqual(r.status_code, 409)

    def test_passive_is_403_on_every_mutating_route(self):
        import os
        a = self.idea("one")
        self.stage_all()
        os.environ["VIRA_PASSIVE"] = "1"
        self.addCleanup(os.environ.pop, "VIRA_PASSIVE", None)
        for path, body in (
                ("/api/showroom/build", {"idea_ids": [a["id"]]}),
                (f"/api/showroom/{a['id']}/serve", None),
                (f"/api/showroom/{a['id']}/land", None),
                (f"/api/showroom/{a['id']}/discard", None),
                (f"/api/showroom/{a['id']}/retry", None),
                (f"/api/showroom/{a['id']}/iterate", {"note": "do it"})):
            r = self.client.post(path, json=body) if body else \
                self.client.post(path)
            self.assertEqual(r.status_code, 403, path)
        # ...while the read stays open: reviewing on a test instance is
        # exactly what one is for (the orphanwork context precedent).
        self.assertEqual(self.client.get("/api/showroom").status_code, 200)


class ConfigContract(unittest.TestCase):
    def test_the_config_key_has_a_default(self):
        # settings.get raises KeyError without a DEFAULTS entry, and this
        # is read by the driver at tick time - the mail_body_index lesson.
        from server import settings
        self.assertEqual(settings.DEFAULTS.get("showroom_max_building"), 3)


if __name__ == "__main__":
    unittest.main()

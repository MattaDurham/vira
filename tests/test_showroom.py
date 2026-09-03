"""The Showroom, take two - every draft branch as a card.

Everything is rooted at ONE throwaway git repo wired as BOTH orphanwork's
and showroom's ROOT, with the ledger, gh, branch.sh, the model pass and
the outbound ping pinned at their seams. The module reads six things
outside its own store (git, the sweeper's store, the ledger, gh, the
pidfiles, the config), so `test_an_empty_repo_shows_nothing` is the
isolation guard (the readinglist / JournalBase lesson).
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import orphanwork, showroom

_REAL_KICK = showroom._kick_describe


def _git(*args, cwd):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=True)


class _RepoCase(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: on the Windows job a git child can still
        # hold the fixture repo's directory when the tempdir is removed
        # (WinError 32 at teardown, the test body already green); a leaked
        # tempdir on a throwaway runner is nothing, a red job is a blocked
        # merge.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.root)
        _git("config", "user.email", "t@example.com", cwd=self.root)
        _git("config", "user.name", "T", cwd=self.root)
        (self.root / "server").mkdir()
        (self.root / "server" / "main.py").write_text("# live\n", encoding="utf-8")
        _git("add", "-A", cwd=self.root)
        _git("commit", "-qm", "init", cwd=self.root)
        (self.root / "data").mkdir()

        def pin(target, attr, value):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

        pin(orphanwork, "ROOT", self.root)
        pin(orphanwork, "STORE", self.root / "data" / "orphan-work.json")
        pin(orphanwork, "_kick_assess", lambda: None)
        pin(showroom, "ROOT", self.root)
        pin(showroom, "STORE", self.root / "data" / "showroom.json")
        pin(showroom, "_kick_describe", lambda: None)
        self.ledger = []
        lp = mock.patch("server.joblog.list_records",
                        side_effect=lambda: list(self.ledger))
        lp.start()
        self.addCleanup(lp.stop)
        gp = mock.patch("server.joblog.get_record",
                        side_effect=lambda jid: next(
                            (r for r in self.ledger if r.get("id") == jid), None))
        gp.start()
        self.addCleanup(gp.stop)
        up = mock.patch("server.update.status", return_value={"git": False})
        up.start()
        self.addCleanup(up.stop)
        np = mock.patch("server.notify.agent_ping", return_value=True)
        np.start()
        self.addCleanup(np.stop)
        self.prs = {}
        pin(showroom, "_gh_prs", lambda: dict(self.prs))
        self.branch_sh = mock.Mock(return_value=(True, "test instance up:  http://localhost:8391  (passive, LOCAL ONLY)"))
        pin(showroom, "_branch_sh", self.branch_sh)
        pin(showroom, "_serves", {})
        with orphanwork._actions_lock:
            orphanwork._actions.clear()
        os.environ.pop("VIRA_PASSIVE", None)
        self.addCleanup(lambda: os.environ.pop("VIRA_PASSIVE", None))

    def make_worktree(self, slug, dirty=False, commits=0):
        branch = f"claude/{slug}"
        wt = self.root / ".worktrees" / slug
        _git("worktree", "add", "-b", branch, str(wt), "main", cwd=self.root)
        for i in range(commits):
            (wt / f"static/file{i}.js").parent.mkdir(exist_ok=True)
            # Content carries the slug: two branches committing identical
            # bytes in the same second mint the SAME commit hash, and the
            # "unlanded" one would literally be the landed commit.
            (wt / f"static/file{i}.js").write_text(f"// {slug} {i}\n", encoding="utf-8")
            _git("add", "-A", cwd=wt)
            _git("commit", "-qm", f"work {i}", cwd=wt)
        if dirty:
            (wt / "dirty.py").write_text("# wip\n", encoding="utf-8")
        return wt

    def land(self, slug):
        """Merge the branch --no-ff the way branch.sh does, leaving the
        worktree behind - the pre-teardown backlog shape."""
        _git("merge", "--no-ff", "-q", "-m", f"Merge branch 'claude/{slug}'",
             f"claude/{slug}", cwd=self.root)

    def by_branch(self, items=None):
        items = items if items is not None else showroom.sweep()
        return {it["branch"]: it for it in items}


class Inventory(_RepoCase):
    def test_an_empty_repo_shows_nothing(self):
        self.assertEqual(showroom.sweep(), [])
        out = showroom.compose()
        self.assertEqual(out["items"], [])
        self.assertEqual(out["counts"], {"session": 0, "unlanded": 0, "landed": 0})
        self.assertEqual(self.branch_sh.call_count, 0)

    def test_unlanded_work_joins_the_sweepers_row(self):
        self.make_worktree("feature-a", commits=2, dirty=True)
        it = self.by_branch()["claude/feature-a"]
        self.assertEqual(it["band"], "unlanded")
        self.assertEqual(it["ahead"], 2)
        self.assertEqual(it["dirty"], 1)
        self.assertTrue(it["orphan_key"].startswith("claude/feature-a:"))
        self.assertEqual(it["commits"], ["work 1", "work 0"])
        self.assertEqual(it["areas"].get("interface"), 2)
        self.assertEqual(it["title"], "Feature a")

    def test_a_landed_clean_worktree_is_the_landed_band(self):
        self.make_worktree("done-b", commits=1)
        self.land("done-b")
        it = self.by_branch()["claude/done-b"]
        self.assertEqual(it["band"], "landed")
        self.assertEqual(it["ahead"], 0)
        self.assertTrue(it["merged_sha"])
        self.assertTrue(it["merged_at"])
        self.assertEqual(it["areas"].get("interface"), 1)
        self.assertEqual(it["orphan_key"], "")
        self.assertIn("Merged into main", showroom.fallback_blurb(it))
        self.assertIn("never torn down", showroom.fallback_blurb(it))

    def test_a_fast_forwarded_landing_still_dates_and_classifies(self):
        wt = self.make_worktree("ff", commits=1)
        _git("merge", "--ff-only", "-q", "claude/ff", cwd=self.root)   # no merge commit
        it = self.by_branch()["claude/ff"]
        self.assertEqual(it["band"], "landed")
        self.assertTrue(it["merged_at"])
        self.assertEqual(it["areas"].get("interface"), 1)
        self.assertEqual(it["module_guess"], "interface")

    def test_a_worktree_mid_rebase_is_found_by_its_path(self):
        wt = self.make_worktree("rb", commits=1)
        _git("checkout", "-q", "--detach", cwd=wt)          # what a rebase in progress looks like
        self.assertEqual(showroom._worktree_of("claude/rb").resolve(), wt.resolve())
        it = self.by_branch()["claude/rb"]
        self.assertEqual(Path(it["worktree"]).resolve(), wt.resolve())

    def test_a_merged_branch_with_no_worktree_still_lists(self):
        self.make_worktree("ref-only", commits=1)
        self.land("ref-only")
        _git("worktree", "remove", "--force", str(self.root / ".worktrees" / "ref-only"),
             cwd=self.root)
        it = self.by_branch()["claude/ref-only"]
        self.assertEqual(it["band"], "landed")
        self.assertEqual(it["worktree"], "")
        self.assertIn("only the local branch ref", showroom.fallback_blurb(it))

    def test_a_live_session_is_its_own_band_and_never_an_orphan(self):
        wt = self.make_worktree("busy", dirty=True)
        self.ledger.append({"id": "job1", "branch": "claude/busy",
                            "worktree": str(wt), "status": "running",
                            "prompt": "Build the thing", "started": "x"})
        it = self.by_branch()["claude/busy"]
        self.assertEqual(it["band"], "session")
        self.assertEqual(it["orphan_key"], "")
        self.assertEqual(it["job"]["id"], "job1")
        self.assertIn("A session is live", showroom.fallback_blurb(it))

    def test_the_title_ladder_prefers_the_pr_then_the_ledger(self):
        wt = self.make_worktree("named", commits=1)
        self.prs = {"claude/named": {"number": 7, "title": "A real title",
                                     "state": "OPEN", "draft": True,
                                     "url": "https://example.com/pull/7",
                                     "body": "the body", "merged_at": "",
                                     "updated_at": ""}}
        it = self.by_branch()["claude/named"]
        self.assertEqual(it["title"], "A real title")
        self.assertEqual(it["pr"]["number"], 7)
        self.assertNotIn("body", it["pr"])
        self.prs = {}
        self.ledger.append({"id": "j", "branch": "claude/named", "worktree": str(wt),
                            "status": "done", "prompt": "Rename the dock", "title": "Rename the dock"})
        showroom._mutate(lambda s: (s.update(prs={}, prs_at=""), s)[1])
        it = self.by_branch()["claude/named"]
        self.assertEqual(it["title"], "Rename the dock")

    def test_the_title_skips_machine_authored_land_runs(self):
        # The NEWEST ledger row for a stalled-then-landed branch names the
        # harness ("Finishing stalled work in a branch-first repository");
        # the card must be named for the ORIGINATING ask.
        wt = self.make_worktree("stalled", commits=1)
        self.ledger.append({"id": "j1", "branch": "claude/stalled", "worktree": str(wt),
                            "status": "error", "prompt": "Add a birthday lane to the brief",
                            "title": "Add a birthday lane to the brief"})
        self.ledger.append({"id": "j2", "branch": "claude/stalled", "worktree": str(wt),
                            "status": "done",
                            "prompt": "Finishing stalled work in a branch-first repository so it can LAND.\nmore",
                            "title": "Finishing stalled work in a branch-first repository"})
        it = self.by_branch()["claude/stalled"]
        self.assertEqual(it["title"], "Add a birthday lane to the brief")
        self.assertEqual(it["job"]["id"], "j2")     # the newest row still rides the card
        self.assertEqual(showroom._objective([]), "")

    def test_a_preamble_slug_is_named_by_its_first_commit(self):
        wt = self.root / ".worktrees" / "you-are-vira-s-coding-agent-work-92f2d9"
        _git("worktree", "add", "-b", "claude/you-are-vira-s-coding-agent-work-92f2d9",
             str(wt), "main", cwd=self.root)
        (wt / "a.py").write_text("# a\n", encoding="utf-8")
        _git("add", "-A", cwd=wt)
        _git("commit", "-qm", "The system map can no longer ship a link to nowhere", cwd=wt)
        it = self.by_branch()["claude/you-are-vira-s-coding-agent-work-92f2d9"]
        self.assertEqual(it["title"], "The system map can no longer ship a link to nowhere")

    def test_a_landed_row_costs_one_git_status_not_a_spawn_per_fact(self):
        # The live sweep's cost is subprocess SPAWNS (a spawn out of the
        # multi-gigabyte server process is ~10x a bare python's), so the
        # per-branch reads that git can answer for every branch at once
        # - tip, date, merged-ness, the merge commit, its paths - must be
        # batched. A landed worktree may cost its own `git status` and
        # nothing else per row.
        for i in range(6):
            self.make_worktree(f"land{i}", commits=1)
            self.land(f"land{i}")
        calls = []
        real = showroom.gitutil.git

        def counting(cwd, *args, **kw):
            calls.append(args[0] if args else "")
            return real(cwd, *args, **kw)
        with mock.patch.object(showroom.gitutil, "git", counting), \
                mock.patch.object(orphanwork.gitutil, "git", counting):
            items = showroom.sweep()
        self.assertEqual(sum(1 for it in items if it["band"] == "landed"), 6)
        # The sweeper's own pass costs a status + rev-list per worktree; the
        # Showroom adds ONE status per worktree and nothing else per row -
        # never a log/show/rev-parse/for-each-ref per landed branch.
        self.assertLessEqual(calls.count("status"), 12, calls)
        self.assertLessEqual(calls.count("show"), 1, calls)
        self.assertLessEqual(calls.count("rev-parse"), 1, calls)
        self.assertLessEqual(calls.count("log"), 2, calls)
        self.assertLessEqual(calls.count("for-each-ref"), 1, calls)
        self.assertLessEqual(calls.count("worktree"), 2, calls)
        self.assertTrue(all(it["areas"].get("interface") == 1 for it in items))

    def test_the_sweeper_is_not_rerun_inside_its_freshness_window(self):
        self.make_worktree("w", commits=1)
        showroom.sweep()                               # sweeper ran (store was empty)
        with mock.patch.object(orphanwork, "refresh") as r:
            showroom.sweep()
            r.assert_not_called()
        with mock.patch.object(showroom, "ORPHAN_FRESH_S", 0), \
                mock.patch.object(orphanwork, "refresh") as r:
            showroom.sweep()
            r.assert_called_once()

    def test_humanize_drops_a_dispatch_hash(self):
        self.assertEqual(showroom.humanize("you-are-vira-s-coding-agent-work-92f2d9"),
                         "You are vira s coding agent work")
        self.assertEqual(showroom.humanize("attention-card-redesign"), "Attention card redesign")

    def test_compose_orders_bands_and_counts(self):
        self.make_worktree("u1", commits=1)
        self.make_worktree("l1", commits=1)
        self.land("l1")
        showroom.refresh()
        out = showroom.compose()
        self.assertEqual(sorted(it["band"] for it in out["items"]), ["landed", "unlanded"])
        # Newest activity first, whatever the band - no sections by type.
        self.assertEqual([it["last_activity"] for it in out["items"]],
                         sorted((it["last_activity"] for it in out["items"]), reverse=True))
        self.assertEqual(out["counts"], {"session": 0, "unlanded": 1, "landed": 1})
        self.assertEqual(out["describing"], 1)     # the unlanded one wants a read
        self.assertTrue(all(it["blurb_source"] == "derived" for it in out["items"]))

    def test_a_running_instance_is_reported(self):
        wt = self.make_worktree("served", commits=1)
        # A fake pid, with liveness pinned: os.kill(pid, 0) is a liveness
        # probe on POSIX and TerminateProcess on Windows, so a real pid here
        # would kill the test runner on the Windows job.
        (wt / ".test-instance.json").write_text(
            json.dumps({"pid": 4242, "port": 8390}), encoding="utf-8")
        with mock.patch("server.worktree._instance_alive", return_value=True):
            showroom.refresh()
        out = showroom.compose()
        self.assertEqual(out["items"][0]["instance"], {"port": 8390, "alive": True,
                                                       "snapshot": False})
        self.assertEqual(out["running"], 1)


class Describing(_RepoCase):
    def test_reads_are_grounded_or_dropped_and_cached_by_key(self):
        self.make_worktree("d1", commits=1)
        self.make_worktree("d2", commits=1)
        showroom.refresh()
        reply = json.dumps([
            {"branch": "claude/d1", "blurb": "Adds a thing. Built and waiting."},
            {"branch": "claude/nope", "blurb": "invented"},
            {"branch": "claude/d2", "blurb": ""},
        ])
        with mock.patch("server.suggest.complete", return_value=reply) as m:
            self.assertEqual(showroom.describe_missing(), 1)
            prompt = m.call_args[0][0]
        self.assertIn("claude/d1", prompt)
        self.assertIn("work 0", prompt)
        out = showroom.compose()
        by = {it["branch"]: it for it in out["items"]}
        self.assertEqual(by["claude/d1"]["blurb"], "Adds a thing. Built and waiting.")
        self.assertEqual(by["claude/d1"]["blurb_source"], "vira")
        self.assertEqual(by["claude/d2"]["blurb_source"], "derived")
        self.assertEqual(out["describing"], 1)
        # A new commit mints a new key: the cached read stops matching.
        wt = self.root / ".worktrees" / "d1"
        (wt / "more.py").write_text("# more\n", encoding="utf-8")
        _git("add", "-A", cwd=wt)
        _git("commit", "-qm", "more", cwd=wt)
        showroom.refresh()
        by = {it["branch"]: it for it in showroom.compose()["items"]}
        self.assertEqual(by["claude/d1"]["blurb_source"], "derived")

    def test_the_read_carries_a_module_tag_and_the_guess_stands_in(self):
        self.make_worktree("m1", commits=1)        # touches static/ only
        showroom.refresh()
        row = showroom.compose()["items"][0]
        self.assertEqual(row["module"], "interface")
        self.assertEqual(showroom.module_guess(["server/orphanwork.py", "server/orphanwork.py",
                                                "server/main.py", "tests/test_x.py"]), "orphanwork")
        self.assertEqual(showroom.module_guess(["scripts/branch.sh"]), "branch-tooling")
        self.assertEqual(showroom.module_guess([]), "other")
        reply = json.dumps([{"branch": "claude/m1", "module": "Reader Queue",
                             "blurb": "Sorts the reader."}])
        with mock.patch("server.suggest.complete", return_value=reply) as m:
            showroom.describe_missing()
            self.assertIn("MODULE VOCABULARY", m.call_args[0][0])
            self.assertIn("interface x1", m.call_args[0][0])
        out = showroom.compose()
        self.assertEqual(out["items"][0]["module"], "reader-queue")
        self.assertEqual(out["modules"], [["reader-queue", 1]] if isinstance(out["modules"][0], list)
                         else [("reader-queue", 1)])

    def test_an_off_shape_module_falls_back_to_the_guess(self):
        self.make_worktree("m2", commits=1)
        showroom.refresh()
        reply = json.dumps([{"branch": "claude/m2", "module": "!!!", "blurb": "Does a thing."}])
        with mock.patch("server.suggest.complete", return_value=reply):
            showroom.describe_missing()
        row = showroom.compose()["items"][0]
        self.assertEqual(row["blurb"], "Does a thing.")
        self.assertEqual(row["module"], "interface")

    def test_landed_rows_never_ask_for_a_read(self):
        self.make_worktree("l", commits=1)
        self.land("l")
        showroom.refresh()
        self.assertEqual(showroom.pending_reads(), [])
        with mock.patch("server.suggest.complete") as m:
            self.assertEqual(showroom.describe_missing(), 0)
            m.assert_not_called()

    def test_a_broken_model_reply_stores_nothing(self):
        self.make_worktree("b", commits=1)
        showroom.refresh()
        with mock.patch("server.suggest.complete", return_value="not json"):
            self.assertEqual(showroom.describe_missing(), 0)
        with mock.patch("server.suggest.complete", side_effect=RuntimeError("down")):
            self.assertEqual(showroom.describe_missing(), 0)

    def test_kick_describe_refuses_on_a_passive_instance(self):
        os.environ["VIRA_PASSIVE"] = "1"
        with mock.patch.object(showroom, "describe_missing") as dm, \
                mock.patch.object(showroom, "_spawn") as sp:
            _REAL_KICK()
            dm.assert_not_called()
            sp.assert_not_called()


class Actions(_RepoCase):
    def test_serve_runs_branch_sh_local_and_records_the_port(self):
        self.make_worktree("s", commits=1)
        showroom.refresh()
        threads = []
        with mock.patch.object(showroom, "_spawn", lambda target, name: threads.append(target)):
            self.assertEqual(showroom.serve("claude/s"), {"started": True})
        threads[0]()
        self.branch_sh.assert_called_with(["serve", "s", "--local"], showroom.SERVE_TIMEOUT)
        row = showroom.compose()["items"][0]
        self.assertEqual(row["serving"]["status"], "up")
        self.assertEqual(row["serving"]["port"], 8391)

    def test_serve_never_bridges_to_the_tailnet(self):
        self.make_worktree("s", commits=1)
        showroom.refresh()
        with mock.patch.object(showroom, "_spawn", lambda target, name: target()):
            showroom.serve("claude/s")
        for call in self.branch_sh.call_args_list:
            if call[0][0][0] == "serve":
                self.assertIn("--local", call[0][0])

    def test_serve_makes_a_worktree_for_a_bare_ref(self):
        # A branch with only a local ref is still work someone left; the
        # card's Launch must be able to look at it, so the server makes
        # the worktree (git worktree add + branch.sh adopt) before serving.
        self.make_worktree("gone", commits=1)
        _git("worktree", "remove", "--force", str(self.root / ".worktrees" / "gone"),
             cwd=self.root)
        showroom.refresh()
        self.assertEqual(showroom._worktree_of("claude/gone"), None)
        with mock.patch.object(showroom, "_spawn", lambda target, name: target()):
            showroom.serve("claude/gone")
        wt = self.root / ".worktrees" / "gone"
        self.assertTrue((wt / "static" / "file0.js").exists())
        self.branch_sh.assert_any_call(["adopt", "gone"], showroom.QUICK_TIMEOUT)
        self.branch_sh.assert_any_call(["serve", "gone", "--local"], showroom.SERVE_TIMEOUT)

    def test_serve_needs_no_card_and_names_a_missing_branch(self):
        # No sweep has run, so the store is empty - a launch still works
        # off git, and a branch git does not know fails by name.
        self.make_worktree("fresh", commits=1)
        with mock.patch.object(showroom, "_spawn", lambda target, name: target()):
            self.assertEqual(showroom.serve("claude/fresh"), {"started": True})
            showroom.serve("claude/never-existed")
        with showroom._serves_lock:
            self.assertEqual(showroom._serves["claude/fresh"]["status"], "up")
            self.assertEqual(showroom._serves["claude/never-existed"]["status"], "failed")
            self.assertIn("not a local branch", showroom._serves["claude/never-existed"]["text"])
        with self.assertRaises(ValueError):
            showroom.serve("main")

    def test_a_failed_serve_is_named_never_a_port(self):
        self.make_worktree("s", commits=1)
        showroom.refresh()
        self.branch_sh.return_value = (False, "error: no free port")
        with mock.patch.object(showroom, "_spawn", lambda target, name: target()):
            showroom.serve("claude/s")
        row = showroom.compose()["items"][0]
        self.assertEqual(row["serving"]["status"], "failed")
        self.assertIsNone(row["serving"]["port"])
        self.assertIn("no free port", row["serving"]["text"])

    def test_stop_runs_branch_sh_stop(self):
        self.make_worktree("s", commits=1)
        showroom.refresh()
        showroom.stop("claude/s")
        self.branch_sh.assert_any_call(["stop", "s"], showroom.QUICK_TIMEOUT)

    def test_cleanup_only_for_a_landed_row_and_through_the_sweepers_discard(self):
        self.make_worktree("u", commits=1)
        self.make_worktree("l", commits=1)
        self.land("l")
        showroom.refresh()
        with self.assertRaises(ValueError):
            showroom.cleanup("claude/u")
        with mock.patch.object(orphanwork, "discard", return_value=(True, "started")) as d:
            self.assertEqual(showroom.cleanup("claude/l"), {"started": True})
            d.assert_called_once_with("l")
        with mock.patch.object(orphanwork, "discard", return_value=(False, "an action is already running")):
            with self.assertRaises(ValueError):
                showroom.cleanup("claude/l")

    def test_every_action_refuses_passive_by_name(self):
        self.make_worktree("l", commits=1)
        self.land("l")
        showroom.refresh()
        os.environ["VIRA_PASSIVE"] = "1"
        for fn in (showroom.serve, showroom.stop, showroom.cleanup):
            with self.assertRaises(PermissionError) as cm:
                fn("claude/l")
            self.assertIn("passive", str(cm.exception))
        self.assertEqual(self.branch_sh.call_count, 0)

    def test_an_unknown_branch_is_a_key_error_for_cleanup(self):
        with self.assertRaises(KeyError):
            showroom.cleanup("claude/nowhere")


class Context(_RepoCase):
    def test_an_unlanded_card_carries_the_sweepers_full_context(self):
        self.make_worktree("c", commits=2)
        self.prs = {"claude/c": {"number": 3, "title": "T", "state": "OPEN", "draft": True,
                                 "url": "u", "body": "full body", "merged_at": "", "updated_at": ""}}
        showroom.refresh()
        c = showroom.context("claude/c")
        self.assertEqual(c["band"], "unlanded")
        self.assertEqual(c["pr"]["body"], "full body")
        self.assertEqual(len(c["orphan"]["commits"]), 2)
        self.assertIsNotNone(c["disk_mb"])

    def test_a_landed_card_carries_the_merge(self):
        self.make_worktree("m", commits=1)
        self.land("m")
        showroom.refresh()
        c = showroom.context("claude/m")
        self.assertIn("Merge branch 'claude/m'", c["merge"])
        self.assertEqual(c["merge_paths"], ["static/file0.js"])
        self.assertNotIn("orphan", c)


class RouteLayer(_RepoCase):
    def setUp(self):
        super().setUp()
        from server import main
        self.client = TestClient(main.app)

    def test_get_and_refresh_shape(self):
        self.make_worktree("r", commits=1)
        r = self.client.post("/api/showroom/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["unlanded"], 1)
        r = self.client.get("/api/showroom")
        self.assertEqual({"items", "counts", "running", "describing", "last_sweep", "passive",
                          "modules"},
                         set(r.json()))

    def test_context_is_read_only_and_answers_on_passive(self):
        self.make_worktree("r", commits=1)
        showroom.refresh()
        os.environ["VIRA_PASSIVE"] = "1"
        r = self.client.get("/api/showroom/context", params={"branch": "claude/r"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["branch"], "claude/r")
        self.assertEqual(self.branch_sh.call_count, 0)

    def test_refusals_map_to_403_404_409(self):
        self.make_worktree("u", commits=1)
        showroom.refresh()
        r = self.client.post("/api/showroom/cleanup", json={"branch": "claude/none"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/showroom/cleanup", json={"branch": "claude/u"})
        self.assertEqual(r.status_code, 409)
        r = self.client.post("/api/showroom/serve", json={"branch": "main"})
        self.assertEqual(r.status_code, 409)
        os.environ["VIRA_PASSIVE"] = "1"
        for path in ("serve", "stop", "cleanup"):
            r = self.client.post(f"/api/showroom/{path}", json={"branch": "claude/u"})
            self.assertEqual(r.status_code, 403, path)


class Surface(unittest.TestCase):
    """The client and the launch page: contracts a suite can pin without a
    browser. Comments are stripped so a rule cannot pass as its own
    explanation."""
    ROOT = Path(__file__).resolve().parent.parent

    @staticmethod
    def _code(text):
        import re
        return re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)

    def test_launch_opens_the_tab_and_the_page_starts_the_serve(self):
        js = self._code((self.ROOT / "static" / "app.js").read_text(encoding="utf-8"))
        fn = js[js.index("function shrLaunch("):js.index("async function loadShowroomQuiet")]
        self.assertIn("window.open(", fn)
        self.assertIn("shrLaunchOrigin()", fn)
        self.assertNotIn("/api/showroom/serve", fn)     # the PAGE starts it, on its own origin
        page = (self.ROOT / "static" / "showroom-launch.html").read_text(encoding="utf-8")
        self.assertIn("color-scheme", page)
        self.assertIn('fetch("/api/showroom/serve"', page)
        self.assertIn("location.replace(\"http://localhost:\"", page)

    def test_every_card_carries_launch_and_the_card_opens_a_focus_panel(self):
        js = self._code((self.ROOT / "static" / "app.js").read_text(encoding="utf-8"))
        foot = js[js.index("function shrFoot("):js.index("function shrLaunch(")]
        self.assertIn('"Launch the test"', foot)
        self.assertNotIn("d.passive && it.worktree", foot)
        card = js[js.index("function shrCard("):js.index("function shrFoot(")]
        self.assertIn("openShowroomCard(it)", card)
        self.assertNotIn("shr-detail", card)
        self.assertIn("enterFocus(panel", js[js.index("async function openShowroomCard"):])
        self.assertIn('"#shr-panel"', js)
        html = (self.ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="shr-panel"', html)

    def test_no_band_tabs_only_the_content_grouping(self):
        js = self._code((self.ROOT / "static" / "app.js").read_text(encoding="utf-8"))
        block = js[js.index("let shrData = null;"):js.index("function renderBrief(b)")]
        self.assertNotIn("SHR_FILTERS", block)
        self.assertIn('"Grouped by module"', block)
        self.assertIn('"module:"', block)

    def test_verdicts_go_through_the_sweepers_routes(self):
        js = self._code((self.ROOT / "static" / "app.js").read_text(encoding="utf-8"))
        fn = js[js.index("function shrArm("):js.index("async function shrFillDetail")]
        for route in ("/api/orphanwork/land", "/api/orphanwork/resume",
                      "/api/orphanwork/discard", "/api/showroom/cleanup"):
            self.assertIn(route, fn)
        self.assertNotIn("/api/showroom/land", fn)

    def test_the_window_is_registered_and_loads(self):
        js = (self.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('{ id: "showroom", title: "Showroom"', js)
        self.assertIn('if (id === "showroom") loadShowroom()', js)
        html = (self.ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="view-showroom"', html)


if __name__ == "__main__":
    unittest.main()

"""branch.sh: a clean merge tears its own worktree down.

Teardown used to be line 122 of branch.sh — an `echo` of a checklist item,
with nothing running it. Measured on the owner's machine 2026-09-02: of 30
worktrees, 26 were finished work whose teardown never ran and only 4 carried
anything unlanded (89 GB of ~9 GB `data/` clones, plus 43 stale claude/*
branch refs). The one case that ALWAYS leaves a worktree worth removing —
work that merged — was removed by nothing automatic: worktree.tidy refuses a
branch with commits ("keeping the work"), so it only ever cleans sessions
that produced nothing.

These drive the real cmd_merge with git/gh/preflight replaced by RECORDERS
and assert the EFFECT — did teardown run, was it held, and for which named
reason — never the argv spelling (the caffeinate lesson).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

BRANCH_SH = Path(__file__).resolve().parents[1] / "scripts" / "branch.sh"

posix_only = unittest.skipUnless(
    os.name == "posix", "branch.sh is POSIX dev tooling, not a shipped path")


@posix_only
class MergeTeardownBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rec = self.root / "rec"
        self.rec.write_text("", encoding="utf-8")
        self.live = self.root / "live"
        self.wt = self.root / "wt"
        for d in (self.live, self.wt):
            d.mkdir()
        (self.live / "CLAUDE.md").write_text("shared line\n", encoding="utf-8")
        (self.wt / "CLAUDE.md").write_text("shared line\n", encoding="utf-8")
        # cmd_merge announces a MISSING preflight and merges anyway, so the
        # file has to exist for the stubbed `bash` to be the thing deciding.
        (self.live / "scripts").mkdir()
        (self.live / "scripts" / "preflight.sh").write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_merge(self, args="demo", dirty="", preflight_ok=True):
        """Source branch.sh, stub everything that touches the machine, run
        cmd_merge. `dirty` is what the WORKTREE's status --porcelain reports
        AFTER the merge (the pre-merge check reads the same stub, so a dirty
        value also exercises the early refusal)."""
        script = f'''
source "{BRANCH_SH}"
LIVE="{self.live}"
REC="{self.rec}"
wt_dir() {{ echo "{self.wt}"; }}
instance_pid() {{ echo ""; }}
stop_test_process() {{ :; }}
pr_require() {{ return 0; }}
pr_sync_head() {{ return 0; }}
pr_merge_hook() {{ PR_NOTE=""; return 0; }}
# cmd_discard is the teardown under test — record that it ran, with its slug.
cmd_discard() {{ echo "DISCARD $1" >> "$REC"; return 0; }}
git() {{
  case "$*" in
    *show-ref*)                  return 0;;
    *"status --porcelain -uno"*) return 0;;
    *"ls-files --others"*)       echo ""; return 0;;
    *status*--porcelain*)        echo -n "{dirty}"; return 0;;
    *"diff --name-only"*)        echo ""; return 0;;
    *merge*--no-ff*)             echo "MERGED" >> "$REC"; return 0;;
    *) return 0;;
  esac
}}
bash() {{ return {0 if preflight_ok else 1}; }}
cmd_merge {args}
'''
        return subprocess.run(["zsh", "-c", script], capture_output=True,
                              text=True, cwd=BRANCH_SH.parents[1])

    def recorded(self):
        return self.rec.read_text(encoding="utf-8")


class ACleanMergeTearsDown(MergeTeardownBase):
    def test_teardown_runs_without_being_asked(self):
        r = self.run_merge()
        self.assertIn("MERGED", self.recorded(), r.stderr)
        self.assertIn("DISCARD demo", self.recorded(),
                      f"teardown did not run\n{r.stdout}\n{r.stderr}")

    def test_it_reuses_cmd_discard_rather_than_its_own_teardown(self):
        """Never a second implementation: cmd_discard already stops a serving
        instance, refuses on dirt, and routes the PR through the hook that
        keeps a merged PR open while main is unpushed."""
        r = self.run_merge()
        self.assertIn("DISCARD", self.recorded())
        for own in ("rm -rf", "worktree remove"):
            self.assertNotIn(own, r.stdout)

    def test_the_push_step_is_still_named(self):
        """Teardown is now automatic; the push is NOT, and must stay visible —
        an unpushed merge is what leaves a PR unable to flip to Merged."""
        r = self.run_merge()
        self.assertIn("push", r.stdout)


class HoldsAreNamed(MergeTeardownBase):
    def test_keep_holds_it(self):
        r = self.run_merge(args="demo --keep")
        self.assertIn("MERGED", self.recorded())
        self.assertNotIn("DISCARD", self.recorded())
        self.assertIn("--keep", r.stdout)

    def test_unported_spec_lines_hold_it(self):
        """The port step diffs against the worktree, so removing it first
        would delete the only copy of the session's spec edits."""
        (self.wt / "CLAUDE.md").write_text(
            "shared line\na line only the worktree has\n", encoding="utf-8")
        r = self.run_merge()
        self.assertNotIn("DISCARD", self.recorded())
        self.assertIn("CLAUDE.md", r.stdout)

    def test_live_being_merely_AHEAD_does_not_hold_it(self):
        """The predicate is one-way on purpose. CLAUDE.md is gitignored and
        sessions write their spec section straight into LIVE, so live is
        routinely ahead of a worktree snapshot — a plain `diff -q` would fire
        on nearly every merge and turn a real signal into noise."""
        (self.live / "CLAUDE.md").write_text(
            "shared line\nanother session's new section\n", encoding="utf-8")
        r = self.run_merge()
        self.assertIn("DISCARD demo", self.recorded(),
                      f"held on a live-is-ahead diff\n{r.stdout}")

    def test_a_worktree_with_no_spec_at_all_does_not_hold_it(self):
        """No CLAUDE.md means the session never read the spec — worth saying,
        but there is nothing in the worktree to port FROM."""
        (self.wt / "CLAUDE.md").unlink()
        self.run_merge()
        self.assertIn("DISCARD demo", self.recorded())

    def test_every_hold_names_its_reason_and_the_manual_command(self):
        r = self.run_merge(args="demo --keep")
        self.assertIn("HELD", r.stdout)
        self.assertIn("branch.sh discard demo", r.stdout)


class NothingTearsDownWithoutAMerge(MergeTeardownBase):
    def test_a_failed_preflight_reaches_neither_merge_nor_teardown(self):
        r = self.run_merge(preflight_ok=False)
        self.assertNotIn("MERGED", self.recorded())
        self.assertNotIn("DISCARD", self.recorded())
        self.assertNotEqual(r.returncode, 0)

    def test_a_dirty_worktree_is_refused_before_the_merge(self):
        r = self.run_merge(dirty=" M server/x.py")
        self.assertNotIn("MERGED", self.recorded())
        self.assertNotIn("DISCARD", self.recorded())
        self.assertNotEqual(r.returncode, 0)


class Flags(MergeTeardownBase):
    def test_an_unknown_flag_exits_rather_than_reading_as_off(self):
        """A safety flag that can be ignored is not a safety flag — a
        misspelled --keep must never mean 'tear it down anyway'."""
        r = self.run_merge(args="demo --kepe")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("DISCARD", self.recorded())
        self.assertIn("--kepe", r.stderr)

    def test_the_flag_is_read_by_name_not_by_position(self):
        r = self.run_merge(args="demo --keep")
        self.assertNotIn("DISCARD", self.recorded())


class Documented(unittest.TestCase):
    @posix_only
    def test_usage_names_the_new_behaviour(self):
        head = BRANCH_SH.read_text(encoding="utf-8").split("set -eu")[0]
        self.assertIn("--keep", head)
        self.assertRegex(head, r"merge <slug>.*\n.*tears the worktree down")


if __name__ == "__main__":
    unittest.main()


@posix_only
class TheJoinAgainstRealGit(unittest.TestCase):
    """The stubs above prove cmd_merge DECIDES to tear down. This proves the
    teardown actually happens: a real repo, a real linked worktree, a real
    merge, and the real cmd_discard — only GitHub and the machine are stubbed.

    Testing each half and never the join is what let the branch-first write
    guard ship disarmed for four days.
    """

    def sh(self, *args, cwd=None):
        return subprocess.run(args, cwd=cwd or self.repo, capture_output=True,
                              text=True, check=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.sh("git", "init", "-q", "-b", "main")
        self.sh("git", "config", "user.email", "t@example.com")
        self.sh("git", "config", "user.name", "T")
        # data/ is gitignored in the real repo — it must be here too, or the
        # clone reads as uncommitted work and cmd_merge correctly refuses.
        (self.repo / ".gitignore").write_text("data\n", encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("spec\n", encoding="utf-8")
        (self.repo / "f.txt").write_text("one\n", encoding="utf-8")
        self.sh("git", "add", "-A")
        self.sh("git", "commit", "-qm", "init")
        # a real linked worktree carrying one real commit
        self.wt = Path(self.tmp.name) / "wt"
        self.sh("git", "worktree", "add", "-q", "-b", "claude/demo",
                str(self.wt))
        (self.wt / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
        self.sh("git", "add", "-A", cwd=self.wt)
        self.sh("git", "commit", "-qm", "the work", cwd=self.wt)
        # a gitignored data/ clone, the thing that actually holds the GBs
        (self.wt / "data").mkdir()
        (self.wt / "data" / "big.sqlite").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_merge(self):
        script = f'''
source "{BRANCH_SH}"
LIVE="{self.repo}"
wt_dir() {{ echo "{self.wt}"; }}
instance_pid() {{ echo ""; }}
stop_test_process() {{ :; }}
pr_require() {{ return 0; }}
pr_sync_head() {{ return 0; }}
pr_merge_hook() {{ PR_NOTE=""; return 0; }}
pr_discard_hook() {{ return 0; }}
cmd_merge demo
'''
        return subprocess.run(["zsh", "-c", script], capture_output=True,
                              text=True, cwd=BRANCH_SH.parents[1])

    def test_the_work_lands_and_the_worktree_is_really_gone(self):
        self.assertTrue(self.wt.exists())
        r = self.run_merge()
        self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
        # the commit is in main...
        log = subprocess.run(["git", "log", "--oneline", "main"],
                             cwd=self.repo, capture_output=True, text=True)
        self.assertIn("the work", log.stdout)
        # ...and the directory, its gitignored data/ included, is gone
        self.assertFalse(self.wt.exists(), f"worktree survived\n{r.stdout}")
        # git agrees, so no stale registration is left behind
        wl = subprocess.run(["git", "worktree", "list"], cwd=self.repo,
                            capture_output=True, text=True)
        self.assertNotIn("wt", wl.stdout.replace(str(self.repo), ""))

    def test_the_branch_ref_goes_too(self):
        """43 stale claude/* refs had accumulated alongside the worktrees."""
        self.run_merge()
        br = subprocess.run(["git", "branch", "--list", "claude/demo"],
                            cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(br.stdout.strip(), "")

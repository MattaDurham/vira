"""branch.sh merge — untracked files in the LIVE tree never block a merge.

The gate preflighted the live checkout with plain `git status --porcelain`,
which counts UNTRACKED files. So one artifact dropped in the repo root —
Playwright's .playwright-mcp/ dump, a stray screenshot — blocked EVERY
session's merge, whatever it was working on: the single shared chokepoint
in an otherwise well-isolated branch-first system, surfacing to the owner
as sessions "bumping into each other" when no two had touched the same
code (2026-08-28).

The check was also redundant where it mattered. Verified both directions
against real git: a merge that would OVERWRITE an untracked path is
refused by git itself, naming the file, with the local content preserved;
an unrelated untracked file is a bystander and survives the merge. So git
is both stricter and better worded than the check that was failing.

These drive the real script against a real throwaway repo and assert which
gate fired — no mocked git, because every refusal here IS a git question
(the reasoning tests/test_branch_worktree.py uses).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

BRANCH_SH = Path(__file__).resolve().parents[1] / "scripts" / "branch.sh"

posix_only = unittest.skipUnless(
    os.name == "posix", "branch.sh is POSIX dev tooling, not a shipped path")

LIVE_GATE = "live tree has uncommitted changes"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


@posix_only
class LiveTreeGate(unittest.TestCase):
    """What the live-tree preflight accepts and refuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.live = Path(self.tmp.name) / "live"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        git("config", "user.email", "t@example.com", cwd=self.live)
        git("config", "user.name", "T", cwd=self.live)
        (self.live / "tracked.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=self.live)
        git("commit", "-qm", "init", cwd=self.live)
        git("branch", "claude/demo", cwd=self.live)

    def merge(self):
        """Run cmd_merge's preflight far enough to learn which gate fired.

        Everything past the tree checks is stubbed, so the assertion is
        about the gate and not about a real merge.
        """
        script = f"""
        set -u
        LIVE={self.live}
        source {BRANCH_SH} >/dev/null 2>&1 || true
        LIVE={self.live}
        wt_dir() {{ echo ""; }}
        instance_pid() {{ echo ""; }}
        stop_test_process() {{ :; }}
        preflight_gate() {{ echo "PAST_TREE_CHECKS"; exit 7; }}
        # everything after the tree checks — stop here and say so
        git() {{
          if [[ "$2" == "{self.live}" && "$3" == "merge" ]]; then
            echo "PAST_TREE_CHECKS"; exit 7
          fi
          command git "$@"
        }}
        cmd_merge demo
        """
        return subprocess.run(["/bin/zsh", "-c", script],
                              capture_output=True, text=True)

    def _ran_past_live_gate(self, r):
        return LIVE_GATE not in (r.stdout + r.stderr)

    def test_an_untracked_artifact_does_not_block(self):
        (self.live / ".playwright-mcp").mkdir()
        (self.live / ".playwright-mcp" / "page.yml").write_text(
            "x", encoding="utf-8")
        (self.live / "shot.png").write_text("x", encoding="utf-8")
        r = self.merge()
        self.assertTrue(self._ran_past_live_gate(r),
                        "an untracked artifact blocked the merge — this is "
                        "the chokepoint the change exists to remove\n"
                        + r.stdout + r.stderr)

    def test_untracked_files_are_reported_not_silent(self):
        """Allowed through, never silently: something writing into the
        checkout should still be visible, just not a refusal."""
        (self.live / "shot.png").write_text("x", encoding="utf-8")
        r = self.merge()
        self.assertIn("untracked file", r.stdout + r.stderr)

    def test_a_clean_tree_says_nothing_about_untracked(self):
        r = self.merge()
        self.assertNotIn("untracked file", r.stdout + r.stderr)

    def test_a_modified_tracked_file_STILL_blocks(self):
        """The half that must not weaken: real uncommitted work a merge
        can entangle or lose."""
        (self.live / "tracked.txt").write_text("edited\n", encoding="utf-8")
        r = self.merge()
        self.assertFalse(self._ran_past_live_gate(r),
                         "a modified tracked file must still block")

    def test_a_staged_change_STILL_blocks(self):
        (self.live / "tracked.txt").write_text("staged\n", encoding="utf-8")
        git("add", "-A", cwd=self.live)
        r = self.merge()
        self.assertFalse(self._ran_past_live_gate(r),
                         "a staged change must still block")

    def test_the_refusal_names_what_is_dirty(self):
        """A gate that only says no invites a retry of the same command."""
        (self.live / "tracked.txt").write_text("edited\n", encoding="utf-8")
        r = self.merge()
        self.assertIn("tracked.txt", r.stdout + r.stderr)


@posix_only
class GitOwnsTheCollision(unittest.TestCase):
    """The premise the change rests on, asserted rather than assumed: git
    itself refuses a merge that would overwrite an untracked file. If this
    ever stops being true, letting untracked files through stops being
    safe — so it is pinned here rather than left as a comment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = Path(self.tmp.name) / "repo"
        self.r.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.r)
        git("config", "user.email", "t@example.com", cwd=self.r)
        git("config", "user.name", "T", cwd=self.r)
        (self.r / "a.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A", cwd=self.r)
        git("commit", "-qm", "init", cwd=self.r)
        git("checkout", "-qb", "feat", cwd=self.r)
        (self.r / "new.txt").write_text("from-branch\n", encoding="utf-8")
        git("add", "-A", cwd=self.r)
        git("commit", "-qm", "adds new", cwd=self.r)
        git("checkout", "-q", "main", cwd=self.r)

    def test_git_refuses_to_clobber_an_untracked_file(self):
        (self.r / "new.txt").write_text("local work\n", encoding="utf-8")
        out = git("merge", "feat", cwd=self.r)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("untracked working tree files would be overwritten",
                      out.stdout + out.stderr)
        self.assertIn("new.txt", out.stdout + out.stderr)
        self.assertEqual((self.r / "new.txt").read_text(encoding="utf-8"),
                         "local work\n", "local content must survive")

    def test_an_unrelated_untracked_file_survives_the_merge(self):
        (self.r / "shot.png").write_text("art\n", encoding="utf-8")
        out = git("merge", "feat", cwd=self.r)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual((self.r / "shot.png").read_text(encoding="utf-8"),
                         "art\n")


if __name__ == "__main__":
    unittest.main()

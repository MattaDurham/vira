"""check-pii.sh at full strength from a branch worktree.

The instance patterns file (data/pii-patterns.txt) is gitignored, so it
exists only in the main checkout; a worktree has no data/ until it is served.
The pre-commit hook therefore ran the generic patterns only for every commit
made in a worktree - and `branch.sh pr` pushes the branch to the public
remote before the merge gate's full scan sees it. Found 2026-10-07 when the
branch's own preflight reported the scan as REDUCED.

These drive the real script against real throwaway repos and assert the
effect: is a staged line matching an instance pattern blocked.

Run: .venv/bin/python -m unittest tests.test_check_pii_worktree
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CHECK_PII = Path(__file__).resolve().parents[1] / "scripts" / "check-pii.sh"

posix_only = unittest.skipUnless(
    os.name == "posix", "check-pii.sh is the POSIX pre-commit hook")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


@posix_only
class FullStrengthInAWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.live = root / "live"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        (self.live / ".gitignore").write_text("data\n", encoding="utf-8")
        git("add", ".gitignore", cwd=self.live)
        git("-c", "user.email=t@example.com", "-c", "user.name=t",
            "commit", "-q", "-m", "base", cwd=self.live)
        (self.live / "data").mkdir()
        # a synthetic instance identifier, nothing a generic pattern catches
        (self.live / "data" / "pii-patterns.txt").write_text(
            "# instance patterns\nZorblax Quintavelle\n", encoding="utf-8")
        self.wt = root / "wt"
        git("worktree", "add", "-q", "-b", "claude/demo", str(self.wt),
            cwd=self.live)

    def stage(self, where, text):
        (where / "note.txt").write_text(text, encoding="utf-8")
        git("add", "note.txt", cwd=where)

    def check(self, where, env=None):
        return subprocess.run(["sh", str(CHECK_PII)], cwd=where,
                              capture_output=True, text=True,
                              env={**os.environ, **(env or {})})

    def test_a_worktree_commit_is_checked_against_live_patterns(self):
        self.stage(self.wt, "met Zorblax Quintavelle today\n")
        r = self.check(self.wt)
        self.assertNotEqual(r.returncode, 0, "instance name passed the hook")
        self.assertIn("Zorblax", r.stderr)

    def test_the_main_checkout_still_uses_its_own(self):
        self.stage(self.live, "met Zorblax Quintavelle today\n")
        self.assertNotEqual(self.check(self.live).returncode, 0)

    def test_a_clean_worktree_commit_passes(self):
        self.stage(self.wt, "nothing personal here\n")
        r = self.check(self.wt)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_explicit_patterns_file_still_wins(self):
        """PII_PATTERNS_FILE lets another repo borrow these patterns; an
        explicit choice is never second-guessed by the fallback."""
        other = Path(self.tmp.name) / "other.txt"
        other.write_text("# nothing\n", encoding="utf-8")
        self.stage(self.wt, "met Zorblax Quintavelle today\n")
        r = self.check(self.wt, {"PII_PATTERNS_FILE": str(other)})
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()

"""branch.sh's PR layer — pr / merge-hook / discard-hook effects.

The PR is the DISPLAY of the work on GitHub; the local merge gate stays the
door (2026-08-27). These drive the real shell functions with git and gh
stubbed by RECORDERS, and assert the EFFECT — what was pushed, created,
commented, closed — never the argv spelling (the caffeinate lesson: assert
the effect, not the flag). The merge/discard hooks must also be provably
best-effort: a dead gh may never fail a finished merge or a discard.
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
class PrLayerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rec = Path(self.tmp.name) / "rec"
        self.rec.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_zsh(self, body, pr_exists=True, gh_alive=True):
        """Source branch.sh, replace git/gh with recorders, run `body`.

        The gh stub answers `pr view` from pr_exists (a draft, OPEN PR #12)
        and appends every other call — including a comment body read from
        stdin via `--body-file -` — to the recorder file.
        """
        stubs = f'''
source "{BRANCH_SH}"
REC="{self.rec}"
git() {{
  # record mutating calls; answer the reads the code needs
  case "$*" in
    *show-ref*refs/heads/*)          return 0;;
    *show-ref*refs/remotes/origin/*) return {0 if pr_exists else 1};;
    *" log "*|*log\\ --reverse*)      echo "First commit subject"; return 0;;
    *) echo "GIT $*" >> "$REC"; return 0;;
  esac
}}
gh() {{
  if ! {"true" if gh_alive else "false"}; then return 1; fi
  case "$1 ${{2:-}}" in
    "auth status") return 0;;
    "pr view")
      if {"true" if pr_exists else "false"}; then
        echo "12 OPEN true https://github.com/x/y/pull/12"; return 0
      else
        return 1
      fi;;
    "pr comment")
      echo "GH $*" >> "$REC"
      case "$*" in *"--body-file -"*) sed 's/^/BODY /' >> "$REC";; esac
      return 0;;
    *) echo "GH $*" >> "$REC"; return 0;;
  esac
}}
{body}
'''
        return subprocess.run(["/bin/zsh", "-c", stubs],
                              cwd=BRANCH_SH.parents[1],
                              capture_output=True, text=True)

    def recorded(self):
        return self.rec.read_text(encoding="utf-8")


@posix_only
class CmdPr(PrLayerBase):
    def test_a_new_branch_is_pushed_and_gets_a_draft_pr(self):
        out = self.run_zsh("cmd_pr demo", pr_exists=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = self.recorded()
        self.assertIn("GIT -C", rec)
        self.assertIn("push -u origin claude/demo", rec)
        self.assertIn("pr create", rec)
        self.assertIn("--draft", rec)

    def test_ready_opens_a_non_draft_pr(self):
        out = self.run_zsh("cmd_pr demo --ready", pr_exists=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("--draft", self.recorded())

    def test_title_and_body_ride_the_create(self):
        body = Path(self.tmp.name) / "body.md"
        body.write_text("the write-up", encoding="utf-8")
        out = self.run_zsh(
            f'cmd_pr demo --title "My title" --body-file "{body}"',
            pr_exists=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = self.recorded()
        self.assertIn("My title", rec)
        self.assertIn(str(body), rec)

    def test_an_absent_title_falls_back_to_the_first_commit_subject(self):
        out = self.run_zsh("cmd_pr demo", pr_exists=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("First commit subject", self.recorded())

    def test_an_existing_pr_is_updated_not_duplicated(self):
        body = Path(self.tmp.name) / "body.md"
        body.write_text("v2 of the write-up", encoding="utf-8")
        out = self.run_zsh(f'cmd_pr demo --body-file "{body}"', pr_exists=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = self.recorded()
        self.assertIn("push -u origin claude/demo", rec)
        self.assertNotIn("pr create", rec)
        self.assertIn("pr edit", rec)
        self.assertIn("PR #12 updated", out.stdout)

    def test_ready_on_an_existing_draft_marks_it_ready(self):
        out = self.run_zsh("cmd_pr demo --ready", pr_exists=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("pr ready", self.recorded())

    def test_an_unknown_flag_is_refused_rather_than_ignored(self):
        out = self.run_zsh("cmd_pr demo --raedy", pr_exists=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("unknown flag", out.stderr)
        self.assertNotIn("push", self.recorded())

    def test_dead_gh_refuses_before_pushing_anything(self):
        out = self.run_zsh("cmd_pr demo", pr_exists=False, gh_alive=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("gh is missing or not authenticated", out.stderr)
        self.assertNotIn("push", self.recorded())


@posix_only
class MergeHook(PrLayerBase):
    def pf_log(self, text):
        p = Path(self.tmp.name) / "pf.log"
        p.write_text(text, encoding="utf-8")
        return p

    def test_the_gate_rows_land_in_the_pr_comment(self):
        log = self.pf_log(
            "  ok    base      branch base exists in main\n"
            "  ok    pii       tree clean (FULL scan)\n")
        out = self.run_zsh(
            f'pr_merge_hook claude/demo "{log}" 1; echo "NOTE:$PR_NOTE"')
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = self.recorded()
        self.assertIn("pr comment", rec)
        self.assertIn("BODY ", rec)
        self.assertIn("FULL scan", rec)
        self.assertIn("passed", rec)
        # the draft is marked ready and the push line learns the flip
        self.assertIn("pr ready", rec)
        self.assertIn("flips PR #12 to Merged", out.stdout)

    def test_an_overridden_gate_says_so_in_the_comment(self):
        log = self.pf_log("  FAIL  ci        CI is failure\n")
        out = self.run_zsh(f'pr_merge_hook claude/demo "{log}" 0')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("VIRA_SKIP_PREFLIGHT", self.recorded())

    def test_no_pr_means_no_comment_and_no_failure(self):
        log = self.pf_log("  ok    base      x\n")
        out = self.run_zsh(
            f'pr_merge_hook claude/demo "{log}" 1; echo HOOK-DONE',
            pr_exists=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("HOOK-DONE", out.stdout)
        self.assertEqual(self.recorded(), "")

    def test_dead_gh_never_fails_the_hook(self):
        """Best-effort by construction: a GitHub outage must not fail a
        finished merge, which is what a nonzero return under set -eu does."""
        log = self.pf_log("  ok    base      x\n")
        out = self.run_zsh(
            f'pr_merge_hook claude/demo "{log}" 1; echo HOOK-DONE',
            gh_alive=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("HOOK-DONE", out.stdout)


@posix_only
class DiscardHook(PrLayerBase):
    def test_an_open_pr_is_commented_and_closed(self):
        out = self.run_zsh("pr_discard_hook claude/demo")
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = self.recorded()
        self.assertIn("pr comment", rec)
        self.assertIn("Closed without merging", rec)
        self.assertIn("pr close", rec)
        self.assertIn("closed PR #12 without merging", out.stdout)

    def test_no_pr_means_nothing_happens(self):
        out = self.run_zsh("pr_discard_hook claude/demo; echo HOOK-DONE",
                           pr_exists=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("HOOK-DONE", out.stdout)
        self.assertEqual(self.recorded(), "")

    def test_dead_gh_never_fails_the_hook(self):
        out = self.run_zsh("pr_discard_hook claude/demo; echo HOOK-DONE",
                           gh_alive=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("HOOK-DONE", out.stdout)


@posix_only
class UsageContract(PrLayerBase):
    def test_the_pr_command_is_documented_and_dispatched(self):
        """The sandbox.sh lesson: a documented command must be dispatched and
        a dispatched one documented — usage() prints a fixed line range that
        has fallen behind the command block before."""
        out = subprocess.run(["/bin/zsh", str(BRANCH_SH)],
                             cwd=BRANCH_SH.parents[1],
                             capture_output=True, text=True)
        self.assertIn("branch.sh pr <slug>", out.stdout)
        src = BRANCH_SH.read_text(encoding="utf-8")
        self.assertIn('pr)      [[ $# -ge 1 ]] || usage; cmd_pr "$@";;', src)


if __name__ == "__main__":
    unittest.main()

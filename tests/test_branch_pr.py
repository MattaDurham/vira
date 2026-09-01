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

    def run_zsh(self, body, pr_exists=True, gh_alive=True,
                merged_local=False, merged_origin=False, pr_flips=False):
        """Source branch.sh, replace git/gh with recorders, run `body`.

        The gh stub answers `pr view` from pr_exists (a draft, OPEN PR #12,
        head sha abc123) and appends every other call — including a comment
        body read from stdin via `--body-file -` — to the recorder file.
        merged_local/merged_origin answer the discard guard's two
        merge-base --is-ancestor probes (head vs main, head vs origin/main);
        pr_flips makes the SECOND `pr view` report MERGED, modelling
        GitHub's async merge detection landing mid-poll.
        """
        stubs = f'''
source "{BRANCH_SH}"
REC="{self.rec}"
PR_FLIP_TRIES=2
PR_FLIP_WAIT_S=0
git() {{
  # record mutating calls; answer the reads the code needs
  case "$*" in
    *show-ref*refs/heads/*)          return 0;;
    *show-ref*refs/remotes/origin/*) return {0 if pr_exists else 1};;
    *merge-base*origin/main*)        return {0 if merged_origin else 1};;
    *merge-base*)                    return {0 if merged_local else 1};;
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
        if {"true" if pr_flips else "false"} && [[ -f "$REC.seen" ]]; then
          echo "12 MERGED false abc123 https://github.com/x/y/pull/12"
        else
          touch "$REC.seen"
          echo "12 OPEN true abc123 https://github.com/x/y/pull/12"
        fi
        return 0
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
class MergedPrIsNeverClosed(PrLayerBase):
    """The PR #17 incident (2026-09-01): the push landed FIRST and the close
    still won, because GitHub's merge detection is async and the hook beat
    it. Closed-not-Merged is permanent (GitHub refuses to reopen a PR whose
    head is already in its base), so the guard keys on the head sha, never
    on call ordering."""

    def test_a_merged_pr_waits_for_the_flip_and_is_never_closed(self):
        out = self.run_zsh(
            "pr_discard_hook claude/demo; echo KEEP=$PR_KEEP_REMOTE",
            merged_local=True, merged_origin=True, pr_flips=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("pr close", self.recorded())
        self.assertIn("reads Merged", out.stdout)
        # flipped -> the remote branch may be tidied as usual
        self.assertIn("KEEP=0", out.stdout)

    def test_a_merged_pr_that_never_flips_keeps_the_remote_branch(self):
        out = self.run_zsh(
            "pr_discard_hook claude/demo; echo KEEP=$PR_KEEP_REMOTE",
            merged_local=True, merged_origin=True, pr_flips=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("pr close", self.recorded())
        self.assertIn("has not flipped it", out.stdout)
        self.assertIn("KEEP=1", out.stdout)

    def test_an_unpushed_merge_refuses_to_close_and_says_push_first(self):
        out = self.run_zsh(
            "pr_discard_hook claude/demo; echo KEEP=$PR_KEEP_REMOTE",
            merged_local=True, merged_origin=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("pr close", self.recorded())
        self.assertIn("main is NOT pushed", out.stdout)
        self.assertIn("KEEP=1", out.stdout)

    def test_a_genuinely_dropped_pr_still_closes(self):
        # head NOT reachable from main = real drop-it — the old behaviour.
        out = self.run_zsh("pr_discard_hook claude/demo",
                           merged_local=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("pr close", self.recorded())


@posix_only
class SyncHead(PrLayerBase):
    """The PR #5 lesson: a branch rebased after its PR opened lands under a
    sha the PR head does not carry, and the PR reads Closed instead of
    Merged. cmd_merge syncs the head first, best-effort."""

    def test_a_branch_with_a_pr_gets_its_head_pushed(self):
        out = self.run_zsh("pr_sync_head claude/demo")
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = self.recorded()
        self.assertIn("push --force-with-lease origin claude/demo", rec)
        self.assertIn("synced origin/claude/demo", out.stdout)

    def test_no_pr_means_no_push(self):
        out = self.run_zsh("pr_sync_head claude/demo; echo SYNC-DONE",
                           pr_exists=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("SYNC-DONE", out.stdout)
        self.assertNotIn("push", self.recorded())

    def test_dead_gh_never_fails_the_sync(self):
        out = self.run_zsh("pr_sync_head claude/demo; echo SYNC-DONE",
                           gh_alive=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("SYNC-DONE", out.stdout)
        self.assertNotIn("push", self.recorded())


@posix_only
class PrRequire(PrLayerBase):
    """The PR step is REQUIRED before merge (owner ruling 2026-09-01):
    pr_require is the door cmd_merge opens with. Unlike the rest of the
    layer it is deliberately NOT best-effort — a dead gh refuses rather
    than waives, and only an explicit VIRA_SKIP_PR=1 passes without one."""

    def test_an_open_pr_lets_the_merge_proceed(self):
        out = self.run_zsh('pr_require slug claude/slug && echo PROCEED')
        self.assertIn("PROCEED", out.stdout)

    def test_no_pr_refuses_the_merge(self):
        out = self.run_zsh('pr_require slug claude/slug || echo REFUSED',
                           pr_exists=False)
        self.assertIn("REFUSED", out.stdout)
        self.assertIn("branch.sh pr slug", out.stderr)

    def test_dead_gh_refuses_rather_than_waives(self):
        out = self.run_zsh('pr_require slug claude/slug || echo REFUSED',
                           gh_alive=False)
        self.assertIn("REFUSED", out.stdout)
        self.assertIn("VIRA_SKIP_PR=1", out.stderr)

    def test_the_override_is_explicit_and_says_so(self):
        out = self.run_zsh(
            'export VIRA_SKIP_PR=1; pr_require slug claude/slug && echo PROCEED',
            pr_exists=False, gh_alive=False)
        self.assertIn("PROCEED", out.stdout)
        self.assertIn("WITHOUT a PR", out.stdout)


@posix_only
class GuardWiring(unittest.TestCase):
    """The joins, as source contracts — the shell harness cannot drive
    cmd_merge/cmd_discard whole (they touch worktrees and launchd), so pin
    that the guarded pieces are actually reached from them."""

    def test_cmd_merge_syncs_the_pr_head_before_merging(self):
        src = BRANCH_SH.read_text(encoding="utf-8")
        at_sync = src.index('pr_sync_head "$branch" || true')
        at_merge = src.index('git -C "$LIVE" merge --no-ff')
        self.assertLess(at_sync, at_merge,
                        "the head must be on origin BEFORE the sha it will "
                        "merge as exists, or GitHub cannot connect the PR")

    def test_cmd_merge_requires_the_pr_before_anything_else(self):
        src = BRANCH_SH.read_text(encoding="utf-8")
        merge_at = src.index("cmd_merge()")
        req_at = src.index('pr_require "$1" "$branch" || exit 1', merge_at)
        preflight_at = src.index("preflight.sh", merge_at)
        self.assertLess(req_at, preflight_at,
                        "the required-PR door must be reached before the "
                        "preflight gate, so a PR-less merge cannot start")

    def test_cmd_discard_honours_the_keep_remote_flag(self):
        src = BRANCH_SH.read_text(encoding="utf-8")
        hook_at = src.index("pr_discard_hook", src.index("cmd_discard()"))
        keep_at = src.index('"${PR_KEEP_REMOTE:-0}" == 1',
                            src.index("cmd_discard()"))
        self.assertLess(hook_at, keep_at,
                        "the flag is set by the hook and must be consulted "
                        "AFTER it, before the origin branch deletion")


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

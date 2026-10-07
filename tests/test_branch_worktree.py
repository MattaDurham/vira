"""branch.sh worktree resolution — finding a branch's worktree wherever it is.

`start` creates worktrees at .worktrees/<slug>, the app's worktree toggle
creates them under .claude/worktrees/<slug>, and everything made before
2026-07-29 sits at ../vira-<slug>. wt_dir used to hardcode one layout, so
serve/stop/discard died with "no worktree at ..." on every worktree the
script hadn't made itself — and discard, finding no directory to remove,
fell through to a branch delete that git refuses while the branch is checked
out somewhere.

Asking git instead is what let the canonical path MOVE (out of ~/workspace,
where a throwaway tree read as a sibling project) without touching any other
command, and without stranding the worktrees already on disk. All three
layouts are exercised below against real throwaway git repos.

Run: .venv/bin/python -m unittest tests.test_branch_worktree
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

BRANCH_SH = Path(__file__).resolve().parents[1] / "scripts" / "branch.sh"

# See tests/test_branch_clone.py: branch.sh is POSIX-only dev tooling, driven
# through /bin/zsh. A Windows install never runs it, so these skip there.
posix_only = unittest.skipUnless(
    os.name == "posix", "branch.sh is POSIX dev tooling, not a shipped path")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


def run_in(live: Path, body: str) -> subprocess.CompletedProcess:
    """Source branch.sh from inside `live` so it resolves that checkout.

    VIRA_SKIP_PR: these sandboxes have no PRs and test other gates; the
    required-PR door is covered by test_branch_pr.PrRequire."""
    return subprocess.run(
        ["/bin/zsh", "-c",
         f'export VIRA_SKIP_PR=1\nsource "{BRANCH_SH}"\n{body}\n'],
        cwd=live, capture_output=True, text=True)


@posix_only
class WorktreeResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # resolve(): macOS hands out /var/... temp dirs that git reports back
        # as /private/var/..., and the paths are compared as strings here
        self.root = Path(self.tmp.name).resolve()
        self.live = self.root / "vira"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "base", cwd=self.live)
        # what the live checkout provisions from
        (self.live / "AGENTS.local.md").write_text("the owner's rules")
        (self.live / ".venv").mkdir()
        (self.live / ".claude").mkdir()
        (self.live / ".claude" / "launch.json").write_text("{}")

    def _add_worktree(self, path: Path, slug: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-q", "-b", f"claude/{slug}", str(path), "main",
            cwd=self.live)

    def test_resolves_harness_style_worktree(self):
        wt = self.live / ".claude" / "worktrees" / "feat"
        self._add_worktree(wt, "feat")
        r = run_in(self.live, 'wt_dir feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(wt))

    def test_resolves_canonical_worktree(self):
        wt = self.live / ".worktrees" / "feat"
        self._add_worktree(wt, "feat")
        r = run_in(self.live, 'wt_dir feat')
        self.assertEqual(r.stdout.strip(), str(wt))

    def test_resolves_legacy_sibling_worktree(self):
        """Pre-2026-07-29 worktrees are siblings of the checkout. They are
        just as real, and every command must keep working on them — the move
        must not strand the ones already on disk."""
        wt = self.root / "vira-feat"
        self._add_worktree(wt, "feat")
        r = run_in(self.live, 'wt_dir feat')
        self.assertEqual(r.stdout.strip(), str(wt))

    def test_unknown_slug_falls_back_to_canonical_path(self):
        # `start` needs a path to create, and merge/discard accept a branch
        # whose worktree is already gone. Inside the checkout, NOT beside it:
        # a worktree is an implementation detail of a branch, not a project.
        r = run_in(self.live, 'wt_dir nope')
        self.assertEqual(r.stdout.strip(),
                         str(self.live / ".worktrees" / "nope"))

    def test_start_creates_the_worktree_inside_the_checkout(self):
        """The whole point, through the real cmd_start."""
        r = run_in(self.live, 'cmd_start feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.live / ".worktrees" / "feat").is_dir())
        self.assertFalse((self.root / "vira-feat").exists())

    def test_discard_removes_a_harness_worktree_and_its_branch(self):
        wt = self.live / ".claude" / "worktrees" / "feat"
        self._add_worktree(wt, "feat")
        r = run_in(self.live, 'cmd_discard feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(wt.exists())
        branches = git("branch", "--format=%(refname:short)",
                       cwd=self.live).stdout.split()
        self.assertNotIn("claude/feat", branches)


@posix_only
class NestedWorktreesDoNotDirtyTheLiveTree(unittest.TestCase):
    """The load-bearing consequence of moving worktrees inside the checkout.

    git reports a nested worktree as an untracked directory. `cmd_merge`
    preflights `git status --porcelain` on the live tree and refuses on any
    output — so without `.worktrees/` in .gitignore, creating one worktree
    would block EVERY subsequent merge. Pinned with the repo's real
    .gitignore, so deleting that entry fails here rather than at the next
    merge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.live = self.root / "vira"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        real = Path(__file__).resolve().parents[1] / ".gitignore"
        (self.live / ".gitignore").write_text(
            real.read_text(encoding="utf-8"), encoding="utf-8")
        git("add", ".gitignore", cwd=self.live)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "base", cwd=self.live)
        (self.live / "AGENTS.local.md").write_text("rules", encoding="utf-8")
        (self.live / ".venv").mkdir()
        (self.live / ".claude").mkdir()
        (self.live / ".claude" / "launch.json").write_text("{}",
                                                           encoding="utf-8")

    def test_the_live_tree_stays_clean_after_start(self):
        r = run_in(self.live, 'cmd_start feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.live / ".worktrees" / "feat").is_dir())
        status = git("status", "--porcelain", cwd=self.live).stdout
        self.assertEqual(status.strip(), "",
                         "a nested worktree dirtied the live tree — merge "
                         "would refuse; is .worktrees/ still in .gitignore?")

    def test_a_data_clone_in_the_worktree_stays_clean_too(self):
        """`serve` clones data/ into the worktree. Doubly ignored, and it has
        to be: the merge preflight checks the worktree's status as well."""
        run_in(self.live, 'cmd_start feat')
        wt = self.live / ".worktrees" / "feat"
        (wt / "data").mkdir()
        (wt / "data" / "config.json").write_text("{}", encoding="utf-8")
        (wt / ".test-instance.json").write_text('{"pid": 1, "port": 8378}',
                                                encoding="utf-8")
        self.assertEqual(
            git("status", "--porcelain", cwd=wt).stdout.strip(), "")
        self.assertEqual(
            git("status", "--porcelain", cwd=self.live).stdout.strip(), "")

    def test_the_owners_rules_dirty_neither_tree(self):
        """AGENTS.local.md sits in live and is LINKED into every worktree.
        The merge preflights the worktree's status strictly, untracked files
        included, so without the .gitignore entry this link would refuse
        every merge - and in live it would be one `git add -A` from public."""
        r = run_in(self.live, 'cmd_start feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        wt = self.live / ".worktrees" / "feat"
        self.assertTrue((wt / "AGENTS.local.md").is_symlink())
        self.assertEqual(
            git("status", "--porcelain", cwd=wt).stdout.strip(), "")
        self.assertEqual(
            git("status", "--porcelain", cwd=self.live).stdout.strip(), "")


@posix_only
class Provisioning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # resolve(): macOS hands out /var/... temp dirs that git reports back
        # as /private/var/..., and the paths are compared as strings here
        self.root = Path(self.tmp.name).resolve()
        self.live = self.root / "vira"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "base", cwd=self.live)
        (self.live / "AGENTS.local.md").write_text("the owner's rules",
                                                    encoding="utf-8")
        (self.live / ".venv").mkdir()
        (self.live / ".claude").mkdir()
        (self.live / ".claude" / "launch.json").write_text("{}")
        self.wt = self.live / ".claude" / "worktrees" / "feat"
        self.wt.parent.mkdir(parents=True)
        git("worktree", "add", "-q", "-b", "claude/feat", str(self.wt), "main",
            cwd=self.live)

    def test_adopt_installs_the_gitignored_pieces(self):
        # the state a harness-made worktree starts in: no rules, no venv
        self.assertFalse((self.wt / "AGENTS.local.md").exists())
        self.assertFalse((self.wt / ".venv").exists())
        r = run_in(self.live, 'cmd_adopt feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        rules = self.wt / "AGENTS.local.md"
        self.assertTrue(rules.is_symlink(), "a copy, not a link")
        self.assertEqual(rules.resolve(),
                         (self.live / "AGENTS.local.md").resolve())
        self.assertTrue((self.wt / ".venv").is_symlink())
        self.assertEqual((self.wt / ".venv").resolve(),
                         (self.live / ".venv").resolve())
        self.assertTrue((self.wt / ".claude" / "launch.json").exists())

    def test_an_edit_in_the_worktree_is_the_owners_real_file(self):
        """The reason it is a link: one file, so nothing waits to be ported
        back by hand at merge time."""
        run_in(self.live, 'cmd_adopt feat')
        (self.wt / "AGENTS.local.md").write_text("a rule a session added",
                                                  encoding="utf-8")
        self.assertEqual(
            (self.live / "AGENTS.local.md").read_text(encoding="utf-8"),
            "a rule a session added")

    def test_provision_never_clobbers_a_worktree_copy(self):
        (self.wt / "AGENTS.local.md").write_text("this worktree's own",
                                                  encoding="utf-8")
        r = run_in(self.live, f'provision "{self.wt}"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.wt / "AGENTS.local.md").is_symlink())
        self.assertEqual(
            (self.wt / "AGENTS.local.md").read_text(encoding="utf-8"),
            "this worktree's own")

    def test_no_live_rules_means_a_note_not_a_dangling_link(self):
        (self.live / "AGENTS.local.md").unlink()
        r = run_in(self.live, f'provision "{self.wt}"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.wt / "AGENTS.local.md").is_symlink())
        self.assertIn("no AGENTS.local.md", r.stderr)

    def test_provision_is_idempotent(self):
        for _ in range(2):
            r = run_in(self.live, f'provision "{self.wt}"')
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.wt / ".venv").is_symlink())

    def test_adopt_refuses_the_live_tree(self):
        r = run_in(self.live, 'cmd_adopt')     # no slug = adopt cwd
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("live tree", r.stderr)


@posix_only
class MergeChecklistRulesNote(unittest.TestCase):
    """A worktree that never received AGENTS.local.md means the session
    worked without the owner's rules. The merge says so, but never holds the
    teardown for it: the file is a link, so there is nothing to port back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.live = self.root / "vira"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        # merge preflights both trees clean, so the fixture needs the real
        # repo's ignores for the provisioned pieces
        (self.live / ".gitignore").write_text(
            "AGENTS.local.md\n.venv\n.claude/\n", encoding="utf-8")
        git("add", ".gitignore", cwd=self.live)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "base", cwd=self.live)
        (self.live / "AGENTS.local.md").write_text("the owner's rules",
                                                    encoding="utf-8")
        (self.live / ".venv").mkdir()
        (self.live / ".claude").mkdir()
        (self.live / ".claude" / "launch.json").write_text("{}")
        self.wt = self.live / ".claude" / "worktrees" / "feat"
        self.wt.parent.mkdir(parents=True)
        git("worktree", "add", "-q", "-b", "claude/feat", str(self.wt), "main",
            cwd=self.live)
        # something to merge
        (self.wt / "f.txt").write_text("work")
        git("add", "f.txt", cwd=self.wt)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "-m", "work", cwd=self.wt)

    def test_names_a_worktree_that_never_had_the_rules(self):
        r = run_in(self.live, 'cmd_merge feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NO AGENTS.local.md", r.stdout)
        self.assertIn("adopt", r.stdout)

    def test_quiet_when_the_rules_were_linked(self):
        (self.wt / "AGENTS.local.md").symlink_to(
            self.live / "AGENTS.local.md")
        r = run_in(self.live, 'cmd_merge feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("AGENTS.local.md", r.stdout)
        self.assertNotIn("HELD", r.stdout)
        self.assertEqual(
            (self.live / "AGENTS.local.md").read_text(encoding="utf-8"),
            "the owner's rules")


if __name__ == "__main__":
    unittest.main()

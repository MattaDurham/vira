"""The AGENTS files: a thin public router, the install file, the private
operating file seeded from a public template, and the tool that keeps the
private copy and the template in step without ever overwriting the owner's.

AGENTS.md is the one file every agent harness loads first (Claude Code,
Codex, Cursor), so it is kept to what every agent needs and routes the rest.
These pin the routing, the content each route must still carry, and the
seed/diff/adopt/promote contract of server/agentslocal.py.

Run: .venv/bin/python -m unittest tests.test_agents_files
"""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from server import agentslocal

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


class TheRouter(unittest.TestCase):
    def test_it_stays_thin(self):
        """Every session loads it; install detail and coding rules live
        behind the routes, not here."""
        self.assertLessEqual(len(read("AGENTS.md").splitlines()), 60)

    def test_every_route_resolves(self):
        text = read("AGENTS.md")
        for target in ("AGENTS.install.md", "AGENTS.local.md",
                       "AGENTS.local.example.md"):
            self.assertIn(target, text)
        self.assertTrue((ROOT / "AGENTS.install.md").is_file())
        self.assertTrue((ROOT / "AGENTS.local.example.md").is_file())

    def test_the_rules_that_must_not_wait_for_a_hop_are_in_it(self):
        """An agent can read the router and start working without following
        it, so the coding safety rules sit on the first rung."""
        text = read("AGENTS.md")
        for rule in ("scripts/branch.sh start", "live checkout",
                     "Never restart", "Merge and push only when the owner",
                     "Personal data never enters git"):
            self.assertIn(rule, text)

    def test_it_never_sends_an_agent_to_claude_md(self):
        text = read("AGENTS.md")
        self.assertIn("Do not create `CLAUDE.md`", text)
        self.assertEqual(text.count("CLAUDE.md"), 1)


class TheInstallFile(unittest.TestCase):
    def test_it_carries_the_whole_install(self):
        text = read("AGENTS.install.md")
        for piece in ("bash scripts/agent-install.sh", "scripts\\run.ps1",
                      "localhost:8377/api/onboard/steps", "If something fails"):
            self.assertIn(piece, text)

    def test_the_front_doors_point_at_it(self):
        self.assertIn("AGENTS.install.md", read("README.md"))
        self.assertIn("AGENTS.install.md", read("scripts/agent-install.sh"))


class TheTemplate(unittest.TestCase):
    def test_it_has_every_section(self):
        text = read("AGENTS.local.example.md")
        for heading in ("## 1. Adding local content", "## 2. Changing the code",
                        "## 3. Finishing a branch",
                        "## 4. Public code and private state",
                        "## 5. Coding rules", "## 6. This install"):
            self.assertIn(heading, text)

    def test_merge_it_is_the_owners_word(self):
        text = read("AGENTS.local.example.md")
        self.assertIn('"merge it" from the owner', text)
        self.assertIn("Never merge or push without that word", text)

    def test_it_carries_no_machine_specifics(self):
        """It is public and every install starts from it."""
        text = read("AGENTS.local.example.md")
        for leak in ("/Users/", "/home/", "C:\\Users", "gui/501"):
            self.assertNotIn(leak, text)

    def test_the_private_copy_is_ignored(self):
        lines = read(".gitignore").splitlines()
        self.assertIn("AGENTS.local.md", lines)


class Seeding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.example = self.root / "AGENTS.local.example.md"
        self.mine = self.root / "AGENTS.local.md"
        self.base = self.root / "data" / "agents-local-base.md"
        self.example.write_text("rule one\nrule two\n", encoding="utf-8")

    def test_seed_copies_the_template_and_records_the_baseline(self):
        self.assertEqual(agentslocal.seed(self.root), "seeded")
        self.assertEqual(self.mine.read_text(encoding="utf-8"),
                         "rule one\nrule two\n")
        self.assertEqual(self.base.read_text(encoding="utf-8"),
                         "rule one\nrule two\n")

    def test_seed_never_overwrites_the_owners_copy(self):
        self.mine.write_text("mine\n", encoding="utf-8")
        self.assertEqual(agentslocal.seed(self.root), "present")
        self.assertEqual(self.mine.read_text(encoding="utf-8"), "mine\n")
        self.assertFalse(self.base.exists())

    @unittest.skipUnless(os.name == "posix",
                         "symlinks need extra privileges on Windows")
    def test_a_link_counts_as_present_even_when_dangling(self):
        self.mine.symlink_to(self.root / "elsewhere.md")
        self.assertEqual(agentslocal.seed(self.root), "present")
        self.assertFalse((self.root / "elsewhere.md").exists())

    def test_a_worktree_is_never_seeded(self):
        """branch.sh links live's copy into a worktree; a seeded second copy
        there would quietly take an agent's edits away from the owner's."""
        (self.root / ".git").write_text("gitdir: /somewhere\n",
                                        encoding="utf-8")
        self.assertEqual(agentslocal.seed(self.root), "worktree")
        self.assertFalse(self.mine.exists())

    def test_no_template_means_nothing_to_seed(self):
        self.example.unlink()
        self.assertEqual(agentslocal.seed(self.root), "no-template")
        self.assertFalse(self.mine.exists())


class KeepingInStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.example = self.root / "AGENTS.local.example.md"
        self.mine = self.root / "AGENTS.local.md"
        self.example.write_text("rule one\nrule two\n", encoding="utf-8")
        agentslocal.seed(self.root)

    def test_in_step_right_after_seeding(self):
        s = agentslocal.status(self.root)
        self.assertFalse(s["template_changed"])
        self.assertEqual(s["local_only_lines"], 0)
        self.assertEqual(agentslocal.diff(self.root)[0], "")

    def test_a_template_change_shows_and_the_copy_is_untouched(self):
        self.example.write_text("rule one\nrule two\nrule three\n",
                                encoding="utf-8")
        self.assertTrue(agentslocal.status(self.root)["template_changed"])
        text, note = agentslocal.diff(self.root)
        self.assertIn("+rule three", text)
        self.assertEqual(note, "")
        self.assertEqual(self.mine.read_text(encoding="utf-8"),
                         "rule one\nrule two\n")

    def test_adopt_moves_the_baseline_never_the_copy(self):
        self.mine.write_text("rule one\nmy own rule\n", encoding="utf-8")
        self.example.write_text("rule one\nrule two\nrule three\n",
                                encoding="utf-8")
        self.assertTrue(agentslocal.adopt(self.root))
        self.assertFalse(agentslocal.status(self.root)["template_changed"])
        self.assertEqual(self.mine.read_text(encoding="utf-8"),
                         "rule one\nmy own rule\n")

    def test_promote_lists_only_the_owners_own_lines(self):
        """Line-set containment, so a moved template line is not new."""
        self.mine.write_text("rule two\nrule one\n\nmy own rule\n",
                             encoding="utf-8")
        self.assertEqual(agentslocal.promote(self.root), [(4, "my own rule")])

    def test_with_no_baseline_diff_says_what_it_compared(self):
        (self.root / "data" / "agents-local-base.md").unlink()
        self.example.write_text("rule one\nrule two\nrule three\n",
                                encoding="utf-8")
        text, note = agentslocal.diff(self.root)
        self.assertIn("+rule three", text)
        self.assertIn("whole difference", note)
        self.assertIsNone(agentslocal.status(self.root)["template_changed"])

    def test_the_cli_runs_every_command(self):
        for cmd in ("seed", "status", "diff", "adopt", "promote"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = agentslocal.main([cmd, "--root", str(self.root)])
            self.assertEqual(code, 0, cmd)
            self.assertTrue(out.getvalue().strip(), cmd)


if __name__ == "__main__":
    unittest.main()

"""scripts/preflight.sh — the executable form of the process's own lessons.

The most important test here is REGISTRY CONTRACT: every check must carry a
description, the incident that earned it, and a fix. That is what stops this
from decaying back into prose — a check nobody can act on is a complaint, and a
check with no incident behind it is a style rule that will be argued about.

Run: .venv/bin/python -m unittest tests.test_preflight
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight.sh"
DEPS = ROOT / "scripts" / "preflight_deps.py"
BRANCH_SH = ROOT / "scripts" / "branch.sh"
BASH = shutil.which("bash") or "/bin/bash"

posix_only = unittest.skipUnless(os.name == "posix", "bash script")


def run(*args, cwd=ROOT, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["bash", str(PREFLIGHT), *args], cwd=str(cwd),
                          capture_output=True, text=True, env=e)


@posix_only
class RegistryContract(unittest.TestCase):
    """Adding a lesson means adding a ROW — and a row is all four parts."""

    def setUp(self):
        self.src = PREFLIGHT.read_text(encoding="utf-8")
        m = re.search(r"^CHECKS=\(([^)]*)\)", self.src, re.M)
        self.assertIsNotNone(m, "CHECKS registry not found")
        self.ids = m.group(1).split()
        self.assertTrue(self.ids, "registry is empty")

    def _has(self, pattern):
        # re.M: these are line-anchored declarations. assertRegex would not
        # only miss them, it would dump the whole script into the failure.
        return re.search(pattern, self.src, re.M) is not None

    def test_every_check_has_all_four_parts(self):
        for cid in self.ids:
            for part in ("desc", "incident", "fix"):
                self.assertTrue(
                    self._has(rf"^{part}_{cid}="),
                    f"check '{cid}' has no {part}_ — a check without one is not "
                    f"actionable, see the header of preflight.sh")
            self.assertTrue(self._has(rf"^check_{cid}\(\)"),
                            f"check '{cid}' is registered but not implemented")

    def test_every_incident_is_dated(self):
        """An incident without a date is a hunch. Each must cite when it bit."""
        for cid in self.ids:
            m = re.search(rf'^incident_{cid}="(.*?)"', self.src, re.M | re.S)
            self.assertIsNotNone(m, cid)
            self.assertRegex(m.group(1), r"\d{4}-\d{2}-\d{2}",
                             f"incident_{cid} cites no date")

    def test_list_names_every_check(self):
        r = run("--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        for cid in self.ids:
            self.assertIn(cid, r.stdout)


@posix_only
class DepsCheck(unittest.TestCase):
    """The Pillow class: a module the code imports but nothing declares."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "server").mkdir()
        (self.root / "tests").mkdir()
        shutil.copy(DEPS, self.root / "scripts" / "preflight_deps.py")
        (self.root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    def _run(self):
        return subprocess.run(
            ["python3", str(self.root / "scripts" / "preflight_deps.py")],
            capture_output=True, text=True)

    def test_undeclared_import_fails_and_names_the_file(self):
        (self.root / "server" / "x.py").write_text(
            "import fastapi\nimport nowhere_declared\n", encoding="utf-8")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nowhere_declared", r.stdout)
        self.assertIn("server/x.py", r.stdout)

    def test_declared_import_passes(self):
        (self.root / "server" / "x.py").write_text("import fastapi\n", encoding="utf-8")
        self.assertEqual(self._run().returncode, 0)

    def test_stdlib_and_relative_imports_are_not_flagged(self):
        (self.root / "server" / "x.py").write_text(
            "import json, os, sqlite3\nfrom . import sibling\n", encoding="utf-8")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_alias_maps_import_name_to_distribution(self):
        """PIL is declared as 'pillow' — the incident that started this."""
        (self.root / "requirements.txt").write_text("pillow\n", encoding="utf-8")
        (self.root / "server" / "x.py").write_text("from PIL import Image\n",
                                                   encoding="utf-8")
        self.assertEqual(self._run().returncode, 0)

    def test_extras_marker_does_not_hide_a_real_import(self):
        """uvicorn[standard] declares 'uvicorn'."""
        (self.root / "requirements.txt").write_text("uvicorn[standard]\n", encoding="utf-8")
        (self.root / "server" / "x.py").write_text("import uvicorn\n", encoding="utf-8")
        self.assertEqual(self._run().returncode, 0)


@posix_only
class Ratchet(unittest.TestCase):
    """Pre-existing debt is tolerated; NEW debt is not."""

    def test_baseline_matches_reality_right_now(self):
        """If this drifts, the ratchet is silently either useless or blocking."""
        r = run("encoding")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("at baseline", r.stdout,
                      "baseline is stale — update scripts/preflight-baseline.txt")

    def test_baseline_file_is_parseable_and_nonzero(self):
        txt = (ROOT / "scripts" / "preflight-baseline.txt").read_text(encoding="utf-8")
        rows = [l.split() for l in txt.splitlines()
                if l.strip() and not l.startswith("#")]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row), 2, row)
            self.assertTrue(row[1].isdigit(), row)


@posix_only
class BaseCheck(unittest.TestCase):
    """The incident this whole file exists because of: a branch whose base was
    rewritten out from under it. A plain merge contributed 146 commits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "server").mkdir()
        (self.root / "tests").mkdir()
        for f in ("preflight.sh", "preflight-baseline.txt",
                  "preflight_deps.py", "preflight_encoding.py"):
            shutil.copy(ROOT / "scripts" / f, self.root / "scripts" / f)
        shutil.copy(ROOT / "scripts" / "check-pii.sh", self.root / "scripts")
        (self.root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        self.base = self._out("rev-parse", "main")

    def _git(self, *a):
        return subprocess.run(["git", *a], cwd=str(self.root),
                              capture_output=True, text=True)

    def _out(self, *a):
        return self._git(*a).stdout.strip()

    def _record(self, slug, sha):
        d = self.root / ".git" / "vira-bases"
        d.mkdir(parents=True, exist_ok=True)
        (d / slug).write_text(sha, encoding="utf-8")

    def _preflight(self, slug):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "preflight.sh"), "base"],
            cwd=str(self.root), capture_output=True, text=True,
            env={**os.environ, "PREFLIGHT_SLUG": slug})

    def test_live_base_passes(self):
        self._git("branch", "claude/feat", "main")
        self._record("feat", self.base)
        r = self._preflight("feat")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("still in main", r.stdout)

    def test_rewritten_base_fails_and_prints_the_onto_fix(self):
        """main is rewritten; the branch's recorded base is no longer in it."""
        self._git("branch", "claude/feat", "main")
        self._record("feat", self.base)
        # rewrite main: amend the root so every sha changes
        self._git("commit", "-q", "--amend", "-m", "base (rewritten)")
        r = self._preflight("feat")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("history was rewritten", r.stdout)
        self.assertIn("--onto", r.stdout)
        self.assertNotIn("git rebase main\n", r.stdout)

    def test_unrecorded_base_falls_back_to_the_count_tell(self):
        """Legacy branches have no record; a huge contribution is the signal."""
        self._git("branch", "claude/legacy", "main")
        self._git("checkout", "-q", "claude/legacy")
        for i in range(30):
            (self.root / f"f{i}").write_text("x", encoding="utf-8")
            self._git("add", "-A")
            self._git("commit", "-qm", f"c{i}")
        self._git("checkout", "-q", "main")
        r = self._preflight("legacy")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("base is almost certainly dead", r.stdout)


@posix_only
class CiCheck(unittest.TestCase):
    """2026-07-28: red CI blocked nothing, so red CI changed nothing — two
    branches merged with a failing Windows job hours apart. This check is the
    close. Its hard part is not detecting failure, it is NOT crying wolf: a
    run in flight, an unpushed commit, and CI grading itself are all normal
    states that must never block a merge, or the gate gets routed around and
    the real red goes with it. gh is stubbed so these are offline and exact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy(PREFLIGHT, self.root / "scripts" / "preflight.sh")
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(self.root))
        for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "config", k, v], cwd=str(self.root))
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root))
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(self.root))

    def _stub_gh(self, payload):
        """A fake gh that answers `run list` with `payload` and `run view`
        with one failing job line."""
        (self.bin / "runs.json").write_text(payload, encoding="utf-8")
        gh = self.bin / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            f'  *"run list"*) cat {self.bin / "runs.json"} ;;\n'
            '  *"run view"*) printf "        failure\\ttest-windows\\n" ;;\n'
            "esac\n", encoding="utf-8")
        gh.chmod(0o755)

    def _run(self, path=None):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
        env["PATH"] = (f"{self.bin}:{os.environ['PATH']}" if path is None
                       else path)
        # bash by absolute path: one case deliberately empties PATH, and
        # resolving the interpreter through it would fail the test for the
        # wrong reason.
        return subprocess.run(
            [BASH, str(self.root / "scripts" / "preflight.sh"), "ci"],
            cwd=str(self.root), capture_output=True, text=True, env=env)

    def test_a_red_run_blocks_and_names_the_failing_job(self):
        self._stub_gh('[{"status":"completed","conclusion":"failure",'
                      '"databaseId":42}]')
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("CI is failure", r.stdout)
        self.assertIn("test-windows", r.stdout)
        self.assertIn("--log-failed 42", r.stdout)

    def test_a_green_run_passes(self):
        self._stub_gh('[{"status":"completed","conclusion":"success",'
                      '"databaseId":7}]')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("CI green", r.stdout)

    def test_a_run_still_in_flight_warns_but_does_not_block(self):
        self._stub_gh('[{"status":"in_progress","conclusion":null,'
                      '"databaseId":9}]')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("WARN", r.stdout)
        self.assertIn("no verdict yet", r.stdout)

    def test_an_unpushed_commit_warns_but_does_not_block(self):
        """Working locally before a first push is normal, not an error."""
        self._stub_gh("[]")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("has not run", r.stdout)

    def test_missing_gh_warns_but_does_not_block(self):
        """A check that cannot run says so; it does not fail the merge."""
        r = self._run(path=str(self.bin))          # empty dir: no gh anywhere
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("gh is not installed", r.stdout)

    def test_inside_ci_the_check_skips_itself(self):
        """A run cannot grade itself — otherwise --all is circular in CI."""
        self._stub_gh('[{"status":"completed","conclusion":"failure",'
                      '"databaseId":42}]')
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        env["GITHUB_ACTIONS"] = "true"
        r = subprocess.run(
            ["bash", str(self.root / "scripts" / "preflight.sh"), "ci"],
            cwd=str(self.root), capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("cannot grade itself", r.stdout)

    def test_ci_is_in_the_pre_merge_gate(self):
        """The whole point: it has to run where merges happen."""
        src = PREFLIGHT.read_text(encoding="utf-8")
        m = re.search(r"^PRE_MERGE=\(([^)]*)\)", src, re.M)
        self.assertIsNotNone(m)
        self.assertIn("ci", m.group(1).split())


@posix_only
class PiiHonesty(unittest.TestCase):
    """The false comfort was the real bug: a pass must state its strength."""

    def test_pass_states_which_mode_it_ran_in(self):
        r = run("pii")
        self.assertTrue(re.search(r"FULL|REDUCED", r.stdout),
                        "the PII check must name its strength on a PASS, not "
                        "only on a failure")

    def test_reduced_mode_is_a_warning_not_a_silent_pass(self):
        if (ROOT / "data" / "pii-patterns.txt").is_file():
            self.skipTest("this checkout has the patterns file (FULL mode)")
        r = run("pii")
        self.assertIn("REDUCED", r.stdout)
        self.assertIn("does NOT mean", r.stdout)



@posix_only
class PiiBranchScan(unittest.TestCase):
    """2026-08-29: a name INTRODUCED by a branch was invisible to the merge
    gate. --pre-merge runs the LIVE copy of preflight, so ROOT is the live
    checkout and the branch's new files are not in it yet; the branch's own
    worktree has the files but no data/pii-patterns.txt (gitignored, so it
    lives only in live). The full-strength scan and the new files never met.

    The fixture name is deliberately invented - planting a real one here would
    trip this repo's own guard on this very file.
    """
    NAME = "Zoltan Kovacs"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "data").mkdir()
        (self.root / "tests").mkdir()
        for f in ("preflight.sh", "preflight-baseline.txt", "preflight_deps.py",
                  "preflight_encoding.py", "check-pii.sh"):
            shutil.copy(ROOT / "scripts" / f, self.root / "scripts" / f)
        (self.root / "data" / "pii-patterns.txt").write_text(
            "\\b%s\\b\n" % self.NAME, encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.root / "clean.txt").write_text("nothing here\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        # the branch, in its own worktree - the real shape
        self.wt = self.root / ".worktrees" / "feat"
        self._git("worktree", "add", "-q", "-b", "claude/feat", str(self.wt), "main")

    def _git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=str(cwd or self.root),
                              capture_output=True, text=True)

    def _commit_on_branch(self, relpath, text):
        f = self.wt / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        self._git("add", "-A", cwd=self.wt)
        self._git("commit", "-qm", "add", cwd=self.wt)

    def _preflight(self, slug="feat"):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "preflight.sh"), "pii"],
            cwd=str(self.root), capture_output=True, text=True,
            env={**os.environ, "PREFLIGHT_SLUG": slug})

    def test_a_name_only_on_the_branch_is_caught(self):
        self._commit_on_branch("tests/fixture.py", 'owner = "%s"\n' % self.NAME)
        r = self._preflight()
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("BRANCH tree", r.stdout)
        self.assertIn("tests/fixture.py", r.stdout)

    def test_the_live_tree_alone_would_have_passed(self):
        """Pins WHY the branch scan is needed: the live tree is clean the whole
        time. Without this, the case above could pass for the wrong reason."""
        self._commit_on_branch("tests/fixture.py", 'owner = "%s"\n' % self.NAME)
        live = subprocess.run(["sh", str(self.root / "scripts" / "check-pii.sh"), "--tree"],
                              cwd=str(self.root), capture_output=True, text=True)
        self.assertEqual(live.returncode, 0, live.stdout + live.stderr)

    def test_a_clean_branch_passes(self):
        self._commit_on_branch("tests/fixture.py", "owner = 'nobody'\n")
        r = self._preflight()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("clean", r.stdout)

    def test_a_missing_worktree_warns_rather_than_silently_passing(self):
        r = self._preflight(slug="no-such-branch")
        self.assertIn("only the live tree was scanned", r.stdout)



@posix_only
class TreeCheck(unittest.TestCase):
    """2026-10-27: the module tree became a gate (tests/test_moduletree.py)
    while another branch was in flight. Each branch was consistent on its
    own and each had green CI; only the MERGED tree had a README that no
    longer matched the code, so main's CI went red after the push. The merge
    gate had never run the tree check, and a check of the branch tip alone
    could not have caught it: the stale page exists only where the two meet.

    These rebuild that shape in a throwaway repo and drive the real check.
    main has no module tree at first, the branch introduces it (PR 148's
    shape), and main gains a route meanwhile. A branch behind main is
    refused until it is rebased; only then is the worktree the tree the
    merge would produce, and that is what gets judged.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "server").mkdir()
        (self.root / "tests").mkdir()
        # Everything --pre-merge runs, so the join test can drive the real
        # cmd_merge against this root as LIVE.
        for f in ("preflight.sh", "preflight-baseline.txt", "preflight_deps.py",
                  "preflight_encoding.py", "preflight_capdoc.py",
                  "check-pii.sh"):
            shutil.copy(ROOT / "scripts" / f, self.root / "scripts" / f)
        (self.root / "requirements.txt").write_text("fastapi\n",
                                                    encoding="utf-8")
        (self.root / ".gitignore").write_text(".worktrees/\n__pycache__/\n",
                                              encoding="utf-8")
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        (self.root / "server" / "alpha.py").write_text(
            '"""Alpha - the first thing."""\n', encoding="utf-8")
        (self.root / "tests" / "test_alpha.py").write_text(
            '"""Alpha tests."""\n', encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        # The branch, in its own worktree, introduces the tree - consistent
        # with the code as it forked.
        self.wt = self.root / ".worktrees" / "feat"
        self._git("worktree", "add", "-q", "-b", "claude/feat", str(self.wt),
                  "main")
        shutil.copy(ROOT / "server" / "moduletree.py",
                    self.wt / "server" / "moduletree.py")
        (self.wt / "server" / "__init__.py").write_text("", encoding="utf-8")
        tree = self.wt / "modules"
        self._write(tree / "README.md",
                    "# Vira modules\n\nVira > Modules\n\nprose\n")
        self._write(tree / "one" / "module.json", json.dumps({
            "id": "one", "name": "One", "group": "operate", "rank": 10,
            "summary": "The first module.", "windows": ["one"],
            "submodules": ["alpha"]}))
        self._write(tree / "one" / "README.md",
                    "# One\n\nVira > [Modules](../README.md) > One\n\nprose\n")
        self.manifest = tree / "one" / "alpha" / "module.json"
        self._write(self.manifest, json.dumps({
            "id": "one/alpha", "name": "Alpha", "summary": "Alpha.",
            "owns": ["server/alpha.py", "server/__init__.py",
                     "server/moduletree.py", "tests/test_alpha.py",
                     "scripts/**"],
            "routes": ["/api"], "stores": [], "reads": [], "feeds": [],
            "registry": []}))
        self._write(tree / "one" / "alpha" / "README.md",
                    "# Alpha\n\nVira > [Modules](../../README.md) > "
                    "[One](../README.md) > Alpha\n\nprose\n")
        self._moduletree("index", "--write")
        self._commit(self.wt, "module tree")

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=str(cwd or self.root),
                              capture_output=True, text=True)

    def _commit(self, cwd, msg):
        self._git("add", "-A", cwd=cwd)
        r = self._git("commit", "-qm", msg, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _moduletree(self, *args):
        """The branch's own moduletree CLI, run in its worktree."""
        return subprocess.run(
            ["python3", "-m", "server.moduletree", *args], cwd=str(self.wt),
            capture_output=True, text=True)

    def _place(self, relpath):
        """Add a file to the manifest's owns list (no README refresh)."""
        m = json.loads(self.manifest.read_text(encoding="utf-8"))
        m["owns"].append(relpath)
        self.manifest.write_text(json.dumps(m), encoding="utf-8")

    def _main_gains_a_route(self):
        """The in-flight branch's contribution: a route the README that
        lists routes has never seen. Written in the live root, where main
        is checked out."""
        (self.root / "server" / "alpha.py").write_text(
            '"""Alpha - the first thing."""\n\n'
            '@app.get("/api/new")\ndef new():\n    return {}\n',
            encoding="utf-8")
        self._commit(self.root, "alpha: a new route")

    def _preflight(self, slug="feat"):
        """preflight.sh's `tree` check as cmd_merge runs it: the live root's
        copy, from the live root, with the branch in context. slug="" is
        the no-branch form CI's --all runs."""
        env = {k: v for k, v in os.environ.items() if k != "PREFLIGHT_SLUG"}
        if slug:
            env["PREFLIGHT_SLUG"] = slug
        return subprocess.run(
            [BASH, str(self.root / "scripts" / "preflight.sh"), "tree"],
            cwd=str(self.root), capture_output=True, text=True, env=env)

    def _branch_sh_merge(self):
        """The real cmd_merge, sourced from the branch's branch.sh with this
        root as LIVE (the test_branch_merge_gate shape): the PR door passed
        with its own conscious override, the instance and PR sync stubbed,
        gh stubbed to 'no run yet', and the merge itself replaced by a
        sentinel so the assertion is about the gate, not a real merge."""
        if not shutil.which("zsh"):
            self.skipTest("branch.sh is zsh")
        gh = self.bin / "gh"
        gh.write_text('#!/bin/sh\ncase "$*" in *"run list"*) echo "[]";; esac\n',
                      encoding="utf-8")
        gh.chmod(0o755)
        script = f"""
        set -u
        LIVE={self.root}
        source {BRANCH_SH} >/dev/null 2>&1 || true
        LIVE={self.root}
        export VIRA_SKIP_PR=1
        wt_dir() {{ echo "{self.wt}"; }}
        instance_pid() {{ echo ""; }}
        stop_test_process() {{ :; }}
        pr_sync_head() {{ return 0; }}
        git() {{
          if [[ "$2" == "{self.root}" && "$3" == "merge" ]]; then
            echo "PAST_PREFLIGHT"; exit 7
          fi
          command git "$@"
        }}
        cmd_merge feat
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_ACTIONS", "PREFLIGHT_SLUG",
                            "VIRA_SKIP_PREFLIGHT")}
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        return subprocess.run([shutil.which("zsh"), "-c", script],
                              capture_output=True, text=True, env=env)

    # -- the gate --------------------------------------------------------

    def test_branch_sh_merge_refuses_a_stale_tree_and_prints_the_fix(self):
        """The join, not the halves: cmd_merge runs --pre-merge, --pre-merge
        runs tree, and the refusal reaches the agent with the command to
        run before any merge starts."""
        self._write(self.wt / "server" / "gamma.py", '"""Gamma."""\n')
        self._place("server/gamma.py")
        self._commit(self.wt, "stale on the branch")
        r = self._branch_sh_merge()
        out = r.stdout + r.stderr
        self.assertNotIn("PAST_PREFLIGHT", out, out)
        self.assertIn("merge refused", out)
        self.assertIn("FAIL  tree", out)
        self.assertIn("python -m server.moduletree index --write", out)

    def test_branch_sh_merge_lets_a_current_tree_through(self):
        """Pins that the gate is a gate and not a wall."""
        r = self._branch_sh_merge()
        out = r.stdout + r.stderr
        self.assertIn("ok    tree", out, out)
        self.assertIn("PAST_PREFLIGHT", out, out)


    def test_a_branch_that_contains_main_and_is_current_passes(self):
        r = self._preflight()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("complete and current", r.stdout)
        self.assertIn("worktree", r.stdout)

    def test_a_branch_behind_main_is_refused_with_the_rebase_fix(self):
        """The tree the merge would produce exists nowhere yet, so nothing
        can have checked it. The fix names the worktree."""
        (self.root / "tests" / "test_alpha.py").write_text(
            '"""Alpha tests, more of them."""\n', encoding="utf-8")
        self._commit(self.root, "main moves")
        r = self._preflight()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 commit(s) claude/feat lacks", r.stdout)
        # git reports the worktree by its real path (/private/var on a Mac)
        self.assertIn(f"cd {self.wt.resolve()} && git rebase main", r.stdout)

    def test_the_incident_is_caught_on_the_merged_tree_and_nowhere_else(self):
        """The whole story: main gains a route while the branch that
        introduced the tree is in flight. The branch tip alone passes its
        own check (the pin: this is why a tip check could not have caught
        it); the gate refuses the branch as behind; after the rebase the
        worktree is the merged tree and the README is stale there; the
        printed fix makes it pass."""
        self._main_gains_a_route()
        tip = self._moduletree("check")
        self.assertEqual(tip.returncode, 0,
                         "the branch tip must be consistent on its own, or "
                         "this test is not reproducing the incident\n"
                         + tip.stdout)
        r = self._preflight()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("git rebase main", r.stdout)

        rb = self._git("rebase", "main", cwd=self.wt)
        self.assertEqual(rb.returncode, 0, rb.stdout + rb.stderr)
        r = self._preflight()
        self.assertEqual(r.returncode, 1,
                         "after the rebase the worktree IS the merged tree, "
                         "and its README does not list the new route\n"
                         + r.stdout)
        self.assertIn("generated block is stale", r.stdout)
        self.assertIn("modules/one/alpha/README.md", r.stdout)
        self.assertIn("python -m server.moduletree index --write", r.stdout)

        self.assertEqual(self._moduletree("index", "--write").returncode, 0)
        self._commit(self.wt, "module tree: refresh")
        r = self._preflight()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_unplaced_file_on_the_branch_is_refused_and_named(self):
        self._write(self.wt / "server" / "gamma.py", '"""Gamma."""\n')
        self._commit(self.wt, "gamma")
        r = self._preflight()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("unplaced: server/gamma.py", r.stdout)
        self.assertIn("module.json", r.stdout)

    def test_a_stale_readme_on_the_branch_is_refused_with_the_index_command(self):
        self._write(self.wt / "server" / "gamma.py", '"""Gamma."""\n')
        self._place("server/gamma.py")
        self._commit(self.wt, "gamma, placed but not indexed")
        r = self._preflight()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("generated block is stale", r.stdout)
        self.assertIn("index --write", r.stdout)

    def test_a_file_not_yet_committed_in_the_worktree_is_seen(self):
        """A standalone run judges the working files, so a session learns
        about the file it just wrote before it commits, not at the merge."""
        self._write(self.wt / "server" / "gamma.py", '"""Gamma."""\n')
        r = self._preflight()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("unplaced: server/gamma.py", r.stdout)

    def test_the_live_tree_alone_would_have_passed(self):
        """Pins WHY the branch's tree is the one judged: the live tree has
        nothing wrong with it the whole time."""
        self._write(self.wt / "server" / "gamma.py", '"""Gamma."""\n')
        self._place("server/gamma.py")
        self._commit(self.wt, "stale on the branch")
        r = self._preflight(slug="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("FAIL", r.stdout)

    def test_without_a_worktree_the_branch_tip_is_exported_and_judged(self):
        """cmd_merge merges the branch ref whether or not a worktree exists;
        the gate must not read 'no worktree' as 'nothing to check'."""
        self._write(self.wt / "server" / "gamma.py", '"""Gamma."""\n')
        self._place("server/gamma.py")
        self._commit(self.wt, "stale on the branch")
        rm = self._git("worktree", "remove", "--force", str(self.wt))
        self.assertEqual(rm.returncode, 0, rm.stdout + rm.stderr)
        r = self._preflight()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("exported tip", r.stdout)
        self.assertIn("generated block is stale", r.stdout)

    def test_a_checkout_without_a_tree_warns_and_does_not_block(self):
        """The no-branch form on a tree that has no modules/: say so, do not
        read an absent check as a pass, and do not block either."""
        r = self._preflight(slug="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WARN", r.stdout)
        self.assertIn("no server/moduletree.py", r.stdout)

    def test_tree_is_in_the_pre_merge_gate(self):
        """The point: it has to run where merges happen."""
        src = PREFLIGHT.read_text(encoding="utf-8")
        m = re.search(r"^PRE_MERGE=\(([^)]*)\)", src, re.M)
        self.assertIsNotNone(m)
        self.assertIn("tree", m.group(1).split())



if __name__ == "__main__":
    unittest.main()

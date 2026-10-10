"""Why a session stopped, and the Resume prompt that carries it.

The incident these pin (2026-08-28): three sessions on one branch died at
the identical instant — each ran Edit on static/app.js, which is 1,062,221
bytes against the SDK's 1,048,576-byte NDJSON line ceiling — and the only
record anywhere was a truncated error string per row. The prompt said
"carry the work to done" and named none of it, so a fourth session would
have walked into the same wall.

That evidence first rode Land's diagnose-first run. Land was retired on
2026-10-09, and Resume is now the only way back into a stalled worktree,
so the evidence rides the Resume prompt.

Two things are therefore tested, and the SECOND is the one that matters:

  1. the halves — the buffer floor, the classifier, the repeat detector;
  2. the JOIN — that the prompt which actually reaches
     session.sessions.launch carries the failure evidence.

That split is this repo's own hard lesson: the branch-first write guard
was fully tested on both halves and silently disarmed for four days
because _spawn_runner never passed the fields, and the suite was green
throughout. A guard is only real where the two ends are tested together.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import orphanwork, runner, sessiondiag

BUFFER_ERR = ("Failed to decode JSON: JSON message exceeded maximum "
              "buffer size of 1048576 bytes...")


def _transcript(path):
    return (f"  → Read {path}\n"
            "Now wiring the retry onto the Attention row itself.\n"
            f"  → Edit {path}\n")


class BufferFloor(unittest.TestCase):
    """The runner's buffer, and the floor that keeps a bad config from
    reintroducing the failure."""

    def test_default_is_the_configured_size(self):
        with mock.patch.object(runner, "_scfg", return_value=64):
            self.assertEqual(runner._max_buffer_bytes(), 64 * 1024 * 1024)

    def test_never_below_the_sdk_default(self):
        # A config of 0 (or a typo'd string) must not shrink the buffer to
        # something SMALLER than shipping behaviour — a misconfiguration
        # may only ever be harmless.
        for bad in (0, -5, "", "banana", None):
            with mock.patch.object(runner, "_scfg", return_value=bad):
                self.assertGreaterEqual(runner._max_buffer_bytes(),
                                        runner._SDK_DEFAULT_BUFFER, bad)

    def test_it_clears_this_repos_own_largest_file(self):
        """The concrete regression. app.js is why this exists, so the
        assertion is against the real file rather than a number."""
        big = max((p.stat().st_size for p in
                   (Path(__file__).resolve().parent.parent / "static")
                   .glob("*.js")), default=0)
        self.assertGreater(big, 0, "no static/*.js found — fixture broken")
        with mock.patch.object(runner, "_scfg", return_value=64):
            self.assertGreater(runner._max_buffer_bytes(), big * 4)


class RunnerPassesIt(unittest.TestCase):
    """THE JOIN for the fix: the option must actually reach the SDK.

    A helper that computes the right number is worth nothing if the
    options object never carries it — the reader-with-no-writer shape
    that disarmed the branch guard and silently emptied `model_used`.
    """

    def test_options_carry_max_buffer_size(self):
        src = (Path(runner.__file__).read_text(encoding="utf-8"))
        head = src.split("ClaudeAgentOptions(", 1)
        self.assertEqual(len(head), 2, "no ClaudeAgentOptions( construction")
        block = head[1][:2000]
        self.assertIn("max_buffer_size=", block,
                      "ClaudeAgentOptions is built without max_buffer_size "
                      "— the SDK falls back to its 1 MiB default and "
                      "editing a large file kills the session")


class Classify(unittest.TestCase):
    def test_buffer_error_is_named_and_certain(self):
        d = sessiondiag.classify(BUFFER_ERR)
        self.assertEqual(d["kind"], "buffer")
        self.assertTrue(d["harness"], "a harness limit is not a defect in "
                                      "the work and must not read as one")
        self.assertTrue(d["certain"])
        self.assertIn("1,048,576", d["why"])

    def test_it_names_the_oversized_file_from_the_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            big = Path(td) / "app.js"
            big.write_bytes(b"x" * 1_062_221)
            d = sessiondiag.classify(BUFFER_ERR, _transcript(big))
        self.assertIn(str(big), d["why"])
        self.assertIn("1,062,221", d["why"])

    def test_a_windows_path_is_a_path(self):
        """WHICH OS RUNS THE SUITE MUST NOT DECIDE WHETHER THIS IS TESTED.

        _PATH_RE matched only /... , so on Windows the transcript's
        C:\\Users\\...\\app.js never matched and the oversized file - the one
        fact this diagnosis exists to state - could never be named. Every
        case around this one uses a POSIX tmp path, so they pass against the
        broken regex on a Mac and the failure showed up only in CI.

        The extraction is asserted directly rather than through classify():
        a real Windows path cannot be stat'd here, and the bug was in the
        matching, not in the stat.
        """
        win = r"  \u2192 Edit C:\Users\RUNNER~1\AppData\Local\Temp\t1\app.js"
        self.assertEqual(
            sessiondiag._PATH_RE.findall(win),
            [r"C:\Users\RUNNER~1\AppData\Local\Temp\t1\app.js"])
        # The POSIX form must keep working - this widened, it did not move.
        self.assertEqual(
            sessiondiag._PATH_RE.findall("  \u2192 Edit /srv/vira/static/app.js"),
            ["/srv/vira/static/app.js"])
        # A bare word is still not a path: guessing one would put a
        # fabricated filename in a diagnosis.
        self.assertEqual(sessiondiag._PATH_RE.findall("  \u2192 Edit app.js"), [])

    def test_it_does_not_invent_a_file_it_cannot_stat(self):
        """Grounded-or-silent: a path that is not on disk is never
        reported with a size, because the point of naming a file is that
        the owner can go and check it."""
        d = sessiondiag.classify(BUFFER_ERR, _transcript("/nope/gone.js"))
        self.assertIn("Edit", d["why"])
        self.assertNotIn("bytes — over", d["why"])

    def test_a_small_file_is_not_blamed(self):
        with tempfile.TemporaryDirectory() as td:
            small = Path(td) / "tiny.js"
            small.write_text("x", encoding="utf-8")
            d = sessiondiag.classify(BUFFER_ERR, _transcript(small))
        self.assertNotIn("over the", d["why"])

    def test_empty_error_reads_as_interrupted_not_unknown(self):
        d = sessiondiag.classify("")
        self.assertEqual(d["kind"], "interrupted")
        self.assertFalse(d["certain"])

    def test_unknown_is_honest_rather_than_confident(self):
        d = sessiondiag.classify("Segmentation fault in something odd")
        self.assertEqual(d["kind"], "unknown")
        self.assertFalse(d["certain"])
        self.assertFalse(d["harness"])

    def test_usage_limit_delegates_to_aihealth(self):
        d = sessiondiag.classify("You've hit your monthly spend limit")
        self.assertEqual(d["kind"], "limit")
        self.assertTrue(d["certain"])

    def test_classify_never_raises(self):
        for bad in (None, "", 0, "\x00", "x" * 20000):
            sessiondiag.classify(bad, bad)


class ToolParsing(unittest.TestCase):
    def test_last_call_is_last(self):
        calls = sessiondiag.tool_calls(
            "  → Read /a/one.js\n  → Bash: grep -n foo\n  → Edit /a/two.js\n")
        self.assertEqual(calls[-1]["tool"], "Edit")
        self.assertEqual(calls[-1]["arg"], "/a/two.js")

    def test_prose_is_not_a_tool_call(self):
        self.assertEqual(sessiondiag.tool_calls("Now wiring the retry.\n"), [])


class _LedgerCase(unittest.TestCase):
    """A fixture ledger + job dirs. sessiondiag reads BOTH the joblog and
    the job-dir tree, so both are rooted in the fixture — a test that
    reads this machine only runs on this machine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jobs.mkdir()
        p = mock.patch("server.jobfiles.JOBS_DIR", self.jobs)
        p.start()
        self.addCleanup(p.stop)
        self.rows = []
        r = mock.patch("server.joblog.list_records",
                       side_effect=lambda: list(self.rows))
        r.start()
        self.addCleanup(r.stop)

    def add(self, jid, branch, status="error", error=BUFFER_ERR,
            finished="2026-08-28T15:32:25-04:00", target="/x/app.js"):
        self.rows.append({"id": jid, "branch": branch, "status": status,
                          "title": f"job {jid}", "finished": finished})
        d = self.jobs / jid
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({"error": error}),
                                      encoding="utf-8")
        (d / "output.log").write_text(_transcript(target), encoding="utf-8")


class BranchFailures(_LedgerCase):
    def test_an_empty_ledger_reports_nothing(self):
        """The isolation guard: this module reads a ledger AND a job-dir
        tree, so an empty fixture proving empty is what says neither is
        the real one."""
        self.assertEqual(sessiondiag.failures_for_branch("claude/x"), [])
        self.assertEqual(sessiondiag.evidence_block("claude/x"), "")

    def test_repeat_is_detected(self):
        for jid in ("aaa", "bbb", "ccc"):
            self.add(jid, "claude/x")
        fails = sessiondiag.failures_for_branch("claude/x")
        self.assertEqual(len(fails), 3)
        self.assertEqual(sessiondiag.repeated_kind(fails), "buffer")

    def test_one_failure_is_not_a_repeat(self):
        self.add("aaa", "claude/x")
        self.assertIsNone(
            sessiondiag.repeated_kind(sessiondiag.failures_for_branch("claude/x")))

    def test_uncertain_kinds_never_count_as_a_repeat(self):
        """Two 'unknown' failures are not a known repeated cause, and
        claiming they are would put a confident sentence over a guess."""
        for jid in ("aaa", "bbb"):
            self.add(jid, "claude/x", error="something weird happened")
        self.assertIsNone(
            sessiondiag.repeated_kind(sessiondiag.failures_for_branch("claude/x")))

    def test_only_this_branch(self):
        self.add("aaa", "claude/x")
        self.add("bbb", "claude/other")
        self.assertEqual(
            [f["id"] for f in sessiondiag.failures_for_branch("claude/x")], ["aaa"])

    def test_successful_sessions_are_not_failures(self):
        self.add("ok1", "claude/x", status="done", error="")
        self.assertEqual(sessiondiag.failures_for_branch("claude/x"), [])

    def test_evidence_block_leads_with_the_repeat(self):
        for jid in ("aaa", "bbb", "ccc"):
            self.add(jid, "claude/x")
        block = sessiondiag.evidence_block("claude/x")
        self.assertIn("3 of these ended the SAME way", block)
        self.assertIn("expected to fail again", block)
        self.assertLess(block.index("SAME way"), block.index("failure 1"),
                        "the repeat must lead — it is the fact that decides "
                        "whether retrying can work")


class LandIsRetired(unittest.TestCase):
    def test_no_land_entry_points_remain(self):
        """Land merged a clean branch with a bare script that could not
        open a PR, catch up with main, or resolve a conflict. A row
        reaches main through Resume and the landing card now."""
        for name in ("land", "land_all", "land_prompt",
                     "land_diagnose_prompt", "norm_land_mode"):
            self.assertFalse(hasattr(orphanwork, name), name)

    def test_the_recommendation_no_longer_offers_land(self):
        self.assertEqual(orphanwork.VERDICTS, ("resume", "discard"))
        self.assertNotIn('"land"', orphanwork.ASSESS_PROMPT)


class ResumePromptContract(_LedgerCase):
    """What the resuming session is actually told."""

    def _item(self):
        return {"branch": "claude/x", "worktree": "/tmp/wt-x", "dirty": 5}

    def setUp(self):
        super().setUp()
        f = mock.patch.object(orphanwork, "_prompt_fields", return_value={
            "worktree": "/tmp/wt-x", "branch": "claude/x",
            "live_root": "/repo", "job_block": "", "status": "M app.js",
            "log": "(no unmerged commits)"})
        f.start()
        self.addCleanup(f.stop)

    def test_it_carries_the_prior_failures(self):
        for jid in ("aaa", "bbb", "ccc"):
            self.add(jid, "claude/x")
        p = orphanwork.resume_prompt(self._item())
        self.assertIn("PRIOR FAILURES ON THIS BRANCH", p)
        self.assertIn("ended the SAME way", p)
        self.assertLess(p.index("PRIOR FAILURES"), p.index("Finish the work"),
                        "the evidence must come before the instruction it "
                        "should change")

    def test_no_failures_adds_no_section(self):
        p = orphanwork.resume_prompt(self._item())
        self.assertNotIn("PRIOR FAILURES", p)
        self.assertNotIn("No failed session", p)

    def test_it_leaves_the_decision_to_the_landing_card(self):
        """Vira raises Merge / Keep testing / Discard when the turn ends;
        a session that also asks puts two menus in front of the owner."""
        p = orphanwork.resume_prompt(self._item())
        self.assertIn("do not ask that yourself", p)
        self.assertNotIn("decision menu", p)


class ResumeDispatchJoin(_LedgerCase):
    """THE JOIN. What reaches session.sessions.launch — not what a prompt
    function returns when called directly."""

    def setUp(self):
        super().setUp()
        self.launched = []

        class _Fake:
            def launch(_s, prompt, cwd=None, meta=None, **kw):
                self.launched.append({"prompt": prompt, "cwd": cwd,
                                      "meta": meta or {}})
                return "job123"

        import server.session as ses
        p = mock.patch.object(ses, "sessions", _Fake())
        p.start()
        self.addCleanup(p.stop)
        f = mock.patch.object(orphanwork, "_prompt_fields", return_value={
            "worktree": "/tmp/wt-x", "branch": "claude/x",
            "live_root": "/repo", "job_block": "", "status": "M app.js",
            "log": "(none)"})
        f.start()
        self.addCleanup(f.stop)
        b = mock.patch.object(orphanwork, "_refuse_if_busy")
        b.start()
        self.addCleanup(b.stop)

    def _item(self):
        return {"branch": "claude/x", "worktree": "/tmp/wt-x", "dirty": 5,
                "kind": "dirty"}

    def test_resume_dispatch_carries_the_evidence(self):
        for jid in ("aaa", "bbb", "ccc"):
            self.add(jid, "claude/x")
        orphanwork.resume(self._item())
        self.assertEqual(len(self.launched), 1)
        sent = self.launched[0]
        self.assertIn("PRIOR FAILURES ON THIS BRANCH", sent["prompt"])
        self.assertIn("ended the SAME way", sent["prompt"])
        self.assertEqual(sent["cwd"], "/tmp/wt-x")
        self.assertEqual(sent["meta"].get("kind"), "orphan-resume")


if __name__ == "__main__":
    unittest.main()

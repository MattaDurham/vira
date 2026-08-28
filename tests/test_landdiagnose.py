"""Why a session stopped, and Land's diagnose-first gesture.

The incident these pin (2026-08-28): three sessions on one branch died at
the identical instant — each ran Edit on static/app.js, which is 1,062,221
bytes against the SDK's 1,048,576-byte NDJSON line ceiling — and the only
record anywhere was a truncated error string per row. Land's prompt said
"carry the work to done" and named none of it, so a fourth session would
have walked into the same wall.

Two things are therefore tested, and the SECOND is the one that matters:

  1. the halves — the buffer floor, the classifier, the repeat detector;
  2. the JOIN — that the prompt which actually reaches
     session.sessions.launch carries the failure evidence and the
     stop-and-ask instruction.

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


class LandModes(unittest.TestCase):
    def test_unknown_mode_falls_back_to_diagnose(self):
        for bad in ("", None, "junk", "FINISHED", 7):
            self.assertEqual(orphanwork.norm_land_mode(bad), "diagnose")

    def test_finish_is_honoured(self):
        self.assertEqual(orphanwork.norm_land_mode("finish"), "finish")
        self.assertEqual(orphanwork.norm_land_mode("  Finish "), "finish")

    def test_land_defaults_to_diagnose(self):
        import inspect
        self.assertEqual(
            inspect.signature(orphanwork.land).parameters["mode"].default,
            "diagnose")
        self.assertEqual(
            inspect.signature(orphanwork.land_all).parameters["mode"].default,
            "diagnose")


class DiagnosePromptContract(_LedgerCase):
    """What the diagnosing session is actually told."""

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
        p = orphanwork.land_diagnose_prompt(self._item())
        self.assertIn("PRIOR FAILURES ON THIS BRANCH", p)
        self.assertIn("ended the SAME way", p)

    def test_it_says_so_when_there_are_none(self):
        p = orphanwork.land_diagnose_prompt(self._item())
        self.assertIn("No failed session is recorded", p)
        self.assertNotIn("PRIOR FAILURES", p)

    def test_it_orders_diagnose_then_stop_then_ask(self):
        p = orphanwork.land_diagnose_prompt(self._item())
        self.assertIn("ask_owner", p)
        self.assertIn("Do NOT start fixing", p)
        self.assertLess(p.index("DIAGNOSE"), p.index("STOP AND ASK"))

    def test_it_forbids_merging_and_pushing(self):
        p = orphanwork.land_diagnose_prompt(self._item())
        self.assertIn("do NOT merge or push yourself", p)

    def test_a_refusal_is_expressed_as_not_committing(self):
        """The deterministic half of the contract: Vira merges only a
        clean, committed tree, so 'do not commit' IS the refusal and the
        prompt must say that rather than relying on a second mechanism."""
        p = orphanwork.land_diagnose_prompt(self._item())
        self.assertIn("do NOT commit", p)
        self.assertIn("never merged", p)

    def test_the_finish_prompt_is_still_available(self):
        p = orphanwork.land_prompt(self._item())
        self.assertIn("carry the work to", p)
        self.assertNotIn("PRIOR FAILURES", p)


class LandDispatchJoin(_LedgerCase):
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

    def _item(self):
        return {"branch": "claude/x", "worktree": "/tmp/wt-x", "dirty": 5,
                "kind": "dirty"}

    def test_diagnose_dispatch_carries_the_evidence(self):
        for jid in ("aaa", "bbb", "ccc"):
            self.add(jid, "claude/x")
        orphanwork._launch_land_session(self._item(), "diagnose")
        self.assertEqual(len(self.launched), 1)
        sent = self.launched[0]
        self.assertIn("PRIOR FAILURES ON THIS BRANCH", sent["prompt"])
        self.assertIn("ended the SAME way", sent["prompt"])
        self.assertIn("ask_owner", sent["prompt"])
        self.assertEqual(sent["meta"].get("land_mode"), "diagnose")
        self.assertTrue(sent["meta"].get("machine"),
                        "a machine dispatch must not park in the reply "
                        "window")

    def test_finish_dispatch_is_the_old_prompt(self):
        orphanwork._launch_land_session(self._item(), "finish")
        self.assertNotIn("PRIOR FAILURES", self.launched[0]["prompt"])
        self.assertEqual(self.launched[0]["meta"].get("land_mode"), "finish")

    def test_an_unknown_mode_dispatches_the_diagnosis(self):
        """The safe direction, at the dispatch seam rather than only in
        the normaliser — this is where being wrong costs a re-run of the
        step that just failed."""
        orphanwork._launch_land_session(self._item(), "banana")
        self.assertIn("STOP AND ASK", self.launched[0]["prompt"])

    def test_land_without_a_mode_diagnoses(self):
        for jid in ("aaa", "bbb"):
            self.add(jid, "claude/x")
        with mock.patch.object(orphanwork, "_refuse_if_busy"), \
             mock.patch.object(orphanwork, "_set_action"), \
             mock.patch.object(orphanwork.threading, "Thread"):
            orphanwork.land(self._item())
        self.assertIn("STOP AND ASK", self.launched[0]["prompt"])

    def test_a_clean_row_launches_nothing(self):
        clean = {"branch": "claude/x", "worktree": "/tmp/wt-x", "dirty": 0,
                 "kind": "unmerged"}
        with mock.patch.object(orphanwork, "_refuse_if_busy"), \
             mock.patch.object(orphanwork, "_set_action"), \
             mock.patch.object(orphanwork.threading, "Thread"):
            jid = orphanwork.land(clean)
        self.assertIsNone(jid)
        self.assertEqual(self.launched, [],
                         "a committed clean branch has nothing to diagnose")


if __name__ == "__main__":
    unittest.main()

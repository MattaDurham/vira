"""The two modules that size an AGENT SESSION's prompt: judge and circuits.

Every case pins a JOIN rather than a number. Both halves of a feature have
been separately correct and still done nothing in this repo before - the
branch-first write guard sat disarmed for four days because _spawn_runner
never passed the fields - so asserting that modelbudget answers well, and
asserting that these modules truncate, would both pass against a module that
never asks. Each case moves the seam's answer and asserts the prompt that
actually reaches the session moved with it.

The other half of the contract is the one this sweep is most likely to break:
a cap that bounds a STORE, or a file read, is not a context budget and must
NOT move with the window. Those are pinned in the opposite direction.
"""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import circuits, jobfiles, joblog, judge, modelbudget

# The literals each module carried before the seam existed. A budget that
# cannot beat these on a real backend would be the same defect wearing a
# function call.
OLD_ASK, OLD_OUTPUT, OLD_DIFF = 8_000, 20_000, 30_000
OLD_INJECT = 24_000
# The SDK's NDJSON line bound - the ceiling that killed five sessions on
# 2026-08-28. A composed prompt must stay far under it whatever the window.
SDK_LINE_BYTES = 1_048_576


def _budget(chars):
    """Pin what the backend reports, so a case says nothing about which
    machine ran it."""
    return mock.patch.object(modelbudget, "context_chars",
                             lambda kind=None, *a, **k: chars)


def _kept(prompt, ch):
    """The longest contiguous run of a marker character. Counting every
    occurrence would count the brief's own prose (VERDICT_CONTRACT is full of
    ordinary letters); the run is only ever our material."""
    return max((len(m) for m in re.findall(ch + "+", prompt)), default=0)


def _floor_rung():
    """Force the conservative per-provider FLOOR to be what answers.

    The learned rung reads data/model-limits.json - this machine's own
    receipts - so a case asking the real seam what a backend can hold would
    be asking which Mac ran it (the 2026-07-30 test_aihealth trap).
    """
    return mock.patch.object(modelbudget, "STORE",
                             Path(tempfile.mkdtemp()) / "absent.json")


class JudgeEvidence(unittest.TestCase):
    """build_prompt is the join: four channels, one composed brief."""

    def _prompt(self, chars, ask=None, output=None, context=None):
        with _budget(chars):
            return judge.build_prompt(ask or "a" * 200_000,
                                      output or "b" * 200_000,
                                      cwd=None, context=context or "c" * 50_000)

    def test_every_channel_is_the_backends_answer_not_a_literal(self):
        wide, narrow = self._prompt(600_000), self._prompt(20_000)
        for ch in "abc":
            self.assertGreater(_kept(wide, ch), _kept(narrow, ch), ch)

    def test_a_real_backend_beats_the_literals_it_replaced(self):
        """The anthropic floor is the worst a configured install reports."""
        with _floor_rung():
            floor = modelbudget.context_chars("deep", "anthropic", "cli")
        with _budget(floor):
            prompt = judge.build_prompt("a" * 200_000, "b" * 200_000,
                                        cwd=None, context="c" * 50_000)
        self.assertGreater(_kept(prompt, "a"), OLD_ASK)
        self.assertGreater(_kept(prompt, "b"), OLD_OUTPUT)

    def test_it_degrades_downward_and_never_guesses_high(self):
        """An over-large prompt is rejected or silently truncated by the
        provider, and a truncation we did not perform is one we cannot
        report - so a backend that can hold less is handed less."""
        prompt = self._prompt(2_000)
        self.assertLess(_kept(prompt, "a"), OLD_ASK)
        self.assertLess(_kept(prompt, "b"), OLD_OUTPUT)
        self.assertGreater(_kept(prompt, "a"), 0)
        self.assertGreater(_kept(prompt, "b"), 0)

    def test_the_prompt_cannot_outgrow_the_harness_that_persists_it(self):
        """A judge prompt is written VERBATIM into data/jobs-log.json, which
        is never pruned - so the window is not the only ceiling. However wide
        the backend, the brief stays far under the SDK's line bound."""
        prompt = self._prompt(50_000_000)
        self.assertLess(len(prompt), SDK_LINE_BYTES)
        for ch in "abc":
            self.assertLessEqual(
                _kept(prompt, ch),
                judge.SESSION_PROMPT_CHARS // judge.EVIDENCE_CHANNELS)

    def test_a_transcript_tail_is_sized_the_same_way(self):
        """The tail is the diff's alternative, never both - and it was the
        one channel read from the END, so an off-by-a-sign here would hand a
        judge the start of a session instead of its conclusion."""
        with _budget(600_000):
            wide = judge.build_prompt("ask", "", cwd=None,
                                      transcript_tail="t" * 100_000 + "ENDMARK")
        with _budget(20_000):
            narrow = judge.build_prompt("ask", "", cwd=None,
                                        transcript_tail="t" * 100_000 + "ENDMARK")
        self.assertGreater(_kept(wide, "t"), _kept(narrow, "t"))
        self.assertIn("ENDMARK", narrow)


class UntrackedFilesAreNotAContextCap(unittest.TestCase):
    """NEW_FILES and NEW_FILE_BYTES bound how many files this function opens
    and how large one may be before it is not slurped into memory. Neither is
    a statement about the model's window, so neither may move with it -
    routing them through the budget would make a wide backend read the repo.
    """

    def _repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / "small.txt").write_text("SMALLMARK" + "s" * 500,
                                        encoding="utf-8")
        (root / "huge.txt").write_text(
            "HUGEMARK" + "h" * (judge.NEW_FILE_BYTES + 1_000), encoding="utf-8")
        return root

    def test_an_oversized_untracked_file_is_never_read_however_wide(self):
        root = self._repo()
        with _budget(50_000_000):
            out = judge._git_diff(str(root))
        self.assertIn("SMALLMARK", out)
        self.assertNotIn("HUGEMARK", out)

    def test_the_files_it_does_read_grow_with_the_window(self):
        """Only the per-file TRUNCATION was a context budget - it was a flat
        [:4000], so a new 200-line source file was graded from its first
        half."""
        root = self._repo()
        (root / "mid.txt").write_text("m" * 20_000, encoding="utf-8")
        with _budget(600_000):
            wide = judge._git_diff(str(root))
        with _budget(20_000):
            narrow = judge._git_diff(str(root))
        self.assertGreater(_kept(wide, "m"), _kept(narrow, "m"))
        self.assertGreater(_kept(wide, "m"), 4_000)


class CircuitHandoff(unittest.TestCase):
    """render_prompt's {{stage.x.output}} substitution."""

    RUN = {"input": "go", "stages": {
        "a": {"result_text": "a" * 200_000},
        "b": {"result_text": "b" * 200_000},
        "c": {"result_text": "c" * 200_000}}}

    def _render(self, chars, template):
        with _budget(chars):
            return circuits.render_prompt(template, self.RUN)

    def test_a_substitution_is_the_backends_answer_not_a_literal(self):
        wide = self._render(600_000, "{{stage.a.output}}")
        narrow = self._render(20_000, "{{stage.a.output}}")
        self.assertGreater(_kept(wide, "a"), _kept(narrow, "a"))

    def test_a_real_backend_beats_the_literal_it_replaced(self):
        with _floor_rung():
            floor = modelbudget.context_chars(circuits.HANDOFF_CLASS,
                                              "anthropic", "cli")
        with _budget(floor):
            out = circuits.render_prompt("{{stage.a.output}}", self.RUN)
        self.assertGreater(_kept(out, "a"), OLD_INJECT)

    def test_the_references_share_one_budget(self):
        """A synthesis stage reading three upstream stages must not let
        whichever ran longest spend the whole window."""
        one = self._render(600_000, "{{stage.a.output}}")
        three = self._render(
            600_000, "{{stage.a.output}}{{stage.b.output}}{{stage.c.output}}")
        self.assertLess(_kept(three, "a"), _kept(one, "a"))
        for ch in "abc":
            self.assertGreater(_kept(three, ch), 0)

    def test_a_template_with_no_reference_still_renders(self):
        self.assertEqual(self._render(600_000, "just {{input}}"), "just go")

    def test_the_store_cap_is_not_the_window(self):
        """_stage_output's DEFAULT bounds what goes back into
        data/circuit-runs.json - a local Output part's result_text is the
        join of its inputs and never passes runner.RESULT_KEEP. A store cap
        that grew with the backend would let one raised config balloon a
        store; it must stay exactly where it was."""
        for chars in (2_000, 600_000, 50_000_000):
            with _budget(chars):
                self.assertEqual(
                    len(circuits._stage_output(self.RUN, "a")), OLD_INJECT)


class StubSessions:
    """Minimal registry: launch() finishes instantly, writing the state.json
    the driver reads back."""

    def __init__(self, root, script):
        self.root, self.script, self.launched, self.n = Path(root), script, [], 0

    def launch(self, prompt, cwd=None, permission_mode=None, model=None,
               publish_plan=False, idea_id=None, mode=None,
               read_only=False, meta=None):
        jid = f"stub{self.n:04d}"
        self.n += 1
        rec = {"id": jid, "prompt": prompt, "meta": meta or {}}
        self.launched.append(rec)
        jdir = self.root / jid
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / "state.json").write_text(json.dumps(
            {"id": jid, "status": "done", "result_text": self.script(rec)}),
            encoding="utf-8")
        return jid

    def get(self, jid):
        p = self.root / jid / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def close(self, jid):
        pass


class JudgeOverSeveralStages(unittest.TestCase):
    """The whole join, driven for real: several build stages, one judge over
    all of them, and the question of whose evidence survives."""

    STAGES = ["one", "two", "three", "four"]
    MARK = {s: chr(ord("p") + i) for i, s in enumerate(STAGES)}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for p in [mock.patch.object(circuits, "DEFS", root / "circuits.json"),
                  mock.patch.object(circuits, "RUNS", root / "runs.json"),
                  mock.patch.object(jobfiles, "JOBS_DIR", root / "jobs"),
                  mock.patch.object(joblog, "STORE", root / "log.json")]:
            p.start()
            self.addCleanup(p.stop)
        self.jobs = root / "jobs"

    def _judge_prompt(self, chars):
        def script(rec):
            sid = rec["meta"].get("stage")
            if sid in self.MARK:            # each build stage fills its ceiling
                return self.MARK[sid] * 20_000
            return '```json\n{"grade": "A", "score": 95, "summary": "ok."}\n```'

        stub = StubSessions(self.jobs, script)
        p = mock.patch("server.session.sessions", stub)
        p.start()
        self.addCleanup(p.stop)
        circuits.save_circuit({
            "id": "fan", "name": "fan", "stages": [
                {"id": s, "name": s, "mode": "autopilot", "prompt": "do it",
                 "needs": []} for s in self.STAGES]
            + [{"id": "check", "name": "Check", "mode": "judge",
                "needs": list(self.STAGES),
                "judge": {"of": list(self.STAGES)}}]})
        with _budget(chars):
            run = circuits.start_run("fan", "ship it")
            d = circuits.Driver.__new__(circuits.Driver)   # no thread start
            for _ in range(20):
                run = circuits.get_run(run["id"])
                if run["status"] != "running":
                    break
                d._advance(run)
        judged = [r for r in stub.launched
                  if r["meta"].get("stage") == "check"]
        self.assertTrue(judged, "the judge stage never launched")
        return judged[0]["prompt"]

    def test_the_last_judged_stage_is_not_the_one_that_disappears(self):
        """The evidence join IS the judge's report channel, and that channel
        is cut from the TAIL - so sizing each stage against a wider budget
        than the channel let the first stages spend it and quietly starved
        the last. Every judged stage must be represented."""
        prompt = self._judge_prompt(600_000)
        kept = {s: _kept(prompt, self.MARK[s]) for s in self.STAGES}
        for s in self.STAGES:
            self.assertIn(f"[stage {s} output]", prompt, s)
            self.assertGreater(kept[s], 1_000, f"{s} was starved: {kept}")
        self.assertLess(max(kept.values()) - min(kept.values()), 1_000, kept)

    def test_a_narrow_backend_still_grades_every_stage(self):
        prompt = self._judge_prompt(20_000)
        for s in self.STAGES:
            self.assertGreater(_kept(prompt, self.MARK[s]), 0, s)


if __name__ == "__main__":
    unittest.main()

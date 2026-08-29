"""The three modules that size a prompt: brief, groupchat, radar.

Every case here pins a JOIN rather than a number. Both halves of a feature
have been separately correct and still done nothing in this repo before -
the branch-first write guard sat disarmed for four days because
_spawn_runner never passed the fields, and app.js preferred `model_used`
over `model` in three places while nothing ever wrote it. A test that
asserts modelbudget answers correctly, and a test that asserts these
modules truncate, would both pass against a module that never asks.

So each case drives the real function with the seam's answer moved, and
asserts the prompt that actually reaches the model moved with it.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import brief, groupchat, modelbudget, radar

# The house pattern for a source contract: anchored on this file, never
# on the cwd - `python -m unittest` from anywhere but the repo root
# would otherwise error rather than check anything.
ROOT = Path(__file__).resolve().parent.parent

# The literals each module carried before the seam existed. A budget that
# cannot beat these on a real backend would be the same defect wearing a
# function call.
OLD_THREAD_TAIL, OLD_MSG_CHARS = 80, 200
OLD_NARRATIVE_CHARS = 14_000
OLD_DOSSIER_CHARS, OLD_CURATE_CHARS = 260, 30_000


def _longest_run(text, ch):
    return max((len(m) for m in re.findall(ch + "+", text)), default=0)


def _budget(chars):
    """Pin what the backend reports, so a case says nothing about which
    machine ran it."""
    return mock.patch.object(modelbudget, "context_chars",
                             lambda kind=None, *a, **k: chars)


def _floor_rung():
    """Force the conservative per-provider FLOOR to be what answers.

    The learned rung reads data/model-limits.json - this machine's own
    receipts - so a case asking the real seam what a backend can hold
    would be asking which Mac ran it (the 2026-07-30 test_aihealth trap:
    ProbeTests read models.connected() and went red on every CI runner).
    Emptying the store leaves modelbudget.FLOORS answering, which is the
    SMALLEST budget a configured install ever reports. A case that beats
    the retired literal here beats it everywhere.
    """
    return mock.patch.object(modelbudget, "STORE",
                             Path(tempfile.mkdtemp()) / "absent.json")


class GroupBriefThread(unittest.TestCase):
    def test_the_pair_is_the_backends_answer_not_a_literal(self):
        with _budget(600_000):
            big = groupchat.thread_budget()
        with _budget(20_000):
            small = groupchat.thread_budget()
        self.assertGreater(big[0], small[0])
        self.assertGreater(big[1], small[1])

    def test_a_real_backend_beats_the_literals_it_replaced(self):
        # The anthropic floor is the worst a configured install reports.
        with _floor_rung():
            floor = modelbudget.context_chars("standard", "anthropic", "cli")
        with _budget(floor):
            tail, chars = groupchat.thread_budget()
        self.assertGreater(tail, OLD_THREAD_TAIL)
        self.assertGreater(chars, OLD_MSG_CHARS)

    def test_it_degrades_downward_and_never_guesses_high(self):
        """An over-large prompt is rejected or silently truncated by the
        provider, and a truncation we did not perform is one we cannot
        report - so a backend that can hold less gets less."""
        with _budget(2_000):
            tail, chars = groupchat.thread_budget()
        self.assertLess(tail, OLD_THREAD_TAIL)
        self.assertLess(chars, OLD_MSG_CHARS)
        self.assertGreater(tail, 0)
        self.assertGreater(chars, 0)

    def test_the_brief_really_spends_it(self):
        """The join: the budget must reach BOTH the chat.db read and the
        message truncation, or one half of the pair is decorative."""
        long_msg = "x" * 5_000
        seen = {}

        def fake_thread(chat_ids, limit=None):
            seen["limit"] = limit
            return [{"from_me": False, "sender": "A", "text": long_msg}]

        prof = {"label": "Ski trip", "activity": [],
                "group": {"participants": [{"name": "A"}]},
                "connections": {"edges": []}, "related": []}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from server import suggest
        with mock.patch.object(groupchat.settings, "fixture_mode",
                               lambda: False), \
             mock.patch.object(groupchat, "BRIEFS",
                               Path(tmp.name) / "briefs.json"), \
             mock.patch.object(groupchat, "_latest_rowid", lambda ids: 1), \
             mock.patch.object(groupchat, "profile",
                               lambda **kw: dict(prof, status="ok")), \
             mock.patch.object(groupchat.imessage, "group_thread",
                               fake_thread), \
             mock.patch.object(
                 suggest, "complete",
                 lambda p, **k: seen.update(prompt=p) or json.dumps(
                     {"read": "they are planning a trip"})), \
             _budget(400_000):
            tail, chars = groupchat.thread_budget()
            groupchat.brief([1])
        self.assertEqual(seen["limit"], tail)
        # the message survives past the retired 200-character cut
        self.assertIn("x" * (OLD_MSG_CHARS + 1), seen["prompt"])
        self.assertNotIn("x" * (chars + 1), seen["prompt"])


class BriefNarrative(unittest.TestCase):
    def _run(self, chars):
        seen = {}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(brief, "compose",
                               lambda f=None: {"pad": "y" * 60_000}), \
             mock.patch.object(brief, "NARRATIVE_CACHE",
                               Path(tmp.name) / "n.json"), \
             mock.patch.object(brief.suggest, "complete",
                               lambda p, **k: seen.update(prompt=p) or "ok"), \
             _budget(chars):
            brief.generate_narrative(force=True)
        return seen["prompt"]

    def test_a_wide_window_carries_the_whole_brief(self):
        prompt = self._run(400_000)
        self.assertIn("y" * 60_000, prompt)
        self.assertNotIn("truncated", prompt)

    def test_a_narrow_one_cuts_and_says_so(self):
        """A truncation we perform is one we can report; the retired
        [:14000] handed the model a JSON dump that simply stopped."""
        prompt = self._run(5_000)
        self.assertNotIn("y" * 60_000, prompt)
        self.assertIn("truncated", prompt)

    def test_a_real_backend_beats_the_literal_it_replaced(self):
        with _floor_rung():
            room = modelbudget.context_chars(brief.NARRATIVE_CLASS,
                                             "anthropic", "cli")
        self.assertGreater(room, OLD_NARRATIVE_CHARS)


class RadarCuration(unittest.TestCase):
    def _prompt(self, chars, rooms=1):
        summary = "z" * 4_000
        pids = [f"p_{i:012x}" for i in range(rooms)]
        crm_stub = {
            "by_id": {p: {"id": p, "name": "A"} for p in pids},
            "profiles": {p: {"relationship_summary": summary} for p in pids},
        }
        cand = [{"members": [p], "topics": ["synths"], "score": 1.0,
                 "trigger": {"type": "overlap"}} for p in pids]
        seen = {}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from server import suggest
        with mock.patch.object(radar, "STORE", Path(tmp.name) / "r.json"), \
             mock.patch.object(radar.crm, "_load", lambda: crm_stub), \
             mock.patch.object(radar.crm, "get_person",
                               lambda pid: {"master": {}}), \
             mock.patch.object(radar, "candidates", lambda: (cand, [])), \
             mock.patch.object(radar, "_atlas_pairs", set), \
             mock.patch.object(radar, "_group_finder",
                               lambda: (lambda m: None)), \
             mock.patch.object(
                 suggest, "complete",
                 lambda p, **k: seen.update(prompt=p) or '{"groupings": []}'), \
             _budget(chars):
            radar.refresh_groupings()
        return seen["prompt"]

    def test_a_dossier_is_sized_by_the_backend(self):
        """How much of a person's summary the curator reads moves with the
        window, and on a real one it passes the retired 260 characters."""
        wide = _longest_run(self._prompt(400_000), "z")
        narrow = _longest_run(self._prompt(20_000), "z")
        self.assertGreater(wide, OLD_DOSSIER_CHARS)
        self.assertGreater(wide, narrow)

    def test_a_real_backend_beats_the_block_cap_it_replaced(self):
        with _floor_rung():
            total, each = modelbudget.split(
                radar.CURATE_CLASS, radar.MAX_CANDIDATES * radar.MAX_MEMBERS,
                "anthropic", "cli")
        self.assertGreater(total, OLD_CURATE_CHARS)
        self.assertGreater(each, OLD_DOSSIER_CHARS)

    def test_a_block_over_the_budget_says_so_instead_of_stopping(self):
        """The retired [:30_000] dropped the tail candidates mid-block in
        silence - a cap that is too small yields confident output, never an
        error. The budget makes that far rarer; it cannot make it
        impossible, so what is cut is announced."""
        wide = self._prompt(400_000, rooms=20)
        self.assertNotIn("truncated", wide)
        narrow = self._prompt(2_000, rooms=20)
        self.assertIn("truncated", narrow)
        self.assertLess(len(narrow), len(wide))

    def test_the_candidate_count_is_not_a_context_cap(self):
        """MAX_CANDIDATES is bounded by the curator's MEASURED wall-clock
        timeout (forty ran past 120s and fell back to raw matches), not by
        the window. Routing it through the budget would raise it and buy
        that failure back, so the decision is pinned here rather than left
        to drift - the dupgate-absolute precedent.
        """
        src = (ROOT / "server" / "radar.py").read_text(encoding="utf-8")
        body = src.split("def candidates(")[1].split("\ndef ")[0]
        self.assertNotIn("modelbudget", body)
        self.assertIn("MAX_CANDIDATES", body)


if __name__ == "__main__":
    unittest.main()

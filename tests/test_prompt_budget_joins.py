"""The six prompt-sizing modules ask the seam, and the answer reaches the model.

Every case here pins a JOIN. Both halves of a feature have been separately
correct and still done nothing in this repo before - the branch-first write
guard sat disarmed for four days because _spawn_runner never passed the
fields, and app.js preferred `model_used` over `model` in three places while
nothing ever wrote it. A test that asserts modelbudget answers correctly, and
a test that asserts these modules truncate, would both pass against a module
that never asks. So each case drives the REAL function with the seam's answer
moved and asserts the prompt that actually reaches the model moved with it.

No case names a character count of its own. The numbers here are the budgets
handed IN; what is asserted is the direction things moved and whether a cut
that bound was stated - so raising a budget cannot break a case and removing
a bound cannot pass one.

`_budget` patches modelbudget.context_chars rather than reading the machine:
the learned rung reads data/model-limits.json, this Mac's own receipts, and
a case that consulted it would be asking which computer ran it (the
2026-07-30 test_aihealth trap).
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import (doctags, ideatags, journal, modelbudget, onboard,
                    profilerefresh, suggest, threadread)

NARROW, WIDE = 3_000, 400_000


def _budget(chars):
    return mock.patch.object(modelbudget, "context_chars",
                             lambda kind=None, *a, **k: chars)


def _run(text, ch):
    return max((len(m) for m in re.findall(ch + "+", text)), default=0)


class JournalRoster(unittest.TestCase):
    """The roster and the loop list are REACHABILITY, not ranking: the prompt
    says use only person_ids present in the roster and copy a listed loop's
    text byte-for-byte, so a row outside the budget cannot be reached at all."""

    def setUp(self):
        self.people = [{"id": "p_%012x" % i, "name": f"Person {i}"}
                       for i in range(400)]
        self.loops = [{"person_id": "p_%012x" % 0, "person_name": "Person 0",
                       "what": f"loop {i}", "owed_by": "me",
                       "since": "2026-01-01"} for i in range(200)]

    def _prompt(self, chars, person_id=None):
        seen = {}
        crm_stub = {"people": self.people,
                    "by_id": {p["id"]: p for p in self.people},
                    "profiles": {}}
        with _budget(chars), \
             mock.patch.object(
                 journal.crm, "search_people",
                 lambda q=None, limit=60, sort="recent": self.people[:limit]), \
             mock.patch.object(journal.crm, "_load", lambda: crm_stub), \
             mock.patch.object(journal, "_all_open_loops",
                               lambda: list(self.loops)), \
             mock.patch.object(
                 suggest, "complete",
                 lambda p, **k: seen.update(prompt=p) or '{"summary": "x"}'):
            journal._plan({"id": "n1", "text": "a note", "person_id": person_id,
                           "person_name": "Person 399" if person_id else None})
        return seen["prompt"]

    def test_both_blocks_are_the_backends_answer_not_a_literal(self):
        wide, narrow = self._prompt(WIDE), self._prompt(NARROW)
        self.assertGreater(wide.count(" -> p_"), narrow.count(" -> p_"))
        self.assertGreater(wide.count("loop "), narrow.count("loop "))

    def test_a_wide_window_reaches_every_person_and_every_loop(self):
        p = self._prompt(WIDE)
        self.assertEqual(p.count(" -> p_"), len(self.people))
        self.assertNotIn("more people are in the registry", p)
        self.assertNotIn("more open loops exist", p)

    def test_a_cut_that_binds_is_stated_in_the_prompt(self):
        """A model told to say so when a mention cannot be resolved can only
        do that honestly if it knows the list is not the whole registry."""
        p = self._prompt(NARROW)
        self.assertIn("more people are in the registry", p)
        self.assertIn("more open loops exist", p)

    def test_the_scoped_person_survives_any_squeeze(self):
        """The owner filed the note against them, so they are the one name
        that cannot be optional - they are added after the fit, not inside
        it."""
        pid = self.people[-1]["id"]
        self.assertIn(pid, self._prompt(NARROW, person_id=pid))

    def test_a_squeeze_never_empties_a_block(self):
        p = self._prompt(200)
        self.assertIn(" -> p_", p)
        self.assertIn("loop ", p)


class IdeaTagging(unittest.TestCase):
    def _prompt(self, chars):
        vocab = {ax["id"]: [(f"tag-{i:03d}", 5) for i in range(150)]
                 for ax in ideatags.AXES}
        batch = [{"id": f"idea_{i}", "project": "Vira", "text": "z" * 6_000}
                 for i in range(ideatags.BATCH)]
        with _budget(chars):
            return ideatags._tag_prompt(batch, vocab)

    def test_the_idea_text_is_sized_by_the_backend(self):
        self.assertGreater(_run(self._prompt(WIDE), "z"),
                           _run(self._prompt(NARROW), "z"))

    def test_the_vocabulary_is_sized_by_the_backend(self):
        self.assertGreater(self._prompt(WIDE).count("tag-"),
                           self._prompt(NARROW).count("tag-"))

    def test_a_cut_vocabulary_says_how_much_it_is_missing(self):
        """The vocabulary block is the whole reason this pass converges; a
        tagger that believes it has seen all of it mints a synonym for a tag
        it was never shown."""
        self.assertIn("more not shown", self._prompt(NARROW))
        self.assertNotIn("more not shown", self._prompt(WIDE))


class FoldAnalysis(unittest.TestCase):
    """The owner ticked these boxes; a candidate that arrives half-quoted is
    judged on material he can see and the model cannot."""

    def _prompt(self, chars, n):
        items = [{"id": f"idea_{i}", "status": "open", "text": "z" * 9_000}
                 for i in range(n + 1)]
        seen = {}
        with _budget(chars), \
             mock.patch.object(ideatags, "_read", lambda: {"entries": {}}), \
             mock.patch.object(
                 suggest, "complete",
                 lambda p, **k: seen.update(prompt=p) or '{"verdicts": []}'):
            ideatags.fold_analysis("idea_0", [i["id"] for i in items[1:]],
                                   items=items)
        return seen["prompt"]

    def test_each_candidate_is_sized_by_the_backend(self):
        self.assertGreater(_run(self._prompt(WIDE, 5), "z"),
                           _run(self._prompt(NARROW, 5), "z"))

    def test_the_backstop_does_not_trim_at_the_routes_own_ceiling(self):
        """main.py hands this at most 40 candidates. With every one of them
        filling its share, the composed block plus its scaffolding must
        still sit under the total - or the last candidate is cut mid-text
        with nothing said, which is the failure the shares exist to end."""
        p = self._prompt(modelbudget.context_chars(
            ideatags.FOLD_CLASS, "anthropic", "cli"), 40)
        self.assertEqual(len(re.findall(r"- id: idea_", p)), 40)
        # the last candidate's text is whole, not cut short by the backstop
        runs = [len(m) for m in re.findall("z+", p)]
        self.assertEqual(len(set(runs[1:])), 1, runs[-3:])


class DocTagging(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.doc = Path(d.name) / "a.md"
        self.doc.write_text("word " * 8_000, encoding="utf-8")

    def _prompt(self, chars):
        vocab = {ax["id"]: [(f"tag-{i:03d}", 5) for i in range(150)]
                 for ax in doctags.AXES}
        batch = [{"id": "d1", "kind": "retro", "title": "2026-08-03 2133 vira",
                  "locator": "x"}]
        with _budget(chars), \
             mock.patch.object(doctags.readinglist, "source_path",
                               lambda it: self.doc):
            return doctags._prompt(batch, vocab)

    def test_the_excerpt_is_sized_by_the_backend(self):
        """423 of these documents are tagged from this excerpt alone - a
        retro's title is a date, so the opening prose is all there is."""
        self.assertGreater(_run(self._prompt(WIDE), "(?:word )"),
                           _run(self._prompt(NARROW), "(?:word )"))

    def test_a_cut_vocabulary_says_how_much_it_is_missing(self):
        self.assertIn("more not shown", self._prompt(NARROW))
        self.assertNotIn("more not shown", self._prompt(WIDE))


class ProfileRefresh(unittest.TestCase):
    def _ctx(self):
        return {
            "person": {"name": "Ann Example", "handles": {}},
            "prof": {}, "prof_slim": {"relationship_summary": "s" * 20_000},
            "evidence": "e" * 5_000,
            "thread": [{"when": "2026-08-01", "from_me": i % 2,
                        "text": f"msg{i} " + "t" * 300} for i in range(60)],
            "mail_recent": [f"recent{i} " + "m" * 300 for i in range(25)],
            "mail_oldest": [f"old{i} " + "o" * 300 for i in range(15)],
        }

    def _prompt(self, chars):
        with _budget(chars):
            return profilerefresh._prompt(self._ctx())

    def test_every_block_is_sized_by_the_backend(self):
        wide, narrow = self._prompt(WIDE), self._prompt(NARROW)
        for marker in ("msg", "recent", "old"):
            self.assertGreater(wide.count(marker), narrow.count(marker),
                               marker)

    def test_a_squeezed_thread_keeps_the_NEWEST_exchange(self):
        """The thread is oldest-first, so what has to survive a squeeze is
        the most recent messages, not the first ones."""
        p = self._prompt(NARROW)
        self.assertIn("msg59", p)
        self.assertNotIn("msg0 ", p)


class OnboardDossier(unittest.TestCase):
    def _prompt(self, chars):
        thread = [{"from_me": i % 2, "text": f"m{i} " + "q" * 2_000}
                  for i in range(onboard.DOSSIER_MESSAGES)]
        seen = {}
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        with _budget(chars), \
             mock.patch.object(onboard.imessage, "thread_for_person",
                               lambda pid, limit=60: thread), \
             mock.patch.object(
                 suggest, "complete",
                 lambda p, **k: seen.update(prompt=p) or json.dumps(
                     {"relationship_class": "friend",
                      "relationship_summary": "x" * 60,
                      "hooks": [], "open_loops": [], "personal_facts": []})):
            onboard._build_one("p_1", "Ann", Path(d.name), "Matt")
        return seen["prompt"]

    def test_the_transcript_is_sized_by_the_backend(self):
        wide, narrow = self._prompt(WIDE), self._prompt(NARROW)
        # both the per-message cut and the transcript tail move with it
        self.assertGreater(_run(wide, "q"), _run(narrow, "q"))
        self.assertGreater(wide.count("\nAnn: ") + wide.count("\nme: "),
                           narrow.count("\nAnn: ") + narrow.count("\nme: "))


class ThreadBrief(unittest.TestCase):
    def _prompt(self, chars):
        facts = {"empty": False, "baseline": {"a": "b" * 3_000},
                 "recent": {}, "deltas": {}, "bursts": [],
                 "colocation_caveat": None, "asks": [{"q": "c" * 3_000}]}
        msgs = [{"when": "2026-08-01T10:00", "from_me": i % 2,
                 "text": "w" * 1_000} for i in range(threadread.BRIEF_MESSAGES)]
        seen = {}
        with _budget(chars), \
             mock.patch.object(threadread, "analyze",
                               lambda pid, window_days=14: facts), \
             mock.patch.object(threadread.imessage, "thread_for_person",
                               lambda pid, limit=40: msgs), \
             mock.patch.object(suggest, "config",
                               lambda: {"owner_name": "Matt"}), \
             mock.patch.object(
                 suggest, "_run",
                 lambda p, c, **k: (seen.update(prompt=p) or '{"read": "x"}',
                                    "cli")):
            threadread.brief("p_1")
        return seen["prompt"]

    def test_all_three_blocks_are_sized_by_the_backend(self):
        # counted, not longest-run: one message is 1,000 characters whatever
        # the budget, and what moves is how many of them survive
        wide, narrow = self._prompt(WIDE), self._prompt(NARROW)
        for ch in ("b", "c", "w"):
            self.assertGreater(wide.count(ch), narrow.count(ch), ch)

    def test_the_shares_hold_their_ratio(self):
        """Only the RATIO between the three blocks was ever a judgement -
        the transcript is the evidence and the two computed blocks are its
        index, so the thread gets three times the room."""
        p = self._prompt(NARROW)
        self.assertEqual(threadread.BRIEF_SHARES["facts"],
                         threadread.BRIEF_SHARES["asks"])
        self.assertAlmostEqual(p.count("w") / p.count("b"),
                               threadread.BRIEF_SHARES["thread"]
                               / threadread.BRIEF_SHARES["facts"], delta=0.2)


if __name__ == "__main__":
    unittest.main()

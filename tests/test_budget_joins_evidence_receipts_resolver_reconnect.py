"""The four gather-then-ask modules: evidence, receipts, resolver, reconnect.

Each composes ONE prompt out of material it has already paid to fetch, and
each used to size that prompt with a literal typed once. server/modelbudget
answers how much the answering backend can hold; every case here pins the
JOIN - that the seam's answer really reaches the prompt - never a number.

That distinction is this repo's most expensive recurring defect: the
branch-first write guard was fully tested on both halves and sat disarmed
for four days because `_spawn_runner` never passed the fields, and app.js
preferred `model_used` in three places while nothing ever wrote it. A test
that modelbudget answers correctly, and a test that these modules truncate,
would BOTH pass against a module that never asks.

Every case was mutation-checked against the literal it replaced. Nothing
here asserts a budget's value, so raising a budget cannot break a case and
removing a bound cannot pass one.

The filename is long on purpose: it names its four modules so a parallel
session writing the same kind of file cannot silently overwrite it (one
already did, under a shorter name).
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import evidence, modelbudget, receipts, reconnect, resolver

# The literals each module carried before the seam existed. A budget that
# cannot beat these on the worst rung a configured install reports would be
# the same defect wearing a function call.
OLD_RESOLVER_MSGS = 30        # messages, not characters - see _fmt_msgs
OLD_JOB_PROMPT = 200          # _format_jobs' cut, under a 400-char head
OLD_RECEIPT_TEXT = 2_600
OLD_SUMMARY = 200


def _longest_run(text, ch):
    """How much of one padded field survived, independent of the chrome
    around it. Measuring a whole rendered line instead is what makes a
    beats-the-literal case pass against the literal it pins."""
    return max((len(m) for m in re.findall(ch + "+", text)), default=0)


def _budget(chars):
    """Pin what the backend reports, so a case says nothing about which
    machine ran it. `split` still does its real division on top."""
    return mock.patch.object(modelbudget, "context_chars",
                             lambda kind=None, *a, **k: chars)


def _floor_rung():
    """Force the conservative per-provider FLOOR to answer.

    The learned rung reads data/model-limits.json - this machine's own
    receipts - so asking the real seam what a backend holds would be asking
    which Mac ran the suite (the 2026-07-30 test_aihealth trap). Emptying
    the store leaves modelbudget.FLOORS, the SMALLEST budget a configured
    install ever reports, so a case that beats a retired literal here beats
    it everywhere.
    """
    return mock.patch.object(modelbudget, "STORE",
                             Path(tempfile.mkdtemp()) / "absent.json")


def _ev(thread=(), cards=()):
    return {"thread": list(thread), "group_msgs": [], "cards": list(cards),
            "referrer": "", "referrer_pids": [], "ambiguous": False,
            "candidates": [], "sources": [], "verdict": {}}


class ResolverBlocks(unittest.TestCase):
    def test_the_thread_block_moves_with_the_backend(self):
        msgs = [("them", "m" * 100)] * 300
        with _budget(400_000):
            wide = resolver._build_prompt("+15550100", "", _ev(msgs))
        with _budget(4_000):
            narrow = resolver._build_prompt("+15550100", "", _ev(msgs))
        self.assertGreater(wide.count("them:"), narrow.count("them:"))

    def test_a_real_backend_beats_the_message_count_it_replaced(self):
        """The retired cap was 30 MESSAGES, applied downstream of a fetch
        that had already read up to 120 of them."""
        msgs = [("them", "m" * 40)] * 200
        with _floor_rung():
            prompt = resolver._build_prompt("+15550100", "", _ev(msgs))
        self.assertGreater(prompt.count("them:"), OLD_RESOLVER_MSGS)

    def test_the_cards_block_is_bounded_at_all(self):
        """It was not: each card was cut at 2,000 chars and the block that
        joined them had no ceiling, so enough shared vCards was an
        unbounded prompt."""
        cards = ["c" * 5_000] * 40
        with _budget(200_000):
            prompt = resolver._build_prompt("+15550100", "", _ev((), cards))
        self.assertLess(len(prompt), len("".join(cards)))


class EvidenceCompose(unittest.TestCase):
    def _cap_seen(self, run):
        seen = {}

        def rec(prompt_cap):
            seen["cap"] = prompt_cap
            return []

        with mock.patch.object(evidence, "_jobs", rec), \
             mock.patch.object(evidence, "_retro_files", list):
            run()
        return seen["cap"]

    def test_the_browser_payload_keeps_its_head(self):
        """Nothing renders a job's prompt on the episodes payload, so it
        must NOT grow to a model's budget."""
        self.assertEqual(self._cap_seen(evidence.mine),
                         evidence.PAYLOAD_PROMPT_HEAD)

    def test_the_compose_path_reads_at_the_models_budget(self):
        def run():
            with self.assertRaises(KeyError):
                evidence._episode("no-such-episode")

        with _budget(400_000):
            cap = self._cap_seen(run)
        self.assertGreater(cap, evidence.PAYLOAD_PROMPT_HEAD)

    def test_the_jobs_block_moves_with_the_backend(self):
        jobs = [{"id": "j1", "status": "done", "title": "t",
                 "prompt": "p" * 40_000}]
        with _budget(400_000):
            wide = evidence._format_jobs(jobs)
        with _budget(4_000):
            narrow = evidence._format_jobs(jobs)
        self.assertGreater(len(wide), len(narrow))

    def test_a_real_backend_beats_the_literal_it_replaced(self):
        """Measured on the PROMPT's own characters, not the line's: the
        row's `- <id> [<status>] <title>: ` prefix clears 200 on its own and
        would pass against the very cut this pins."""
        jobs = [{"id": "j1", "status": "done", "title": "t",
                 "prompt": "p" * 40_000}]
        with _floor_rung():
            carried = _longest_run(evidence._format_jobs(jobs), "p")
        self.assertGreater(carried, OLD_JOB_PROMPT)


class ReceiptsCandidates(unittest.TestCase):
    def _cap_seen(self, accounts):
        seen = {}

        def rec(merchant, text_cap):
            seen["cap"] = text_cap
            return []

        with mock.patch.object(receipts, "_accounts", lambda: accounts), \
             mock.patch.object(receipts, "_candidates_media", rec):
            receipts.gather_candidates({"id": "m1", "display_name": "Acme",
                                        "aliases": []})
        return seen["cap"]

    def test_the_budget_reaches_the_source_that_reads_the_document(self):
        """The cap is applied where the text is read, not where the prompt
        is built - so the source functions must be HANDED it."""
        with _floor_rung():
            self.assertGreater(self._cap_seen([]), OLD_RECEIPT_TEXT)

    def test_the_source_count_is_really_the_divisor(self):
        """`sources` is passed so the share divides by the most candidates
        one merchant's prompt can carry; ignored, two mailboxes would read
        as much per candidate as none."""
        with _budget(400_000):
            alone = self._cap_seen([])
            crowded = self._cap_seen([{"email": "a@example.com"},
                                      {"email": "b@example.com"}])
        self.assertGreater(alone, crowded)


class ReconnectDossier(unittest.TestCase):
    CD = {"name": "A", "pid": "p_a", "company": "", "company_source": "",
          "title": "", "days": 60, "reasons": ["overlap"]}

    def _dossier(self, cap):
        from server import threadread
        with mock.patch.object(threadread, "enrich_person", lambda pid: None):
            return reconnect._dossier(
                self.CD, {"relationship_summary": "z" * 6_000}, cap)

    def test_the_summary_is_what_the_budget_reaches(self):
        """Every other bit of a dossier is a short fixed field."""
        self.assertGreater(len(self._dossier(4_000)), len(self._dossier(500)))

    def test_a_real_backend_beats_the_summary_cut_it_replaced(self):
        """Measured on the summary's own characters: the dossier's fixed
        bits alone clear 200 and would pass against the retired cut."""
        with _floor_rung():
            _total, per = reconnect._curate_budget(reconnect.MAX_CANDIDATES)
        self.assertGreater(_longest_run(self._dossier(per), "z"), OLD_SUMMARY)

    def test_refresh_spends_the_budget_on_both_halves(self):
        """A source contract, because refresh() is a whole dormant scan plus
        a model call: the per-candidate share must reach _dossier AND the
        joined block, or one of the two retired literals still decides."""
        src = Path("server/reconnect.py").read_text(encoding="utf-8")
        body = src.split("def refresh(")[1]
        self.assertIn("_curate_budget(", body)
        self.assertIn("per_cap", body)
        self.assertIn("[:total_cap]", body)
        self.assertNotIn("20_000", body)
        self.assertNotIn("[:600]", body)


if __name__ == "__main__":
    unittest.main()

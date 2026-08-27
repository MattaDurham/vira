"""threadread: the arithmetic layer under the dossier Rhythm block, the
brief's ask splits, and the facts block in the reply prompts. Pure-function
tests over synthetic messages — no chat.db, no CRM."""
import unittest
from datetime import datetime, timedelta

from server import suggest, threadread


def m(minute, from_me, text="", reaction=False, attachments=0, base=None):
    base = base or datetime(2026, 8, 1, 9, 0)
    return {"rowid": minute, "when": base + timedelta(minutes=minute),
            "from_me": from_me, "text": text, "reaction": reaction,
            "attachments": attachments}


class CadenceTests(unittest.TestCase):
    def test_initiation_counts_who_speaks_after_six_quiet_hours(self):
        msgs = [m(0, False, "hi"), m(1, True, "hey"),
                m(500, True, "thinking of you"),      # >6h later: owner starts
                m(505, False, "aw")]
        c = threadread.cadence(msgs)
        self.assertEqual(c["starts_theirs"], 1)
        self.assertEqual(c["starts_mine"], 1)
        self.assertEqual(c["my_initiation_pct"], 50)

    def test_median_reply_measures_them_to_me(self):
        msgs = [m(0, False, "q"), m(10, True, "a"),
                m(20, False, "q2"), m(50, True, "a2")]
        self.assertEqual(threadread.cadence(msgs)["median_reply_min"], 20)

    def test_reactions_never_count_as_speech(self):
        msgs = [m(0, False, "hi"), m(1, True, reaction=True), m(2, True, "hey")]
        self.assertEqual(threadread.cadence(msgs)["mine"], 1)


class BurstTests(unittest.TestCase):
    def test_peak_density_is_objects_not_messages(self):
        msgs = [m(0, False, "a"), m(1, False, "b", attachments=7),
                m(2, False, "c"), m(3, False, "d")]
        b = threadread.bursts(msgs)
        self.assertEqual(b["count"], 1)
        self.assertEqual(b["peak"]["attachments"], 7)
        self.assertGreater(b["peak"]["objects_per_min"],
                           b["peak"]["messages"] / 3)

    def test_owner_reply_ends_a_run(self):
        msgs = [m(0, False, "a"), m(1, False, "b"), m(2, True, "mine"),
                m(3, False, "c"), m(4, False, "d")]
        self.assertEqual(threadread.bursts(msgs, min_len=2)["count"], 2)


class AskTests(unittest.TestCase):
    def test_released_wording_beats_the_question_mark(self):
        # "optional"/"no rush" in the message releases it even when it asks
        self.assertEqual(threadread._classify(
            "Can you look? No rush at all"), "released")
        self.assertEqual(threadread._classify(
            "It's for Tuesday and I added you as optional"), "released")

    def test_requests_and_questions_classify(self):
        self.assertEqual(threadread._classify("Can you reply to my dad?"),
                         "question")
        self.assertEqual(threadread._classify("Please write him back"),
                         "request")
        self.assertIsNone(threadread._classify("What a lovely day"))

    def test_pending_means_arrived_after_owner_last_spoke(self):
        msgs = [m(0, False, "can you fix the door?"), m(5, True, "done"),
                m(10, False, "what do you think of this?")]
        ak = threadread.open_asks(msgs)
        self.assertEqual(len(ak["pending"]), 1)
        self.assertIn("think", ak["pending"][0]["text"])

    def test_answered_but_slow_lands_in_stale(self):
        msgs = [m(0, False, "can you get gatorade"),
                m(13 * 60, True, "got it"),   # 13h later
                m(13 * 60 + 1, False, "ty")]
        ak = threadread.open_asks(msgs)
        self.assertEqual(len(ak["stale"]), 1)
        self.assertGreater(ak["stale"][0]["sat_hours"], 12)


class PromptContractTests(unittest.TestCase):
    """A missing format key crashes suggest() at runtime; pin the contract."""
    def test_reply_prompt_accepts_the_facts_key(self):
        out = suggest.PROMPT.format(owner="o", channel="imessage",
                                    profile="{}", thread="t", extra="",
                                    facts="F")
        self.assertIn("F", out)

    def test_hook_prompt_accepts_the_facts_key(self):
        out = suggest.HOOK_PROMPT.format(owner="o", profile="{}", thread="t",
                                         extra="hook", facts="F")
        self.assertIn("F", out)


if __name__ == "__main__":
    unittest.main()

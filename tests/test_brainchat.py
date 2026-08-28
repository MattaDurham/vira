"""Persistent Find chat, concept validation, and session accumulation."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import brainchat, vault


HITS = [
    {"path": "Sessions/alpha.md", "title": "Alpha", "heading": "Decision",
     "text": "The durable chat decision and supporting research."},
    {"path": "wiki/vault.md", "title": "Vault", "heading": "Grounding",
     "text": "Answers must stay grounded in locally indexed notes."},
]


def _answer(text="The decision is recorded in [[Sessions/alpha.md]]."):
    return {
        "answer": text,
        "citations": [{"ref": "Sessions/alpha.md",
                       "path": "Sessions/alpha.md", "title": "Alpha"}],
        "hits": [{"path": "Sessions/alpha.md", "title": "Alpha",
                  "heading": "Decision"}],
    }


def _concepts(term="durable vault chat", weight=0.7):
    return (
        '{"concepts":[{"term":"' + term + '","weight":'
        + str(weight)
        + ',"primary_path":"Sessions/alpha.md",'
          '"related_paths":["wiki/vault.md","invented.md"]}],'
          '"follow_up_questions":["What should the next window show?"],'
          '"topic_clusters":[{"label":"Chat grounding",'
          '"paths":["Sessions/alpha.md","wiki/vault.md","invented.md"]}]}'
    )


class BrainChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(brainchat, "STORE",
                                  Path(self.tmp.name) / "brain-chat.json")
        patch.start()
        self.addCleanup(patch.stop)

    @mock.patch("server.brainchat.suggest.complete", return_value=_concepts())
    @mock.patch("server.brainchat.vault.ask", return_value=_answer())
    @mock.patch("server.brainchat.vault.search", return_value=HITS)
    def test_first_turn_persists_grounded_chat_and_companions(
            self, search, ask, complete):
        session = brainchat.ask("What did I decide?")

        self.assertEqual(brainchat.current(), session)
        self.assertEqual(session["turns"][0]["question"],
                         "What did I decide?")
        self.assertEqual(session["concepts"][0]["turns"], 1)
        self.assertEqual(session["concepts"][0]["related_paths"],
                         ["wiki/vault.md"])
        self.assertEqual(session["topic_clusters"][0]["paths"],
                         ["Sessions/alpha.md", "wiki/vault.md"])
        self.assertEqual(session["cited"][0]["count"], 1)
        # The width is asked of the seam, never pinned to a number here --
        # asserting a literal is part of how the old 10 sat unexamined.
        search.assert_called_once_with(
            "What did I decide?",
            limit=brainchat.vault.ask_hits(brainchat.BUDGET))
        ask.assert_called_once_with("What did I decide?", hits=HITS)
        self.assertIn("PRIOR CONCEPTS", complete.call_args.args[0])

    @mock.patch("server.brainchat.suggest.complete",
                side_effect=[_concepts(), _concepts(weight=0.8)])
    @mock.patch("server.brainchat.vault.ask",
                side_effect=[_answer(), _answer("It still is [[Sessions/alpha.md]].")])
    @mock.patch("server.brainchat.vault.search", return_value=HITS)
    def test_later_turn_uses_context_and_accumulates_repeated_concepts(
            self, search, ask, complete):
        first = brainchat.ask("What did I decide?")
        second = brainchat.ask("Why?", first["id"])

        self.assertEqual(len(second["turns"]), 2)
        self.assertEqual(second["concepts"][0]["turns"], 2)
        self.assertAlmostEqual(second["concepts"][0]["weight"], 0.85)
        self.assertEqual(second["cited"][0]["count"], 2)
        contextual = ask.call_args_list[1].args[0]
        self.assertIn("EARLIER EXCHANGE", contextual)
        self.assertIn("CURRENT QUESTION:\nWhy?", contextual)

    @mock.patch("server.brainchat.suggest.complete",
                side_effect=RuntimeError("extractor unavailable"))
    @mock.patch("server.brainchat.vault.ask", return_value=_answer())
    @mock.patch("server.brainchat.vault.search", return_value=HITS)
    def test_concept_failure_does_not_discard_answer(self, search, ask, complete):
        session = brainchat.ask("What did I decide?")
        self.assertTrue(session["turns"][0]["answer"])
        self.assertEqual(session["concepts"], [])

    def test_new_chat_becomes_active_without_destroying_prior_chat(self):
        one = brainchat.new()
        two = brainchat.new()
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(brainchat.current()["id"], two["id"])


class ContextBudgetTest(unittest.TestCase):
    """The prompts are sized by modelbudget, and nothing is cut in silence.

    Every cap these pin used to be a literal: 10 retrieved passages read as
    8 by the concept prompt, the last 8 turns of a 40-turn session, and a
    2,400-character slice of each passage -- none of them ever compared to
    the window of the backend that reads them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(brainchat, "STORE",
                                  Path(self.tmp.name) / "brain-chat.json")
        patch.start()
        self.addCleanup(patch.stop)

    @mock.patch("server.brainchat.suggest.complete", return_value=_concepts())
    @mock.patch("server.brainchat.vault.ask", return_value=_answer())
    @mock.patch("server.brainchat.vault.search", return_value=HITS)
    def test_retrieval_width_moves_with_the_backend(
            self, search, ask, complete):
        """A smaller backend really does retrieve less.

        Deliberately no number: pinning one is how the old literal 10 sat
        unexamined for as long as it did, and it would break every time a
        budget or an engine ceiling moved. What must hold is that the width
        is ASKED of the seam, so it tracks whichever backend answers -- a
        module that went back to a literal would report the same width on
        both of these and fail here.
        """
        widths = []
        for room in (4_800, 10 ** 7):
            with mock.patch("server.modelbudget.context_chars",
                            return_value=room):
                brainchat.ask("What did I decide?")
                widths.append(search.call_args.kwargs["limit"])
        self.assertLess(widths[0], widths[1])
        self.assertGreaterEqual(widths[0], 1)   # never nothing to stand on

    @mock.patch("server.modelbudget.context_chars", return_value=40_000)
    def test_every_retrieved_passage_reaches_the_concept_prompt(self, chars):
        hits = [{"path": f"wiki/n{i}.md", "heading": "H", "text": "t" * 60}
                for i in range(10)]
        prompt = brainchat._concept_prompt("q", "a", hits, [])
        self.assertIn("--- CHUNK 10 |", prompt)   # every passage retrieved
        self.assertNotIn("omitted", prompt)

    @mock.patch("server.modelbudget.context_chars", return_value=40_000)
    def test_the_whole_conversation_survives_when_it_fits(self, chars):
        turns = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(12)]
        text = brainchat._transcript(turns)
        self.assertEqual(text.count("User: "), 12)   # was the last 8 only
        self.assertNotIn("omitted", text)

    @mock.patch("server.modelbudget.context_chars", return_value=10 ** 7)
    def test_a_retrieved_passage_is_never_one_the_engine_drops(self, chars):
        """The retrieval width must fit qocha's OWN joined-prompt cap.

        Two ceilings bind a grounded answer and only the smaller matters.
        On a large backend the engine's is the smaller, and a width counted
        on passage TEXT alone overruns it: qocha renders every hit under a
        header line and joins the blocks with a blank line, then truncates
        the join. A passage past that point is searched for, paid for, and
        dropped with nothing said -- the same silent-cut failure the budget
        seam exists to end, one layer along. Everything here is derived
        from vault's own constants, so raising a budget cannot break it and
        removing the reserve cannot pass it.
        """
        n = vault.ask_hits()
        self.assertGreater(n, 1, "the engine cap should bind on a big window")
        worst = "x" * (vault.ENGINE_CHUNK_CHARS
                       + vault.ENGINE_BLOCK_OVERHEAD - 2)   # 2 = the join
        self.assertLessEqual(len("\n\n".join([worst] * n)),
                             vault.ENGINE_PROMPT_CHARS)

    @mock.patch("server.modelbudget.context_chars", return_value=2_000)
    def test_what_does_not_fit_is_counted_not_dropped(self, chars):
        turns = [{"question": "q" * 900, "answer": "a" * 900}
                 for _ in range(6)]
        text = brainchat._transcript(turns)
        self.assertIn("omitted", text)
        # The running total binds, not the per-part ceiling `split` floors.
        self.assertLess(len(text), 2_400)


class BrainChatRouteTest(unittest.TestCase):
    """The static /chat routes must beat the older dynamic Find route."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app, raise_server_exceptions=False)

    @mock.patch("server.main.brainchat.current", return_value=None)
    def test_chat_get_has_versioned_session_envelope(self, current):
        response = self.client.get("/api/find/chat")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"session": None})

    @mock.patch("server.main.brainchat.ask",
                return_value={"id": "brain_test", "turns": []})
    def test_chat_post_is_not_captured_as_dynamic_find_query(self, ask):
        response = self.client.post("/api/find/chat", json={
            "question": "What changed?", "session_id": "brain_test",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["id"], "brain_test")
        ask.assert_called_once_with("What changed?", "brain_test")


if __name__ == "__main__":
    unittest.main()

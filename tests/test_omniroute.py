"""The omni router - rung 2 of the dictation door (claude/omni-router).

One model call classifies unprefixed palette prose into a validated
route. The contract under test is grounded-or-held: an off-vocabulary
intent, an unparseable reply, a dead backend all return None, so the
palette's deterministic rows can never be taken away by the layer that
exists only to sharpen them.

Run: .venv/bin/python -m unittest tests.test_omniroute
"""
import unittest
from unittest import mock

from server import omniroute


def _payload(**over):
    got = {"intent": "ask", "text": "insurance texts from Kim",
           "target": None, "why": "a question over messages"}
    got.update(over)
    import json
    return json.dumps(got)


class RouteValidation(unittest.TestCase):
    def _route(self, payload, text="find the insurance texts from Kim"):
        with mock.patch("server.suggest.complete", return_value=payload):
            return omniroute.route(text)

    def test_a_valid_reply_routes(self):
        r = self._route(_payload())
        self.assertEqual(r["intent"], "ask")
        self.assertEqual(r["text"], "insurance texts from Kim")

    def test_fenced_json_is_tolerated(self):
        r = self._route("```json\n" + _payload() + "\n```")
        self.assertEqual(r["intent"], "ask")

    def test_an_off_vocabulary_intent_is_held(self):
        self.assertIsNone(self._route(_payload(intent="search")))

    def test_an_empty_routed_text_falls_back_to_the_command(self):
        r = self._route(_payload(text=""), text="do the thing please")
        self.assertEqual(r["text"], "do the thing please")

    def test_an_unparseable_reply_is_held(self):
        self.assertIsNone(self._route("sorry, I cannot help"))

    def test_a_dead_backend_is_held_never_raised(self):
        with mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("no model")):
            self.assertIsNone(omniroute.route("open the daily brief now"))

    def test_blank_input_never_costs_a_model_call(self):
        with mock.patch("server.suggest.complete",
                        side_effect=AssertionError("must not be called")):
            self.assertIsNone(omniroute.route("   "))

    def test_target_and_why_are_bounded(self):
        r = self._route(_payload(intent="open", target="x" * 500,
                                 why="y" * 500))
        self.assertLessEqual(len(r["target"]), omniroute.TARGET_CAP)
        self.assertLessEqual(len(r["why"]), omniroute.WHY_CAP)

    def test_the_prompt_is_composed_from_the_intent_table(self):
        # the ideatags AXES rule: the instruction derives from the
        # vocabulary, so the two cannot drift
        prompt = omniroute.compose_prompt("hello there my friend")
        for intent, desc in omniroute.INTENTS.items():
            self.assertIn(f"- {intent}:", prompt)
            self.assertIn(desc[:40], prompt)
        self.assertIn("hello there my friend", prompt)

    def test_the_prompt_caps_what_we_send_not_what_was_said(self):
        long = "w" * (omniroute.TEXT_CAP * 2)
        prompt = omniroute.compose_prompt(long)
        self.assertNotIn("w" * (omniroute.TEXT_CAP + 1), prompt)


class ClientJoin(unittest.TestCase):
    """The client posts the path the server defines - the
    reader-with-no-writer shape, pinned at the join."""

    def test_the_endpoint_and_the_client_agree_on_the_path(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        main_py = (root / "server" / "main.py").read_text(encoding="utf-8")
        self.assertIn('post("/api/omni/route"', app_js)
        self.assertIn('"/api/omni/route"', main_py)


if __name__ == "__main__":
    unittest.main()

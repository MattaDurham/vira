"""The answer path lands the thing it found (2026-09-01, branch
claude/omni-ask-landing).

"Show me the insurance card that Casey texted me" reached the right photo
and still could not say what it was: the media narrator's row slimming
dropped the OCR field, so the model saw a caption of "WOOHOO" and called
the card unconfirmed while the index held "BlueCross BlueShield, Subscriber
Name ..." in full. And a HEAD on the thumbnail URL 404d while GET served
the bytes, which is how a dispatched session concluded the thumbnail did
not exist. Both pinned here as the JOIN — the real ask() with only the
model and the search stubbed, and the real routes through a test client.

Run: .venv/bin/python -m unittest tests.test_ask_landing
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import find, search

CARD = ("BlueCross BlueShield Subscriber Name PAT EXAMPLE "
        "Identification Number ZZZ000000000 Group Number 000000")


def _row(**kw):
    base = {"seq": 1, "kind": "photo", "id": 17443, "name": "PNG image.png",
            "when": "2026-08-03T12:12:00", "sender": "Casey", "person": None,
            "context": {"text": "WOOHOO", "from_me": False, "own": True},
            "from_me": False, "purged": True, "source": "imessage"}
    base.update(kw)
    return base


class TheNarratorSeesTheOcr(unittest.TestCase):
    PLAN = {"query": "insurance card", "person": None, "sender": None,
            "direction": None, "kind": ["photo"], "face_person": None,
            "since": None, "until": None}

    def _ask(self, rows):
        seen = {}

        def fake_complete(prompt):
            seen["prompt"] = prompt
            return "It is the BlueCross card."
        with mock.patch.object(search, "search", return_value=rows), \
             mock.patch("server.suggest.complete", side_effect=fake_complete):
            out = search.ask("show me the insurance card", plan=dict(self.PLAN))
        return out, seen["prompt"]

    def test_the_ocr_reaches_the_narrator(self):
        out, prompt = self._ask([_row(ocr=CARD)])
        self.assertIn("BlueCross BlueShield", prompt)
        self.assertIn("ZZZ000000000", prompt)
        self.assertEqual(out["answer"], "It is the BlueCross card.")

    def test_a_row_without_ocr_says_nothing_about_it(self):
        _, prompt = self._ask([_row()])
        self.assertNotIn('"ocr":', prompt)   # the results JSON carries no such key

    def test_the_prompt_tells_the_model_what_ocr_is(self):
        self.assertIn('"ocr"', search.NARRATE_PROMPT)
        self.assertIn("read off the image", search.NARRATE_PROMPT)

    def test_the_excerpt_is_bounded_and_stated(self):
        # the cap is a module constant with its reasoning beside it
        # (scripts/preflight_capdoc.py) and it binds at the row
        self.assertGreaterEqual(search.OCR_EXCERPT_CHARS, 300)
        self.assertLessEqual(search.OCR_EXCERPT_CHARS, 1000)


class TheAnswerRowsCarryTheOcr(unittest.TestCase):
    """find._row_line is what the messages/people answer and the sibling
    media rows read; a photo's OCR rides it so an ask whose primary is
    messages can still say the photo IS the card."""

    def test_a_media_row_states_the_text_on_it(self):
        line = find._row_line(3, _row(ocr=CARD[:60]))
        self.assertIn("a photo 'PNG image.png'", line)
        self.assertIn("text on it: BlueCross BlueShield", line)
        self.assertTrue(line.startswith("[3] 2026-08-03T12:12 from Casey"))

    def test_no_ocr_no_claim(self):
        self.assertNotIn("text on it", find._row_line(1, _row()))


class TheByteRoutesAnswerHead(unittest.TestCase):
    """FastAPI's @app.get registers GET alone, so a HEAD on the thumbnail
    404d while GET served it. Both byte routes answer HEAD now."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from server import main, media
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        f = Path(self.tmp.name) / "thumb.jpg"
        f.write_bytes(b"\xff\xd8jpeg")
        self.client = TestClient(main.app)
        for p in (mock.patch.object(media, "thumbnail", return_value=f),
                  mock.patch.object(media, "preview_file",
                                    return_value=(f, "image/jpeg", "PNG image.png"))):
            p.start()
            self.addCleanup(p.stop)

    def test_head_and_get_agree_on_the_thumbnail(self):
        self.assertEqual(self.client.head("/api/media/thumb/17443").status_code, 200)
        r = self.client.get("/api/media/thumb/17443")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"\xff\xd8"))

    def test_head_and_get_agree_on_the_file(self):
        self.assertEqual(self.client.head("/api/media/file/17443").status_code, 200)
        self.assertEqual(self.client.get("/api/media/file/17443").status_code, 200)

    def test_a_missing_thumbnail_is_still_a_404_on_both_verbs(self):
        from server import media
        with mock.patch.object(media, "thumbnail", return_value=None):
            self.assertEqual(self.client.get("/api/media/thumb/1").status_code, 404)
            self.assertEqual(self.client.head("/api/media/thumb/1").status_code, 404)

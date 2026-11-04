"""Reading-room pages: what the Reader lists and what a room's tab shows.

There is no read state (owner's call, 2026-11-04), so a page row carries an
item count and never a done count.

Run: .venv/bin/python -m unittest tests.test_reading
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import reading


GENERATOR_PAGE = """<!DOCTYPE html><html><head><title>Test Room</title></head>
<body><header><h1>Test Room</h1><p>Everything on one subject.</p></header>
<main id="list"></main>
<footer>Built 2026-07-20 by Vira.</footer>
<script>window.ROOM={"slug":"test-room"};window.DATA=[{"id":"aaa111","title":"A"},{"id":"bbb222","title":"B"},{"id":"ccc333","title":"C"}];</script>
</body></html>"""

HANDBUILT_PAGE = """<!DOCTYPE html><html><head><title>Hand Room</title></head>
<body><header><h1>Hand Room</h1><p>The full universe. Built 2026-07-17.</p></header>
<script>
const DATA = [{"id": "d1", "title": "One"}, {"id": "d2", "title": "Two"}];
const API = "/api/reading/hand-room/done";
</script></body></html>"""


class PageDetailsTest(unittest.TestCase):
    """The card-facing view: subtitle, item count, built date — parsed from
    both generations of room page (window.DATA and const DATA)."""

    def setUp(self):
        from server import readingroom
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        p = mock.patch.object(reading, "PAGES_DIR", base / "pages")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(readingroom, "ROOMS_DIR", base / "rooms")
        p.start()
        self.addCleanup(p.stop)
        (base / "pages").mkdir()
        self.addCleanup(self.tmp.cleanup)

    def page(self, name, text):
        (reading.PAGES_DIR / f"{name}.html").write_text(text, encoding="utf-8")

    def test_generator_page_yields_every_field(self):
        self.page("test-room", GENERATOR_PAGE)
        row = reading.page_details()[0]
        self.assertEqual(row["title"], "Test Room")
        self.assertEqual(row["subtitle"], "Everything on one subject.")
        self.assertEqual(row["built"], "2026-07-20")
        self.assertEqual(row["items"], 3)
        self.assertNotIn("done", row)

    def test_handbuilt_page_parses_too(self):
        self.page("hand-room", HANDBUILT_PAGE)
        row = reading.page_details()[0]
        self.assertEqual(row["items"], 2)
        self.assertEqual(row["built"], "2026-07-17")

    def test_unparseable_page_omits_items_never_claims_zero(self):
        self.page("odd", "<html><head><title>Odd</title></head><body></body></html>")
        row = reading.page_details()[0]
        self.assertNotIn("items", row)
        self.assertNotIn("done", row)


class ReadingPagesTest(unittest.TestCase):
    def setUp(self):
        from server import readingroom
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = Path(self.tmp.name) / "reading"
        for target, attr, val in (
                (reading, "PAGES_DIR", self.pages),
                (readingroom, "ROOMS_DIR", Path(self.tmp.name) / "rooms")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_dir_is_empty(self):
        self.assertEqual(reading.list_pages(), [])

    def test_pages_listed_with_titles(self):
        self.pages.mkdir(parents=True)
        (self.pages / "b-list.html").write_text(
            "<html><head><title>  Second\n Queue </title></head></html>")
        (self.pages / "a-list.html").write_text("<html>no title here</html>")
        (self.pages / "notes.txt").write_text("ignored")
        out = reading.list_pages()
        self.assertEqual(
            out,
            [{"name": "a-list", "title": "a-list",
              "url": "/reading/a-list.html", "native": False},
             {"name": "b-list", "title": "Second Queue",
              "url": "/reading/b-list.html", "native": False}])


if __name__ == "__main__":
    unittest.main()

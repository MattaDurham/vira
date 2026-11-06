"""One Reader: the Inflow, Vira's documents and the reading rooms in one list.

The owner's call (2026-11-06): no Inflow / Documents / room tabs, one search
and filter bar, and three views (Explore, Shelves, List) in place of Grid.
These pin the pieces that make it one list rather than three tabs:

- a capture from /api/ingestfeed becomes a document row every view can read
  (its kind is its source, its topics are its themes, it opens as a vault
  note, its first picture is its thumb);
- the base every view, count and filter reads is documents + captures;
- Group by Month buckets by year-month and labels it in words;
- the view switch offers Explore, Shelves and List, and Grid is gone.
"""

from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def _function(name):
    """The source of one top-level `function name(...) {...}` in app.js."""
    start = APP.index("function " + name + "(")
    depth = 0
    for i in range(APP.index("{", start), len(APP)):
        if APP[i] == "{":
            depth += 1
        elif APP[i] == "}":
            depth -= 1
            if depth == 0:
                return APP[start:i + 1]
    raise AssertionError("unbalanced " + name)


class OneList(unittest.TestCase):

    def test_the_base_is_documents_and_captures(self):
        self.assertIn("(readerDocs || []).concat(readerInflow || [])",
                      _function("rdocBase"))

    def test_no_tabs_and_no_separate_inflow_panel(self):
        head = _function("renderReaderHead")
        self.assertNotIn('"The Inflow"', head)
        self.assertNotIn('"Vira\'s Documents"', head)
        self.assertNotIn('id="reader-inflow"', HTML)

    def test_three_views_and_no_grid(self):
        self.assertIn('[...Object.entries(window.RDOC_VIEWS || {}).map(([v, x]) => '
                      '[v, x.label]), ["list", "List"]]', APP)
        self.assertNotIn('["grid", "Grid"]', APP)
        self.assertNotIn("renderRdocGrid", APP)
        # A saved Grid choice lands on Explore rather than an empty switch.
        self.assertIn('if (rdocView === "grid") rdocView = "explore";', APP)
        scripts = re.findall(r'<script src="/([^"]+)"', HTML)
        # Explore registers before Shelves, so the switch reads
        # Explore / Shelves / List.
        self.assertLess(scripts.index("reader-explore.js"),
                        scripts.index("reader-shelves.js"))

    def test_one_show_picker_covers_captures_and_documents(self):
        bar = _function("rdocBar")
        self.assertIn("readerShowPicker", bar)
        self.assertNotIn("rdoc-kchip", bar)
        picker = _function("readerShowPicker")
        self.assertIn('"Captures"', picker)
        self.assertIn('"Vira\'s documents"', picker)


HARNESS = r"""
const src = require("node:fs").readFileSync(0, "utf8");
const MONTHS = ["January","February","March","April","May","June","July",
  "August","September","October","November","December"];
const READER_CAPTURE_KINDS = { youtube: "YouTube", x: "X post", notes: "Notes capture",
  other: "other capture" };
const READER_KIND = Object.assign({}, READER_CAPTURE_KINDS);
const RDOC_UNTAGGED = " untagged";
let rdocGroup = "month";
const rdocTagsOf = (it, ax) => ((it.tags || {})[ax] || []);
eval(src);
const row = readerCaptureRow({
  id: "if1", path: "wiki/a.md", title: "A talk", source: "youtube",
  date: "2026-11-05", url: "https://youtu.be/x", blurb: "b",
  topics: ["ai", "cad"], images: [{ path: "wiki/assets/a/p 1.jpg" }],
});
const odd = readerCaptureRow({ id: "if2", path: "wiki/b.md", title: "B",
  source: "mystery", date: "", topics: [], images: [] });
process.stdout.write(JSON.stringify({
  row, odd,
  bucket: rdocBucket(row), undated: rdocBucket(odd),
  label: rdocGroupLabel("2026-11"),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CaptureRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        src = "\n".join(_function(n) for n in
                        ("readerCaptureRow", "rdocBucket", "readerKindPlural",
                         "rdocGroupLabel"))
        src = "const READER_KIND_PLURAL = {};\n" + src
        r = subprocess.run([shutil.which("node"), "-e", HARNESS], input=src,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_a_capture_reads_as_a_document_row(self):
        row = self.out["row"]
        self.assertEqual(row["kind"], "youtube")
        self.assertEqual(row["locator_kind"], "vault")
        self.assertEqual(row["locator"], "wiki/a.md")
        self.assertEqual(row["created"], "2026-11-05")
        self.assertEqual(row["tags"]["theme"], ["ai", "cad"])
        self.assertEqual(row["thumb"],
                         "/api/vault/thumb?path=wiki%2Fassets%2Fa%2Fp%201.jpg")
        self.assertTrue(row["capture"])

    def test_an_unknown_source_is_other_and_has_no_thumb(self):
        self.assertEqual(self.out["odd"]["kind"], "other")
        self.assertEqual(self.out["odd"]["thumb"], "")

    def test_month_buckets_and_labels(self):
        self.assertEqual(self.out["bucket"], "2026-11")
        self.assertEqual(self.out["undated"], " untagged")
        self.assertEqual(self.out["label"], "November 2026")


if __name__ == "__main__":
    unittest.main()

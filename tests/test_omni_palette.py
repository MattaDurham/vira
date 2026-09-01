"""The dictation door is held as a source contract.

Wispr Flow (or any dictation tool) is just a keyboard: it inserts the
transcript into whatever field holds focus. The omni layer therefore has
two halves in static/app.js - a FOCUS SINK that keeps an invisible input
focused whenever the Vira window is active and nothing real holds focus,
and a prefix grammar that turns a spoken label ("this is a tell...",
"ask...", "open...", "start a session...") into a pre-selected palette
row routed at existing machinery.

A Python suite cannot run the browser, but it can pin the parts whose
silent loss would be invisible in a demo: that the sink never steals
focus from a real field, that it is desktop-only, that the grammar names
all five spoken intents, that each intent lands on the HOUSE endpoint
rather than a second implementation, and that the sink's CSS cannot eat
a click. Held the way test_atlas3d_nav.py holds the camera - comments
stripped before every scan, so a guard surviving only as the comment
explaining it does not pass.

Mutation-checked when written: removing the idle()/eligible() guard from
claim, dropping the isDesktop early-return, seeding the palette without
the seed parameter, retargeting tell at a non-journal endpoint, and
dropping pointer-events from the sink CSS each fail at least one case.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"^\s*//.*$", "", s, flags=re.M)


def _block(marker):
    """Balanced body starting at `marker`, comments stripped.

    Counts {} AND [] together, so it closes an array table at its `]`
    rather than at the first row's `}`. Regex character classes inside
    the scanned code are balanced pairs, so they cancel out.
    """
    start = SRC.index(marker)
    depth, i, opened = 0, start, False
    while i < len(SRC):
        if SRC[i] in "{[":
            depth, opened = depth + 1, True
        elif SRC[i] in "}]":
            depth -= 1
            if opened and depth == 0:
                return _strip_comments(SRC[start:i + 1])
        i += 1
    raise AssertionError(f"unbalanced block at {marker!r}")


class TheGrammarNamesTheSpokenIntents(unittest.TestCase):
    """OMNI_PREFIXES is the one table; the five doors must all be in it."""

    def setUp(self):
        # reach guard first: a scan over a missing table would pass every
        # negative assertion below it
        self.assertIn("const OMNI_PREFIXES", SRC)
        self.table = _block("const OMNI_PREFIXES")

    def test_all_five_intents_are_rows(self):
        for intent in ("tell", "ask", "idea", "session", "open"):
            self.assertIn(f'intent: "{intent}"', self.table, intent)

    def test_the_spoken_sentence_forms_are_accepted(self):
        # the owner's own phrasings, not just bare labels (regex literals
        # are code, not comments - they survive the strip)
        for phrase in ("this is a tell", "this is an ask",
                       "start a session", "open"):
            self.assertIn(phrase, self.table, phrase)

    def test_tell_me_files_as_ask_not_tell(self):
        # "tell me about X" is a question. The ask row carries the phrase,
        # sits ABOVE tell in the table, and tell's own pattern refuses it -
        # so order alone never decides.
        ask_pos = self.table.index('intent: "ask"')
        tell_pos = self.table.index('intent: "tell"')
        self.assertLess(ask_pos, tell_pos)
        ask_row = self.table[ask_pos:tell_pos]
        self.assertIn("tell me", ask_row)
        tell_row = self.table[tell_pos:self.table.index('intent: "idea"')]
        self.assertIn(r"(?!\s+me", tell_row)


class TheRoutesLandOnHouseEndpoints(unittest.TestCase):
    """Each intent reuses existing machinery - never a second composer."""

    def setUp(self):
        self.assertIn("const OMNI_ROUTES", SRC)
        self.routes = _block("const OMNI_ROUTES")

    def test_tell_saves_a_journal_note(self):
        self.assertIn("/api/brief/note", self.routes)
        self.assertIn("watchBriefNote", self.routes)

    def test_ask_hands_off_to_find_with_the_ask_flag(self):
        self.assertIn("findSetQuery", self.routes)
        self.assertIn("ask: true", self.routes)

    def test_idea_files_into_the_queue(self):
        self.assertIn("/api/ideas", self.routes)
        self.assertIn('source: "omni"', self.routes)

    def test_session_uses_the_one_dispatch_helper(self):
        self.assertIn("launchJob(text)", self.routes)

    def test_the_palette_consults_the_grammar(self):
        self.assertIn("omniRows(", _block("function paletteMatches"))


class TheSinkNeverSteals(unittest.TestCase):
    """The sink claims focus only from the floor, and only on desktop."""

    def setUp(self):
        self.assertIn("function initOmniSink", SRC)
        self.body = _block("function initOmniSink")

    def test_desktop_only(self):
        self.assertIn("if (!isDesktop) return;", self.body)

    def test_focus_is_guarded_by_idle_and_eligible(self):
        # the ONLY sink.focus call sits behind the guard join - removing
        # either check breaks this exact conjunction
        self.assertEqual(self.body.count("sink.focus"), 1)
        self.assertRegex(
            self.body,
            r"if \(document\.hasFocus\(\) && idle\(\) && eligible\(\)\)\s*"
            r"sink\.focus")

    def test_idle_means_focus_on_the_floor(self):
        self.assertIn("document.body", self.body)
        self.assertIn("document.documentElement", self.body)

    def test_it_yields_to_sheets_the_palette_and_a_live_selection(self):
        self.assertIn("openSheets.length", self.body)
        self.assertIn("paletteOpen", self.body)
        self.assertIn("isCollapsed", self.body)

    def test_dictated_text_opens_the_palette_seeded(self):
        self.assertIn("togglePalette(true, text)", self.body)
        # ...and togglePalette really carries a seed parameter that lands
        # in the input (a reader with no writer is the documented trap)
        toggle = _block("function togglePalette")
        self.assertIn("seed", toggle)
        self.assertIn('seed || ""', toggle)


class TheSinkCssCannotEatAClick(unittest.TestCase):
    def test_pointer_events_none(self):
        m = re.search(r"\.omni-sink \{([^}]*)\}", CSS)
        self.assertIsNotNone(m, ".omni-sink rule missing from style.css")
        self.assertIn("pointer-events: none", m.group(1))
        self.assertIn("opacity: 0", m.group(1))


if __name__ == "__main__":
    unittest.main()

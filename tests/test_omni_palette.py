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

    def test_a_press_in_flight_is_never_claimed_over(self):
        # The press that starts a click-and-drag collapses the selection,
        # and that collapse fires selectionchange - so a sink that only
        # tests isCollapsed focuses itself mid-drag and kills the selection
        # (measured live 2026-09-03: 0 characters selected with the sink
        # present, 54 with it detached). The guard is the FIRST test in
        # eligible, and both ends of the press are wired in capture.
        self.assertRegex(self.body, r"const eligible = \(\) => \{\s*if \(pressed\) return false;")
        self.assertIn('document.addEventListener("pointerdown", down, true)', self.body)
        self.assertIn('document.addEventListener("pointerup", up, true)', self.body)
        self.assertIn('document.addEventListener("pointercancel", up, true)', self.body)
        # the release re-runs the claim, so a plain click still hands the
        # floor to the sink
        self.assertRegex(self.body, r"const up = \(\) => \{ pressed = false; claimSoon\(\); \}")

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


class TheRouterOnlyEverAddsARow(unittest.TestCase):
    """Rung 2 (claude/omni-router). The routed row pins first when it
    lands; the deterministic rows must survive it untouched - a null
    route, an error, a dead backend leave the palette exactly as rung 1
    shipped it."""

    def setUp(self):
        self.assertIn("function omniRouteKick", SRC)
        self.kick = _block("function omniRouteKick")
        self.rows = _block("function omniRows")
        self.routed = _block("function omniRoutedRow")

    def test_the_deterministic_rows_survive_the_router(self):
        # the trailing branch still offers every intent - the router can
        # only PREPEND, and only when its route resolved
        self.assertIn('["tell", "ask", "idea", "session"]', self.rows)
        self.assertIn("omniRouteKick(t)", self.rows)

    def test_the_routed_row_reuses_the_house_routes(self):
        # OMNI_ROUTES is the one composer; a second dispatch path here
        # would drift from the deterministic rows' behaviour
        self.assertIn("OMNI_ROUTES[r.intent]", self.routed)
        self.assertIn("base.run(text)", self.routed)

    def test_a_held_route_renders_nothing(self):
        self.assertIn("return null", self.routed)
        self.assertIn("if (!base || !text) return null;", self.routed)

    def test_an_unresolvable_open_is_held_not_a_dead_row(self):
        resolve = _block("function omniResolveOpen")
        self.assertIn("return null", resolve)
        # resolved both directions against windows AND people, like the
        # spoken "open ..." prefix
        self.assertIn("wt.includes(res) || res.includes(wt)", resolve)
        self.assertIn("peopleCache", resolve)

    def test_the_call_is_debounced_and_cached_per_text(self):
        self.assertIn("setTimeout", self.kick)
        # a repeat of the same text never re-spends the call
        self.assertIn("omniRouted.text === text && omniRouted.state",
                      self.kick)
        # a stale answer is dropped when the input moved on
        self.assertIn("omniRouted.text !== text", self.kick)

    def test_the_pending_state_is_shown_never_blocking(self):
        palette = _block("function renderPalette")
        self.assertIn("omniRoutePending", palette)


if __name__ == "__main__":
    unittest.main()


class AQuestionIsNeverFiledByDefault(unittest.TestCase):
    """2026-09-01: "Show me the insurance card that Casey texted me" was
    filed as a Tell — the deterministic first row — because Enter beat a
    four-second route, and a coding session was dispatched to "search the
    messages and show it". Two guards, each pinned: question-shaped prose
    puts Ask first without waiting for any model, and Enter while a route
    is pending ARMS rather than runs.

    Mutation-checked when written: dropping `omniAsksFirst` from the
    trailing order, removing the arm branch from the Enter handler,
    dropping the fire from omniRouteKick, and hardcoding a tell in
    omniFireArmed each fail at least one case."""

    def _question_re(self):
        m = re.search(r"const OMNI_QUESTION_RE = /(.*)/i;", SRC)
        self.assertIsNotNone(m, "OMNI_QUESTION_RE is gone")
        return re.compile(m.group(1), re.I)

    def test_the_question_grammar_reads_the_real_sentence(self):
        rx = self._question_re()
        for t in ("Show me the insurance card that Casey texted me the other day.",
                  "find the text with the account numbers",
                  "Pull up Casey's profile",
                  "Did Alex send me that photo",
                  "what's the address of the cabin",
                  "Can you show me the receipt from last week"):
            self.assertTrue(rx.match(t), t)
        for t in ("Casey sent me the new insurance card this month",
                  "The rent went up to 4200",
                  "What Casey said was that the girls stay up til 8",
                  "Which reminds me, Alex is moving in October"):
            self.assertFalse(rx.match(t), t)

    def test_question_shaped_prose_puts_ask_first(self):
        body = _block("function omniRows(")
        self.assertRegex(body, r'omniAsksFirst\(t\)\s*\?\s*\["ask",\s*"tell"')
        self.assertRegex(body, r':\s*\["tell",\s*"ask"')

    def test_enter_while_a_route_is_pending_arms(self):
        # the palette's own handlers live inside buildPalette — the Find chat
        # composer registers an earlier "keydown" listener of its own
        body = _block("function buildPalette(")
        body = body[body.index('addEventListener("keydown"'):]
        enter = body[body.index('e.key === "Enter"'):body.index('e.key === "Escape"')]
        self.assertIn("omniRoutePending(q)", enter)
        self.assertIn("omniArm(q)", enter)
        self.assertIn("paletteIdx === 0", enter)   # an arrowed pick still runs at once

    def test_the_route_landing_fires_the_armed_pick(self):
        body = _block("function omniRouteKick(")
        self.assertIn("omniArmed === text", body)
        self.assertIn("omniFireArmed()", body)

    def test_the_armed_pick_is_the_first_row_never_a_fixed_intent(self):
        body = _block("function omniFireArmed(")
        self.assertIn("paletteMatches(q)[0]", body)
        self.assertNotIn("OMNI_ROUTES", body)

    def test_closing_or_editing_disarms(self):
        self.assertIn("omniDisarm()", _block("function togglePalette("))
        body = _block("function buildPalette(")
        i = body.index('addEventListener("input"')
        j = body.index('addEventListener("keydown"')
        self.assertIn("omniDisarm()", body[i:j])

    def test_a_redirected_question_hands_off_to_find(self):
        body = _block("function watchBriefNote(")
        self.assertIn('u.redirect === "ask"', body)
        self.assertIn("openFindQuery(e.text, { ask: true })", body)

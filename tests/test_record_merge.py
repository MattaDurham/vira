"""Source contracts for the Runs + Record merge (2026-08-27) and its
retirement (2026-11-04).

The Work window's Runs and Record tabs became ONE chronological ledger
(tab id `live`, later "Results"), with Rules and Deferred & Dropped as
list-swapping chips. On 2026-11-04 that pane retired into Fleet > History:
Rules is History's "Sessions that broke a rule" filter and Filed is the
Ideas tab's Filed filter. RetiredResults pins that every retired id still
lands, on History; the stream contracts below still hold for the run code
other surfaces share.

They read the SOURCE because the suite cannot execute app.js.

Run: .venv/bin/python -m unittest tests.test_record_merge
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _block(src, marker, span=2500):
    at = src.index(marker)
    return src[at:at + span]


class RetiredResults(unittest.TestCase):
    """Work > Results and its folded list retired on 2026-11-04: history is
    Fleet > History. Every retired tab id still lands, on History."""

    def test_the_work_tabs_carry_no_results_or_record_button(self):
        seg = _block(HTML, 'id="work-tabs"', 700)
        seg = seg[:seg.index("</div>")]
        for tab in ("record", "live", "results"):
            self.assertNotIn(f'data-tab="{tab}"', seg)
        self.assertIn('data-tab="queue"', seg)
        self.assertIn('data-tab="dispatch"', seg)

    def test_setworktab_sends_the_retired_ids_to_history(self):
        aliases = _block(APP, "const WORK_TAB_ALIAS = {", 300).split("}", 1)[0]
        for old in ("record", "results", "live"):
            self.assertIn(f'{old}: "history"', aliases)
        self.assertIn('ideas: "queue"', aliases)
        self.assertIn('automations: "dispatch"', aliases)
        body = _block(APP, "function setWorkTab(tab", 500)
        self.assertIn("tab = WORK_TAB_ALIAS[tab] || tab;", body)
        self.assertIn('if (tab === "history") {', body)
        self.assertIn("openHistoryView();", body)

    def test_the_hash_routes_send_results_to_history(self):
        work_route = _block(APP, '"work": (rest) =>', 500)
        self.assertIn('["live", "record", "results"].includes(t)) { openHistoryView(); return; }', work_route)
        fleet_route = _block(APP, '"fleet": (rest) =>', 400)
        self.assertIn('["history", "timeline", "week"].includes(project)', fleet_route)
        for route in ('"jobs": () => openHistoryView()', '"results": () => openHistoryView()',
                      '"history": (rest) => openHistoryView('):
            self.assertIn(route, APP)

    def test_no_markup_holds_the_retired_pane(self):
        for needle in ("work-live-pane", "work-results-root", 'id="runs-filter"',
                       'id="runs-list"', "work-rules-list", "work-filed-list", "work-advanced"):
            self.assertNotIn(needle, HTML, f"{needle} still in index.html")

    def test_filed_is_a_filter_of_the_ideas_tab(self):
        queue = _block(HTML, 'id="work-queue-pane"', 4000)
        self.assertIn('id="idea-filed"', queue)
        self.assertIn("function setIdeaFiled(on) {", APP)
        render = _block(APP, "function renderQueue() {", 1800)
        self.assertIn("if (ideaFiledView) {", render)
        self.assertIn("renderFiled(list, filed);", render)
        pointer = _block(APP, "function appendFiledPointer(list, filed) {", 900)
        self.assertIn("setIdeaFiled(true)", pointer)


class MergedStreamContract(unittest.TestCase):
    def test_changelog_job_entries_dedupe_by_job_id(self):
        # One piece of work never renders twice: a kind-"job" changelog
        # entry is dropped when its job_id is already covered by a ledger
        # row, a session row, a flow stage or an unlanded row. (The server
        # half — the entry carrying job_id at all — is pinned in
        # tests/test_changelog.py.)
        self.assertIn(
            'e.kind !== "job" || (e.job_id && !covered.has(e.job_id))', APP)

    def test_a_shipped_group_is_never_dated_now(self):
        # The regression the day-ledger redesign exists to kill: the
        # dateless "unfiled" bucket was stamped Date.now() at render, so
        # seven weeks of work read "Today · shipped 0s ago". A group
        # without a date is now skipped, never handed an invented time.
        self.assertNotIn('Date.now() / 1000;   // the "recent / unfiled"',
                         APP)
        fold = _block(APP, "runsState.shipped.forEach", 1400)
        self.assertIn("if (!g.date) return;", fold)

    def test_a_no_retro_day_states_its_own_gap(self):
        # An honest label replaces the lie: the card names the gap in its
        # title fallback and in its meta line.
        fold = _block(APP, "runsState.shipped.forEach", 1400)
        self.assertIn('g.no_retro ? "No retro yet" : "Session"', fold)
        self.assertIn('g.no_retro ? " · no retro yet" : ""', fold)

    def test_paging_is_counted_and_inside_the_signature(self):
        # Older history makes the list long, so it pages — a bounded render
        # with a COUNTED Show more, never a silent cap — and the page size
        # rides the repaint signature so the Show more click rebuilds
        # through the same guard the armed confirm relies on.
        self.assertIn("Show more (${shown.length - runsShown} more)", APP)
        sig = _block(APP, "const sig = [runsFilter, runsQuery", 200)
        self.assertIn("runsShown", sig)


if __name__ == "__main__":
    unittest.main()

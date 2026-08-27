"""Source contracts for the Runs + Record merge (2026-08-27).

The Work window's Runs tab (flow runs + sessions + unlanded branches) and
Record tab (joblog history + the retro-derived Shipped changelog) were two
timelines of one subject, split by recency. They are ONE chronological
ledger now — tab id `live`, retitled "Record" — and the old Record tab's
two non-timeline segs (Rules, Deferred & Dropped) survive as the stream's
list-swapping chips.

These are option contracts in the test_applications style: every filter
chip in the markup must be named by the code that handles it, the retired
tab id must be aliased everywhere a stored value could still carry it, and
no path may still address the deleted pane. They read the SOURCE because
the suite cannot execute app.js; each was checked against its mutation
(chip with no handler, alias removed, a stale #work-record-pane caller)
before landing.

Run: .venv/bin/python -m unittest tests.test_record_merge
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CAPTURE = (ROOT / "static" / "tour" / "capture.py").read_text(
    encoding="utf-8")


def _block(src, marker, span=2500):
    at = src.index(marker)
    return src[at:at + span]


def _chip_values():
    """Every data-run value inside the #runs-filter seg."""
    seg = _block(HTML, 'id="runs-filter"', 1200)
    seg = seg[:seg.index("</div>")]
    vals = re.findall(r'data-run="([a-z]+)"', seg)
    assert vals, "no data-run chips found — the scan lost its target"
    return vals


def _handled_values():
    """RUN_KINDS keys + RUN_VIEWS entries, parsed out of app.js."""
    kinds_src = _block(APP, "const RUN_KINDS = {", 400)
    kinds_src = kinds_src[:kinds_src.index("}")]
    kinds = re.findall(r"(\w+):\s*\"", kinds_src)
    views_src = _block(APP, "const RUN_VIEWS = [", 200)
    views_src = views_src[:views_src.index("]")]
    views = re.findall(r"\"(\w+)\"", views_src)
    assert kinds and views, "RUN_KINDS / RUN_VIEWS scan lost its target"
    return kinds, views


class FilterChipContract(unittest.TestCase):
    def test_every_chip_is_handled(self):
        # A chip the code does not know is a dead control; a handled value
        # with no chip is a filter nobody can reach. The two lists must be
        # the same set.
        kinds, views = _handled_values()
        self.assertEqual(sorted(_chip_values()), sorted(kinds + views))

    def test_the_swap_views_are_rules_and_filed(self):
        # Rules and Filed are not runs: they SWAP the pane's list rather
        # than filter the chronology. Anything added to RUN_VIEWS needs its
        # own list element and applyRunsView branch.
        _, views = _handled_values()
        self.assertEqual(sorted(views), ["filed", "rules"])

    def test_a_saved_filter_value_falls_back(self):
        # The persisted key (vira-runs-filter) can hold a value a later
        # build no longer offers; it must fall back to All, never dead-end.
        self.assertIn('if (!(f in RUN_KINDS) && !RUN_VIEWS.includes(f)) '
                      'f = "all";', APP)


class RetiredTabAlias(unittest.TestCase):
    def test_the_work_tabs_carry_no_record_button(self):
        seg = _block(HTML, 'id="work-tabs"', 700)
        seg = seg[:seg.index("</div>")]
        self.assertNotIn('data-tab="record"', seg)
        self.assertIn('data-tab="live"', seg)

    def test_setworktab_normalizes_the_retired_id(self):
        # WORK_TAB_ALIAS is what keeps #work/record deep links, saved
        # values and pre-merge callers landing on the merged pane.
        self.assertIn('const WORK_TAB_ALIAS = { record: "live" };', APP)
        body = _block(APP, "function setWorkTab(tab", 300)
        self.assertIn("tab = WORK_TAB_ALIAS[tab] || tab;", body)

    def test_the_hash_route_still_accepts_record(self):
        # #work/record must keep working — the route hands the retired id
        # to setWorkTab, whose alias lands it.
        work_route = _block(APP, '"work": (rest) =>', 500)
        self.assertIn('"record"', work_route)

    def test_no_path_addresses_the_deleted_pane(self):
        # The pane, its list and its seg are gone; a surviving reference is
        # a caller that renders into nothing (the reader-with-no-writer
        # shape, inverted). The alias comment in app.js is the one
        # permitted mention of the retired id.
        for src, name in ((HTML, "index.html"), (CAPTURE, "capture.py")):
            for needle in ("work-record-pane", "work-record-list",
                           "record-filter", "data-rec"):
                self.assertNotIn(needle, src, f"{needle} still in {name}")
        for needle in ("#work-record-pane", "#work-record-list",
                       "#record-filter", "setRecordFilter", "loadRecord(",
                       "renderRecord("):
            self.assertNotIn(needle, APP, f"{needle} still in app.js")

    def test_the_swap_lists_live_in_the_merged_pane(self):
        # Rules and Filed render into their own elements INSIDE the live
        # pane, so the chips can swap them in without a second tab.
        pane = _block(HTML, 'id="work-live-pane"', 3000)
        pane = pane[:pane.index("</section>")]
        self.assertIn('id="work-rules-list"', pane)
        self.assertIn('id="work-filed-list"', pane)
        self.assertIn('id="runs-list"', pane)


class MergedStreamContract(unittest.TestCase):
    def test_changelog_job_entries_dedupe_by_job_id(self):
        # One piece of work never renders twice: a kind-"job" changelog
        # entry is dropped when its job_id is already covered by a ledger
        # row, a session row, a flow stage or an unlanded row. (The server
        # half — the entry carrying job_id at all — is pinned in
        # tests/test_changelog.py.)
        self.assertIn(
            'e.kind !== "job" || (e.job_id && !covered.has(e.job_id))', APP)

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

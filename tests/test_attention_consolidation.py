"""Static contracts for the one-shell Attention consolidation.

These tests pin information architecture seams that are easy to regress while
editing a large client file: retired peer windows must not reappear, old ids
must land on an explicit lane, and source/context gestures must target exact
objects rather than generic module landing pages.
"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class AttentionConsolidationContracts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_one_top_level_surface_replaces_four_peer_windows(self):
        self.assertIn('id="view-attention"', self.html)
        for retired in ("brief", "review", "triage", "subsviz"):
            self.assertNotIn(f'id="view-{retired}"', self.html)
            self.assertIsNone(re.search(
                rf'\{{\s*id:\s*"{retired}"\s*,\s*title:', self.app))

    def test_the_three_cognitive_lanes_and_picker_drill_in_exist(self):
        for lane in ("now", "day", "decide", "picker"):
            self.assertIn(f'id="attention-{lane}-pane"', self.html)
        self.assertIn('class="subsviz-frame" id="subsviz-frame"', self.html)

    def test_retired_ids_resolve_to_attention_lanes(self):
        self.assertIn(
            'const ATTENTION_ALIAS = { brief: "day", review: "decide", '
            'subsviz: "picker" };', self.app)
        self.assertIn(
            'const MDOCK_DEFAULT = ["feed", "people", "work", '
            '"attention", "find"]', self.app)

    def test_visual_and_full_source_context_are_first_class(self):
        self.assertIn('class="attention-hero attention-hero-now"', self.html)
        self.assertIn('id="attention-source-text"', self.html)
        self.assertIn('/api/review/context?id=', self.app)
        self.assertIn('.review-visual img, .review-visual video', self.css)

    def test_now_is_thumbnail_led_and_decisions_are_independent_cards(self):
        self.assertIn('class="attention-filters" id="review-filters"',
                      self.html)
        self.assertIn('`attn-item attn-kind-${kind}`', self.app)
        self.assertIn('"review-card " + reviewTypeClass', self.app)
        self.assertIn('let reviewFilter = "all"', self.app)
        self.assertIn('column-width: 255px', self.css)

    def test_attention_cards_activate_their_one_safe_primary_destination(self):
        self.assertIn(
            'cardAction(row, () => verb.run(btn), { hint: verb.title });',
            self.app)
        self.assertIn(
            'cardAction(row, () => openReviewTarget(it), {', self.app)
        self.assertIn('if (!it.open) {\n    openReviewContext(it);', self.app)
        self.assertIn('.attn-item.card-actionable:hover', self.css)
        self.assertIn('.review-card.card-actionable:hover', self.css)

    def test_now_renders_one_newest_first_chronology(self):
        self.assertIn('briefSection(body, "Newest activity first")', self.app)
        self.assertIn('new Map(cards.map((c) => [c.card.req_id, c]))',
                      self.app)
        self.assertNotIn('briefSection(body, "Waiting on you")', self.app)
        self.assertNotIn('briefSection(body, "Working")', self.app)

    def test_revealed_destinations_hold_a_strong_ten_second_highlight(self):
        self.assertIn('const REVEAL_HIGHLIGHT_MS = 10000;', self.app)
        self.assertEqual(self.app.count('revealHighlight(node);'), 4)
        self.assertIn('outline: 2px solid var(--accent)', self.css)

    def test_record_card_click_opens_a_fully_expanded_focus_view(self):
        self.assertIn(
            'cardAction(card, () => openOrphanFocus(it)', self.app)
        self.assertIn(
            'const card = runCard(orphanRunItem(it), { focused: true });',
            self.app)
        self.assertIn('inner.open = !!opts.expandAll;', self.app)
        self.assertIn('context.open = true;', self.app)
        self.assertIn('"Full context — opens in the foreground"', self.app)
        self.assertIn('.run-focus-scrim', self.css)
        self.assertIn('.run-focus-card .run-ctx-body', self.css)

    def test_attention_reveal_opens_the_same_focus_view(self):
        self.assertIn('if (orphan) openOrphanFocus(orphan);', self.app)

    def test_attention_prose_wraps_instead_of_ellipsizing(self):
        self.assertIn('Global to the combined Attention module', self.css)
        self.assertIn('text-overflow: clip; overflow-wrap: anywhere', self.css)
        self.assertIn('#view-attention .brief-row { flex-wrap: wrap;',
                      self.css)

    def test_context_has_a_visual_fallback_when_source_media_is_absent(self):
        self.assertIn('"attention-context-map"', self.app)
        self.assertIn('"Review evidence", "full context below"', self.app)
        self.assertIn('.attention-map-flow', self.css)

    def test_attention_verbs_reveal_exact_objects(self):
        self.assertIn(
            'run: () => revealOrphan(r.orphan_key, r.orphan_branch)',
            self.app)
        self.assertIn('n.dataset.runBranch === branch', self.app)
        self.assertIn(
            'card.dataset.runBranch = it.src.branch || "";', self.app)
        self.assertIn('if (runsLoadPromise) return runsLoadPromise;',
                      self.app)
        self.assertIn('run: () => revealBoardsHealth()', self.app)
        self.assertNotIn(
            'run: () => { openApp("work"); setWorkTab("live"); }', self.app)

    def test_live_attention_always_surfaces_the_now_lane(self):
        self.assertIn('if (fresh.length && open) setAttentionTab("now")',
                      self.app)
        self.assertIn('setAttentionTab("now", { defer: true });\n    '
                      'openWindow("attention")', self.app)
        self.assertIn('setAttentionTab("now", { defer: true });\n      '
                      'openApp("attention")', self.app)


if __name__ == "__main__":
    unittest.main()

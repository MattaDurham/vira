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
        cls.results = (ROOT / "static" / "work-results.js").read_text(encoding="utf-8")

    def test_one_top_level_surface_replaces_four_peer_windows(self):
        self.assertIn('id="view-attention"', self.html)
        for retired in ("brief", "review", "triage", "subsviz"):
            self.assertNotIn(f'id="view-{retired}"', self.html)
            self.assertIsNone(re.search(
                rf'\{{\s*id:\s*"{retired}"\s*,\s*title:', self.app))

    def test_the_three_cognitive_lanes_and_picker_drill_in_exist(self):
        for lane in ("now", "day", "decide", "inbox", "picker"):
            self.assertIn(f'id="attention-{lane}-pane"', self.html)
        tabs = self.html.split('id="attention-tabs"', 1)[1].split("</div>", 1)[0]
        self.assertEqual(re.findall(r'data-tab="([^"]+)"', tabs), ["now", "decide", "inbox"])
        self.assertIn('name === "day" && tab === "now"', self.app)
        self.assertIn('class="subsviz-frame" id="subsviz-frame"', self.html)

    def test_retired_ids_resolve_to_attention_lanes(self):
        self.assertIn(
            'const ATTENTION_ALIAS = { brief: "day", review: "decide", '
            'subsviz: "picker" };', self.app)
        self.assertIn(
            'const MDOCK_DEFAULT = ["feed", "people", "work", '
            '"attention", "find"]', self.app)

    def test_visual_and_full_source_context_are_first_class(self):
        self.assertIn('id="attention-live-decisions"', self.html)
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
            'cardAction(row, () => verb.run(btn, row), { hint: verb.title });',
            self.app)
        self.assertIn(
            'cardAction(row, () => openReviewTarget(it), {', self.app)
        self.assertIn('if (!it.open) {\n    openReviewContext(it);', self.app)
        self.assertIn('.attn-item.card-actionable:hover', self.css)
        self.assertIn('.review-card.card-actionable:hover', self.css)

    def test_today_keeps_day_context_and_links_counted_activity_to_work(self):
        self.assertIn('briefSection(body, "In motion"', self.app)
        self.assertIn('activity.slice(0, 4)', self.app)
        self.assertIn('"See all work (" + activity.length', self.app)
        self.assertIn('r.kind !== "assistant" && r.kind !== "review"', self.app)
        self.assertIn('window.ViraAssistant?.load();', self.app)

    def test_pending_answers_keep_their_nodes_across_unrelated_polls(self):
        block = self.app.split("function renderAttention()", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('const id = c.card.req_id;', block)
        self.assertIn('if (!attentionDecisionNodes.has(id))', block)
        self.assertIn('if (node.parentNode !== decisions) decisions.appendChild(node)', block)
        self.assertNotIn('decisions.replaceChildren', block)
        self.assertNotIn('decisions.innerHTML', block)
        self.assertIn('if (!active.has(id))', block)

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

    def test_foreground_review_is_visual_and_retroactive(self):
        self.assertIn('function orphanVisualBrief(c)', self.app)
        self.assertIn('body.appendChild(orphanVisualBrief(c));', self.app)
        self.assertIn('function orphanChangeAreas(paths)', self.app)
        self.assertIn('/api/orphanwork/visual?key=', self.app)
        self.assertIn('"Decision brief"', self.app)
        self.assertIn('"run-brief-table"', self.app)
        self.assertIn('.run-brief-flow', self.css)
        self.assertIn('.run-brief-visuals', self.css)

    def test_attention_reveal_opens_the_same_focus_view(self):
        self.assertIn('openOrphanFocus(orphan);', self.app)
        self.assertIn('requestAnimationFrame(() => trace.cancel());',
                      self.app)

    def test_attention_to_forge_wait_has_an_honest_signal_trace(self):
        self.assertIn(
            'function beginOrphanTrace(sourceNode, branch = "", summary = {})',
                      self.app)
        self.assertIn(
            'const trace = beginOrphanTrace(sourceNode, branch, summary);',
            self.app)
        self.assertIn('trace.stage("forge");', self.app)
        self.assertIn('trace.stage("context");', self.app)
        self.assertIn('trace.stage("ready");', self.app)
        self.assertIn('"Still gathering full context"', self.app)
        self.assertIn('"Nothing will be hidden"', self.app)
        self.assertIn('const clock = setInterval(updateClock, 1000);', self.app)
        self.assertIn('.run-trace-source', self.css)
        self.assertIn('.run-trace-target', self.css)
        self.assertIn('.run-trace-packet', self.css)
        self.assertIn('@keyframes run-trace-draw', self.css)
        self.assertIn('@media (prefers-reduced-motion: reduce)', self.css)
        self.assertNotIn('run-trace-percent', self.app + self.css)

    def test_signal_trace_first_frame_is_a_truthful_evidence_receipt(self):
        self.assertIn('"Instant branch evidence"', self.app)
        self.assertIn('"Decision route"', self.app)
        self.assertIn('"Full context manifest"', self.app)
        self.assertIn('"Fills only when Forge returns evidence"', self.app)
        self.assertIn('countFrom(/(\\d+)\\s+dirty files?/i)', self.app)
        for label in ("Prompt", "Commits", "Files", "Visuals",
                      "Resume instructions"):
            self.assertIn(f'"{label}"', self.app)
        self.assertIn('.run-trace-receipt', self.css)
        self.assertIn('.run-trace-flow-steps', self.css)
        self.assertIn('.run-trace-manifest-row', self.css)

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
            'run: () => openWorkResult({ branch: r.orphan_branch })',
            self.app)
        self.assertIn('run: () => openWorkResult({ job_id: r.job_id })', self.app)
        self.assertIn('ensureWorkResults()?.open(ref);', self.app)
        self.assertIn('ref?.branch ? "branch:" + ref.branch', self.results)
        self.assertIn('ref?.job_id ? "job:" + ref.job_id', self.results)
        self.assertIn('(row.aliases || [row.id]).includes(identity)', self.results)
        self.assertIn('/api/work/results/detail?id=', self.results)
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

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

    def test_attention_verbs_reveal_exact_objects(self):
        self.assertIn('run: () => revealOrphan(r.orphan_key)', self.app)
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

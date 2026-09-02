"""Static wiring contract for the World window and temporal renderer."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class WorldGraphUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.atlas = (ROOT / "static" / "atlas.js").read_text(encoding="utf-8")
        cls.renderer = (ROOT / "static" / "atlas3d.js").read_text(
            encoding="utf-8")
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.main = (ROOT / "server" / "main.py").read_text(encoding="utf-8")

    def test_legacy_window_id_now_names_the_world(self):
        self.assertIn('{ id: "atlas", title: "World"', self.app)
        self.assertIn('<div class="section-head"><h2>World</h2>', self.html)
        self.assertNotIn('id="atlas-vault"', self.html)

    def test_two_time_axes_are_visible_and_wired(self):
        for needle in ('id="world-axis-valid"', 'id="world-axis-recorded"',
                       'id="world-time"', 'id="world-time-label"'):
            self.assertIn(needle, self.html)
        self.assertIn('function timeActive(item)', self.atlas)
        self.assertIn('S.time.axis === "recorded"', self.atlas)
        self.assertIn('S.time.timeline[S.time.axis]', self.atlas)
        self.assertIn('const isEdgeShown = (e)', self.atlas)
        self.assertIn('timeZone: "UTC"', self.atlas)
        self.assertIn('host.isEdgeShown ? !host.isEdgeShown(e)', self.renderer)
        self.assertIn('const POINT_CLOUD_AT = 2500', self.renderer)
        self.assertIn('if (S.fixedLayout)', self.renderer)

    def test_world_api_replaces_atlas_only_for_this_window(self):
        self.assertIn('api("/api/world")', self.atlas)
        self.assertIn('api("/api/world/node/"', self.atlas)
        self.assertIn('post("/api/world/refresh"', self.atlas)
        self.assertIn('@app.get("/api/world")', self.main)
        self.assertIn('@app.get("/api/atlas")', self.main)

    def test_people_are_a_kind_not_the_graph_schema(self):
        self.assertIn('kindLabel(d.node.kind)', self.atlas)
        self.assertIn('p.kind === "person"', self.atlas)
        self.assertIn('Receipt · ${receipt.ref}', self.atlas)
        self.assertIn('placeholder="Find anything', self.html)


if __name__ == "__main__":
    unittest.main()

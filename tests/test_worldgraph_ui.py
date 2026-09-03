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
                       'id="world-time"', 'id="world-time-label"',
                       'id="world-time-summary"', '>Content date</button>',
                       '>Learned by</button>'):
            self.assertIn(needle, self.html)
        self.assertIn('function timeActive(item)', self.atlas)
        self.assertIn('S.time.axis === "recorded"', self.atlas)
        self.assertIn('S.time.timeline[S.time.axis]', self.atlas)
        self.assertIn('const isEdgeShown = (e)', self.atlas)
        self.assertIn('timeZone: "UTC"', self.atlas)
        self.assertIn('host.isEdgeShown ? !host.isEdgeShown(e)', self.renderer)
        self.assertIn('const POINT_CLOUD_AT = 2500', self.renderer)
        self.assertIn('semantic_coverage', (ROOT / "server" /
                      "worldlayout.py").read_text(encoding="utf-8"))

    def test_filter_panel_supports_structured_queries_and_kinds(self):
        for needle in ('id="atlas-filter-mode"', 'id="atlas-hide-orphans"',
                       'id="atlas-filter-kinds"',
                       'id="atlas-search-results"'):
            self.assertIn(needle, self.html)
        self.assertIn('function queryTerms(query)', self.atlas)
        self.assertIn('kind|type|tag|source|company|title', self.atlas)
        self.assertIn('function matchesSearch(node)', self.atlas)
        self.assertIn('function renderSearchResults()', self.atlas)

    def test_geometry_and_physics_are_wired_and_node_dragging_is_gone(self):
        for needle in ('id="atlas-geometry"', 'id="atlas-center"',
                       'id="atlas-repel"', 'id="atlas-link-force"',
                       'id="atlas-link-distance"',
                       'id="atlas-semantic"'):
            self.assertIn(needle, self.html)
        self.assertIn('const PHYSICS_GLOBAL_AT = 4000', self.renderer)
        self.assertIn('const PHYSICS_LOCAL_LIMIT = 1400', self.renderer)
        self.assertIn('1,400-node performance ceiling', self.atlas)
        self.assertIn('function refreshPhysics(', self.renderer)
        # a press is always the camera's (owner's call, 2026-09-02): no
        # node drag in either renderer
        self.assertNotIn('function pointOnDragPlane(', self.renderer)
        self.assertNotIn('S.dragNode = p', self.renderer)
        self.assertNotIn('S.dragNode = p', self.atlas)
        self.assertIn('semantic-home force', self.renderer)

    def test_rotation_colors_arcs_and_every_slider_reach_the_renderer(self):
        for needle in ('id="atlas-auto-rotate"', 'id="atlas-curved-links"',
                       'id="atlas-link-curve"', 'id="atlas-reset-colors"'):
            self.assertIn(needle, self.html)
        self.assertIn('picker.type = "color"', self.atlas)
        self.assertIn('colorOverrides: S.colorOverrides', self.atlas)
        self.assertIn('S.display.autoRotate && !host.reducedMotion',
                      self.renderer)
        self.assertIn('function writeEdgePositions(', self.renderer)
        self.assertIn('function linkGeometryChanged()', self.renderer)
        self.assertIn('R3.refreshPhysics(seed, true)', self.atlas)

        display_controls = {
            "#atlas-geometry": "scale",
            "#atlas-node-size": "nodeSize",
            "#atlas-link-thickness": "linkThickness",
        }
        for control, field in display_controls.items():
            self.assertIn(f'bindPercentRange("{control}", S.display, '
                          f'"{field}"', self.atlas)
        force_controls = {
            "#atlas-center": "center",
            "#atlas-repel": "repel",
            "#atlas-link-force": "link",
            "#atlas-link-distance": "distance",
            "#atlas-semantic": "semantic",
        }
        for control, field in force_controls.items():
            self.assertIn(f'bindPercentRange("{control}", S.physics, '
                          f'"{field}"', self.atlas)
            self.assertIn(f'S.physics.{field}', self.renderer)

    def test_article_favorites_playback_and_node_materials_are_wired(self):
        for needle in ('id="atlas-starred-only"',
                       'id="atlas-spherical-nodes"',
                       'id="atlas-node-opacity"', 'id="world-play"',
                       'id="world-speed"'):
            self.assertIn(needle, self.html)
        for needle in ('function navigateArticle(',
                       'function followArticleLink(',
                       'function renderImageRail(',
                       'function renderExternalLinks(',
                       'function toggleTimelinePlayback(',
                       'S.starred.has(d.node.id)',
                       'mdToHtml(d.content, d.content_path)'):
            self.assertIn(needle, self.atlas)
        self.assertIn('centerOn(p);\n      toggleSelect(p);', self.atlas)
        self.assertIn('S.display.nodeOpacity', self.renderer)
        self.assertIn('uniform float uSphere;', self.renderer)
        self.assertIn('S.display.sphericalNodes', self.renderer)

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
        self.assertIn("placeholder='Search, or try kind:person", self.html)


if __name__ == "__main__":
    unittest.main()

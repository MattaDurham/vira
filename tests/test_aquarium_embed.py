"""Exercise aquarium iframe messages and redraw clocks without a browser/GPU."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the aquarium embed harness")
class AquariumEmbedTest(unittest.TestCase):
    def test_parent_protocol_activity_cleanup_and_accepted_render_clock(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_embed.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

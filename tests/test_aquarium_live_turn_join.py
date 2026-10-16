"""Exercise opt-in live-turn and mass-placement joins on the actual koi asset."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the actual koi join")
class AquariumLiveTurnJoinTest(unittest.TestCase):
    def test_actual_placed_material_and_live_navigation(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_live_turn_join.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

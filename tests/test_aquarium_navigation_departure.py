"""Join navigation departures and vertical goals to actual fish motion."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for fish navigation")
class AquariumNavigationDepartureTest(unittest.TestCase):
    def test_departures_vertical_azimuth_and_actual_rig(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_navigation_departure.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

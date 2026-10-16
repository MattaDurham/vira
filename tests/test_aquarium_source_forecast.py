"""Guard the shared head/body forecast and committed source-turn navigation join."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the source forecast join")
class AquariumSourceForecastTest(unittest.TestCase):
    def test_queued_source_forecast_uses_one_clock_and_clearance_authority(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_source_forecast.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

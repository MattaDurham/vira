"""Keep shared median-fin frames identical to the uncached material geometry."""
from pathlib import Path
import shutil
import subprocess
import unittest


class AquariumMedianCacheTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the median cache harness")
    def test_cached_frames_preserve_actual_geometry_across_pose_history(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_median_cache.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

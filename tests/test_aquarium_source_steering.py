"""Validate the source-informed finite authored steering clock and jets."""
import shutil
import subprocess
import unittest
from pathlib import Path


class AquariumSourceSteeringTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for source steering")
    def test_source_heading_and_relative_body_share_finite_committed_clock(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_source_steering.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

"""Validate the actual refined conformation, opaque atlas and material join."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for refined asset quality")
class AquariumRefinedQualityTest(unittest.TestCase):
    def test_exported_skin_silhouette_and_continuous_opaque_pbr_atlas(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_refined_quality.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=45,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

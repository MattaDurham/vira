"""Join fixed-mass placement to actual GLB material points and parent frames."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for fish mass placement")
class AquariumMassPlacementTest(unittest.TestCase):
    def test_actual_centroid_reference_and_parent_transform_mutations(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_mass_placement.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

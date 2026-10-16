"""Check the prescribed source reference against actual geometry and fixed masses."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for reference animation")
class AquariumReferenceTurnTest(unittest.TestCase):
    def test_source_knots_c2_centroid_isolation_and_mutations(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_reference_turn.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""The warm source-entry bridge preserves fin preparation before body loading."""
import shutil
import subprocess
import unittest
from pathlib import Path


class AquariumSourcePreparationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for source steering")
    def test_warm_source_preparation(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_source_preparation.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

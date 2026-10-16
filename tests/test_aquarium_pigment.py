"""Check live pigment masks against the actual authored skin geometry."""
from pathlib import Path
import shutil
import subprocess
import unittest


class AquariumPigmentTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the pigment harness")
    def test_distinct_pigments_preserve_rest_space_and_source_detail(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_pigment.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

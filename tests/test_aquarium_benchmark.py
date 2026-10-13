"""Exercise the shipped aquarium asset and motion together, without a GPU."""
import shutil
import subprocess
import unittest
from pathlib import Path


class AquariumBenchmarkTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the graphics asset harness")
    def test_authored_rig_and_swimming(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run([shutil.which("node"), str(root / "tests" / "aquarium_benchmark.mjs")],
                             cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

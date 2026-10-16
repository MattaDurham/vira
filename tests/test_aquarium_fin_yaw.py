"""Join finite paired-fin motors to the shipped swimmer and actual geometry."""
import shutil
import subprocess
import unittest
from pathlib import Path


class AquariumFinYawTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the fin yaw harness")
    def test_finite_paired_fin_motors_cause_small_course_changes(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_fin_yaw.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=90,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

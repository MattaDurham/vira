"""Exercise the authored finite turn controller without a browser or GPU."""
import shutil
import subprocess
import unittest
from pathlib import Path


class AquariumTurnControlTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the finite turn controller")
    def test_issued_material_load_causes_turn_and_recovery_is_passive(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_turn_control.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

"""Check tight-turn fin coordination against the shipped articulated geometry."""
from pathlib import Path
import shutil
import subprocess
import unittest


class AquariumFinClearanceTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the fin clearance harness")
    def test_managed_paired_fins_clear_the_body_during_tight_turns(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_fin_clearance.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

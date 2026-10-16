"""Exercise multiple live fish against their real exported skeleton and tissue."""
from pathlib import Path
import shutil
import subprocess
import unittest


class AquariumInstancesTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the fish instance harness")
    def test_live_koi_keep_rigs_tissue_and_history_independent(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_fish_instances.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

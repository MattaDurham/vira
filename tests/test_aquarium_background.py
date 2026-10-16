"""Exercise the actual aquarium background adapter without a GPU or stores."""
from pathlib import Path
import shutil
import subprocess
import unittest


class AquariumBackgroundTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the aquarium background harness")
    def test_desktop_bridge_preserves_input_and_motion_lifecycle(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_background.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

"""Exercise scene switching and motion lifecycle without real stores or a GPU."""
import shutil
import subprocess
import unittest
from pathlib import Path


class DesktopBackgroundTests(unittest.TestCase):
    def run_harness(self, name):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "tests/" + name], cwd=root,
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scene_lifecycle(self):
        self.run_harness("desktop_backgrounds_ui.js")

    def test_pond_behavior(self):
        self.run_harness("koi_pond.js")

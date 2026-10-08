"""Exercise scene switching and motion lifecycle without real stores or a GPU."""
import shutil
import subprocess
import unittest
from pathlib import Path


class DesktopBackgroundTests(unittest.TestCase):
    def test_scene_lifecycle(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "tests/desktop_backgrounds_ui.js"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

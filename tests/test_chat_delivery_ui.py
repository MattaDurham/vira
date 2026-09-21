"""Behavior checks for answer delivery while optional panels are loading."""
from pathlib import Path
import shutil
import subprocess
import unittest


class ChatDeliveryUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for UI behavior checks")
    def test_answer_unlocks_reply_while_panels_are_followed(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [shutil.which("node"), "tests/chat_delivery_ui.js"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""The Daily Brief's chat bar (Attention > Today): send, steer, follow,
the one-chat-per-day rule and a failed send, run in Node against the real
functions in static/app.js (tests/brief_chat_ui.js)."""
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.child_timeout import child_timeout


class BriefChatUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for UI behavior checks")
    def test_the_brief_chat_bar_sends_steers_and_follows_one_chat_a_day(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [shutil.which("node"), "tests/brief_chat_ui.js"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", timeout=child_timeout(20),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

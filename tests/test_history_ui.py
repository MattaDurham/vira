"""The Time map and every History view (static/history.js), run under node
in tests/history_ui.js with a fake DOM and a stubbed fetch."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class HistoryUi(unittest.TestCase):
    def test_the_ui_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        result = subprocess.run([node, str(ROOT / "tests" / "history_ui.js")], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=60, check=False)
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        self.assertIn("history UI: ok", result.stdout)

    def test_the_page_loads_history_before_fleet(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="stylesheet" href="/history.css">', html)
        self.assertLess(html.index('src="/history.js"'), html.index('src="/fleet.js"'))


if __name__ == "__main__":
    unittest.main()

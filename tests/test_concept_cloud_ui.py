"""The Concept Cloud keeps its layout across chat polls (tests/concept_cloud_ui.js)."""
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.child_timeout import child_timeout


class ConceptCloudUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for UI behavior checks")
    def test_poll_does_not_reshuffle_the_cloud(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [shutil.which("node"), "tests/concept_cloud_ui.js"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", timeout=child_timeout(20),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

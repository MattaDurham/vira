"""Use the original join oracle on the separately published visual koi."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the refined koi join")
class AquariumRefinedJoinTest(unittest.TestCase):
    def test_actual_refined_attachment_registration(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_refined_attachments.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_refined_fin_body_clearance(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_refined_clearance.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_refined_placed_material_and_live_navigation(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_live_turn_join.mjs"),
             str(root / "static" / "aquarium" / "refined" / "koi.glb"),
             str(root / "static" / "aquarium" / "refined" / "body-mass.json")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""Exercise the articulated mechanics study and its actual uploaded geometry."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for articulated fish mechanics")
class ArticulatedAquariumTest(unittest.TestCase):
    def run_harness(self, name):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / name)],
                                cwd=root, capture_output=True, text=True,
                                encoding="utf-8", timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_articulated_motion_and_command_timing(self):
        self.run_harness("aquarium_articulated_motion.mjs")

    def test_actual_skin_face_and_physical_bones_agree(self):
        self.run_harness("aquarium_articulated_render.mjs")

    def test_neutral_mass_profile_reproduces_from_uploaded_skin(self):
        self.run_harness("aquarium_body_mass.mjs")

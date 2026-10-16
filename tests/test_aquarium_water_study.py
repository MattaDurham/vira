"""Check recorded water data and frozen kicks without a full animal simulation."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for recorded water data")
class AquariumWaterStudyDataTest(unittest.TestCase):
    def test_exact_recorded_data_integrity_units_and_actual_render_join(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_water_study_data.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_independent_frozen_kick_solver_and_physical_mutations(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "aquarium_water_solver_oracle.mjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

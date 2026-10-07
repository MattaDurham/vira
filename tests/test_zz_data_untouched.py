"""The suite's closing check: no test wrote into a checkout's data/.

tests/datadir_guard.py refuses those writes as they happen and records each
one, because product code that tolerates a read-only store swallows the
refusal and the case that caused it still passes. This module reads the
record. Its name sorts after every other test module and discovery runs
modules in name order, so under `python -m unittest discover tests` it runs
last; keep it that way.

Run: .venv/bin/python -m unittest discover tests
"""
import os
import unittest

from tests import datadir_guard as guard

guard.arm()


class CheckoutDataUntouched(unittest.TestCase):
    def test_no_case_wrote_into_data(self):
        self.assertEqual(
            guard.violations, [],
            "tests wrote into a checkout's data/ (refused, then swallowed):\n  "
            + "\n  ".join(guard.violations))
        for root, was_there in guard.existed.items():
            if not was_there:
                self.assertFalse(
                    os.path.exists(root),
                    f"{root} appeared during the run; the in-process guard "
                    "saw no write, so a child process the suite spawned "
                    "made it")

    def test_the_guard_refuses_a_write_and_records_it(self):
        before = len(guard.violations)
        probe = os.path.join(guard.watched[0], "datadir-guard-probe")
        try:
            with self.assertRaises(guard.RealStoreWrite):
                os.mkdir(probe)
        finally:
            if os.path.isdir(probe):   # only reachable with the guard broken
                os.rmdir(probe)
        self.assertFalse(os.path.exists(probe))
        recorded = guard.violations[before:]
        del guard.violations[before:]
        self.assertEqual(len(recorded), 1)
        self.assertIn("datadir-guard-probe", recorded[0])
        self.assertIn("test_the_guard_refuses_a_write_and_records_it",
                      recorded[0])


if __name__ == "__main__":
    unittest.main()

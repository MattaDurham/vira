"""Process liveness probes never terminate the runner being inspected."""
import ctypes
import json
import subprocess
import sys
import unittest
from unittest import mock

from server import jobfiles


class PidAliveTests(unittest.TestCase):
    def windows_probe(self, *, handle=123, result=258, error=0):
        kernel = mock.Mock(spec=["OpenProcess", "WaitForSingleObject", "CloseHandle"])
        kernel.OpenProcess.return_value = handle
        kernel.WaitForSingleObject.return_value = result
        with mock.patch.object(jobfiles.os, "name", "nt"), \
                mock.patch.object(jobfiles.os, "kill", side_effect=AssertionError("signal sent")), \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=error, create=True):
            alive = jobfiles.pid_alive(42)
        kernel.OpenProcess.assert_called_once_with(0x00100000, False, 42)
        if handle:
            kernel.WaitForSingleObject.assert_called_once_with(handle, 0)
            kernel.CloseHandle.assert_called_once_with(handle)
        else:
            kernel.WaitForSingleObject.assert_not_called()
            kernel.CloseHandle.assert_not_called()
        return alive

    def test_windows_live_process_is_queried_without_a_signal(self):
        self.assertTrue(self.windows_probe())

    def test_windows_exited_process_is_dead(self):
        self.assertFalse(self.windows_probe(result=0))

    def test_windows_missing_pid_is_dead(self):
        self.assertFalse(self.windows_probe(handle=None, error=87))

    def test_windows_query_failure_is_not_evidence_of_a_dead_runner(self):
        self.assertTrue(self.windows_probe(handle=None, error=5))
        self.assertTrue(self.windows_probe(result=0xFFFFFFFF))

    def test_invalid_pids_never_reach_a_process_api(self):
        for platform in ("nt", "posix"):
            with mock.patch.object(jobfiles.os, "name", platform), \
                    mock.patch.object(jobfiles.os, "kill") as kill, \
                    mock.patch.object(jobfiles, "_windows_pid_alive") as query:
                for value in (None, "", "bad", 0, -1, float("inf")):
                    self.assertFalse(jobfiles.pid_alive(value))
                if platform == "nt":
                    self.assertFalse(jobfiles.pid_alive(2**32 + 42))
                kill.assert_not_called()
                query.assert_not_called()

    def test_posix_uses_signal_zero_and_distinguishes_missing_and_denied(self):
        with mock.patch.object(jobfiles.os, "name", "posix"), \
                mock.patch.object(jobfiles.os, "kill") as kill:
            self.assertTrue(jobfiles.pid_alive("42"))
            kill.assert_called_once_with(42, 0)
            kill.side_effect = ProcessLookupError()
            self.assertFalse(jobfiles.pid_alive(42))
            kill.side_effect = PermissionError()
            self.assertTrue(jobfiles.pid_alive(42))

    def test_native_self_probe_returns_without_terminating_its_process(self):
        # A subprocess protects the test suite against regression: the old
        # Windows probe exited zero before printing, falsely passing CI.
        script = ("import json, os; from server import jobfiles; "
                  "print(json.dumps({'alive': jobfiles.pid_alive(os.getpid())}))")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"alive": True})


if __name__ == "__main__":
    unittest.main()

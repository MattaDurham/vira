"""System launch boundaries, with all OS actions replaced."""
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from server import settings, systembrowser


class SystemBrowserTests(unittest.TestCase):
    def test_guided_frontend_connects_through_native_browser_and_confirms_result(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        script = Path(__file__).resolve().parent / "microsoft_frontend_harness.js"
        result = subprocess.run([node, str(script)], capture_output=True, text=True,
                                encoding="utf-8", timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_supported_platforms_dispatch_without_shell_or_real_browser(self):
        url = "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
        with mock.patch.object(settings, "IS_MAC", True), mock.patch.object(settings, "IS_WIN", False), mock.patch.object(systembrowser.subprocess, "run") as run:
            systembrowser.open_url(url)
            run.assert_called_once()
        with mock.patch.object(settings, "IS_MAC", False), mock.patch.object(settings, "IS_WIN", True), mock.patch.object(systembrowser.os, "startfile", create=True) as start:
            systembrowser.open_url(url)
            start.assert_called_once_with(url)
        with mock.patch.object(settings, "IS_MAC", False), mock.patch.object(settings, "IS_WIN", False), mock.patch.object(systembrowser.shutil, "which", return_value="/usr/bin/xdg-open"), mock.patch.object(systembrowser.subprocess, "Popen") as spawn:
            systembrowser.open_url(url)
            spawn.assert_called_once()

    def test_arbitrary_external_urls_or_local_paths_cannot_be_opened(self):
        with mock.patch.object(systembrowser.subprocess, "run") as run:
            for url in ["https://example.com/", "file:///tmp/example", "http://localhost/another-path", "https://entra.microsoft.com.evil.example/", "https://entra.microsoft.com@evil.example/", "https://entra.microsoft.com/?secret=example"]:
                with self.assertRaises(ValueError):
                    systembrowser.open_url(url)
            run.assert_not_called()

    def test_os_failure_reports_action_without_exposing_launch_ticket(self):
        url = "http://localhost:8378/api/mail/graph/browser/launch#ticket=private-example"
        with mock.patch.object(settings, "IS_MAC", True), mock.patch.object(systembrowser.subprocess, "run", side_effect=OSError(url)):
            with self.assertRaises(RuntimeError) as raised:
                systembrowser.open_url(url)
        self.assertIn("default browser", str(raised.exception))
        self.assertNotIn("private-example", str(raised.exception))

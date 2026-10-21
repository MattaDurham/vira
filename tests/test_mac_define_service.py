"""Native text encoding joins the actual live-definition route; installs stay isolated."""
import importlib.util
import plistlib
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mac_define_service", ROOT / "scripts" / "install-define-service.py")
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.services = Path(tmp.name) / "Services"

    def test_install_update_and_remove_only_own_service(self):
        target = service.install(self.services)
        info = plistlib.loads((target / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["NSServices"][0]["NSSendTypes"], ["public.utf8-plain-text"])
        self.assertNotIn("NSRequiredContext", info["NSServices"][0])
        service.install(self.services, "http://localhost:8390")
        doc = plistlib.loads((target / "Contents/document.wflow").read_bytes())
        self.assertIn('"http://localhost:8390/"', doc["actions"][0]["action"]["ActionParameters"]["source"])
        self.assertEqual(doc["workflowMetaData"]["serviceInputTypeIdentifier"], "com.apple.Automator.text")
        self.assertEqual(doc["workflowMetaData"]["applicationPaths"], [])
        service.uninstall(self.services)
        self.assertFalse(target.exists())
        service.uninstall(self.services)

    def test_foreign_workflow_is_neither_overwritten_nor_removed(self):
        target = self.services / (service.NAME + ".workflow")
        target.mkdir(parents=True)
        marker = target / "keep.txt"
        marker.write_text("owner workflow", encoding="utf-8")
        for action in [service.install, service.uninstall]:
            with self.assertRaises(ValueError):
                action(self.services)
            self.assertEqual(marker.read_text(encoding="utf-8"), "owner workflow")

    def test_failed_update_restores_previous_installation(self):
        target = service.install(self.services)
        previous = (target / "Contents/document.wflow").read_bytes()
        rename = Path.rename

        def fail_staged(path, destination):
            if path.name == target.name and path != target:
                raise OSError("simulated rename failure")
            return rename(path, destination)

        with mock.patch.object(Path, "rename", fail_staged):
            with self.assertRaises(OSError):
                service.install(self.services, "http://localhost:8390/")
        self.assertEqual((target / "Contents/document.wflow").read_bytes(), previous)

    def test_selection_cannot_be_sent_to_a_remote_or_script_url(self):
        for url in ["https://example.com/", "http://localhost.evil/", "javascript:alert(1)",
                    "http://localhost:0/", "http://localhost:65536/", "http://localhost/#other",
                    "http://localhost/?x=1", 'http://localhost/"', "http://localhost/\n"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                service.install(self.services, url)
        self.assertFalse(self.services.exists())


class DefinitionLinkTests(unittest.TestCase):
    def run_frontend(self, *args):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run(
            [node, "tests/define_link_ui.js", *args], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_links_use_live_module_on_desktop_and_mobile(self):
        self.run_frontend()

    @unittest.skipUnless(sys.platform == "darwin", "AppleScript requires macOS")
    def test_native_selection_reaches_live_lookup_without_changing_text(self):
        # Compile and execute the URL handler shipped inside the workflow.
        # Do not invoke run: it opens a real browser and could touch real stores.
        source = service.workflow_files()["document.wflow"]["actions"][0]["action"]["ActionParameters"]["source"]
        script = "script textService\n" + source + '\nend script\nreturn textService\'s definitionURL({"C++ / naïve & 100%"})'
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script], capture_output=True,
            text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        link = result.stdout.strip()
        self.assertEqual(unquote(urlsplit(link).fragment.removeprefix("define/")), "C++ / naïve & 100%")
        self.run_frontend(link)

    @unittest.skipUnless(sys.platform == "darwin", "Automator requires macOS")
    def test_native_workflow_receives_text_and_emits_the_definition_link(self):
        with TemporaryDirectory() as tmp:
            target = service.install(Path(tmp))
            path = target / "Contents/document.wflow"
            doc = plistlib.loads(path.read_bytes())
            params = doc["actions"][0]["action"]["ActionParameters"]
            # Intercept only the final browser launch. Run the generated
            # bundle in Automator itself, including its real input plumbing.
            params["source"] = params["source"].replace(
                "open location definitionURL(input)\n    return {}",
                "return definitionURL(input)")
            path.write_bytes(plistlib.dumps(doc))
            result = subprocess.run(
                ["/usr/bin/automator", "-i", "C++ / naïve & 100%", str(target)],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("http://localhost:8377/#define/C%2B%2B%20%2F%20na%C3%AFve%20%26%20100%25", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "AppleScript requires macOS")
    def test_native_action_rejects_empty_and_sentence_length_selections(self):
        source = service.workflow_files()["document.wflow"]["actions"][0]["action"]["ActionParameters"]["source"]
        for text in ["", "one two three four five six seven eight nine", "x" * 121]:
            script = "script textService\n" + source + '\nend script\nreturn textService\'s definitionURL({"' + text + '"})'
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script], capture_output=True,
                text=True, encoding="utf-8", timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Select a word or a short phrase", result.stderr)

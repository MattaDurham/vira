"""Exercise the Vira handoff over HTTP with synthetic vaults and no app launch."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import fractallink


class FractalLinkTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.primary, self.secondary = root / "primary", root / "secondary"
        name = "Plan #1 + café.md"
        self.note, self.primary_note = self.secondary / name, self.primary / name
        self.original = self.secondary / "inbox" / "Estimate.pdf"
        self.copy = self.secondary / "project" / "Estimate.pdf"
        for f in (self.note, self.primary_note, self.original, self.copy):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f.name, encoding="utf-8")
        self.scan = {"state": "filed", "vault_id": "second", "path": str(self.original),
                     "filing": {"rel": "inbox/Estimate.pdf", "copies": [{"rel": "project/Estimate.pdf"}]}}
        self.specs = [
            {"root": self.primary, "id": "primary", "primary": True, "read_enabled": True},
            {"root": self.secondary, "id": "second", "primary": False, "read_enabled": True},
        ]
        for patcher in (
            patch.object(fractallink.settings, "IS_MAC", True),
            patch.object(fractallink.vault, "source_specs", return_value=self.specs),
            patch.object(fractallink.scans, "get", return_value=self.scan),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        launcher = patch.object(fractallink.subprocess, "run")
        self.launch = launcher.start()
        self.addCleanup(launcher.stop)
        self.app = FastAPI()
        self.app.include_router(fractallink.router)
        self.origin = "http://localhost:8378"
        self.client = TestClient(self.app, base_url=self.origin, client=("127.0.0.1", 12345))
        self.addCleanup(self.client.close)

    def post(self, kind, ref, **extra):
        return self.client.post("/api/fractal/reveal", headers={"Origin": self.origin},
                                json={"kind": kind, "ref": ref, **extra})

    def target(self):
        url = urlsplit(self.launch.call_args.args[0][-1])
        self.assertEqual((url.scheme, url.hostname), ("fractal-hybrid", "reveal"))
        return parse_qs(url.query)["path"][0]

    def test_exact_vault_and_filename_survive_handoff(self):
        self.assertEqual(self.post("note", "@second/" + self.note.name).status_code, 200)
        self.assertEqual(self.target(), str(self.note))
        self.assertEqual(self.post("note", self.note.name).status_code, 200)
        self.assertEqual(self.target(), str(self.primary_note))

    def test_filed_scan_and_explicit_copy_are_distinct(self):
        self.assertEqual(self.post("scan", "scan1").status_code, 200)
        self.assertEqual(self.target(), str(self.original))
        self.assertEqual(self.post("scan", "scan1", copy="project/Estimate.pdf").status_code, 200)
        self.assertEqual(self.target(), str(self.copy))
        self.assertEqual(self.post("scan", "scan1", copy="inbox/Estimate.pdf").status_code, 404)
        self.assertEqual(self.launch.call_count, 2)

    def test_moved_files_and_unfiled_scans_do_not_launch(self):
        self.note.rename(self.secondary / "Moved.md")
        self.assertEqual(self.post("note", "@second/" + self.note.name).status_code, 404)
        self.copy.unlink()
        self.assertEqual(self.post("scan", "scan1", copy="project/Estimate.pdf").status_code, 404)
        self.scan["state"] = "undone"
        self.assertEqual(self.post("scan", "scan1").status_code, 404)
        self.launch.assert_not_called()

    def test_file_permissions_and_scope(self):
        self.assertEqual(self.post("note", str(self.note)).status_code, 403)
        self.assertEqual(self.post("note", "@second/../primary/" + self.note.name).status_code, 403)
        self.specs[1]["read_enabled"] = False
        self.assertEqual(self.post("note", "@second/" + self.note.name).status_code, 403)
        self.assertEqual(self.post("scan", "scan1").status_code, 403)
        self.launch.assert_not_called()

    def test_remote_or_cross_origin_never_launches(self):
        data = {"kind": "note", "ref": self.note.name}
        for headers in ({}, {"Origin": "https://elsewhere.example"},
                        {"Origin": self.origin, "Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.post("/api/fractal/reveal", json=data, headers=headers).status_code, 403)
        with TestClient(self.app, base_url=self.origin, client=("10.0.0.10", 12345)) as remote:
            self.assertEqual(remote.post("/api/fractal/reveal", json=data, headers={"Origin": self.origin}).status_code, 403)
        with TestClient(self.app, base_url="http://elsewhere.example", client=("127.0.0.1", 12345)) as proxy:
            self.assertEqual(proxy.post("/api/fractal/reveal", json=data, headers={"Origin": "http://elsewhere.example"}).status_code, 403)
        self.launch.assert_not_called()

    def test_missing_fractal_tells_the_owner_what_to_install(self):
        self.launch.side_effect = subprocess.CalledProcessError(1, "open")
        result = self.post("note", self.note.name)
        self.assertEqual(result.status_code, 503)
        self.assertIn("Fractal Hybrid", result.json()["detail"])

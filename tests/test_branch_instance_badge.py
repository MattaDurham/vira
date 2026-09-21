"""Instance identity on /api/config drives the branch badge independently of capabilities."""
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from server import instance, main


class BranchInstanceBadgeTests(unittest.TestCase):
    def test_config_exposes_the_branch_identity(self):
        metadata = {"id": "example", "kind": "branch", "url": "http://localhost:8392",
                    "service_label": "nyc.durham.vira.test.example",
                    "primary_url": "http://localhost:8377"}
        with mock.patch.object(instance, "metadata", return_value=metadata):
            self.assertEqual(main.api_config()["instance"], metadata)

    def test_config_exposes_the_primary_identity(self):
        metadata = {"id": "primary", "kind": "primary", "url": "http://localhost:8377",
                    "service_label": "nyc.durham.vira", "primary_url": "http://localhost:8377"}
        with mock.patch.object(instance, "metadata", return_value=metadata):
            self.assertEqual(main.api_config()["instance"], metadata)


class BranchNavigationTests(unittest.TestCase):
    def test_primary_links_preserve_remote_browser_reachability(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        root = Path(__file__).resolve().parents[1]
        script = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const context = { window: {}, URL, location: {
  origin: "https://example.ts.net:8392", protocol: "https:", hostname: "example.ts.net"
} };
vm.runInNewContext(fs.readFileSync("static/work-results.js", "utf8"), context);
const origin = context.window.ViraWorkResults.primaryOrigin;
assert.equal(origin({kind: "branch", primary_url: "http://localhost:8450"}), "https://example.ts.net:8450");
assert.equal(origin({kind: "branch", primary_url: "https://primary.example:8443"}), "https://primary.example:8443");
assert.equal(origin({kind: "primary"}), "https://example.ts.net:8392");
assert.equal(origin({kind: "branch", primary_url: "invalid"}), "https://example.ts.net:8392");
context.location = {origin: "http://localhost:8392", protocol: "http:", hostname: "localhost"};
assert.equal(origin({kind: "branch", primary_url: "http://localhost:8450"}), "http://localhost:8450");
"""
        result = subprocess.run([node, "-e", script], cwd=root, capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

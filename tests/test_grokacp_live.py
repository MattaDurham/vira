"""Opt-in protocol smoke against the installed, signed-in Grok Build CLI.

Run explicitly with VIRA_LIVE_GROK=1. It spends a little of the signed-in
account's Grok usage. The cwd is a temporary empty directory, the only Vira
tool returns a synthetic constant, the ledger is a mock, and the preamble is
one line: no Vira or owner data enters the model call.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import grokacp, models, settings


class PermissionResultAllow:
    pass


class PermissionResultDeny:
    def __init__(self, message):
        self.message = message


class LiveRunner:
    END = object()

    def __init__(self, cwd):
        self.spec = {"id": "grok-live", "cwd": cwd, "provider": "xai",
                     "prompt": "", "model": "grok-4.7-build-fast",
                     "effort": "low", "mode": "manual", "read_only": False,
                     "worktree": "", "branch": "", "live_root": "",
                     "resume_session": "", "resumed_from": ""}
        self.state = {"session_id": "", "turn": 0}
        self.inbox = asyncio.Queue()
        self.interrupted = False
        self.closing = False
        self.client = None
        self.gates = []
        self.tools = []
        self.out = []

    async def gate(self, name, args, _context):
        self.gates.append((name, args))
        if name == "Edit" and str(args.get("file_path", "")).endswith("denied.txt"):
            return PermissionResultDeny("denied.txt is off limits in this test")
        return PermissionResultAllow()

    async def ask_owner(self, question, options, allow_text=True):
        return "yes"

    def record_tool(self, name, args):
        self.tools.append((name, args))

    def append(self, text):
        self.out.append(text)

    def flush_state(self):
        pass


@unittest.skipUnless(os.environ.get("VIRA_LIVE_GROK") == "1",
                     "opt-in installed Grok Build protocol smoke")
class LiveGrokAcpTest(unittest.TestCase):
    def test_gated_edit_denial_and_bridge_tool(self):
        binary = models.find_binary("xai")
        self.assertTrue(binary, "Grok CLI is not installed")
        probe = {"name": "parity_probe",
                 "description": "Returns the parity code. Call it when asked "
                                "for the parity code.",
                 "parameters": {"type": "object", "properties": {}}}

        async def fake_invoke(name, args, **_kw):
            return {"content": [{"type": "text", "text": "PARITY_PROBE_42"}]}

        async def run(tmp):
            runner = LiveRunner(tmp)
            session = grokacp.GrokSession(runner, binary, settings.strip_env())
            try:
                await session.start()
                last, ok = await session.run_turn(
                    "Do these in order. 1) Create allowed.txt containing yes. "
                    "2) Create denied.txt containing no. 3) Call the "
                    "vira__parity_probe tool. Finish by replying with only "
                    "the parity code it returned.")
            finally:
                await session.close()
            return runner, last, ok

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(grokacp, "joblog"), \
                mock.patch.object(grokacp.viratools, "function_tool_specs",
                                  return_value=[probe]), \
                mock.patch.object(grokacp.viratools, "has_tool",
                                  side_effect=lambda n: n == "parity_probe"), \
                mock.patch.object(grokacp.viratools, "invoke", fake_invoke), \
                mock.patch.object(grokacp.viratools, "preamble",
                                  return_value="You are running a protocol test."):
            tmp = os.path.realpath(tmp)
            runner, last, ok = asyncio.run(run(tmp))
            allowed = Path(tmp, "allowed.txt").exists()
            denied = Path(tmp, "denied.txt").exists()
        transcript = "".join(runner.out)
        self.assertTrue(ok, transcript[-2000:])
        self.assertTrue(allowed, transcript[-2000:])
        self.assertFalse(denied, transcript[-2000:])
        self.assertIn("denial sent back", transcript)
        self.assertIn(("mcp__vira__parity_probe", {}),
                      [(n, a) for n, a in runner.gates])
        self.assertIn("PARITY_PROBE_42", last)
        self.assertTrue(any(n == "Edit" and str(a.get("file_path", "")).endswith("denied.txt")
                            for n, a in runner.gates))


if __name__ == "__main__":
    unittest.main()

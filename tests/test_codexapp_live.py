"""Opt-in protocol smoke against the installed Codex App Server.

Run explicitly with VIRA_LIVE_CODEX=1. The thread is ephemeral, the cwd is a
temporary empty directory, and the only dynamic tool returns a synthetic
constant. No Vira or owner data enters the model call.
"""
import asyncio
import os
import tempfile
import unittest

from server import codexapp, models, settings


@unittest.skipUnless(os.environ.get("VIRA_LIVE_CODEX") == "1",
                     "opt-in installed Codex protocol smoke")
class LiveCodexAppServerTest(unittest.TestCase):
    def test_ephemeral_dynamic_tool_turn(self):
        async def run():
            binary = models.find_binary("openai")
            self.assertTrue(binary, "Codex binary is not installed")
            calls = []

            async def on_request(method, params):
                if method == "item/tool/call" and params.get("tool") == "parity_probe":
                    calls.append(params)
                    return {"success": True, "contentItems": [
                        {"type": "inputText", "text": "PARITY_PROBE_42"}]}
                if method.endswith("requestApproval"):
                    return {"decision": "decline"}
                raise ValueError(f"unexpected server request {method}")

            with tempfile.TemporaryDirectory() as tmp:
                rpc = codexapp.JsonRpcClient(
                    binary, tmp, settings.strip_env(), on_request)
                try:
                    await rpc.start()
                    catalog = await rpc.request("model/list", {"limit": 5})
                    self.assertTrue(catalog.get("data"), catalog)
                    started = await rpc.request("thread/start", {
                        "cwd": tmp,
                        "runtimeWorkspaceRoots": [tmp],
                        "ephemeral": True,
                        "sandbox": "read-only",
                        "approvalPolicy": "never",
                        "approvalsReviewer": "user",
                        "dynamicTools": [{
                            "type": "namespace", "name": "vira",
                            "description": "Synthetic Vira parity probe",
                            "tools": [{
                                "type": "function", "name": "parity_probe",
                                "description": "Return the synthetic parity marker.",
                                "inputSchema": {"type": "object",
                                                "properties": {},
                                                "additionalProperties": False},
                                "deferLoading": False,
                            }],
                        }],
                    })
                    thread_id = started["thread"]["id"]
                    turn = await rpc.request("turn/start", {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": (
                            "Call vira.parity_probe exactly once, then reply "
                            "with only the marker it returns.")}],
                    })
                    turn_id = turn["turn"]["id"]
                    answer = ""
                    status = ""
                    while not status:
                        message = await asyncio.wait_for(
                            rpc.notifications.get(), 45)
                        params = message.get("params") or {}
                        if message.get("method") == "item/completed":
                            item = params.get("item") or {}
                            if item.get("type") == "agentMessage":
                                answer = item.get("text") or answer
                        elif (message.get("method") == "turn/completed"
                              and (params.get("turn") or {}).get("id") == turn_id):
                            status = params["turn"].get("status")
                    self.assertEqual(status, "completed")
                    self.assertEqual(len(calls), 1)
                    self.assertIn("PARITY_PROBE_42", answer)
                finally:
                    await rpc.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

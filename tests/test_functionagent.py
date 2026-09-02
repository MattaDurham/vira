"""Wire-contract tests for Gemini and Grok function sessions."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import functionagent, viratools


class PermissionResultAllow:
    pass


class FakeRunner:
    END = object()

    def __init__(self, provider):
        self.spec = {"id": provider + "-job", "cwd": "/tmp",
                     "prompt": "answer", "provider": provider,
                     "model": provider + "-model", "mode": "manual",
                     "read_only": False, "worktree": "", "branch": "",
                     "live_root": "", "resume_session": "",
                     "resumed_from": ""}
        self.state = {"session_id": "", "turn": 0}
        self.inbox = asyncio.Queue()
        self.session_allow = set()
        self.gates = []
        self.tools = []
        self.messages = []
        self.client = None

    async def gate(self, name, args, _context):
        self.gates.append((name, args))
        return PermissionResultAllow()

    async def ask_owner(self, question, options, allow_text=True):
        return "owner answer"

    def record_tool(self, name, args):
        self.tools.append((name, args))

    def append(self, text):
        self.messages.append(text)

    def flush_state(self):
        pass


class FunctionSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session_dir = mock.patch.object(
            functionagent, "SESSION_DIR", Path(self.tmp.name))
        self.session_dir.start()
        self.addCleanup(self.session_dir.stop)

    def make_session(self, provider):
        runner = FakeRunner(provider)
        session = functionagent.FunctionSession(
            runner, provider, "secret", provider + "-model")
        patches = [
            mock.patch.object(functionagent.joblog, "record_session"),
            mock.patch.object(functionagent.joblog, "record_model_used"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        session.start()
        return runner, session

    def test_gemini_function_call_round_trip(self):
        runner, session = self.make_session("google")
        replies = [
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "calendar",
                                  "args": {"days": 2}}}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [
                {"text": "Calendar answer"}]}}]},
        ]
        with mock.patch.object(functionagent, "_post",
                               side_effect=replies) as post, \
             mock.patch.object(viratools, "invoke", mock.AsyncMock(
                 return_value={"content": [{"type": "text",
                                            "text": "tool result"}]})):
            result = asyncio.run(session.turn("What is next?"))
        self.assertEqual(result, "Calendar answer")
        self.assertEqual(runner.gates[0][0], "mcp__vira__calendar")
        second_body = post.call_args_list[1].args[1]
        response = next(
            part for content in second_body["contents"]
            for part in content.get("parts") or []
            if "functionResponse" in part)
        self.assertEqual(response["functionResponse"]["name"], "calendar")
        saved = json.loads(session.path.read_text(encoding="utf-8"))
        self.assertTrue(saved["contents"])

    def test_grok_responses_function_call_round_trip(self):
        runner, session = self.make_session("xai")
        replies = [
            {"choices": [{"message": {"role": "assistant",
              "content": "", "tool_calls": [{"id": "call-1",
              "type": "function", "function": {"name": "calendar",
              "arguments": "{\"days\": 1}"}}]}}]},
            {"choices": [{"message": {"role": "assistant",
                                        "content": "Grok answer"}}]},
        ]
        with mock.patch.object(functionagent, "_post",
                               side_effect=replies) as post, \
             mock.patch.object(viratools, "invoke", mock.AsyncMock(
                 return_value={"content": [{"type": "text",
                                            "text": "tool result"}]})):
            result = asyncio.run(session.turn("What is next?"))
        self.assertEqual(result, "Grok answer")
        second_body = post.call_args_list[1].args[1]
        tool_message = next(row for row in second_body["messages"]
                            if row.get("role") == "tool")
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertEqual(runner.tools[0][0], "mcp__vira__calendar")

    def test_resume_requires_matching_durable_transport(self):
        runner = FakeRunner("google")
        runner.spec.update(resume_session="old", resumed_from="prior")
        session = functionagent.FunctionSession(
            runner, "google", "secret", "gemini-model")
        with mock.patch.object(functionagent.joblog, "get_record",
                               return_value={"session_transport": "cli-exec"}):
            with self.assertRaises(RuntimeError):
                session.start()


if __name__ == "__main__":
    unittest.main()

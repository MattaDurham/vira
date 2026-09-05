"""Provider-contract tests for the Codex App Server adapter."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import codexapp, viratools
from server import runner as runner_mod


class PermissionResultAllow:
    pass


class PermissionResultDeny:
    pass


class FakeRunner:
    END = object()

    def __init__(self, mode="manual", read_only=False):
        self.spec = {"id": "c" * 12, "cwd": "/tmp/work", "prompt": "go",
                     "provider": "openai", "mode": mode,
                     "read_only": read_only, "worktree": "",
                     "live_root": "", "branch": ""}
        self.state = {"session_id": "", "turn": 0}
        self.session_allow = set()
        self.inbox = asyncio.Queue()
        self.messages = []
        self.tools = []
        self.gates = []
        self.answers = []
        self.client = None
        self.closing = self.interrupted = self.finished_cleanly = False

    async def gate(self, tool, inp, _context):
        self.gates.append((tool, inp))
        if tool == "Denied":
            return PermissionResultDeny()
        return PermissionResultAllow()

    async def ask_owner(self, question, options, allow_text=True):
        self.answers.append((question, options, allow_text))
        return "Owner answer"

    def record_tool(self, name, inp):
        self.tools.append((name, inp))

    def append(self, text):
        self.messages.append(text)

    def flush_state(self):
        pass


class RegistryAdapterTests(unittest.TestCase):
    def test_permission_modes_map_to_codex_policy(self):
        self.assertEqual(codexapp.approval_policy("manual"), "untrusted")
        self.assertEqual(codexapp.approval_policy("acceptEdits"), "on-request")
        self.assertEqual(codexapp.approval_policy("bypassPermissions"), "never")
        # A PLACED bypass session asks on-request: its sandbox is
        # workspace-write, and with `never` a write outside the worktree
        # would fail silently inside Codex - no request, no branch-first
        # denial on the record. The other rungs are unchanged by placement.
        self.assertEqual(codexapp.approval_policy(
            "bypassPermissions", placed=True), "on-request")
        self.assertEqual(codexapp.approval_policy("manual", placed=True),
                         "untrusted")
        self.assertEqual(codexapp.approval_policy("acceptEdits", placed=True),
                         "on-request")

    def test_a_placed_bypass_thread_is_workspace_write_and_asks(self):
        runner = FakeRunner(mode="bypassPermissions")
        runner.spec["worktree"] = "/tmp/work"
        runner.spec["live_root"] = "/tmp/live"
        session = codexapp.CodexSession(runner, "/codex", {})
        params = session._thread_params()
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertEqual(params["approvalPolicy"], "on-request")
        # And the unplaced control keeps the rung's old shape.
        loose = codexapp.CodexSession(
            FakeRunner(mode="bypassPermissions"), "/codex", {})
        loose_params = loose._thread_params()
        self.assertEqual(loose_params["sandbox"], "danger-full-access")
        self.assertEqual(loose_params["approvalPolicy"], "never")

    def test_workspace_write_keeps_the_network(self):
        # workspace-write cuts the network by default, and Vira's own HTTP
        # API on :8377 is the session's whole-store fallback - so the argv
        # opens loopback for every app-server it spawns.
        seen = {}

        async def fake_exec(*argv, **kw):
            seen["argv"] = argv
            raise OSError("stop here")

        async def run():
            rpc = codexapp.JsonRpcClient("/codex", "/tmp", {}, None)
            with mock.patch.object(codexapp.asyncio, "create_subprocess_exec",
                                   fake_exec):
                with self.assertRaises(codexapp.AppServerUnavailable):
                    await rpc.start()
        asyncio.run(run())
        argv = list(seen["argv"])
        self.assertIn("sandbox_workspace_write.network_access=true", argv)
        self.assertIn("mcp_servers={}", argv)
        self.assertEqual(argv[:3], ["/codex", "app-server", "--stdio"])

    def test_a_file_change_is_recorded_as_an_edit(self):
        # The guard probe's write landed with nothing in state.tools: only
        # commandExecution items were recorded, so the ledger could not say
        # the session had edited anything.
        runner = FakeRunner()
        session = codexapp.CodexSession(runner, "/codex", {})
        session.thread_id = "t1"

        async def run():
            await session._handle_notification({
                "method": "item/completed",
                "params": {"threadId": "t1", "item": {
                    "type": "fileChange", "id": "i1",
                    "changes": [{"path": "/tmp/work/README.md"},
                                {"filePath": "/tmp/work/a.py"}]}}})
            await session._handle_notification({
                "method": "item/completed",
                "params": {"threadId": "t1", "item": {
                    "type": "commandExecution", "id": "i2",
                    "command": "ls"}}})
        asyncio.run(run())
        self.assertEqual([t[0] for t in runner.tools], ["Edit", "Bash"])
        self.assertEqual(runner.tools[0][1]["path"],
                         "/tmp/work/README.md, /tmp/work/a.py")
        self.assertIn("Edit: /tmp/work/README.md", "".join(runner.messages))

    def test_dynamic_tool_call_uses_vira_registry_and_gate(self):
        async def run():
            runner = FakeRunner()
            session = codexapp.CodexSession(runner, "/codex", {})
            with mock.patch.object(
                    viratools, "invoke",
                    mock.AsyncMock(return_value={"content": [
                        {"type": "text", "text": "calendar result"}]})):
                out = await session.handle_request("item/tool/call", {
                    "namespace": "vira", "tool": "calendar",
                    "arguments": {"days": 2}})
            return runner, out
        runner, out = asyncio.run(run())
        self.assertTrue(out["success"])
        self.assertEqual(out["contentItems"][0]["text"], "calendar result")
        self.assertEqual(runner.gates[0][0], "mcp__vira__calendar")
        self.assertEqual(runner.tools[0][0], "mcp__vira__calendar")

    def test_command_and_file_approvals_use_the_vira_gate(self):
        async def run():
            runner = FakeRunner()
            session = codexapp.CodexSession(runner, "/codex", {})
            command = await session.handle_request(
                "item/commandExecution/requestApproval",
                {"command": "git status", "cwd": "/tmp/work"})
            file_change = await session.handle_request(
                "item/fileChange/requestApproval", {"grantRoot": "/tmp/work"})
            return runner, command, file_change
        runner, command, file_change = asyncio.run(run())
        self.assertEqual(command, {"decision": "accept"})
        self.assertEqual(file_change, {"decision": "accept"})
        self.assertEqual([row[0] for row in runner.gates], ["Bash", "Edit"])

    def test_user_input_becomes_a_vira_owner_card(self):
        async def run():
            runner = FakeRunner()
            session = codexapp.CodexSession(runner, "/codex", {})
            out = await session.handle_request("item/tool/requestUserInput", {
                "questions": [{"id": "route", "header": "Route",
                               "question": "Which route?", "isOther": False,
                               "options": [{"label": "A", "description": "One"}]}]})
            return runner, out
        runner, out = asyncio.run(run())
        self.assertEqual(out["answers"]["route"], {"answers": ["Owner answer"]})
        self.assertEqual(runner.answers[0][0], "Which route?")

    def test_permission_expansion_uses_vira_gate(self):
        async def run():
            runner = FakeRunner()
            session = codexapp.CodexSession(runner, "/codex", {})
            out = await session.handle_request(
                "item/permissions/requestApproval", {
                    "permissions": {
                        "fileSystem": {"write": ["/tmp/work/generated"]},
                        "network": {"enabled": True}}})
            return runner, out
        runner, out = asyncio.run(run())
        self.assertEqual(out["permissions"]["network"], {"enabled": True})
        self.assertEqual([row[0] for row in runner.gates],
                         ["Edit", "WebSearch"])

    def test_broad_write_permission_is_checked_against_live_root(self):
        checks = codexapp._permission_checks({
            "fileSystem": {"entries": [{
                "access": "write", "path": {"type": "glob_pattern",
                                               "pattern": "**/*"}}]}},
            "/repo/live")
        self.assertEqual(checks[0][0], "Edit")
        self.assertEqual(checks[0][1]["file_path"], "/repo/live")

    def test_legacy_resume_falls_back_instead_of_losing_native_tools(self):
        async def run():
            runner = FakeRunner()
            runner.spec.update(resume_session="old-thread",
                               resumed_from="old-job")
            session = codexapp.CodexSession(runner, "/codex", {})
            session.rpc.start = mock.AsyncMock(return_value={})
            with mock.patch.object(codexapp.joblog, "get_record",
                                   return_value={"session_transport":
                                                 "cli-exec"}):
                await session.start()
        with self.assertRaises(codexapp.AppServerUnavailable):
            asyncio.run(run())

    def test_read_only_thread_omits_write_tools(self):
        runner = FakeRunner(read_only=True)
        session = codexapp.CodexSession(runner, "/codex", {})
        params = session._thread_params()
        params["dynamicTools"] = viratools.dynamic_tool_specs(read_only=True)
        names = {tool["name"] for tool in params["dynamicTools"][0]["tools"]}
        for fqname in viratools.WRITE_TOOLS:
            self.assertNotIn(fqname.removeprefix("mcp__vira__"), names)


class RealRunnerControlPlaneTests(unittest.TestCase):
    """Codex requests must raise the same on-disk cards the UI consumes."""

    def make_runner(self, root):
        spec = {"id": "codexcards01", "prompt": "go", "cwd": "/tmp",
                "provider": "openai", "model": None,
                "model_resolved": None, "permission_mode": None,
                "publish_plan": False, "idea_id": None, "mode": "manual",
                "started": time.time(), "auto_allow": [],
                "permission_timeout": 2, "reply_window": 2,
                "read_only": False, "worktree": "", "live_root": ""}
        jdir = Path(root) / spec["id"]
        jdir.mkdir()
        (jdir / "job.json").write_text(json.dumps(spec), encoding="utf-8")
        return runner_mod.Runner(jdir)

    def test_command_approval_round_trips_through_a_real_card(self):
        async def scenario(runner):
            session = codexapp.CodexSession(runner, "/codex", {})
            task = asyncio.create_task(session.handle_request(
                "item/commandExecution/requestApproval",
                {"command": "git status", "cwd": "/tmp"}))
            await asyncio.sleep(0.02)
            card = runner.state["pending"][0]
            self.assertEqual(card["tool"], "Bash")
            self.assertEqual(runner.state["awaiting"], "permission")
            await runner.handle({"op": "permission", "req_id": card["req_id"],
                                 "allow": True, "scope": "session"})
            return await task

        with tempfile.TemporaryDirectory() as root:
            runner = self.make_runner(root)
            try:
                result = asyncio.run(scenario(runner))
            finally:
                runner.out.close()
        self.assertEqual(result, {"decision": "acceptForSession"})
        self.assertIn("Bash", runner.session_allow)
        self.assertEqual(runner.state["pending"], [])


class RenderingTests(unittest.TestCase):
    def test_v2_command_and_dynamic_tool_shapes_render(self):
        self.assertIn("Bash: git status", codexapp.render_item({
            "type": "commandExecution", "command": "git status", "exitCode": 0}))
        self.assertIn("vira.calendar", codexapp.render_item({
            "type": "dynamicToolCall", "namespace": "vira", "tool": "calendar"}))

    def test_agent_message_is_streamed_not_double_rendered(self):
        self.assertIsNone(codexapp.render_item(
            {"type": "agentMessage", "text": "done"}))


if __name__ == "__main__":
    unittest.main()

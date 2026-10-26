"""The Grok Build lane: xAI sessions and drafts on the owner's Grok
subscription through the `grok` CLI.

Covers the registry row (auth tell, model list, device-code sign-in), the
lane rules (subscription CLI first, API key second, a resume stays on its
lane), the ACP adapter against a scripted fake `grok agent stdio` that
speaks the shapes measured from grok 1.0.70, the MCP bridge that carries
Vira's tools, and the draft path. Nothing here runs the real CLI; the
opt-in live smoke is tests/test_grokacp_live.py.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from server import agentbackend, grokacp, mcpbridge, models, settings, suggest


class PermissionResultAllow:
    pass


class PermissionResultDeny:
    def __init__(self, message):
        self.message = message


def _done(stdout, rc=0):
    return mock.Mock(returncode=rc, stdout=stdout, stderr="")


SIGNED_OUT = ("You are not authenticated.\n\nDefault model: grok-4.6\n\n"
              "Available models:\n  * grok-4.6 (default)\n  - grok-4.5\n")
SIGNED_IN = ("You are logged in with grok.com.\n\nDefault model: grok-4.7\n\n"
             "Available models:\n  * grok-4.7 (default)\n"
             "  - grok-4.7-build-fast\n  - grok-4.6\n")


class GrokRegistryTest(unittest.TestCase):
    def setUp(self):
        models._bin_cache.clear()
        models._cli_catalog_cache.clear()
        models._signed_in_cache.clear()
        self.addCleanup(models._bin_cache.clear)
        self.addCleanup(models._cli_catalog_cache.clear)
        self.addCleanup(models._signed_in_cache.clear)
        mock.patch.object(models, "disabled_providers", return_value=set()).start()
        mock.patch.object(settings, "demo", return_value=False).start()
        self.addCleanup(mock.patch.stopall)

    def test_signed_out_text_is_logged_out_despite_exit_zero(self):
        with mock.patch.object(models.subprocess, "run",
                               return_value=_done(SIGNED_OUT)):
            auth, _detail = models._probe_auth("xai", "/x/grok")
        self.assertEqual(auth, models.LOGGED_OUT)

    def test_signed_in_text_is_signed_in(self):
        with mock.patch.object(models.subprocess, "run",
                               return_value=_done(SIGNED_IN)) as run:
            auth, detail = models._probe_auth("xai", "/x/grok")
        self.assertEqual(auth, models.SIGNED_IN)
        self.assertIn("logged in", detail)
        self.assertEqual(run.call_args.args[0],
                         ["/x/grok", "--no-auto-update", "models"])

    def test_model_list_is_read_from_the_cli(self):
        with mock.patch.object(models, "find_binary", return_value="/x/grok"), \
             mock.patch.object(models.subprocess, "run",
                               return_value=_done(SIGNED_IN + "garbage row\n")):
            got = models._grok_models()
        self.assertEqual([m["id"] for m in got],
                         ["grok-4.7", "grok-4.7-build-fast", "grok-4.6"])
        self.assertIn("CLI default", got[0]["label"])

    def test_signed_in_cli_puts_sessions_on_the_subscription_lane(self):
        with mock.patch.object(models, "find_binary", return_value="/x/grok"), \
             mock.patch.object(models, "api_key", return_value="xai-key"), \
             mock.patch.object(models.subprocess, "run",
                               return_value=_done(SIGNED_IN)):
            r = models.probe("xai")
        self.assertEqual(r["auth"], models.SIGNED_IN)
        self.assertEqual(r["session_backend"], "cli")
        self.assertTrue(r["capabilities"]["workspace_tools"])
        self.assertEqual(r["models"], ["grok-4.7", "grok-4.7-build-fast", "grok-4.6"])

    def test_signed_out_cli_with_a_key_runs_on_the_api(self):
        with mock.patch.object(models, "find_binary", return_value="/x/grok"), \
             mock.patch.object(models, "api_key", return_value="xai-key"), \
             mock.patch.object(models.subprocess, "run",
                               return_value=_done(SIGNED_OUT)):
            r = models.probe("xai")
        self.assertEqual(r["auth"], models.KEY)
        self.assertTrue(r["connected"])
        self.assertEqual(r["session_backend"], "api")
        self.assertFalse(r["capabilities"]["workspace_tools"])
        self.assertIn("login --device-auth", r["login_cmd"])

    def test_nothing_installed_offers_the_installer(self):
        with mock.patch.object(models, "find_binary", return_value=""), \
             mock.patch.object(models, "api_key", return_value=""):
            r = models.probe("xai")
        self.assertFalse(r["connected"])
        self.assertIn("install the grok CLI", r["action"])
        self.assertIn("x.ai/cli/install", r["install_cmd"])

    def test_device_code_sign_in_needs_no_paste(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        out = ["To sign in, open this URL in your browser:\n",
               "  https://accounts.x.ai/oauth2/device?user_code=AB12-CD34\n"]
        with mock.patch.dict(models._login, pid="xai", proc=proc, out=out,
                             started=time.time(), error=""):
            st = models.login_status("xai")
        self.assertTrue(st["running"])
        self.assertFalse(st["paste"])
        self.assertEqual(st["code"], "AB12-CD34")
        self.assertIn("user_code=AB12-CD34", st["url"])

    def test_claude_sign_in_still_asks_for_the_code(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.dict(models._login, pid="anthropic", proc=proc,
                             out=["https://claude.ai/oauth?x=1\n"],
                             started=time.time(), error=""):
            st = models.login_status("anthropic")
        self.assertTrue(st["paste"])

    def test_signed_in_answer_is_reused_briefly(self):
        with mock.patch.object(models, "find_binary", return_value="/x/grok"), \
             mock.patch.object(models, "_probe_auth",
                               return_value=(models.SIGNED_IN, "")) as probe:
            self.assertTrue(models.cli_signed_in("xai"))
            self.assertTrue(models.cli_signed_in("xai"))
        self.assertEqual(probe.call_count, 1)

    def test_no_status_command_means_no_subscription_lane(self):
        with mock.patch.object(models, "find_binary", return_value="/x/gemini"), \
             mock.patch.object(models, "_probe_auth") as probe:
            self.assertFalse(models.cli_signed_in("google"))
        probe.assert_not_called()


class LaneTest(unittest.TestCase):
    def setUp(self):
        mock.patch.object(models, "disabled_providers", return_value=set()).start()
        self.addCleanup(mock.patch.stopall)

    def test_default_lane_prefers_the_subscription(self):
        lane = agentbackend.default_lane
        self.assertEqual(lane("xai", True, True, True), "cli")
        self.assertEqual(lane("xai", False, True, True), "api")
        self.assertEqual(lane("xai", False, True, False), "cli")
        self.assertEqual(lane("xai", False, False, False), "api")
        self.assertEqual(lane("google", True, True, False), "api")
        self.assertEqual(lane("openai", False, False, True), "cli")

    def test_single_lane_providers_never_probe(self):
        with mock.patch.object(models, "cli_signed_in") as probe:
            self.assertEqual(agentbackend.session_backend("anthropic"), "cli")
            self.assertEqual(agentbackend.session_backend("google", "cli"), "api")
        probe.assert_not_called()

    def test_an_explicit_pick_wins_over_the_probe(self):
        with mock.patch.object(models, "cli_signed_in", return_value=True):
            self.assertEqual(agentbackend.session_backend("xai", "api"), "api")
        with mock.patch.object(models, "cli_signed_in", return_value=False), \
             mock.patch.object(models, "find_binary", return_value=""), \
             mock.patch.object(models, "api_key", return_value="k"):
            self.assertEqual(agentbackend.session_backend("xai", "cli"), "cli")
            self.assertEqual(agentbackend.session_backend("xai"), "api")

    def test_a_resume_stays_on_the_lane_its_conversation_lives_in(self):
        from server import joblog
        with mock.patch.object(joblog, "get_record",
                               return_value={"session_transport": "grok-acp"}), \
             mock.patch.object(models, "cli_signed_in", return_value=False):
            self.assertEqual(agentbackend.session_backend("xai", "api", "j1"), "cli")
        with mock.patch.object(joblog, "get_record",
                               return_value={"session_transport": "xai-function-api"}), \
             mock.patch.object(models, "cli_signed_in", return_value=True):
            self.assertEqual(agentbackend.session_backend("xai", None, "j1"), "api")

    def test_effort_vocabulary(self):
        self.assertEqual(agentbackend.validate_effort("xai", "LOW"), "low")
        with self.assertRaises(ValueError):
            agentbackend.validate_effort("xai", "ultra")

    def test_transport_maps_back_to_its_lane(self):
        self.assertEqual(agentbackend.lane_of_transport("xai", "grok-acp"), "cli")
        self.assertEqual(agentbackend.lane_of_transport("xai", "xai-function-api"), "api")
        self.assertIsNone(agentbackend.lane_of_transport("xai", ""))

    def test_default_model_follows_the_lane(self):
        cfg = {"xai_cli_model": "grok-cli-pick", "xai_api_model": "grok-api-pick"}
        with mock.patch("server.suggest.config", return_value=cfg):
            self.assertEqual(agentbackend.default_model("xai", "cli"), "grok-cli-pick")
            self.assertEqual(agentbackend.default_model("xai", "api"), "grok-api-pick")
            self.assertEqual(agentbackend.default_model("xai"), "grok-cli-pick")

    def test_routing_sends_each_lane_to_its_adapter(self):
        runner = FakeRunner("/tmp")
        runner.spec["backend"] = "cli"
        with mock.patch.object(models, "find_binary", return_value="/x/grok"), \
             mock.patch.object(grokacp, "run_session",
                               mock.AsyncMock(return_value=("grok", True))) as grok, \
             mock.patch("server.functionagent.run_session",
                        mock.AsyncMock(return_value=("api", True))) as api:
            self.assertEqual(asyncio.run(agentbackend.run_provider_session(runner)),
                             ("grok", True))
            runner.spec["backend"] = "api"
            self.assertEqual(asyncio.run(agentbackend.run_provider_session(runner)),
                             ("api", True))
        grok.assert_awaited_once()
        api.assert_awaited_once()

    def test_a_missing_cli_stops_by_name(self):
        runner = FakeRunner("/tmp")
        runner.spec["backend"] = "cli"
        with mock.patch.object(models, "find_binary", return_value=""):
            self.assertEqual(asyncio.run(agentbackend.run_provider_session(runner)),
                             ("", False))
        self.assertIn("Grok CLI not found", "".join(runner.out))


class PermissionMappingTest(unittest.TestCase):
    SPEC = {"cwd": "/w/.worktrees/x", "worktree": "/w/.worktrees/x",
            "live_root": "/w"}

    def edit(self, **extra):
        return {"toolCallId": "t", "kind": "edit", "title": "Edit",
                "_meta": {"x.ai/tool": {"name": "search_replace", "kind": "edit"}},
                **extra}

    def test_an_edit_is_gated_on_its_path(self):
        call = self.edit(rawInput={"file_path": "/w/README.md"},
                         locations=[{"path": "/w/README.md"}])
        self.assertEqual(grokacp.permission_checks(call, self.SPEC),
                         [("Edit", {"file_path": "/w/README.md"})])

    def test_an_edit_with_no_path_is_judged_against_the_live_root(self):
        self.assertEqual(grokacp.permission_checks(self.edit(), self.SPEC),
                         [("Edit", {"file_path": "/w"})])
        unplaced = {"cwd": "/tmp/job"}
        self.assertEqual(grokacp.permission_checks(self.edit(), unplaced),
                         [("Edit", {"file_path": "/tmp/job"})])

    def test_a_command_is_gated_as_bash(self):
        call = {"kind": "execute", "rawInput": {"command": "rm -rf build"}}
        self.assertEqual(grokacp.permission_checks(call, self.SPEC),
                         [("Bash", {"command": "rm -rf build",
                                    "cwd": "/w/.worktrees/x"})])

    def test_a_vira_tool_is_gated_inside_the_bridge_not_twice(self):
        call = {"kind": "other", "title": "vira__vault_search",
                "rawInput": {"variant": "UseTool", "tool_name": "vira__vault_search",
                             "tool_input": {"query": "x"}}}
        with mock.patch.object(grokacp.viratools, "has_tool", return_value=True):
            self.assertEqual(grokacp.permission_checks(call, self.SPEC), [])

    def test_a_foreign_mcp_tool_is_gated_by_name(self):
        call = {"kind": "other", "title": "github__create_issue",
                "rawInput": {"variant": "UseTool", "tool_name": "github__create_issue",
                             "tool_input": {"title": "t"}}}
        self.assertEqual(grokacp.permission_checks(call, self.SPEC),
                         [("mcp__github__create_issue", {"title": "t"})])

    def test_allow_never_picks_allow_always(self):
        options = [{"optionId": "allow-edits-session", "kind": "allow_always"},
                   {"optionId": "allow-once", "kind": "allow_once"},
                   {"optionId": "reject-once", "kind": "reject_once"}]
        self.assertEqual(grokacp.pick_option(options, True)["outcome"]["optionId"],
                         "allow-once")
        self.assertEqual(grokacp.pick_option(options, False)["outcome"]["optionId"],
                         "reject-once")
        self.assertEqual(grokacp.pick_option([], True),
                         {"outcome": {"outcome": "cancelled"}})

    def test_render_lines(self):
        edit = self.edit(status="failed", locations=[{"path": "/w/a.py"}],
                         _meta={"x.ai/tool": {"kind": "edit"}, "toolNotRun": "rejected"})
        self.assertEqual(grokacp.render_tool(edit), "  → Edit: /w/a.py (denied)\n")
        bash = {"kind": "execute", "status": "completed",
                "rawInput": {"command": "make"}, "rawOutput": {"exit_code": 2}}
        self.assertEqual(grokacp.render_tool(bash), "  → Bash: make (exit 2)\n")
        mcp = {"status": "completed", "rawInput": {"variant": "UseTool",
                                                   "tool_name": "vira__list_ideas"}}
        self.assertEqual(grokacp.render_tool(mcp), "  → vira.list_ideas\n")
        search = {"_meta": {"x.ai/tool": {"name": "search_tool", "kind": "search_tool"}}}
        self.assertIsNone(grokacp.render_tool(search))


# A scripted `grok agent stdio`: the message shapes are the ones grok 1.0.70
# sent on 2026-10-26 (tests/test_grokacp_live.py checks them for real).
FAKE_AGENT = r'''
import json, sys, urllib.request
MODE, LOG = sys.argv[1], sys.argv[2]
state = {"opts": {"model": "grok-default", "reasoning_effort": "high"}}
def log(kind, obj):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps([kind, obj]) + "\n")
def send(obj):
    obj["jsonrpc"] = "2.0"
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    msg = json.loads(line)
    log("in", msg)
    return msg
nid = [1000]
def ask(method, params):
    nid[0] += 1
    rid = nid[0]
    send({"id": rid, "method": method, "params": params})
    while True:
        msg = read()
        if msg.get("id") == rid and "method" not in msg:
            return msg.get("result") or {}
        if msg.get("method") == "session/cancel":
            state["cancelled"] = True
def update(sid, upd):
    send({"method": "session/update", "params": {"sessionId": sid, "update": upd}})
def options():
    return [{"id": k, "currentValue": v} for k, v in state["opts"].items()]
def tool(sid, tid, kind, name, raw, path=None):
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": tid, "title": name,
                 "rawInput": raw,
                 "_meta": {"x.ai/tool": {"name": name, "kind": kind}}})
    upd = {"sessionUpdate": "tool_call_update", "toolCallId": tid, "kind": kind,
           "title": name, "rawInput": raw}
    if path:
        upd["locations"] = [{"path": path}]
    update(sid, upd)
    perm = {"toolCall": {"toolCallId": tid, "kind": kind, "title": name, "rawInput": raw,
                         "_meta": {"x.ai/tool": {"name": name, "kind": kind}}},
            "options": [{"optionId": "allow-always", "kind": "allow_always"},
                        {"optionId": "allow-once", "kind": "allow_once"},
                        {"optionId": "reject-once", "kind": "reject_once"}],
            "sessionId": sid}
    answer = ask("session/request_permission", perm)
    return answer.get("outcome", {}).get("optionId") in ("allow-once", "allow-always")
def mcp_call(server, name):
    head = {"Content-Type": "application/json"}
    for h in server["headers"]:
        head[h["name"]] = h["value"]
    def post(body):
        req = urllib.request.Request(server["url"], data=json.dumps(body).encode(), headers=head)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")
    post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18"}})
    out = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": name, "arguments": {}}})
    return out["result"]["content"][0]["text"]
def prompt(rid, p):
    sid = p["sessionId"]
    text = p["prompt"][0]["text"]
    if MODE == "cancel":
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "thinking"}})
        while not state.get("cancelled"):
            msg = read()
            if msg.get("method") == "session/cancel":
                break
        send({"id": rid, "result": {"stopReason": "cancelled"}})
        return
    if MODE == "replay":
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "fresh answer"}})
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if not text.startswith("[Vira]"):
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Starting."}})
        if tool(sid, "t1", "edit", "search_replace", {"file_path": "/w/allowed.txt"}, "/w/allowed.txt"):
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
        if not tool(sid, "t2", "edit", "search_replace", {"file_path": "/w/denied.txt"}, "/w/denied.txt"):
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "failed",
                         "_meta": {"toolNotRun": "rejected"}})
            send({"id": rid, "result": {"stopReason": "cancelled"}})
            return
    raw = {"variant": "UseTool", "tool_name": "vira__probe", "tool_input": {}}
    tool(sid, "t3", "use_tool", "use_tool", raw)
    got = mcp_call(state["mcp"], "probe")
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t3", "status": "completed"})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Result: " + got}})
    send({"id": rid, "result": {"stopReason": "end_turn", "_meta": {"usage": {"totalTokens": 5}}}})
while True:
    msg = read()
    method, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        methods = [{"id": "grok.com"}]
        if MODE != "signed_out":
            methods.append({"id": "cached_token"})
        send({"id": rid, "result": {"protocolVersion": 1, "authMethods": methods}})
    elif method == "authenticate":
        send({"id": rid, "result": {"_meta": {"backend_billed": False}}})
    elif method in ("session/new", "session/load"):
        state["mcp"] = p["mcpServers"][0]
        if method == "session/load":
            update(p["sessionId"], {"sessionUpdate": "user_message_chunk",
                                    "content": {"type": "text", "text": "old question"}})
            update(p["sessionId"], {"sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "OLD ANSWER"}})
            send({"id": rid, "result": {"configOptions": options()}})
        else:
            send({"id": rid, "result": {"sessionId": "s-1", "configOptions": options()}})
            send({"id": "skills-reload", "result": {"reloaded": 1}})
    elif method == "session/set_config_option":
        state["opts"][p["configId"]] = p["value"]
        send({"id": rid, "result": {"configOptions": options()}})
    elif method == "session/prompt":
        prompt(rid, p)
'''


class FakeRunner:
    END = object()

    def __init__(self, cwd, **spec):
        self.spec = {"id": "job-1", "cwd": cwd, "prompt": "do the work",
                     "provider": "xai", "model": "grok-pick", "effort": "low",
                     "mode": "manual", "read_only": False, "worktree": "",
                     "branch": "", "live_root": "", "resume_session": "",
                     "resumed_from": "", **spec}
        self.state = {"session_id": "", "turn": 0, "runtime": {}}
        self.inbox = asyncio.Queue()
        self.interrupted = False
        self.closing = False
        self.finished_cleanly = True
        self.client = None
        self.gates = []
        self.tools = []
        self.out = []
        self.turns = []
        self.runtime = None

    async def gate(self, name, args, _context):
        self.gates.append((name, args))
        if str(args.get("file_path", "")).endswith("denied.txt"):
            return PermissionResultDeny("denied.txt is off limits")
        return PermissionResultAllow()

    async def ask_owner(self, question, options, allow_text=True):
        return "owner answer"

    def record_tool(self, name, args):
        self.tools.append((name, args))

    def append(self, text):
        self.out.append(text)

    def flush_state(self):
        pass

    def end_turn(self, status, answer=""):
        self.turns.append((status, answer))

    def set_runtime(self, model=None, effort=None, source="", **extra):
        self.runtime = {"model": model, "effort": effort, **extra}

    def should_park(self, ok):
        return False


class GrokAcpProtocolTest(unittest.TestCase):
    """GrokSession against the scripted agent: gate, denial, bridge, replay."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.agent = root / "fake_grok_agent.py"
        self.agent.write_text(FAKE_AGENT, encoding="utf-8")
        self.log = root / "agent.log"
        self.cwd = str(root)
        self.mode = "work"
        mock.patch.object(grokacp, "argv", side_effect=lambda _b: [
            sys.executable, str(self.agent), self.mode, str(self.log)]).start()
        self.joblog = mock.patch.object(grokacp, "joblog").start()
        probe = {"name": "probe", "description": "Return the probe code.",
                 "parameters": {"type": "object", "properties": {}}}
        mock.patch.object(grokacp.viratools, "function_tool_specs",
                          return_value=[probe]).start()
        mock.patch.object(grokacp.viratools, "has_tool",
                          side_effect=lambda n: n == "probe").start()

        async def invoke(name, args, **_kw):
            return {"content": [{"type": "text", "text": "PROBE_OK"}]}

        mock.patch.object(grokacp.viratools, "invoke", invoke).start()
        mock.patch.object(grokacp.viratools, "preamble",
                          return_value="VIRA PREAMBLE").start()
        self.addCleanup(mock.patch.stopall)

    def received(self, method):
        rows = [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines()]
        return [msg for kind, msg in rows if kind == "in"
                and (msg.get("method") == method or method is None)]

    def answers(self):
        """The client's replies to the agent's permission asks, in order."""
        rows = [json.loads(line)[1] for line in
                self.log.read_text(encoding="utf-8").splitlines()]
        return [m["result"]["outcome"].get("optionId") for m in rows
                if "result" in m and isinstance(m.get("result"), dict)
                and "outcome" in m["result"]]

    def run_turn(self, runner, prompt="do the work"):
        async def go():
            session = grokacp.GrokSession(runner, "/x/grok", dict(os.environ))
            try:
                await session.start()
                return session, await session.run_turn(prompt)
            finally:
                await session.close()
        return asyncio.run(go())

    def test_gated_edit_denial_resume_and_bridge_tool(self):
        runner = FakeRunner(self.cwd)
        session, (last, ok) = self.run_turn(runner)
        self.assertTrue(ok)
        self.assertEqual(last, "Result: PROBE_OK")
        self.assertEqual(runner.gates, [
            ("Edit", {"file_path": "/w/allowed.txt"}),
            ("Edit", {"file_path": "/w/denied.txt"}),
            ("mcp__vira__probe", {})])
        # Once answers only: "allow always" would let Grok stop asking.
        self.assertEqual(self.answers(), ["allow-once", "reject-once", "allow-once"])
        prompts = self.received("session/prompt")
        self.assertEqual(len(prompts), 2)
        self.assertIn("denied.txt is off limits",
                      prompts[1]["params"]["prompt"][0]["text"])
        transcript = "".join(runner.out)
        self.assertIn("  → Edit: /w/allowed.txt\n", transcript)
        self.assertIn("  → Edit: /w/denied.txt (denied)\n", transcript)
        self.assertIn("[vira] denial sent back to Grok", transcript)
        self.assertIn("  → vira.probe\n", transcript)
        self.assertEqual(runner.turns, [("completed", "Result: PROBE_OK")])
        self.assertEqual(session.session_id, "s-1")
        self.joblog.record_session.assert_called_once_with(
            "job-1", "s-1", transport="grok-acp")

    def test_session_setup_carries_rules_bridge_model_and_effort(self):
        runner = FakeRunner(self.cwd)
        self.run_turn(runner)
        new = self.received("session/new")[0]["params"]
        self.assertEqual(new["cwd"], os.path.abspath(self.cwd))
        self.assertEqual(new["_meta"]["rules"], "VIRA PREAMBLE")
        bridge = new["mcpServers"][0]
        self.assertEqual(bridge["type"], "http")
        self.assertTrue(bridge["url"].startswith("http://127.0.0.1:"))
        sets = {m["params"]["configId"]: m["params"]["value"]
                for m in self.received("session/set_config_option")}
        self.assertEqual(sets, {"model": "grok-pick", "reasoning_effort": "low"})
        self.assertEqual(runner.runtime["model"], "grok-pick")
        self.assertEqual(runner.runtime["transport"], "grok-acp")
        auth = self.received("authenticate")[0]["params"]
        self.assertEqual(auth["methodId"], "cached_token")

    def test_signed_out_cli_stops_by_name_before_any_prompt(self):
        self.mode = "signed_out"
        runner = FakeRunner(self.cwd)
        result = asyncio.run(grokacp.run_session(runner, "/x/grok", dict(os.environ)))
        self.assertEqual(result, ("", False))
        self.assertIn("not signed in", "".join(runner.out))
        self.assertEqual(self.received("session/prompt"), [])
        self.assertEqual(self.received("authenticate"), [])

    def test_resume_discards_the_replayed_history(self):
        self.mode = "replay"
        self.joblog.get_record.return_value = {"session_transport": "grok-acp"}
        runner = FakeRunner(self.cwd, resume_session="s-1", resumed_from="job-0")
        _session, (last, ok) = self.run_turn(runner, "next question")
        self.assertTrue(ok)
        self.assertEqual(last, "fresh answer")
        transcript = "".join(runner.out)
        self.assertNotIn("OLD ANSWER", transcript)
        self.assertEqual(self.received("session/load")[0]["params"]["sessionId"], "s-1")

    def test_resume_refuses_a_session_from_another_lane(self):
        self.joblog.get_record.return_value = {"session_transport": "xai-function-api"}
        runner = FakeRunner(self.cwd, resume_session="f-1", resumed_from="job-0")
        with self.assertRaises(grokacp.AcpUnavailable):
            self.run_turn(runner)

    def test_stop_cancels_the_turn_in_flight(self):
        self.mode = "cancel"
        runner = FakeRunner(self.cwd)

        async def go():
            session = grokacp.GrokSession(runner, "/x/grok", dict(os.environ))
            try:
                await session.start()
                turn = asyncio.ensure_future(session.run_turn("long job"))
                for _ in range(200):
                    if "thinking" in "".join(runner.out):
                        break
                    await asyncio.sleep(0.02)
                runner.interrupted = True
                await session.interrupt()
                return await asyncio.wait_for(turn, 10)
            finally:
                await session.close()

        last, ok = asyncio.run(go())
        self.assertFalse(ok)
        self.assertEqual(runner.turns, [("interrupted", "")])
        self.assertEqual(len(self.received("session/cancel")), 1)


class McpBridgeTest(unittest.TestCase):
    def call(self, bridge, body, token=True, method="POST"):
        req = urllib.request.Request(
            bridge.url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {bridge.token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, None

    def with_bridge(self, fn, on_call=None):
        calls = []

        async def default(name, args):
            calls.append((name, args))
            return f"ran {name}", name == "bad"

        async def go():
            bridge = await mcpbridge.McpBridge(
                [{"name": "good", "description": "d", "parameters": {"type": "object"}},
                 {"name": "bad", "description": "d", "parameters": {"type": "object"}}],
                on_call or default).start()
            try:
                return await asyncio.to_thread(fn, bridge)
            finally:
                await bridge.close()
        return asyncio.run(go()), calls

    def test_tools_list_and_call(self):
        def fn(bridge):
            init = self.call(bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-03-26"}})
            listed = self.call(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            good = self.call(bridge, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "good", "arguments": {"q": 1}}})
            bad = self.call(bridge, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "bad"}})
            unknown = self.call(bridge, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                         "params": {"name": "nope"}})
            return init, listed, good, bad, unknown
        (init, listed, good, bad, unknown), calls = self.with_bridge(fn)
        self.assertEqual(init[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual([t["name"] for t in listed[1]["result"]["tools"]], ["good", "bad"])
        self.assertEqual(listed[1]["result"]["tools"][0]["inputSchema"], {"type": "object"})
        self.assertEqual(good[1]["result"], {"content": [{"type": "text", "text": "ran good"}],
                                             "isError": False})
        self.assertTrue(bad[1]["result"]["isError"])
        self.assertIn("unknown tool", unknown[1]["error"]["message"])
        self.assertEqual(calls, [("good", {"q": 1}), ("bad", {})])

    def test_no_token_no_tools(self):
        def fn(bridge):
            return self.call(bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             token=False)
        (status, _body), _calls = self.with_bridge(fn)
        self.assertEqual(status, 401)

    def test_notifications_get_202_and_get_gets_405(self):
        def fn(bridge):
            note = self.call(bridge, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            get = self.call(bridge, None, method="GET")
            return note, get
        (note, get), _calls = self.with_bridge(fn)
        self.assertEqual(note, (202, None))
        self.assertEqual(get[0], 405)

    def test_a_tool_that_raises_is_an_error_result(self):
        async def boom(name, args):
            raise RuntimeError("store unreadable")

        def fn(bridge):
            return self.call(bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "good"}})
        (status, body), _calls = self.with_bridge(fn, on_call=boom)
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("store unreadable", body["result"]["content"][0]["text"])


class GrokDraftTest(unittest.TestCase):
    def setUp(self):
        mock.patch.object(models, "disabled_providers", return_value=set()).start()
        self.addCleanup(mock.patch.stopall)

    def cfg(self, backend="cli"):
        return {"ai_provider": "xai", "ai_backend": backend,
                "xai_cli_model": "", "xai_api_model": "", "timeout": 30}

    def test_signed_in_cli_drafts_on_the_subscription(self):
        with mock.patch.object(models, "api_key", return_value="k"), \
             mock.patch.object(models, "cli_signed_in", return_value=True), \
             mock.patch("server.aihealth.preferred_backend", side_effect=lambda b, k: b):
            self.assertEqual(suggest.effective_backend(self.cfg()), ("xai", "cli"))

    def test_signed_out_cli_with_a_key_drafts_on_the_api(self):
        with mock.patch.object(models, "api_key", return_value="k"), \
             mock.patch.object(models, "cli_signed_in", return_value=False):
            self.assertEqual(suggest.effective_backend(self.cfg()), ("xai", "api"))

    def test_api_without_a_key_falls_to_the_cli(self):
        with mock.patch.object(models, "api_key", return_value=""), \
             mock.patch("server.aihealth.preferred_backend", side_effect=lambda b, k: b):
            self.assertEqual(suggest.effective_backend(self.cfg("api")), ("xai", "cli"))

    def test_the_draft_is_a_toolless_completion_off_argv(self):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"] = cmd
            seen["cwd"] = kw.get("cwd")
            prompt_file = cmd[cmd.index("--prompt-file") + 1]
            seen["prompt"] = Path(prompt_file).read_text(encoding="utf-8")
            seen["env"] = kw.get("env")
            return _done(json.dumps({"text": "drafted", "stopReason": "end_turn"}))

        with mock.patch.object(models, "find_binary", return_value="/x/grok"), \
             mock.patch.object(suggest.subprocess, "run", side_effect=run), \
             mock.patch.dict(os.environ, {"XAI_API_KEY": "xai-live"}):
            out = suggest._call_grok_cli("private thread text", "grok-4.7", 30)
        self.assertEqual(out, "drafted")
        cmd = seen["cmd"]
        self.assertNotIn("private thread text", " ".join(cmd))
        self.assertEqual(seen["prompt"], "private thread text")
        tools = cmd[cmd.index("--disallowed-tools") + 1].split(",")
        for tool in ("run_terminal_cmd", "search_replace", "write", "read_file", "web_fetch"):
            self.assertIn(tool, tools)
        self.assertIn("--disable-web-search", cmd)
        self.assertIn("--no-subagents", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "grok-4.7")
        self.assertEqual(cmd[cmd.index("--cwd") + 1], seen["cwd"])
        self.assertNotIn("XAI_API_KEY", seen["env"])

    def test_a_failed_draft_says_why(self):
        with mock.patch.object(models, "find_binary", return_value="/x/grok"), \
             mock.patch.object(suggest.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="",
                                                      stderr="You are not authenticated.")):
            with self.assertRaises(RuntimeError) as ctx:
                suggest._call_grok_cli("p", "", 30)
        self.assertIn("not authenticated", str(ctx.exception))


class EnvTest(unittest.TestCase):
    def test_children_never_inherit_an_xai_key(self):
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "xai-live",
                                          "VIRA_XAI_KEY": "vira-owned",
                                          "ANTHROPIC_API_KEY": "a"}):
            env = settings.strip_env()
        self.assertNotIn("XAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["VIRA_XAI_KEY"], "vira-owned")


if __name__ == "__main__":
    unittest.main()

"""Subagents in a live session, and the turns the CLI starts by itself.

2026-10-08: three map sessions each launched four background Explore
agents, ended the turn "waiting on the agents", and hung for good. The
runner read the CLI's stream only between its own query and the next
ResultMessage, so once it parked nothing read the agents' messages; the
SDK's buffer filled, its reader blocked, the CLI's permission requests
went unread, and the session froze while the status bar said "complete -
nothing pending". The feed had also blended every agent's lines into the
main agent's.

These tests drive the REAL run_session loop against a scripted CLI stream
(the join, not just the halves): agents launch, the turn ends, the agents
report back, and the CLI starts the next turn with nobody sending a query.
The message shapes are the ones CLI 2.1.283 emitted when measured.
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import joblog, maps, session, viratools
from server import runner as runner_mod

try:
    from claude_agent_sdk import (AssistantMessage, ResultMessage,
                                  SystemMessage, TaskNotificationMessage,
                                  TaskProgressMessage, TaskStartedMessage,
                                  TextBlock, ToolResultBlock, ToolUseBlock,
                                  UserMessage)
    SDK = True
except Exception:  # noqa: BLE001 — the SDK path is what is under test
    SDK = False

from tests.test_runner import make_spec

LANE = runner_mod.AGENT_LANE
MODEL = "test-model"
REAL_CLI_PATH = runner_mod._cli_path


# ---------- scripted CLI messages ----------

def init():
    return SystemMessage(subtype="init",
                         data={"session_id": "sess-1", "model": MODEL})


def say(text, parent=None):
    return AssistantMessage(content=[TextBlock(text=text)], model=MODEL,
                            parent_tool_use_id=parent)


def use(tool_id, name, inp, parent=None):
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name,
                                                  input=inp)],
                            model=MODEL, parent_tool_use_id=parent)


def tool_result(tool_id, text, parent=None):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id,
                                                content=text)],
                       parent_tool_use_id=parent)


def result(text, error=False):
    return ResultMessage(subtype="error" if error else "success",
                         duration_ms=1, duration_api_ms=1, is_error=error,
                         num_turns=1, session_id="sess-1", result=text)


def started(task_id, tool_id, desc, background=True):
    return TaskStartedMessage(
        subtype="task_started",
        data={"is_backgrounded": background, "subagent_type": "Explore"},
        task_id=task_id, description=desc, uuid="u", session_id="sess-1",
        tool_use_id=tool_id, task_type="local_agent")


def progress(task_id, tool_id, calls, desc):
    return TaskProgressMessage(
        subtype="task_progress", data={}, task_id=task_id, description=desc,
        usage={"total_tokens": 100, "tool_uses": calls, "duration_ms": 5},
        uuid="u", session_id="sess-1", tool_use_id=tool_id)


def notified(task_id, tool_id, status="completed"):
    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id, status=status,
        output_file="", summary="", uuid="u", session_id="sess-1",
        tool_use_id=tool_id)


def first_turn(desc="Verify the sources"):
    """The main agent launches one background agent and ends its turn."""
    return [init(),
            use("toolu_A", "Agent", {"description": desc,
                                     "subagent_type": "Explore",
                                     "prompt": "look"}),
            started("task_A", "toolu_A", desc),
            tool_result("toolu_A", "Async agent launched successfully."),
            say("Waiting on the agent."),
            result("Waiting on the agent.")]


class FakeClient:
    """Stands in for ClaudeSDKClient. `script(client)` is an async
    generator: the messages the CLI emits, in order, for the whole session.
    A generator only advances when it is read - exactly the property that
    made the old runner hang - so a script that gets past a turn boundary
    proves the runner kept reading there."""

    def __init__(self, script):
        self.script = script
        self.queries = []
        self.stopped = []
        self._queried = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, text):
        self.queries.append(text)
        self._queried.set()

    async def interrupt(self):
        pass

    async def stop_task(self, task_id):
        self.stopped.append(task_id)

    def receive_messages(self):
        return self.script(self)


async def until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


@unittest.skipUnless(SDK, "claude-agent-sdk not installed")
class AgentsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = Path(self.tmp.name) / "jobs-log.json"
        for p in (mock.patch.object(joblog, "STORE", store),
                  mock.patch.object(runner_mod.viratools, "sdk_server",
                                    lambda **kw: None),
                  mock.patch.object(runner_mod.viratools, "preamble",
                                    lambda **kw: ""),
                  mock.patch.object(runner_mod, "_cli_path", lambda: None),
                  mock.patch.object(runner_mod, "_max_buffer_bytes",
                                    lambda: 1 << 20),
                  mock.patch.object(runner_mod, "SDK_IMPORT_ERROR", None),
                  mock.patch.object(runner_mod.Runner, "acquire_execution",
                                    self._no_admission)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _no_admission(_self):
        return None

    def make_runner(self, **over):
        spec = make_spec(**over)
        jdir = Path(self.tmp.name) / "jobs" / spec["id"]
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / "job.json").write_text(json.dumps(spec), encoding="utf-8")
        r = runner_mod.Runner(jdir)
        self.addCleanup(r.out.close)
        return r

    def drive(self, r, script, *, while_running=None, timeout=10):
        """run_session against the scripted stream. `while_running(r,
        client)` runs alongside it (to finish a parked session, say)."""
        client = FakeClient(script)
        seen = {}

        def options(**kw):
            seen.update(kw)
            return kw

        async def go():
            task = asyncio.ensure_future(r.run_session())
            if while_running:
                await while_running(r, client)
            await asyncio.wait_for(task, timeout)

        with mock.patch.object(runner_mod, "ClaudeAgentOptions", options), \
             mock.patch.object(runner_mod, "ClaudeSDKClient",
                               lambda opts: client):
            asyncio.run(go())
        return client, seen

    def output(self, r):
        return (r.dir / "output.log").read_text(encoding="utf-8")


class AgentsReportBack(AgentsCase):
    def test_a_machine_run_waits_for_its_agents_and_delivers_the_real_answer(self):
        """A routine's deliverable is the turn AFTER the last report. The
        old runner finalized on "Waiting on the agent." and killed the
        agent with the session."""
        r = self.make_runner(meta={"routine_id": "r1"})
        saw = {}

        async def script(client):
            await client._queried.wait()
            for m in first_turn():
                yield m
            await until(lambda: r.state.get("awaiting") == "agents")
            saw["awaiting"] = r.state["awaiting"]
            yield use("toolu_s1", "Bash", {"command": "ls server"},
                      parent="toolu_A")
            yield progress("task_A", "toolu_A", 1, "Running ls")
            yield say("Sources confirmed.\nmail polls every 60s.",
                      parent="toolu_A")
            yield notified("task_A", "toolu_A")
            # the CLI starts the next turn itself: nobody sent a query
            yield init()
            yield say("Saved the map: two sources changed.")
            yield result("Saved the map: two sources changed.")
            await asyncio.Event().wait()

        client, _ = self.drive(r, script)
        self.assertEqual(saw["awaiting"], "agents",
                         "a turn that ended with an agent out must read as "
                         "working, never as complete")
        self.assertEqual(client.queries, ["test"],
                         "the report-back turn needs no query from Vira")
        st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "done")
        self.assertEqual(st["result_text"], "Saved the map: two sources changed.")
        agent = st["agents"][0]
        self.assertEqual((agent["label"], agent["status"], agent["type"]),
                         ("Verify the sources", "completed", "Explore"))
        self.assertEqual(agent["tool_uses"], 1)
        self.assertIn("mail polls every 60s.", agent["report"])
        out = self.output(r)
        self.assertIn(f"{LANE} Verify the sources · → Bash: ls server", out)
        self.assertIn(f"{LANE} Verify the sources · Sources confirmed.", out)
        self.assertIn(f"{LANE} Verify the sources · done - 1 tool call", out)
        self.assertIn("[vira] Verify the sources reported back", out)
        self.assertIn("  → Agent: Verify the sources (Explore)", out)

    def test_the_stream_is_read_while_parked(self):
        """The hang itself: far more messages arrive after the turn ends
        than the SDK buffers (100). Each yield below only returns once the
        runner has read the message, so finishing at all is the proof."""
        r = self.make_runner()

        async def script(client):
            await client._queried.wait()
            for m in first_turn():
                yield m
            for i in range(250):
                yield use(f"toolu_s{i}", "Read", {"file_path": f"f{i}.py"},
                          parent="toolu_A")
            yield say("Report.", parent="toolu_A")
            yield notified("task_A", "toolu_A")
            yield init()
            yield say("All four areas verified; nothing left to do.")
            yield result("All four areas verified; nothing left to do.")
            await asyncio.Event().wait()

        async def finish_when_parked(r, client):
            await until(lambda: r.state.get("awaiting") == "reply", timeout=8)
            r.inbox.put_nowait(runner_mod._END)

        self.drive(r, script, while_running=finish_when_parked)
        st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["agents"][0]["tool_uses"], 250)
        self.assertEqual(st["result_text"],
                         "All four areas verified; nothing left to do.")
        # 250 tool lines, the report, the "done" line
        self.assertEqual(self.output(r).count(LANE + " Verify the sources"), 252)

    def test_agent_lines_never_reach_the_main_answer_stream(self):
        """message_items is the session's answer (chat and the reply
        channel read it back); an agent's report is not."""
        r = self.make_runner()
        r.render_message(use("toolu_A", "Agent", {"description": "Scan"}))
        r.render_message(say("agent words", parent="toolu_A"))
        r.render_message(say("main words"))
        texts = [i["text"] for i in r.state["execution"]["message_items"]]
        self.assertEqual(texts, ["main words"])

    def test_a_long_report_is_capped_in_the_feed_and_kept_whole_on_the_card(self):
        r = self.make_runner()
        r.render_message(use("toolu_A", "Agent", {"description": "Scan"}))
        report = "\n".join(f"line {i}" for i in range(30))
        r.render_message(say(report, parent="toolu_A"))
        out = self.output(r)
        cap = runner_mod.AGENT_TEXT_LINES
        self.assertIn(f"line {cap - 1}", out)
        self.assertNotIn(f"line {cap}\n", out)
        self.assertIn(f"{30 - cap} more lines on the agent's card", out)
        self.assertEqual(r.agents["toolu_A"]["report"], report)

    def test_a_foreground_agent_finishes_when_its_call_returns(self):
        r = self.make_runner()
        r.render_message(use("toolu_F", "Agent", {"description": "Quick look"}))
        r.render_message(tool_result("toolu_F", "Here is what I found."))
        self.assertEqual(r.agents["toolu_F"]["status"], "completed")
        self.assertFalse(r.background_running())

    def test_the_task_snapshot_closes_an_agent_whose_notice_never_came(self):
        r = self.make_runner()
        r.render_message(started("task_A", "toolu_A", "Scan"))
        self.assertTrue(r.background_running())
        r.render_message(SystemMessage(subtype="background_tasks_changed",
                                       data={"tasks": []}))
        self.assertFalse(r.background_running())
        self.assertEqual(r.agents["toolu_A"]["status"], "completed")
        # the snapshot only knows it stopped running; the real ending wins
        r.render_message(notified("task_A", "toolu_A", status="failed"))
        self.assertEqual(r.agents["toolu_A"]["status"], "failed")
        out = self.output(r)
        self.assertIn(f"{LANE} Scan · finished - 0 tool calls", out)
        self.assertIn(f"{LANE} Scan · failed - 0 tool calls", out)

    def test_a_resumed_agent_reopens_and_finishes_again(self):
        """Measured: an agent that left its own shell running finishes,
        is restarted with the same task_id when the shell ends, then
        finishes again. The card follows, and the next report-back turn is
        named after it, not after whichever agent ended before."""
        r = self.make_runner()
        r.render_message(use("toolu_A", "Agent", {"description": "Mail"}))
        r.render_message(use("toolu_B", "Agent", {"description": "Routing"}))
        r.render_message(started("task_A", "toolu_A", "Mail"))
        r.render_message(started("task_B", "toolu_B", "Routing"))
        r.render_message(notified("task_B", "toolu_B"))
        r._turn_open = False
        r.ingest(init())
        self.assertIn("[vira] Routing reported back", self.output(r))
        r.render_message(result("ok"))
        r.render_message(notified("task_A", "toolu_A"))
        r.render_message(started("task_B", "toolu_B", "Routing"))   # resumed
        self.assertEqual(r.agents["toolu_B"]["status"], "running")
        self.assertTrue(r.background_running())
        r.render_message(say("routing ok", parent="toolu_B"))
        r.render_message(notified("task_B", "toolu_B"))
        self.assertEqual(r.agents["toolu_B"]["status"], "completed")
        r.ingest(init())
        out = self.output(r)
        self.assertEqual(out.count(f"{LANE} Routing \u00b7 done"), 2)
        self.assertEqual(out.count("[vira] Routing reported back"), 2)

    def test_a_turn_with_no_new_report_is_not_named_after_an_old_one(self):
        r = self.make_runner()
        r.render_message(use("toolu_A", "Agent", {"description": "Mail"}))
        r.render_message(started("task_A", "toolu_A", "Mail"))
        r.render_message(notified("task_A", "toolu_A"))
        r.ingest(init())
        r.render_message(result("ok"))
        r.ingest(init())
        self.assertIn("[vira] background work reported back", self.output(r))

    def test_a_subagents_own_shell_is_not_background_work(self):
        """Measured: an agent's Bash call is a task too, is_backgrounded
        false. Counting it would hold the session for nothing."""
        r = self.make_runner()
        r.render_message(TaskStartedMessage(
            subtype="task_started",
            data={"is_backgrounded": False, "owned_by_subagent": True},
            task_id="b1", description="ls", uuid="u", session_id="s",
            tool_use_id="toolu_x", task_type="local_bash"))
        self.assertFalse(r.background_running())

    def test_labels_cannot_break_the_lane_format(self):
        r = self.make_runner()
        label = r._agent_label(f"Check · the {LANE} thing\nnow")
        self.assertNotIn(" · ", label)
        self.assertNotIn(LANE, label)
        self.assertNotIn("\n", label)


class SettlingAndStopping(AgentsCase):
    def test_a_lost_report_turn_settles_after_the_grace(self):
        """The agent ends but the CLI never starts a turn: the boundary is
        decided again as an ordinary finished turn, rather than holding
        "working" for the whole reply window."""
        r = self.make_runner(meta={"routine_id": "r1"})

        async def script(client):
            await client._queried.wait()
            for m in first_turn():
                yield m
            yield notified("task_A", "toolu_A")
            await asyncio.Event().wait()

        with mock.patch.object(runner_mod, "SETTLE_GRACE", 0.05):
            self.drive(r, script)
        st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "done")

    def test_stop_while_parked_on_agents_stops_them_and_keeps_the_session(self):
        r = self.make_runner()
        r.client = FakeClient(None)
        r.render_message(started("task_A", "toolu_A", "Scan"))
        r.awaiting_reply = True
        asyncio.run(r.handle({"op": "interrupt"}))
        self.assertEqual(r.client.stopped, ["task_A"])
        self.assertTrue(r.interrupted)
        self.assertTrue(r.inbox.empty(), "Stop over running agents is not Finish")

    def test_a_mid_turn_stop_also_stops_background_agents(self):
        r = self.make_runner()
        r.client = FakeClient(None)
        r.render_message(started("task_A", "toolu_A", "Scan"))
        asyncio.run(r.handle({"op": "interrupt"}))
        self.assertEqual(r.client.stopped, ["task_A"])

    def test_a_failed_turn_does_not_hold_for_agents(self):
        r = self.make_runner()

        async def script(client):
            await client._queried.wait()
            for m in first_turn()[:-1]:
                yield m
            yield result("auth failed", error=True)
            await asyncio.Event().wait()

        self.drive(r, script)
        st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "error")


class SessionOptions(AgentsCase):
    def _seen(self, binary):
        r = self.make_runner()

        async def script(client):
            await client._queried.wait()
            yield init()
            yield result("ok")
            await asyncio.Event().wait()

        # setUp pins _cli_path to None; this class tests the real one
        with mock.patch.object(runner_mod, "_cli_path", REAL_CLI_PATH), \
             mock.patch("server.models.find_binary", lambda pid: binary):
            _, seen = self.drive(r, script, while_running=self._finish)
        return seen

    @staticmethod
    async def _finish(r, client):
        await until(lambda: r.state.get("awaiting") == "reply")
        r.inbox.put_nowait(runner_mod._END)

    def test_sessions_run_the_installed_cli(self):
        self.assertEqual(self._seen("/opt/example/bin/claude")["cli_path"],
                         "/opt/example/bin/claude")

    def test_no_installed_cli_falls_back_to_the_sdk_copy(self):
        self.assertIsNone(self._seen("")["cli_path"])

    def test_the_cli_timer_tools_are_off(self):
        seen = self._seen("")
        for tool in ("ScheduleWakeup", "CronCreate"):
            self.assertIn(tool, seen["disallowed_tools"])



class Instructions(unittest.TestCase):
    def test_claude_sessions_are_told_how_agents_report_back(self):
        p = viratools.preamble(subagents=True)
        self.assertIn("SUBAGENTS", p)
        self.assertIn("reports back by itself", p)
        self.assertIn("unless background agents you launched are still running", p)

    def test_other_engines_are_not_told_about_a_tool_they_lack(self):
        for p in (viratools.preamble(), viratools.preamble(native=False),
                  viratools.preamble(tool_prefix="vira.")):
            self.assertNotIn("SUBAGENTS", p)

    def test_the_agent_launch_line_names_the_agent(self):
        line = session._tool_summary({"name": "Agent", "input": {
            "description": "Verify routing", "subagent_type": "Explore"}})
        self.assertEqual(line, "Agent: Verify routing (Explore)")
        self.assertEqual(session._tool_summary({"name": "Task", "input": {}}),
                         "Agent: delegating a subtask…")


class MapPrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(maps, "STORE", Path(self.tmp.name) / "maps.json")
        p.start()
        self.addCleanup(p.stop)

    def _spec(self, title):
        return {"title": title, "columns": [{"id": "a", "title": "A"}],
                "nodes": [{"id": "n", "column": "a", "name": "N",
                           "what": "A box."}]}

    def test_the_ask_prompt_names_the_slugs_already_taken(self):
        self.assertIn("the owner has no saved maps yet", maps.ask_prompt("x"))
        maps.save("routines", self._spec("Routines"), "map my routines")
        self.assertIn("routines ('Routines')", maps.ask_prompt("x"))

    def test_the_refresh_prompt_says_how_to_close(self):
        maps.save("routines", self._spec("Routines"), "map my routines")
        p = maps.refresh_prompt("routines")
        self.assertIn("say in two or three sentences what changed", p)

    def test_both_prompts_wait_for_every_agent(self):
        # The worktree half moved to the session preamble, which says it for
        # every placed session (test_landing_card.ThePreamble).
        maps.save("routines", self._spec("Routines"), "map my routines")
        for p in (maps.ask_prompt("x"), maps.refresh_prompt("routines")):
            self.assertIn("save only after every one of them has reported", p)


class AgentsUI(unittest.TestCase):
    def test_lanes_and_the_agents_strip(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        root = Path(__file__).resolve().parents[1]
        res = subprocess.run([node, "tests/session_agents_ui.js"], cwd=root,
                             capture_output=True, text=True, encoding="utf-8",
                             timeout=20)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()

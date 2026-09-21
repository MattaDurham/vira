"""Runtime and execution admission regressions; no provider is contacted."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from server import modeladmission as admission, agentbackend, answer_runtime, codexapp, functionagent, modelbudget, modulemodels, runner, suggest


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "queue.sqlite3"
        store = mock.patch.object(answer_runtime, "COMPLETIONS_STORE", Path(self.tmp.name) / "calls.jsonl")
        store.start()
        self.addCleanup(store.stop)

    def lease(self, work_class="foreground", capacity=2):
        lease = admission.Lease("test", work_class, capacity, self.path)
        self.addCleanup(lease.release)
        return lease

    def test_atomic_reservation_cannot_oversubscribe(self):
        leases = [self.lease(capacity=2) for _ in range(12)]
        barrier = threading.Barrier(len(leases))
        def compete(lease):
            barrier.wait()
            return lease.poll()["status"]
        with ThreadPoolExecutor(max_workers=len(leases)) as pool:
            results = list(pool.map(compete, leases))
        self.assertEqual(results.count("running"), 2)
        self.assertEqual(results.count("queued"), 10)

    def test_background_leaves_a_slot_and_owner_passes_queued_auxiliary(self):
        busy = self.lease("background")
        self.assertEqual(busy.poll()["status"], "running")
        aux = self.lease("auxiliary")
        self.assertEqual(aux.poll()["status"], "queued")
        owner = self.lease()
        self.assertEqual(owner.poll()["status"], "running")
        owner.release()
        busy.release()
        self.assertEqual(aux.poll()["status"], "running")

    def test_separate_processes_share_the_same_capacity(self):
        script = ("import sys,json; from server.modeladmission import Lease; "
                  "lease=Lease('child','foreground',2,sys.argv[1]); "
                  "print(json.dumps(lease.poll()),flush=True); "
                  "sys.stdin.readline(); lease.release()")
        processes = []
        try:
            for _ in range(4):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", script, str(self.path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8"))
            states = [json.loads(p.stdout.readline())["status"] for p in processes]
            self.assertEqual(states.count("running"), 2)
            self.assertEqual(states.count("queued"), 2)
        finally:
            for process in processes:
                process.communicate("release\n", timeout=10)

    def test_queue_timeout_releases_ticket_without_evicting_running_work(self):
        busy, waiting = self.lease(capacity=1), self.lease(capacity=1)
        busy.acquire(0)
        with self.assertRaises(admission.QueueTimeout):
            waiting.acquire(0)
        self.assertEqual(busy.poll()["status"], "running")
        self.assertEqual(waiting.poll()["status"], "released")

    def test_nested_completion_borrows_parent_slot(self):
        lease = self.lease(capacity=1)
        lease.acquire(0)
        runtime = {"execution_lease": {"id": lease.id, "path": str(self.path), "pid": admission.os.getpid()}}
        with answer_runtime.scope(runtime), mock.patch.object(suggest, "_run_admitted", return_value=("ok", "cli")) as run:
            self.assertEqual(suggest._run("input", {"timeout": 1}), ("ok", "cli"))
        run.assert_called_once()
        calls = [json.loads(x) for x in answer_runtime.COMPLETIONS_STORE.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([x["event"] for x in calls], ["queued", "started", "completed"])
        self.assertNotIn("input", json.dumps(calls))

    def test_parallel_nested_model_calls_share_one_parent_slot(self):
        lease = self.lease(capacity=1)
        lease.acquire(0)
        runtime = {"execution_lease": {"id": lease.id, "path": str(self.path), "pid": admission.os.getpid()}}
        lock = threading.Lock()
        ready = threading.Barrier(4)
        counters = {"active": 0, "peak": 0}
        def model(*_args):
            with lock:
                counters["active"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
            time.sleep(.03)
            with lock:
                counters["active"] -= 1
            return "ok", "cli"
        def run(_index):
            ready.wait(timeout=10)
            with answer_runtime.scope(runtime):
                return suggest._run("input", {"timeout": 2})
        # Receipt persistence is covered above. Its Windows byte-range lock
        # retries in one-second steps, unrelated to parent-slot serialization.
        with mock.patch.object(suggest, "_run_admitted", side_effect=model), \
                mock.patch.object(answer_runtime, "completion_event"), \
                mock.patch.object(admission, "STORE", self.path):
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(run, range(4))), [("ok", "cli")] * 4)
        self.assertEqual(counters["peak"], 1)
        with admission._connect(self.path) as db:
            tickets = db.execute("SELECT id,status FROM tickets").fetchall()
        self.assertEqual([tuple(row) for row in tickets], [(lease.id, "running")])


class RuntimeBudgetTests(unittest.TestCase):
    def test_module_pick_drives_completion_and_budget_without_a_runtime_pin(self):
        cfg = {"ai_provider": "anthropic", "ai_backend": "cli", "timeout": 10,
               "cli_model": "global-model", "openai_cli_model": "provider-model",
               "module_models": {"find": {"provider": "openai", "backend": "cli", "model": "module-model"}}}
        with modulemodels.scope("find"), answer_runtime.scope({}), \
                mock.patch.object(suggest, "base_config", return_value=cfg), \
                mock.patch.object(suggest, "_run", return_value=("answer", "cli")) as call, \
                mock.patch.object(modelbudget, "_learned", return_value=None), \
                mock.patch("server.models.is_disabled", return_value=False):
            self.assertEqual(suggest.complete("question", tools=[]), "answer")
            self.assertEqual(call.call_args.args[1]["openai_cli_model"], "module-model")
            self.assertEqual(modelbudget.capability()["model"], "module-model")

    def test_pinned_consumer_overrides_later_module_choice_without_transport_fallback(self):
        cfg = {"ai_provider": "anthropic", "ai_backend": "cli", "timeout": 10,
               "cli_model": "later-module-model", "openai_api_model": "other-model",
               "_module_model_explicit": {"provider": "anthropic", "backend": "cli", "model": "later-module-model"}}
        manifest = {"requested": {"provider": "openai", "backend": "api", "model": "pinned-model"},
                    "effective": {"provider": "openai", "backend": "api", "model": None}}
        with answer_runtime.scope(manifest), mock.patch.object(suggest, "config", return_value=cfg), \
                mock.patch.object(suggest, "_run", return_value=("answer", "api")) as call:
            suggest.complete("question", tools=[])
        selected = call.call_args.args[1]
        self.assertEqual(selected["openai_api_model"], "pinned-model")
        self.assertNotIn("_module_model_explicit", selected)
        with mock.patch("server.models.is_disabled", return_value=False), \
                mock.patch("server.models.api_key", return_value=""), \
                mock.patch("server.aihealth.preferred_backend", return_value="cli"):
            self.assertEqual(suggest.effective_backend(selected), ("openai", "api"))

    def test_explicit_empty_runtime_uses_current_module_not_ambient_consumer(self):
        cfg = {"ai_provider": "anthropic", "ai_backend": "cli", "timeout": 10,
               "cli_model": "module-model"}
        manifest = {"effective": {"provider": "openai", "backend": "cli", "model": "ambient-model"}}
        with answer_runtime.scope(manifest), mock.patch.object(suggest, "config", return_value=cfg), \
                mock.patch.object(suggest, "_run", return_value=("answer", "cli")) as call:
            suggest.complete("question", runtime={})
        self.assertEqual(call.call_args.args[1]["cli_model"], "module-model")
        self.assertNotIn("_runtime_model_explicit", call.call_args.args[1])

    def test_provider_specific_config_key_and_runtime_override(self):
        cfg = {"cli_model": "claude-config", "api_model": "claude-api",
               "openai_cli_model": "codex-config", "openai_api_model": "openai-api"}
        with mock.patch.object(modelbudget, "_cfg", return_value=cfg), mock.patch.object(modelbudget, "_learned", return_value=None):
            self.assertEqual(modelbudget.capability("openai", "cli")["model"], "codex-config")
            manifest = {"effective": {"provider": "openai", "backend": "cli", "model": "pinned-consumer"}}
            with answer_runtime.scope(manifest):
                self.assertEqual(modelbudget.capability()["model"], "pinned-consumer")
                self.assertEqual(modelbudget.capability("anthropic", "cli")["model"], "claude-config")
                self.assertEqual(modelbudget.capability("openai", "api")["model"], "openai-api")

    def test_tool_receipt_finishes_on_exception(self):
        observer = mock.Mock()
        observer.start_tool.return_value = "receipt"
        with answer_runtime.scope({}, observer), self.assertRaises(ValueError):
            with answer_runtime.tool_call("source_read", {"id": "abc"}):
                raise ValueError("missing source")
        observer.finish_tool.assert_called_once_with("receipt", None, "missing source")

    def test_scope_copies_policy_and_normalizes_source_list(self):
        seed = {"evidence_scope": ["vault:sample"]}
        manifest = answer_runtime.manifest("openai", "test", seed=seed)
        with answer_runtime.scope(manifest):
            manifest["evidence_scope"]["sources"].append("mail")
            self.assertEqual(answer_runtime.current()["evidence_scope"], {"sources": ["vault:sample"]})
        self.assertEqual(seed, {"evidence_scope": ["vault:sample"]})


class RunnerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name) / "jobs" / "test-job"
        directory.mkdir(parents=True)
        spec = {"id": "test-job", "cwd": self.tmp.name, "mode": "manual", "provider": "openai",
                "prompt": "question", "effort": "medium", "model": "test-model",
                "runtime": answer_runtime.manifest("openai", "test-model", "medium", {"work_class": "foreground"})}
        (directory / "job.json").write_text(json.dumps(spec), encoding="utf-8")
        self.runner = runner.Runner(directory)
        self.addCleanup(self.runner.release_execution)
        self.addCleanup(self.runner.out.close)

    def test_completed_answer_is_durable_before_parking(self):
        async def run():
            await self.runner.begin_turn()
            self.runner.bind_turn("turn-1")
            self.runner.publish_message("i1", "Working", "commentary", completed=True)
            self.runner.end_turn("completed", "Answer")
        asyncio.run(run())
        state = json.loads((self.runner.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["result_text"], "Answer")
        self.assertEqual(state["execution"]["status"], "completed")
        self.assertEqual(state["admission"]["status"], "released")
        self.assertEqual(state["execution"]["message_items"][-1]["phase"], "final_answer")
        events = [json.loads(x) for x in (self.runner.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("turn_completed", [x["event"] for x in events])

    def test_codex_pins_effort_and_filters_old_turn_events(self):
        session = codexapp.CodexSession(self.runner, "/not-executed", {})
        session.thread_id = "thread"
        session.rpc.request = mock.AsyncMock(return_value={"turn": {"id": "native-turn"}})
        session.rpc.notifications = asyncio.Queue()
        session.rpc.notifications.put_nowait({"method": "thread/tokenUsage/updated", "params": {"threadId": "thread", "turnId": "old-turn", "tokenUsage": {"total": 999}}})
        session.rpc.notifications.put_nowait({"method": "thread/tokenUsage/updated", "params": {"threadId": "thread", "turnId": "native-turn", "tokenUsage": {"total": {"totalTokens": 10}}}})
        session.rpc.notifications.put_nowait({"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "native-turn", "status": "completed"}}})
        asyncio.run(session.run_turn("question"))
        sent = session.rpc.request.call_args.args[1]
        self.assertEqual(sent["effort"], "medium")
        self.assertEqual(self.runner.state["execution"]["usage"]["total"]["totalTokens"], 10)

    def test_deadline_requests_interrupt_without_claiming_terminal_state(self):
        self.runner.state["runtime"]["latency_budget"] = {"hard_limit_s": .01}
        self.runner.client = mock.Mock(interrupt=mock.AsyncMock())
        async def run():
            await self.runner.begin_turn()
            await asyncio.sleep(.13)
            self.assertEqual(self.runner.state["execution"]["status"], "stopping")
            self.runner.client.interrupt.assert_awaited_once()
            self.runner.end_turn("interrupted")
        asyncio.run(run())

    def test_owner_wait_releases_capacity_and_does_not_consume_execution_deadline(self):
        self.runner.state["runtime"]["latency_budget"] = {"hard_limit_s": .08}
        self.runner.client = mock.Mock(interrupt=mock.AsyncMock())
        async def run():
            await self.runner.begin_turn()
            task = asyncio.create_task(self.runner.ask_owner("Choose a scope", []))
            await asyncio.sleep(.12)
            self.assertFalse(self.runner.interrupted)
            self.assertIsNone(self.runner._lease)
            self.assertEqual(self.runner.state["execution"]["status"], "awaiting_input")
            future = next(iter(self.runner.futures.values()))
            future.set_result("Sample")
            self.assertEqual(await task, "Sample")
            self.assertTrue(self.runner._lease.active)
            self.assertEqual(self.runner.state["execution"]["status"], "running")
            self.runner.end_turn("completed", "Sample answer")
        asyncio.run(run())

    def test_tool_receipts_record_partial_evidence_and_json_errors(self):
        ident = self.runner.start_tool("source_read", {"source": "vault:sample"})
        evidence = {"evidence_handle": "evidence:sample", "version": "v1", "span": {"start": 0, "end": 8},
                    "complete": False, "cache": "hit", "text": "private source body"}
        self.runner.finish_tool(ident, {"content": [{"type": "text", "text": json.dumps(evidence)}]})
        receipt = self.runner.state["receipts"][-1]
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["metadata"]["evidence_handle"], "evidence:sample")
        self.assertNotIn("private source body", json.dumps(receipt))
        ident = self.runner.start_tool("source_read", {})
        self.runner.finish_tool(ident, {"content": [{"type": "text", "text": '{"error":"missing"}'}]})
        self.assertEqual(self.runner.state["receipts"][-1]["status"], "failed")

    def test_native_command_receipt_and_commentary_boundary(self):
        session = codexapp.CodexSession(self.runner, "/not-executed", {})
        session.thread_id = "thread"
        async def run():
            for method, item in [
                ("item/started", {"id": "cmd", "type": "commandExecution", "command": "sample", "status": "inProgress"}),
                ("item/completed", {"id": "cmd", "type": "commandExecution", "command": "sample", "status": "failed", "exitCode": 1, "durationMs": 42}),
                ("item/completed", {"id": "commentary", "type": "agentMessage", "phase": "commentary", "text": "Reading"}),
            ]:
                await session._handle_notification({"method": method, "params": {"threadId": "thread", "item": item}})
        asyncio.run(run())
        self.assertEqual(session.last_message, "")
        self.assertEqual(self.runner.state["receipts"][-1]["status"], "failed")
        self.assertEqual(self.runner.state["receipts"][-1]["metadata"]["durationMs"], 42)

    def test_codex_stop_accepts_the_followup_in_the_same_session(self):
        prompts = []
        async def turn(prompt):
            prompts.append(prompt)
            await self.runner.begin_turn()
            if len(prompts) == 1:
                self.runner.interrupted = True
                self.runner.inbox.put_nowait("Continue with the narrower scope")
                self.runner.end_turn("interrupted")
                return "", False
            self.assertFalse(self.runner.interrupted)
            self.runner.end_turn("completed", "Answer")
            self.runner.closing = True
            return "Answer", True
        transport = mock.Mock(start=mock.AsyncMock(), run_turn=turn, close=mock.AsyncMock())
        with mock.patch.object(codexapp, "CodexSession", return_value=transport):
            result = asyncio.run(codexapp.run_session(self.runner, "/not-executed", {}))
        self.assertEqual(result, ("Answer", True))
        self.assertEqual(prompts, ["question", "Continue with the narrower scope"])

    def test_api_stop_accepts_the_followup_after_inflight_request_returns(self):
        prompts = []
        async def turn(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                self.runner.interrupted = True
                self.runner.inbox.put_nowait("Continue")
                raise RuntimeError("model turn interrupted")
            self.assertFalse(self.runner.interrupted)
            self.runner.closing = True
            return "Answer"
        transport = mock.Mock(start=mock.Mock(), turn=turn)
        with mock.patch.object(functionagent, "FunctionSession", return_value=transport), mock.patch.object(functionagent.models, "api_key", return_value="test-key"):
            result = asyncio.run(functionagent.run_session(self.runner, "xai"))
        self.assertEqual(result, ("Answer", True))
        self.assertEqual(prompts, ["question", "Continue"])

    def test_compatibility_stop_accepts_the_followup_in_the_same_thread(self):
        prompts = []
        async def turn(_runner, _binary, prompt, thread):
            prompts.append((prompt, thread))
            await self.runner.begin_turn()
            if len(prompts) == 1:
                self.runner.interrupted = True
                self.runner.inbox.put_nowait("Continue")
                self.runner.end_turn("interrupted")
                return "native-thread", "", False
            self.assertFalse(self.runner.interrupted)
            self.runner.end_turn("completed", "Answer")
            self.runner.closing = True
            return "native-thread", "Answer", True
        with mock.patch.object(agentbackend, "_run_turn", side_effect=turn), mock.patch.object(agentbackend.models, "find_binary", return_value="/not-executed"), mock.patch.object(agentbackend.viratools, "preamble", return_value="Instructions"):
            result = asyncio.run(agentbackend.run_cliexec(self.runner))
        self.assertEqual(result, ("Answer", True))
        self.assertEqual(prompts[-1], ("Continue", "native-thread"))

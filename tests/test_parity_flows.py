"""The Forge's parity machinery: provider pins, stage timeouts, continued
conversations, the record-reading logic gates, and the three parity starters.

Everything roots at ONE tmp fixture (circuit defs, runs, job dirs, the
ledger, the flow store) and every session is a stub that writes the same
files a runner would - state.json, output.log, a ledger row - so a gate is
tested against the RECORD it reads on live, never against a mocked verdict.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import (agentbackend, circuits, flows, jobfiles, joblog, routines,
                    runner)

ROOT = Path(__file__).resolve().parent.parent


class StubSessions:
    """A session registry that finishes instantly. `script(rec)` returns a
    dict: {"out": text, "state": {...extra state.json}, "record": {...ledger
    stamps}, "log": transcript, "hold": True keeps it running until
    interrupt()}."""

    def __init__(self, jobs_root, script):
        self.root = Path(jobs_root)
        self.script = script
        self.launched = []
        self.interrupted = []
        self.n = 0

    def launch(self, prompt, cwd=None, permission_mode=None, model=None,
               publish_plan=False, idea_id=None, mode=None, read_only=False,
               meta=None, provider=None, resume_session=None,
               resumed_from=None, worktree=None, **name_inputs):
        jid = f"stub{self.n:04d}"
        self.n += 1
        rec = {"id": jid, "prompt": prompt, "cwd": cwd, "model": model,
               "mode": mode, "read_only": read_only, "meta": meta or {},
               "provider": provider, "resume_session": resume_session,
               "resumed_from": resumed_from,
               "subject": name_inputs.get("subject") or ""}
        self.launched.append(rec)
        spec = self.script(rec) or {}
        if isinstance(spec, str):
            spec = {"out": spec}
        joblog.record_launch({
            "id": jid, "prompt": prompt, "cwd": cwd or "", "model": model,
            "provider": provider or "anthropic", "mode": mode,
            "read_only": read_only, "meta": meta or {},
            "worktree": (spec.get("record") or {}).get("worktree") or "",
            "live_root": (spec.get("record") or {}).get("live_root") or "",
        })
        stamps = spec.get("record") or {}
        if stamps.get("session_id"):
            joblog.record_session(jid, stamps["session_id"],
                                  transport=stamps.get("transport") or "")
        if stamps.get("model_used"):
            joblog.record_model_used(jid, stamps["model_used"])
        jdir = self.root / jid
        jdir.mkdir(parents=True, exist_ok=True)
        state = {"id": jid, "status": "running" if spec.get("hold") else "done",
                 "result_text": spec.get("out", ""),
                 "session_id": stamps.get("session_id") or "",
                 "provider": provider or "anthropic"}
        state.update(spec.get("state") or {})
        (jdir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (jdir / "output.log").write_text(spec.get("log", ""), encoding="utf-8")
        if not spec.get("hold"):
            joblog.record_finish(jid, "done", spec.get("out", ""))
        return jid

    def get(self, jid):
        p = self.root / jid / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def interrupt(self, jid):
        self.interrupted.append(jid)
        p = self.root / jid / "state.json"
        st = json.loads(p.read_text(encoding="utf-8"))
        st.update({"status": "done", "interrupted": True, "aborted": True})
        p.write_text(json.dumps(st), encoding="utf-8")
        joblog.record_finish(jid, "done", st.get("result_text", ""))

    def close(self, jid):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        for p in [mock.patch.object(circuits, "DEFS", root / "circuits.json"),
                  mock.patch.object(circuits, "RUNS", root / "circuit-runs.json"),
                  mock.patch.object(jobfiles, "JOBS_DIR", root / "jobs"),
                  mock.patch.object(joblog, "STORE", root / "jobs-log.json"),
                  mock.patch.object(flows, "STORE", root / "flow-graphs.json"),
                  mock.patch.object(flows, "LIBRARY", root / "library"),
                  mock.patch.object(routines, "STORE", root / "routines.json")]:
            p.start()
            self.addCleanup(p.stop)
        self.jobs_root = root / "jobs"

    def stub(self, script):
        stub = StubSessions(self.jobs_root, script)
        p = mock.patch("server.session.sessions", stub)
        p.start()
        self.addCleanup(p.stop)
        return stub

    def circuit(self, stages, cid="t"):
        circuits.save_circuit({"id": cid, "name": "T", "stages": stages})
        return cid

    def drive(self, run_id, ticks=12):
        d = circuits.Driver.__new__(circuits.Driver)
        for _ in range(ticks):
            run = circuits.get_run(run_id)
            if run["status"] != "running":
                break
            d._advance(run)
        return circuits.get_run(run_id)


AGENT = {"id": "a", "name": "A", "mode": "manual", "read_only": True,
         "needs": [], "prompt": "do {{input}}"}


class Knobs(Base):
    def test_a_stage_may_pin_a_known_provider_and_refuses_an_unknown_one(self):
        circuits.validate_stages([{**AGENT, "provider": "openai"}])
        with self.assertRaises(ValueError):
            circuits.validate_stages([{**AGENT, "provider": "bard"}])

    def test_timeout_and_on_timeout_are_bounded(self):
        circuits.validate_stages([{**AGENT, "timeout_s": 90,
                                   "on_timeout": "continue"}])
        for bad in ({"timeout_s": -1}, {"timeout_s": "soon"},
                    {"timeout_s": circuits.TIMEOUT_MAX_S + 1},
                    {"on_timeout": "shrug"}):
            with self.assertRaises(ValueError, msg=bad):
                circuits.validate_stages([{**AGENT, **bad}])

    def test_a_continuation_must_follow_the_agent_stage_it_continues(self):
        b = {"id": "b", "mode": "manual", "read_only": True, "needs": ["a"],
             "continues": "a", "prompt": "and?"}
        circuits.validate_stages([AGENT, b])
        with self.assertRaises(ValueError):          # not a need
            circuits.validate_stages([AGENT, {**b, "needs": []}])
        gate = {"id": "g", "mode": "logic", "needs": ["a"],
                "logic": {"operation": "always"}}
        with self.assertRaises(ValueError):          # a gate has no conversation
            circuits.validate_stages([AGENT, gate,
                                      {**b, "needs": ["g"], "continues": "g"}])
        with self.assertRaises(ValueError):          # a judge cannot continue
            circuits.validate_stages([AGENT, {"id": "j", "mode": "judge",
                                              "needs": ["a"], "continues": "a",
                                              "judge": {"of": ["a"]}}])

    def test_the_tray_can_retune_the_knobs_but_not_a_judges_timeout(self):
        stages = [dict(AGENT), {"id": "j", "mode": "judge", "needs": ["a"],
                                "judge": {"of": ["a"]}}]
        circuits.apply_overrides(stages, {"a": {"provider": "google",
                                                "timeout_s": 30,
                                                "on_timeout": "continue"}})
        self.assertEqual(stages[0]["provider"], "google")
        self.assertEqual(stages[0]["timeout_s"], 30)
        with self.assertRaises(ValueError):
            circuits.apply_overrides(stages, {"a": {"provider": "nope"}})
        with self.assertRaises(ValueError):
            circuits.apply_overrides(stages, {"j": {"timeout_s": 5}})

    def test_render_prompt_carries_cwd_and_provider(self):
        run = {"input": "x", "cwd": "/tmp/repo", "provider": "xai",
               "stages": {}}
        out = circuits.render_prompt("at {{cwd}} on {{provider}}", run)
        self.assertIn("at /tmp/repo on xai", out)
        out = circuits.render_prompt("{{provider}}", run, {"provider": "google"})
        self.assertTrue(out.startswith("google"))


VERDICT = '{"grade": "B+", "score": 80, "summary": "ok", "findings": []}'


class ProviderRouting(Base):
    def test_stage_provider_reaches_the_launch(self):
        stub = self.stub(lambda r: "done")
        cid = self.circuit([{**AGENT, "provider": "openai"}])
        run = circuits.start_run(cid, "task")
        self.drive(run["id"])
        self.assertEqual(stub.launched[0]["provider"], "openai")

    def test_run_provider_covers_agents_but_never_judges(self):
        def script(rec):
            return VERDICT if rec["mode"] == "judge" else "built"
        stub = self.stub(script)
        cid = self.circuit([dict(AGENT),
                            {"id": "j", "mode": "judge", "needs": ["a"],
                             "judge": {"of": ["a"], "min_grade": ""}}])
        run = circuits.start_run(cid, "task", provider="google")
        self.assertEqual(run["provider"], "google")
        self.drive(run["id"])
        by_stage = {r["meta"]["stage"]: r for r in stub.launched}
        self.assertEqual(by_stage["a"]["provider"], "google")
        self.assertIsNone(by_stage["j"]["provider"])

    def test_a_stages_own_provider_outranks_the_runs(self):
        stub = self.stub(lambda r: "done")
        cid = self.circuit([{**AGENT, "provider": "xai"}])
        run = circuits.start_run(cid, "task", provider="google")
        self.drive(run["id"])
        self.assertEqual(stub.launched[0]["provider"], "xai")

    def test_start_run_refuses_an_unknown_provider(self):
        cid = self.circuit([dict(AGENT)])
        with self.assertRaises(ValueError):
            circuits.start_run(cid, "task", provider="bard")

    def test_flow_run_passes_the_provider_through(self):
        with mock.patch.object(circuits, "start_run",
                               return_value={"id": "r"}) as sr:
            flows.run_flow("cid", "x", provider="openai")
        self.assertEqual(sr.call_args.kwargs.get("provider"), "openai")


class Continuation(Base):
    def test_a_continuing_stage_is_the_prior_sessions_next_turn(self):
        def script(rec):
            if rec["meta"]["stage"] == "a":
                return {"out": "noted", "record": {"session_id": "sess-A",
                                                   "transport": "codex-app-server"}}
            return {"out": "MARIGOLD-7"}
        stub = self.stub(script)
        cid = self.circuit([{**AGENT, "provider": "openai"},
                            {"id": "b", "mode": "manual", "read_only": True,
                             "needs": ["a"], "continues": "a",
                             "prompt": "the codeword?"}])
        run = self.drive(circuits.start_run(cid, "task")["id"])
        self.assertEqual(run["status"], "done")
        b = next(r for r in stub.launched if r["meta"]["stage"] == "b")
        self.assertEqual(b["resume_session"], "sess-A")
        self.assertEqual(b["resumed_from"], "stub0000")
        self.assertEqual(b["provider"], "openai")   # a conversation stays put

    def test_a_prior_with_no_recorded_session_fails_by_name(self):
        self.stub(lambda r: "no session recorded")
        cid = self.circuit([dict(AGENT),
                            {"id": "b", "mode": "manual", "read_only": True,
                             "needs": ["a"], "continues": "a",
                             "prompt": "and?"}])
        run = self.drive(circuits.start_run(cid, "task")["id"])
        self.assertEqual(run["status"], "error")
        self.assertIn("no conversation to continue", run["error"])


class Timeouts(Base):
    def _backdate(self, run_id, sid, seconds):
        circuits.Driver._apply(
            circuits.Driver.__new__(circuits.Driver), run_id,
            {sid: {"started": "2000-01-01T00:00:00+00:00"}})

    def test_a_stage_past_its_budget_is_interrupted_once(self):
        stub = self.stub(lambda r: {"hold": True, "out": "partial"})
        cid = self.circuit([{**AGENT, "timeout_s": 5}])
        run = circuits.start_run(cid, "task")
        self.drive(run["id"], ticks=1)               # launched, running
        self.assertEqual(stub.interrupted, [])
        self._backdate(run["id"], "a", 60)
        run = self.drive(run["id"], ticks=1)         # past budget -> interrupt
        self.assertEqual(stub.interrupted, ["stub0000"])
        self.assertTrue(run["stages"]["a"]["timed_out"])
        run = self.drive(run["id"], ticks=3)
        self.assertEqual(run["stages"]["a"]["status"], "error")
        self.assertEqual(run["status"], "error")
        self.assertEqual(stub.interrupted, ["stub0000"])  # never twice

    def test_on_timeout_continue_keeps_what_the_stage_produced(self):
        stub = self.stub(lambda r: {"hold": True, "out": "1\n2\n3"})
        cid = self.circuit([{**AGENT, "timeout_s": 5, "on_timeout": "continue"},
                            {"id": "g", "mode": "logic", "needs": ["a"],
                             "logic": {"operation": "interrupt_honored"}}])
        run = circuits.start_run(cid, "task")
        self.drive(run["id"], ticks=1)
        self._backdate(run["id"], "a", 60)
        run = self.drive(run["id"], ticks=4)
        self.assertEqual(run["stages"]["a"]["status"], "done")
        self.assertEqual(run["stages"]["g"]["status"], "done",
                         run["stages"]["g"]["result_text"])
        self.assertEqual(run["status"], "done")

    def test_a_stage_with_no_budget_is_never_interrupted(self):
        stub = self.stub(lambda r: {"hold": True})
        cid = self.circuit([dict(AGENT)])
        run = circuits.start_run(cid, "task")
        self.drive(run["id"], ticks=1)
        self._backdate(run["id"], "a", 10**6)
        self.drive(run["id"], ticks=2)
        self.assertEqual(stub.interrupted, [])


class Gates(Base):
    """logic_passes against the files a real runner leaves behind."""

    def probe_run(self, provider="anthropic", record=None, state=None, log="",
                  stage=None):
        self._n = getattr(self, "_n", 0) + 1
        jid = f"job{self._n}"
        joblog.record_launch({"id": jid, "prompt": "p", "cwd": "/w",
                              "provider": provider,
                              "read_only": (record or {}).get("read_only", False),
                              "worktree": (record or {}).get("worktree", ""),
                              "live_root": (record or {}).get("live_root", "")})
        if (record or {}).get("session_id"):
            joblog.record_session(jid, record["session_id"],
                                  transport=record.get("transport", ""))
        if (record or {}).get("model_used"):
            joblog.record_model_used(jid, record["model_used"])
        joblog.record_finish(jid, (record or {}).get("status", "done"), "out")
        jdir = self.jobs_root / jid
        jdir.mkdir(parents=True)
        st = {"status": "done", "provider": provider}
        st.update(state or {})
        (jdir / "state.json").write_text(json.dumps(st), encoding="utf-8")
        (jdir / "output.log").write_text(log, encoding="utf-8")
        return {"input": "x", "provider": provider, "cwd": "/w",
                "stages": {"s": {"job_id": jid, "status": "done",
                                 **(stage or {})}}}

    def gate(self, op, value="", run=None, subject=None):
        st_def = {"id": "g", "mode": "logic", "needs": ["s"],
                  "logic": {"operation": op, "value": value,
                            **({"subject": subject} if subject else {})}}
        return circuits.logic_passes(op, value, "", run, st_def)

    def test_text_gates_are_unchanged(self):
        run = {"stages": {}}
        st = {"id": "g", "logic": {}}
        self.assertTrue(circuits.logic_passes("contains", "Ok", "ok there", run, st)[0])
        self.assertFalse(circuits.logic_passes("equals", "a", "b", run, st)[0])
        self.assertFalse(circuits.logic_passes("has_output", "", "  ", run, st)[0])

    def test_an_unknown_gate_and_a_gate_with_no_upstream_both_fail(self):
        run = self.probe_run()
        self.assertFalse(self.gate("frobnicate", run=run)[0])
        st_def = {"id": "g", "mode": "logic", "needs": [],
                  "logic": {"operation": "provider_is"}}
        ok, why = circuits.logic_passes("provider_is", "", "", run, st_def)
        self.assertFalse(ok)
        self.assertIn("upstream", why)

    def test_provider_is_defaults_to_the_runs_provider(self):
        run = self.probe_run(provider="openai")
        self.assertTrue(self.gate("provider_is", run=run)[0])
        self.assertTrue(self.gate("provider_is", "openai", run=run)[0])
        self.assertFalse(self.gate("provider_is", "google", run=run)[0])

    def test_transport_matches_provider_reads_the_expected_lane(self):
        run = self.probe_run(provider="openai",
                             record={"session_id": "t1",
                                     "transport": "codex-app-server"})
        self.assertTrue(self.gate("transport_matches_provider", run=run)[0])
        run = self.probe_run(provider="openai",
                             record={"session_id": "t1", "transport": "cli-exec"})
        ok, why = self.gate("transport_matches_provider", run=run)
        self.assertFalse(ok)
        self.assertIn("cli-exec", why)
        self.assertIn("codex-app-server", why)

    def test_tool_called_normalises_every_adapters_prefix(self):
        for name in ("mcp__vira__find", "vira.find", "find"):
            run = self.probe_run(state={"tools": [{"name": name}]})
            self.assertTrue(self.gate("tool_called", "find", run=run)[0], name)
            self.assertTrue(self.gate("tool_called", "mcp__vira__find",
                                      run=run)[0], name)
            self.assertFalse(self.gate("tool_not_called", "find", run=run)[0])
        run = self.probe_run(state={"tools": [{"name": "Bash"}]})
        ok, why = self.gate("tool_called", "find", run=run)
        self.assertFalse(ok)
        self.assertIn("bash", why)

    def test_card_raised_reads_the_history_and_states_non_parity(self):
        run = self.probe_run(state={"cards": [{"kind": "ask"}]})
        self.assertTrue(self.gate("card_raised", "ask", run=run)[0])
        self.assertTrue(self.gate("card_raised", "", run=run)[0])
        self.assertFalse(self.gate("card_raised", "permission", run=run)[0])
        # No shell or file tool exists on Gemini, so a permission card cannot
        # be raised there - the gate says so rather than failing the probe.
        run = self.probe_run(provider="google")
        ok, why = self.gate("card_raised", "permission", run=run)
        self.assertTrue(ok)
        self.assertIn("no workspace tools", why)
        self.assertFalse(self.gate("card_raised", "ask", run=run)[0])

    def test_guard_held_needs_the_worktree_and_the_denial(self):
        denial = "[vira] denied (branch-first) — Write targets the live checkout: /w/README.md\n"
        run = self.probe_run(record={"worktree": "/w/.worktrees/x",
                                     "live_root": "/w"}, log=denial)
        self.assertTrue(self.gate("guard_held", run=run)[0])
        run = self.probe_run(record={"worktree": "/w/.worktrees/x"}, log="")
        self.assertFalse(self.gate("guard_held", run=run)[0])
        run = self.probe_run(record={}, log=denial)
        self.assertFalse(self.gate("guard_held", run=run)[0])
        run = self.probe_run(provider="xai", record={})
        ok, why = self.gate("guard_held", run=run)
        self.assertTrue(ok)
        self.assertIn("no workspace tools", why)

    def test_read_only_honored_and_worktree_gates(self):
        run = self.probe_run(record={"read_only": True})
        self.assertTrue(self.gate("read_only_honored", run=run)[0])
        self.assertTrue(self.gate("not_in_worktree", run=run)[0])
        self.assertFalse(self.gate("placed_in_worktree", run=run)[0])
        run = self.probe_run(record={"read_only": True, "worktree": "/w/.wt"})
        self.assertFalse(self.gate("read_only_honored", run=run)[0])
        self.assertTrue(self.gate("placed_in_worktree", run=run)[0])

    def test_interrupt_honored_forks_on_the_capability(self):
        run = self.probe_run(state={"interrupted": True},
                             stage={"timed_out": True})
        self.assertTrue(self.gate("interrupt_honored", run=run)[0])
        run = self.probe_run(state={}, stage={"timed_out": True})
        self.assertFalse(self.gate("interrupt_honored", run=run)[0])
        run = self.probe_run(state={"interrupted": True}, stage={})
        ok, why = self.gate("interrupt_honored", run=run)
        self.assertFalse(ok)
        self.assertIn("never hit", why)
        run = self.probe_run(provider="google", state={}, stage={"timed_out": True})
        self.assertTrue(self.gate("interrupt_honored", run=run)[0])

    def test_log_model_and_outcome_gates(self):
        run = self.probe_run(record={"session_id": "s", "model_used": "gpt-5.6-sol",
                                     "status": "done"},
                             log="[vira] gpt-5.6-sol working\n")
        self.assertTrue(self.gate("log_contains", "working", run=run)[0])
        self.assertFalse(self.gate("log_not_contains", "working", run=run)[0])
        self.assertTrue(self.gate("model_used_contains", "5.6", run=run)[0])
        self.assertTrue(self.gate("outcome_is", "done", run=run)[0])
        self.assertFalse(self.gate("outcome_is", "error", run=run)[0])

    def test_a_named_subject_outranks_the_first_need(self):
        run = self.probe_run(provider="openai")
        run["stages"]["other"] = {"job_id": None, "status": "done"}
        st_def = {"id": "g", "mode": "logic", "needs": ["other", "s"],
                  "logic": {"operation": "provider_is", "value": "openai",
                            "subject": "s"}}
        self.assertTrue(circuits.logic_passes("provider_is", "openai", "",
                                              run, st_def)[0])
        st_def["logic"].pop("subject")
        ok, why = circuits.logic_passes("provider_is", "openai", "", run, st_def)
        self.assertFalse(ok)
        self.assertIn("launched no session", why)

    def test_a_gate_runs_inside_a_driven_flow(self):
        def script(rec):
            return {"out": "hi", "state": {"tools": [{"name": "mcp__vira__find"}]},
                    "record": {"session_id": "s1", "transport": "claude-sdk"}}
        self.stub(script)
        cid = self.circuit([dict(AGENT),
                            {"id": "g1", "mode": "logic", "needs": ["a"],
                             "logic": {"operation": "tool_called", "value": "find"}},
                            {"id": "g2", "mode": "logic", "needs": ["a"],
                             "logic": {"operation": "transport_matches_provider"}},
                            {"id": "g3", "mode": "logic", "needs": ["a"],
                             "logic": {"operation": "tool_called", "value": "mail_search"}}])
        run = self.drive(circuits.start_run(cid, "task", provider="anthropic")["id"])
        self.assertEqual(run["stages"]["g1"]["status"], "done")
        self.assertEqual(run["stages"]["g2"]["status"], "done")
        self.assertEqual(run["stages"]["g3"]["status"], "error")
        self.assertIn("tools called: find", run["stages"]["g3"]["result_text"])
        self.assertEqual(run["status"], "error")   # one failed gate = failed run


class Starters(Base):
    PARITY = ("parity-council", "parity-harness", "parity-cards")

    def test_the_three_starters_seed_and_validate(self):
        by_id = {c["id"]: c for c in circuits.list_circuits()}
        for cid in self.PARITY:
            self.assertIn(cid, by_id)
            circuits.validate_stages(by_id[cid]["stages"])
            self.assertTrue(by_id[cid]["builtin"])

    def test_the_council_covers_every_provider_with_its_own_judge(self):
        t = next(t for t in circuits.TEMPLATES if t["id"] == "parity-council")
        agents = [s for s in t["stages"] if s.get("mode") == "manual"]
        judges = [s for s in t["stages"] if s.get("mode") == "judge"]
        self.assertEqual({s["provider"] for s in agents},
                         set(agentbackend.CAPABILITIES))
        self.assertEqual(len(judges), len(agents))
        for j in judges:
            self.assertEqual(len(j["judge"]["of"]), 1)
            self.assertEqual(j["judge"]["min_grade"], "")   # grade, never retry
            self.assertFalse(j.get("provider"))            # the constant grader

    def test_every_harness_gate_reads_a_real_operation_and_its_own_probe(self):
        t = next(t for t in circuits.TEMPLATES if t["id"] == "parity-harness")
        by_id = {s["id"]: s for s in t["stages"]}
        gates = [s for s in t["stages"] if s.get("mode") == "logic"]
        self.assertGreaterEqual(len(gates), 5)
        for g in gates:
            self.assertIn(g["logic"]["operation"], circuits.LOGIC_OPS)
            self.assertEqual(len(g["needs"]), 1)
            self.assertNotEqual(by_id[g["needs"][0]].get("mode"), "logic")
        probes = [s for s in t["stages"] if s.get("mode") != "logic"]
        self.assertFalse(any(s.get("provider") for s in probes),
                         "the harness takes its provider from the run")
        self.assertEqual(by_id["recall"]["continues"], "codeword")
        self.assertEqual(by_id["slow"]["on_timeout"], "continue")
        self.assertGreater(by_id["slow"]["timeout_s"], 0)
        self.assertIn("{{cwd}}", by_id["guard"]["prompt"])
        self.assertFalse(by_id["guard"].get("read_only"))
        ops = {g["logic"]["operation"] for g in gates}
        self.assertTrue({"tool_called", "transport_matches_provider",
                         "read_only_honored", "guard_held",
                         "interrupt_honored", "contains"} <= ops)

    def test_the_cards_flow_needs_a_person_and_says_so(self):
        t = next(t for t in circuits.TEMPLATES if t["id"] == "parity-cards")
        self.assertIn("waits", t["description"])
        ops = {s["logic"]["operation"] for s in t["stages"]
               if s.get("mode") == "logic"}
        self.assertEqual(ops, {"card_raised"})

    def test_the_starters_appear_as_flows(self):
        ids = {f["id"] for f in flows.list_flows()}
        self.assertTrue(set(self.PARITY) <= ids)


class FlowRoundTrip(Base):
    def test_the_knobs_survive_the_forge(self):
        payload = {"name": "Knobs", "nodes": [
            {"id": "a", "type": "agent", "name": "A", "model": "",
             "provider": "openai", "mode": "manual", "read_only": True,
             "prompt": "remember"},
            {"id": "b", "type": "agent", "name": "B", "model": "",
             "mode": "manual", "read_only": True, "prompt": "recall",
             "continues": "a", "timeout_s": 45, "on_timeout": "continue"},
            {"id": "g", "type": "logic", "name": "G",
             "logic": {"operation": "tool_called", "value": "find",
                       "subject": "a"}}],
            "edges": [{"from": "a", "to": "b"}, {"from": "a", "to": "g"}],
            "contexts": []}
        saved = flows.save_flow(payload, save_as=True)
        circ = circuits.get_circuit(saved["id"])
        by_id = {s["id"]: s for s in circ["stages"]}
        self.assertEqual(by_id["a"]["provider"], "openai")
        self.assertEqual(by_id["b"]["continues"], "a")
        self.assertEqual(by_id["b"]["timeout_s"], 45)
        self.assertEqual(by_id["b"]["on_timeout"], "continue")
        self.assertEqual(by_id["g"]["logic"]["subject"], "a")
        nodes = {n["id"]: n for n in flows.get_flow(saved["id"])["nodes"]}
        self.assertEqual(nodes["a"]["provider"], "openai")
        self.assertEqual(nodes["b"]["continues"], "a")
        self.assertEqual(nodes["b"]["timeout_s"], 45)

    def test_a_continuation_the_forge_did_not_wire_is_refused(self):
        payload = {"name": "Bad", "nodes": [
            {"id": "a", "type": "agent", "name": "A", "prompt": "x"},
            {"id": "b", "type": "agent", "name": "B", "prompt": "y",
             "continues": "a"}],
            "edges": [], "contexts": []}
        with self.assertRaises(ValueError):
            flows.save_flow(payload, save_as=True)


class CardHistory(unittest.TestCase):
    def test_cards_are_kept_after_they_resolve_and_bounded(self):
        r = SimpleNamespace(state={}, CARDS_KEEP=runner.Runner.CARDS_KEEP)
        for i in range(runner.Runner.CARDS_KEEP + 5):
            runner.Runner.record_card(r, f"r{i}", "permission", f"call {i}")
        runner.Runner.resolve_card(r, "r3", "deny")
        cards = r.state["cards"]
        self.assertEqual(len(cards), runner.Runner.CARDS_KEEP)
        self.assertNotIn("r3", {c["req_id"] for c in cards})   # pruned
        runner.Runner.record_card(r, "ask1", "ask", "which?")
        runner.Runner.resolve_card(r, "ask1", "answered")
        last = r.state["cards"][-1]
        self.assertEqual((last["kind"], last["decision"]), ("ask", "answered"))

    def test_every_card_site_records_and_the_epilogue_marks_interrupts(self):
        src = (ROOT / "server" / "runner.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("self.record_card("), 3)   # ask, permission, landing
        self.assertGreaterEqual(src.count("self.resolve_card("), 2)
        self.assertIn('self.state["interrupted"] = bool(self.interrupted)', src)


class TransportContract(unittest.TestCase):
    """EXPECTED_TRANSPORT must be what the adapters really stamp, and the
    Forge's gate vocabulary must be the engine's."""

    def test_expected_transport_names_every_provider_the_way_its_adapter_does(self):
        self.assertEqual(set(agentbackend.EXPECTED_TRANSPORT),
                         set(agentbackend.CAPABILITIES))
        runner_src = (ROOT / "server" / "runner.py").read_text(encoding="utf-8")
        codex_src = (ROOT / "server" / "codexapp.py").read_text(encoding="utf-8")
        fa_src = (ROOT / "server" / "functionagent.py").read_text(encoding="utf-8")
        self.assertIn('transport="claude-sdk"', runner_src)
        self.assertIn('transport="codex-app-server"', codex_src)
        m = re.search(r'return f"\{self\.provider\}-(function-api)"', fa_src)
        self.assertIsNotNone(m, "functionagent's transport shape moved")
        exp = agentbackend.EXPECTED_TRANSPORT
        self.assertEqual(exp["anthropic"], "claude-sdk")
        self.assertEqual(exp["openai"], "codex-app-server")
        for pid in ("google", "xai"):
            self.assertEqual(exp[pid], f"{pid}-{m.group(1)}")

    def test_the_forge_offers_exactly_the_engines_gates(self):
        src = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
        block = src[src.index("const LOGIC_OPS = ["):src.index("function logicControls")]
        offered = set(re.findall(r'\["([a-z_]+)", "', block))
        self.assertEqual(offered, set(circuits.LOGIC_OPS))

    def test_the_launch_bar_and_the_run_request_carry_a_provider(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "forge.js").read_text(encoding="utf-8")
        self.assertIn('id="forge-run-provider"', html)
        self.assertIn('provider: q("#forge-run-provider")', js)
        main = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
        self.assertIn("provider=req.provider", main)


if __name__ == "__main__":
    unittest.main()

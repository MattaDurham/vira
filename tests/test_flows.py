"""The Forge product adapter and its executable graph parts.

A Flow is a board of cards wired in one direction. The server reads: who
runs a card (model, provider), what it may touch (read / ask / edit /
full), its instruction box, which cards feed it, and the source cards
wired into it. Everything else on the board is placement, and the sidecar
holds only that. These tests pin the contract the editor relies on:

- the card's permission reaches the stage (read_only was unsettable for
  three months: every added card ran read-only whatever its mode said);
- a judge card picks its own retry target, and its box is what to weigh;
- a source card is a name and a path, never the text behind it;
- a wire is from one card to another with an optional note, nothing more;
- standing loops are not Flows.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import actions, circuits, flows, ideas, routines


def _agent(nid, name, prompt="Do {{input}}", **extra):
    return {"id": nid, "type": "agent", "name": name, "model": "sonnet",
            "mode": "acceptEdits", "read_only": False, "prompt": prompt,
            "x": 100, "y": 100, **extra}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patches = [
            mock.patch.object(flows, "STORE", root / "flow-graphs.json"),
            mock.patch.object(flows, "LIBRARY", root / "library"),
            mock.patch.object(circuits, "DEFS", root / "circuits.json"),
            mock.patch.object(circuits, "RUNS", root / "circuit-runs.json"),
            mock.patch.object(routines, "STORE", root / "routines.json"),
            mock.patch.object(actions, "scan_library", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _stages(self, flow_id):
        return {s["id"]: s for s in circuits.get_circuit(flow_id)["stages"]}

    def test_flow_compiles_sources_approval_and_output(self):
        payload = {
            "name": "Editorial gate",
            "nodes": [
                {"id": "ctx", "type": "context", "name": "Brief",
                 "description": "Use the approved facts.",
                 "source_ref": "vault/brief.md"},
                {"id": "tool", "type": "tool", "name": "Research",
                 "source": "skill", "source_ref": "/lib/skills/deep-research",
                 "description": "Check primary sources."},
                _agent("write", "Write", "Write from {{input}}"),
                {"id": "okay", "type": "approval", "name": "Owner review",
                 "prompt": "Approve this draft before it is recorded."},
                {"id": "record", "type": "output", "name": "Record",
                 "output": {"destination": "record"},
                 "prompt": "One paragraph, no headings."},
            ],
            "edges": [
                {"from": "ctx", "to": "write", "instructions": "Prefer this source."},
                {"from": "tool", "to": "write"},
                {"from": "write", "to": "okay"},
                {"from": "okay", "to": "record"},
                {"from": "write", "to": "record"},
            ],
        }
        saved = flows.save_flow(payload, save_as=True)
        stages = self._stages(saved["id"])
        self.assertEqual(stages["okay"]["mode"], "approval")
        self.assertEqual(stages["okay"]["approval"]["instructions"],
                         "Approve this draft before it is recorded.")
        self.assertEqual(stages["record"]["mode"], "output")
        self.assertEqual(stages["record"]["output"]["instructions"],
                         "One paragraph, no headings.")
        self.assertEqual(stages["okay"]["needs"], ["write"])
        self.assertEqual(sorted(stages["record"]["needs"]), ["okay", "write"])
        forge = stages["write"]["forge"]
        self.assertEqual(forge["contexts"][0]["ref"], "vault/brief.md")
        self.assertEqual(forge["tools"][0]["name"], "Research")
        self.assertEqual(forge["tools"][0]["source_ref"], "/lib/skills/deep-research")
        self.assertEqual(forge["tools"][0]["instructions"], "Check primary sources.")
        self.assertIn("Prefer this source.", forge["wire_instructions"])
        self.assertEqual(forge["outputs"][0]["instructions"], "One paragraph, no headings.")
        prompt = circuits.render_prompt(stages["write"]["prompt"],
                                         {"input": "an essay", "stages": {}},
                                         stages["write"])
        self.assertIn("CONNECTED CONTEXT", prompt)
        self.assertIn("CONNECTED CAPABILITIES", prompt)
        self.assertIn("/lib/skills/deep-research", prompt)

    def test_the_cards_permission_reaches_the_stage(self):
        payload = {"name": "Permissions", "nodes": [
            _agent("look", "Look", read_only=True, mode="manual"),
            _agent("ask", "Ask", read_only=False, mode="manual"),
            _agent("edit", "Edit", read_only=False, mode="acceptEdits"),
            _agent("full", "Full", read_only=False, mode="bypassPermissions"),
        ], "edges": []}
        stages = self._stages(flows.save_flow(payload, save_as=True)["id"])
        self.assertTrue(stages["look"]["read_only"])
        self.assertFalse(stages["ask"]["read_only"])
        self.assertEqual(stages["ask"]["mode"], "manual")
        self.assertFalse(stages["edit"]["read_only"])
        self.assertEqual(stages["edit"]["mode"], "acceptEdits")
        self.assertEqual(stages["full"]["mode"], "bypassPermissions")
        self.assertFalse(stages["full"]["read_only"])
        # and it comes back the same way, so the card shows what will run
        nodes = {n["id"]: n for n in flows.get_flow(stages["full"] and
                                                     circuits.list_circuits()[-1]["id"])["nodes"]}
        self.assertTrue(nodes["look"]["read_only"])
        self.assertFalse(nodes["full"]["read_only"])

    def test_the_judge_card_picks_its_retry_target_and_what_to_weigh(self):
        payload = {"name": "Judged", "nodes": [
            _agent("build", "Build"),
            {"id": "judge", "type": "judge", "name": "Judge", "model": "",
             "prompt": "Weigh correctness over polish.",
             "judge": {"min_grade": "B", "max_retries": 2,
                       "retry_stage": "build", "stray": "ignored"}},
        ], "edges": [{"from": "build", "to": "judge"}]}
        saved = flows.save_flow(payload, save_as=True)
        stage = self._stages(saved["id"])["judge"]
        self.assertEqual(stage["judge"], {"of": ["build"], "min_grade": "B",
                                          "max_retries": 2, "retry_stage": "build"})
        self.assertEqual(stage["extra"], "Weigh correctness over polish.")
        self.assertNotIn("prompt", stage)
        node = next(n for n in saved["nodes"] if n["id"] == "judge")
        self.assertEqual(node["prompt"], "Weigh correctness over polish.")
        self.assertEqual(node["judge"]["retry_stage"], "build")

    def test_a_judge_with_no_pick_only_grades(self):
        payload = {"name": "Graded", "nodes": [
            _agent("build", "Build"),
            {"id": "judge", "type": "judge", "name": "Judge",
             "judge": {"min_grade": "", "max_retries": 1, "retry_stage": ""}},
        ], "edges": [{"from": "build", "to": "judge"}]}
        stage = self._stages(flows.save_flow(payload, save_as=True)["id"])["judge"]
        self.assertNotIn("retry_stage", stage["judge"])
        self.assertEqual(stage["judge"]["of"], ["build"])

    def test_a_retry_target_that_is_not_a_card_is_refused(self):
        payload = {"name": "Bad judge", "nodes": [
            _agent("build", "Build"),
            {"id": "judge", "type": "judge", "name": "Judge",
             "judge": {"min_grade": "B", "max_retries": 1, "retry_stage": "nope"}},
        ], "edges": [{"from": "build", "to": "judge"}]}
        with self.assertRaises(ValueError):
            flows.save_flow(payload, save_as=True)

    def test_revision_and_placement_round_trip(self):
        payload = {"name": "Round trip", "nodes": [
            _agent("a", "A", x=410, y=230)], "edges": []}
        first = flows.save_flow(payload, save_as=True)
        self.assertEqual(first["nodes"][0]["x"], 410)
        first["nodes"][0]["x"] = 777
        second = flows.save_flow(first)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["nodes"][0]["x"], 777)

    def test_the_sidecar_holds_placement_and_sources_only(self):
        payload = {"name": "Lean", "nodes": [
            _agent("a", "A", description="thrown away", spatial_layer=2,
                   width=900, expanded=True),
            {"id": "src", "type": "tool", "name": "watch", "source": "skill",
             "source_ref": "/lib/skills/watch", "description": "Use it for video.",
             "prompt": "x" * 50_000, "source_files": ["SKILL.md"], "x": 5, "y": 6},
        ], "edges": [{"from": "src", "to": "a", "from_port": "capability",
                      "to_port": "tools", "label": "L", "direction": "both",
                      "instructions": "note"}]}
        saved = flows.save_flow(payload, save_as=True)
        graphs = flows._store()["graphs"][saved["id"]]
        agent = next(n for n in graphs["nodes"] if n["id"] == "a")
        self.assertEqual(set(agent), {"id", "type", "x", "y"})
        source = next(n for n in graphs["nodes"] if n["id"] == "src")
        self.assertEqual(set(source), {"id", "type", "x", "y", "name",
                                       "description", "source", "source_ref"})
        self.assertEqual(set(graphs["edges"][0]), {"id", "from", "to", "instructions"})
        # the node the editor gets back carries no description to discard
        node = next(n for n in saved["nodes"] if n["id"] == "a")
        self.assertNotIn("description", node)
        self.assertNotIn("spatial_layer", node)
        # a shared-reference wire no longer adds a sentence to the prompt
        forge = self._stages(saved["id"])["a"]["forge"]
        self.assertEqual(forge["wire_instructions"], ["note"])

    def test_a_schedule_card_becomes_a_routine_and_keeps_its_place(self):
        payload = {"name": "Scheduled", "nodes": [
            _agent("a", "A"),
            {"id": "when", "type": "trigger", "name": "Every morning",
             "daily_at": "07:30", "enabled": True, "notify": True,
             "x": 40, "y": 300}],
            "edges": [{"from": "when", "to": "a"}]}
        saved = flows.save_flow(payload, save_as=True)
        rows = [r for r in routines.list_routines() if r.get("circuit_id") == saved["id"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["daily_at"], "07:30")
        trigger = next(n for n in saved["nodes"] if n["type"] == "trigger")
        self.assertEqual(trigger["id"], f"routine:{rows[0]['id']}")
        self.assertEqual((trigger["x"], trigger["y"]), (40, 300))
        self.assertEqual([n["type"] for n in saved["nodes"]].count("trigger"), 1)
        # editing the cadence on the card and saving again updates the row
        trigger["daily_at"] = "08:00"
        again = flows.save_flow(saved)
        rows = [r for r in routines.list_routines() if r.get("circuit_id") == saved["id"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["daily_at"], "08:00")
        self.assertEqual([n["type"] for n in again["nodes"]].count("trigger"), 1)

    def test_standing_loops_are_not_flows(self):
        routines.save_routine({"name": "Hourly thing", "kind": "custom",
                               "prompt": "look", "every_hours": 1})
        ids = [f["id"] for f in flows.list_flows()]
        self.assertFalse(any(i.startswith("routine:") for i in ids))
        self.assertIsNone(flows.get_flow("routine:muse"))

    def test_the_library_offers_sources_and_no_primitives(self):
        root = flows.LIBRARY
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "capture.py").write_text("print(1)\n", encoding="utf-8")
        catalog = {item["id"]: item for item in flows.kit_catalog()}
        self.assertFalse([k for k in catalog if k.startswith("primitive:")])
        self.assertEqual(catalog["script:capture"]["type"], "tool")
        self.assertTrue(catalog["script:capture"]["source_ref"].endswith("capture.py"))

    def test_approval_waits_then_resumes_without_a_model_session(self):
        circuits.save_circuit({"id": "approve", "name": "Approve", "stages": [
            {"id": "gate", "name": "Owner", "mode": "approval", "needs": [],
             "approval": {"instructions": "Ship this?"}},
            {"id": "record", "name": "Record", "mode": "output",
             "needs": ["gate"], "output": {"destination": "record"}},
        ]})
        run = circuits.start_run("approve", "the draft")
        driver = circuits.Driver.__new__(circuits.Driver)
        driver._advance(run)
        waiting = circuits.get_run(run["id"])
        self.assertEqual(waiting["stages"]["gate"]["status"], "waiting")
        circuits.decide_approval(run["id"], "gate", True, "Looks right")
        driver._advance(circuits.get_run(run["id"]))
        final = circuits.get_run(run["id"])
        self.assertEqual(final["status"], "done")
        self.assertIn("Approved", final["stages"]["record"]["result_text"])

    def test_logic_gate_can_stop_downstream_execution(self):
        circuits.save_circuit({"id": "logic", "name": "Logic", "stages": [
            {"id": "gate", "name": "Contains yes", "mode": "logic", "needs": [],
             "logic": {"operation": "contains", "value": "yes"}},
            {"id": "record", "name": "Record", "mode": "output",
             "needs": ["gate"], "output": {"destination": "record"}},
        ]})
        run = circuits.start_run("logic", "no")
        driver = circuits.Driver.__new__(circuits.Driver)
        driver._advance(run)
        driver._advance(circuits.get_run(run["id"]))
        final = circuits.get_run(run["id"])
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["stages"]["record"]["status"], "skipped")

    def test_the_starters_round_trip_unchanged(self):
        for circ in circuits.list_circuits():
            with self.subTest(circ["id"]):
                flow = flows.get_flow(circ["id"])
                before = {s["id"]: s for s in circ["stages"]}
                after = {s["id"]: s for s in flows._compile(flow)}
                self.assertEqual(set(before), set(after))
                for sid, stage in before.items():
                    for key in ("mode", "model", "prompt", "needs", "provider",
                                "timeout_s", "on_timeout", "continues", "logic",
                                "approval"):
                        if key in stage:
                            self.assertEqual(after[sid].get(key), stage[key],
                                             f"{circ['id']}.{sid}.{key}")
                    if stage.get("mode") != "judge":
                        # a judge launches read-only whatever its row says
                        self.assertEqual(bool(after[sid].get("read_only")),
                                         bool(stage.get("read_only")), f"{circ['id']}.{sid}")
                    if stage.get("output"):
                        self.assertEqual(after[sid]["output"]["destination"],
                                         stage["output"]["destination"])
                        self.assertEqual(after[sid]["output"].get("instructions") or "",
                                         stage["output"].get("instructions") or "")
                    if stage.get("judge"):
                        self.assertEqual(after[sid]["judge"]["of"], stage["judge"]["of"])
                        self.assertEqual(after[sid]["judge"].get("retry_stage"),
                                         stage["judge"].get("retry_stage"))
                        self.assertEqual(after[sid]["judge"]["min_grade"],
                                         stage["judge"].get("min_grade", ""))

    def test_the_input_card_is_the_ask(self):
        payload = {"name": "Asked", "nodes": [
            {"id": "input", "type": "input", "name": "Input",
             "prompt": "Rebuild the radar", "x": 10, "y": 10},
            _agent("a", "A"), _agent("b", "B")],
            "edges": [{"from": "input", "to": "a"}]}
        saved = flows.save_flow(payload, save_as=True)
        stages = self._stages(saved["id"])
        self.assertTrue(stages["a"]["forge"]["input"])
        self.assertFalse(stages["b"]["forge"]["input"])
        card = next(n for n in saved["nodes"] if n["type"] == "input")
        self.assertEqual(card["prompt"], "Rebuild the radar")
        self.assertEqual([e["to"] for e in saved["edges"] if e["from"] == "input"], ["a"])
        self.assertEqual(flows.input_text(saved["id"]), "Rebuild the radar")
        seen = {}
        with mock.patch.object(circuits, "start_run",
                               lambda cid, text, **kw: seen.update(cid=cid, text=text) or {"id": "r"}):
            flows.run_flow(saved["id"], "")
            self.assertEqual(seen["text"], "Rebuild the radar")
            flows.run_flow(saved["id"], "something else")
            self.assertEqual(seen["text"], "something else")

    def test_a_starter_gets_an_input_card_wired_to_its_entry_cards(self):
        flow = flows.get_flow("plan-build-judge")
        self.assertTrue(any(n["type"] == "input" for n in flow["nodes"]))
        fed = sorted(e["to"] for e in flow["edges"] if e["from"] == "input")
        self.assertEqual(fed, ["plan"])
        # the parity probes say no to the ask on purpose
        harness = flows.get_flow("parity-harness")
        self.assertEqual([e for e in harness["edges"] if e["from"] == "input"], [])
        # and a Flow saved from a starter keeps that shape
        saved = flows.save_flow({**flow, "name": "copy"}, save_as=True)
        stages = self._stages(saved["id"])
        self.assertTrue(stages["plan"]["forge"]["input"])
        self.assertFalse(stages["build"]["forge"]["input"])

    def test_the_cards_environment_is_what_is_wired_in(self):
        payload = {"name": "Env", "nodes": [
            {"id": "watch", "type": "tool", "name": "watch", "source": "skill",
             "source_ref": "/lib/skills/watch/SKILL.md"},
            {"id": "settings", "type": "tool", "name": "Your Claude settings",
             "source": "settings", "source_ref": "user"},
            {"id": "plugin", "type": "tool", "name": "adhd", "source": "plugin",
             "source_ref": "/lib/plugins/adhd/0.4.1"},
            {"id": "script", "type": "tool", "name": "capture", "source": "script",
             "source_ref": "/lib/scripts/capture.py"},
            {"id": "bundle", "type": "env", "name": "Research kit", "items": [
                {"kind": "skill", "name": "deep-dive", "source_ref": "/lib/skills/deep-dive/SKILL.md"},
                {"kind": "agent", "name": "scout", "source_ref": "/lib/agents/scout.md"}]},
            _agent("a", "A"),
            _agent("bare", "Bare", environment={"instructions": []}),
            _agent("custom", "Custom", environment={"instructions": ["/vault/RULES.md"]}),
        ], "edges": [{"from": "watch", "to": "a"}, {"from": "settings", "to": "a"},
                     {"from": "plugin", "to": "a"}, {"from": "script", "to": "a"},
                     {"from": "bundle", "to": "a"}]}
        stages = self._stages(flows.save_flow(payload, save_as=True)["id"])
        env = stages["a"]["forge"]["environment"]
        self.assertEqual(env["skills"], ["watch", "deep-dive"])
        self.assertEqual(env["settings"], ["user"])
        self.assertEqual(env["plugins"], ["/lib/plugins/adhd/0.4.1"])
        self.assertEqual(env["agents"], ["/lib/agents/scout.md"])
        self.assertEqual(env["instructions"], ["project"])
        # a script is a capability line, not part of the environment
        self.assertEqual([t["name"] for t in stages["a"]["forge"]["tools"]][:1], ["watch"])
        self.assertIn("capture", [t["name"] for t in stages["a"]["forge"]["tools"]])
        bare = stages["bare"]["forge"]["environment"]
        self.assertEqual(bare, {"skills": [], "plugins": [], "agents": [], "commands": [],
                                "mcp": [], "hooks": [], "settings": [], "instructions": []})
        self.assertEqual(stages["custom"]["forge"]["environment"]["instructions"], ["/vault/RULES.md"])

    def test_a_starters_environment_shows_as_cards(self):
        flow = flows.get_flow("watch-build")
        skill = next(n for n in flow["nodes"] if n["type"] == "tool" and n.get("source") == "skill")
        self.assertEqual(skill["name"], "watch")
        self.assertEqual([e["to"] for e in flow["edges"] if e["from"] == skill["id"]], ["watch"])
        agent = next(n for n in flow["nodes"] if n["id"] == "plan")
        self.assertEqual(agent["environment"], {"instructions": ["project"]})
        # saved as the owner's copy, the cards are the environment
        saved = flows.save_flow({**flow, "name": "copy"}, save_as=True)
        env = self._stages(saved["id"])["watch"]["forge"]["environment"]
        self.assertEqual(env["skills"], ["watch"])
        self.assertEqual(env["instructions"], ["project"])

    def test_saved_environments_round_trip(self):
        with mock.patch.object(flows, "ENVS", Path(self.tmp.name) / "envs.json"):
            with self.assertRaises(ValueError):
                flows.save_environment({"name": "Empty", "items": []})
            row = flows.save_environment({"name": "Research kit", "items": [
                {"kind": "skill", "name": "deep-dive", "source_ref": "/x"}, "junk"]})
            self.assertEqual([r["id"] for r in flows.list_environments()], [row["id"]])
            self.assertEqual(flows.list_environments()[0]["items"],
                             [{"kind": "skill", "name": "deep-dive", "source_ref": "/x"}])
            flows.delete_environment(row["id"])
            self.assertEqual(flows.list_environments(), [])
            with self.assertRaises(KeyError):
                flows.delete_environment(row["id"])

    def test_defaults_name_the_configured_provider(self):
        with mock.patch.object(flows.agentbackend, "default_session_provider",
                               return_value="openai"):
            out = flows.defaults()
        self.assertEqual(out["provider"], "openai")
        self.assertEqual(out["provider_label"], flows.models.PROVIDERS["openai"]["label"])

    def test_a_scheduled_flow_carries_the_input_cards_ask(self):
        payload = {"name": "Nightly", "nodes": [
            {"id": "input", "type": "input", "name": "Input", "prompt": "https://example.com/feed"},
            _agent("a", "A"),
            {"id": "when", "type": "trigger", "name": "Nightly", "daily_at": "20:00",
             "enabled": True, "notify": False}],
            "edges": [{"from": "input", "to": "a"}, {"from": "when", "to": "a"}]}
        saved = flows.save_flow(payload, save_as=True)
        row = next(r for r in routines.list_routines() if r.get("circuit_id") == saved["id"])
        self.assertEqual(row["prompt"], "https://example.com/feed")

    def test_the_library_offers_settings_and_instructions_as_sources(self):
        catalog = {item["id"]: item for item in flows.kit_catalog()}
        self.assertEqual(catalog["settings:user"]["source_ref"], "user")
        self.assertEqual(catalog["instructions:project"]["kind"], "instructions")

    def test_kit_source_unfolds_text_but_cannot_escape_library(self):
        root = flows.LIBRARY
        skill = root / "skills" / "sample"
        skill.mkdir(parents=True)
        source = skill / "SKILL.md"
        source.write_text("# Sample\n\nComplete instructions.", encoding="utf-8")
        loaded = flows.kit_source(source)
        self.assertIn("Complete instructions", loaded["text"])
        self.assertEqual(loaded["files"], ["SKILL.md"])
        with self.assertRaises(ValueError):
            flows.kit_source(Path(self.tmp.name).parent / "outside.md")


class DeliveryTests(unittest.TestCase):
    """The wire delivers (circuits._delivered): a card gets the finished
    text of the cards wired into it, and an entry card gets the ask, unless
    its box already places them."""

    def _run(self, defs, outputs, ask="the ask"):
        return {"input": ask, "stages_def": defs,
                "stages": {sid: {"result_text": text} for sid, text in outputs.items()}}

    def _render(self, stage, run):
        return circuits.render_prompt(stage.get("prompt") or "", run, stage)

    def test_an_entry_card_gets_the_ask_unless_it_places_it(self):
        defs = [{"id": "a", "mode": "manual", "name": "A", "prompt": "Plan it."}]
        run = self._run(defs, {})
        self.assertIn("THE ASK\nthe ask", self._render(defs[0], run))
        placed = {"id": "a", "mode": "manual", "name": "A", "prompt": "Plan {{input}} now."}
        out = self._render(placed, run)
        self.assertIn("Plan the ask now.", out)
        self.assertNotIn("THE ASK", out)

    def test_the_card_before_is_delivered_unless_referenced(self):
        defs = [{"id": "a", "mode": "manual", "name": "Plan (Fable)", "prompt": "x"},
                {"id": "b", "mode": "manual", "name": "Build", "needs": ["a"], "prompt": "Build it."}]
        run = self._run(defs, {"a": "THE PLAN TEXT"})
        out = self._render(defs[1], run)
        self.assertIn("FROM Plan (Fable)\nTHE PLAN TEXT", out)
        self.assertNotIn("THE ASK", out)       # a later card gets the ask only by placing it
        placed = {**defs[1], "prompt": "Build this: {{stage.a.output}}"}
        out = self._render(placed, run)
        self.assertIn("Build this: THE PLAN TEXT", out)
        self.assertNotIn("FROM Plan", out)

    def test_gates_judges_and_continued_conversations_get_nothing(self):
        defs = [{"id": "a", "mode": "manual", "name": "A", "prompt": "x"},
                {"id": "g", "mode": "logic", "name": "G", "needs": ["a"]},
                {"id": "j", "mode": "judge", "name": "J", "needs": ["a"]},
                {"id": "c", "mode": "manual", "name": "C", "needs": ["a"],
                 "continues": "a", "prompt": "Go on."},
                {"id": "after", "mode": "manual", "name": "After", "needs": ["g", "j"],
                 "prompt": "Next."}]
        run = self._run(defs, {"a": "OUT A", "g": "pass", "j": "Grade: B"})
        self.assertNotIn("FROM", self._render(defs[3], run))
        # a card fed only by a gate and a judge receives neither
        out = self._render(defs[4], run)
        self.assertNotIn("FROM", out)
        self.assertNotIn("THE ASK", out)

    def test_an_explicit_no_keeps_the_ask_away(self):
        probe = {"id": "p", "mode": "manual", "name": "Probe", "prompt": "Count.",
                 "forge": {"input": False}}
        self.assertNotIn("THE ASK", self._render(probe, self._run([probe], {})))
        wired = {"id": "w", "mode": "manual", "name": "W", "needs": ["p"], "prompt": "Then.",
                 "forge": {"input": True}}
        out = self._render(wired, self._run([probe, wired], {"p": "1 2 3"}))
        self.assertIn("THE ASK\nthe ask", out)
        self.assertIn("FROM Probe\n1 2 3", out)


class ReseedTests(unittest.TestCase):
    """A changed starter reaches an install that already holds the old
    text, unless the owner saved that starter as their own."""

    def test_untouched_builtins_refresh_and_edited_ones_stay(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        defs = Path(tmp.name) / "circuits.json"
        import json
        stale = {"id": "plan", "name": "Plan it", "builtin": True,
                 "stages": [{"id": "plan", "name": "Plan", "mode": "manual",
                             "needs": [], "prompt": "OLD {{input}}"}]}
        edited = {"id": "council", "name": "Mine", "builtin": True,
                  "updated": "2026-10-01T00:00:00+00:00",
                  "stages": [{"id": "x", "name": "X", "mode": "manual",
                              "needs": [], "prompt": "owner's own"}]}
        defs.write_text(json.dumps({"circuits": [stale, edited],
                                    "plan_outputs_seeded": True}), encoding="utf-8")
        with mock.patch.object(circuits, "DEFS", defs):
            by_id = {c["id"]: c for c in circuits.list_circuits()}
            self.assertNotIn("OLD", by_id["plan"]["stages"][0]["prompt"])
            self.assertEqual(by_id["council"]["name"], "Mine")
            # and only once: a second load does not touch the edited one
            self.assertEqual(json.loads(defs.read_text(encoding="utf-8"))["templates_text_version"],
                             circuits.TEMPLATE_TEXT_VERSION)


class IdeaToFlowTests(unittest.TestCase):
    """"Build in Flows" - the third answer to a queued idea, between
    dispatching it blind and handing it to another session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for patch in [mock.patch.object(circuits, "DEFS",
                                        root / "circuits.json"),
                      mock.patch.object(ideas, "STORE", root / "ideas.json")]:
            patch.start()
            self.addCleanup(patch.stop)
        self.idea = ideas.add("Rebuild the renewal radar", source="test")

    def test_it_copies_the_starter_into_a_flow_named_for_the_idea(self):
        out = flows.flow_for_idea(self.idea["id"])
        circ = circuits.get_circuit(out["flow_id"])
        starter = circuits.get_circuit("plan-build-judge")
        self.assertEqual(circ["name"], "Rebuild the renewal radar")
        self.assertEqual([s["id"] for s in circ["stages"]],
                         [s["id"] for s in starter["stages"]])
        # A copy, not the starter itself - editing it must not rewrite the
        # workflow every other idea starts from.
        self.assertNotEqual(out["flow_id"], "plan-build-judge")
        self.assertFalse(circ["builtin"])

    def test_the_idea_is_the_run_input_not_baked_into_the_graph(self):
        out = flows.flow_for_idea(self.idea["id"])
        self.assertEqual(out["input"], "Rebuild the renewal radar")
        prompts = " ".join(s.get("prompt") or ""
                           for s in circuits.get_circuit(out["flow_id"])["stages"])
        self.assertNotIn("renewal radar", prompts)
        self.assertIn("{{input}}", prompts)

    def test_a_second_click_makes_a_second_copy(self):
        """Two attempts at one idea are two experiments; reusing the first
        would silently discard whatever the owner had changed."""
        first = flows.flow_for_idea(self.idea["id"])
        second = flows.flow_for_idea(self.idea["id"])
        self.assertNotEqual(first["flow_id"], second["flow_id"])

    def test_a_long_idea_is_truncated_into_a_readable_name(self):
        long_idea = ideas.add("x" * 200, source="test")
        out = flows.flow_for_idea(long_idea["id"])
        self.assertEqual(len(out["name"]), 61)
        self.assertTrue(out["name"].endswith("…"))

    def test_unknown_idea_and_unknown_starter_are_named_refusals(self):
        with self.assertRaises(KeyError):
            flows.flow_for_idea("idea_nope")
        with self.assertRaises(ValueError):
            flows.flow_for_idea(self.idea["id"], template="no-such-starter")

    def test_the_run_carries_the_idea_so_it_closes_out(self):
        out = flows.flow_for_idea(self.idea["id"])
        seen = {}

        def fake_start(cid, text, **kw):
            seen.update(cid=cid, text=text, **kw)
            return {"id": "run_1"}

        with mock.patch.object(circuits, "start_run", fake_start):
            flows.run_flow(out["flow_id"], out["input"],
                           idea_id=self.idea["id"])
        self.assertEqual(seen["idea_id"], self.idea["id"])
        self.assertEqual(seen["source"], "forge")


if __name__ == "__main__":
    unittest.main()

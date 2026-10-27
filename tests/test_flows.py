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

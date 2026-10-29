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
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from server import actions, circuits, flows, ideas, notify, routinerun, routines


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
        # born one column left of the first stage, on top of nothing
        card = next(n for n in flow["nodes"] if n["type"] == "input")
        self.assertEqual((card["x"], card["y"]), (flows.LEFT_COLUMN_X, 130))
        self.assertTrue(all(n["x"] >= 150 for n in flow["nodes"] if n["type"] == "agent"))
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

    def _mac(self):
        """A pretend Mac: a library with a global CLAUDE.md, a settings file
        with a secret, one plugin; a working folder with AGENTS.md, its
        private AGENTS.local.md, and an MCP server in .mcp.json."""
        root = Path(self.tmp.name)
        library = flows.LIBRARY
        library.mkdir(parents=True, exist_ok=True)
        (library / "CLAUDE.md").write_text("# Global\n\nRun it yourself.", encoding="utf-8")
        (library / "settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls *)", "Read"], "deny": ["Bash(rm *)"]},
            "hooks": {"Stop": [{"matcher": "", "hooks": []}]},
            "env": {"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "4"},
            "enabledPlugins": {"adhd@adhd": True},
            "apiToken": "hunter2", "cleanupPeriodDays": 30}), encoding="utf-8")
        plugin = library / "plugins" / "cache" / "market" / "adhd" / "0.4.1"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": "adhd", "version": "0.4.1", "description": "Shape the output"}), encoding="utf-8")
        for skill in ("focus", "recap"):
            (plugin / "skills" / skill).mkdir(parents=True)
            (plugin / "skills" / skill / "SKILL.md").write_text(f"# {skill}", encoding="utf-8")
        (plugin / "hooks").mkdir()
        (plugin / "hooks" / "hooks.json").write_text(json.dumps(
            {"hooks": {"SessionStart": [{"hooks": []}]}}), encoding="utf-8")
        (plugin / "README.md").write_text("# adhd\n\nRead me.", encoding="utf-8")
        project = root / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("# Start here\n\nRead `AGENTS.local.md`.", encoding="utf-8")
        (project / "AGENTS.local.md").write_text("# Private\n\nThe owner's file.", encoding="utf-8")
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"notes": {
            "command": "npx", "args": ["notes-mcp"], "env": {"NOTES_TOKEN": "secret"}}}}), encoding="utf-8")
        (root / "claude.json").write_text(json.dumps({"mcpServers": {"vault": {
            "type": "http", "url": "http://localhost:9/mcp"}}}), encoding="utf-8")
        for patch in (mock.patch.object(flows, "ROOT", project),
                      mock.patch.object(flows, "CLAUDE_JSON", root / "claude.json")):
            patch.start()
            self.addCleanup(patch.stop)
        return library, plugin, project

    def test_the_library_lists_this_macs_instruction_files_plugins_and_mcp_servers(self):
        library, plugin, project = self._mac()
        catalog = {item["id"]: item for item in flows.kit_catalog()}
        self.assertEqual(catalog["instructions:project"]["description"], "AGENTS.md of the working folder")
        self.assertEqual(catalog["instructions:user"]["source_ref"], str(library / "CLAUDE.md"))
        self.assertEqual(catalog["instructions:project-local"]["source_ref"], str(project / "AGENTS.local.md"))
        self.assertEqual(catalog["plugin:adhd"]["description"], "Shape the output")
        self.assertEqual(catalog["plugin:adhd"]["items"]["skills"], ["focus", "recap"])
        self.assertEqual(catalog["plugin:adhd"]["items"]["hooks"], ["SessionStart"])
        self.assertEqual(sorted(k for k in catalog if k.startswith("mcp:")), ["mcp:notes", "mcp:vault"])
        self.assertEqual(catalog["mcp:notes"]["description"], "npx notes-mcp")
        # the secret in the server's env block never leaves the config file
        self.assertNotIn("secret", json.dumps(catalog))

    def test_a_preview_is_the_file_an_outline_or_a_listing(self):
        library, plugin, project = self._mac()
        shown = flows.kit_preview("project", "instructions")
        self.assertEqual((shown["format"], shown["path"]), ("markdown", str(project / "AGENTS.md")))
        self.assertIn("Start here", shown["text"])
        shown = flows.kit_preview("user", "settings")
        self.assertEqual(shown["format"], "json")
        outline = {row["key"]: row["summary"] for row in shown["outline"]}
        self.assertEqual(outline["permissions"], "2 allow, 1 deny rules")
        self.assertEqual(outline["hooks"], "Stop 1")
        self.assertEqual(outline["env"], "1 variable: CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS")
        self.assertEqual(outline["enabledPlugins"], "1 entry: adhd@adhd")
        self.assertEqual(outline["apiToken"], "set")
        self.assertEqual(outline["cleanupPeriodDays"], "30")
        shown = flows.kit_preview(str(plugin), "plugin")
        self.assertEqual(shown["format"], "folder")
        self.assertEqual(shown["listing"]["skills"], ["focus", "recap"])
        self.assertEqual(shown["listing"]["name"], "adhd")
        self.assertIn("Read me", shown["text"])
        self.assertIn("skills/focus/SKILL.md", shown["files"])
        shown = flows.kit_preview("notes", "mcp")
        self.assertEqual(shown["server"], {"name": "notes", "scope": "project", "type": "stdio",
                                           "command": "npx notes-mcp", "url": "",
                                           "env": ["NOTES_TOKEN"]})
        self.assertFalse(flows.kit_preview("gone", "mcp")["exists"])
        self.assertFalse(flows.kit_preview("local", "settings")["exists"])

    def test_a_preview_over_the_network_stays_in_the_library_and_the_working_folder(self):
        library, plugin, project = self._mac()
        outside = Path(self.tmp.name) / "elsewhere.md"
        outside.write_text("private", encoding="utf-8")
        with self.assertRaises(ValueError):
            flows.kit_preview(str(outside), "instructions")
        self.assertEqual(flows.kit_preview(str(outside), "instructions", local=True)["text"], "private")
        self.assertTrue(flows.kit_preview(str(project / "AGENTS.local.md"), "instructions")["exists"])
        self.assertTrue(flows.kit_preview(str(library / "CLAUDE.md"), "instructions")["exists"])

    def test_a_plugin_card_brings_its_skills_unless_switched_off(self):
        library, plugin, project = self._mac()
        payload = {"name": "Plug", "nodes": [
            {"id": "adhd", "type": "tool", "name": "adhd", "source": "plugin",
             "source_ref": str(plugin), "skills_off": ["recap"]},
            {"id": "mine", "type": "tool", "name": "Your Claude settings", "source": "settings",
             "source_ref": "/vault/mine.json", "stock_ref": "user"},
            {"id": "bogus", "type": "tool", "name": "Bogus", "source": "settings",
             "source_ref": "nope"},
            _agent("a", "A")],
            "edges": [{"from": "adhd", "to": "a"}, {"from": "mine", "to": "a"},
                      {"from": "bogus", "to": "a"}]}
        saved = flows.save_flow(payload, save_as=True)
        env = self._stages(saved["id"])["a"]["forge"]["environment"]
        self.assertEqual(env["plugins"], [str(plugin)])
        self.assertEqual(env["skills"], ["adhd:focus"])
        self.assertEqual(env["settings"], ["/vault/mine.json"])
        card = next(n for n in saved["nodes"] if n["id"] == "adhd")
        self.assertEqual(card["skills_off"], ["recap"])
        mine = next(n for n in saved["nodes"] if n["id"] == "mine")
        self.assertEqual(mine["stock_ref"], "user")

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


class LoopCardTests(unittest.TestCase):
    """A Loop card over a command loop: the Flow starts the program, waits
    for the runner to record an end, and the wire carries the run in words.
    Until 2026-10-29 the card finished the moment the program started, so
    the card after it ran on a line of JSON about a launch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patches = [
            mock.patch.object(flows, "STORE", root / "flow-graphs.json"),
            mock.patch.object(circuits, "DEFS", root / "circuits.json"),
            mock.patch.object(circuits, "RUNS", root / "circuit-runs.json"),
            mock.patch.object(routines, "STORE", root / "routines.json"),
            mock.patch.object(routines, "SEEDS", []),
            mock.patch.object(routinerun, "STORE", root / "routine-runs.json"),
            mock.patch.object(routinerun, "RUNS", root / "routine-runs"),
            mock.patch.object(routinerun, "_spawn",
                              return_value=types.SimpleNamespace(pid=4242)),
            mock.patch.dict(os.environ, {"VIRA_RUNTIME_ROOT": str(root / "runtime")}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("VIRA_INSTANCE_ID", None)
        self.loop = routines.save_routine({
            "name": "Demo capture", "kind": "command", "every_hours": 24,
            "command": ["/bin/echo", "hi"], "cwd": str(root)})
        circuits.save_circuit({"id": "capture", "name": "Capture", "stages": [
            {"id": "loop", "name": "Demo capture", "mode": "native", "needs": [],
             "native": {"routine_id": self.loop["id"]}},
            {"id": "record", "name": "Record", "mode": "output",
             "needs": ["loop"], "output": {"destination": "record"}},
        ]})
        self.driver = circuits.Driver.__new__(circuits.Driver)

    def _advance(self, run_id):
        self.driver._advance(circuits.get_run(run_id))
        return circuits.get_run(run_id)

    def _finish(self, run_id, **fields):
        out = routinerun.run_dir(run_id) / "output.log"
        out.write_text("[demo] start\ndone: 5 captured, 1 failed\n", encoding="utf-8")
        routinerun._update(run_id, finished="2026-10-29T03:00:00+00:00", **fields)

    def test_the_flow_waits_for_the_program_then_the_wire_carries_its_result(self):
        run = circuits.start_run("capture", "tonight")
        self.driver._advance(run)
        stage = circuits.get_run(run["id"])["stages"]["loop"]
        self.assertEqual(stage["status"], "running")
        self.assertEqual(routinerun.get(stage["command_run"])["status"], "starting")
        # the program is still going: nothing moves
        after = self._advance(run["id"])
        self.assertEqual(after["stages"]["loop"]["status"], "running")
        self.assertEqual(after["stages"]["record"]["status"], "pending")
        # the runner records an end
        self._finish(stage["command_run"], status="ok", exit_code=0,
                     counts={"captured": 5, "failed": 1},
                     errors=["FAILED abc: timed out"], error_count=1)
        self._advance(run["id"])
        final = self._advance(run["id"])
        self.assertEqual(final["status"], "done")
        text = final["stages"]["record"]["result_text"]
        self.assertIn("PROGRAM Demo capture", text)
        self.assertIn("5 captured, 1 failed", text)
        self.assertIn("FAILED abc: timed out", text)
        self.assertIn("done: 5 captured, 1 failed", text)

    def test_a_failed_program_fails_the_card_and_skips_the_card_after(self):
        run = circuits.start_run("capture", "tonight")
        self.driver._advance(run)
        stage = circuits.get_run(run["id"])["stages"]["loop"]
        self._finish(stage["command_run"], status="failed", exit_code=1)
        self._advance(run["id"])
        final = self._advance(run["id"])
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["stages"]["loop"]["status"], "error")
        self.assertEqual(final["stages"]["record"]["status"], "skipped")
        self.assertIn("failed (exit 1)", final["stages"]["loop"]["result_text"])

    def test_a_program_already_going_is_waited_for_not_started_again(self):
        first = routinerun.start(self.loop, trigger="schedule")
        with mock.patch.object(routinerun, "held", return_value=True):
            run = circuits.start_run("capture", "tonight")
            self.driver._advance(run)
        stage = circuits.get_run(run["id"])["stages"]["loop"]
        self.assertEqual(stage["status"], "running")
        self.assertEqual(stage["command_run"], first["id"])
        self.assertIn("Waiting for", stage["result_text"])
        self.assertEqual(routinerun.runs(self.loop["id"])["total"], 1)

    def test_an_output_set_to_a_notification_reaches_the_owner(self):
        circuits.save_circuit({"id": "tell", "name": "Tell", "stages": [
            {"id": "gate", "name": "Always", "mode": "logic", "needs": [],
             "logic": {"operation": "always", "value": ""}},
            {"id": "phone", "name": "Tell me", "mode": "output", "needs": ["gate"],
             "output": {"destination": "notification",
                        "instructions": "Lead with the count."}},
        ]})
        with mock.patch.object(notify, "agent_ping", return_value=True) as ping:
            run = circuits.start_run("tell", "the ask")
            self.driver._advance(run)
            self.driver._advance(circuits.get_run(run["id"]))
        self.assertEqual(circuits.get_run(run["id"])["status"], "done")
        ping.assert_called_once()
        text = ping.call_args[0][0]
        self.assertTrue(text.startswith("Tell: "), text)
        self.assertIn("Logic gate always: passed", text)
        self.assertNotIn("Output instructions", text)


class ProgramCardTests(unittest.TestCase):
    """A Program card runs a command as its own process: the card waits for
    the runner to record an end and the wire carries the run in words. A
    Schedule runs only the cards it is wired to and what is downstream of
    them, so two Schedules on one board run two chains (2026-10-29)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = Path(self.tmp.name)
        patches = [
            mock.patch.object(flows, "STORE", root / "flow-graphs.json"),
            mock.patch.object(circuits, "DEFS", root / "circuits.json"),
            mock.patch.object(circuits, "RUNS", root / "circuit-runs.json"),
            mock.patch.object(routines, "STORE", root / "routines.json"),
            mock.patch.object(routines, "SEEDS", []),
            mock.patch.object(routinerun, "STORE", root / "routine-runs.json"),
            mock.patch.object(routinerun, "RUNS", root / "routine-runs"),
            mock.patch.object(routinerun, "_spawn",
                              return_value=types.SimpleNamespace(pid=4242)),
            mock.patch.dict(os.environ, {"VIRA_RUNTIME_ROOT": str(root / "runtime")}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("VIRA_INSTANCE_ID", None)
        self.driver = circuits.Driver.__new__(circuits.Driver)

    def board(self, **extra):
        """Two chains on one board: hourly Schedule -> Program, nightly
        Schedule -> Report agent -> Output."""
        folder = str(self.root)
        payload = {
            "name": "Notes capture",
            "nodes": [
                {"id": "input", "type": "input", "prompt": "Report the day"},
                {"id": "hourly", "type": "trigger", "every_hours": 1, "enabled": True},
                {"id": "nightly", "type": "trigger", "daily_at": "20:45", "enabled": True},
                {"id": "export", "type": "program", "name": "Export notes",
                 "program": {"command_line": f"/bin/bash {folder}/notes.sh --hourly",
                             "cwd": folder, "env": "PATH=/usr/bin:/bin"}},
                {"id": "report", "type": "agent", "name": "Report", "prompt": "Say what landed."},
                {"id": "out", "type": "output", "name": "Text me",
                 "output": {"destination": "record"}},
            ],
            "edges": [
                {"from": "hourly", "to": "export"},
                {"from": "nightly", "to": "report"},
                {"from": "input", "to": "report"},
                {"from": "report", "to": "out"},
            ],
        }
        payload.update(extra)
        return flows.save_flow(payload, save_as=True)

    def schedules(self, flow):
        rows = {r["id"]: r for r in routines.list_routines()}
        return {n["routine_id"]: rows[n["routine_id"]] for n in flow["nodes"] if n["type"] == "trigger"}

    def test_the_card_compiles_to_a_program_stage_and_keeps_its_run_key(self):
        flow = self.board()
        stage = next(s for s in circuits.get_circuit(flow["id"])["stages"] if s["id"] == "export")
        self.assertEqual(stage["mode"], "program")
        self.assertEqual(stage["program"]["command"],
                         ["/bin/bash", f"{self.root}/notes.sh", "--hourly"])
        self.assertEqual(stage["program"]["env"], {"PATH": "/usr/bin:/bin"})
        key = stage["program"]["run_key"]
        self.assertRegex(key, r"^prog_[0-9a-f]{10}$")
        node = next(n for n in flow["nodes"] if n["id"] == "export")
        self.assertEqual(node["type"], "program")
        self.assertEqual(node["program"]["command_line"], f"/bin/bash {self.root}/notes.sh --hourly")
        # saved again from the board, the card keeps its lock and history
        again = flows.save_flow({**flow, "id": flow["id"]})
        stage2 = next(s for s in circuits.get_circuit(again["id"])["stages"] if s["id"] == "export")
        self.assertEqual(stage2["program"]["run_key"], key)

    def test_a_relative_folder_is_refused(self):
        with self.assertRaisesRegex(ValueError, "absolute path"):
            flows.save_flow({"name": "Bad", "nodes": [
                {"id": "p", "type": "program", "program": {"command_line": "echo hi", "cwd": "vault"}}],
                "edges": []}, save_as=True)

    def test_each_schedule_keeps_the_cards_it_is_wired_to(self):
        flow = self.board()
        rows = self.schedules(flow)
        targets = sorted(tuple(r["targets"]) for r in rows.values())
        self.assertEqual(targets, [("export",), ("report",)])
        # the board draws each Schedule's own wires, not a wire to every entry card
        wires = {(e["from"], e["to"]) for e in flow["edges"] if e["from"].startswith("routine:")}
        self.assertEqual(len(wires), 2)
        self.assertEqual({to for _, to in wires}, {"export", "report"})

    def test_a_schedule_runs_only_its_own_chain(self):
        flow = self.board()
        hourly = next(r for r in self.schedules(flow).values() if r.get("every_hours"))
        nightly = next(r for r in self.schedules(flow).values() if r.get("daily_at"))
        with mock.patch.object(routines, "_ai_ready", return_value=True):
            first = routines.dispatch(hourly, trigger="schedule")
            second = routines.dispatch(nightly, trigger="schedule")
        hour_run = circuits.get_run(first["run_id"])
        night_run = circuits.get_run(second["run_id"])
        self.assertEqual(set(hour_run["stages"]), {"export"})
        self.assertEqual(set(night_run["stages"]), {"report", "out"})
        self.assertEqual(hour_run["entry"], ["export"])

    def test_the_card_waits_for_its_program_then_the_wire_carries_the_result(self):
        flow = self.board(edges=[{"from": "export", "to": "out"}])
        # only the Program card's chain: the board's report card is a model
        # session, which a unit test never launches
        run = circuits.start_run(flow["id"], "now", entry=["export"])
        self.assertEqual(set(run["stages"]), {"export", "out"})
        self.driver._advance(run)
        stage = circuits.get_run(run["id"])["stages"]["export"]
        self.assertEqual(stage["status"], "running")
        rec = routinerun.get(stage["command_run"])
        key = next(s for s in circuits.get_circuit(flow["id"])["stages"]
                   if s["id"] == "export")["program"]["run_key"]
        self.assertEqual(rec["routine_id"], key)
        self.assertEqual(rec["trigger"], "manual")
        out = routinerun.run_dir(rec["id"]) / "output.log"
        out.write_text("[notes] start\ndone: 4 exported, 0 failed\n", encoding="utf-8")
        routinerun._update(rec["id"], status="ok", exit_code=0,
                           finished="2026-10-29T03:00:00+00:00",
                           counts={"exported": 4, "failed": 0})
        self.driver._advance(circuits.get_run(run["id"]))
        self.driver._advance(circuits.get_run(run["id"]))
        final = circuits.get_run(run["id"])
        self.assertEqual(final["stages"]["export"]["status"], "done")
        self.assertIn("PROGRAM Export notes", final["stages"]["out"]["result_text"])
        self.assertIn("done: 4 exported, 0 failed", final["stages"]["out"]["result_text"])

    def test_a_program_already_going_is_waited_for(self):
        flow = self.board()
        stage = next(s for s in circuits.get_circuit(flow["id"])["stages"] if s["id"] == "export")
        first = routinerun.start(circuits.program_spec(stage), trigger="schedule")
        with mock.patch.object(routinerun, "held", return_value=True):
            run = circuits.start_run(flow["id"], "now", entry=["export"])
            self.driver._advance(run)
        st = circuits.get_run(run["id"])["stages"]["export"]
        self.assertEqual(st["command_run"], first["id"])
        self.assertIn("Waiting for", st["result_text"])

    def test_a_schedule_wired_to_nothing_is_skipped_and_stamped(self):
        flow = self.board(edges=[{"from": "input", "to": "report"}, {"from": "report", "to": "out"}])
        row = next(iter(self.schedules(flow).values()))
        self.assertEqual(row["targets"], [])
        result = routines.dispatch(row, trigger="schedule")
        self.assertEqual(result, {"skipped": "no_targets"})
        self.assertIn("wired to no card", routines.get_routine(row["id"])["last_status"])
        self.assertEqual(circuits.list_runs(10), [])

    def test_run_now_on_a_schedule_runs_its_chain(self):
        flow = self.board()
        nightly = next(r for r in self.schedules(flow).values() if r.get("daily_at"))
        run = flows.run_flow(flow["id"], "", trigger=nightly["id"])
        self.assertEqual(set(run["stages"]), {"report", "out"})
        with self.assertRaises(KeyError):
            flows.run_flow(flow["id"], "", trigger="rt_nope")

    def test_archive_stops_every_schedule_and_restore_puts_them_back(self):
        flow = self.board()
        rows = self.schedules(flow)
        off = next(iter(rows.values()))
        routines.save_routine({"enabled": False}, rid=off["id"])
        archived = flows.archive_flow(flow["id"])
        self.assertTrue(archived["archived"])
        self.assertTrue(all(not routines.get_routine(r["id"])["enabled"] for r in rows.values()))
        # saving an archived board keeps it archived
        again = flows.save_flow({**archived, "id": flow["id"]})
        self.assertTrue(again["archived"])
        restored = flows.restore_flow(flow["id"])
        self.assertIsNone(restored["archived"])
        states = {r["id"]: routines.get_routine(r["id"])["enabled"] for r in rows.values()}
        self.assertFalse(states[off["id"]])
        self.assertEqual(sum(states.values()), 1)

    def test_an_archived_routine_is_never_due(self):
        row = routines.save_routine({"name": "Sweep", "kind": "custom",
                                     "prompt": "x", "every_hours": 1, "enabled": True})
        self.assertTrue(routines.is_due(row))
        archived = routines.archive_routine(row["id"])
        self.assertFalse(routines.is_due(archived))
        self.assertTrue(routines.restore_routine(row["id"])["enabled"])

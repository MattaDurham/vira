"""The Forge's product model over Vira's existing orchestration engines.

A Flow is the stable object the owner edits in the Forge. Existing circuits
remain the execution authority and existing routines remain the scheduling
authority; this module supplies the visual graph, version metadata, Kit
catalog, and the lossless adapters between those two proven stores.

The graph sidecar is deliberately not a second circuit store. Prompts, models,
permissions, dependencies, run state, and cadence continue to live in their
existing canonical stores. The sidecar owns only visual/editor information
that those stores cannot represent: card placement, the source cards (a
skill, a script, a file) wired into a stage, wire notes, and revision
numbers.

A card is one of eleven kinds. Six run: agent, judge, logic (a gate),
approval (ask me), output, native (a Vira routine). Five do not: input
(the ask, one per board), trigger (a schedule), tool and context
(sources: a thing with a name and a path that a card is told about when
it is wired in) and env (a saved set of sources as one card). A wire means
the finished text of the card before goes into the card after, delivered
by circuits._delivered unless the box places it; it may carry a note. A
card's environment is what is wired into it and nothing else (the skills,
settings, plugins, agent files and instruction files; see ENV_KEYS), with
the project's instruction file on by default. There are no port names,
payload kinds, connectors, or nested systems: the compiler never read
them, so the editor no longer draws them (2026-10-27, 10-28).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import actions, agentbackend, circuits, jsonstore, models, routines

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "flow-graphs.json"
# Saved environments: a named set of sources (skills, settings, plugins,
# instruction files) the owner wires into a card as one Environment card.
ENVS = ROOT / "data" / "forge-environments.json"
LIBRARY = Path.home() / ".claude"
MAX_ENV_ITEMS = 40

MAX_NODES = 80
MAX_EDGES = 240
MAX_CONTEXTS = 80
MAX_TEXT = 24_000
MAX_SOURCE = 250_000
SOURCE_SUFFIXES = {".md", ".txt", ".py", ".sh", ".zsh", ".js", ".ts",
                   ".json", ".yaml", ".yml", ".toml", ".html", ".css"}
AGENT_NODE_TYPES = {"agent", "judge"}
EXECUTABLE_NODE_TYPES = AGENT_NODE_TYPES | {"logic", "approval", "output", "native"}
SOURCE_NODE_TYPES = {"tool", "context"}
# input: the ask, one card per board (what Run sends). env: a saved set of
# sources wired in as one card.
DISPLAY_NODE_TYPES = EXECUTABLE_NODE_TYPES | SOURCE_NODE_TYPES | {"trigger", "input", "env"}
INPUT_ID = "input"
# Which source kinds become part of a card's environment, and under which
# key (runner._environment_options reads these). Every other kind (a
# script, a file, a Find reference) reaches the card as a text line only.
ENV_KEYS = {"skill": "skills", "plugin": "plugins", "agent": "agents",
            "command": "commands", "mcp": "mcp", "hook": "hooks",
            "settings": "settings", "instructions": "instructions"}
# What a source card of each kind contributes: its name, or its path.
ENV_BY_REF = {"plugin", "agent", "settings", "instructions"}
SETTING_SOURCES = ("user", "project", "local")
# The judge's grade and retry knobs the card may set; anything else on a
# judge dict is dropped at compile time.
JUDGE_KEYS = ("of", "min_grade", "max_retries", "retry_stage")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blank():
    return {"graphs": {}}


def _store():
    value = jsonstore.read(STORE, _blank())
    if not isinstance(value, dict) or not isinstance(value.get("graphs"), dict):
        return _blank()
    return value


def _layout(stages):
    """Deterministic columns from dependency depth, rows by source order."""
    by_id = {stage["id"]: stage for stage in stages}
    depth = {}

    def visit(sid, seen=None):
        if sid in depth:
            return depth[sid]
        seen = set(seen or ())
        if sid in seen:
            return 0
        seen.add(sid)
        needs = [n for n in by_id[sid].get("needs") or [] if n in by_id]
        depth[sid] = 0 if not needs else max(visit(n, seen) for n in needs) + 1
        return depth[sid]

    for sid in by_id:
        visit(sid)
    rows = {}
    out = {}
    for stage in stages:
        col = depth.get(stage["id"], 0)
        row = rows.get(col, 0)
        rows[col] = row + 1
        out[stage["id"]] = {"x": 150 + col * 310, "y": 130 + row * 180}
    return out


def _node(stage, placed):
    mode = circuits.norm_stage_mode(stage.get("mode"))
    node_type = mode if mode in {"judge", "logic", "approval", "output", "native"} else "agent"
    data = {
        "id": stage["id"],
        "type": node_type,
        "name": stage.get("name") or stage["id"],
        "x": int(placed.get("x", 0)),
        "y": int(placed.get("y", 0)),
        "model": stage.get("model") or "",
        "provider": stage.get("provider") or "",
        "mode": mode,
        "read_only": bool(stage.get("read_only")) or node_type == "judge",
        # A judge's box is what the owner asked it to weigh (stage.extra);
        # every other card's box is its prompt.
        "prompt": (stage.get("extra") if node_type == "judge"
                   else stage.get("prompt")) or "",
        "judge": {k: stage["judge"][k] for k in JUDGE_KEYS
                  if k in (stage.get("judge") or {})},
        "logic": dict(stage.get("logic") or {}),
        "approval": dict(stage.get("approval") or {}),
        "output": dict(stage.get("output") or {}),
        "native": dict(stage.get("native") or {}),
        "forge": dict(stage.get("forge") or {}),
        # The card's own environment row: the project's instruction file
        # (on by default), or a custom file, or none. Skills, settings and
        # plugins are cards wired in, never a field here.
        "environment": {"instructions": list(
            ((stage.get("forge") or {}).get("environment") or {}).get("instructions")
            or (["project"] if "environment" not in (stage.get("forge") or {}) else []))},
        "source": "circuit-stage",
    }
    if node_type == "agent":
        # The budget and continuation knobs belong to agent parts alone; a
        # gate or a judge carrying "on_timeout: error" would be noise.
        data["timeout_s"] = int(stage.get("timeout_s") or 0)
        data["on_timeout"] = stage.get("on_timeout") or "error"
        data["continues"] = stage.get("continues") or ""
    return data


def _edge(source, target, meta=None):
    meta = meta or {}
    return {
        "id": meta.get("id") or f"edge:{source}:{target}",
        "from": source,
        "to": target,
        "instructions": str(meta.get("instructions") or "")[:2000],
    }


def _graph_for_circuit(circuit, rows, saved):
    stages = list(circuit.get("stages") or [])
    defaults = _layout(stages)
    saved_nodes = {n.get("id"): n for n in saved.get("nodes") or []
                   if isinstance(n, dict) and n.get("id")}
    nodes = [_node(stage, {**defaults.get(stage["id"], {}),
                           **saved_nodes.get(stage["id"], {})})
             for stage in stages]
    stage_ids = {stage["id"] for stage in stages}
    for stored in saved_nodes.values():
        # Source cards and manual schedules live only in the sidecar. A
        # schedule WITH a cadence is a routine (saved under routine:<id>
        # below), so a stored copy of one is placement, never a card.
        if (stored.get("id") in stage_ids
                or str(stored.get("id") or "").startswith("routine:")
                or stored.get("type") not in DISPLAY_NODE_TYPES
                or stored.get("type") in EXECUTABLE_NODE_TYPES
                or stored.get("type") == "input"):   # built by _attach_input
            continue
        nodes.append(dict(stored))
    edge_meta = {(e.get("from"), e.get("to")): e
                 for e in saved.get("edges") or [] if isinstance(e, dict)}
    edges = []
    for stage in stages:
        for need in stage.get("needs") or []:
            edges.append(_edge(need, stage["id"], edge_meta.get((need, stage["id"]))))
    node_ids = {node["id"] for node in nodes}
    for saved_edge in saved.get("edges") or []:
        if not isinstance(saved_edge, dict):
            continue
        pair = (saved_edge.get("from"), saved_edge.get("to"))
        if pair in {(edge["from"], edge["to"]) for edge in edges} or pair[0] == INPUT_ID:
            continue       # the Input card's wires come from forge.input
        if pair[0] in node_ids and pair[1] in node_ids:
            edges.append(_edge(pair[0], pair[1], saved_edge))
    _attach_input(stages, nodes, edges, saved_nodes, edge_meta)
    if not saved_nodes:
        _attach_environment(stages, nodes, edges)
    triggers = [dict(r) for r in rows if r.get("kind") == "circuit"
                and r.get("circuit_id") == circuit["id"]]
    entry_ids = [stage["id"] for stage in stages if not stage.get("needs")]
    for index, trigger in enumerate(triggers):
        trigger_id = f"routine:{trigger['id']}"
        placed = saved_nodes.get(trigger_id) or {}
        nodes.append({
            "id": trigger_id, "type": "trigger",
            "name": trigger.get("name") or "Schedule",
            "x": int(placed.get("x", 25)),
            "y": int(placed.get("y", 120 + index * 170)),
            "source": "routine", "routine_id": trigger["id"],
            "daily_at": trigger.get("daily_at") or "",
            "every_hours": trigger.get("every_hours"),
            "enabled": bool(trigger.get("enabled")),
            "notify": bool(trigger.get("notify")),
        })
        for entry_id in entry_ids:
            edges.append(_edge(trigger_id, entry_id,
                               edge_meta.get((trigger_id, entry_id))))
    return {
        "id": circuit["id"],
        "name": circuit.get("name") or circuit["id"],
        "description": circuit.get("description") or "",
        "kind": "flow",
        "builtin": bool(circuit.get("builtin")),
        "revision": max(1, int(saved.get("revision") or 1)),
        "created": circuit.get("created"),
        "updated": circuit.get("updated"),
        "nodes": nodes,
        "edges": edges,
        "contexts": list(saved.get("contexts") or [])[:MAX_CONTEXTS],
        "triggers": triggers,
        "executable": True,
    }


def _attach_input(stages, nodes, edges, saved_nodes, edge_meta):
    """The Input card: one per board, holding the ask. Its text and place
    live in the sidecar; it is wired into every stage the Forge recorded
    it on (forge.input) and, for a definition the Forge never saved, into
    every entry stage that runs a model."""
    placed = saved_nodes.get(INPUT_ID) or {}
    nodes.append({
        "id": INPUT_ID, "type": "input", "name": str(placed.get("name") or "Input")[:200],
        "prompt": str(placed.get("prompt") or "")[:MAX_TEXT],
        "x": int(placed.get("x", 25)), "y": int(placed.get("y", 130)),
    })
    for stage in stages:
        mode = circuits.norm_stage_mode(stage.get("mode"))
        if mode in circuits.LOCAL_MODES or mode == "judge":
            continue
        forge = stage.get("forge") or {}
        wired = (bool(forge["input"]) if "input" in forge
                 else not stage.get("needs"))
        if wired:
            edges.append(_edge(INPUT_ID, stage["id"], edge_meta.get((INPUT_ID, stage["id"]))))


def _env_card(kind, value):
    """The source card a stage's recorded environment entry stands for."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")[:40] or kind
    card = {"type": "tool", "source": kind, "description": ""}
    if kind == "skill":
        path = LIBRARY / "skills" / str(value) / "SKILL.md"
        card.update(id=f"skill-{slug}", name=str(value),
                    source_ref=str(path) if path.exists() else "")
    elif kind == "command":
        path = LIBRARY / "commands" / f"{value}.md"
        card.update(id=f"command-{slug}", name=str(value),
                    source_ref=str(path) if path.exists() else "")
    elif kind == "settings":
        card.update(id=f"settings-{slug}", name=SETTINGS_NAMES.get(str(value), str(value)),
                    source_ref=str(value))
    elif kind in ("mcp", "hook"):
        card.update(id=f"{kind}-{slug}", name=str(value), source_ref=str(LIBRARY / f"{kind}-servers" / str(value)) if kind == "mcp" else str(LIBRARY / "hooks" / str(value)))
    else:
        name = Path(str(value)).name or str(value)
        card.update(id=f"{kind}-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40]}",
                    name=name, source_ref=str(value))
    return card


def _attach_environment(stages, nodes, edges):
    """A starter's stated environment as the cards it would be, wired in:
    the board shows what each card is told about. Only for a definition
    with no sidecar yet; once saved, the cards are the record."""
    cards = {}
    for stage in stages:
        env = (stage.get("forge") or {}).get("environment") or {}
        for kind, key in ENV_KEYS.items():
            if kind == "instructions":
                continue
            for value in env.get(key) or []:
                card = _env_card(kind, value)
                if card["id"] not in cards:
                    cards[card["id"]] = {**card, "x": 25, "y": 320 + 150 * len(cards)}
                edges.append(_edge(card["id"], stage["id"]))
    nodes.extend(cards.values())


SETTINGS_NAMES = {"user": "Your Claude settings", "project": "The project's settings",
                  "local": "The project's local settings"}


def list_flows():
    """Every circuit as a Flow. Standing loops (routines) are NOT Flows:
    they have their own list in the Forge, over /api/routines, where Run
    now, Pause and Under the hood live. Until 2026-10-27 each one was
    dressed as a locked two-card Flow, which is how eleven loops came to
    outnumber the Flows in a list called My Flows."""
    sidecar = _store()["graphs"]
    rows = routines.list_routines()
    return [_graph_for_circuit(c, rows, sidecar.get(c["id"]) or {})
            for c in circuits.list_circuits()]


def get_flow(flow_id):
    return next((flow for flow in list_flows() if flow["id"] == flow_id), None)


def _clean_contexts(values):
    out = []
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("title") or "Context").strip()[:200]
        out.append({
            "id": str(raw.get("id") or "ctx_" + uuid.uuid4().hex[:10])[:100],
            "name": name,
            "kind": str(raw.get("kind") or "reference")[:40],
            "ref": str(raw.get("ref") or raw.get("path") or "")[:1000],
            "note": str(raw.get("note") or "")[:2000],
        })
        if len(out) >= MAX_CONTEXTS:
            break
    return out


def _compile(payload):
    nodes = [dict(node) for node in payload.get("nodes") or []
             if isinstance(node, dict)]
    edges = [dict(edge) for edge in payload.get("edges") or []
             if isinstance(edge, dict)]
    if not nodes or len(nodes) > MAX_NODES:
        raise ValueError("a Flow needs 1-80 parts")
    if len(edges) > MAX_EDGES:
        raise ValueError("a Flow may have at most 240 connections")
    exec_nodes = [n for n in nodes if n.get("type") in EXECUTABLE_NODE_TYPES]
    if not exec_nodes:
        raise ValueError("a Flow needs at least one executable part")
    input_ids = {str(n.get("id") or "") for n in nodes if n.get("type") == "input"}
    ids = [str(n.get("id") or "").strip() for n in exec_nodes]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("executable part ids must be unique and non-empty")
    known = set(ids)
    by_id = {str(node.get("id") or ""): node for node in nodes}
    contexts = {str(item.get("id") or ""): item
                for item in _clean_contexts(payload.get("contexts"))}
    needs = {sid: [] for sid in ids}
    incoming = {sid: [] for sid in ids}
    outgoing = {sid: [] for sid in ids}
    for edge in edges:
        source, target = edge.get("from"), edge.get("to")
        if source in known and target in known and source not in needs[target]:
            needs[target].append(source)
        if target in incoming:
            incoming[target].append(edge)
        if source in outgoing:
            outgoing[source].append(edge)
    stages = []
    for node in exec_nodes:
        sid = str(node["id"])
        node_type = node.get("type")
        mode = node_type if node_type in {"judge", "logic", "approval", "output", "native"} \
            else circuits.norm_stage_mode(node.get("mode"), "manual")
        stage = {
            "id": sid,
            "name": str(node.get("name") or sid).strip()[:200],
            "model": str(node.get("model") or "").strip()[:80],
            "mode": mode,
            "needs": needs[sid],
        }
        # The provider / timeout / continuation knobs ride the node as data;
        # circuits.validate_stages is the one place that judges them.
        if node.get("provider"):
            stage["provider"] = str(node.get("provider")).strip()[:20]
        if mode not in {"judge", "logic", "approval", "output", "native"}:
            try:
                timeout_s = int(node.get("timeout_s") or 0)
            except (TypeError, ValueError):
                raise ValueError(f"part {sid}: timeout must be whole seconds")
            if timeout_s:
                stage["timeout_s"] = timeout_s
                stage["on_timeout"] = str(node.get("on_timeout")
                                          or "error").strip()[:20]
            if node.get("continues"):
                stage["continues"] = str(node.get("continues")).strip()[:80]
        if mode == "judge":
            raw = dict(node.get("judge") or {})
            judge = {k: raw[k] for k in JUDGE_KEYS if k in raw}
            judge["of"] = [str(v) for v in judge.get("of") or needs[sid]]
            judge["min_grade"] = str(judge.get("min_grade") or "")[:1]
            try:
                judge["max_retries"] = max(0, min(int(judge.get("max_retries") or 0),
                                                  circuits.MAX_RETRIES))
            except (TypeError, ValueError):
                raise ValueError(f"part {sid}: retries must be a whole number")
            # The card picks which upstream card a failing grade re-runs; an
            # empty pick means grade only. circuits.validate_stages refuses
            # a target that is not a stage.
            retry = str(judge.get("retry_stage") or "").strip()[:80]
            if retry:
                judge["retry_stage"] = retry
            else:
                judge.pop("retry_stage", None)
            stage["judge"] = judge
            # The judge's box: what to weigh in particular. The grading
            # prompt itself is built by server/judge.py at launch.
            extra = str(node.get("prompt") or node.get("extra") or "").strip()
            if extra:
                stage["extra"] = extra[:circuits.EXTRA_CAP]
        elif mode == "logic":
            logic = dict(node.get("logic") or {})
            logic["operation"] = str(logic.get("operation") or "always")[:40]
            logic["value"] = str(logic.get("value") or "")[:2000]
            if logic.get("subject"):
                logic["subject"] = str(logic["subject"]).strip()[:80]
            else:
                logic.pop("subject", None)
            stage["logic"] = logic
        elif mode == "approval":
            approval = dict(node.get("approval") or {})
            approval["instructions"] = str(
                node.get("prompt") or approval.get("instructions")
                or "Approval required")[:4000]
            stage["approval"] = approval
        elif mode == "output":
            output = dict(node.get("output") or {})
            output["destination"] = str(
                output.get("destination") or "record")[:80]
            output["instructions"] = str(
                node.get("prompt") or output.get("instructions") or "")[:4000]
            stage["output"] = output
        elif mode == "native":
            routine_id = str((node.get("native") or {}).get("routine_id")
                             or node.get("routine_id") or node.get("source_ref") or "")[:100]
            if not routine_id:
                raise ValueError(f"part {sid}: native system has no routine source")
            stage["native"] = {"routine_id": routine_id}
        else:
            prompt = str(node.get("prompt") or "").strip()[:MAX_TEXT]
            if not prompt:
                raise ValueError(f"part {sid}: instruction prompt is empty")
            stage["prompt"] = prompt
            # read / ask / edit / full on the card: read is this flag, the
            # other three are the mode. Until 2026-10-27 no control set it,
            # so every card the editor added ran read-only for good.
            stage["read_only"] = bool(node.get("read_only"))
            if node.get("extra"):
                stage["extra"] = str(node.get("extra"))[:circuits.EXTRA_CAP]
        attachments = {"contexts": [], "tools": [], "wire_instructions": []}
        if mode not in {"logic", "approval", "output", "native"}:
            # The card's environment: what its session is told about, and
            # nothing else (owner's rule, 2026-10-28). Built from the cards
            # wired in plus the card's own instruction-file row.
            attachments["environment"] = _environment(node, incoming[sid], by_id)
            if input_ids:
                attachments["input"] = any(str(edge.get("from") or "") in input_ids
                                           for edge in incoming[sid])
        for edge in incoming[sid]:
            source = by_id.get(str(edge.get("from") or "")) or {}
            source_type = source.get("type")
            instructions = str(edge.get("instructions") or "").strip()[:2000]
            if instructions:
                attachments["wire_instructions"].append(instructions)
            if source_type == "context":
                saved_context = contexts.get(str(source.get("id") or ""), {})
                attachments["contexts"].append({
                    "name": str(source.get("name") or saved_context.get("name")
                                or "Context")[:200],
                    "ref": str(source.get("source_ref") or saved_context.get("ref")
                               or "")[:1000],
                    "note": str(source.get("description") or saved_context.get("note")
                                or "")[:2000],
                })
            elif source_type == "tool":
                attachments["tools"].append({
                    "name": str(source.get("name") or "Capability")[:200],
                    "source": str(source.get("source") or "")[:80],
                    "source_ref": str(source.get("source_ref") or "")[:1000],
                    "instructions": str(source.get("description") or "")[:2000],
                })
        attached_outputs = []
        for edge in outgoing[sid]:
            target = by_id.get(str(edge.get("to") or "")) or {}
            if target.get("type") != "output":
                continue
            config = target.get("output") or {}
            attached_outputs.append({
                "name": str(target.get("name") or "Output")[:200],
                "destination": str(config.get("destination") or "record")[:80],
                "instructions": str(target.get("prompt")
                                    or config.get("instructions") or "")[:2000],
            })
        if attached_outputs:
            attachments["outputs"] = attached_outputs
        if any(value not in (None, [], {}) for value in attachments.values()):
            stage["forge"] = attachments
        stages.append(stage)
    circuits.validate_stages(stages)
    return stages


def _environment(node, incoming, by_id):
    env = {key: [] for key in ENV_KEYS.values()}
    own = node.get("environment") if isinstance(node.get("environment"), dict) else {}
    instructions = own.get("instructions") if "instructions" in own else ["project"]
    for item in instructions or []:
        _env_add(env, "instructions", str(item))
    for edge in incoming:
        source = by_id.get(str(edge.get("from") or "")) or {}
        if source.get("type") in SOURCE_NODE_TYPES:
            _env_add_card(env, source)
        elif source.get("type") == "env":
            for item in source.get("items") or []:
                if isinstance(item, dict):
                    _env_add_card(env, item)
    return env


def _env_add_card(env, card):
    kind = str(card.get("source") or card.get("kind") or "").strip().lower()
    if kind not in ENV_KEYS:
        return
    value = (card.get("source_ref") if kind in ENV_BY_REF else card.get("name")) or card.get("name")
    _env_add(env, kind, str(value or ""))


def _env_add(env, kind, value):
    value = value.strip()[:1000]
    if not value:
        return
    if kind == "settings" and value not in SETTING_SOURCES:
        return
    key = ENV_KEYS[kind]
    if value not in env[key] and len(env[key]) < MAX_ENV_ITEMS:
        env[key].append(value)


def save_flow(payload, save_as=False):
    """Save a circuit-backed Flow and its editor sidecar."""
    if ((payload.get("kind") == "native"
         or str(payload.get("id") or "").startswith("routine:")) and not save_as):
        raise ValueError("native Vira systems are edited through their schedule and source parts")
    stages = _compile(payload)
    source_id = "" if save_as else str(payload.get("id") or "").strip()
    rec = circuits.save_circuit({
        "id": source_id or None,
        "name": str(payload.get("name") or "Untitled Flow").strip()[:200],
        "description": str(payload.get("description") or "").strip()[:2000],
        "stages": stages,
    })
    # Schedule cards with a cadence become routines of kind circuit. Their
    # placement is stored under routine:<id>, which is the id the graph
    # gives them back, so the card lands where it was left.
    placement = {}
    input_text = next((str(n.get("prompt") or "").strip() for n in payload.get("nodes") or []
                       if isinstance(n, dict) and n.get("type") == "input"), "")
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "trigger":
            continue
        daily_at = str(node.get("daily_at") or "").strip()
        every_hours = node.get("every_hours")
        if not daily_at and not every_hours:
            continue
        routine_data = {
            "name": str(node.get("name") or f"{rec['name']} schedule")[:200],
            "kind": "circuit", "circuit_id": rec["id"],
            # a scheduled run's ask is the Input card's text
            "prompt": input_text[:MAX_TEXT],
            "description": str(node.get("description") or "")[:2000],
            "daily_at": daily_at or None,
            "every_hours": every_hours if not daily_at else None,
            "enabled": bool(node.get("enabled", True)),
            "notify": bool(node.get("notify")),
        }
        routine_id = None if save_as else str(node.get("routine_id") or "") or None
        row = routines.save_routine(routine_data, rid=routine_id)
        placement[str(node.get("id") or "")] = f"routine:{row['id']}"
    clean_nodes = []
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") not in DISPLAY_NODE_TYPES:
            continue
        node_id = placement.get(str(node.get("id") or "")) or str(node.get("id") or "")[:100]
        if node.get("type") == "input":
            node_id = INPUT_ID
        clean = {
            "id": node_id,
            "type": str(node.get("type") or "")[:40],
            "x": int(node.get("x") or 0), "y": int(node.get("y") or 0),
        }
        if node.get("type") in AGENT_NODE_TYPES:
            own = node.get("environment") if isinstance(node.get("environment"), dict) else {}
            if "instructions" in own:
                clean["environment"] = {"instructions": [
                    str(i)[:1000] for i in (own.get("instructions") or [])][:8]}
        if node.get("type") == "input":
            clean.update({"name": str(node.get("name") or "Input")[:200],
                          "prompt": str(node.get("prompt") or "")[:MAX_TEXT]})
        if node.get("type") == "env":
            clean.update({"name": str(node.get("name") or "Environment")[:200],
                          "items": _clean_env_items(node.get("items"))})
        if node.get("type") in SOURCE_NODE_TYPES or (
                node.get("type") == "trigger" and not node_id.startswith("routine:")):
            # A source card is a name, a path and a note; a manual schedule
            # card is a name. Never the text behind the path: the agent reads
            # the file itself when told where it is.
            clean.update({
                "name": str(node.get("name") or node.get("type") or "Part")[:200],
                "description": str(node.get("description") or "")[:2000],
                "source": str(node.get("source") or "forge")[:80],
                "source_ref": str(node.get("source_ref") or "")[:1000],
            })
        clean_nodes.append(clean)
    clean_edges = []
    for e in payload.get("edges") or []:
        if not isinstance(e, dict):
            continue
        source = placement.get(str(e.get("from") or "")) or str(e.get("from") or "")
        target = placement.get(str(e.get("to") or "")) or str(e.get("to") or "")
        clean_edges.append(_edge(source, target, e))
    previous = _store()["graphs"].get(rec["id"]) or {}
    graph = {
        "revision": int(previous.get("revision") or 0) + 1,
        "nodes": clean_nodes,
        "edges": clean_edges,
        "contexts": _clean_contexts(payload.get("contexts")),
        "updated": _now(),
    }

    def mutate(state):
        state.setdefault("graphs", {})[rec["id"]] = graph
    jsonstore.mutate(STORE, mutate, _blank(), indent=1, ensure_ascii=False)
    return get_flow(rec["id"])


def _clean_env_items(items):
    out = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or raw.get("source") or "")[:40]
        if not kind:
            continue
        out.append({"kind": kind, "name": str(raw.get("name") or kind)[:200],
                    "source_ref": str(raw.get("source_ref") or "")[:1000]})
        if len(out) >= MAX_ENV_ITEMS:
            break
    return out


def input_text(flow_id):
    """The saved Input card's text, the ask a run uses when none is given."""
    graph = _store()["graphs"].get(flow_id) or {}
    node = next((n for n in graph.get("nodes") or []
                 if isinstance(n, dict) and n.get("type") == "input"), None)
    return str((node or {}).get("prompt") or "").strip()


def run_flow(flow_id, input_text_given, cwd=None, notify=False, output="",
             idea_id=None, provider=None, vault_destination=None, vault_context=None):
    """Start a run. The ask is the Input card's text; the API may hand one
    in (the Queue's idea) and it wins. `provider` is kept for the API: a
    card that names no engine runs on the configured default. `output` is
    kept for the API's sake; the destination of a run is what its Output
    cards say."""
    ask = str(input_text_given or "").strip() or input_text(flow_id)
    return circuits.start_run(flow_id, ask, cwd=cwd, notify=notify,
                              source="forge", idea_id=idea_id,
                              flow_options={"output": output},
                              provider=provider,
                              vault_destination=vault_destination,
                              vault_context=vault_context)


def defaults():
    """What a card that names nothing runs on: the configured go-to
    provider, by name, so the picker never says "Vira default" for a thing
    that has a name (owner's rule, 2026-10-28)."""
    try:
        pid = agentbackend.default_session_provider()
    except Exception:  # noqa: BLE001 - a disabled go-to still has a label
        pid = str(models.PROVIDERS and "" or "")
    label = (models.PROVIDERS.get(pid) or {}).get("label") or pid or "the configured default"
    return {"provider": pid, "provider_label": label}


def _blank_envs():
    return {"environments": []}


def list_environments():
    value = jsonstore.read(ENVS, _blank_envs())
    rows = value.get("environments") if isinstance(value, dict) else None
    return [row for row in rows or [] if isinstance(row, dict) and row.get("id")]


def save_environment(data):
    """Create or update a saved environment: a name and up to 40 sources."""
    name = str((data or {}).get("name") or "").strip()[:200]
    if not name:
        raise ValueError("an environment needs a name")
    items = _clean_env_items((data or {}).get("items"))
    if not items:
        raise ValueError("an environment needs at least one source")
    eid = str((data or {}).get("id") or "").strip()[:60] or "env_" + uuid.uuid4().hex[:8]
    row = {"id": eid, "name": name, "items": items, "updated": _now()}

    def mutate(state):
        rows = [r for r in state.setdefault("environments", []) if r.get("id") != eid]
        rows.append(row)
        state["environments"] = rows
    jsonstore.mutate(ENVS, mutate, _blank_envs(), indent=1, ensure_ascii=False)
    return row


def delete_environment(eid):
    found = {"ok": False}

    def mutate(state):
        rows = state.setdefault("environments", [])
        found["ok"] = any(r.get("id") == eid for r in rows)
        state["environments"] = [r for r in rows if r.get("id") != eid]
    jsonstore.mutate(ENVS, mutate, _blank_envs(), indent=1, ensure_ascii=False)
    if not found["ok"]:
        raise KeyError(eid)


DEFAULT_IDEA_TEMPLATE = "plan-build-judge"


def flow_for_idea(idea_id, template=DEFAULT_IDEA_TEMPLATE):
    """Copy a starter into a Flow of this idea's own, ready to be edited.

    The idea is NOT baked into the graph — it is the run input. What the
    owner gets is a private copy of the workflow, named after the idea, so
    editing the graph, testing it, and re-running it are all ordinary Forge
    actions rather than a one-shot dispatch he cannot open. A second click
    on the same idea makes a second copy, deliberately: two attempts at one
    idea are two experiments, and silently reusing the first would discard
    whatever he had changed.
    """
    from . import ideaimages, ideas
    idea = next((i for i in ideas.list_items() if i["id"] == idea_id), None)
    if not idea:
        raise KeyError(idea_id)
    starter = circuits.get_circuit(template)
    if not starter:
        raise ValueError(f"unknown starter {template!r}")
    text = str(idea.get("text") or "").strip()
    # The NAME is the owner's words alone; the run INPUT also carries the
    # paths of any screenshots attached to the idea, or a Flow built from
    # "this, but broken" would reach the graph with the evidence stripped
    # off it.
    name = (text[:60] + ("…" if len(text) > 60 else "")) or "Idea"
    text = (text + ideaimages.prompt_block(idea)).strip()
    rec = circuits.save_circuit({
        "id": None,
        "name": name,
        "description": (f"From the Queue — {template}. "
                        f"{starter.get('description') or ''}").strip()[:2000],
        "stages": json.loads(json.dumps(starter.get("stages") or [])),
    })
    return {"flow_id": rec["id"], "name": rec["name"], "input": text,
            "idea_id": idea_id, "template": template}


def kit_source(ref):
    """Read one Kit source safely, never outside the local Claude library."""
    root = LIBRARY.expanduser().resolve()
    path = Path(str(ref or "")).expanduser().resolve()
    if path != root and root not in path.parents:
        raise ValueError("Kit source is outside the local library")
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            raise ValueError("Kit source is not a readable text file")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"ref": str(path), "files": [path.name],
                "text": text[:MAX_SOURCE], "truncated": len(text) > MAX_SOURCE}
    chunks = []
    files = []
    size = 0
    candidates = sorted((item for item in path.rglob("*")
                         if item.is_file() and not any(part.startswith(".")
                                                       for part in item.relative_to(path).parts)
                         and item.suffix.lower() in SOURCE_SUFFIXES),
                        key=lambda item: str(item.relative_to(path)).lower())
    for item in candidates[:80]:
        relative = str(item.relative_to(path))
        text = item.read_text(encoding="utf-8", errors="replace")
        header = f"\n\n--- {relative} ---\n"
        remaining = MAX_SOURCE - size - len(header)
        if remaining <= 0:
            break
        chunk = header + text[:remaining]
        chunks.append(chunk)
        files.append(relative)
        size += len(chunk)
        if len(text) > remaining:
            break
    truncated = len(candidates) > len(files) or size >= MAX_SOURCE
    return {"ref": str(path), "files": files,
            "text": "".join(chunks).lstrip(), "truncated": truncated}


def native_source(routine_id):
    row = routines.get_routine(routine_id)
    if not row:
        raise KeyError(routine_id)
    from . import routinesrc
    return routinesrc.hood(row)


def kit_catalog():
    items = []
    for item in actions.scan_library():
        if item["kind"] == "skill":
            source_ref = LIBRARY / "skills" / item["name"] / "SKILL.md"
        else:
            source_ref = LIBRARY / "commands" / f"{item['name']}.md"
        items.append({**item, "id": f"{item['kind']}:{item['name']}",
                      "type": "tool", "source_ref": str(source_ref)})
    # The two things a session used to receive without being asked: its
    # settings files and the project's instruction file. As cards, so a
    # bare card can have them back on purpose.
    for source, name, note in (
            ("user", SETTINGS_NAMES["user"], "~/.claude/settings.json: permissions, hooks, env"),
            ("project", SETTINGS_NAMES["project"], ".claude/settings.json in the working folder"),
            ("local", SETTINGS_NAMES["local"], ".claude/settings.local.json in the working folder")):
        items.append({"id": f"settings:{source}", "name": name, "kind": "settings",
                      "type": "tool", "invoke": "", "source_ref": source,
                      "description": note, "arg_fields": []})
    items.append({"id": "instructions:project", "name": "Project instructions",
                  "kind": "instructions", "type": "tool", "invoke": "",
                  "source_ref": "project",
                  "description": "The AGENTS.md or CLAUDE.md of the working folder",
                  "arg_fields": []})
    roots = [
        ("script", LIBRARY / "scripts", {".py", ".sh", ".js", ".zsh"}),
        ("agent", LIBRARY / "agents", {".md"}),
        ("mcp", LIBRARY / "mcp-servers", None),
        ("plugin", LIBRARY / "dist-plugins", None),
        ("scheduled-task", LIBRARY / "scheduled-tasks", None),
        ("harness", LIBRARY / "harnesses", None),
        ("hook", LIBRARY / "hooks", None),
    ]
    seen = {item["id"] for item in items}
    for kind, root, suffixes in roots:
        if not root.exists():
            continue
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        for path in children:
            if path.name.startswith("."):
                continue
            if suffixes is not None and (not path.is_file() or path.suffix not in suffixes):
                continue
            if suffixes is None and not path.is_dir():
                continue
            name = path.stem if path.is_file() else path.name
            item_id = f"{kind}:{name}"
            if item_id in seen:
                continue
            seen.add(item_id)
            items.append({
                "id": item_id, "name": name, "kind": kind,
                "type": "tool",
                "invoke": str(path), "source_ref": str(path),
                "description": f"{kind.replace('_', ' ').title()} capability",
                "arg_fields": [],
            })
    # Installed plugins: ~/.claude/plugins/cache/<marketplace>/<plugin>/
    # <version>/; the newest version folder is the plugin's root.
    cache = LIBRARY / "plugins" / "cache"
    if cache.is_dir():
        for marketplace in sorted(cache.iterdir(), key=lambda p: p.name.lower()):
            if not marketplace.is_dir() or marketplace.name.startswith("."):
                continue
            for plugin in sorted(marketplace.iterdir(), key=lambda p: p.name.lower()):
                if not plugin.is_dir() or plugin.name.startswith("."):
                    continue
                versions = sorted((v for v in plugin.iterdir() if v.is_dir()),
                                  key=lambda v: v.name)
                if not versions:
                    continue
                item_id = f"plugin:{plugin.name}"
                if item_id in seen:
                    continue
                seen.add(item_id)
                items.append({"id": item_id, "name": plugin.name, "kind": "plugin",
                              "type": "tool", "invoke": str(versions[-1]),
                              "source_ref": str(versions[-1]),
                              "description": f"Installed plugin ({marketplace.name}, {versions[-1].name})",
                              "arg_fields": []})
    return items

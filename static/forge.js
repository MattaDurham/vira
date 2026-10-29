/* The Forge: Vira's Flow editor. The product shell, persistence and
   execution live in Vira; this file owns the board, the library and the
   right-column editor.

   A Flow is a board of cards wired in one direction. Six kinds run
   (agent, judge, gate, ask me, output, native); the Input card is the ask;
   a schedule runs it; sources (a skill, a settings file, a script, a file,
   a Find reference) and saved environments are wired into a card and are
   the whole of what its session is told about. A card is who runs it,
   what it must do and what it may touch; the board shows it small and the
   right column is the only editor. A wire means the finished text of the
   card before goes into the card after (the server appends it unless the
   box places it), and it may carry a note. server/flows.py reads nothing
   else, so the editor draws nothing else (2026-10-27, rebuilt 10-28). */
(() => {
  "use strict";

  const q = (selector, root = document) => root.querySelector(selector);
  const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const id = (prefix) => `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  const requestRaw = async (path, options) => {
    const response = await fetch(path, options);
    if (!response.ok) throw new Error((await response.text()).slice(0, 300));
    return response.json();
  };
  // the board's own requests wear the app's busy wheel on the control that
  // started them (app.js busyTrack) when the board runs inside Vira
  const request = (path, options) => (typeof window.busyTrack === "function"
    ? window.busyTrack(requestRaw(path, options)) : requestRaw(path, options));
  const send = (path, method, body) => request(path, {
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  /* ---------- vocabulary ------------------------------------------------ */

  const KIND = {
    input: { name: "Input", hint: "The ask: what this Flow works on. Run sends it." },
    agent: { name: "Agent", hint: "A session: a model, a permission, an instruction." },
    judge: { name: "Judge", hint: "Grades the card before it and can send it back." },
    logic: { name: "Gate", hint: "Passes or stops on what the card before it did." },
    approval: { name: "Ask me", hint: "Pauses until you answer." },
    output: { name: "Output", hint: "Where the result lands and what shape it takes." },
    trigger: { name: "Schedule", hint: "Runs the Flow on a cadence." },
    native: { name: "Loop", hint: "Runs one of your loops inside this Flow and waits for it; the card after gets its result." },
    tool: { name: "Source", hint: "A skill, command, script or file the card is told about." },
    context: { name: "Reference", hint: "A note or file from Find the card is told about." },
    env: { name: "Environment", hint: "A saved set of sources, wired in as one card." },
  };
  // What a source card is, by the kind of thing behind it.
  const SOURCE_LABEL = {
    skill: "Skill", command: "Command", script: "Script", agent: "Agent file",
    plugin: "Plugin", mcp: "MCP server", hook: "Hook", settings: "Settings",
    instructions: "Instructions", harness: "Harness", "scheduled-task": "Scheduled task",
    file: "File", folder: "Folder", find: "Reference", library: "Source",
  };
  // Kinds that become part of a card's environment (server/flows.py
  // ENV_KEYS); the rest reach the card as a line of text.
  const ENV_KINDS = new Set(["skill", "plugin", "agent", "command", "mcp", "hook", "settings", "instructions"]);
  const INPUT_ID = "input";
  const EXEC = new Set(["agent", "judge", "logic", "approval", "output", "native"]);
  const SOURCE = new Set(["tool", "context"]);
  const CARD_W = 264;

  // Who runs a card. One picker: the model aliases Vira resolves, or a
  // whole provider (its own default model). Mirrors server/models.py
  // PROVIDERS and agentbackend.provider_of_model.
  const MODEL_CHOICES = [
    ["", "the configured default"], ["provider:anthropic", "Claude"], ["fable", "Fable"],
    ["opus", "Opus"], ["sonnet", "Sonnet"], ["haiku", "Haiku"],
    ["provider:openai", "Codex"], ["provider:google", "Gemini"], ["provider:xai", "Grok"],
    ["other", "Another model id"],
  ];
  const MODEL_LABEL = Object.fromEntries(MODEL_CHOICES);
  const PROVIDER_LABEL = { anthropic: "Claude", openai: "Codex", google: "Gemini", xai: "Grok" };
  const PERMS = [["read", "read"], ["ask", "ask"], ["edit", "edit"], ["full", "full"]];
  const PERM_HINT = {
    read: "Read-only. It can look but not change anything.",
    ask: "It may write, and asks you before each write.",
    edit: "It may edit files on its own.",
    full: "Full execution, no questions. Use for a build you trust.",
  };
  const GRADES = [["", "no grade gate"], ["A", "A"], ["B", "B"], ["C", "C"], ["D", "D"], ["F", "F"]];
  const DESTINATIONS = [["record", "a Vira record"], ["artifact", "an artifact"],
    ["decision_brief", "a decision brief"], ["notification", "a notification"],
    ["plan", "a plan dossier"]];
  const DESTINATION_LABEL = Object.fromEntries(DESTINATIONS);

  // Two families of gate, and the split is what the labels say: the first
  // reads what the card before it WROTE, the second reads how it RAN - its
  // ledger row, the tools it called, the cards it raised, its transcript.
  // Mirrors server/circuits.py LOGIC_OPS; the server refuses an unknown one.
  const LOGIC_OPS = [
    ["What it wrote", [["always", "always passes"], ["has_output", "wrote anything"],
      ["contains", "wrote text containing"], ["not_contains", "wrote nothing containing"],
      ["equals", "wrote exactly"]]],
    ["How it ran", [["provider_is", "ran on provider"],
      ["transport_matches_provider", "ran on its provider's own lane"],
      ["transport_is", "ran on transport"], ["model_used_contains", "used a model containing"],
      ["outcome_is", "ended with status"], ["tool_called", "called the Vira tool"],
      ["tool_not_called", "did not call the Vira tool"],
      ["card_raised", "raised a card of kind"],
      ["log_contains", "has a transcript containing"], ["log_not_contains", "has no transcript line containing"],
      ["placed_in_worktree", "was placed in a worktree"], ["not_in_worktree", "ran with no worktree"],
      ["read_only_honored", "stayed read-only"], ["guard_held", "was held by the branch-first guard"],
      ["interrupt_honored", "was interrupted by its timeout"]]],
  ];
  const NO_VALUE_OPS = new Set(["always", "has_output", "transport_matches_provider",
    "placed_in_worktree", "not_in_worktree", "read_only_honored", "guard_held",
    "interrupt_honored"]);
  const OP_LABEL = Object.fromEntries(LOGIC_OPS.flatMap(([, ops]) => ops));

  // The gate editor's menu, flat: every operation the engine offers.
  function logicControls() { return LOGIC_OPS; }

  const ICON = {
    flow: "M4 6h5v5H4zM15 4h5v5h-5zM15 15h5v5h-5zM9 8.5h3c2 0 3-1 3-2M9 8.5h2c3 0 4 4 4 9",
    skill: "M12 3l2.2 4.5L19 8l-3.5 3.4.9 4.8L12 14l-4.4 2.2.9-4.8L5 8l4.8-.5z",
    command: "M4 6h16v12H4zM7 10l2 2-2 2M11 14h5",
    script: "M6 3h9l3 3v15H6zM15 3v4h4M9 11l-2 2 2 2M13 11l2 2-2 2",
    agent: "M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8M5 21c.6-4 3-6 7-6s6.4 2 7 6",
    context: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h5",
    tool: "M14 5a4 4 0 0 0-5 5L4 15l5 5 5-5a4 4 0 0 0 5-5l-3 3-3-3 3-3z",
    loop: "M4 12a8 8 0 0 1 14-5.3M20 12a8 8 0 0 1-14 5.3M18 3v4h-4M6 21v-4h4",
  };

  const state = {
    ready: false,
    loading: false,
    flows: [],
    kit: [],
    loops: [],
    envs: [],
    defaults: { provider: "", provider_label: "" },
    mac: null,
    current: null,
    selectedNode: null,
    selectedEdge: null,
    selectedLoop: null,
    library: "flows",
    query: "",
    findRows: [],
    zoom: 1,
    panX: 25,
    panY: 55,
    dirty: false,
    z: 20,
    connect: null,
    tempPoint: null,
    libraryDrag: null,
    runs: [],
    heights: {},
    // The armed run trace: { runId, run (stash for runs older than the
    // fetch window), sig (repaint guard) }. The overlay is a STATE laid
    // over the flow's own board: one renderer, never a second one.
    trace: null,
  };

  function flowPayload(flow = state.current) {
    return {
      id: flow?.id || null,
      name: flow?.name || "Untitled Flow",
      description: flow?.description || "",
      kind: flow?.kind || "flow",
      nodes: copy(flow?.nodes || []),
      edges: copy(flow?.edges || []),
      contexts: copy(flow?.contexts || []),
    };
  }

  /* ---------- Edit history ------------------------------------------------
     setDirty() is the Forge's one mutation choke point - every edit on the
     board already calls it - so the undo stack hangs off it rather than off
     the individual call sites, which is what keeps a newly added mutation
     undoable without anyone remembering this file exists.

     A snapshot is the DOCUMENT only. Server-owned identity (id, revision,
     created, updated) is deliberately left out: an undo restores content and
     must never re-point the flow at another record. */

  const HISTORY_MAX = 60;
  const COALESCE_MS = 800;
  const CLIP_KEY = "vira-forge-clip";
  const history = { past: [], future: [], baseline: null, saved: null, key: null, at: 0 };
  let historyLock = false;

  function docSnapshot(flow = state.current) {
    if (!flow) return null;
    return JSON.stringify({
      name: flow.name || "",
      description: flow.description || "",
      kind: flow.kind || "flow",
      nodes: (flow.nodes || []).map((node) => {
        const clean = { ...node };
        delete clean.z;
        return clean;
      }),
      edges: flow.edges || [],
      contexts: flow.contexts || [],
      triggers: flow.triggers || [],
    });
  }

  function restoreDoc(snap) {
    const doc = JSON.parse(snap);
    const flow = state.current;
    if (!flow) return;
    flow.name = doc.name;
    flow.description = doc.description;
    flow.kind = doc.kind;
    flow.nodes = doc.nodes;
    flow.edges = doc.edges;
    flow.contexts = doc.contexts;
    flow.triggers = doc.triggers;
  }

  // A text field fires change() on every keystroke, so without coalescing one
  // typed sentence would be sixty undo steps and would evict every structural
  // edit behind it. The key is stamped on the element itself.
  function editKey() {
    const el = document.activeElement;
    if (!el || (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA")) return null;
    if (el.type === "checkbox" || el.type === "radio") return null;
    if (!el.dataset.forgeKey) el.dataset.forgeKey = id("edit");
    return el.dataset.forgeKey;
  }

  function historyMark() {
    const before = history.baseline;
    const after = docSnapshot();
    history.baseline = after;
    if (after == null || before === after) return;
    const key = editKey();
    const now = Date.now();
    const merge = key && key === history.key && now - history.at < COALESCE_MS;
    history.key = key;
    history.at = now;
    if (merge || before == null) return;
    history.past.push(before);
    if (history.past.length > HISTORY_MAX) history.past.shift();
    history.future.length = 0;
  }

  function historyReset() {
    history.past.length = 0;
    history.future.length = 0;
    history.baseline = docSnapshot();
    history.saved = history.baseline;
    history.key = null;
    history.at = 0;
  }

  function stepHistory(back) {
    const from = back ? history.past : history.future;
    const to = back ? history.future : history.past;
    if (!state.current || !from.length) return;
    to.push(history.baseline);
    const snap = from.pop();
    history.baseline = snap;
    history.key = null;
    historyLock = true;
    try {
      restoreDoc(snap);
      // The editor holds live closures over the node and edge objects this
      // restore just replaced; left open, further typing writes into orphans.
      if (!state.current.nodes.some((node) => node.id === state.selectedNode)) state.selectedNode = null;
      if (!state.current.edges.some((edge) => edge.id === state.selectedEdge)) state.selectedEdge = null;
      setDirty(snap !== history.saved);
    } finally {
      historyLock = false;
    }
    renderIdentity();
    renderBoard();
    renderInspector();
  }

  /* ---------- Clipboard --------------------------------------------------- */

  function readClip() {
    try {
      const raw = localStorage.getItem(CLIP_KEY);
      const clip = raw ? JSON.parse(raw) : null;
      return clip && clip.node ? clip : null;
    } catch (error) {
      return null;
    }
  }

  function currentNode() {
    return state.current?.nodes.find((node) => node.id === state.selectedNode) || null;
  }

  // The board has no edge (owner, 2026-10-28): a card goes where it is put.
  function freeSpot(node) {
    let x = (Number(node.x) || 0) + 42;
    let y = (Number(node.y) || 0) + 42;
    for (let tries = 0; tries < 24; tries += 1) {
      const taken = state.current.nodes.some((item) => Math.abs(item.x - x) < 14 && Math.abs(item.y - y) < 14);
      if (!taken) break;
      x += 42;
      y += 42;
    }
    return { x, y };
  }

  function copyNode() {
    const node = currentNode();
    if (!node) return;
    const clean = { ...node };
    ["z", "routine_id", "forge", "continues"].forEach((key) => delete clean[key]);
    try {
      localStorage.setItem(CLIP_KEY, JSON.stringify({ node: clean, from: state.current?.name || "" }));
    } catch (error) {
      toast("Could not copy that card");
      return;
    }
    syncTools();
    toast(`Copied "${node.name}"`);
  }

  function pasteNode() {
    const clip = readClip();
    if (!clip || !editable()) return;
    if (clip.node.type === "input" && inputCard()) return toast("A board has one Input card.");
    const clone = copy(clip.node);
    clone.id = cardId(clone.name, clone.type);
    if (clone.id.startsWith("routine:")) clone.id = cardId("schedule", "trigger");
    const spot = freeSpot(clone);
    clone.x = spot.x;
    clone.y = spot.y;
    clone.z = ++state.z;
    state.current.nodes.push(clone);
    state.selectedNode = clone.id;
    state.selectedEdge = null;
    setDirty();
    renderBoard();
    renderInspector();
    toast(`Pasted "${clone.name}"`);
  }

  function editable() {
    return !!state.current && state.current.kind !== "trace";
  }

  function syncTools() {
    const flow = state.current;
    const node = currentNode();
    const set = (selector, enabled) => {
      const button = q(selector);
      if (button) button.disabled = !enabled;
    };
    set("#forge-undo", !!flow && history.past.length > 0);
    set("#forge-redo", !!flow && history.future.length > 0);
    set("#forge-copy", !!node);
    set("#forge-paste", editable() && !!readClip());
    set("#forge-duplicate", editable() && !!node);
    set("#forge-delete", editable() && (!!node || !!state.selectedEdge));
  }

  function setDirty(value = true) {
    state.dirty = value;
    if (!historyLock) {
      if (value) historyMark();
      else historyReset();
    }
    syncTools();
    const line = q("#forge-status");
    if (!line) return;
    const flow = state.current;
    line.textContent = !flow ? "Live definitions"
      : value ? "Unsaved changes"
        : flow.kind === "trace" ? "A finished run's own cards; nothing to save"
          : flow.builtin ? "Shipped starter. Save as makes your own copy."
            : flow.id ? "Saved" : "Not saved yet";
  }

  function toast(message) {
    const shell = q("#forge-shell");
    if (!shell) return;
    q(".forge-toast", shell)?.remove();
    const note = make("div", "forge-toast", message);
    shell.appendChild(note);
    setTimeout(() => note.remove(), 3600);
  }

  /* ---------- loading ----------------------------------------------------- */

  async function loadForge(options = {}) {
    if (state.loading) return;
    if (state.ready && !options.force) {
      renderAll();
      return;
    }
    state.loading = true;
    q("#forge-status").textContent = "Loading";
    try {
      const [flowData, kitData] = await Promise.all([
        request("/api/flows"),
        request("/api/flows/kit"),
      ]);
      state.flows = flowData.flows || [];
      state.defaults = flowData.defaults || state.defaults;
      state.envs = flowData.environments || [];
      state.kit = kitData.items || [];
      const wanted = options.flowId || state.current?.id || localStorage.getItem("vira-forge-flow") || "plan-build-judge";
      const selected = state.flows.find((flow) => flow.id === wanted) || state.flows[0] || null;
      state.current = selected ? copy(selected) : null;
      state.ready = true;
      state.selectedNode = null;
      state.selectedEdge = null;
      setDirty(false);
      renderAll();
    } catch (error) {
      q("#forge-status").textContent = "Forge unavailable";
      toast(`Could not load the Forge: ${error.message}`);
    } finally {
      state.loading = false;
    }
  }

  async function selectFlow(flowId) {
    if (state.dirty && !await forgeDialog({
      title: "Open another Flow?",
      message: "This board has unsaved changes. Discard them and open the other Flow?",
      confirm: "Discard changes",
    })) return;
    const flow = state.flows.find((item) => item.id === flowId);
    if (!flow) return;
    state.current = copy(flow);
    // The Queue link is a fact about ONE Flow, so opening another drops it;
    // otherwise a later run would close out an idea it has nothing to do with.
    if (state.idea && state.idea.flow_id !== flow.id) state.idea = null;
    // An armed trace is equally a fact about one Flow.
    if (state.trace && (tracedRun() || {}).circuit_id !== flow.id) state.trace = null;
    state.selectedNode = null;
    state.selectedEdge = null;
    state.selectedLoop = null;
    state.zoom = 1;
    state.panX = 25;
    state.panY = 55;
    localStorage.setItem("vira-forge-flow", flow.id);
    setDirty(false);
    renderAll();
    if (matchMedia("(max-width: 1099px)").matches) closeLibrary();
  }

  function forgeDialog({ title, message, value = null, confirm = "Continue", choices = null }) {
    return new Promise((resolve) => {
      const shell = q("#forge-shell");
      q(".forge-dialog-backdrop", shell)?.remove();
      const backdrop = make("div", "forge-dialog-backdrop");
      const dialog = make("form", "forge-dialog");
      dialog.append(make("span", "forge-kicker", "The Forge"), make("h3", "", title), make("p", "", message));
      let input = null;
      if (value != null) {
        input = make("input"); input.value = value; input.autocomplete = "off";
        dialog.appendChild(input);
      }
      const actions = make("div", "forge-dialog-actions");
      const cancel = make("button", "fchip sm", "Cancel"); cancel.type = "button";
      actions.appendChild(cancel);
      const finish = (result) => { backdrop.remove(); resolve(result); };
      cancel.addEventListener("click", () => finish(value == null && !choices ? false : null));
      if (choices) {
        choices.forEach(([key, label], index) => {
          const button = make("button", index === choices.length - 1 ? "btn primary" : "fchip sm", label);
          button.type = "button";
          button.addEventListener("click", () => finish(key));
          actions.appendChild(button);
        });
      } else {
        const accept = make("button", "btn primary", confirm); accept.type = "submit";
        actions.appendChild(accept);
      }
      dialog.appendChild(actions); backdrop.appendChild(dialog); shell.appendChild(backdrop);
      backdrop.addEventListener("pointerdown", (event) => {
        if (event.target === backdrop) finish(value == null && !choices ? false : null);
      });
      dialog.addEventListener("submit", (event) => {
        event.preventDefault();
        if (choices) return;
        const result = value == null ? true : input.value.trim();
        if (value != null && !result) { input.focus(); return; }
        finish(result);
      });
      setTimeout(() => (input || q("button:last-child", actions)).focus(), 0);
    });
  }

  function renderAll() {
    renderIdentity();
    renderLibrary();
    renderBoard();
    renderInspector();
    renderTraceBar();
  }

  function renderIdentity() {
    const flow = state.current;
    q("#forge-flow-name").textContent = flow?.name || "Choose a Flow";
    q("#forge-flow-description").textContent = flow?.description
      || "Who runs each step, what it must do, and what it may touch. Wire the cards; the finished text of one is the input of the next.";
    q("#forge-flow-revision").textContent = flow ? `v${flow.revision || 1}` : "";
    const ghost = flow?.kind === "trace";
    q("#forge-save").disabled = !flow || ghost;
    q("#forge-save-as").disabled = !flow || ghost;
    q("#forge-run").disabled = !flow || ghost;
    syncTools();
  }

  /* ---------- Run trace ---------------------------------------------------
     Selecting a run paints its state onto the flow's OWN board: per-card
     status, the active handoff pulsing, a judge wearing its grade, and the
     badge on a card that ran opening that stage's terminal. */

  function tracedRun() {
    if (!state.trace) return null;
    return state.runs.find((run) => run.id === state.trace.runId) || state.trace.run || null;
  }

  function runFitsFlow(run) {
    return Boolean(run && state.current
      && (state.current.id === run.circuit_id
          || (state.current.kind === "trace" && state.current.trace_run === run.id)));
  }

  function currentRun() {
    if (!state.current) return null;
    const traced = tracedRun();
    if (traced && runFitsFlow(traced)) return traced;
    return state.runs.find((run) => run.circuit_id === state.current.id) || null;
  }

  function runStage(nodeId) {
    const run = tracedRun();
    if (!run || !runFitsFlow(run)) return null;
    return (run.stages || {})[nodeId] || null;
  }

  function stopTrace() {
    if (!state.trace) return;
    state.trace = null;
    if (state.current?.kind === "trace") state.current = null;
    renderBoard();
    renderTraceBar();
    renderIdentity();
  }

  function renderTraceBar() {
    const bar = q("#forge-trace");
    if (!bar) return;
    const run = tracedRun();
    bar.hidden = !run;
    bar.replaceChildren();
    if (!run) return;
    const defs = run.stages_def || [];
    const states = run.stages || {};
    const done = defs.filter((d) => (states[d.id] || {}).status === "done").length;
    const grades = defs.map((d) => (states[d.id] || {}).grade).filter(Boolean);
    bar.appendChild(make("i", `forge-trace-dot is-${run.status || "done"}`));
    const label = make("span", "forge-trace-label");
    label.appendChild(make("strong", "", `Tracing ${run.circuit_name || run.circuit_id || "run"}`));
    label.appendChild(make("small", "", [
      run.id,
      run.status === "running"
        ? `running, ${done}/${defs.length || Object.keys(states).length} steps done`
        : run.status,
      grades.length ? `graded ${grades.join(", ")}` : "",
    ].filter(Boolean).join(" / ")));
    bar.appendChild(label);
    const stop = make("button", "fchip sm", "Stop tracing");
    stop.type = "button";
    stop.addEventListener("click", stopTrace);
    bar.appendChild(stop);
  }

  /* A run can outlive its circuit (a deleted Flow). The trace then compiles
     the run's OWN frozen stages_def into a display graph: topo columns from
     `needs`, the same deterministic layout the server's flows._layout uses.
     Marked kind "trace" so nothing offers to save or launch a ghost. */
  function traceFlowFromRun(run) {
    const defs = (run.stages_def || []).filter((d) => d && d.id);
    const byId = Object.fromEntries(defs.map((d) => [d.id, d]));
    const depth = {};
    const visit = (sid, seen) => {
      if (depth[sid] != null) return depth[sid];
      seen = new Set(seen || []);
      if (seen.has(sid)) return 0;
      seen.add(sid);
      const needs = (byId[sid].needs || []).filter((n) => byId[n]);
      depth[sid] = needs.length ? Math.max(...needs.map((n) => visit(n, seen))) + 1 : 0;
      return depth[sid];
    };
    defs.forEach((d) => visit(d.id));
    const rows = {};
    const nodes = defs.map((d) => {
      const col = depth[d.id] || 0;
      const row = rows[col] || 0;
      rows[col] = row + 1;
      const mode = d.mode;
      const type = ["judge", "logic", "approval", "output", "native"].includes(mode) ? mode : "agent";
      return {
        id: d.id, type, name: d.name || d.id,
        x: 150 + col * 310, y: 130 + row * 180,
        model: d.model || "", provider: d.provider || "", mode: d.mode || "",
        read_only: Boolean(d.read_only),
        prompt: (type === "judge" ? d.extra : d.prompt) || "",
        judge: copy(d.judge || {}), logic: copy(d.logic || {}),
        output: copy(d.output || {}), approval: copy(d.approval || {}),
      };
    });
    const edges = [];
    defs.forEach((d) => (d.needs || []).forEach((need) => {
      if (byId[need]) edges.push({ id: `edge:${need}:${d.id}`, from: need, to: d.id, instructions: "" });
    }));
    return {
      id: run.circuit_id || `trace:${run.id}`, trace_run: run.id,
      name: run.circuit_name || run.circuit_id || "Traced run",
      description: "This Flow's definition is gone; the board shows the run's own frozen cards.",
      kind: "trace", builtin: false, revision: 1,
      nodes, edges, contexts: [], triggers: [], executable: false,
    };
  }

  /** The one entry point app.js calls: load the run's circuit onto the
   *  board and arm the trace overlay. */
  async function traceRun(runId) {
    if (!runId) return;
    if (!state.ready) await loadForge();
    let run = state.runs.find((r) => r.id === runId);
    if (!run) {
      try {
        const data = await request("/api/circuits/runs?limit=200");
        if (data.runs?.length) state.runs = data.runs;
        run = state.runs.find((r) => r.id === runId);
      } catch (error) { /* the honest refusal below covers it */ }
    }
    if (!run) return toast("That run is no longer on the record.");
    const flow = state.flows.find((f) => f.id === run.circuit_id);
    if (flow) {
      if (state.current?.id !== flow.id) await selectFlow(flow.id);
      if (state.current?.id !== flow.id) return;   // dirty-guard declined
    } else {
      if (state.dirty && !await forgeDialog({
        title: "Trace this run?",
        message: "This board has unsaved changes. Discard them and show the traced run's own cards?",
        confirm: "Discard changes",
      })) return;
      state.current = traceFlowFromRun(run);
      state.selectedNode = null;
      state.selectedEdge = null;
      setDirty(false);
    }
    state.trace = { runId: run.id, run, sig: "" };
    renderAll();
    loadForgeRuns();
  }

  /* ---------- library: Flows, Sources, Loops ------------------------------ */

  function icon(key) {
    const span = make("span", "forge-card-icon");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ICON[key] || ICON.tool);
    svg.appendChild(path); span.appendChild(svg);
    return span;
  }

  function cadenceText(row) {
    if (!row) return "Manual";
    if (row.daily_at) return `Daily at ${row.daily_at}`;
    const hours = Number(row.every_hours);
    if (!hours) return "Manual";
    return hours === 1 ? "Every hour" : `Every ${hours} hours`;
  }

  function libraryCard({ title, line, iconKey, active, draggable, dot, tone }) {
    const card = make("button", "forge-library-card");
    card.type = "button";
    card.draggable = Boolean(draggable);
    if (active) card.classList.add("is-active");
    const text = make("span");
    text.appendChild(make("strong", "", title));
    const small = make("small", tone ? `forge-tone-${tone}` : "", line);
    text.appendChild(small);
    card.append(icon(iconKey), text);
    if (dot) card.appendChild(make("i", `forge-dot ${dot}`));
    return card;
  }

  function matches(term, ...values) {
    if (!term) return true;
    return values.some((value) => String(value || "").toLowerCase().includes(term));
  }

  function renderLibrary() {
    const body = q("#forge-library-body");
    if (!body) return;
    qa("#forge-library-tabs .seg-btn").forEach((button) =>
      button.classList.toggle("on", button.dataset.forgeLibrary === state.library));
    body.replaceChildren();
    const term = state.query.trim().toLowerCase();
    if (state.library === "flows") renderFlowsTab(body, term);
    else if (state.library === "sources") renderSourcesTab(body, term);
    else renderLoopsTab(body, term);
  }

  function renderFlowsTab(body, term) {
    const groups = [["Starters", state.flows.filter((f) => f.builtin)],
      ["My Flows", state.flows.filter((f) => !f.builtin)]];
    let any = false;
    groups.forEach(([label, flows]) => {
      const rows = flows.filter((f) => matches(term, f.name, f.description));
      if (!rows.length) return;
      any = true;
      body.appendChild(make("div", "forge-library-group", label));
      rows.forEach((flow) => {
        const schedule = flow.triggers?.[0];
        const paused = schedule && schedule.enabled === false ? ", paused" : "";
        const card = libraryCard({
          title: flow.name, iconKey: "flow", active: flow.id === state.current?.id,
          line: `${flow.nodes?.length || 0} cards, ${cadenceText(schedule).toLowerCase()}${paused}`,
        });
        card.addEventListener("click", () => selectFlow(flow.id));
        body.appendChild(card);
      });
    });
    if (!any) body.appendChild(make("p", "hint", term ? "No Flow matches." : "No Flows yet. New starts a blank board."));
  }

  function sourceLine(item) {
    return item.description || item.invoke || item.source_ref || "";
  }

  const GROUP_ORDER = ["instructions", "settings", "skill", "command", "agent", "plugin",
    "mcp", "hook", "script", "scheduled-task", "harness"];
  const GROUP_LABEL = {
    instructions: "Instruction files", settings: "Settings files", skill: "Skills",
    command: "Commands", agent: "Agent files", plugin: "Plugins", mcp: "MCP servers",
    hook: "Hooks", script: "Scripts", "scheduled-task": "Scheduled tasks", harness: "Harnesses",
  };

  function browseRow() {
    const row = make("div", "forge-browse");
    row.appendChild(make("span", "forge-card-kind", "This Mac"));
    [["folder", "Folder"], ["file", "File"]].forEach(([kind, label]) => {
      const button = make("button", "fchip sm", label);
      button.type = "button";
      button.title = kind === "folder" ? "Pick a folder in a window" : "Pick a file in a window";
      button.addEventListener("click", () => browseMac(kind));
      row.appendChild(button);
    });
    return row;
  }

  function renderSourcesTab(body, term) {
    if (editable()) body.appendChild(browseRow());
    const envs = state.envs.filter((env) => matches(term, env.name, (env.items || []).map((i) => i.name).join(" ")));
    if (envs.length) {
      body.appendChild(make("div", "forge-library-group", "Saved environments"));
      envs.forEach((env) => {
        const card = libraryCard({ title: env.name, iconKey: "tool", draggable: true,
          line: (env.items || []).map((i) => i.name).join(", ") });
        card.addEventListener("click", () => addEnvironment(env));
        card.addEventListener("dragstart", (event) => {
          state.libraryDrag = { environment: copy(env) };
          event.dataTransfer.effectAllowed = "copy";
          event.dataTransfer.setData("text/plain", env.name);
        });
        body.appendChild(card);
      });
    }
    const items = state.kit.filter((item) => matches(term, item.name, item.kind, item.description));
    const groups = new Map();
    items.forEach((item) => {
      const kind = item.kind || "parts";
      if (!groups.has(kind)) groups.set(kind, []);
      groups.get(kind).push(item);
    });
    const order = [...GROUP_ORDER.filter((k) => groups.has(k)), ...[...groups.keys()].filter((k) => !GROUP_ORDER.includes(k))];
    order.forEach((kind) => {
      body.appendChild(make("div", "forge-library-group", GROUP_LABEL[kind] || kind));
      groups.get(kind).forEach((item) => {
        const card = libraryCard({ title: item.name, line: sourceLine(item), iconKey: ICON[item.kind] ? item.kind : "tool", draggable: true });
        card.addEventListener("click", () => addSource(item));
        card.addEventListener("dragstart", (event) => {
          state.libraryDrag = copy(item);
          event.dataTransfer.effectAllowed = "copy";
          event.dataTransfer.setData("text/plain", item.id || item.name || "part");
        });
        body.appendChild(card);
      });
    });
    if (state.findRows.length) {
      body.appendChild(make("div", "forge-library-group", "From Find"));
      state.findRows.forEach((item) => {
        const card = libraryCard({ title: item.name, line: item.note || item.ref, iconKey: "context", draggable: true });
        card.addEventListener("click", () => addSource(item));
        card.addEventListener("dragstart", (event) => {
          state.libraryDrag = copy(item);
          event.dataTransfer.setData("text/plain", item.id);
        });
        body.appendChild(card);
      });
    }
    if (!items.length && !state.findRows.length && !envs.length) {
      body.appendChild(make("p", "hint", term
        ? (term.length < 3 ? "Keep typing to search Find too." : "Nothing in the library or in Find matches.")
        : "Everything a card can be told about. A card with nothing wired in is told nothing."));
    }
  }

  // Browse this Mac: the Mac's own open panel, opened by the server, which
  // runs here. On a phone over Tailscale the panel would open on the wrong
  // screen, so the server refuses and the typed-path fallback appears.
  async function browseMac(kind) {
    if (!editable()) return;
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
    let path = "";
    try {
      const res = await send("/api/pick-folder", "POST", {
        prompt: kind === "file" ? "Choose a file for this card" : "Choose a folder for this card",
        local, kind });
      if (res.cancelled) return;
      if (res.unavailable) {
        if (kind === "folder" && window.FolderPicker?.choose) {
          const picked = await window.FolderPicker.choose({ title: "Choose a folder" });
          path = typeof picked === "string" ? picked : (picked?.path || "");
        } else {
          path = await forgeDialog({ title: "Type the path", message: res.reason || "No window could open here.",
            value: "", confirm: "Add" });
        }
        if (!path) return;
      } else path = res.path || "";
    } catch (error) {
      return toast(`Could not open a window: ${error.message}`);
    }
    if (!path) return;
    const name = path.replace(/\/+$/, "").split("/").pop() || path;
    addSource({ id: `${kind}:${path}`, kind, name, source_ref: path, description: "" });
  }

  async function loadFindRows(term) {
    const wanted = term.trim();
    if (wanted.length < 3) { state.findRows = []; renderLibrary(); return; }
    try {
      const data = await request(`/api/find?q=${encodeURIComponent(wanted)}&limit=12`);
      if (state.query.trim() !== wanted) return;
      const rows = [];
      Object.entries(data.groups || {}).forEach(([database, group]) => {
        (group.rows || []).forEach((row, index) => {
          const rawNote = row.snippet || row.caption || row.context?.text || row.summary || row.text || "";
          rows.push({
            id: `find:${database}:${row.id || row.path || index}`,
            type: "context", kind: database,
            name: row.title || row.name || row.subject || row.path || "Reference",
            ref: row.path || row.url || row.id || "",
            note: typeof rawNote === "object" ? JSON.stringify(rawNote) : String(rawNote),
          });
        });
      });
      state.findRows = rows;
    } catch (error) {
      state.findRows = [];
    }
    if (state.library === "sources") renderLibrary();
  }

  function loopDot(row) {
    const status = String(row.last_status || "").toLowerCase();
    if (["running", "starting"].includes(status)) return "is-live";
    if (["error", "failed", "interrupted"].includes(status)) return "is-error";
    return row.enabled ? "is-on" : "";
  }

  function stamp(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function renderLoopsTab(body, term) {
    if (!state.loops.length) body.appendChild(make("p", "hint", "Reading the schedule"));
    const rows = state.loops.filter((r) => matches(term, r.name, r.description, r.kind));
    rows.forEach((row) => {
      const last = row.last_run ? `last ${stamp(row.last_run)}${row.last_status ? ` (${row.last_status})` : ""}` : "never run";
      // A paused loop that a Flow runs as a card is not idle: the Flow's
      // schedule is its schedule now, and the line says so.
      const hosts = flowsRunning(row.id);
      const when = hosts.length && !row.enabled
        ? `Run by ${hosts.map((flow) => flow.name).join(", ")}`
        : `${cadenceText(row)}${row.enabled ? "" : ", paused"}`;
      const card = libraryCard({
        title: row.name, iconKey: "loop", active: row.id === state.selectedLoop,
        line: `${when}, ${last}`, dot: loopDot(row), draggable: true,
      });
      card.addEventListener("click", () => openLoop(row.id));
      card.addEventListener("dragstart", (event) => {
        state.libraryDrag = { loop: copy(row) };
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("text/plain", row.name || row.id);
      });
      body.appendChild(card);
    });
    if (state.loops.length && !rows.length) body.appendChild(make("p", "hint", "No loop matches."));
    body.appendChild(macSection());
  }

  // This Mac: every launchd job on the machine, read-only. launchd runs
  // these, not Vira (server/launchdjobs.py reads the three job folders).
  function macSection() {
    const details = make("details", "forge-mac");
    details.open = Boolean(state.macOpen);
    details.appendChild(make("summary", "", "This Mac: launchd jobs, read-only"));
    const box = make("div");
    details.appendChild(box);
    const paint = () => {
      box.replaceChildren();
      const mac = state.mac;
      if (!mac) { box.appendChild(make("p", "hint", "Reading launchd's job folders")); return; }
      if (mac.status === "not_mac") { box.appendChild(make("p", "hint", mac.note || "Not a Mac.")); return; }
      box.appendChild(make("p", "hint", `launchd runs these, not Vira. ~/Library/LaunchAgents, /Library/LaunchAgents and /Library/LaunchDaemons${mac.checked ? `, read ${stamp(mac.checked)}` : ""}.`));
      (mac.groups || []).forEach((group) => {
        if (group.error) box.appendChild(make("p", "hint", `${group.dir}: ${group.error}`));
        (group.jobs || []).forEach((job) => {
          const card = libraryCard({ title: job.label, line: `${job.state || ""}${(job.when || []).length ? ", " + job.when.join("; ") : ""}`,
            iconKey: "command", tone: job.tone === "running" ? "running" : "" });
          card.addEventListener("click", () => {
            const open = card.nextElementSibling?.classList.contains("forge-path");
            if (open) card.nextElementSibling.remove();
            else card.after(make("code", "forge-path", `${job.program || "(no program)"}\n${job.path}${job.error ? `\n${job.error}` : ""}`));
          });
          box.appendChild(card);
        });
      });
    };
    details.addEventListener("toggle", async () => {
      state.macOpen = details.open;
      if (!details.open || state.mac) return;
      paint();
      try { state.mac = await request("/api/routines/background"); }
      catch (error) { state.mac = { status: "error", groups: [], note: error.message }; }
      paint();
    });
    if (details.open) paint();
    return details;
  }

  async function loadLoops() {
    try {
      const data = await request("/api/routines");
      state.loops = data.routines || [];
    } catch (error) {
      toast(`Could not read the schedule: ${error.message}`);
    }
    if (state.library === "loops") renderLibrary();
  }

  function switchLibrary(tab) {
    state.library = tab;
    state.query = "";
    state.findRows = [];
    const search = q("#forge-library-search");
    if (search) search.value = "";
    if (tab === "loops") loadLoops();
    renderLibrary();
  }

  function closeLibrary() {
    q("#forge-library")?.classList.add("is-hidden");
    q("#forge-shell")?.classList.add("library-hidden");
    q("#work-dispatch-pane")?.classList.add("library-away");
  }

  function openLibrary() {
    q("#forge-library")?.classList.remove("is-hidden");
    q("#forge-shell")?.classList.remove("library-hidden");
    q("#work-dispatch-pane")?.classList.remove("library-away");
  }

  /* ---------- cards --------------------------------------------------------- */

  // A card's id is a slug of its name, unique on this board: it is what
  // {{stage.<id>.output}} names in another card's box, so it has to read.
  function cardId(name, type) {
    const base = String(name || type || "card").toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || type || "card";
    const taken = new Set((state.current?.nodes || []).map((node) => node.id));
    let candidate = base;
    let n = 2;
    while (taken.has(candidate)) { candidate = `${base}-${n}`; n += 1; }
    return candidate;
  }

  function inputCard() {
    return (state.current?.nodes || []).find((node) => node.type === "input") || null;
  }

  function newCard(type, point) {
    const base = {
      id: type === "input" ? INPUT_ID : cardId(KIND[type].name, type), type, name: KIND[type].name,
      x: Math.round(point.x), y: Math.round(point.y),
    };
    if (type === "input") base.prompt = "";
    if (type === "agent") Object.assign(base, {
      model: "", provider: "", mode: "manual", read_only: true,
      prompt: "Work on the ask and report what you found.",
      timeout_s: 0, on_timeout: "error", continues: "",
      environment: { instructions: ["project"] },
    });
    if (type === "judge") Object.assign(base, {
      model: "", provider: "", mode: "judge", read_only: true, prompt: "",
      judge: { min_grade: "B", max_retries: 1, retry_stage: "" },
      environment: { instructions: ["project"] },
    });
    if (type === "logic") base.logic = { operation: "contains", value: "" };
    if (type === "approval") base.prompt = "Show me the result so far and ask whether to go on.";
    if (type === "output") { base.output = { destination: "record" }; base.prompt = ""; }
    if (type === "trigger") Object.assign(base, { daily_at: "07:30", every_hours: null, enabled: true, notify: false });
    return base;
  }

  function addCard(type, text) {
    if (!editable()) return;
    if (type === "input" && inputCard()) {
      const existing = inputCard();
      if (text && !existing.prompt) { existing.prompt = text; touch(existing); }
      openNode(existing.id);
      return existing;
    }
    const selected = currentNode();
    let point;
    if (selected && (type === "trigger" || type === "input")) point = { x: selected.x - CARD_W - 90, y: selected.y };
    else if (selected) point = { x: selected.x + CARD_W + 90, y: selected.y };
    else {
      const rect = q("#forge-viewport").getBoundingClientRect();
      point = boardPoint(rect.left + rect.width / 2 - CARD_W / 2, rect.top + rect.height / 2 - 60);
    }
    const node = newCard(type, freeSpot({ x: point.x - 42, y: point.y - 42 }));
    if (type === "input" && text) node.prompt = text;
    node.z = ++state.z;
    state.current.nodes.push(node);
    // A new running card placed from a selected running card is wired from
    // it: that is nearly always what the next card is for. A schedule or
    // the Input is wired INTO the selected card.
    if (selected && EXEC.has(selected.type) && EXEC.has(type)) {
      state.current.edges.push({ id: id("wire"), from: selected.id, to: node.id, instructions: "" });
      if (hasExecutableCycle()) state.current.edges.pop();
    }
    if (selected && (type === "trigger" || type === "input") && EXEC.has(selected.type)
        && !(type === "input" && ["logic", "approval", "output"].includes(selected.type))) {
      state.current.edges.push({ id: id("wire"), from: node.id, to: selected.id, instructions: "" });
    }
    state.selectedNode = node.id;
    state.selectedEdge = null;
    setDirty();
    renderBoard();
    renderInspector();
    return node;
  }

  // The Flows that run a loop as a card (the server: a native stage naming it).
  function flowsRunning(rid) {
    return (state.flows || []).filter((flow) => (flow.nodes || []).some((node) =>
      node.type === "native" && (node.native?.routine_id || node.routine_id) === rid));
  }

  function loopOf(node) {
    const rid = node.native?.routine_id || node.routine_id || "";
    return (state.loops || []).find((row) => row.id === rid) || null;
  }

  // A loop from the Loops tab becomes a Loop card: the Flow runs that loop
  // and waits for it, and the card after gets what it wrote. Wired from the
  // selected running card, as addCard does.
  function addLoop(row, point) {
    if (!editable() || !row?.id) return;
    const selected = currentNode();
    const p = point || (selected
      ? { x: selected.x + CARD_W + 90, y: selected.y }
      : (() => {
        const rect = q("#forge-viewport").getBoundingClientRect();
        return boardPoint(rect.left + rect.width / 2 - CARD_W / 2, rect.top + rect.height / 2 - 60);
      })());
    const spot = freeSpot({ x: p.x - 42, y: p.y - 42 });
    const node = { id: cardId(row.name, "native"), type: "native", name: row.name || "Loop",
      native: { routine_id: row.id }, prompt: "", x: spot.x, y: spot.y, z: ++state.z };
    state.current.nodes.push(node);
    if (selected && EXEC.has(selected.type) && selected.type !== "output") {
      state.current.edges.push({ id: id("wire"), from: selected.id, to: node.id, instructions: "" });
      if (hasExecutableCycle()) state.current.edges.pop();
    }
    state.selectedNode = node.id;
    state.selectedEdge = null;
    setDirty();
    renderBoard();
    renderInspector();
    return node;
  }

  function addSource(item, point) {
    if (!editable()) return;
    const isFind = String(item.id || "").startsWith("find:") || item.type === "context";
    const p = point || (() => {
      const rect = q("#forge-viewport").getBoundingClientRect();
      return boardPoint(rect.left + 80, rect.top + rect.height / 2);
    })();
    const selected = currentNode();
    const node = {
      id: cardId(item.name, isFind ? "context" : "tool"),
      type: isFind ? "context" : "tool",
      name: item.name || item.title || "Source",
      description: isFind ? String(item.note || "").slice(0, 600) : "",
      source: item.kind || (isFind ? "find" : "library"),
      source_ref: item.source_ref || item.invoke || item.ref || item.path || "",
      x: Math.round(p.x), y: Math.round(p.y),
      z: ++state.z,
    };
    if (!point && selected && (selected.type === "agent" || selected.type === "judge")) {
      // added while a card is open: it goes to that card, wired in
      const spot = freeSpot({ x: selected.x - CARD_W - 90 - 42, y: selected.y + 120 - 42 });
      node.x = spot.x; node.y = spot.y;
      state.current.edges.push({ id: id("wire"), from: node.id, to: selected.id, instructions: "" });
    }
    state.current.nodes.push(node);
    state.selectedNode = node.id;
    state.selectedEdge = null;
    setDirty();
    renderBoard();
    renderInspector();
    if (matchMedia("(max-width: 1099px)").matches) closeLibrary();
    return node;
  }

  function addEnvironment(env, point) {
    if (!editable()) return;
    const selected = currentNode();
    const p = point || (selected ? { x: selected.x - CARD_W - 90, y: selected.y + 120 } : (() => {
      const rect = q("#forge-viewport").getBoundingClientRect();
      return boardPoint(rect.left + 80, rect.top + rect.height / 2);
    })());
    const node = {
      id: cardId(env.name, "env"), type: "env", name: env.name || "Environment",
      items: copy(env.items || []), saved_id: env.id || "",
      x: Math.round(p.x), y: Math.round(p.y), z: ++state.z,
    };
    if (!point && selected && (selected.type === "agent" || selected.type === "judge")) {
      state.current.edges.push({ id: id("wire"), from: node.id, to: selected.id, instructions: "" });
    }
    state.current.nodes.push(node);
    state.selectedNode = node.id;
    state.selectedEdge = null;
    setDirty();
    renderBoard();
    renderInspector();
    return node;
  }

  function boardPoint(clientX, clientY) {
    const rect = q("#forge-viewport").getBoundingClientRect();
    return {
      x: (clientX - rect.left - state.panX) / state.zoom,
      y: (clientY - rect.top - state.panY) / state.zoom,
    };
  }

  function modelChoice(node) {
    if (node.model) return MODEL_LABEL[node.model] !== undefined && node.model !== "" ? node.model : "other";
    if (node.provider) return `provider:${node.provider}`;
    return "";
  }

  function defaultLabel() {
    const name = state.defaults?.provider_label || "";
    return name ? `Default: ${name}` : "the configured default";
  }

  function modelLabel(node) {
    if (node.model) return MODEL_LABEL[node.model] || node.model;
    if (node.provider) return PROVIDER_LABEL[node.provider] || node.provider;
    return defaultLabel();
  }

  function permOf(node) {
    if (node.type === "judge" || node.read_only) return "read";
    if (node.mode === "bypassPermissions") return "full";
    if (node.mode === "acceptEdits") return "edit";
    return "ask";
  }

  function setPerm(node, perm) {
    node.read_only = perm === "read";
    node.mode = perm === "full" ? "bypassPermissions" : perm === "edit" ? "acceptEdits" : "manual";
  }

  function wiredIn(node, onlyExec = true) {
    const edges = (state.current?.edges || []).filter((edge) => edge.to === node.id);
    return edges.map((edge) => state.current.nodes.find((item) => item.id === edge.from))
      .filter((item) => item && (!onlyExec || EXEC.has(item.type)));
  }

  function nameOf(nodeId) {
    return state.current?.nodes.find((node) => node.id === nodeId)?.name || nodeId;
  }

  function judgeSentence(node) {
    const judge = node.judge || {};
    if (!judge.min_grade) return "Grades the card before it; the Flow goes on whatever the grade.";
    const retry = judge.retry_stage ? `, below that re-runs ${nameOf(judge.retry_stage)} up to ${judge.max_retries || 0} time${judge.max_retries === 1 ? "" : "s"}` : "";
    return `Passes at ${judge.min_grade} or better${retry}.`;
  }

  function gateSentence(node) {
    const logic = node.logic || {};
    const op = OP_LABEL[logic.operation] || logic.operation || "always passes";
    const value = NO_VALUE_OPS.has(logic.operation) ? "" : ` "${logic.value || ""}"`;
    const subject = logic.subject ? nameOf(logic.subject) : "the card before this";
    return `Passes when ${subject} ${op}${value}.`;
  }

  function cardSummary(node) {
    const body = make("div", "forge-node-body");
    const tags = make("div", "forge-node-meta");
    const tag = (text, cls) => tags.appendChild(make("span", cls || "", text));
    let text = "";
    let path = "";
    if (node.type === "input") {
      text = node.prompt || "";
      if (!text) path = "Empty. Click to write what this Flow should work on.";
    } else if (node.type === "env") {
      tag(`${(node.items || []).length} source${(node.items || []).length === 1 ? "" : "s"}`);
      text = (node.items || []).map((i) => i.name).join(", ");
    } else if (node.type === "agent") {
      tag(modelLabel(node));
      const perm = permOf(node);
      tag(perm, perm === "full" ? "is-full" : "");
      if (node.timeout_s) tag(`${node.timeout_s}s`);
      if (node.continues) tag(`continues ${nameOf(node.continues)}`);
      text = node.prompt || "";
    } else if (node.type === "judge") {
      tag(modelLabel(node));
      text = `${judgeSentence(node)}${node.prompt ? `\n${node.prompt}` : ""}`;
    } else if (node.type === "logic") {
      text = gateSentence(node);
    } else if (node.type === "approval") {
      text = node.prompt || "";
    } else if (node.type === "output") {
      tag(DESTINATION_LABEL[node.output?.destination] || node.output?.destination || "a Vira record");
      text = node.prompt || "";
    } else if (node.type === "trigger") {
      tag(cadenceText(node));
      tag(node.enabled === false ? "paused" : "on", node.enabled === false ? "is-off" : "");
      if (node.notify) tag("tells me");
    } else if (node.type === "native") {
      const loop = loopOf(node);
      if (loop) tag(loop.kind === "command" ? "a program" : "a session");
      text = `Runs the loop ${loop?.name || node.native?.routine_id || node.routine_id || ""} and waits for it. The card after gets its result.`;
    } else {
      path = node.source_ref || "";
      text = node.description || "";
    }
    if (tags.childElementCount) body.appendChild(tags);
    if (path) body.appendChild(make("p", "forge-node-path", path));
    if (text) body.appendChild(make("p", "", text));
    if (!tags.childElementCount && !path && !text) body.appendChild(make("p", "", KIND[node.type]?.hint || ""));
    return body;
  }

  function renderBoard() {
    const world = q("#forge-world");
    const nodeHost = q("#forge-nodes");
    const empty = q("#forge-empty");
    if (!world || !nodeHost) return;
    world.style.transform = `translate(${state.panX}px, ${state.panY}px) scale(${state.zoom})`;
    q("#forge-zoom-value").textContent = `${Math.round(state.zoom * 100)}%`;
    nodeHost.replaceChildren();
    const nodes = state.current?.nodes || [];
    empty.style.display = nodes.length ? "none" : "grid";
    nodes.forEach((node) => nodeHost.appendChild(nodeElement(node)));
    measureCards();
    renderWires();
    syncTools();
  }

  function measureCards() {
    qa(".forge-node", q("#forge-nodes")).forEach((card) => {
      state.heights[card.dataset.nodeId] = card.offsetHeight || state.heights[card.dataset.nodeId] || 120;
    });
  }

  // Repaint one card in place after an edit in the right column, so the
  // board follows the typing without tearing the editor down.
  function paintNode(node) {
    const old = q(`.forge-node[data-node-id="${CSS.escape(node.id)}"]`, q("#forge-nodes"));
    if (!old) return renderBoard();
    old.replaceWith(nodeElement(node));
    measureCards();
    renderWires();
  }

  function nodeElement(node) {
    const meta = KIND[node.type] || KIND.tool;
    const card = make("article", "forge-node");
    card.dataset.nodeId = node.id;
    card.dataset.type = node.type;
    card.style.left = `${node.x}px`;
    card.style.top = `${node.y}px`;
    card.style.zIndex = node.z || 1;
    if (node.id === state.selectedNode) card.classList.add("is-selected");

    const head = make("header", "forge-node-head");
    const title = make("span", "forge-node-copy");
    const kindLabel = SOURCE.has(node.type) ? (SOURCE_LABEL[node.source] || meta.name) : meta.name;
    title.append(make("span", "forge-node-type", kindLabel), make("strong", "forge-node-name", node.name || meta.name));
    head.appendChild(title);
    if (editable()) {
      const menu = make("button", "forge-node-menu", "×");
      menu.type = "button";
      menu.title = "Remove this card";
      menu.addEventListener("pointerdown", (event) => event.stopPropagation());
      menu.addEventListener("click", (event) => { event.stopPropagation(); removeNode(node.id); });
      head.appendChild(menu);
    }
    card.appendChild(head);
    card.appendChild(cardSummary(node));

    // The trace overlay: this run's state, painted on the flow's own card.
    const stage = runStage(node.id);
    if (stage) {
      const status = stage.status || "pending";
      card.classList.add("has-run", `run-${status}`);
      const badge = make("span", "forge-run-badge", status);
      if (stage.job_id) {
        badge.classList.add("is-open");
        badge.title = "Open this step's terminal";
        badge.addEventListener("pointerdown", (event) => event.stopPropagation());
        badge.addEventListener("click", (event) => {
          event.stopPropagation();
          window.openSession?.(stage.job_id);
        });
      }
      card.appendChild(badge);
      if (stage.grade) card.appendChild(make("span", "forge-run-grade", stage.grade));
    }

    if (node.type !== "trigger" && node.type !== "input" && !SOURCE.has(node.type) && node.type !== "env") card.appendChild(port(node, "in"));
    if (node.type !== "output") card.appendChild(port(node, "out"));
    bindNodeDrag(card, head, node);
    card.addEventListener("pointerdown", () => bringNodeFront(node, card));
    card.addEventListener("click", (event) => {
      if (event.target.closest("button,.forge-port")) return;
      openNode(node.id);
    });
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      event.stopPropagation();
      showNodeMenu(node, event);
    });
    return card;
  }

  function bringNodeFront(node, card) {
    node.z = ++state.z;
    card.style.zIndex = node.z;
  }

  /* ---------- the right column: the editor --------------------------------- */

  function field(label, control, grow = false) {
    const wrap = make("label", `forge-field${grow ? " is-grow" : ""}`);
    wrap.append(make("span", "", label), control);
    return wrap;
  }

  function textInput(value, onInput, attrs = {}) {
    const input = make("input");
    input.type = attrs.type || "text";
    Object.entries(attrs).forEach(([key, val]) => { if (key !== "type") input.setAttribute(key, val); });
    input.value = value == null ? "" : value;
    input.addEventListener("input", () => onInput(input.value));
    return input;
  }

  function textArea(value, onInput, rows = 6) {
    const area = make("textarea");
    area.rows = rows;
    area.value = value || "";
    area.addEventListener("input", () => onInput(area.value));
    return area;
  }

  function selectControl(options, value, onChange) {
    const select = make("select");
    options.forEach(([val, label]) => {
      const option = make("option", "", label);
      option.value = val;
      select.appendChild(option);
    });
    select.value = value;
    if (select.value !== value && options.length) select.value = options[0][0];
    select.addEventListener("change", () => onChange(select.value));
    return select;
  }

  function segControl(options, value, onChange, classFor) {
    const wrap = make("div", "forge-seg");
    options.forEach(([val, label]) => {
      const button = make("button", `${val === value ? "on" : ""}${classFor ? ` ${classFor(val)}` : ""}`.trim(), label);
      button.type = "button";
      button.addEventListener("click", () => {
        qa("button", wrap).forEach((item) => item.classList.toggle("on", item === button));
        onChange(val);
      });
      wrap.appendChild(button);
    });
    return wrap;
  }

  function checkbox(label, checked, onChange) {
    const wrap = make("label");
    const input = make("input");
    input.type = "checkbox";
    input.checked = Boolean(checked);
    input.addEventListener("change", () => onChange(input.checked));
    wrap.append(input, document.createTextNode(label));
    return wrap;
  }

  function touch(node) {
    setDirty();
    paintNode(node);
  }

  function setInspector(kind, title, content) {
    q("#forge-inspector-kind").textContent = kind;
    q("#forge-inspector-title").textContent = title;
    const body = q("#forge-inspector-body");
    body.replaceChildren(content);
    q("#forge-inspector").classList.add("is-open");
  }

  function renderInspector() {
    const node = currentNode();
    if (node) return setInspector(KIND[node.type]?.name || "Card", node.name || "", nodeEditor(node));
    const edge = state.current?.edges.find((item) => item.id === state.selectedEdge);
    if (edge) return setInspector("Wire", `${nameOf(edge.from)} to ${nameOf(edge.to)}`, edgeEditor(edge));
    if (state.selectedLoop) {
      const row = state.loops.find((item) => item.id === state.selectedLoop);
      if (row) return setInspector("Standing loop", row.name, loopDetail(row));
    }
    q("#forge-inspector-kind").textContent = "Selection";
    q("#forge-inspector-title").textContent = "Nothing selected";
    q("#forge-inspector-body").replaceChildren(make("p", "hint", "Click a card, a wire, or a loop. It opens here."));
    q("#forge-inspector").classList.remove("is-open");
  }

  function openNode(nodeId) {
    state.selectedNode = nodeId;
    state.selectedEdge = null;
    state.selectedLoop = null;
    qa(".forge-node", q("#forge-nodes")).forEach((item) => item.classList.toggle("is-selected", item.dataset.nodeId === nodeId));
    renderWires();
    renderInspector();
  }

  function openEdge(edge) {
    state.selectedEdge = edge.id;
    state.selectedNode = null;
    state.selectedLoop = null;
    qa(".forge-node", q("#forge-nodes")).forEach((item) => item.classList.remove("is-selected"));
    renderWires();
    renderInspector();
  }

  function closeInspector() {
    state.selectedNode = null;
    state.selectedEdge = null;
    state.selectedLoop = null;
    qa(".forge-node", q("#forge-nodes")).forEach((item) => item.classList.remove("is-selected"));
    renderWires();
    renderInspector();
    if (state.library === "loops") renderLibrary();
  }

  // The pills under an instruction box: everything the card can place in
  // its box, each saying how it arrives. A wired card's text, and the
  // Input card's ask, are appended under the box unless the box places
  // them; a pill inserts the placement at the cursor.
  function refChips(node, area) {
    const wrap = make("div", "forge-refs");
    const insert = (text) => {
      const start = area.selectionStart ?? area.value.length;
      const end = area.selectionEnd ?? start;
      area.value = area.value.slice(0, start) + text + area.value.slice(end);
      area.selectionStart = area.selectionEnd = start + text.length;
      area.dispatchEvent(new Event("input", { bubbles: true }));
      area.focus();
    };
    const paint = () => {
      wrap.replaceChildren(make("span", "", "Insert"));
      const wired = new Set((state.current?.edges || []).filter((e) => e.to === node.id).map((e) => e.from));
      const chip = (label, token, isWired, what) => {
        const placed = area.value.includes(token);
        const button = make("button", placed ? "is-placed" : (isWired ? "" : "is-away"), label);
        button.type = "button";
        button.appendChild(make("small", "", placed ? "placed" : (isWired ? "at the end" : "not wired")));
        button.title = placed ? `${what} is placed where the token sits`
          : isWired ? `${what} is appended under the box; click to place it`
            : `${what} is not wired into this card; click to place it anyway`;
        button.addEventListener("click", () => insert(token));
        wrap.appendChild(button);
      };
      chip("Input", "{{input}}", wired.has(INPUT_ID), "The ask");
      (state.current?.nodes || []).filter((item) => wired.has(item.id) && EXEC.has(item.type)
        && !["logic", "approval", "output"].includes(item.type) && item.id !== node.id)
        .forEach((item) => chip(item.name, `{{stage.${item.id}.output}}`, true, `The finished text of ${item.name}`));
    };
    paint();
    area.addEventListener("input", paint);
    return wrap;
  }

  // The environment rows on an agent or judge card: the instruction file
  // (the only default-on source), then what is wired in, by kind. Nothing
  // else reaches the session.
  function environmentRows(node) {
    const env = node.environment || (node.environment = { instructions: ["project"] });
    if (!Array.isArray(env.instructions)) env.instructions = ["project"];
    const box = make("div", "forge-envrows");
    box.appendChild(make("span", "forge-card-kind", "Environment"));
    const row = make("div", "forge-envrow");
    const peeks = make("div", "forge-envpeeks");
    const paintRow = () => {
      row.replaceChildren();
      peeks.replaceChildren();
      const on = env.instructions.includes("project");
      row.appendChild(checkbox("the working folder's AGENTS.md or CLAUDE.md", on, (value) => {
        env.instructions = env.instructions.filter((i) => i !== "project");
        if (value) env.instructions.unshift("project");
        touch(node); paintRow();
      }));
      row.appendChild(peekChip("project", "instructions", peeks));
      const custom = env.instructions.find((i) => i !== "project") || "";
      const replace = make("button", "fchip sm", custom ? "Change file" : "Add a file");
      replace.type = "button";
      replace.addEventListener("click", async () => {
        const path = await pickOnThisMac("Choose an instruction file", "file", custom);
        if (!path) return;
        env.instructions = env.instructions.filter((i) => i === "project").concat([path]);
        touch(node); paintRow();
      });
      row.appendChild(replace);
      if (custom) {
        const drop = make("button", "fchip sm", "Drop the file");
        drop.type = "button";
        drop.addEventListener("click", () => { env.instructions = env.instructions.filter((i) => i === "project"); touch(node); paintRow(); });
        row.appendChild(drop);
        row.appendChild(peekChip(custom, "instructions", peeks));
        row.appendChild(make("code", "forge-path", custom));
      }
    };
    paintRow();
    box.append(row, peeks);
    const wired = wiredIn(node, false).filter((item) => SOURCE.has(item.type) || item.type === "env");
    const byKind = {};
    wired.forEach((item) => {
      const items = item.type === "env" ? (item.items || []) : [{ kind: item.source, name: item.name }];
      items.forEach((entry) => {
        const kind = ENV_KINDS.has(entry.kind) ? (SOURCE_LABEL[entry.kind] || entry.kind) : "Told about";
        (byKind[kind] = byKind[kind] || []).push(entry.name);
      });
    });
    const line = Object.entries(byKind).map(([kind, names]) => `${kind}: ${names.join(", ")}`).join(". ");
    box.appendChild(make("p", "hint", line
      ? `${line}. Nothing else reaches this card.`
      : "No sources wired in: this card sees no skills, settings or plugins. Add one from Sources, or wire a saved environment."));
    const save = make("button", "fchip sm", "Save these as an environment");
    save.type = "button";
    save.disabled = !wired.length;
    save.addEventListener("click", () => saveEnvironmentFrom(node, wired));
    box.appendChild(save);
    return box;
  }

  async function saveEnvironmentFrom(node, wired) {
    const items = [];
    wired.forEach((item) => {
      if (item.type === "env") (item.items || []).forEach((entry) => items.push(copy(entry)));
      else items.push({ kind: item.source || "library", name: item.name, source_ref: item.source_ref || "" });
    });
    const name = await forgeDialog({ title: "Save an environment", message: `${items.length} source${items.length === 1 ? "" : "s"} wired into ${node.name}, as one card you can wire anywhere.`, value: `${node.name} kit`, confirm: "Save" });
    if (!name) return;
    try {
      const saved = await send("/api/flows/environments", "POST", { name, items });
      state.envs = state.envs.filter((env) => env.id !== saved.id).concat([saved]);
      toast(`Saved "${saved.name}" under Sources`);
      if (state.library === "sources") renderLibrary();
    } catch (error) {
      toast(`Could not save it: ${error.message}`);
    }
  }

  function nameField(node) {
    return field("Name", textInput(node.name, (value) => { node.name = value; touch(node); renderIdentity(); }));
  }

  // The run's text, pasted onto the board: a link, a question, anything.
  // It fills an empty Input card, or makes one.
  function pasteAsInput(text) {
    const trimmed = String(text || "").trim();
    if (!trimmed || !editable()) return false;
    const existing = inputCard();
    if (existing) {
      if (existing.prompt && existing.prompt !== trimmed) {
        existing.prompt = `${existing.prompt}\n${trimmed}`;
      } else existing.prompt = trimmed;
      touch(existing);
      openNode(existing.id);
    } else addCard("input", trimmed);
    toast("Pasted into the Input card");
    return true;
  }

  function modelField(node, editor) {
    const choice = modelChoice(node);
    const wrap = make("div");
    const other = textInput(node.model || "", (value) => { node.model = value.trim(); touch(node); }, { placeholder: "the model id" });
    const otherField = field("Model id", other);
    otherField.style.display = choice === "other" ? "" : "none";
    otherField.style.marginTop = "8px";
    const choices = MODEL_CHOICES.map(([value, label]) => [value, value === "" ? defaultLabel() : label]);
    const select = selectControl(choices, choice, (value) => {
      if (value.startsWith("provider:")) { node.model = ""; node.provider = value.slice(9); }
      else if (value === "other") { node.model = node.model && !MODEL_LABEL[node.model] ? node.model : ""; }
      else { node.model = value; node.provider = ""; }
      otherField.style.display = value === "other" ? "" : "none";
      if (value === "other") other.focus();
      touch(node);
    });
    wrap.append(field("Who", select), otherField);
    return wrap;
  }

  function agentEditor(node) {
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const row = make("div", "forge-row");
    const permHint = make("p", "hint", PERM_HINT[permOf(node)]);
    row.append(modelField(node, editor), field("May it change things", segControl(PERMS, permOf(node), (value) => {
      setPerm(node, value);
      permHint.textContent = PERM_HINT[value];
      touch(node);
    }, (val) => (val === "full" ? "is-full" : ""))));
    editor.append(row, permHint);
    const area = textArea(node.prompt, (value) => { node.prompt = value; touch(node); }, 12);
    editor.appendChild(field("What it must do", area, true));
    editor.appendChild(refChips(node, area));
    editor.appendChild(environmentRows(node));
    const advanced = make("details");
    advanced.appendChild(make("summary", "", "Advanced: time limit, continue a conversation"));
    const inner = make("div");
    inner.appendChild(field("Give up after (seconds, 0 for never)", textInput(String(node.timeout_s || 0), (value) => {
      const n = parseInt(value, 10);
      node.timeout_s = Number.isFinite(n) && n > 0 ? n : 0;
      touch(node);
    }, { type: "number", min: "0" })));
    inner.appendChild(field("When the time runs out", selectControl([["error", "the card fails"], ["continue", "what it wrote so far stands"]],
      node.on_timeout || "error", (value) => { node.on_timeout = value; touch(node); })));
    const others = wiredIn(node).filter((item) => item.type === "agent");
    inner.appendChild(field("Continue the conversation of", selectControl(
      [["", "nobody: start fresh"]].concat(others.map((item) => [item.id, item.name])),
      node.continues || "", (value) => { node.continues = value; touch(node); })));
    inner.appendChild(make("p", "hint", "A continued card is the next turn of that card's session, on the same model. It must be wired from the card it continues."));
    advanced.appendChild(inner);
    editor.appendChild(advanced);
    return editor;
  }

  function judgeEditor(node) {
    const judge = node.judge || (node.judge = { min_grade: "B", max_retries: 1, retry_stage: "" });
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    editor.appendChild(modelField(node, editor));
    const sentence = make("p", "forge-sentence");
    const paint = () => {
      sentence.replaceChildren();
      sentence.append("Grades the card before this with fresh eyes. Pass at ");
      sentence.appendChild(selectControl(GRADES, judge.min_grade || "", (value) => { judge.min_grade = value; touch(node); paint(); }));
      if (judge.min_grade) {
        sentence.append(" or better; below that, re-run ");
        const targets = wiredIn(node).filter((item) => item.type !== "trigger");
        sentence.appendChild(selectControl([["", "nothing: just fail"]].concat(targets.map((item) => [item.id, item.name])),
          judge.retry_stage || "", (value) => { judge.retry_stage = value; touch(node); paint(); }));
        if (judge.retry_stage) {
          sentence.append(" up to ");
          sentence.appendChild(selectControl([1, 2, 3, 4, 5].map((n) => [String(n), String(n)]), String(judge.max_retries || 1),
            (value) => { judge.max_retries = Number(value); touch(node); }));
          sentence.append(` time${judge.max_retries === 1 ? "" : "s"} with the findings.`);
        } else sentence.append(".");
      } else sentence.append(": the Flow goes on whatever the grade.");
    };
    paint();
    editor.appendChild(sentence);
    const area = textArea(node.prompt, (value) => { node.prompt = value; touch(node); }, 8);
    editor.appendChild(field("What to weigh in particular (optional)", area, true));
    editor.appendChild(make("p", "hint", "The judge reads the finished text of every card wired into it and grades it A to F against what the Flow was asked."));
    editor.appendChild(environmentRows(node));
    return editor;
  }

  function gateEditor(node) {
    const logic = node.logic || (node.logic = { operation: "contains", value: "" });
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const sentence = make("p", "forge-sentence");
    const paint = () => {
      sentence.replaceChildren();
      const sources = wiredIn(node).filter((item) => item.type !== "trigger");
      sentence.append("Passes when ");
      if (sources.length > 1) {
        sentence.appendChild(selectControl([["", "the card before this"]].concat(sources.map((item) => [item.id, item.name])),
          logic.subject || "", (value) => { if (value) logic.subject = value; else delete logic.subject; touch(node); }));
      } else sentence.append("the card before this ");
      const op = make("select");
      logicControls().forEach(([groupLabel, ops]) => {
        const group = make("optgroup"); group.label = groupLabel;
        ops.forEach(([value, label]) => {
          const option = make("option", "", label); option.value = value; group.appendChild(option);
        });
        op.appendChild(group);
      });
      op.value = logic.operation || "always";
      op.addEventListener("change", () => { logic.operation = op.value; touch(node); paint(); });
      sentence.appendChild(op);
      if (!NO_VALUE_OPS.has(logic.operation)) {
        sentence.append(" ");
        sentence.appendChild(textInput(logic.value || "", (value) => { logic.value = value; touch(node); }, { placeholder: "value", class: "forge-wide" }));
      }
      sentence.append(".");
    };
    paint();
    editor.appendChild(sentence);
    editor.appendChild(make("p", "hint", "No model runs. A gate that fails stops every card after it and the run ends with an error."));
    return editor;
  }

  function approvalEditor(node) {
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const area = textArea(node.prompt, (value) => { node.prompt = value; touch(node); }, 8);
    editor.appendChild(field("What to show me and ask", area, true));
    editor.appendChild(make("p", "hint", "The run pauses here and raises a card in Attention. Decline ends the run."));
    return editor;
  }

  function outputEditor(node) {
    const output = node.output || (node.output = { destination: "record" });
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const sentence = make("p", "forge-sentence");
    sentence.append("Save the result as ");
    const planNote = make("p", "hint", "A plan dossier is saved to your vault as an editable note and rendered as an HTML page with diagrams.");
    const paintNote = () => { planNote.style.display = output.destination === "plan" ? "" : "none"; };
    sentence.appendChild(selectControl(DESTINATIONS, output.destination || "record", (value) => { output.destination = value; paintNote(); touch(node); }));
    sentence.append(".");
    paintNote();
    editor.append(sentence, planNote);
    const area = textArea(node.prompt, (value) => { node.prompt = value; touch(node); }, 6);
    editor.appendChild(field("Shape it like this (optional)", area, true));
    editor.appendChild(make("p", "hint", "The card feeding this one is told the shape before it writes."));
    return editor;
  }

  function scheduleEditor(node) {
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const sentence = make("p", "forge-sentence");
    const paint = () => {
      sentence.replaceChildren();
      const kind = node.daily_at ? "daily" : (node.every_hours ? "hours" : "manual");
      sentence.append("Run ");
      sentence.appendChild(selectControl([["daily", "every day at"], ["hours", "every"], ["manual", "only when I run it"]], kind, (value) => {
        if (value === "daily") { node.daily_at = "07:30"; node.every_hours = null; }
        else if (value === "hours") { node.daily_at = ""; node.every_hours = 24; }
        else { node.daily_at = ""; node.every_hours = null; }
        touch(node); paint();
      }));
      if (kind === "daily") {
        sentence.append(" ");
        sentence.appendChild(textInput(node.daily_at, (value) => { node.daily_at = value; touch(node); }, { type: "time" }));
      } else if (kind === "hours") {
        sentence.append(" ");
        sentence.appendChild(textInput(String(node.every_hours || 24), (value) => { node.every_hours = Number(value) || 1; touch(node); }, { type: "number", min: "0.25", step: "0.25" }));
        sentence.append(" hours");
      }
      sentence.append(". ");
      sentence.appendChild(checkbox("on", node.enabled !== false, (value) => { node.enabled = value; touch(node); }));
      sentence.appendChild(checkbox("tell me when it has run", node.notify, (value) => { node.notify = value; touch(node); }));
    };
    paint();
    editor.appendChild(sentence);
    editor.appendChild(make("p", "hint", node.routine_id
      ? "This schedule is a standing loop; Save applies a change. Pause or Run now from the Loops tab."
      : "Save the Flow to put it on the schedule. It then appears in Loops."));
    return editor;
  }

  function sourceEditor(node) {
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const area = textArea(node.description, (value) => { node.description = value; touch(node); }, 3);
    editor.appendChild(field("Tell the card about it (optional)", area));
    editor.appendChild(make("p", "hint", ENV_KINDS.has(node.source)
      ? `Wired into a card, this is part of that card's environment (${SOURCE_LABEL[node.source] || node.source}).`
      : "Wired to a card, the card is told its name, its path and this note. It reads the file itself."));
    if (node.type === "tool" && (node.source === "instructions" || node.source === "settings")) {
      editor.appendChild(ownFileRow(node));
    }
    if (node.type === "tool" && node.source === "plugin" && node.source_ref) {
      editor.appendChild(pluginSwitches(node));
    }
    if (node.type === "tool" && node.source_ref) {
      editor.appendChild(previewBlock(node.source_ref, node.source));
    } else if (node.type === "tool") {
      editor.appendChild(make("code", "forge-path", "(no path)"));
    } else if (node.source_ref) {
      editor.appendChild(make("code", "forge-path", node.source_ref));
    }
    return editor;
  }

  function atTheKeyboard() {
    return ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  }

  // The Mac's own open panel, through the server that runs here; over the
  // tailnet the panel would open on the wrong screen, so a typed path.
  async function pickOnThisMac(prompt, kind = "file", value = "") {
    try {
      const res = await send("/api/pick-folder", "POST", { prompt, local: atTheKeyboard(), kind });
      if (res.cancelled) return "";
      if (res.unavailable) {
        return await forgeDialog({ title: "Type the file's path", message: res.reason || "", value, confirm: "Use it" }) || "";
      }
      return res.path || "";
    } catch (error) {
      toast(`Could not open a window: ${error.message}`);
      return "";
    }
  }

  // An instruction or settings card can stand for the owner's own file
  // instead of the stock one; the stock ref is kept so it can come back.
  function ownFileRow(node) {
    const row = make("div", "forge-editor-actions");
    const pick = make("button", "fchip sm", "Use my own file");
    pick.type = "button";
    pick.addEventListener("click", async () => {
      const path = await pickOnThisMac(node.source === "settings" ? "Choose a settings file (.json)" : "Choose an instruction file");
      if (!path) return;
      if (node.source === "settings" && !/\.json$/i.test(path)) return toast("A settings file ends in .json");
      if (!node.stock_ref) node.stock_ref = node.source_ref;
      node.source_ref = path;
      touch(node);
      renderInspector();
    });
    row.appendChild(pick);
    if (node.stock_ref) {
      const back = make("button", "fchip sm", "Back to the stock file");
      back.type = "button";
      back.addEventListener("click", () => {
        node.source_ref = node.stock_ref;
        node.stock_ref = "";
        touch(node);
        renderInspector();
      });
      row.appendChild(back);
    }
    return row;
  }

  // A plugin's skills, each with a switch: the SDK's skill filter honors
  // that. Its hooks and commands come with the plugin as a set.
  function pluginSwitches(node) {
    const box = make("div", "forge-envrows");
    box.appendChild(make("span", "forge-card-kind", "Its skills"));
    const rows = make("div", "forge-envrow", "Reading the plugin…");
    box.appendChild(rows);
    request(`/api/flows/kit/preview?ref=${encodeURIComponent(node.source_ref)}&kind=plugin`).then((shown) => {
      rows.replaceChildren();
      const skills = (shown.listing && shown.listing.skills) || [];
      if (!skills.length) { rows.textContent = "No skills in it."; return; }
      const off = new Set(node.skills_off || []);
      skills.forEach((skill) => {
        rows.appendChild(checkbox(skill, !off.has(skill), (on) => {
          if (on) off.delete(skill); else off.add(skill);
          node.skills_off = [...off];
          touch(node);
        }));
      });
      const hooks = (shown.listing && shown.listing.hooks) || [];
      if (hooks.length) rows.appendChild(make("small", "", `Hooks come with it: ${hooks.join(", ")}.`));
    }).catch((error) => { rows.textContent = `Could not read it: ${error.message}`; });
    return box;
  }

  // The file behind a source card, shown in the column: markdown through
  // the renderer the vault uses, a settings file as an outline, a plugin
  // folder as what it holds. Open puts the file in Vira's viewer.
  function previewBlock(ref, kind) {
    const box = make("div", "forge-preview");
    const head = make("div", "forge-preview-head");
    const query = `ref=${encodeURIComponent(ref)}&kind=${encodeURIComponent(kind || "")}`;
    const open = make("button", "fchip sm", "Open");
    open.type = "button";
    open.addEventListener("click", () => {
      if (typeof openViewer === "function") openViewer(`/api/flows/kit/file?${query}`);
      else window.open(`/api/flows/kit/file?${query}`, "_blank");
    });
    head.appendChild(open);
    if (atTheKeyboard() && kind !== "mcp") {
      const finder = make("button", "fchip sm", "Show in Finder");
      finder.type = "button";
      finder.addEventListener("click", async () => {
        try { await send("/api/flows/kit/reveal", "POST", { ref, kind: kind || "" }); }
        catch (error) { toast(`Could not show it: ${error.message}`); }
      });
      head.appendChild(finder);
    }
    const body = make("div", "forge-preview-body", "Reading…");
    box.append(head, body);
    request(`/api/flows/kit/preview?${query}`).then((shown) => {
      body.replaceChildren();
      paintPreview(body, shown);
      if (!shown.exists || shown.format === "binary") head.hidden = true;
    }).catch((error) => {
      body.textContent = `Could not read it: ${error.message}`;
      head.hidden = true;
    });
    return box;
  }

  function outlineList(rows) {
    const list = make("ul", "forge-outline");
    rows.forEach(([key, value]) => {
      const li = make("li");
      li.append(make("b", "", key), make("span", "", value));
      list.appendChild(li);
    });
    return list;
  }

  function paintPreview(body, shown) {
    body.appendChild(make("code", "forge-path", shown.path || ""));
    if (!shown.exists) { body.appendChild(make("p", "hint", "Not on this Mac.")); return; }
    if (shown.format === "mcp") {
      const server = shown.server || {};
      body.appendChild(outlineList([["Type", server.type], ["Starts with", server.command], ["URL", server.url],
        ["Scope", server.scope], ["Env", (server.env || []).join(", ")]].filter(([, value]) => value)));
      return;
    }
    if (shown.format === "binary") { body.appendChild(make("p", "hint", "Not a text file.")); return; }
    if (shown.format === "folder") {
      const listing = shown.listing;
      if (listing) {
        if (listing.description) body.appendChild(make("p", "forge-preview-text", listing.description));
        body.appendChild(outlineList([["Skills", listing.skills], ["Commands", listing.commands],
          ["Agents", listing.agents], ["Hooks", listing.hooks]].map(([key, names]) => [key, names.length ? names.join(", ") : "none"])));
      }
      if (shown.text) body.appendChild(markdownBlock(shown.text));
      const files = shown.files || [];
      if (files.length) {
        const details = make("details");
        details.appendChild(make("summary", "", `${files.length}${shown.more_files ? "+" : ""} files`));
        const inner = make("div");
        inner.appendChild(make("pre", "", files.join("\n")));
        details.appendChild(inner);
        body.appendChild(details);
      }
      return;
    }
    if (shown.format === "json") {
      const rows = (shown.outline || []).map((row) => [row.key, row.summary]);
      body.appendChild(rows.length ? outlineList(rows) : make("p", "hint", shown.outline_error || "Nothing in it."));
      const details = make("details");
      details.appendChild(make("summary", "", "The file itself"));
      const inner = make("div");
      inner.appendChild(make("pre", "", shown.text || ""));
      details.appendChild(inner);
      body.appendChild(details);
    } else if (shown.format === "markdown") {
      body.appendChild(markdownBlock(shown.text));
    } else {
      body.appendChild(make("pre", "", shown.text || "(empty)"));
    }
    if (shown.truncated) body.appendChild(make("p", "hint", "Cut at the preview limit; Open shows the whole file."));
  }

  function markdownBlock(text) {
    const box = make("div", "forge-md");
    if (typeof mdToHtml === "function") box.innerHTML = mdToHtml(text);
    else box.appendChild(make("pre", "", text));
    return box;
  }

  // Show / Hide for a file named in a row: the preview opens under it.
  function peekChip(ref, kind, host) {
    const chip = make("button", "fchip sm", "Show");
    chip.type = "button";
    let open = null;
    chip.addEventListener("click", () => {
      if (open) { open.remove(); open = null; chip.textContent = "Show"; return; }
      open = previewBlock(ref, kind);
      host.appendChild(open);
      chip.textContent = "Hide";
    });
    return chip;
  }

  function inputEditor(node) {
    const editor = make("div", "forge-editor");
    const area = textArea(node.prompt, (value) => { node.prompt = value; touch(node); }, 10);
    area.placeholder = "A link, a question, an idea: what this Flow works on.";
    editor.appendChild(field("What should this Flow work on?", area, true));
    editor.appendChild(make("p", "hint", "Run sends this. Wire it into the first card; that card gets it under its box unless the box places it. Paste a link onto the board to fill it."));
    setTimeout(() => area.focus(), 0);
    return editor;
  }

  function envEditor(node) {
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const list = make("ul", "forge-envlist");
    const paint = () => {
      list.replaceChildren();
      (node.items || []).forEach((item, index) => {
        const li = make("li");
        const head = make("div", "forge-envhead");
        const text = make("span", "", item.name);
        text.appendChild(make("small", "", ` ${SOURCE_LABEL[item.kind] || item.kind}`));
        const actions = make("span", "forge-envactions");
        if (item.source_ref) actions.appendChild(peekChip(item.source_ref, item.kind, li));
        const drop = make("button", "fchip sm", "Remove");
        drop.type = "button";
        drop.addEventListener("click", () => { node.items.splice(index, 1); touch(node); paint(); });
        actions.appendChild(drop);
        head.append(text, actions);
        li.appendChild(head);
        list.appendChild(li);
      });
      if (!(node.items || []).length) list.appendChild(make("li", "", "Nothing in it."));
    };
    paint();
    editor.appendChild(list);
    editor.appendChild(make("p", "hint", "Wire this into a card and every source in it is wired in. Changing it here changes this card only; the saved environment under Sources stays as it was."));
    if (node.saved_id) {
      const forget = make("button", "fchip sm is-danger", "Forget the saved environment");
      forget.type = "button";
      forget.addEventListener("click", async () => {
        if (!await forgeDialog({ title: "Forget it?", message: "The saved environment leaves Sources. Cards already on boards keep their copies.", confirm: "Forget" })) return;
        try {
          await send(`/api/flows/environments/${encodeURIComponent(node.saved_id)}`, "DELETE", {});
          state.envs = state.envs.filter((env) => env.id !== node.saved_id);
          node.saved_id = "";
          toast("Forgotten");
          renderLibrary(); renderInspector();
        } catch (error) { toast(`Could not forget it: ${error.message}`); }
      });
      editor.appendChild(forget);
    }
    return editor;
  }

  function nativeEditor(node) {
    const editor = make("div", "forge-editor");
    editor.appendChild(nameField(node));
    const rid = node.native?.routine_id || node.routine_id || "";
    const loop = loopOf(node);
    editor.appendChild(make("code", "forge-path", loop
      ? `${loop.name}: ${loop.kind === "command" ? "a program" : loop.kind}, ${cadenceText(loop).toLowerCase()}${loop.enabled ? "" : ", paused"}`
      : `Loop ${rid}`));
    editor.appendChild(make("p", "hint", loop?.kind === "command"
      ? "Runs the loop's program and waits for it to finish. The card after gets the run in words: the outcome, the counts, the errors and the last lines it wrote. If the program is already going, this card waits for that run."
      : "Runs the loop as a step of this Flow and waits for it. The card after gets what it wrote."));
    const open = make("button", "fchip sm", "Open in Loops");
    open.type = "button";
    open.addEventListener("click", () => { openLibrary(); switchLibrary("loops"); openLoop(rid); });
    editor.appendChild(open);
    return editor;
  }

  function nodeEditor(node) {
    if (!editable()) {
      const view = make("div", "forge-editor");
      view.appendChild(cardSummary(node));
      if (node.prompt) view.appendChild(make("pre", "", node.prompt));
      view.appendChild(make("p", "hint", "A traced run's card, read-only."));
      return view;
    }
    const editor = node.type === "agent" ? agentEditor(node)
      : node.type === "input" ? inputEditor(node)
        : node.type === "env" ? envEditor(node)
      : node.type === "judge" ? judgeEditor(node)
        : node.type === "logic" ? gateEditor(node)
          : node.type === "approval" ? approvalEditor(node)
            : node.type === "output" ? outputEditor(node)
              : node.type === "trigger" ? scheduleEditor(node)
                : node.type === "native" ? nativeEditor(node)
                  : sourceEditor(node);
    const actions = make("div", "forge-editor-actions");
    const dup = make("button", "fchip sm", "Duplicate");
    dup.type = "button";
    dup.addEventListener("click", () => duplicateNode(node));
    const remove = make("button", "fchip sm is-danger", "Remove card");
    remove.type = "button";
    remove.addEventListener("click", () => removeNode(node.id));
    actions.append(dup, remove);
    editor.appendChild(actions);
    return editor;
  }

  function edgeEditor(edge) {
    const editor = make("div", "forge-editor");
    editor.appendChild(make("p", "hint", `The finished text of ${nameOf(edge.from)} goes into ${nameOf(edge.to)}.`));
    if (editable()) {
      editor.appendChild(field("Note for the card after (optional)", textArea(edge.instructions, (value) => {
        edge.instructions = value; setDirty(); renderWires();
      }, 5), true));
      editor.appendChild(make("p", "hint", "The note is added to that card's instructions under CONNECTION INSTRUCTIONS."));
      const actions = make("div", "forge-editor-actions");
      const remove = make("button", "fchip sm is-danger", "Remove wire");
      remove.type = "button";
      remove.addEventListener("click", () => removeEdge(edge.id));
      actions.appendChild(remove);
      editor.appendChild(actions);
    }
    return editor;
  }

  /* ---------- standing loops in the right column --------------------------- */

  const LIVE_RUN = new Set(["starting", "running"]);
  const RUN_POLL_MS = 5000;
  const took = (a, b) => {
    if (!a || !b) return "";
    const s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  };
  const untilText = (iso) => {
    const ms = new Date(iso) - Date.now();
    if (Number.isNaN(ms)) return "";
    if (ms <= 60000) return "within a minute";
    const m = Math.round(ms / 60000);
    return m < 90 ? `in ${m} min` : `in ${Math.floor(m / 60)} h ${m % 60} min`;
  };
  let loopTimer = null;

  function openLoop(rid) {
    state.selectedLoop = rid;
    state.selectedNode = null;
    state.selectedEdge = null;
    qa(".forge-node", q("#forge-nodes")).forEach((item) => item.classList.remove("is-selected"));
    renderWires();
    renderLibrary();
    renderInspector();
  }

  function loopDetail(row) {
    const rid = row.id;
    const wrap = make("div", "forge-editor");
    const stateLine = make("p", "forge-loop-state");
    const actions = make("div", "forge-editor-actions");
    const runNow = make("button", "btn primary", "Run now");
    runNow.type = "button";
    const pause = make("button", "fchip sm", "");
    pause.type = "button";
    actions.append(runNow, pause);
    if (editable()) {
      const add = make("button", "fchip sm", "Add to this Flow");
      add.type = "button";
      add.title = "A Loop card that runs this loop and waits for it";
      add.addEventListener("click", () => addLoop(row));
      actions.appendChild(add);
    }
    if (row.last_job) {
      const open = make("button", "fchip sm", "Open the last session");
      open.type = "button";
      open.addEventListener("click", () => window.openSession?.(row.last_job));
      actions.appendChild(open);
    }
    wrap.append(stateLine, actions);
    if (row.description) wrap.appendChild(make("p", "hint", row.description));
    const facts = make("div", "forge-facts");
    const runsBox = make("div");
    const hood = make("details");
    hood.appendChild(make("summary", "", "Under the hood"));
    const hoodBody = make("div");
    hood.appendChild(hoodBody);
    wrap.append(facts, runsBox, hood);

    let data = { ...row, runs: [], total: 0 };
    let older = [];
    const open = new Set();
    const isLive = () => Boolean(data.live) || (data.runs || []).some((r) => LIVE_RUN.has(r.status))
      || LIVE_RUN.has(String(data.last_status || ""));

    function paintState() {
      const cadence = cadenceText(data);
      if (isLive()) {
        stateLine.textContent = "Running now. It runs on its own, so closing this panel does not stop it.";
      } else if (!data.enabled) {
        stateLine.textContent = `Paused. Vira will not start it on its schedule (${cadence.toLowerCase()}). Run now still works.`;
      } else {
        stateLine.textContent = data.next_run
          ? `${cadence}. Next run ${stamp(data.next_run)} (${untilText(data.next_run)}).`
          : `${cadence}.${data.last_run ? ` Last run ${stamp(data.last_run)}${data.last_status ? `, ${data.last_status}` : ""}.` : ""}`;
      }
      runNow.disabled = isLive();
      pause.textContent = data.enabled ? "Pause" : "Resume";
      pause.title = data.enabled ? "Stop starting it on its schedule" : "Start it on its schedule again";
    }
    function paintFacts() {
      facts.replaceChildren();
      (data.facts || []).forEach((fact) => {
        const tile = make("div");
        tile.append(make("span", "", fact.label), make("strong", "", fact.value));
        facts.appendChild(tile);
      });
      hoodBody.replaceChildren();
      const steps = make("ol", "forge-steps");
      (data.steps || []).forEach((step) => {
        const li = make("li"); li.append(make("strong", "", step.title), make("p", "", step.body)); steps.appendChild(li);
      });
      if (steps.childElementCount) hoodBody.appendChild(steps);
      (data.parts || []).forEach((part) => {
        const details = make("details");
        details.appendChild(make("summary", "", `${part.title}, ${part.kind}${part.lines ? `, ${part.lines} lines` : ""}`));
        const inner = make("div");
        if (part.note) inner.appendChild(make("p", "hint", part.note));
        if (part.path) inner.appendChild(make("code", "", `${part.path}${part.symbol ? ` (${part.symbol})` : ""}`));
        inner.appendChild(make("pre", "", part.text || "(no source text)"));
        details.appendChild(inner);
        hoodBody.appendChild(details);
      });
      if (!hoodBody.childElementCount) hoodBody.appendChild(make("p", "hint", "Nothing to show yet."));
    }
    function counts(r) {
      const c = r.counts || {};
      const keys = Object.keys(c);
      return keys.length ? keys.map((k) => `${c[k]} ${k}`).join(", ") : "-";
    }
    function logRow(r, cell) {
      cell.replaceChildren(make("p", "hint", "Loading this run's log"));
      request(`/api/routines/${encodeURIComponent(rid)}/runs/${encodeURIComponent(r.id)}/log`).then((res) => {
        cell.replaceChildren();
        if (res.run?.note) cell.appendChild(make("p", "hint", res.run.note));
        if (res.run?.errors?.length) {
          const list = make("ul", "forge-run-errors");
          res.run.errors.forEach((e) => list.appendChild(make("li", "", e)));
          cell.append(make("strong", "", `Errors (${res.run.error_count})`), list);
        }
        (res.files || []).forEach((f) => {
          const title = f.source ? `${f.source} (this run's new lines)` : "Output";
          cell.appendChild(make("strong", "", `${title}${f.cut ? ", last 1 MB" : ""}`));
          cell.appendChild(make("pre", "", f.text || "(empty)"));
        });
        if (!(res.files || []).length) cell.appendChild(make("p", "hint", "This run wrote no log."));
      }).catch((e) => cell.replaceChildren(make("p", "hint", `Could not load the log: ${e.message}`)));
    }
    function paintRuns() {
      runsBox.replaceChildren();
      if (data.engine !== "command") return;
      runsBox.appendChild(make("span", "forge-card-kind", "Runs"));
      const list = make("div", "forge-runs");
      const rows = [...(data.runs || []), ...older];
      rows.forEach((r) => {
        const rowEl = make("div", `forge-run run-${r.status}`);
        const top = make("div", "forge-run-top");
        const end = LIVE_RUN.has(r.status) ? "still running" : stamp(r.finished);
        const span = took(r.started, r.finished);
        top.append(make("span", `forge-run-chip is-${r.status}`, r.status),
          make("span", "", `${stamp(r.started || r.queued)} to ${end}${span ? ` (${span})` : ""}`),
          make("span", "forge-run-dim", r.trigger === "schedule" ? "schedule" : "Run now"),
          make("span", "forge-run-dim", `exit ${r.exit_code == null ? "-" : r.exit_code}`));
        const bottom = make("div", "forge-run-bottom");
        bottom.appendChild(make("span", "", counts(r)));
        const err = make("span", r.error_count ? "has-errors" : "forge-run-dim",
          `${r.error_count || 0} error line${r.error_count === 1 ? "" : "s"}`);
        if (r.errors?.length) err.title = r.errors[0];
        bottom.appendChild(err);
        const log = make("button", "fchip sm", open.has(r.id) ? "Hide log" : "Log");
        log.type = "button";
        bottom.appendChild(log);
        rowEl.append(top, bottom);
        if (open.has(r.id)) {
          const cell = make("div", "forge-run-logbox");
          rowEl.appendChild(cell);
          logRow(r, cell);
        }
        log.addEventListener("click", () => {
          if (open.has(r.id)) open.delete(r.id); else open.add(r.id);
          paintRuns();
        });
        list.appendChild(rowEl);
      });
      if (!rows.length) list.appendChild(make("p", "hint", "No runs yet."));
      runsBox.appendChild(list);
      const foot = make("p", "forge-run-foot", `${rows.length} of ${data.total || 0} runs shown${data.pruned ? `; ${data.pruned} older runs were dropped past the history limit` : ""}.`);
      runsBox.appendChild(foot);
      if (rows.length < (data.total || 0)) {
        const more = make("button", "fchip sm", "Show older runs");
        more.type = "button";
        more.addEventListener("click", async () => {
          try {
            const page = await request(`/api/routines/${encodeURIComponent(rid)}/runs?offset=${rows.length}&limit=50`);
            older = older.concat(page.runs || []);
            paintRuns();
          } catch (e) { toast(`Could not load older runs: ${e.message}`); }
        });
        runsBox.appendChild(more);
      }
    }
    async function refresh() {
      if (!wrap.isConnected) { clearTimeout(loopTimer); return; }
      try {
        data = { ...data, ...await request(`/api/routines/${encodeURIComponent(rid)}/hood`) };
        paintState(); paintFacts(); paintRuns();
      } catch (e) { stateLine.textContent = `Could not read it: ${e.message}`; }
      clearTimeout(loopTimer);
      loopTimer = isLive() && wrap.isConnected ? setTimeout(refresh, RUN_POLL_MS) : null;
    }
    runNow.addEventListener("click", async () => {
      runNow.disabled = true;
      try {
        const res = await send(`/api/routines/${encodeURIComponent(rid)}/run`, "POST", {});
        toast(res.command_run ? `Run started (${res.command_run})` : res.job_id ? `Started session ${res.job_id}` : "Started");
        if (res.job_id && typeof window.openSession === "function") window.openSession(res.job_id);
      } catch (e) { toast(`Run refused: ${e.message}`); }
      await loadLoops();
      refresh();
    });
    pause.addEventListener("click", async () => {
      const enabled = !data.enabled;
      try {
        await send(`/api/routines/${encodeURIComponent(rid)}`, "PUT", { enabled });
        data = { ...data, enabled, next_run: null };
        toast(enabled ? "Resumed: it runs on its schedule again" : "Paused: it will not start on its schedule");
        // a paused loop is a paused Schedule card on its Flow's board too
        const flow = state.flows.find((f) => (f.triggers || []).some((t) => t.id === rid));
        [flow, state.current].forEach((item) => (item?.nodes || []).forEach((n) => {
          if (n.type === "trigger" && n.routine_id === rid) n.enabled = enabled;
        }));
        renderBoard();
      } catch (e) { toast(`Could not ${enabled ? "resume" : "pause"}: ${e.message}`); }
      await loadLoops();
      paintState();
    });
    paintState(); paintFacts(); paintRuns();
    setTimeout(refresh, 0);
    return wrap;
  }

  /* ---------- drag, ports, wires ------------------------------------------- */

  function bindNodeDrag(card, head, node) {
    let active = null;
    head.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button")) return;
      event.preventDefault();
      event.stopPropagation();
      bringNodeFront(node, card);
      active = { id: event.pointerId, x: event.clientX, y: event.clientY, nx: node.x, ny: node.y, moved: false };
      head.setPointerCapture(event.pointerId);
      card.classList.add("is-dragging");
    });
    head.addEventListener("pointermove", (event) => {
      if (!active || active.id !== event.pointerId) return;
      const dx = (event.clientX - active.x) / state.zoom;
      const dy = (event.clientY - active.y) / state.zoom;
      if (Math.hypot(dx, dy) > 3) active.moved = true;
      node.x = Math.round(active.nx + dx);
      node.y = Math.round(active.ny + dy);
      card.style.left = `${node.x}px`;
      card.style.top = `${node.y}px`;
      renderWires();
    });
    const finish = (event) => {
      if (!active || active.id !== event.pointerId) return;
      const moved = active.moved;
      active = null;
      card.classList.remove("is-dragging");
      if (moved) setDirty();
      else openNode(node.id);
    };
    head.addEventListener("pointerup", finish);
    head.addEventListener("pointercancel", () => { active = null; card.classList.remove("is-dragging"); });
  }

  function port(node, direction) {
    const button = make("button", `forge-port ${direction}`);
    button.type = "button";
    button.dataset.nodeId = node.id;
    button.dataset.direction = direction;
    button.title = direction === "out" ? "Drag to the card this feeds" : "Drag to the card that feeds this";
    button.addEventListener("pointerdown", (event) => {
      if (!editable()) return;
      event.preventDefault();
      event.stopPropagation();
      beginConnection(node.id, direction, event);
    });
    return button;
  }

  function beginConnection(nodeId, direction, event) {
    state.connect = { nodeId, direction, pointerId: event.pointerId };
    state.tempPoint = boardPoint(event.clientX, event.clientY);
    const viewport = q("#forge-viewport");
    viewport.classList.add("connecting");
    viewport.setPointerCapture(event.pointerId);
    renderWires();
  }

  function finishConnection(event) {
    if (!state.connect) return;
    const hit = document.elementFromPoint(event.clientX, event.clientY);
    const target = hit?.closest(".forge-port") || hit?.closest(".forge-node");
    const source = state.connect;
    if (target) {
      const targetId = target.dataset.nodeId;
      const from = source.direction === "out" ? source.nodeId : targetId;
      const to = source.direction === "out" ? targetId : source.nodeId;
      connect(from, to);
    }
    state.connect = null;
    state.tempPoint = null;
    q("#forge-viewport").classList.remove("connecting");
    renderWires();
  }

  function connect(from, to) {
    if (!state.current || !from || !to || from === to) return;
    const fromNode = state.current.nodes.find((n) => n.id === from);
    const toNode = state.current.nodes.find((n) => n.id === to);
    if (!fromNode || !toNode) return;
    if (toNode.type === "trigger") return toast("A schedule starts a Flow; nothing feeds it.");
    if (toNode.type === "input") return toast("The Input is the ask; nothing feeds it.");
    if (fromNode.type === "output") return toast("An output is the end of a line.");
    if (SOURCE.has(toNode.type) || toNode.type === "env") return toast("A source is told about; wire it the other way, into the card that should know it.");
    if ((SOURCE.has(fromNode.type) || fromNode.type === "env" || fromNode.type === "input")
        && !["agent", "judge"].includes(toNode.type)) return toast("Only a card that runs a session can be told about a source or the ask.");
    if (state.current.edges.some((edge) => edge.from === from && edge.to === to)) return toast("That wire already exists.");
    const edge = { id: id("wire"), from, to, instructions: "" };
    state.current.edges.push(edge);
    if (hasExecutableCycle()) {
      state.current.edges.pop();
      return toast("That would make a loop. A judge that re-runs a card is how a Flow repeats.");
    }
    setDirty();
    renderBoard();
    openEdge(edge);
  }

  function hasExecutableCycle() {
    const ids = new Set((state.current?.nodes || []).filter((node) => EXEC.has(node.type)).map((node) => node.id));
    const next = {};
    ids.forEach((nodeId) => { next[nodeId] = []; });
    (state.current?.edges || []).forEach((edge) => {
      if (ids.has(edge.from) && ids.has(edge.to)) next[edge.from].push(edge.to);
    });
    const visiting = new Set();
    const done = new Set();
    const visit = (nodeId) => {
      if (visiting.has(nodeId)) return true;
      if (done.has(nodeId)) return false;
      visiting.add(nodeId);
      if (next[nodeId].some(visit)) return true;
      visiting.delete(nodeId);
      done.add(nodeId);
      return false;
    };
    return [...ids].some(visit);
  }

  function nodeBox(node) {
    return { x: node.x, y: node.y, width: CARD_W, height: state.heights[node.id] || 120 };
  }

  function wirePath(fromNode, toNode) {
    const a = nodeBox(fromNode);
    const b = nodeBox(toNode);
    const start = { x: a.x + a.width + 7, y: a.y + a.height / 2 };
    const end = { x: b.x - 7, y: b.y + b.height / 2 };
    const bend = Math.max(60, Math.abs(end.x - start.x) * .45);
    return { path: `M ${start.x} ${start.y} C ${start.x + bend} ${start.y}, ${end.x - bend} ${end.y}, ${end.x} ${end.y}`,
      labelX: (start.x + end.x) / 2, labelY: (start.y + end.y) / 2 - 8 };
  }

  function renderWires() {
    const svg = q("#forge-wires");
    if (!svg) return;
    svg.replaceChildren();
    const byId = Object.fromEntries((state.current?.nodes || []).map((node) => [node.id, node]));
    (state.current?.edges || []).forEach((edge) => {
      const from = byId[edge.from], to = byId[edge.to];
      if (!from || !to) return;
      const shape = wirePath(from, to);
      const hit = document.createElementNS("http://www.w3.org/2000/svg", "path");
      hit.setAttribute("d", shape.path);
      hit.setAttribute("class", "forge-wire-hit");
      hit.addEventListener("click", (event) => { event.stopPropagation(); openEdge(edge); });
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", shape.path);
      // Trace overlay on the handoff itself: the wire INTO a running stage
      // pulses, a wire whose source stage finished reads settled green.
      const fromStage = runStage(edge.from);
      const toStage = runStage(edge.to);
      const runLive = fromStage?.status === "running" || toStage?.status === "running";
      const runDone = fromStage?.status === "done";
      path.setAttribute("class", `forge-wire${edge.id === state.selectedEdge ? " is-selected" : ""}${edge.instructions ? " has-note" : ""}${runLive ? " is-run-live" : (runDone ? " is-run-done" : "")}`);
      path.addEventListener("click", (event) => { event.stopPropagation(); openEdge(edge); });
      svg.append(hit, path);
      if (edge.instructions) {
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", shape.labelX);
        label.setAttribute("y", shape.labelY);
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("class", "forge-wire-note");
        const note = edge.instructions.trim().replace(/\s+/g, " ");
        label.textContent = note.length > 42 ? `${note.slice(0, 40)}...` : note;
        svg.appendChild(label);
      }
    });
    if (state.connect && state.tempPoint) {
      const source = byId[state.connect.nodeId];
      if (!source) return;
      const box = nodeBox(source);
      const start = state.connect.direction === "out"
        ? { x: box.x + box.width + 7, y: box.y + box.height / 2 }
        : { x: box.x - 7, y: box.y + box.height / 2 };
      const temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
      temp.setAttribute("class", "forge-wire is-selected");
      temp.setAttribute("d", `M ${start.x} ${start.y} L ${state.tempPoint.x} ${state.tempPoint.y}`);
      svg.appendChild(temp);
    }
  }

  function removeEdge(edgeId) {
    if (!state.current) return;
    state.current.edges = state.current.edges.filter((item) => item.id !== edgeId);
    if (state.selectedEdge === edgeId) state.selectedEdge = null;
    setDirty();
    renderBoard();
    renderInspector();
  }

  function removeNode(nodeId) {
    const node = state.current?.nodes.find((item) => item.id === nodeId);
    if (!node || !editable()) return;
    state.current.nodes = state.current.nodes.filter((item) => item.id !== nodeId);
    state.current.edges = state.current.edges.filter((edge) => edge.from !== nodeId && edge.to !== nodeId);
    state.current.nodes.forEach((item) => {
      if (item.continues === nodeId) item.continues = "";
      if (item.judge?.retry_stage === nodeId) item.judge.retry_stage = "";
      if (item.logic?.subject === nodeId) delete item.logic.subject;
    });
    if (state.selectedNode === nodeId) state.selectedNode = null;
    setDirty();
    renderBoard();
    renderInspector();
  }

  function duplicateNode(node) {
    if (!editable()) return;
    const clone = copy(node);
    clone.id = cardId(`${clone.name} copy`, clone.type);
    clone.name = `${clone.name} copy`;
    delete clone.routine_id;
    const spot = freeSpot(clone);
    clone.x = spot.x;
    clone.y = spot.y;
    clone.z = ++state.z;
    state.current.nodes.push(clone);
    state.selectedNode = clone.id;
    state.selectedEdge = null;
    setDirty(); renderBoard(); renderInspector();
  }

  /* ---------- menus ---------------------------------------------------------- */

  function closeMenu() {
    q(".forge-menu", q("#forge-shell"))?.remove();
  }

  function menuAction(menu, label, hint, run, danger = false) {
    const button = make("button", `forge-menu-action${danger ? " is-danger" : ""}`);
    button.type = "button";
    button.append(make("strong", "", label), make("small", "", hint));
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      closeMenu();
      run();
    });
    menu.appendChild(button);
  }

  function placeMenu(menu, clientX, clientY) {
    const shell = q("#forge-shell");
    shell.appendChild(menu);
    const shellRect = shell.getBoundingClientRect();
    const width = 250;
    const left = clamp(clientX - shellRect.left, 8, Math.max(8, shellRect.width - width - 8));
    const top = clamp(clientY - shellRect.top, 8, Math.max(8, shellRect.height - menu.offsetHeight - 8));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    menu.addEventListener("pointerdown", (click) => click.stopPropagation());
    setTimeout(() => document.addEventListener("pointerdown", closeMenu, { once: true }), 0);
  }

  function showAddMenu(anchor) {
    if (!editable()) return toast("Pick a Flow or start a new one first.");
    closeMenu();
    const menu = make("div", "forge-menu");
    const title = make("div", "forge-menu-title");
    title.append(make("span", "forge-kicker", "Add a card"), make("strong", "", currentNode() ? `after ${currentNode().name}` : "to the board"));
    menu.appendChild(title);
    ["input", "agent", "judge", "logic", "approval", "output", "trigger"].forEach((type) => {
      if (type === "input" && inputCard()) return;
      menuAction(menu, KIND[type].name, KIND[type].hint, () => addCard(type));
    });
    menuAction(menu, "A skill, script or file", "From the Sources tab: click one, or drag it onto the board.", () => {
      openLibrary();
      switchLibrary("sources");
      q("#forge-library-search")?.focus();
    });
    menuAction(menu, "A loop", "From the Loops tab: open one and add it, or drag it onto the board. The Flow runs it and waits for it.", () => {
      openLibrary();
      switchLibrary("loops");
    });
    const rect = anchor.getBoundingClientRect();
    placeMenu(menu, rect.left, rect.bottom + 6);
  }

  function showNodeMenu(node, event) {
    if (!q("#forge-shell")) return;
    closeMenu();
    openNode(node.id);
    const menu = make("div", "forge-menu");
    const title = make("div", "forge-menu-title");
    title.append(make("span", "forge-kicker", KIND[node.type]?.name || "Card"), make("strong", "", node.name));
    menu.appendChild(title);
    if (editable()) {
      if (EXEC.has(node.type) && node.type !== "output") {
        menuAction(menu, "Add a card after it", "An agent wired from this one.", () => addCard("agent"));
        menuAction(menu, "Add a judge after it", "Grades this card's result.", () => addCard("judge"));
      }
      if ((node.type === "agent" || node.type === "judge") && !inputCard()) {
        menuAction(menu, "Add the Input before it", "The ask, wired into this card.", () => addCard("input"));
      }
      menuAction(menu, "Duplicate", "A copy beside this one.", () => duplicateNode(node));
      menuAction(menu, "Remove", "Delete this card and its wires.", () => removeNode(node.id), true);
    } else {
      menuAction(menu, "Open", "Read this card.", () => openNode(node.id));
    }
    placeMenu(menu, event.clientX, event.clientY);
  }

  /* ---------- board: arrange, zoom, pan ------------------------------------- */

  function arrange() {
    if (!state.current) return;
    const nodes = state.current.nodes;
    const incoming = Object.fromEntries(nodes.map((node) => [node.id, []]));
    state.current.edges.forEach((edge) => {
      if (incoming[edge.to] && incoming[edge.from]) incoming[edge.to].push(edge.from);
    });
    const depths = {};
    const depth = (nodeId, trail = new Set()) => {
      if (depths[nodeId] != null) return depths[nodeId];
      if (trail.has(nodeId)) return 0;
      const next = new Set(trail); next.add(nodeId);
      depths[nodeId] = incoming[nodeId]?.length ? Math.max(...incoming[nodeId].map((source) => depth(source, next))) + 1 : 0;
      return depths[nodeId];
    };
    const rows = {};
    nodes.forEach((node) => {
      const col = depth(node.id);
      const row = rows[col] || 0;
      rows[col] = row + 1;
      node.x = 60 + col * (CARD_W + 90);
      node.y = 90 + row * 190;
    });
    state.zoom = 1;
    state.panX = 25;
    state.panY = 55;
    setDirty();
    renderBoard();
  }

  function zoomAt(next, clientX, clientY) {
    const viewport = q("#forge-viewport");
    const rect = viewport.getBoundingClientRect();
    const old = state.zoom;
    next = clamp(next, .35, 2.2);
    const x = clientX == null ? rect.width / 2 : clientX - rect.left;
    const y = clientY == null ? rect.height / 2 : clientY - rect.top;
    const worldX = (x - state.panX) / old;
    const worldY = (y - state.panY) / old;
    state.zoom = next;
    state.panX = x - worldX * next;
    state.panY = y - worldY * next;
    renderBoard();
  }

  function bindViewport() {
    const viewport = q("#forge-viewport");
    let pan = null;
    viewport.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest(".forge-node,.forge-wire-hit,.forge-port,.forge-boardbar")) return;
      pan = { id: event.pointerId, x: event.clientX, y: event.clientY, px: state.panX, py: state.panY, moved: false };
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add("is-panning");
    });
    viewport.addEventListener("pointermove", (event) => {
      if (state.connect && state.connect.pointerId === event.pointerId) {
        state.tempPoint = boardPoint(event.clientX, event.clientY);
        renderWires();
        return;
      }
      if (!pan || pan.id !== event.pointerId) return;
      if (Math.hypot(event.clientX - pan.x, event.clientY - pan.y) > 3) pan.moved = true;
      state.panX = pan.px + event.clientX - pan.x;
      state.panY = pan.py + event.clientY - pan.y;
      renderBoard();
    });
    viewport.addEventListener("pointerup", (event) => {
      if (state.connect) finishConnection(event);
      if (pan?.id === event.pointerId) {
        const moved = pan.moved;
        pan = null;
        viewport.classList.remove("is-panning");
        // a click on empty board clears the selection; a pan keeps it
        if (!moved && (state.selectedNode || state.selectedEdge)) closeInspector();
      }
    });
    viewport.addEventListener("pointercancel", () => {
      pan = null;
      state.connect = null;
      state.tempPoint = null;
      viewport.classList.remove("is-panning", "connecting");
      renderWires();
    });
    viewport.addEventListener("wheel", (event) => {
      event.preventDefault();
      zoomAt(state.zoom * (event.deltaY < 0 ? 1.09 : .92), event.clientX, event.clientY);
    }, { passive: false });
    viewport.addEventListener("dragover", (event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; });
    viewport.addEventListener("drop", (event) => {
      event.preventDefault();
      if (!state.libraryDrag) return;
      const point = boardPoint(event.clientX, event.clientY);
      if (state.libraryDrag.environment) addEnvironment(state.libraryDrag.environment, point);
      else if (state.libraryDrag.loop) addLoop(state.libraryDrag.loop, point);
      else addSource(state.libraryDrag, point);
      state.libraryDrag = null;
    });
  }

  /* ---------- save, new, run ------------------------------------------------- */

  async function saveFlow(mode = null) {
    if (!editable()) return;
    let create = mode === "copy" || !state.current.id;
    if (!create && state.current.builtin) {
      const choice = await forgeDialog({
        title: "This is a shipped starter",
        message: "Save your own copy and keep the starter as it is, or change the starter itself for every Flow built from it?",
        choices: [["update", "Change the starter"], ["copy", "Save my copy"]],
      });
      if (!choice) return;
      create = choice === "copy";
    }
    let name = state.current.name;
    if (create) {
      name = await forgeDialog({
        title: mode === "copy" || state.current.id ? "Save a copy" : "Save this Flow",
        message: "Name it. The board, its cards and its wires are saved under that name.",
        value: state.current.id ? `${name} copy` : name,
        confirm: "Save",
      });
      if (!name) return;
    }
    const payload = flowPayload();
    payload.name = name;
    const path = create ? "/api/flows" : `/api/flows/${encodeURIComponent(state.current.id)}`;
    const keep = state.selectedNode;
    try {
      const saved = await send(path, create ? "POST" : "PUT", payload);
      const index = state.flows.findIndex((flow) => flow.id === saved.id);
      if (index >= 0) state.flows[index] = saved;
      else state.flows.push(saved);
      state.current = copy(saved);
      state.selectedNode = saved.nodes.some((n) => n.id === keep) ? keep : null;
      state.selectedEdge = null;
      localStorage.setItem("vira-forge-flow", saved.id);
      setDirty(false);
      renderAll();
      if ((saved.triggers || []).length) loadLoops();
      toast(create ? `Saved as "${saved.name}"` : "Saved");
    } catch (error) {
      toast(`Save refused: ${error.message}`);
    }
  }

  async function newFlow() {
    if (state.dirty && !await forgeDialog({
      title: "Start a new Flow?",
      message: "Discard the unsaved changes on this board and start a blank one?",
      confirm: "Start blank",
    })) return;
    state.current = { id: null, name: "Untitled Flow", description: "", kind: "flow", builtin: false,
      revision: 0, nodes: [], edges: [], contexts: [], triggers: [], executable: true };
    state.trace = null;
    state.idea = null;
    const ask = newCard("input", { x: 60, y: 150 });
    const agent = newCard("agent", { x: 60 + CARD_W + 90, y: 150 });
    agent.name = "Work";
    agent.id = "work";
    state.current.nodes.push(ask, agent);
    state.current.edges.push({ id: id("wire"), from: ask.id, to: agent.id, instructions: "" });
    state.selectedNode = ask.id;
    state.selectedEdge = null;
    state.zoom = 1; state.panX = 25; state.panY = 55;
    setDirty();
    renderAll();
    q("#forge-inspector-body textarea")?.focus();
  }

  async function runFlow() {
    if (!editable()) return;
    const card = inputCard();
    if (!card) {
      toast("Add an Input card first: it holds what the Flow works on.");
      addCard("input");
      return;
    }
    const input = String(card.prompt || "").trim();
    if (!input) { openNode(card.id); return toast("Write what the Flow should work on in the Input card."); }
    if (!state.current.id) return toast("Save the Flow first, then run it.");
    // Typing the ask makes the board dirty. Your own Flow saves itself
    // before it runs; a shipped starter runs on the ask without being
    // changed (the ask rides the request).
    if (state.dirty && !state.current.builtin) {
      await saveFlow();
      if (state.dirty) return;
    }
    try {
      const linked = state.idea && state.idea.flow_id === state.current.id ? state.idea.idea_id : null;
      const result = await send(`/api/flows/${encodeURIComponent(state.current.id)}/run`, "POST", {
        input, notify: false, output: "", provider: null,
        idea_id: linked,
      });
      const runId = result.id || result.run_id || result.job_id;
      toast(runId ? `Started ${runId}` : "Started");
      if (typeof window.openSession === "function" && result.job_id) window.openSession(result.job_id);
      if (typeof window.setWorkTab === "function") window.setWorkTab("live");
      loadForgeRuns();
    } catch (error) {
      toast(`Run refused: ${error.message}`);
    }
  }

  // The runs LIST lives in Work > Results, one chronological stream (app.js
  // loadRuns). The Forge drives that one loader; setRuns is how it feeds
  // the trace overlay back.
  async function loadForgeRuns() {
    if (typeof window.loadRuns === "function") return window.loadRuns();
    try {
      const data = await request("/api/circuits/runs?limit=24");
      setRuns(data.runs || []);
    } catch (error) { /* the board simply shows no run overlay */ }
  }

  function setRuns(runs) {
    state.runs = runs || [];
    if (!state.trace) return;
    const fresh = state.runs.find((run) => run.id === state.trace.runId);
    if (fresh) state.trace.run = fresh;
    const run = tracedRun();
    // Repaint the board only when the run's visible state moved: a blind
    // repaint every 3s poll would tear an open editor out from under the
    // owner's typing.
    const sig = run ? JSON.stringify([run.status,
      Object.entries(run.stages || {}).map(([sid, s]) => [sid, s.status, s.grade || "", s.job_id || ""])]) : "";
    if (sig !== state.trace.sig) {
      state.trace.sig = sig;
      renderBoard();
      renderTraceBar();
    }
  }

  /* ---------- keys and binding ------------------------------------------------
     The shortcuts are document-level, so every guard here is about NOT firing:
     the Forge has to be the thing on screen, a dialog must not be up, and a
     text field must keep its own native undo and clipboard. */

  function forgeShowing() {
    const pane = q("#work-dispatch-pane");
    return !!pane && pane.getClientRects().length > 0 && !!state.current;
  }

  function forgeKeys(event) {
    if (!forgeShowing()) return;
    if (q(".forge-dialog-backdrop", q("#forge-shell"))) return;
    const typing = event.target?.closest?.("input, textarea, select, [contenteditable='true']");
    const mod = event.metaKey || event.ctrlKey;
    const key = (event.key || "").toLowerCase();

    if (mod && (key === "z" || key === "y")) {
      if (typing) return;
      event.preventDefault();
      stepHistory(key === "z" && !event.shiftKey);
      return;
    }
    if (mod && key === "c") {
      // Never steal a real text selection: that is what the user meant.
      if (typing || !currentNode() || !window.getSelection()?.isCollapsed) return;
      event.preventDefault();
      copyNode();
      return;
    }
    if (mod && key === "v") {
      if (typing || !readClip()) return;
      event.preventDefault();
      pasteNode();
      return;
    }
    if (mod && key === "d") {
      const node = currentNode();
      if (typing || !node || !editable()) return;
      event.preventDefault();
      duplicateNode(node);
      return;
    }
    if ((key === "delete" || key === "backspace") && !mod) {
      if (typing || !editable()) return;
      if (state.selectedNode) { event.preventDefault(); removeNode(state.selectedNode); }
      else if (state.selectedEdge) { event.preventDefault(); removeEdge(state.selectedEdge); }
    }
  }

  function forgePaste(event) {
    if (!forgeShowing() || !editable()) return;
    if (q(".forge-dialog-backdrop", q("#forge-shell"))) return;
    const typing = event.target?.closest?.("input, textarea, select, [contenteditable='true']");
    const text = event.clipboardData?.getData("text/plain") || "";
    // A link pasted into the library search is an ask, not a search.
    const intoSearch = typing && typing.id === "forge-library-search" && /^https?:\/\/\S+$/i.test(text.trim());
    if (typing && !intoSearch) return;
    if (!text.trim()) return;
    event.preventDefault();
    pasteAsInput(text);
  }

  function bind() {
    if (!q("#forge-shell") || state.bound) return;
    state.bound = true;
    document.addEventListener("paste", forgePaste);
    qa("#forge-library-tabs .seg-btn").forEach((button) => button.addEventListener("click", () => switchLibrary(button.dataset.forgeLibrary)));
    q("#forge-library-search").addEventListener("input", (event) => {
      state.query = event.target.value;
      clearTimeout(state.searchTimer);
      renderLibrary();
      if (state.library === "sources") state.searchTimer = setTimeout(() => loadFindRows(state.query), 280);
    });
    q("#forge-undo").addEventListener("click", () => stepHistory(true));
    q("#forge-redo").addEventListener("click", () => stepHistory(false));
    q("#forge-copy").addEventListener("click", copyNode);
    q("#forge-paste").addEventListener("click", pasteNode);
    q("#forge-duplicate").addEventListener("click", () => { const node = currentNode(); if (node) duplicateNode(node); });
    q("#forge-delete").addEventListener("click", () => {
      if (state.selectedNode) removeNode(state.selectedNode);
      else if (state.selectedEdge) removeEdge(state.selectedEdge);
    });
    document.addEventListener("keydown", forgeKeys);
    q("#forge-library-toggle").addEventListener("click", openLibrary);
    q("#forge-inspector-close").addEventListener("click", closeInspector);
    q("#forge-new").addEventListener("click", newFlow);
    q("#forge-save").addEventListener("click", () => saveFlow());
    q("#forge-save-as").addEventListener("click", () => saveFlow("copy"));
    q("#forge-run").addEventListener("click", runFlow);
    q("#forge-add").addEventListener("click", (event) => { event.stopPropagation(); showAddMenu(event.currentTarget); });
    q("#forge-arrange").addEventListener("click", arrange);
    q("#forge-zoom-in").addEventListener("click", () => zoomAt(state.zoom * 1.12));
    q("#forge-zoom-out").addEventListener("click", () => zoomAt(state.zoom * .88));
    bindViewport();
  }

  /** Open a Flow the Queue just minted for an idea, with the idea as its
   *  run input. The reload is forced because the Flow was created a moment
   *  ago on the server and is not in the cached list yet. */
  async function openIdea(payload) {
    if (!payload || !payload.flow_id) return;
    state.idea = { flow_id: payload.flow_id, idea_id: payload.idea_id };
    setDirty(false);              // nothing to discard; skip the guard prompt
    await loadForge({ force: true });
    await selectFlow(payload.flow_id);
    const card = inputCard() || addCard("input");
    if (card && payload.input) {
      card.prompt = payload.input;
      touch(card);
      await saveFlow();          // the owner's own copy: the ask is kept with it
    }
    toast("Loaded into the Forge. Read the cards, change what you like, then Run.");
  }

  window.loadForge = loadForge;
  window.loadForgeRuns = loadForgeRuns;
  window.initForge = () => { bind(); };
  window.Forge = { state, load: loadForge, selectFlow, render: renderAll,
    setLibrary: switchLibrary, openLibrary, openIdea, setRuns, traceRun, stopTrace };
})();

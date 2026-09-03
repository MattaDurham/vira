/* World — Vira's typed, temporal graph of local knowledge.
   The existing high-performance network renderer now reads /api/world:
   CRM people, vault notes, organizations, projects, places, events,
   sources, concepts and topics share one map.  People are one kind filter,
   not the schema.  Every relation can carry a source receipt and both a
   valid-time interval (when it held) and recorded time (when Vira knew it).
   Selection is the core interaction: clicking a node (or a grouping chip)
   toggles it into the selection — selected people and the ties among them
   light up, bridges between unlinked selections are traced with a local
   BFS, shared connections glow, and everything else fades to a hint.
   Loads lazily (atlasLoad is called by the dock window / mobile tab /
   #atlas deep link), pauses when hidden, honors prefers-reduced-motion
   by settling the layout synchronously instead of animating. */
"use strict";

(() => {
  const canvas = $("#atlas-canvas");
  if (!canvas) return;
  const stage = $("#atlas-stage");
  const tip = $("#atlas-tip");
  const card = $("#atlas-card");
  const emptyEl = $("#atlas-empty");
  const ctx = canvas.getContext("2d");

  // Taurid earthbound cluster palette: lichen, patina, corten, ochre —
  // desaturated per the brand book's material references.
  const CLUSTER_COLORS = ["#8a8478", "#7d8a74", "#7a8f9c", "#a0715f",
                          "#8f7d96", "#a89a6a", "#6f948c", "#a08292",
                          "#8a9a6f", "#9c8f7a", "#a08a6f", "#96a38c"];
  const EGO_R = 26;

  const S = {
    graph: null,          // the served payload
    nodes: [],            // sim nodes (graph nodes + x/y/vx/vy)
    byId: new Map(),
    edges: [],            // {a, b, weight, signals, structural, src}
    egoEdges: [],
    ego: null,            // the ego sim node
    colors: new Map(),    // cluster id -> color
    imgs: new Map(),      // pid -> {img, ok}
    cam: { k: 1, x: 0, y: 0 },   // world -> screen: s = (p - x) * k + c/2
    alpha: 0,             // sim temperature
    raf: 0,
    running: false,
    visible: false,
    hover: null,
    sel: new Set(),          // selected sim nodes (multi-select)
    selEdges: new Set(),     // ties between two selected nodes
    selPathEdges: new Set(), // edges on bridge chains between selections
    selPathNodes: new Set(), // bridge node ids on those chains
    shared: new Set(),       // ids connected to 2+ selected nodes
    neighbors: new Set(),    // ids connected to any selected node
    chains: [],              // [{a, b, nodes}] bridge chains for the card
    articleTrail: [],        // node ids followed through the inspector
    articleIndex: -1,
    detailToken: 0,
    adj: new Map(),          // id -> [{n, e}] adjacency for BFS
    lens: null,           // active lens id (see LENS_KEY)
    bands: [],            // the active lens's bands
    iso: { ids: new Set(), ring: 0 },  // isolate: show only these bands
    shown: null,             // Set of visible node ids (null = everyone)
    hideEgo: false,
    match: "",            // search filter
    filterSearch: true,
    hideOrphans: false,
    starredOnly: false,
    starred: new Set(),
    enabledKinds: new Set(),
    time: { axis: "valid", at: null, min: null, max: null, timeline: {},
            playing: false, speed: 1, raf: 0, lastFrame: 0 },
    fixedLayout: false,      // server-supplied semantic coordinates
    display: { scale: 1, nodeSize: 1, nodeOpacity: 1, linkThickness: 1,
               linkOpacity: 1, whiteLinks: false,
               autoRotate: true, curvedLinks: true, linkCurve: .10,
               sphericalNodes: true },
    colorOverrides: {},
    physics: { enabled: true, center: 0.08, repel: 0.30, link: 0.25,
               distance: 1, semantic: 0.18 },
    loading: false,
    loadedGen: null,
  };

  const CONTROL_KEY = "vira-world-controls";
  const CONTROL_DEFAULTS = {
    filterSearch: true, hideOrphans: false, starredOnly: false,
    display: { scale: 1, nodeSize: 1, nodeOpacity: 1, linkThickness: 1,
               linkOpacity: 1, whiteLinks: false,
               autoRotate: true, curvedLinks: true, linkCurve: .10,
               sphericalNodes: true },
    physics: { enabled: true, center: 0.08, repel: 0.30, link: 0.25,
               distance: 1, semantic: 0.18 },
  };

  function clamp(value, low, high, fallback) {
    value = Number(value);
    return Number.isFinite(value) ? Math.max(low, Math.min(high, value))
                                  : fallback;
  }

  function loadControls() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(CONTROL_KEY) || "null"); }
    catch { saved = null; }
    if (!saved || typeof saved !== "object") return;
    S.filterSearch = saved.filterSearch !== false;
    S.hideOrphans = !!saved.hideOrphans;
    S.starredOnly = !!saved.starredOnly;
    if (Array.isArray(saved.starred))
      S.starred = new Set(saved.starred.filter((id) => typeof id === "string"));
    S.display.scale = clamp(saved.display?.scale, .35, 2.5, 1);
    S.display.nodeSize = clamp(saved.display?.nodeSize, .6, 1.8, 1);
    S.display.nodeOpacity = clamp(saved.display?.nodeOpacity, .1, 1, 1);
    S.display.linkThickness = clamp(
      saved.display?.linkThickness, .25, 2.5, 1);
    S.display.linkOpacity = clamp(saved.display?.linkOpacity, 0, 3, 1);
    S.display.whiteLinks = saved.display?.whiteLinks === true;
    S.display.autoRotate = saved.display?.autoRotate !== false;
    S.display.curvedLinks = saved.display?.curvedLinks !== false;
    S.display.linkCurve = clamp(saved.display?.linkCurve, 0, .3, .10);
    S.display.sphericalNodes = saved.display?.sphericalNodes !== false;
    S.time.speed = clamp(saved.timelineSpeed, .5, 4, 1);
    if (saved.colorOverrides && typeof saved.colorOverrides === "object") {
      for (const [key, color] of Object.entries(saved.colorOverrides))
        if (/^#[0-9a-f]{6}$/i.test(color)) S.colorOverrides[key] = color;
    }
    S.physics.enabled = saved.physics?.enabled !== false;
    S.physics.center = clamp(saved.physics?.center, 0, 1, .08);
    S.physics.repel = clamp(saved.physics?.repel, 0, 1, .30);
    S.physics.link = clamp(saved.physics?.link, 0, 1, .25);
    S.physics.distance = clamp(saved.physics?.distance, .4, 2.2, 1);
    S.physics.semantic = clamp(saved.physics?.semantic, 0, 1, .18);
  }

  function saveControls() {
    try {
      localStorage.setItem(CONTROL_KEY, JSON.stringify({
        filterSearch: S.filterSearch, hideOrphans: S.hideOrphans,
        starredOnly: S.starredOnly, starred: [...S.starred],
        timelineSpeed: S.time.speed,
        display: S.display, physics: S.physics,
        colorOverrides: S.colorOverrides,
      }));
    } catch { /* private browsing or a full origin: controls stay in memory */ }
  }
  loadControls();

  // ---------- the 3D renderer ----------

  // The module is three-dimensional wherever WebGL will have us:
  // static/atlas3d.js owns the layout, the meshes, the picking and the
  // camera, and its navigation is a transcription of the Image Atlas's.
  // This file keeps the data, the state and every piece of chrome, and
  // keeps its flat canvas as the honest fallback for a browser with no
  // WebGL - a module that renders nothing is worse than one that renders
  // flat. R3 is null until asked, then the renderer or false.
  let R3 = null;

  async function ensure3D() {
    if (R3 !== null) return R3;
    R3 = false;
    try {
      const mod = await import("/atlas3d.js");
      const r = mod.create({
        stage, S,
        reducedMotion: REDUCED_MOTION,
        isShown, isEdgeShown, matchDim, tileColor, initials, firstLast,
        onHover: hitHover,
        onSelect: hitSelect,
        onOpen: hitOpen,
        onContext: hitContext,
        onEmpty: hitEmpty,
        onPhysicsScope: paintPhysicsStatus,
      });
      if (r) {
        R3 = r;
        canvas.style.display = "none";     // the flat fallback stands down
        stage.classList.add("atlas-3d");
        // diagnostics handle, the Image Atlas's __atlas by another name
        window.__network3d = r;
      }
    } catch (e) {
      console.warn("World: staying flat -", e && e.message);
    }
    return R3;
  }

  // ---------- data ----------

  async function atlasLoad(force) {
    if (S.loading) return;
    if (S.graph && !force && S.graph.generated === S.loadedGen) {
      resize(); wake(); return;
    }
    S.loading = true;
    try {
      await ensure3D();
      const g = await api("/api/world");
      if (g.status === "empty") {
        showEmpty(false);
        return;
      }
      emptyEl.style.display = "none";
      S.loadedGen = g.generated;
      initGraph(g);
    } catch (e) {
      showEmpty(false, "Network unavailable — " + e.message);
    } finally {
      S.loading = false;
    }
  }
  window.atlasLoad = atlasLoad;

  function showEmpty(building, msg) {
    emptyEl.innerHTML = "";
    emptyEl.appendChild(el("div", "subsviz-empty-title",
      msg || (building ? "Building the network…"
                       : "No connected knowledge is available yet")));
    if (!msg && !building) {
      const b = el("button", "btn small primary", "Build the graph");
      b.textContent = "Scan sources";
      b.addEventListener("click", async () => {
        await post("/api/world/refresh", {});
        showEmpty(true);
        setTimeout(() => atlasLoad(true), 4000);
      });
      emptyEl.appendChild(b);
    } else if (building) {
      emptyEl.appendChild(el("div", "hint",
        "Refreshing the CRM projection and connected local vaults."));
    }
    emptyEl.style.display = "";
    $("#atlas-meta").textContent = "";
  }

  // ---------- lenses ----------
  //
  // ONE web, four questions asked of it. The server bands the same node
  // set four ways (server/atlaslens.py); the client picks which banding
  // paints the map. The LAYOUT never moves between lenses on purpose —
  // the web stays where the eye left it, and a company whose people sit
  // in three different corners is exactly the thing worth seeing.

  const LENS_KEY = "vira-atlas-lens";

  function activeLens() {
    const ls = S.graph?.lenses || [];
    return ls.find((l) => l.id === S.lens) || ls[0] || null;
  }

  function assignColors() {
    S.colors.clear();
    S.bands.forEach((b, i) => {
      const fallback = b.anchor ? "#a39c8d"
        : CLUSTER_COLORS[(i + (S.bands.some((x) => x.anchor) ? 0 : 1))
                         % CLUSTER_COLORS.length];
      S.colors.set(b.id, S.colorOverrides[`${S.lens}|${b.id}`] || fallback);
    });
  }

  function applyLens(redraw = true) {
    const lens = activeLens();
    S.lens = lens?.id || null;
    S.bands = lens?.bands || [];
    const map = lens?.node_band || {};
    for (const p of S.nodes) p.band = map[p.id] || null;
    // an isolate is a set of band ids, and band ids do not survive a lens
    // change — carrying them over would filter the web down to nothing
    for (const id of [...S.iso.ids])
      if (!S.bands.some((b) => b.id === id)) S.iso.ids.delete(id);
    assignColors();
    renderLenses();
    renderLegend();
    renderCoverage();
    if (redraw) isoChanged(false);
  }

  function setLens(id) {
    if (id === S.lens) return;
    if (editorId) closeGroupEditor();
    S.lens = id;
    lsSet(LENS_KEY, id);
    S.iso = { ids: new Set(), ring: 0 };
    applyLens();
  }

  function renderLenses() {
    const host = $("#atlas-lenses");
    if (!host) return;
    host.innerHTML = "";
    (S.graph?.lenses || []).forEach((l) => {
      const b = el("button", "atlas-lens" + (l.id === S.lens ? " on" : ""),
                   l.label);
      b.title = l.blurb || "";
      b.addEventListener("click", () => setLens(l.id));
      host.appendChild(b);
    });
  }

  function renderCoverage() {
    const host = $("#atlas-coverage");
    if (!host) return;
    const lens = activeLens();
    if (!lens) { host.textContent = ""; return; }
    // A banding is explicit about coverage so a filtered slice never reads
    // as the size of the whole knowledge world.
    const left = lens.total - lens.placed;
    host.textContent = lens.bands.length
      ? `${lens.placed} of ${lens.total} items in ${lens.bands.length} `
        + `${lens.bands.length === 1 ? "band" : "bands"}`
        + (left ? ` · ${left} unplaced` : "")
      : `Nothing to band — no ${lens.label.toLowerCase()} on file for `
        + `these ${lens.total} items.`;
  }

  function initGraph(g) {
    S.graph = g;
    S.fixedLayout = !!g.layout?.basis;
    S.enabledKinds = new Set((g.kinds || []).map((row) => row.id));
    S.lens = lsGet(LENS_KEY, null) || (g.lenses || [])[0]?.id || null;

    const n = g.nodes.length;
    const ring = (d) => d === 1 ? 240 + 7 * Math.sqrt(n)
                : d === 2 ? 420 + 8 * Math.sqrt(n)
                : 560 + 8 * Math.sqrt(n);

    // angular home per cluster so communities start (and stay) grouped
    const order = [...g.nodes].sort((a, b) =>
      String(a.cluster || "zz").localeCompare(String(b.cluster || "zz"))
      || b.act - a.act);
    S.nodes = [];
    S.byId.clear();
    order.forEach((node, i) => {
      const ang = (i / n) * Math.PI * 2 - Math.PI / 2;
      const r = ring(node.degree || 3) * (0.92 + 0.16 * ((i * 7919) % 13) / 13);
      const position = Array.isArray(node.position)
        && node.position.length === 3 ? node.position : null;
      const baseX = position ? Number(position[0]) : Math.cos(ang) * r;
      const baseY = position ? Number(position[1]) : Math.sin(ang) * r;
      const baseZ = position ? Number(position[2]) : 0;
      const baseR = nodeRadius(node);
      const sim = {
        ...node,
        x: baseX * S.display.scale, y: baseY * S.display.scale,
        z: baseZ * S.display.scale,
        baseX, baseY, baseZ,
        homeX: baseX * S.display.scale,
        homeY: baseY * S.display.scale,
        homeZ: baseZ * S.display.scale,
        vx: 0, vy: 0, vz: 0,
        baseR, r: baseR * S.display.nodeSize,
        homeR: ring(node.degree || 3),
        pin: false,
      };
      S.nodes.push(sim);
      S.byId.set(node.id, sim);
    });
    S.ego = { id: "ego", name: g.owner?.name || "me", x: 0, y: 0,
              vx: 0, vy: 0, r: EGO_R, pin: true, ego: true };
    S.byId.set("ego", S.ego);

    S.edges = (g.edges || []).map((e) => ({
      ...e,
      an: S.byId.get(e.a), bn: S.byId.get(e.b),
      structural: e.signals.some((s) =>
        ["photo_cooccur", "group_cochat", "family", "colleague",
         "wikilink", "wiki_link", "wiki_org"].includes(s.type)),
    })).filter((e) => e.an && e.bn);
    S.egoEdges = (g.ego_edges || []).map((e) => ({
      ...e, an: S.ego, bn: S.byId.get(e.b),
    })).filter((e) => e.bn);

    // adjacency over contact-to-contact ties, for bridge-chain BFS
    S.adj = new Map();
    const addAdj = (id, n, e) => {
      if (!S.adj.has(id)) S.adj.set(id, []);
      S.adj.get(id).push({ n, e });
    };
    for (const e of S.edges) {
      addAdj(e.an.id, e.bn, e);
      addAdj(e.bn.id, e.an, e);
    }

    S.sel.clear();
    S.iso = { ids: new Set(), ring: 0 };
    S.shown = null;
    editorId = null;
    recomputeSel();
    card.style.display = "none";
    applyLens(false);
    renderKindFilters();
    renderSearchResults();
    syncControlInputs();
    updateIsoBar();
    initTimeline(g.timeline || {});
    const missing = g.scope?.unreadable
      ? ` · ${g.scope.unreadable} unreadable notes` : "";
    const semantic = g.layout?.semantic_nodes
      ? ` · ${g.layout.semantic_nodes} semantically placed` : "";
    const fallback = g.layout?.fallback_nodes
      ? ` · ${g.layout.fallback_nodes} deterministic fallback` : "";
    $("#atlas-meta").textContent =
      `${g.nodes.length} items · ${g.edges.length} relations${semantic}${fallback}${missing}`
      + ` · composed ` + fmtTime(g.generated);

    if (R3) {
      // the renderer seeds its own layout on the sphere, settles it and
      // frames the graph's actual extent
      R3.setGraph();
      R3.setRunning(true);   // the observer stops it again if hidden
    } else {
      const fit = Math.min(stage.clientWidth || 1100,
                           stage.clientHeight || 700);
      S.cam = { k: Math.max(0.16, Math.min(1, (fit / 2 - 24) / ring(3))),
                x: 0, y: 0 };
      resize();
      if (S.fixedLayout) {
        S.alpha = 0;
        draw();
      } else if (REDUCED_MOTION) {
        S.alpha = 1;
        for (let i = 0; i < 420; i++) tick(1 / 60);
        S.alpha = 0;
        draw();
      } else {
        S.alpha = 1;
        wake();
      }
    }
    loadFaces();
  }

  function nodeRadius(node) {
    return 9 + Math.min(11, Math.sqrt((node.act || 0) / 14));
  }

  function loadFaces() {
    // The full-vault renderer uses one GPU point cloud instead of thousands
    // of per-person texture draw calls. Faces return automatically when a
    // filtered slice is small enough for the detailed renderer.
    if (S.nodes.length > 2500) return;
    // avatars trickle in; each arrival repaints once
    S.nodes.forEach((node) => {
      if (node.kind === "person" && node.face && !S.imgs.has(node.id)) {
        const img = new Image();
        const entry = { img, ok: false };
        S.imgs.set(node.id, entry);
        img.onload = () => {
          entry.ok = true;
          if (R3) R3.faceLoaded(node.id);
          else if (!S.running) draw();
        };
        img.src = "/api/atlas/face/" + node.id;
      }
    });
    if (S.graph.owner?.pid && !S.imgs.has("ego")) {
      const img = new Image();
      const entry = { img, ok: false };
      S.imgs.set("ego", entry);
      img.onload = () => {
        entry.ok = true;
        if (R3) R3.faceLoaded("ego");
        else if (!S.running) draw();
      };
      img.src = "/api/atlas/face/" + S.graph.owner.pid;
    }
  }

  // ---------- simulation ----------

  function tick(dt) {
    const nodes = S.nodes;
    const repel = 1300;
    // pairwise repulsion (200 nodes -> 20k pairs, fine per frame)
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 > 240 * 240) continue;
        if (d2 < 1) { dx = (i % 2 ? 1 : -1); dy = 0.5; d2 = 1.25; }
        const f = repel / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      }
    }
    // springs along edges — strong ties pull close
    for (const e of S.edges) {
      if (!isEdgeShown(e)) continue;
      const w = Math.min(1.5, e.weight) / 1.5;
      const rest = 300 - 190 * w;
      const k = (e.structural ? 0.045 : 0.012) * (0.4 + 0.6 * w);
      spring(e.an, e.bn, rest, k);
    }
    if (!S.hideEgo) {
      for (const e of S.egoEdges) {
        const w = Math.min(1, e.weight);
        spring(e.an, e.bn, e.bn.homeR * (1.15 - 0.35 * w), 0.012);
      }
    }
    // radial home (degree ring) + integration
    for (const p of nodes) {
      const d = Math.hypot(p.x, p.y) || 1;
      const pull = (p.homeR - d) * 0.008;
      p.vx += (p.x / d) * pull;
      p.vy += (p.y / d) * pull;
      if (p.pin) { p.vx = p.vy = 0; continue; }
      p.vx *= 0.86; p.vy *= 0.86;
      const sp = Math.hypot(p.vx, p.vy);
      const cap = 260 * S.alpha + 20;
      if (sp > cap) { p.vx *= cap / sp; p.vy *= cap / sp; }
      p.x += p.vx * dt * S.alpha * 3.2;
      p.y += p.vy * dt * S.alpha * 3.2;
    }
    S.alpha = Math.max(0, S.alpha - dt * 0.14);
  }

  function spring(a, b, rest, k) {
    let dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.hypot(dx, dy) || 1;
    const f = (d - rest) * k;
    dx /= d; dy /= d;
    if (!a.pin) { a.vx += dx * f; a.vy += dy * f; }
    if (!b.pin) { b.vx -= dx * f; b.vy -= dy * f; }
  }

  function wake(heat = 0.6) {
    if (R3) { R3.wake(heat); return; }
    if (S.fixedLayout) { draw(); return; }
    S.alpha = Math.max(S.alpha, heat);
    if (REDUCED_MOTION) {
      for (let i = 0; i < 200; i++) tick(1 / 60);
      S.alpha = 0;
      draw();
      return;
    }
    if (!S.running && S.visible) {
      S.running = true;
      let last = performance.now();
      const step = (t) => {
        if (!S.running) return;
        const dt = Math.min(0.05, (t - last) / 1000);
        last = t;
        if (S.alpha > 0.005) tick(dt);
        draw();
        if (S.alpha <= 0.005 && !S.dragNode) {
          S.running = false;   // settled — stop burning frames
          return;
        }
        S.raf = requestAnimationFrame(step);
      };
      S.raf = requestAnimationFrame(step);
    }
  }

  // ---------- projection ----------

  const w2sX = (x) => (x - S.cam.x) * S.cam.k + canvas.clientWidth / 2;
  const w2sY = (y) => (y - S.cam.y) * S.cam.k + canvas.clientHeight / 2;
  const s2wX = (x) => (x - canvas.clientWidth / 2) / S.cam.k + S.cam.x;
  const s2wY = (y) => (y - canvas.clientHeight / 2) / S.cam.k + S.cam.y;

  function nodeAt(sx, sy) {
    const wx = s2wX(sx), wy = s2wY(sy);
    const hitR = (r) => Math.max(r, 12 / S.cam.k);
    if (!S.hideEgo && !S.shown && S.ego
        && Math.hypot(wx - S.ego.x, wy - S.ego.y) < hitR(EGO_R)) return S.ego;
    let best = null, bestD = 1e9;
    for (const p of S.nodes) {
      if (!isShown(p)) continue;
      const d = Math.hypot(wx - p.x, wy - p.y);
      if (d < hitR(p.r) + 2 / S.cam.k && d < bestD) { best = p; bestD = d; }
    }
    return best;
  }

  // ---------- filters + temporal replay ----------

  const parseTime = (value) => value ? Date.parse(value) : NaN;

  function queryTerms(query) {
    const terms = [];
    const re = /(-?)(?:(kind|type|tag|source|company|title):)?(?:"([^"]+)"|(\S+))/gi;
    let match;
    while ((match = re.exec(query || ""))) {
      terms.push({ not: match[1] === "-", key: (match[2] || "").toLowerCase(),
                   value: (match[3] || match[4] || "").toLowerCase() });
    }
    return terms;
  }

  function searchText(node, key) {
    const fields = {
      kind: [node.kind], type: [node.kind], tag: node.tags || [],
      source: [node.source_name, node.source_id, node.ref],
      company: [node.company], title: [node.title],
    };
    const values = key ? (fields[key] || []) : [
      node.name, node.kind, node.company, node.title, node.qualifier,
      node.source_name, node.source_id, node.ref, ...(node.tags || []),
    ];
    return values.filter(Boolean).join(" ").toLowerCase();
  }

  function matchesSearch(node) {
    if (!S.match) return true;
    return queryTerms(S.match).every((term) => {
      const hit = searchText(node, term.key).includes(term.value);
      return term.not ? !hit : hit;
    });
  }

  function passesNodeFilters(node) {
    return S.enabledKinds.has(node.kind)
      && (!S.hideOrphans || Number(node.graph_degree || 0) > 0)
      && (!S.starredOnly || S.starred.has(node.id))
      && (!S.filterSearch || matchesSearch(node));
  }

  function hasNodeFilters() {
    const totalKinds = (S.graph?.kinds || []).length;
    return S.hideOrphans || S.starredOnly || (S.filterSearch && !!S.match)
      || S.enabledKinds.size !== totalKinds;
  }

  function timeActive(item) {
    if (!S.time.at) return true;
    const at = S.time.at;
    if (S.time.axis === "recorded") {
      const learned = parseTime(item.recorded_at);
      return !Number.isFinite(learned) || learned <= at;
    }
    const start = parseTime(item.valid_from);
    const end = parseTime(item.valid_to);
    return (!Number.isFinite(start) || start <= at)
      && (!Number.isFinite(end) || at < end);
  }

  const isShown = (p) => timeActive(p) && (!S.shown || S.shown.has(p.id));
  const isEdgeShown = (e) => timeActive(e)
    && (!S.shown || (S.shown.has(e.an.id) && S.shown.has(e.bn.id)));

  function initTimeline(timeline) {
    S.time.timeline = timeline;
    setTimelineBounds();
    S.time.at = null;
    syncTimelineRange();
    paintTimeline();
  }

  function setTimelineBounds() {
    const row = S.time.timeline[S.time.axis] || S.time.timeline;
    const min = parseTime(row.min);
    const max = parseTime(row.max);
    S.time.min = Number.isFinite(min) ? min
      : Number.isFinite(max) ? max : Date.now();
    S.time.max = Number.isFinite(max) ? max : S.time.min;
    if (S.time.at)
      S.time.at = Math.max(S.time.min, Math.min(S.time.max, S.time.at));
  }

  function syncTimelineRange() {
    const range = $("#world-time");
    if (!range) return;
    if (!S.time.at) { range.value = "1000"; return; }
    const span = Math.max(1, S.time.max - S.time.min);
    range.value = String(Math.round(
      1000 * (S.time.at - S.time.min) / span));
  }

  function paintTimeline() {
    $("#world-axis-valid")?.classList.toggle("on", S.time.axis === "valid");
    $("#world-axis-recorded")?.classList.toggle(
      "on", S.time.axis === "recorded");
    const label = $("#world-time-label");
    if (label) label.textContent = S.time.at
      ? new Date(S.time.at).toLocaleDateString(undefined,
          { year: "numeric", month: "short", day: "numeric",
            timeZone: "UTC" })
      : "Latest";
    const minLabel = $("#world-time-min");
    const maxLabel = $("#world-time-max");
    if (minLabel) minLabel.textContent = shortDate(
      new Date(S.time.min).toISOString(), "day");
    if (maxLabel) maxLabel.textContent = shortDate(
      new Date(S.time.max).toISOString(), "day");
    const row = S.time.timeline[S.time.axis] || {};
    const active = S.nodes.filter((p) => timeActive(p)).length;
    const summary = $("#world-time-summary");
    if (summary) summary.textContent =
      active.toLocaleString() + " of " + S.nodes.length.toLocaleString()
      + " items" + (row.undated_nodes
        ? " · " + row.undated_nodes + " without this date"
        : " · every item dated");
  }

  function timelineChanged() {
    recomputeIso();
    recomputeSel();
    updateIsoBar();
    if (!S.time.playing) renderSelCard();
    renderSearchResults();
    paintFilterCount();
    paintTimeline();
    if (R3) R3.refreshPhysics();
    wake(0.18);
    draw();
  }

  function paintTimelinePlayback() {
    const play = $("#world-play");
    if (!play) return;
    play.textContent = S.time.playing ? "Pause" : "Play";
    play.classList.toggle("on", S.time.playing);
    play.setAttribute("aria-label", S.time.playing
      ? "Pause timeline" : "Play timeline");
  }

  function stopTimelinePlayback(refresh = true) {
    if (!S.time.playing && !S.time.raf) return;
    S.time.playing = false;
    cancelAnimationFrame(S.time.raf);
    S.time.raf = 0;
    paintTimelinePlayback();
    if (refresh) renderSelCard();
  }

  function timelineFrame(now) {
    if (!S.time.playing) return;
    if (!S.time.lastFrame) S.time.lastFrame = now;
    const elapsed = now - S.time.lastFrame;
    // Rebuilding visibility over 25k nodes and 92k links at 60fps would
    // turn playback into a benchmark. Eight updates per second remains
    // visually continuous while leaving the graph interactive.
    if (elapsed >= 125) {
      const span = Math.max(1, S.time.max - S.time.min);
      const start = S.time.at == null ? S.time.min : S.time.at;
      S.time.at = Math.min(S.time.max,
        start + span * (elapsed / 30000) * S.time.speed);
      S.time.lastFrame = now;
      syncTimelineRange();
      timelineChanged();
      if (S.time.at >= S.time.max) {
        S.time.at = null;
        syncTimelineRange();
        stopTimelinePlayback(false);
        timelineChanged();
        return;
      }
    }
    S.time.raf = requestAnimationFrame(timelineFrame);
  }

  function toggleTimelinePlayback() {
    if (S.time.playing) { stopTimelinePlayback(); return; }
    if (S.time.at == null || S.time.at >= S.time.max) {
      S.time.at = S.time.min;
      syncTimelineRange();
    }
    S.time.playing = true;
    S.time.lastFrame = performance.now();
    paintTimelinePlayback();
    timelineChanged();
    S.time.raf = requestAnimationFrame(timelineFrame);
  }

  function recomputeIso() {
    if (!S.iso.ids.size && !S.time.at && !hasNodeFilters()) {
      S.shown = null; return;
    }
    const shown = new Set();
    for (const p of S.nodes) {
      if (!timeActive(p)) continue;
      if (!passesNodeFilters(p)) continue;
      if (!S.iso.ids.size || (p.band && S.iso.ids.has(p.band)))
        shown.add(p.id);
    }
    // ring expansions: people directly connected to what is shown
    for (let r = 0; r < S.iso.ring; r++) {
      const add = [];
      for (const e of S.edges) {
        if (!timeActive(e)) continue;
        const a = shown.has(e.an.id), b = shown.has(e.bn.id);
        const other = a ? e.bn : e.an;
        if (a !== b && passesNodeFilters(other)) add.push(other.id);
      }
      if (!add.length) break;
      add.forEach((id) => shown.add(id));
    }
    S.shown = shown;
  }

  function isoChanged(fit = true) {
    recomputeIso();
    if (S.shown)
      for (const p of [...S.sel])
        if (!S.shown.has(p.id)) S.sel.delete(p);
    recomputeSel();
    syncLegend();
    syncKindFilters();
    renderSearchResults();
    paintFilterCount();
    updateIsoBar();
    if (R3) R3.refreshPhysics();
    if (fit && S.shown && S.shown.size) fitShown();
    draw();
    renderSelCard();
  }

  function fitShown() {
    const pts = S.nodes.filter((p) => S.shown.has(p.id));
    if (!pts.length) return;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    S.cam.x = (minX + maxX) / 2;
    S.cam.y = (minY + maxY) / 2;
    const w = Math.max(140, maxX - minX + 200);
    const h = Math.max(140, maxY - minY + 200);
    S.cam.k = Math.max(0.25, Math.min(2.2,
      Math.min(canvas.clientWidth / w, canvas.clientHeight / h)));
  }

  function updateIsoBar() {
    const bar = $("#atlas-iso");
    if (!bar) return;
    if (!S.iso.ids.size) { bar.style.display = "none"; return; }
    bar.style.display = "";
    bar.innerHTML = "";
    const labels = [...S.iso.ids].map((id) =>
      S.bands.find((b) => b.id === id)?.label || id);
    const n = S.shown ? S.shown.size : 0;
    bar.appendChild(el("span", "atlas-iso-label",
      `${labels.length ? `Showing ${labels.join(" + ")} — ` : ""}`
      + `${n} ${n === 1 ? "item" : "items"}`
      + (S.iso.ring ? ` (+${S.iso.ring} ring${S.iso.ring > 1 ? "s" : ""}`
                      + " of connections)" : "")));
    const grow = el("button", "fchip sm", "+ connected items");
    grow.title = "Also show items directly connected to what is shown";
    grow.addEventListener("click", () => { S.iso.ring += 1; isoChanged(); });
    bar.appendChild(grow);
    if (S.iso.ring) {
      const shrink = el("button", "fchip sm", "fewer");
      shrink.addEventListener("click", () => {
        S.iso.ring = Math.max(0, S.iso.ring - 1);
        isoChanged();
      });
      bar.appendChild(shrink);
    }
    const all = el("button", "fchip sm", "All kinds");
    all.addEventListener("click", () => {
      S.iso = { ids: new Set(), ring: 0 };
      isoChanged(false);
    });
    bar.appendChild(all);
  }

  // ---------- drawing ----------

  function resize() {
    if (R3) { R3.resize(); return; }
    const dpr = devicePixelRatio || 1;
    const w = stage.clientWidth, h = stage.clientHeight;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function matchDim(node) {
    return S.match && !S.filterSearch && !matchesSearch(node);
  }

  function draw() {
    if (R3) { R3.paint(); return; }
    if (!S.graph) return;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    const hasSel = S.sel.size > 0;
    // hover featuring only when nothing is selected — a selection owns
    // the stage, everything else is just a hint of what's left
    const focus = hasSel ? null : S.hover;

    // degree guide rings (skipped while isolating — a clean stage)
    if (!S.shown) {
      ctx.strokeStyle = `rgba(143,141,133,${hasSel ? 0.03 : 0.06})`;
      ctx.lineWidth = 1;
      const rings = new Set(S.nodes.map((p) => p.homeR));
      for (const r of rings) {
        ctx.beginPath();
        ctx.arc(w2sX(0), w2sY(0), r * S.cam.k, 0, 7);
        ctx.stroke();
      }
    }

    // ego spokes — faint, they carry the "everyone connects to me" story
    if (!S.hideEgo && !S.shown) {
      for (const e of S.egoEdges) {
        const hot = hasSel ? S.sel.has(e.bn)
                           : focus && (e.bn === focus || S.ego === focus);
        ctx.strokeStyle = flatEdgeStroke(S.ego, e.bn, [138, 132, 120],
          hot ? 0.4 : (hasSel ? 0.02 : 0.05), hot ? 0.4 : 0);
        ctx.lineWidth = (hot ? 1.4 : 1) * S.display.linkThickness;
        ctx.beginPath();
        traceFlatEdge(e, S.ego, e.bn);
        ctx.stroke();
      }
    }

    // contact-to-contact edges. Each branch names a grey, an alpha, a
    // width and a LIFT - the same six-part style the 3D renderer paints,
    // so the two surfaces blend, whiten and dim ties the same way.
    for (const e of S.edges) {
      if (!isEdgeShown(e)) continue;
      const hot = focus && (e.an === focus || e.bn === focus);
      const w = Math.min(1.5, e.weight) / 1.5;
      let rgb, alpha, lift = 0;
      if (hasSel && S.selEdges.has(e)) {
        // the featured links — ties among the selected
        rgb = [222, 214, 197]; alpha = 0.95; lift = 0.6;
        ctx.lineWidth = (1.6 + 2.2 * w) * S.display.linkThickness;
      } else if (hasSel && S.selPathEdges.has(e)) {
        // bridge chains connecting selections that share no direct tie
        rgb = [163, 156, 141]; alpha = 0.75; lift = 0.45;
        ctx.lineWidth = (1.3 + 1.2 * w) * S.display.linkThickness;
      } else if (hasSel && (S.sel.has(e.an) || S.sel.has(e.bn))) {
        // spokes from a selected node out to its world — prominent for a
        // single selection, quieter once the story is between selections
        const spoke = S.sel.size === 1 ? 0.45 : 0.16;
        rgb = [207, 203, 194]; alpha = spoke * (0.5 + 0.5 * w); lift = 0.3;
        ctx.lineWidth = (0.8 + 1.4 * w) * S.display.linkThickness;
      } else if (hasSel) {
        // the hint of what's left
        rgb = [143, 141, 133]; alpha = 0.015 + 0.03 * w;
        ctx.lineWidth = (0.6 + w) * S.display.linkThickness;
      } else if (hot) {
        rgb = e.shared_interest ? [138, 132, 120] : [207, 203, 194];
        alpha = e.shared_interest ? 0.85 : 0.55; lift = 0.45;
        ctx.lineWidth = (1 + 2 * w) * S.display.linkThickness;
      } else {
        alpha = 0.05 + 0.3 * w * w;
        // isolating strips the noise — let the remaining ties read clearly
        if (S.shown) alpha = Math.min(0.8, alpha * 3 + 0.12);
        if (matchDim(e.an) || matchDim(e.bn)) alpha *= 0.2;
        if (e.shared_interest) { rgb = [138, 132, 120]; alpha += 0.08; }
        else rgb = [143, 141, 133];
        ctx.lineWidth = (0.6 + 1.8 * w) * S.display.linkThickness;
      }
      ctx.strokeStyle = flatEdgeStroke(e.an, e.bn, rgb, alpha, lift);
      ctx.beginPath();
      traceFlatEdge(e, e.an, e.bn);
      ctx.stroke();
    }

    // nodes
    for (const p of S.nodes) {
      if (!isShown(p)) continue;
      drawNode(p, focus);
    }
    if (!S.hideEgo && !S.shown) drawNode(S.ego, focus);
  }

  // A tie blends from A's colour to B's (white when the toggle is on), and
  // its alpha rides the link-opacity slider. `lift` pulls a featured tie
  // part-way toward the branch's own grey so it still reads hot.
  function flatEdgeStroke(A, B, grey, alpha, lift) {
    const a = Math.min(1, alpha * (S.display.linkOpacity ?? 1));
    if (S.display.whiteLinks) return `rgba(255,255,255,${a})`;
    const tint = (p) => hexRgb(p.ego ? "#a39c8d"
      : (p.band && S.colors.get(p.band)) || "#6a6a64");
    const mix = (c) => c.map((v, i) => v + (grey[i] - v) * lift);
    const ca = mix(tint(A)), cb = mix(tint(B));
    const g = ctx.createLinearGradient(w2sX(A.x), w2sY(A.y),
                                       w2sX(B.x), w2sY(B.y));
    g.addColorStop(0, `rgba(${ca.map(Math.round).join(",")},${a})`);
    g.addColorStop(1, `rgba(${cb.map(Math.round).join(",")},${a})`);
    return g;
  }
  const hexCache = new Map();
  function hexRgb(hex) {
    let c = hexCache.get(hex);
    if (!c) {
      const n = parseInt(hex.slice(1, 7), 16);
      c = Number.isFinite(n) ? [(n >> 16) & 255, (n >> 8) & 255, n & 255]
                             : [106, 106, 100];
      hexCache.set(hex, c);
    }
    return c;
  }

  function traceFlatEdge(edge, A, B) {
    const ax = w2sX(A.x), ay = w2sY(A.y);
    const bx = w2sX(B.x), by = w2sY(B.y);
    ctx.moveTo(ax, ay);
    if (!S.display.curvedLinks || S.display.linkCurve <= 0) {
      ctx.lineTo(bx, by);
      return;
    }
    const dx = bx - ax, dy = by - ay;
    const distance = Math.hypot(dx, dy) || 1;
    let hash = 0;
    for (const ch of `${edge.an?.id || "ego"}|${edge.bn?.id || ""}`)
      hash = (hash * 31 + ch.charCodeAt(0)) | 0;
    const sign = hash & 1 ? 1 : -1;
    const offset = distance * S.display.linkCurve * 2 * sign;
    ctx.quadraticCurveTo((ax + bx) / 2 - dy / distance * offset,
                         (ay + by) / 2 + dx / distance * offset, bx, by);
  }

  function drawNode(p, focus) {
    const sx = w2sX(p.x), sy = w2sY(p.y);
    const r = Math.max(4, p.r * S.cam.k * (p.ego ? 1 : 1));
    const isSel = S.sel.has(p);
    let alpha = 1;
    if (S.sel.size) {
      if (isSel) alpha = 1;
      else if (S.selPathNodes.has(p.id)) alpha = 0.95;
      else if (S.shared.has(p.id)) alpha = 0.9;
      else if (S.neighbors.has(p.id)) alpha = S.sel.size === 1 ? 0.8 : 0.3;
      else alpha = 0.08;                       // the hint of what's left
      if (p.ego) alpha = Math.max(alpha, 0.35);
      if (p === S.hover) alpha = Math.max(alpha, 0.7);
    } else if (matchDim(p) && !p.ego) {
      alpha = 0.22;
    }
    ctx.save();
    ctx.globalAlpha = alpha * S.display.nodeOpacity;

    // cluster / ego ring
    const color = p.ego ? "#a39c8d"
      : (p.band && S.colors.get(p.band)) || "#6a6a64";
    ctx.beginPath();
    ctx.arc(sx, sy, r + (p.ego ? 3 : 2), 0, 7);
    ctx.fillStyle = "#191b19";
    ctx.fill();
    ctx.lineWidth = isSel || p === S.hover
      ? 3 : p.ego ? 2.5 : 1.6;
    ctx.strokeStyle = isSel ? "#d4ccba" : color;
    // vault people wear a dashed ring — in the notes, not yet met
    if (p.vault) ctx.setLineDash([4, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    // face (clipped) or letter tile
    const entry = S.imgs.get(p.id);
    ctx.beginPath();
    ctx.arc(sx, sy, r, 0, 7);
    ctx.clip();
    if (entry?.ok) {
      ctx.drawImage(entry.img, sx - r, sy - r, r * 2, r * 2);
    } else {
      ctx.fillStyle = tileColor(p.name);
      ctx.fillRect(sx - r, sy - r, r * 2, r * 2);
      ctx.fillStyle = "rgba(207,203,194,.92)";
      ctx.font = `600 ${Math.max(8, r * 0.78)}px -apple-system, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(initials(p.name), sx, sy + r * 0.05);
    }
    if (S.display.sphericalNodes) {
      const shade = ctx.createRadialGradient(
        sx - r * .38, sy - r * .42, r * .08, sx, sy, r * 1.05);
      shade.addColorStop(0, "rgba(255,255,255,.34)");
      shade.addColorStop(.34, "rgba(255,255,255,.06)");
      shade.addColorStop(.72, "rgba(0,0,0,.06)");
      shade.addColorStop(1, "rgba(0,0,0,.58)");
      ctx.fillStyle = shade;
      ctx.fillRect(sx - r, sy - r, r * 2, r * 2);
    }
    ctx.restore();

    // label
    const showLabel = p.ego || p === S.hover || isSel
      || S.selPathNodes.has(p.id) || S.shared.has(p.id)
      || (S.sel.size === 1 && S.neighbors.has(p.id))
      || S.cam.k > 1.15 || (S.match && !matchDim(p));
    if (showLabel && alpha > 0.25) {
      ctx.font = `${p.ego ? 700 : 500} 11px -apple-system, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const label = p.ego ? p.name : firstLast(p.name);
      ctx.fillStyle = "rgba(7,8,8,.8)";
      const tw = ctx.measureText(label).width;
      ctx.fillRect(sx - tw / 2 - 3, sy + r + 3, tw + 6, 14);
      ctx.fillStyle = p.ego ? "#a39c8d"
        : isSel ? "#e4ddcd" : "rgba(207,203,194,.92)";
      ctx.fillText(label, sx, sy + r + 5);
    }
  }

  function firstLast(name) {
    const parts = (name || "").split(/\s+/);
    return parts.length > 2 ? parts[0] + " " + parts[parts.length - 1]
                            : name;
  }

  function tileColor(name) {
    let h = 0;
    for (const ch of name || "?") h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${20 + (h % 70)}, 16%, 27%)`;   // warm earth band only
  }

  // ---------- legend ----------

  const legendChips = new Map();   // band id -> chip element
  let editorId = null;             // group whose member editor is open

  function renderLegend() {
    const host = $("#atlas-legend");
    host.innerHTML = "";
    legendChips.clear();
    const editable = !!activeLens()?.editable;
    S.bands.forEach((c) => {
      const item = el("span", "atlas-legend-item");
      const picker = document.createElement("input");
      picker.type = "color";
      picker.className = "atlas-color";
      picker.value = S.colors.get(c.id);
      picker.title = `Change the color for ${c.label}`;
      picker.setAttribute("aria-label", `Color for ${c.label}`);
      const chip = el("button", "atlas-chip");
      const dot = el("span", "atlas-dot");
      dot.style.background = S.colors.get(c.id);
      chip.appendChild(dot);
      chip.appendChild(el("span", null, `${c.label} (${c.size})`));
      chip.title = editable
        ? "Show just this group — right-click to rename, edit members, "
          + "or remove it"
        : "Show just this kind — right-click to select all visible items";
      chip.addEventListener("click", () => {
        S.match = "";
        $("#atlas-search").value = "";
        if (S.iso.ids.has(c.id)) S.iso.ids.delete(c.id);
        else S.iso.ids.add(c.id);
        S.iso.ring = 0;
        isoChanged();
      });
      chip.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        groupMenu(ev.clientX, ev.clientY, c.id);
      });
      picker.addEventListener("input", () => {
        S.colorOverrides[`${S.lens}|${c.id}`] = picker.value;
        S.colors.set(c.id, picker.value);
        dot.style.background = picker.value;
        saveControls();
        draw();
      });
      legendChips.set(c.id, chip);
      item.appendChild(picker);
      item.appendChild(chip);
      host.appendChild(item);
    });
  }

  function syncLegend() {
    for (const [cid, chip] of legendChips)
      chip.classList.toggle("on", S.iso.ids.has(cid));
  }

  function renderKindFilters() {
    const host = $("#atlas-filter-kinds");
    if (!host) return;
    host.innerHTML = "";
    for (const row of S.graph?.kinds || []) {
      const label = el("label", "atlas-kind-filter");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = S.enabledKinds.has(row.id);
      input.dataset.kind = row.id;
      input.addEventListener("change", () => {
        if (input.checked) S.enabledKinds.add(row.id);
        else S.enabledKinds.delete(row.id);
        isoChanged(false);
      });
      label.append(input, document.createTextNode(`${row.label} ${row.count}`));
      host.appendChild(label);
    }
  }

  function syncKindFilters() {
    document.querySelectorAll("#atlas-filter-kinds input[data-kind]")
      .forEach((input) => {
        input.checked = S.enabledKinds.has(input.dataset.kind);
      });
  }

  function filteredNodes(ignoreSearch = false) {
    return S.nodes.filter((p) => timeActive(p)
      && S.enabledKinds.has(p.kind)
      && (!S.hideOrphans || Number(p.graph_degree || 0) > 0)
      && (!S.starredOnly || S.starred.has(p.id))
      && (ignoreSearch || matchesSearch(p)));
  }

  function renderSearchResults() {
    const host = $("#atlas-search-results");
    if (!host) return;
    host.innerHTML = "";
    host.classList.toggle("live", !!S.match);
    if (!S.match) return;
    const matches = filteredNodes().sort((a, b) =>
      Number(b.graph_degree || 0) - Number(a.graph_degree || 0)
      || String(a.name).localeCompare(String(b.name)));
    for (const p of matches.slice(0, 12)) {
      const button = el("button", "atlas-search-result");
      button.type = "button";
      button.appendChild(el("b", null, p.name));
      button.appendChild(el("span", null, kindLabel(p.kind)));
      if (p.source_name)
        button.appendChild(el("span", null, p.source_name));
      button.addEventListener("click", () => {
        if (S.filterSearch && S.shown && !S.shown.has(p.id)) return;
        navigateArticle(p);
      });
      host.appendChild(button);
    }
    if (!matches.length)
      host.appendChild(el("div", "hint atlas-member", "No matching items"));
    else if (matches.length > 12)
      host.appendChild(el("div", "hint atlas-member",
        `${matches.length - 12} more matches`));
  }

  function paintFilterCount() {
    const host = $("#atlas-filter-count");
    if (!host || !S.graph) return;
    const shown = S.shown ? S.shown.size : S.nodes.length;
    const matches = S.match ? filteredNodes().length : shown;
    host.textContent = `${shown.toLocaleString()} of `
      + `${S.nodes.length.toLocaleString()} items visible`
      + (S.match ? ` · ${matches.toLocaleString()} search matches` : "")
      + (S.starred.size ? ` · ${S.starred.size.toLocaleString()} starred` : "");
  }

  // ---------- group curation (rename / members / remove / create) ----------

  function groupMenu(x, y, cid) {
    const lens = activeLens();
    const c = S.bands.find((k) => k.id === cid);
    if (!c) return;
    const members = S.nodes.filter((p) => p.band === c.id);
    const items = [
      { head: c.label + (c.custom ? " · your group"
                                  : ` · ${lens?.label || "band"}`) },
      { label: "Show only these items", run: () => {
          S.iso = { ids: new Set([c.id]), ring: 0 };
          isoChanged();
        } },
      // selecting a whole band is how you compare two of them: the
      // selection card already draws the ties BETWEEN what is selected,
      // so two company chips answer "how do these firms connect?"
      { label: "Select all visible items here", run: () => {
          members.forEach((p) => S.sel.add(p));
          selectionChanged();
        } },
    ];
    // A circle with a stable identity (server/circles.py) has a story of
    // its own and a name the owner can override — that rename lives in
    // the circles store, keyed on the identity, so it survives rebuilds.
    if (c.circle)
      items.push(
        { label: c.story ? "About this circle"
                         : "About this circle (not read yet)",
          run: () => { S.iso = { ids: new Set([c.id]), ring: 0 };
                       isoChanged(); } },
        { label: "Rename circle…", run: () => renameCircle(c) },
        { label: "Have Vira read it again", run: () => rereadCircle(c) });
    // Only the Groups lens is a store the owner owns. Companies, circles
    // and locations are derived every read, so a rename there would be
    // an edit with a shelf life of one refresh.
    if (lens?.editable)
      items.push(
        { label: "Edit members…", run: () => openGroupEditor(c.id) },
        { label: "Rename group…", run: () => renameGroup(c) },
        { sep: true },
        { label: "Remove group…", run: () => removeGroup(c) });
    showContextMenu(x, y, items);
  }

  function applyGroups(r) {
    if (!r || !r.clusters) return;
    S.graph.clusters = r.clusters;
    S.graph.node_cluster = r.node_cluster || {};
    for (const p of S.nodes)
      p.cluster = S.graph.node_cluster[p.id] || null;
    if (r.lenses) S.graph.lenses = r.lenses;
    if (editorId) {
      editorId = r.gid || editorId;
      if (!r.clusters.some((c) => c.id === editorId)) editorId = null;
    }
    applyLens();
  }

  async function renameGroup(c) {
    const name = prompt("Group name", c.label);
    if (!name || !name.trim() || name.trim() === c.label) return;
    try {
      applyGroups(await post(`/api/atlas/groups/${c.id}/rename`,
        { label: name.trim() }));
      toast("Group renamed");
    } catch (e) { toast("Rename failed: " + e.message); }
  }

  async function removeGroup(c) {
    const note = c.custom ? "" : " It will not come back on a rebuild.";
    if (!confirm(`Remove "${c.label}" as a group? The people stay — only`
        + ` the grouping goes.${note}`)) return;
    try {
      applyGroups(await post(`/api/atlas/groups/${c.id}/dissolve`, {}));
      toast("Group removed");
    } catch (e) { toast("Remove failed: " + e.message); }
  }

  async function assignGroup(pid, group) {
    try {
      applyGroups(await post("/api/atlas/groups/assign", { pid, group }));
    } catch (e) { toast("Group change failed: " + e.message); }
  }

  async function createGroupWith(pid) {
    const name = prompt("New group name");
    if (!name || !name.trim()) return;
    try {
      const r = await post("/api/atlas/groups", { label: name.trim() });
      applyGroups(r);
      if (pid && r.gid) await assignGroup(pid, r.gid);
      else if (r.gid) openGroupEditor(r.gid);
      toast("Group created");
    } catch (e) { toast("Create failed: " + e.message); }
  }

  function groupChooser(x, y, p) {
    const items = [{ head: "Group for " + firstLast(p.name) }];
    for (const c of S.graph.clusters) {
      if (c.id === p.cluster) continue;
      items.push({ label: "→ " + c.label,
                   run: () => assignGroup(p.id, c.id) });
    }
    if (p.cluster) {
      const cur = S.graph.clusters.find((c) => c.id === p.cluster);
      items.push({ sep: true });
      items.push({ label: "Remove from " + (cur ? cur.label : "group"),
                   run: () => assignGroup(p.id, "") });
    }
    items.push({ sep: true });
    items.push({ label: "New group…", run: () => createGroupWith(p.id) });
    showContextMenu(x, y, items);
  }

  // ---------- the group member editor (lives in the side card) ----------

  function openGroupEditor(cid) {
    editorId = cid;
    renderGroupEditor();
  }

  function closeGroupEditor() {
    editorId = null;
    renderSelCard();
  }

  async function editorToggle(p) {
    if (!p || p.ego) return;
    await assignGroup(p.id, p.cluster === editorId ? "" : editorId);
  }

  function renderGroupEditor() {
    const c = S.graph.clusters.find((k) => k.id === editorId);
    if (!c) { closeGroupEditor(); return; }
    card.style.display = "";
    card.innerHTML = "";
    const head = el("div", "atlas-card-head");
    const mid = el("div", "atlas-card-name");
    const nm = el("div", "click", c.label);
    nm.title = "Rename";
    nm.addEventListener("click", () => renameGroup(c));
    mid.appendChild(nm);
    mid.appendChild(el("div", "hint",
      `${c.size} member${c.size === 1 ? "" : "s"} · click people on the`
      + " map to add or remove"));
    head.appendChild(mid);
    const x = el("button", "idea-del", "×");
    x.addEventListener("click", closeGroupEditor);
    head.appendChild(x);
    card.appendChild(head);

    const list = el("div", "atlas-card-edges");
    const members = S.nodes.filter((p) => p.cluster === c.id)
      .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    members.forEach((p) => {
      const row = el("div", "atlas-edge atlas-member");
      row.appendChild(el("span", "atlas-member-name", p.name));
      const rm = el("button", "idea-del", "×");
      rm.title = "Remove from group";
      rm.addEventListener("click", () => assignGroup(p.id, ""));
      row.appendChild(rm);
      list.appendChild(row);
    });
    if (!members.length)
      list.appendChild(el("div", "hint",
        "No members yet — click people on the map or add them by name."));
    card.appendChild(list);

    const addWrap = el("div", "atlas-add-member");
    const inp = el("input", "search");
    inp.type = "search";
    inp.placeholder = "Add a person by name…";
    const sug = el("div", "atlas-add-sug");
    inp.addEventListener("input", () => {
      const q = inp.value.trim().toLowerCase();
      sug.innerHTML = "";
      if (!q) return;
      S.nodes.filter((p) => p.cluster !== c.id
          && (p.name || "").toLowerCase().includes(q))
        .slice(0, 6).forEach((p) => {
          const b = el("button", "atlas-deg atlas-selchip", p.name);
          b.addEventListener("click", () => {
            inp.value = "";
            sug.innerHTML = "";
            assignGroup(p.id, editorId);
          });
          sug.appendChild(b);
        });
    });
    addWrap.appendChild(inp);
    addWrap.appendChild(sug);
    card.appendChild(addWrap);

    const del = el("button", "fchip sm atlas-group-del",
      "Remove this group");
    del.addEventListener("click", () => removeGroup(c));
    card.appendChild(del);
  }

  // ---------- selection (multi) ----------

  function recomputeSel() {
    S.neighbors.clear(); S.shared.clear();
    S.selEdges.clear(); S.selPathEdges.clear(); S.selPathNodes.clear();
    S.chains = [];
    const n = S.sel.size;
    if (!n) return;
    const counts = new Map();
    for (const e of S.edges) {
      if (!isEdgeShown(e)) continue;
      const a = S.sel.has(e.an), b = S.sel.has(e.bn);
      if (a && b) {
        S.selEdges.add(e);
      } else if (a) {
        S.neighbors.add(e.b);
        counts.set(e.b, (counts.get(e.b) || 0) + 1);
      } else if (b) {
        S.neighbors.add(e.a);
        counts.set(e.a, (counts.get(e.a) || 0) + 1);
      }
    }
    if (n >= 2) {
      for (const [id, c] of counts) if (c >= 2) S.shared.add(id);
    }
    // bridge chains between selections with no direct tie (small
    // selections only — a group selection tells its story in direct ties)
    if (n >= 2 && n <= 6) {
      const tied = new Set();
      for (const e of S.selEdges) {
        tied.add(e.an.id + "|" + e.bn.id);
        tied.add(e.bn.id + "|" + e.an.id);
      }
      const sel = [...S.sel];
      for (let i = 0; i < sel.length; i++) {
        for (let j = i + 1; j < sel.length; j++) {
          if (tied.has(sel[i].id + "|" + sel[j].id)) continue;
          const chain = bfsChain(sel[i], sel[j]);
          if (!chain) continue;
          chain.edges.forEach((e) => S.selPathEdges.add(e));
          chain.nodes.forEach((p) => {
            if (!S.sel.has(p)) S.selPathNodes.add(p.id);
          });
          S.chains.push({ a: sel[i], b: sel[j], nodes: chain.nodes });
        }
      }
    }
  }

  function bfsChain(a, b) {
    // shortest contact-to-contact chain, capped at 4 hops
    const prev = new Map([[a.id, null]]);
    let frontier = [a];
    for (let depth = 0; depth < 4 && frontier.length; depth++) {
      const next = [];
      for (const node of frontier) {
        for (const { n, e } of S.adj.get(node.id) || []) {
          if (!isShown(n) || !isEdgeShown(e)) continue;
          if (prev.has(n.id)) continue;
          prev.set(n.id, { node, via: e });
          if (n === b) {
            const nodes = [], edges = [];
            let cur = n;
            while (cur !== a) {
              const st = prev.get(cur.id);
              edges.push(st.via);
              if (cur !== b) nodes.push(cur);
              cur = st.node;
            }
            nodes.reverse(); edges.reverse();
            return { nodes, edges };
          }
          next.push(n);
        }
      }
      frontier = next;
    }
    return null;
  }

  function toggleSelect(p) {
    if (!p || p.ego) return;
    if (S.sel.has(p)) S.sel.delete(p); else S.sel.add(p);
    if (S.sel.size === 1 && S.sel.has(p)) rememberArticle(p);
    selectionChanged();
  }

  function rememberArticle(p) {
    if (!p || S.articleTrail[S.articleIndex] === p.id) return;
    S.articleTrail = S.articleTrail.slice(0, S.articleIndex + 1);
    S.articleTrail.push(p.id);
    if (S.articleTrail.length > 80) S.articleTrail.shift();
    S.articleIndex = S.articleTrail.length - 1;
  }

  function navigateArticle(p, remember = true) {
    if (!p || p.ego) return;
    if (remember) rememberArticle(p);
    centerOn(p);
    S.sel = new Set([p]);
    selectionChanged();
  }

  function moveArticleTrail(delta) {
    const next = S.articleIndex + delta;
    if (next < 0 || next >= S.articleTrail.length) return;
    const p = S.byId.get(S.articleTrail[next]);
    if (!p) return;
    S.articleIndex = next;
    navigateArticle(p, false);
  }

  function setSelection(list) {
    S.sel = new Set((list || []).filter((p) => p && !p.ego));
    if (S.sel.size === 1) rememberArticle([...S.sel][0]);
    selectionChanged();
  }

  function clearSel() {
    if (!S.sel.size) return;
    S.sel.clear();
    selectionChanged();
  }

  function selectionChanged() {
    recomputeSel();
    syncLegend();
    if (R3) R3.refreshPhysics(null, true);
    draw();
    renderSelCard();
  }

  // ---------- the detail / connection card ----------

  async function renderSelCard() {
    if (editorId) { renderGroupEditor(); return; }
    if (!S.sel.size) {
      // one circle isolated and nothing selected: the card is the
      // circle's own story — click a chip, read what it is
      const iso = [...S.iso.ids];
      const band = iso.length === 1
        && S.bands.find((b) => b.id === iso[0] && b.circle);
      if (band) { renderCircleCard(band); return; }
      circleShown = null;
      card.style.display = "none";
      return;
    }
    circleShown = null;
    if (S.sel.size >= 2) { renderMultiCard(); return; }
    const p = [...S.sel][0];
    card.style.display = "";
    card.innerHTML = "";
    card.appendChild(el("div", "hint", "loading…"));
    try {
      const d = await api("/api/world/node/" + encodeURIComponent(p.id));
      if (editorId || !(S.sel.size === 1 && S.sel.has(p))) return;
      await renderCard(d, ++S.detailToken);
    } catch {
      card.innerHTML = "";
      card.appendChild(el("div", "hint", "detail unavailable"));
    }
  }

  function renderMultiCard() {
    card.style.display = "";
    card.innerHTML = "";
    const head = el("div", "atlas-card-head");
    const mid = el("div", "atlas-card-name");
    mid.appendChild(el("div", null, `${S.sel.size} selected`));
    mid.appendChild(el("div", "hint", "how they connect"));
    head.appendChild(mid);
    const x = el("button", "idea-del", "×");
    x.addEventListener("click", clearSel);
    head.appendChild(x);
    card.appendChild(head);

    const chips = el("div", "atlas-card-chips");
    for (const p of S.sel) {
      const c = el("button", "atlas-deg atlas-selchip",
        firstLast(p.name) + " ×");
      c.title = "Remove from selection";
      c.addEventListener("click", () => toggleSelect(p));
      chips.appendChild(c);
    }
    card.appendChild(chips);

    const list = el("div", "atlas-card-edges");

    if (S.selEdges.size) {
      list.appendChild(el("div", "atlas-card-sub",
        `Direct ties (${S.selEdges.size})`));
      const edges = [...S.selEdges].sort((a, b) => b.weight - a.weight);
      edges.slice(0, 30).forEach((e) => {
        const row = el("div", "atlas-edge");
        const nameRow = el("div", "atlas-edge-name",
          firstLast(e.an.name) + " ↔ " + firstLast(e.bn.name));
        const barWrap = el("span", "atlas-wwrap");
        const bar = el("span", "atlas-w");
        bar.style.width = Math.min(100, e.weight * 55) + "%";
        barWrap.appendChild(bar);
        nameRow.appendChild(barWrap);
        row.appendChild(nameRow);
        const why = e.narrative || (e.signals || [])
          .map((s) => s.detail).filter(Boolean).join(" · ");
        if (why) row.appendChild(el("div", "atlas-edge-why", why));
        list.appendChild(row);
      });
      if (edges.length > 30)
        list.appendChild(el("div", "hint",
          `+ ${edges.length - 30} more ties`));
    }

    if (S.chains.length) {
      list.appendChild(el("div", "atlas-card-sub", "Bridges"));
      S.chains.forEach((ch) => {
        const row = el("div", "atlas-edge");
        row.appendChild(el("div", "atlas-edge-name",
          [ch.a, ...ch.nodes, ch.b].map((p) => firstLast(p.name))
            .join(" → ")));
        row.appendChild(el("div", "atlas-edge-why",
          ch.nodes.length === 1
            ? "no direct tie — connected through "
              + firstLast(ch.nodes[0].name)
            : "no direct tie — the shortest chain between them"));
        list.appendChild(row);
      });
    }

    if (S.shared.size) {
      list.appendChild(el("div", "atlas-card-sub",
        `Shared connections (${S.shared.size})`));
      const row = el("div", "atlas-edge atlas-shared");
      const names = [...S.shared].map((id) => S.byId.get(id))
        .filter(Boolean);
      names.slice(0, 20).forEach((p) => {
        const b = el("button", "atlas-deg atlas-selchip",
          firstLast(p.name));
        b.title = "Add to selection";
        b.addEventListener("click", () => { centerOn(p); toggleSelect(p); });
        row.appendChild(b);
      });
      if (names.length > 20)
        row.appendChild(el("span", "hint", `+${names.length - 20} more`));
      list.appendChild(row);
    }

    if (!S.selEdges.size && !S.chains.length && !S.shared.size)
      list.appendChild(el("div", "hint",
        "No ties, bridges, or shared connections among the selected — "
        + "they only connect through you."));
    card.appendChild(list);
  }

  // ---------- the circle card (server/circles.py) ----------

  let circleShown = null;          // stable circle id the card is showing

  function relDays(iso) {
    if (!iso) return "";
    const d = (Date.now() - Date.parse(iso)) / 864e5;
    if (d < 1) return "today";
    if (d < 2) return "yesterday";
    if (d < 30) return `${Math.floor(d)}d ago`;
    return iso.slice(0, 10);
  }

  async function renderCircleCard(band, quiet = false) {
    if (editorId) return;
    circleShown = band.circle;
    card.style.display = "";
    if (!quiet) {
      card.innerHTML = "";
      card.appendChild(el("div", "hint", "reading the circle…"));
    }
    let d;
    try { d = await api("/api/atlas/circles/" + band.circle); }
    catch {
      card.innerHTML = "";
      card.appendChild(el("div", "hint", "circle unavailable"));
      return;
    }
    if (circleShown !== band.circle || S.sel.size || editorId) return;
    paintCircle(band, d);
  }

  function paintCircle(band, d) {
    card.innerHTML = "";
    const head = el("div", "atlas-card-head");
    const dot = el("span", "atlas-dot atlas-dot-big");
    dot.style.background = S.colors.get(band.id) || "#888";
    head.appendChild(dot);
    const mid = el("div", "atlas-card-name");
    mid.appendChild(el("div", null, d.display_label));
    const n = d.members.length;
    mid.appendChild(el("div", "hint",
      `circle · ${n} people`
      + (d.story?.since ? ` · since ${d.story.since}` : "")));
    head.appendChild(mid);
    const x = el("button", "idea-del", "×");
    x.addEventListener("click", () => {
      circleShown = null;
      S.iso = { ids: new Set(), ring: 0 };
      isoChanged(false);
    });
    head.appendChild(x);
    card.appendChild(head);

    const story = el("div", "atlas-story");
    if (d.read_at) {
      if (d.why) story.appendChild(el("p", "hint", d.why));
      story.appendChild(el("div", "atlas-card-sub", "How you're connected"));
      story.appendChild(el("p", null, d.story?.you || ""));
      story.appendChild(el("div", "atlas-card-sub", "How they connect"));
      story.appendChild(el("p", null, d.story?.them || ""));
      if (d.held?.label)
        story.appendChild(el("p", "hint",
          `Vira proposed "${d.held.label}" and held it — ${d.held.reason}.`));
      story.appendChild(el("div", "hint atlas-read-when",
        "read " + relDays(d.read_at)
        + (d.read_reason ? ` · ${d.read_reason}` : "")));
    } else {
      story.appendChild(el("p", "hint", d.read_error
        ? "Vira could not read this circle yet: " + d.read_error
        : "Vira has not read this circle yet. Until it has, the name is "
          + "its most-shared named group chat, or its hub's."));
    }
    card.appendChild(story);

    card.appendChild(el("div", "atlas-card-sub", `Members (${n})`));
    const chips = el("div", "atlas-card-chips");
    const hub = d.story?.hub || d.ev?.hub;
    for (const m of d.members) {
      const c = el("button", "atlas-deg atlas-selchip",
        firstLast(m.name) + (m.id === hub ? " · hub" : ""));
      c.title = m.id === hub ? "Holds the circle together — click to feature"
                             : "Click to feature this person";
      c.addEventListener("click", () => {
        const p = S.nodes.find((q) => q.id === m.id);
        if (p) toggleSelect(p);
        else openPerson(m.id);
      });
      chips.appendChild(c);
    }
    card.appendChild(chips);

    const chats = (d.ev?.chats || []).filter((g) => g.named).slice(0, 6);
    if (chats.length) {
      card.appendChild(el("div", "atlas-card-sub", "Group chats they share"));
      const list = el("div", "atlas-card-edges");
      for (const g of chats) {
        const row = el("div", "atlas-edge");
        row.appendChild(el("div", "atlas-edge-name", g.label));
        row.appendChild(el("div", "atlas-edge-why",
          `${g.covers} of ${n} here · ${g.messages} messages`
          + (g.last ? ` · last ${g.last}` : "")));
        list.appendChild(row);
      }
      card.appendChild(list);
    }

    const hist = (d.history || []).slice(-6).reverse();
    if (hist.length) {
      card.appendChild(el("div", "atlas-card-sub", "What changed"));
      const list = el("div", "atlas-hist");
      for (const h of hist)
        list.appendChild(el("div", null,
          `${(h.when || "").slice(0, 10)} — ${h.what}`));
      card.appendChild(list);
    }

    const foot = el("div", "atlas-card-chips");
    const rn = el("button", "fchip sm", "Rename");
    rn.addEventListener("click", () => renameCircle(band));
    foot.appendChild(rn);
    const rr = el("button", "fchip sm", "Read again");
    rr.addEventListener("click", () => rereadCircle(band));
    foot.appendChild(rr);
    card.appendChild(foot);
  }

  async function refreshCircleLens(band) {
    // names live on the served graph: re-read it and patch clusters +
    // lenses in place (no re-layout), then repaint the card if it is up
    try {
      const g = await api("/api/atlas" + (vaultOn() ? "?vault=1" : ""));
      if (g.status === "ok") applyGroups(g);
    } catch { /* the next load carries it */ }
    if (circleShown === band.circle && !S.sel.size)
      renderCircleCard(band, true);
  }

  async function renameCircle(band) {
    const name = prompt("Name this circle (empty clears your name)",
                        band.label);
    if (name === null) return;
    try {
      await post(`/api/atlas/circles/${band.circle}/rename`,
                 { label: name.trim() });
      toast(name.trim() ? "Circle renamed" : "Your name cleared");
      await refreshCircleLens(band);
    } catch (e) { toast("Rename failed: " + e.message); }
  }

  async function rereadCircle(band) {
    let before = null;
    try {
      const d = await api("/api/atlas/circles/" + band.circle);
      before = d.read_at || null;
      await post(`/api/atlas/circles/${band.circle}/reread`, {});
    } catch (e) { toast("Read failed: " + e.message); return; }
    toast("Vira is reading the circle…");
    // one model call, typically well under two minutes; poll until the
    // read lands rather than sleeping a fixed time
    const until = Date.now() + 180000;
    const tick = async () => {
      try {
        const d = await api("/api/atlas/circles/" + band.circle);
        if ((d.read_at || null) !== before || d.read_error) {
          toast(d.read_error ? "Vira could not read it: " + d.read_error
                             : `Read as "${d.display_label}"`);
          await refreshCircleLens(band);
          return;
        }
      } catch { /* keep polling */ }
      if (Date.now() < until) setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  }

  async function renderCard(d, token) {
    card.innerHTML = "";
    card.scrollTop = 0;
    const articleNav = el("div", "atlas-article-nav");
    const back = el("button", "atlas-nav-btn", "Back");
    back.disabled = S.articleIndex <= 0;
    back.addEventListener("click", () => moveArticleTrail(-1));
    const forward = el("button", "atlas-nav-btn", "Forward");
    forward.disabled = S.articleIndex >= S.articleTrail.length - 1;
    forward.addEventListener("click", () => moveArticleTrail(1));
    articleNav.append(back, forward);
    if (d.edges.length) {
      const nextEdge = d.edges.find((edge) => {
        const other = S.byId.get(edge.pid);
        return other && isShown(other) && timeActive(edge);
      });
      const nextNode = nextEdge && S.byId.get(nextEdge.pid);
      if (nextNode) {
        const next = el("button", "atlas-nav-btn next", "Next connection");
        next.title = "Follow the strongest visible connection";
        next.addEventListener("click", () => navigateArticle(nextNode));
        articleNav.appendChild(next);
      }
    }
    card.appendChild(articleNav);

    const head = el("div", "atlas-card-head");
    const isPerson = d.node.kind === "person";
    const av = avatarNode(d.node.id, d.node.name, isPerson,
                          isPerson && d.node.face != null);
    if (d.node.face) av.querySelector("img")
      ?.setAttribute("src", "/api/atlas/face/" + d.node.id);
    head.appendChild(av);
    const mid = el("div", "atlas-card-name");
    const nm = el("div", "click", d.node.name);
    nm.title = openLabel(d.node) + " in a window";
    nm.addEventListener("click", () => openWorldNode(d.node));
    mid.appendChild(nm);
    const sub = [kindLabel(d.node.kind), d.node.title, d.node.company,
                 d.node.qualifier, d.node.source_name]
      .filter(Boolean).join(" · ");
    if (sub) mid.appendChild(el("div", "hint", sub));
    head.appendChild(mid);
    const star = el("button", "atlas-star",
      S.starred.has(d.node.id) ? "Starred" : "Star");
    star.type = "button";
    star.title = S.starred.has(d.node.id) ? "Remove from starred" : "Star this node";
    star.setAttribute("aria-label", star.title);
    star.addEventListener("click", () => {
      if (S.starred.has(d.node.id)) S.starred.delete(d.node.id);
      else S.starred.add(d.node.id);
      saveControls();
      star.textContent = S.starred.has(d.node.id) ? "Starred" : "Star";
      star.title = S.starred.has(d.node.id)
        ? "Remove from starred" : "Star this node";
      star.setAttribute("aria-label", star.title);
      paintFilterCount();
      if (S.starredOnly) isoChanged(false); else draw();
    });
    head.appendChild(star);
    const x = el("button", "idea-del", "×");
    x.addEventListener("click", clearSel);
    head.appendChild(x);
    card.appendChild(head);

    const chips = el("div", "atlas-card-chips");
    // Opening the full document is a SIDEBAR act (owner's call): the
    // canvas only selects, so the card carries an explicit Open button
    // beside the clickable name rather than relying on either alone.
    const openBtn = el("button", "fchip sm atlas-open", openLabel(d.node));
    openBtn.type = "button";
    openBtn.title = "Open the full " + (d.node.kind === "person"
      && d.node.open_kind === "person" ? "profile" : "document")
      + " in a window";
    openBtn.addEventListener("click", () => openWorldNode(d.node));
    chips.appendChild(openBtn);
    const noteRef = d.node.ref || d.node.note_ref;
    if (noteRef && d.node.open_kind === "person") {
      const wk = el("span", "atlas-deg click", "open source note");
      wk.addEventListener("click", () => openNote(noteRef, d.node.name));
      chips.appendChild(wk);
    }
    chips.appendChild(el("span", "atlas-deg", kindLabel(d.node.kind)));
    if (d.node.degree)
      chips.appendChild(el("span", "atlas-deg",
        ["1st", "2nd", "3rd"][d.node.degree - 1] || d.node.degree + "th"));
    if (d.node.valid_from || d.node.valid_to) {
      const timeChip = el("span", "atlas-deg",
        "content " + timeRange(d.node.valid_from, d.node.valid_to,
          d.node.time_precision).replace(/^from /, ""));
      timeChip.title = "Date source: "
        + timeSourceLabel(d.node.time_source?.valid_from);
      chips.appendChild(timeChip);
    }
    if (d.node.recorded_at) {
      const learnedChip = el("span", "atlas-deg",
        "learned " + shortDate(d.node.recorded_at,
          d.node.time_precision?.recorded_at));
      learnedChip.title = "Date source: "
        + timeSourceLabel(d.node.time_source?.recorded_at);
      chips.appendChild(learnedChip);
    }
    const clab = S.bands.find((b) => b.id === S.byId.get(d.node.id)?.band);
    if (clab) {
      const cc = el("span", "atlas-deg");
      cc.textContent = clab.label;
      cc.style.borderColor = S.colors.get(clab.id);
      chips.appendChild(cc);
    }
    card.appendChild(chips);

    if (d.person) renderPersonArticle(d.person);

    let markdownHost = null;
    if (d.content != null) {
      card.appendChild(el("div", "atlas-card-sub", "Article"));
      markdownHost = el("article", "atlas-article note-body");
      markdownHost.innerHTML = mdToHtml(d.content, d.content_path);
      if (!markdownHost.innerHTML.trim())
        markdownHost.appendChild(el("div", "hint", "This source note is empty."));
      markdownHost.querySelectorAll(".note-link").forEach((link) => {
        link.addEventListener("click", () =>
          followArticleLink(link.dataset.ref, d.content_path));
      });
      const contentImages = [...markdownHost.querySelectorAll("img")].map(
        (img, index) => ({ src: img.getAttribute("src"),
                          href: img.getAttribute("src"),
                          label: img.alt || `Image ${index + 1}` }));
      if (contentImages.length)
        card.appendChild(renderImageRail(contentImages, "Attached images"));
      const seenLinks = new Set();
      const contentLinks = [...markdownHost.querySelectorAll("a[href]")]
        .map((link) => ({ url: link.href,
                         title: link.textContent.trim() || link.href }))
        .filter((link) => /^https?:/i.test(link.url)
          && !seenLinks.has(link.url) && seenLinks.add(link.url));
      if (contentLinks.length)
        card.appendChild(renderExternalLinks(contentLinks));
      card.appendChild(markdownHost);
      markDeadLinks(markdownHost);
    }

    if (d.ego) {
      const you = el("div", "atlas-edge you");
      you.appendChild(el("div", "atlas-edge-name",
        "you ↔ " + firstLast(d.node.name)));
      you.appendChild(el("div", "atlas-edge-why",
        d.ego.signals.map((s) => s.detail).join(" · ")));
      card.appendChild(you);
    }

    const list = el("div", "atlas-card-edges");
    const visibleEdges = d.edges.filter((e) => {
      const other = S.byId.get(e.pid);
      return other && isShown(other) && timeActive(e);
    });
    if (visibleEdges.length)
      list.appendChild(el("div", "atlas-card-sub",
        `Continue exploring · ${visibleEdges.length} connections`));
    let edgeCursor = 0;
    const more = el("button", "atlas-more", "");
    const appendEdges = () => {
      const end = Math.min(visibleEdges.length, edgeCursor + 30);
      visibleEdges.slice(edgeCursor, end).forEach((e) => {
      const row = el("div", "atlas-edge click");
      const nameRow = el("div", "atlas-edge-name",
        `${e.name} · ${e.label || e.relation || "connected"}`);
      const bar = el("span", "atlas-w");
      bar.style.width = Math.min(100, e.weight * 55) + "%";
      const barWrap = el("span", "atlas-wwrap");
      barWrap.appendChild(bar);
      nameRow.appendChild(barWrap);
      row.appendChild(nameRow);
      row.appendChild(el("div", "atlas-edge-why",
        e.narrative || e.signals.map((s) => s.detail)
          .filter(Boolean).join(" · ")
          || receiptLabel(e.receipts)));
      const receipt = (e.receipts || [])[0];
      if (receipt?.ref) {
        const source = el("button", "atlas-edge-receipt",
          `Receipt · ${receipt.ref}${receipt.line ? `:${receipt.line}` : ""}`);
        source.type = "button";
        source.addEventListener("click", (event) => {
          event.stopPropagation();
          openNote(receipt.ref, receipt.label || e.name);
        });
        row.appendChild(source);
      }
      row.title = "Open this connected node";
      row.addEventListener("click", () => {
        const other = S.byId.get(e.pid);
        if (other) navigateArticle(other);
      });
      list.appendChild(row);
      });
      edgeCursor = end;
      more.remove();
      if (edgeCursor < visibleEdges.length) {
        more.textContent = `Show 30 more · ${visibleEdges.length - edgeCursor} remaining`;
        list.appendChild(more);
      }
    };
    more.addEventListener("click", appendEdges);
    appendEdges();
    if (!visibleEdges.length)
      list.appendChild(el("div", "hint",
        "No visible relations at this point on the selected timeline."));
    card.appendChild(list);

    if (isPerson && S.sel.size === 1 && S.sel.has(S.byId.get(d.node.id))) {
      try {
        const media = await api("/api/person/" + encodeURIComponent(d.node.id)
          + "/media");
        if (token !== S.detailToken || !S.sel.has(S.byId.get(d.node.id))) return;
        const photos = (media.photos || []).map((item) => ({
          src: "/api/media/thumb/" + item.id,
          href: "/api/media/file/" + item.id,
          label: item.context?.text || item.name || "Shared image",
        }));
        if (photos.length)
          card.insertBefore(renderImageRail(photos,
            `Shared images · ${photos.length}`), list);
        if ((media.links || []).length)
          card.insertBefore(renderExternalLinks(media.links), list);
      } catch { /* media is enrichment; the article remains useful */ }
    }
  }

  function renderPersonArticle(person) {
    const profile = person.profile || {};
    const master = person.master || {};
    const section = el("section", "atlas-person-article");
    const summary = profile.relationship_summary || profile.summary
      || master.relationship;
    if (summary) section.appendChild(el("p", "atlas-lede", String(summary)));
    if (profile.how_we_met)
      section.appendChild(articleBlock("How we met", profile.how_we_met));
    const lists = [
      ["Personal context", profile.personal_facts],
      ["Open loops", profile.open_loops],
      ["Conversation hooks", profile.hooks],
    ];
    for (const [label, rows] of lists) {
      if (!Array.isArray(rows) || !rows.length) continue;
      const block = el("div", "atlas-profile-block");
      block.appendChild(el("div", "atlas-card-sub", label));
      const ul = document.createElement("ul");
      rows.forEach((row) => {
        const value = typeof row === "string" ? row
          : row.what || row.fact || row.text || row.hook || row.note;
        if (value) ul.appendChild(el("li", null, String(value)));
      });
      if (ul.children.length) block.appendChild(ul);
      section.appendChild(block);
    }
    if (!section.children.length)
      section.appendChild(el("div", "hint",
        "No written CRM narrative is available for this person yet."));
    card.appendChild(section);
  }

  function articleBlock(label, value) {
    const block = el("div", "atlas-profile-block");
    block.appendChild(el("div", "atlas-card-sub", label));
    block.appendChild(el("p", null, String(value)));
    return block;
  }

  function renderImageRail(images, title) {
    const section = el("section", "atlas-media-section");
    const head = el("div", "atlas-media-head");
    head.appendChild(el("div", "atlas-card-sub", title));
    const controls = el("div", "atlas-media-controls");
    const prev = el("button", "atlas-nav-btn", "Previous");
    const next = el("button", "atlas-nav-btn", "Next");
    controls.append(prev, next); head.appendChild(controls);
    section.appendChild(head);
    const rail = el("div", "atlas-image-rail");
    section.appendChild(rail);
    let cursor = 0;
    const batch = el("button", "atlas-more", "");
    const add = () => {
      const end = Math.min(images.length, cursor + 24);
      images.slice(cursor, end).forEach((item) => {
        const link = el("a", "atlas-image-card");
        link.href = item.href || item.src;
        link.target = "_blank";
        link.rel = "noopener";
        const img = document.createElement("img");
        img.loading = "lazy"; img.src = item.src; img.alt = item.label || "";
        link.appendChild(img);
        if (item.label) link.appendChild(el("span", null, item.label));
        rail.appendChild(link);
      });
      cursor = end;
      batch.remove();
      if (cursor < images.length) {
        batch.textContent = `Load 24 more · ${images.length - cursor} remaining`;
        section.appendChild(batch);
      }
    };
    batch.addEventListener("click", add);
    prev.addEventListener("click", () => rail.scrollBy(
      { left: -rail.clientWidth * .82, behavior: "smooth" }));
    next.addEventListener("click", () => rail.scrollBy(
      { left: rail.clientWidth * .82, behavior: "smooth" }));
    add();
    return section;
  }

  function renderExternalLinks(links) {
    const section = el("section", "atlas-related-links");
    section.appendChild(el("div", "atlas-card-sub",
      `Relevant links · ${links.length}`));
    let cursor = 0;
    const more = el("button", "atlas-more", "");
    const add = () => {
      const end = Math.min(links.length, cursor + 20);
      links.slice(cursor, end).forEach((item) => {
        const a = el("a", "atlas-related-link");
        a.href = item.url; a.target = "_blank"; a.rel = "noopener";
        a.appendChild(el("b", null, item.title || item.domain || item.url));
        if (item.context?.text)
          a.appendChild(el("span", null, item.context.text));
        section.appendChild(a);
      });
      cursor = end; more.remove();
      if (cursor < links.length) {
        more.textContent = `Show 20 more · ${links.length - cursor} remaining`;
        section.appendChild(more);
      }
    };
    more.addEventListener("click", add); add();
    return section;
  }

  async function followArticleLink(ref, fromPath) {
    const clean = String(ref || "").split("#")[0].split("^")[0].trim();
    const stem = clean.split("/").pop().replace(/\.md$/i, "").toLowerCase();
    let hit = S.nodes.find((node) => {
      const path = String(node.ref || node.note_ref || "");
      const nodeStem = path.split("/").pop().replace(/\.md$/i, "").toLowerCase();
      return nodeStem === stem || String(node.name || "").toLowerCase() === stem;
    });
    if (hit) { navigateArticle(hit); return; }
    try {
      const resolved = await api("/api/vault/resolve?ref="
        + encodeURIComponent(clean) + (fromPath ? "&from_path="
        + encodeURIComponent(fromPath) : ""));
      const path = resolved.path || resolved.ref;
      hit = S.nodes.find((node) =>
        (node.ref || node.note_ref) === path);
      if (hit) navigateArticle(hit);
      else if (path) openNote(path, resolved.title || clean);
    } catch { toast("No page is available for " + clean); }
  }

  function kindLabel(kind) {
    return String(kind || "item").replace(/_/g, " ")
      .replace(/\b\w/g, (m) => m.toUpperCase());
  }

  function shortDate(value, precision) {
    const time = Date.parse(value);
    if (!Number.isFinite(time)) return "undated";
    const date = new Date(time);
    if (precision === "year")
      return date.toLocaleDateString(undefined,
        { year: "numeric", timeZone: "UTC" });
    if (precision === "month")
      return date.toLocaleDateString(undefined,
        { year: "numeric", month: "short", timeZone: "UTC" });
    return date.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric",
        timeZone: "UTC" });
  }

  function timeSourceLabel(source) {
    const labels = {
      file_birthtime: "filesystem creation time (fallback)",
      file_ctime: "filesystem change time (fallback)",
      filename_date: "date in filename",
      tagged_notes: "supporting notes",
    };
    return labels[source] || String(source || "unknown");
  }

  function timeRange(from, to, precision = {}) {
    return from && to
      ? `${shortDate(from, precision.valid_from)} – ${shortDate(to,
          precision.valid_to)}`
      : from ? `from ${shortDate(from, precision.valid_from)}`
        : `until ${shortDate(to, precision.valid_to)}`;
  }

  function receiptLabel(receipts) {
    const row = (receipts || [])[0];
    return row ? `receipt: ${row.label || row.ref || row.kind}` : "";
  }

  function openLabel(node) {
    if (node.open_kind === "person" || (node.kind === "person" && !node.ref))
      return "Open profile";
    if (node.ref || node.note_ref) return "Open note";
    return "Open";
  }

  function openWorldNode(node) {
    if (node.open_kind === "person" || (node.kind === "person" && !node.ref))
      openPerson(node.id);
    else if (node.ref || node.note_ref)
      openNote(node.ref || node.note_ref, node.name);
    else
      toast("This derived item has no standalone source page yet");
  }

  function centerOn(p) {
    if (R3) { R3.focusOn(p); return; }
    S.cam.x = p.x; S.cam.y = p.y;
    S.cam.k = Math.max(S.cam.k, 1.1);
    draw();
  }

  // ---------- what a hit means ----------

  // Both renderers find the node under the cursor their own way - the flat
  // one in two dimensions, the 3D one by projection - but what a hover, a
  // click, a double-click, a right-click and a click on empty space MEAN is
  // one implementation, so the two surfaces cannot drift on behaviour.

  function hitHover(p, sx, sy, moveOnly) {
    if (moveOnly) {
      if (!p || p.ego) return;
      tip.style.left = Math.min(sx + 14, stage.clientWidth - 180) + "px";
      tip.style.top = (sy + 14) + "px";
      return;
    }
    if (p && !p.ego) {
      tip.style.display = "";
      tip.style.left = Math.min(sx + 14, stage.clientWidth - 180) + "px";
      tip.style.top = (sy + 14) + "px";
      tip.innerHTML = "";
      tip.appendChild(el("div", "atlas-tip-name", p.name));
      const clab = S.bands.find((b) => b.id === p.band);
      const bits = [kindLabel(p.kind), clab?.label, p.company,
                    p.source_name].filter(Boolean);
      if (bits.length)
        tip.appendChild(el("div", "atlas-tip-sub", bits.join(" \u00b7 ")));
      if (p.qualifier)
        tip.appendChild(el("div", "atlas-tip-sub", p.qualifier));
      if (p.valid_from || p.valid_to)
        tip.appendChild(el("div", "atlas-tip-sub",
          "Content · " + timeRange(
            p.valid_from, p.valid_to, p.time_precision).replace(/^from /, "")));
      if (p.recorded_at)
        tip.appendChild(el("div", "atlas-tip-sub",
          "Learned · " + shortDate(
            p.recorded_at, p.time_precision?.recorded_at)));
    } else {
      tip.style.display = "none";
    }
  }

  function hitSelect(p) {
    if (editorId) editorToggle(p);
    else {
      centerOn(p);
      toggleSelect(p);
    }
  }

  function hitOpen(p) {
    openWorldNode(p);
  }

  function hitEmpty() {
    if (editorId) closeGroupEditor();
    else clearSel();
  }

  function hitContext(p, e) {
    const isPerson = p.kind === "person" && p.open_kind === "person";
    const ctxObj = { component: "World",
                     person: isPerson ? { pid: p.id, name: p.name } : null,
                     snippet: `${p.name} — ${kindLabel(p.kind)}`
                       + (p.ref ? ` · ${p.ref}` : "") };
    showContextMenu(e.clientX, e.clientY, [
      { head: "World \u00b7 " + p.name },
      { label: isPerson ? "Open profile" : "Open source",
        run: () => openWorldNode(p) },
      { label: "Feature connections", run: () => setSelection([p]) },
      { label: S.sel.has(p) ? "Remove from selection"
                            : "Add to selection",
        run: () => toggleSelect(p) },
      { label: S.starred.has(p.id) ? "Remove from starred" : "Star node",
        run: () => {
          if (S.starred.has(p.id)) S.starred.delete(p.id);
          else S.starred.add(p.id);
          saveControls();
          paintFilterCount();
          if (S.starredOnly) isoChanged(false); else draw();
        } },
      // group assignments key on CRM pids; a vault id has no row to hold
      // one, so the chooser is CRM-only
      isPerson && S.graph.clusters?.length && { label: "Set group\u2026",
        run: () => groupChooser(e.clientX, e.clientY, p) },
      { sep: true },
      { label: "New idea about this\u2026",
        run: () => ctxIdeaComposer(e.clientX, e.clientY, ctxObj) },
      { label: "Ask Vira about " + (p.name || "").split(" ")[0] + "\u2026",
        run: () => ctxAskVira(e.clientX, e.clientY, ctxObj) },
    ]);
  }

  // ---------- input (the flat fallback's own gestures) ----------

  // A PRESS IS ALWAYS THE CAMERA'S here too (owner's call, 2026-09-02):
  // no node drag, no single-click select. Double-click selects.
  let panning = null;
  S.dragNode = null;

  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;   // right-click belongs to the context menu
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    const p = nodeAt(sx, sy);
    canvas.setPointerCapture(e.pointerId);
    panning = { sx, sy, cx: S.cam.x, cy: S.cam.y, hitNode: !!p };
  });

  canvas.addEventListener("pointermove", (e) => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    if (panning) {
      S.cam.x = panning.cx - (sx - panning.sx) / S.cam.k;
      S.cam.y = panning.cy - (sy - panning.sy) / S.cam.k;
      draw();
      return;
    }
    const p = nodeAt(sx, sy);
    if (p !== S.hover) {
      S.hover = p;
      canvas.style.cursor = p ? "pointer" : "";
      hitHover(p, sx, sy);
      draw();
    } else if (p) {
      hitHover(p, sx, sy, true);
    }
  });

  canvas.addEventListener("pointerup", (e) => {
    if (e.button !== 0) return;
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    if (panning) {
      const moved = Math.hypot(sx - panning.sx, sy - panning.sy) > 4;
      const hitNode = panning.hitNode;
      panning = null;
      if (!moved && !hitNode) hitEmpty();
    }
  });
  canvas.addEventListener("pointercancel", () => { panning = null; });

  canvas.addEventListener("dblclick", (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const p = nodeAt(e.clientX - rect.left, e.clientY - rect.top);
    if (!p || p.ego) return;
    hitSelect(p);
  });

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    const wx = s2wX(sx), wy = s2wY(sy);
    const k = Math.max(0.25, Math.min(3.2,
      S.cam.k * Math.exp(-e.deltaY * 0.0016)));
    // keep the point under the cursor fixed
    S.cam.k = k;
    S.cam.x = wx - (sx - canvas.clientWidth / 2) / k;
    S.cam.y = wy - (sy - canvas.clientHeight / 2) / k;
    draw();
  }, { passive: false });

  canvas.addEventListener("contextmenu", (e) => {
    const rect = canvas.getBoundingClientRect();
    const p = nodeAt(e.clientX - rect.left, e.clientY - rect.top);
    if (!p || p.ego) return;             // fall through to the Vira menu
    e.preventDefault();
    hitContext(p, e);
  });

  // ---------- toolbar ----------

  function setOutput(id, value) {
    const output = $(id);
    if (output) output.textContent = Math.round(value * 100) + "%";
  }

  function syncControlInputs() {
    const values = {
      "#atlas-filter-mode": S.filterSearch,
      "#atlas-hide-orphans": S.hideOrphans,
      "#atlas-starred-only": S.starredOnly,
      "#atlas-physics": S.physics.enabled,
      "#atlas-auto-rotate": S.display.autoRotate,
      "#atlas-curved-links": S.display.curvedLinks,
      "#atlas-spherical-nodes": S.display.sphericalNodes,
      "#atlas-white-links": S.display.whiteLinks,
    };
    for (const [id, value] of Object.entries(values))
      if ($(id)) $(id).checked = value;
    const ranges = {
      "#atlas-geometry": S.display.scale * 100,
      "#atlas-node-size": S.display.nodeSize * 100,
      "#atlas-node-opacity": S.display.nodeOpacity * 100,
      "#atlas-link-thickness": S.display.linkThickness * 100,
      "#atlas-link-opacity": S.display.linkOpacity * 100,
      "#atlas-link-curve": S.display.linkCurve * 100,
      "#atlas-center": S.physics.center * 100,
      "#atlas-repel": S.physics.repel * 100,
      "#atlas-link-force": S.physics.link * 100,
      "#atlas-link-distance": S.physics.distance * 100,
      "#atlas-semantic": S.physics.semantic * 100,
    };
    for (const [id, value] of Object.entries(ranges))
      if ($(id)) $(id).value = String(Math.round(value));
    setOutput("#atlas-geometry-out", S.display.scale);
    setOutput("#atlas-node-size-out", S.display.nodeSize);
    setOutput("#atlas-node-opacity-out", S.display.nodeOpacity);
    setOutput("#atlas-link-thickness-out", S.display.linkThickness);
    setOutput("#atlas-link-opacity-out", S.display.linkOpacity);
    setOutput("#atlas-link-curve-out", S.display.linkCurve);
    setOutput("#atlas-center-out", S.physics.center);
    setOutput("#atlas-repel-out", S.physics.repel);
    setOutput("#atlas-link-force-out", S.physics.link);
    setOutput("#atlas-link-distance-out", S.physics.distance);
    setOutput("#atlas-semantic-out", S.physics.semantic);
    $("#atlas-link-curve-row")?.classList.toggle(
      "disabled", !S.display.curvedLinks);
    if ($("#atlas-link-curve"))
      $("#atlas-link-curve").disabled = !S.display.curvedLinks;
    if ($("#world-speed")) $("#world-speed").value = String(S.time.speed);
    paintFilterCount();
  }

  function applyGeometry(resetPositions = false) {
    for (const p of S.nodes) {
      p.homeX = p.baseX * S.display.scale;
      p.homeY = p.baseY * S.display.scale;
      p.homeZ = p.baseZ * S.display.scale;
      p.r = p.baseR * S.display.nodeSize;
      if (resetPositions) {
        p.x = p.homeX; p.y = p.homeY; p.z = p.homeZ;
        p.vx = p.vy = p.vz = 0;
      }
    }
    saveControls();
    if (R3) R3.geometryChanged(resetPositions);
    else draw();
  }

  function paintPhysicsStatus(info) {
    const target = $("#atlas-physics-status");
    if (!target) return;
    if (!S.physics.enabled) {
      target.textContent = "Physics off. Dragging still moves individual nodes.";
      return;
    }
    if (!info || !info.count) {
      target.textContent = S.nodes.length > 4000
        ? "Full-vault mode: select a node to engage local physics."
        : "Global physics ready.";
      return;
    }
    target.textContent = (info.mode === "global" ? "Global" : "Local")
      + " physics · " + info.count.toLocaleString() + " nodes"
      + (info.mode === "local" ? " · two connection rings" : "")
      + (info.limited ? " · 1,400-node performance ceiling" : "");
  }

  function physicsChanged() {
    saveControls();
    syncControlInputs();
    // The full graph deliberately does not simulate every node at once.
    // A force slider still needs visible feedback before the owner has made
    // a selection, so heat the neighborhood of the most connected visible
    // node instead of handing the renderer an empty seed.
    const seed = [...S.sel][0] || S.hover || S.nodes.reduce((best, node) =>
      isShown(node) && (!best || (node.graph_degree || node.degree || 0)
        > (best.graph_degree || best.degree || 0)) ? node : best, null);
    if (R3) R3.refreshPhysics(seed, true);
    else draw();
  }

  function bindPercentRange(id, target, key, low, high, geometry) {
    $(id)?.addEventListener("input", (e) => {
      target[key] = clamp(Number(e.target.value) / 100,
                          low, high, target[key]);
      setOutput(id + "-out", target[key]);
      if (geometry) applyGeometry(geometry === "positions");
      else physicsChanged();
    });
  }

  $("#atlas-search")?.addEventListener("input", (e) => {
    S.match = e.target.value.trim().toLowerCase();
    isoChanged(false);
  });
  $("#atlas-search-clear")?.addEventListener("click", () => {
    S.match = "";
    $("#atlas-search").value = "";
    isoChanged(false);
  });
  $("#atlas-filter-mode")?.addEventListener("change", (e) => {
    S.filterSearch = e.target.checked;
    saveControls();
    isoChanged(false);
  });
  $("#atlas-hide-orphans")?.addEventListener("change", (e) => {
    S.hideOrphans = e.target.checked;
    saveControls();
    isoChanged(false);
  });
  $("#atlas-starred-only")?.addEventListener("change", (e) => {
    S.starredOnly = e.target.checked;
    saveControls();
    isoChanged(false);
  });
  $("#atlas-search")?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !S.match) return;
    const hit = filteredNodes().find((p) => isShown(p));
    // Enter ADDS to the selection — search out far-apart items one by
    // one and watch how they connect
    if (hit) {
      centerOn(hit);
      if (!S.sel.has(hit)) toggleSelect(hit);
    }
  });

  bindPercentRange("#atlas-geometry", S.display, "scale", .35, 2.5,
                   "positions");
  bindPercentRange("#atlas-node-size", S.display, "nodeSize", .6, 1.8,
                   true);
  bindPercentRange("#atlas-node-opacity", S.display, "nodeOpacity", .1, 1,
                   true);
  bindPercentRange("#atlas-link-thickness", S.display, "linkThickness",
                   .25, 2.5, true);
  // Link opacity is a plain repaint - no geometry moves, so it must not
  // route through applyGeometry the way the thickness slider does.
  $("#atlas-link-opacity")?.addEventListener("input", (e) => {
    S.display.linkOpacity = clamp(Number(e.target.value) / 100, 0, 3, 1);
    setOutput("#atlas-link-opacity-out", S.display.linkOpacity);
    saveControls();
    draw();
  });
  $("#atlas-white-links")?.addEventListener("change", (e) => {
    S.display.whiteLinks = e.target.checked;
    saveControls();
    draw();
  });
  $("#atlas-link-curve")?.addEventListener("input", (e) => {
    S.display.linkCurve = clamp(Number(e.target.value) / 100, 0, .3, .10);
    setOutput("#atlas-link-curve-out", S.display.linkCurve);
    saveControls();
    draw();
  });
  bindPercentRange("#atlas-center", S.physics, "center", 0, 1, false);
  bindPercentRange("#atlas-repel", S.physics, "repel", 0, 1, false);
  bindPercentRange("#atlas-link-force", S.physics, "link", 0, 1, false);
  bindPercentRange("#atlas-link-distance", S.physics, "distance",
                   .4, 2.2, false);
  bindPercentRange("#atlas-semantic", S.physics, "semantic", 0, 1, false);
  $("#atlas-physics")?.addEventListener("change", (e) => {
    S.physics.enabled = e.target.checked;
    physicsChanged();
  });
  $("#atlas-auto-rotate")?.addEventListener("change", (e) => {
    S.display.autoRotate = e.target.checked;
    saveControls();
  });
  $("#atlas-curved-links")?.addEventListener("change", (e) => {
    S.display.curvedLinks = e.target.checked;
    saveControls();
    syncControlInputs();
    if (R3) R3.linkGeometryChanged();
    else draw();
  });
  $("#atlas-spherical-nodes")?.addEventListener("change", (e) => {
    S.display.sphericalNodes = e.target.checked;
    saveControls();
    draw();
  });
  $("#atlas-reset-colors")?.addEventListener("click", () => {
    const prefix = `${S.lens}|`;
    for (const key of Object.keys(S.colorOverrides))
      if (key.startsWith(prefix)) delete S.colorOverrides[key];
    assignColors();
    saveControls();
    renderLegend();
    draw();
  });
  $("#atlas-reset-geometry")?.addEventListener("click", () =>
    applyGeometry(true));
  $("#atlas-reset-controls")?.addEventListener("click", () => {
    S.filterSearch = CONTROL_DEFAULTS.filterSearch;
    S.hideOrphans = CONTROL_DEFAULTS.hideOrphans;
    S.starredOnly = CONTROL_DEFAULTS.starredOnly;
    S.display = { ...CONTROL_DEFAULTS.display };
    S.physics = { ...CONTROL_DEFAULTS.physics };
    S.colorOverrides = {};
    S.enabledKinds = new Set((S.graph?.kinds || []).map((row) => row.id));
    S.match = "";
    if ($("#atlas-search")) $("#atlas-search").value = "";
    syncControlInputs();
    renderKindFilters();
    assignColors();
    renderLegend();
    applyGeometry(true);
    physicsChanged();
    isoChanged(false);
  });

  // The gear is the module's whole control surface on both widths.
  // Deliberately NOT focus-on-open: the panel leads with the lens row and
  // the bands, and on a phone a focused input throws the keyboard over
  // the chips the gear was opened to reach.
  function setChrome(open) {
    $("#atlas-chrome")?.classList.toggle("open", open);
    $("#atlas-gear")?.setAttribute("aria-expanded", open ? "true" : "false");
  }
  const chromeOpen = () => !!$("#atlas-chrome")?.classList.contains("open");
  $("#atlas-gear")?.addEventListener("click",
    () => setChrome(!chromeOpen()));

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !S.visible) return;
    if (editorId) closeGroupEditor();
    else if (S.sel.size) clearSel();
    else if (S.iso.ids.size) {
      S.iso = { ids: new Set(), ring: 0 };
      isoChanged(false);
    } else if (chromeOpen()) setChrome(false);
  });

  function setTimeAxis(axis) {
    if (axis === S.time.axis) return;
    stopTimelinePlayback(false);
    S.time.axis = axis;
    setTimelineBounds();
    syncTimelineRange();
    timelineChanged();
  }
  $("#world-axis-valid")?.addEventListener("click", () => setTimeAxis("valid"));
  $("#world-axis-recorded")?.addEventListener(
    "click", () => setTimeAxis("recorded"));
  $("#world-time")?.addEventListener("input", (e) => {
    stopTimelinePlayback(false);
    const value = Number(e.target.value);
    const span = Math.max(1, S.time.max - S.time.min);
    S.time.at = value >= 999 ? null : S.time.min + span * (value / 1000);
    timelineChanged();
  });
  $("#world-now")?.addEventListener("click", () => {
    stopTimelinePlayback(false);
    S.time.at = null;
    const range = $("#world-time");
    if (range) range.value = "1000";
    timelineChanged();
  });
  $("#world-play")?.addEventListener("click", toggleTimelinePlayback);
  $("#world-speed")?.addEventListener("change", (e) => {
    S.time.speed = clamp(e.target.value, .5, 4, 1);
    saveControls();
  });

  $("#atlas-ego")?.addEventListener("click", (e) => {
    S.hideEgo = !S.hideEgo;
    e.target.classList.toggle("on", S.hideEgo);
    wake(0.3);
    draw();
  });

  $("#atlas-rescan")?.addEventListener("click", async () => {
    const btn = $("#atlas-rescan");
    btn.disabled = true;
    btn.textContent = "scanning…";
    try { await post("/api/world/refresh", {}); } catch { /* best effort */ }
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Rescan";
      S.loadedGen = null;
      atlasLoad(true);
    }, 1800);
  });

  // ---------- lifecycle: pause when hidden ----------

  const io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      S.visible = en.isIntersecting;
      if (S.visible) {
        // covers entry paths that ran before this script loaded (the
        // #atlas deep link / restored window state at boot)
        if (!S.graph) atlasLoad();
        resize();
        wake(0.2);
        if (R3) R3.setRunning(true);
      } else {
        stopTimelinePlayback(false);
        if (R3) R3.setRunning(false);
        S.running = false;
        cancelAnimationFrame(S.raf);
      }
    }
  });
  io.observe(stage);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopTimelinePlayback(false);
      if (R3) R3.setRunning(false);
      S.running = false;
      cancelAnimationFrame(S.raf);
    } else if (S.visible) {
      if (R3) R3.setRunning(true);
      wake(0.15);
    }
  });
  new ResizeObserver(() => resize()).observe(stage);
})();

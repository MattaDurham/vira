/* Relief - the network as terrain.

   A fresh take on the Visual Network (2026-09-02). The people you know are
   a LANDSCAPE: every contact is a hexagonal column standing on a hex-grid
   ground, its height the volume of history between you, its cap wearing
   their face. Columns pack into ISLANDS by group (the lens you pick -
   groups, circles, companies, locations), islands sit around your own
   plaza at the centre, and islands with many ties between them are placed
   as neighbours so the geography carries meaning: the family sits beside
   the group chats it lives in, the old employer beside the people it
   introduced you to. Ties are ROADS on the ground - faint, always there.
   Ask about one person and their ties rise off the ground as arcs.

   THE CAMERA IS A TURNTABLE, NOT A FREE ORBIT. A landscape is read from
   above and never from underneath, so left-drag spins the table (yaw) and
   tilts it within a bounded pitch, right/middle/shift-drag slides the
   ground under you, and the wheel dollies toward the point on the ground
   under the cursor. Touch: one finger spins, two fingers pinch and slide.
   Every camera value is EASED toward its target each frame, so a click
   that flies to a person glides there rather than cutting.

   WHAT A CLICK SHOWS: the column lifts and is framed; the people they are
   tied to light up (direct ties bright, two hops away faint); everyone
   else recedes into the ground colour; and a plaque beside the stage
   carries the dossier - who they are, the history between you, the
   relationship read, what is open between you, the hooks worth raising -
   plus their ties as a list that flies the camera when clicked. It is a
   window onto the CRM record, not a substitute for it: the name opens the
   profile.

   Data is the Visual Network's own payload (/api/atlas) plus the person
   record (/api/person/{pid}) on click. No new store, no new inference. */
"use strict";

import * as THREE from "./vendor/three.module.js";

// ---------- palette: earthbound, distinct, ten deep ----------
// Band colours are muted on purpose - the stage is stone and graphite, and
// a saturated rainbow would read as a different app. Faces carry the
// colour that matters.
const BAND_COLORS = [
  "#a39c8d", "#7a8f9c", "#a9651b", "#7d8a74", "#a0715f",
  "#5d6a80", "#8a9a4a", "#7a5d75", "#4f8a86", "#b9a06a",
  "#8c4a3c", "#5e7480", "#9a7f5a", "#6c8c9a", "#a58a3c",
];
const UNPLACED = "#4a4b48";
const EGO_COLOR = "#cfcbc2";

const HEX = 1.0;                 // hex circumradius, world units
const COL_R = 0.88 * HEX;        // column radius (the gap between columns)
const NEIGH = Math.sqrt(3) * HEX; // centre-to-centre of adjacent hexes
const MIN_H = 0.22, MAX_H = 2.6;
const LIFT = 0.55;               // how far a selected column rises
// Roads on the ground are the ties at or above this weight (the median tie
// on the live graph is 0.42, the 75th percentile 0.5). Every tie still
// exists - a weaker one rises as an arc the moment its person is selected -
// but drawn all at once the 1,630 of them washed the ground to white.
const ROAD_FLOOR = 0.5;
// The cylinder cap's UV plane maps u to world z and v to world x, so a face
// painted upright on the canvas lies on its side on the cap; this turns it
// to read upright from the default camera side (measured, not derived).
const CAP_ROT = Math.PI / 2;

const CAM = {
  fov: 42,
  pitchMin: 22, pitchMax: 78,    // degrees above the ground plane
  distMin: 6, distMax: 120,
  ease: 9,                       // per-second easing gain toward targets
  yawGain: 0.0065,               // radians per pixel of horizontal drag
  pitchGain: 0.25,               // degrees per pixel of vertical drag
};

const LENS_ORDER = ["groups", "circles", "companies", "locations"];

// ---------- tiny helpers ----------
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const easeInOut = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
const initialsOf = (name) => (name || "?").split(/\s+/).filter(Boolean)
  .slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
const firstName = (name) => (name || "").split(" ")[0];
const dom = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
function daysAgo(iso) {
  if (!iso) return null;
  const t = Date.parse(iso.length <= 10 ? iso + "T12:00:00" : iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 86400000));
}
function agoText(iso) {
  const d = daysAgo(iso);
  if (d == null) return "";
  if (d === 0) return "today";
  if (d === 1) return "yesterday";
  if (d < 30) return d + " days ago";
  if (d < 365) { const m = Math.max(1, Math.round(d / 30)); return m === 1 ? "1 month ago" : m + " months ago"; }
  const y = (d / 365).toFixed(1).replace(/\.0$/, "");
  return y === "1" ? "1 year ago" : y + " years ago";
}

// ---------- module state ----------
const S = {
  stage: null, canvas: null, cardEl: null, tipEl: null, labelsEl: null,
  chromeEl: null, metaEl: null, emptyEl: null,
  graph: null, lens: "groups", nodes: [], byId: new Map(), edges: [],
  adj: new Map(),                 // pid -> [{pid, weight, signals}]
  islands: [],                    // [{band, color, label, members, cx, cz, r}]
  sel: null, hover: null, nb1: new Set(), nb2: new Set(),
  query: "", matches: null,
  loading: false, loadedGen: null, lost: false,
  // three
  renderer: null, scene: null, camera: null, ray: null,
  columns: new Map(),             // pid -> mesh
  roads: null, arcs: [], egoMesh: null, grid: null, ground: null,
  light: null,
  // camera (target values and eased current values)
  cam: { x: 0, z: 0, yaw: 0.6, pitch: 52, dist: 36 },
  cur: { x: 0, z: 0, yaw: 0.6, pitch: 52, dist: 36 },
  tweens: [],
  running: false, dirty: true, lastT: 0, idleT: 0,
  drag: null, pointers: new Map(), pinch: null,
  reduced: false,
  faceCache: new Map(),
  detailSeq: 0,
};

// ---------- entry ----------
export async function load(force) {
  if (!S.stage) init();
  if (S.loading) return;
  if (S.graph && !force && S.graph.generated === S.loadedGen) {
    resize(); wake(); return;
  }
  S.loading = true;
  try {
    const g = await api("/api/atlas");
    if (g.status === "empty") {
      showEmpty(g.building);
      if (g.building) setTimeout(() => load(true), 4000);
      return;
    }
    S.emptyEl.style.display = "none";
    S.loadedGen = g.generated;
    setGraph(g);
  } catch (e) {
    showEmpty(false, "Network unavailable - " + (e && e.message));
  } finally {
    S.loading = false;
  }
}

function showEmpty(building, msg) {
  S.emptyEl.style.display = "";
  S.emptyEl.textContent = msg || (building
    ? "Building the graph - this takes a few seconds the first time."
    : "No graph yet. Rebuild from the Visual Network window, then reopen.");
}

// ---------- DOM + three setup ----------
function init() {
  S.reduced = typeof REDUCED_MOTION !== "undefined" && REDUCED_MOTION;
  S.stage = document.getElementById("relief-stage");
  S.canvas = document.getElementById("relief-canvas");
  S.cardEl = document.getElementById("relief-card");
  S.tipEl = document.getElementById("relief-tip");
  S.labelsEl = document.getElementById("relief-labels");
  S.chromeEl = document.getElementById("relief-chrome");
  S.metaEl = document.getElementById("relief-meta");
  S.emptyEl = document.getElementById("relief-empty");
  S.lens = lsGet("vira-relief-lens", "");
  if (!LENS_ORDER.includes(S.lens)) S.lens = "";   // decided per graph in setGraph

  let renderer = null;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas: S.canvas, antialias: true, alpha: true,
      powerPreference: "high-performance",
    });
  } catch (e) {
    showEmpty(false, "This browser has no WebGL, so the terrain cannot be drawn.");
    return;
  }
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  S.renderer = renderer;

  const scene = new THREE.Scene();
  const bg = cssColor("--bg-stage", "#060707");
  scene.background = new THREE.Color(bg);
  scene.fog = new THREE.Fog(new THREE.Color(bg), 60, 160);
  S.scene = scene;

  S.camera = new THREE.PerspectiveCamera(CAM.fov, 1, 0.1, 400);
  S.ray = new THREE.Raycaster();

  // light: a low warm sun from the upper left, a cool sky fill
  const hemi = new THREE.HemisphereLight(0xcfcbc2, 0x0d0d0d, 0.55);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xffe9c8, 1.35);
  sun.position.set(-30, 46, 22);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.camera.near = 1; sun.shadow.camera.far = 200;
  sun.shadow.bias = -0.0008;
  scene.add(sun);
  scene.add(sun.target);
  S.light = sun;

  // ground: a wide dark disc that receives shadows
  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(400, 64),
    new THREE.MeshStandardMaterial({ color: new THREE.Color(bg).offsetHSL(0, 0, 0.02),
      roughness: 1, metalness: 0 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);
  S.ground = ground;

  // the WebGL context can be taken away; the stage must say so and the
  // meshes must come back on their own (the 2026-08-31 lesson)
  S.canvas.addEventListener("webglcontextlost", (e) => {
    e.preventDefault();
    S.lost = true; S.running = false;
    S.stage.classList.add("relief-lost");
    S.labelsEl.innerHTML = "";
  });
  S.canvas.addEventListener("webglcontextrestored", () => {
    // rebuild while `lost` is still set, so clearScene does not ask the new
    // context to dispose objects that died with the old one
    if (S.graph) buildScene(false);
    S.lost = false;
    S.stage.classList.remove("relief-lost");
    wake();
  });

  bindPointer();
  bindChrome();
  new ResizeObserver(() => { resize(); }).observe(S.stage);
  const io = new IntersectionObserver((ents) => {
    ents.forEach((en) => en.isIntersecting ? wake() : sleep());
  });
  io.observe(S.stage);
  document.addEventListener("visibilitychange", () => {
    document.hidden ? sleep() : wake();
  });
  resize();
}

function cssColor(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function resize() {
  if (!S.renderer) return;
  const w = S.stage.clientWidth, h = S.stage.clientHeight;
  if (!w || !h) return;
  S.renderer.setSize(w, h, false);
  S.camera.aspect = w / h;
  S.camera.updateProjectionMatrix();
  S.dirty = true;
}

// ---------- graph -> islands -> positions ----------
function setGraph(g) {
  S.graph = g;
  S.byId = new Map();
  S.nodes = g.nodes.map((n) => {
    const node = { ...n, x: 0, z: 0, h: MIN_H, band: null, color: UNPLACED };
    S.byId.set(n.id, node);
    return node;
  });
  S.edges = g.edges.filter((e) => S.byId.has(e.a) && S.byId.has(e.b));
  S.adj = new Map();
  for (const e of S.edges) {
    if (!S.adj.has(e.a)) S.adj.set(e.a, []);
    if (!S.adj.has(e.b)) S.adj.set(e.b, []);
    S.adj.get(e.a).push({ pid: e.b, weight: e.weight, signals: e.signals, narrative: e.narrative });
    S.adj.get(e.b).push({ pid: e.a, weight: e.weight, signals: e.signals, narrative: e.narrative });
  }
  for (const list of S.adj.values()) list.sort((a, b) => b.weight - a.weight);
  S.egoW = new Map((g.ego_edges || []).map((e) => [e.b, e]));

  // With no saved choice the opening lens is whichever bands the MOST
  // people - on this graph every lens leaves most contacts unbanded, and an
  // opening view that files three quarters of them under "everyone else"
  // is a worse first sight than one that files two thirds.
  if (!S.lens) {
    const best = [...(g.lenses || [])].sort((a, b) => b.placed - a.placed)[0];
    S.lens = best ? best.id : "groups";
  }
  const maxAct = Math.max(1, ...S.nodes.map((n) => n.act || 0));
  for (const n of S.nodes) {
    n.h = MIN_H + (MAX_H - MIN_H) * Math.log1p(n.act || 0) / Math.log1p(maxAct);
  }
  layout(S.lens);
  buildScene(true);
  renderLenses();
  S.metaEl.textContent = `${S.nodes.length} people - ${S.edges.length} ties - `
    + `${S.islands.length} islands`;
  frameAll(true);
  wake();
}

function bandsFor(lensId) {
  const lens = (S.graph.lenses || []).find((l) => l.id === lensId)
    || (S.graph.lenses || [])[0];
  if (!lens) return { bands: [], nodeBand: {} };
  return { bands: lens.bands || [], nodeBand: lens.node_band || {} };
}

// Spiral hex placement: ring 0 is the centre, then rings outward, walking
// each ring's six sides. The most active member takes the centre, so an
// island reads as "who matters here" from its middle out.
function hexSpiral(n) {
  const out = [[0, 0]];
  const dirs = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]];
  for (let ring = 1; out.length < n; ring++) {
    let q = -ring, r = ring;             // start at the south-west corner
    for (let side = 0; side < 6 && out.length < n; side++) {
      for (let step = 0; step < ring && out.length < n; step++) {
        out.push([q, r]);
        q += dirs[side][0]; r += dirs[side][1];
      }
    }
  }
  return out;
}
const axialToWorld = (q, r) => [HEX * Math.sqrt(3) * (q + r / 2), HEX * 1.5 * r];

function layout(lensId) {
  const { bands, nodeBand } = bandsFor(lensId);
  const groups = new Map();
  bands.forEach((b, i) => groups.set(b.id, {
    band: b.id, label: b.label, color: BAND_COLORS[i % BAND_COLORS.length],
    members: [], kind: b.kind,
  }));
  const none = { band: "__none", label: "Everyone else", color: UNPLACED, members: [] };
  for (const n of S.nodes) {
    const bid = nodeBand[n.id];
    const g = bid != null && groups.has(bid) ? groups.get(bid) : none;
    g.members.push(n);
    n.band = g.band; n.color = g.color;
  }
  const islands = [...groups.values()].filter((g) => g.members.length);
  islands.sort((a, b) => b.members.length - a.members.length);
  if (none.members.length) islands.push(none);

  // members inside an island, spiralled from the most active outward
  for (const isl of islands) {
    isl.members.sort((a, b) => (b.act || 0) - (a.act || 0));
    const cells = hexSpiral(isl.members.length);
    isl.local = cells.map(([q, r]) => axialToWorld(q, r));
    const rings = Math.max(0, Math.ceil((Math.sqrt(12 * isl.members.length - 3) - 3) / 6));
    isl.r = NEIGH * (rings + 0.5) + HEX * 0.6;
  }

  // island-to-island tie weight, which decides who gets to be neighbours
  const idx = new Map(S.nodes.map((n) => [n.id, n.band]));
  const tie = new Map();
  for (const e of S.edges) {
    const a = idx.get(e.a), b = idx.get(e.b);
    if (a === b) continue;
    const k = a < b ? a + "|" + b : b + "|" + a;
    tie.set(k, (tie.get(k) || 0) + e.weight);
  }
  const tieBetween = (x, y) => tie.get(x < y ? x + "|" + y : y + "|" + x) || 0;

  // placement: the plaza at the origin, islands on a slow spiral outward,
  // each choosing among the nearest feasible spots the one closest to the
  // islands it is tied to
  const plazaR = HEX * 2.4;
  const gap = HEX * 1.6;
  const placed = [];
  for (const isl of islands) {
    const feasible = [];
    const step = 0.35;
    // golden-angle spiral: the candidates sweep every direction before the
    // radius grows, so the ring fills all round the plaza rather than
    // piling up on the side the first island happened to take
    for (let t = 0; feasible.length < 40 && t < 4000; t += step) {
      const rad = plazaR + isl.r + gap + t * 0.55;
      const ang = t * 2.399963 + islands.indexOf(isl) * 2.399963;
      const x = Math.cos(ang) * rad, z = Math.sin(ang) * rad;
      let ok = true;
      for (const p of placed) {
        const d = Math.hypot(p.cx - x, p.cz - z);
        if (d < p.r + isl.r + gap) { ok = false; break; }
      }
      if (ok) feasible.push({ x, z, rad });
    }
    let best = feasible[0] || { x: plazaR + isl.r + gap, z: 0, rad: 0 };
    if (placed.length && feasible.length) {
      let bestCost = Infinity;
      const totalTie = placed.reduce((s, p) => s + tieBetween(isl.band, p.band), 0);
      for (const f of feasible) {
        let cost = f.rad * 0.35;
        for (const p of placed) {
          const w = tieBetween(isl.band, p.band);
          if (w) cost += (w / (totalTie || 1)) * Math.hypot(p.cx - f.x, p.cz - f.z) * 3;
        }
        if (cost < bestCost) { bestCost = cost; best = f; }
      }
    }
    isl.cx = best.x; isl.cz = best.z;
    placed.push(isl);
  }
  const outer = placed.reduce((m, p) => Math.max(m, Math.hypot(p.cx, p.cz) + p.r), plazaR);
  for (const isl of islands) {
    isl.members.forEach((n, i) => {
      n.tx = isl.cx + isl.local[i][0];
      n.tz = isl.cz + isl.local[i][1];
    });
  }
  S.islands = islands;
  S.extent = Math.max(outer, 6);
}

// ---------- scene ----------
function clearScene(free) {
  for (const m of S.columns.values()) {
    S.scene.remove(m);
    if (free) { m.geometry.dispose(); m.material.forEach((mm) => { mm.map?.dispose(); mm.dispose(); }); }
  }
  S.columns.clear();
  for (const o of [S.roads, S.egoMesh, S.grid, ...S.arcs]) {
    if (!o) continue;
    S.scene.remove(o);
    if (free) { o.geometry?.dispose(); o.material?.dispose(); }
  }
  S.roads = S.egoMesh = S.grid = null; S.arcs = [];
  S.labelsEl.innerHTML = "";
  S.labels = null;
}

function buildScene(fresh) {
  clearScene(!S.lost);
  // columns: one hex prism per person, sides in the band colour, cap
  // wearing the face (or initials while it loads / when there is none)
  const geo = new THREE.CylinderGeometry(COL_R, COL_R, 1, 6, 1);
  geo.translate(0, 0.5, 0);           // base on the ground, scale y = height
  for (const n of S.nodes) {
    const side = new THREE.MeshStandardMaterial({
      color: new THREE.Color(n.color).multiplyScalar(0.72), roughness: 0.82, metalness: 0.08,
    });
    const cap = new THREE.MeshStandardMaterial({
      map: faceTexture(n), roughness: 0.6, metalness: 0.0,
    });
    const mesh = new THREE.Mesh(geo, [side, cap, side]);
    mesh.castShadow = true; mesh.receiveShadow = true;
    mesh.userData.node = n;
    if (fresh || n.x === 0 && n.z === 0) { n.x = n.tx; n.z = n.tz; }
    mesh.position.set(n.x, 0, n.z);
    mesh.scale.y = n.h;
    // no yaw on the prism: CylinderGeometry already puts a vertex at +z,
    // which is the pointy-top orientation the axial grid is laid out in
    S.scene.add(mesh);
    S.columns.set(n.id, mesh);
    n.mesh = mesh; n.baseColor = side.color.clone();
  }
  // the plaza: you, a low wide platform in the middle
  const plaza = new THREE.Mesh(
    new THREE.CylinderGeometry(HEX * 2.2, HEX * 2.4, 0.16, 6, 1),
    new THREE.MeshStandardMaterial({ color: new THREE.Color(EGO_COLOR).multiplyScalar(0.55),
      roughness: 0.9 }));
  plaza.position.y = 0.08;
  plaza.receiveShadow = true; plaza.castShadow = true;
  plaza.userData.ego = true;
  S.scene.add(plaza);
  S.egoMesh = plaza;
  buildGrid();
  buildRoads();
  buildLabels();
  applyLight();
  S.dirty = true;
}

// a faint hex lattice under everything, drawn once as line segments
function buildGrid() {
  const R = Math.ceil((S.extent + 6) / NEIGH) + 2;
  const pts = [];
  const r0 = HEX * 0.985;
  for (let q = -R; q <= R; q++) {
    for (let r = -R; r <= R; r++) {
      if (Math.abs(q + r) > R) continue;
      const [x, z] = axialToWorld(q, r);
      if (Math.hypot(x, z) > S.extent + 6) continue;
      for (let i = 0; i < 6; i++) {
        const a1 = Math.PI / 3 * i, a2 = Math.PI / 3 * (i + 1);
        pts.push(x + r0 * Math.sin(a1), 0.005, z + r0 * Math.cos(a1),
                 x + r0 * Math.sin(a2), 0.005, z + r0 * Math.cos(a2));
      }
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  const m = new THREE.LineBasicMaterial({ color: 0xcfcbc2, transparent: true, opacity: 0.045 });
  S.grid = new THREE.LineSegments(g, m);
  S.scene.add(S.grid);
}

// roads: every tie as a flat ribbon on the ground, additive so the busy
// crossings glow a little and a lone road stays faint
function roadEdges() { return S.edges.filter((e) => e.weight >= ROAD_FLOOR); }
function buildRoads() {
  const pos = [], col = [];
  const c = new THREE.Color();
  for (const e of roadEdges()) {
    const a = S.byId.get(e.a), b = S.byId.get(e.b);
    const dx = b.x - a.x, dz = b.z - a.z, len = Math.hypot(dx, dz) || 1;
    const w = 0.03 + Math.min(0.09, (e.weight - ROAD_FLOOR) * 0.05);
    const nx = -dz / len * w, nz = dx / len * w;
    const y = 0.012;
    // faint, and a road crossing the whole map fainter still: the long
    // ones are what stack into a haze
    const bright = (0.03 + Math.min(0.12, (e.weight - ROAD_FLOOR) * 0.07)) * (len > 14 ? 0.5 : 1);
    c.set(0xcfcbc2).multiplyScalar(bright);
    const quad = [
      a.x + nx, y, a.z + nz,  b.x + nx, y, b.z + nz,  b.x - nx, y, b.z - nz,
      a.x + nx, y, a.z + nz,  b.x - nx, y, b.z - nz,  a.x - nx, y, a.z - nz,
    ];
    pos.push(...quad);
    for (let i = 0; i < 6; i++) col.push(c.r, c.g, c.b);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  const m = new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true,
    blending: THREE.AdditiveBlending, depthWrite: false });
  S.roads = new THREE.Mesh(g, m);
  S.roads.renderOrder = 1;
  S.scene.add(S.roads);
}

function relayoutRoads() {
  if (!S.roads) return;
  const arr = S.roads.geometry.attributes.position.array;
  let i = 0;
  for (const e of roadEdges()) {
    const a = S.byId.get(e.a), b = S.byId.get(e.b);
    const dx = b.x - a.x, dz = b.z - a.z, len = Math.hypot(dx, dz) || 1;
    const w = 0.03 + Math.min(0.09, (e.weight - ROAD_FLOOR) * 0.05);
    const nx = -dz / len * w, nz = dx / len * w;
    const quad = [
      a.x + nx, a.z + nz, b.x + nx, b.z + nz, b.x - nx, b.z - nz,
      a.x + nx, a.z + nz, b.x - nx, b.z - nz, a.x - nx, a.z - nz,
    ];
    for (let k = 0; k < 6; k++) { arr[i] = quad[k * 2]; arr[i + 2] = quad[k * 2 + 1]; i += 3; }
  }
  S.roads.geometry.attributes.position.needsUpdate = true;
}

// arcs: the selected person's ties, lifted off the ground as tubes
function buildArcs(node) {
  for (const a of S.arcs) { S.scene.remove(a); a.geometry.dispose(); a.material.dispose(); }
  S.arcs = [];
  if (!node) return;
  const ties = S.adj.get(node.id) || [];
  for (const t of ties.slice(0, 40)) {
    const o = S.byId.get(t.pid);
    if (!o) continue;
    const p0 = new THREE.Vector3(node.x, node.h + LIFT + 0.05, node.z);
    const p2 = new THREE.Vector3(o.x, o.h + 0.05, o.z);
    const d = p0.distanceTo(p2);
    const mid = p0.clone().add(p2).multiplyScalar(0.5);
    mid.y = Math.max(p0.y, p2.y) + 0.6 + d * 0.22;
    const curve = new THREE.QuadraticBezierCurve3(p0, mid, p2);
    const rad = 0.018 + Math.min(0.03, t.weight * 0.012);
    const geo = new THREE.TubeGeometry(curve, 24, rad, 6, false);
    const mat = new THREE.MeshBasicMaterial({ color: 0xd8c3af, transparent: true,
      opacity: 0.10 + Math.min(0.28, t.weight * 0.12), blending: THREE.AdditiveBlending,
      depthWrite: false });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.renderOrder = 2;
    S.scene.add(mesh);
    S.arcs.push(mesh);
  }
  // and the road home: you to them, through the plaza
  const ego = S.egoW.get(node.id);
  if (ego) {
    const p0 = new THREE.Vector3(node.x, node.h + LIFT + 0.05, node.z);
    const p2 = new THREE.Vector3(0, 0.2, 0);
    const mid = p0.clone().add(p2).multiplyScalar(0.5);
    mid.y = p0.y + 1.2 + p0.distanceTo(p2) * 0.25;
    const geo = new THREE.TubeGeometry(new THREE.QuadraticBezierCurve3(p0, mid, p2), 32, 0.035, 6, false);
    const mat = new THREE.MeshBasicMaterial({ color: 0xa9651b, transparent: true, opacity: 0.55,
      blending: THREE.AdditiveBlending, depthWrite: false });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.renderOrder = 2;
    S.scene.add(mesh);
    S.arcs.push(mesh);
  }
}

function applyLight() {
  const sun = S.light;
  const e = S.extent + 8;
  sun.shadow.camera.left = -e; sun.shadow.camera.right = e;
  sun.shadow.camera.top = e; sun.shadow.camera.bottom = -e;
  sun.shadow.camera.updateProjectionMatrix();
}

// ---------- faces ----------
// A cap texture is a hex-clipped face over the band colour, initials until
// the image lands (and forever, when there is no face on file).
function faceTexture(n) {
  const size = 256;
  const cv = document.createElement("canvas");
  cv.width = size; cv.height = size;
  const ctx = cv.getContext("2d");
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  const paintBase = () => {
    ctx.clearRect(0, 0, size, size);
    ctx.fillStyle = n.color;
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = "rgba(0,0,0,.28)";
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = "#e8e3d9";
    ctx.font = `600 ${size * 0.34}px "Helvetica Neue", Helvetica, Arial, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(initialsOf(n.name), size / 2, size / 2 + 2);
  };
  paintBase();
  if (n.face) {
    const img = new Image();
    img.onload = () => {
      ctx.clearRect(0, 0, size, size);
      const s = Math.max(size / img.width, size / img.height);
      const w = img.width * s, h = img.height * s;
      ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
      // a thin band-coloured rim so the group reads on the cap too
      ctx.strokeStyle = n.color; ctx.lineWidth = 10;
      ctx.beginPath();
      for (let i = 0; i < 6; i++) {
        const a = Math.PI / 3 * i + Math.PI / 6;
        const x = size / 2 + Math.cos(a) * (size / 2 - 2), y = size / 2 + Math.sin(a) * (size / 2 - 2);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.closePath(); ctx.stroke();
      tex.needsUpdate = true; S.dirty = true;
    };
    img.src = "/api/atlas/face/" + n.id;
  }
  // The cylinder cap's UVs map the unit circle; flip so faces are upright
  // from the default camera side.
  tex.center.set(0.5, 0.5); tex.rotation = CAP_ROT;
  return tex;
}

// ---------- labels (DOM) ----------
function buildLabels() {
  S.labelsEl.innerHTML = "";
  S.labels = [];
  for (const isl of S.islands) {
    const L = dom("div", "relief-isl" + (isl.band === "__none" ? " dim" : ""));
    L.appendChild(dom("span", "relief-isl-name", isl.label));
    L.appendChild(dom("span", "relief-isl-n", String(isl.members.length)));
    L.style.setProperty("--c", isl.color);
    L.addEventListener("click", () => flyTo(isl.cx, isl.cz, Math.max(10, isl.r * 3.2)));
    S.labelsEl.appendChild(L);
    S.labels.push({ el: L, isl });
  }
  S.youEl = dom("div", "relief-you", "you");
  S.labelsEl.appendChild(S.youEl);
  S.nameEl = dom("div", "relief-name");
  S.nameEl.style.display = "none";
  S.labelsEl.appendChild(S.nameEl);
}

const _v = new THREE.Vector3();
function project(x, y, z) {
  _v.set(x, y, z).project(S.camera);
  const w = S.stage.clientWidth, h = S.stage.clientHeight;
  return { sx: (_v.x + 1) / 2 * w, sy: (1 - _v.y) / 2 * h, behind: _v.z > 1 };
}

function paintLabels() {
  if (!S.labels) return;
  const w = S.stage.clientWidth, h = S.stage.clientHeight;
  for (const { el, isl } of S.labels) {
    const p = project(isl.cx, 0.02, isl.cz);
    const tooFar = S.cur.dist > 95;
    const off = p.behind || p.sx < -80 || p.sx > w + 80 || p.sy < -30 || p.sy > h + 30;
    el.style.display = off || tooFar ? "none" : "";
    if (!off && !tooFar) {
      el.style.transform = `translate(${p.sx.toFixed(1)}px, ${p.sy.toFixed(1)}px) translate(-50%, -50%)`;
      el.style.opacity = S.sel ? (S.sel.band === isl.band ? 1 : 0.35) : 1;
    }
  }
  const y = project(0, 0.2, 0);
  S.youEl.style.display = y.behind ? "none" : "";
  S.youEl.style.transform = `translate(${y.sx.toFixed(1)}px, ${y.sy.toFixed(1)}px) translate(-50%, -50%)`;
  const n = S.hover || S.sel;
  if (n && n.mesh) {
    const p = project(n.x, n.h + (n === S.sel ? LIFT : 0) + 0.35, n.z);
    S.nameEl.style.display = p.behind ? "none" : "";
    S.nameEl.textContent = n.name;
    S.nameEl.style.transform = `translate(${p.sx.toFixed(1)}px, ${p.sy.toFixed(1)}px) translate(-50%, -100%)`;
  } else S.nameEl.style.display = "none";
}

// ---------- camera ----------
function camPosition(c) {
  const p = THREE.MathUtils.degToRad(c.pitch);
  return new THREE.Vector3(
    c.x + c.dist * Math.cos(p) * Math.sin(c.yaw),
    c.dist * Math.sin(p),
    c.z + c.dist * Math.cos(p) * Math.cos(c.yaw));
}

function applyCamera() {
  const c = S.cur;
  S.camera.position.copy(camPosition(c));
  S.camera.lookAt(c.x, 0.4, c.z);
  S.scene.fog.near = c.dist * 1.6;
  S.scene.fog.far = c.dist * 4.2 + 40;
}

function frameAll(snap) {
  // frame the terrain's own bounds, not the plaza: the islands are
  // packed around it, never symmetrically
  let minX = -3, maxX = 3, minZ = -3, maxZ = 3;
  for (const n of S.nodes) {
    minX = Math.min(minX, n.tx - 1); maxX = Math.max(maxX, n.tx + 1);
    minZ = Math.min(minZ, n.tz - 1); maxZ = Math.max(maxZ, n.tz + 1);
  }
  const span = Math.max(maxX - minX, maxZ - minZ);
  const d = clamp(span * 1.05 + 6, CAM.distMin, CAM.distMax);
  Object.assign(S.cam, { x: (minX + maxX) / 2, z: (minZ + maxZ) / 2, dist: d });
  if (snap) Object.assign(S.cur, S.cam);
  S.dirty = true;
}

function flyTo(x, z, dist, opts = {}) {
  if (dist != null) S.cam.dist = clamp(dist, CAM.distMin, CAM.distMax);
  // the plaque docks at the right edge, so a person we fly to is framed in
  // the space LEFT of it: shift the target along the camera's right vector
  // by half the plaque's width, measured in ground units at the new distance
  let sx = 0;
  if (opts.clearCard && S.cardEl.style.display !== "none") {
    const h = S.stage.clientHeight || 1;
    const worldPerPx = (2 * S.cam.dist * Math.tan(THREE.MathUtils.degToRad(CAM.fov / 2))) / h;
    sx = (S.cardEl.offsetWidth / 2) * worldPerPx;
  }
  S.cam.x = x + Math.cos(S.cam.yaw) * sx;
  S.cam.z = z - Math.sin(S.cam.yaw) * sx;
  if (S.reduced) Object.assign(S.cur, S.cam);
  S.dirty = true;
}

// where the cursor's ray meets the ground
const _plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
const _hit = new THREE.Vector3();
function groundAt(clientX, clientY) {
  const r = S.canvas.getBoundingClientRect();
  const nx = ((clientX - r.left) / r.width) * 2 - 1;
  const ny = -((clientY - r.top) / r.height) * 2 + 1;
  S.ray.setFromCamera({ x: nx, y: ny }, S.camera);
  return S.ray.ray.intersectPlane(_plane, _hit) ? _hit.clone() : null;
}

function columnAt(clientX, clientY) {
  const r = S.canvas.getBoundingClientRect();
  const nx = ((clientX - r.left) / r.width) * 2 - 1;
  const ny = -((clientY - r.top) / r.height) * 2 + 1;
  S.ray.setFromCamera({ x: nx, y: ny }, S.camera);
  const hits = S.ray.intersectObjects([...S.columns.values()], false);
  return hits.length ? hits[0].object.userData.node : null;
}

// ---------- pointer ----------
function bindPointer() {
  const cv = S.canvas;
  cv.addEventListener("pointerdown", (e) => {
    if (S.lost) return;
    cv.setPointerCapture(e.pointerId);
    S.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (S.pointers.size === 2) {
      const [a, b] = [...S.pointers.values()];
      S.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
      S.drag = null;
      return;
    }
    const pan = e.button === 1 || e.button === 2 || e.shiftKey;
    S.drag = { x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY,
      pan, moved: false, button: e.button,
      yaw: S.cam.yaw, pitch: S.cam.pitch, cx: S.cam.x, cz: S.cam.z, t: performance.now() };
    if (e.button === 0) S.drag.hitNode = columnAt(e.clientX, e.clientY);
  });
  cv.addEventListener("pointermove", (e) => {
    if (S.lost) return;
    if (S.pointers.has(e.pointerId)) S.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (S.pinch && S.pointers.size === 2) {
      const [a, b] = [...S.pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      if (S.pinch.d > 0) S.cam.dist = clamp(S.cam.dist * (S.pinch.d / d), CAM.distMin, CAM.distMax);
      panBy(mx - S.pinch.mx, my - S.pinch.my);
      S.pinch = { d, mx, my };
      S.dirty = true;
      return;
    }
    if (S.drag) {
      const dx = e.clientX - S.drag.x0, dy = e.clientY - S.drag.y0;
      if (!S.drag.moved && Math.hypot(dx, dy) > 4) S.drag.moved = true;
      if (S.drag.moved) {
        if (S.drag.pan) {
          panBy(e.clientX - S.drag.x, e.clientY - S.drag.y);
        } else {
          S.cam.yaw = S.drag.yaw - dx * CAM.yawGain;
          S.cam.pitch = clamp(S.drag.pitch + dy * CAM.pitchGain, CAM.pitchMin, CAM.pitchMax);
        }
        S.drag.x = e.clientX; S.drag.y = e.clientY;
        S.dirty = true;
        hideTip();
      }
      return;
    }
    // hover
    const n = columnAt(e.clientX, e.clientY);
    if (n !== S.hover) {
      S.hover = n;
      cv.style.cursor = n ? "pointer" : "";
      S.dirty = true;
    }
    if (n) showTip(n, e.clientX, e.clientY); else hideTip();
  });
  const up = (e) => {
    S.pointers.delete(e.pointerId);
    if (S.pointers.size < 2) S.pinch = null;
    if (!S.drag) return;
    const d = S.drag; S.drag = null;
    if (d.moved || d.button !== 0) return;
    const n = columnAt(e.clientX, e.clientY);
    if (n) select(n, true); else clearSel();
  };
  cv.addEventListener("pointerup", up);
  cv.addEventListener("pointercancel", (e) => { S.pointers.delete(e.pointerId); S.pinch = null; S.drag = null; });
  cv.addEventListener("pointerleave", () => { S.hover = null; hideTip(); S.dirty = true; });
  cv.addEventListener("dblclick", (e) => {
    const n = columnAt(e.clientX, e.clientY);
    if (n) openPerson(n.id);
  });
  cv.addEventListener("wheel", (e) => {
    if (S.lost) return;
    e.preventDefault();
    const before = groundAt(e.clientX, e.clientY);
    const f = Math.exp(e.deltaY * 0.0016);
    const nd = clamp(S.cam.dist * f, CAM.distMin, CAM.distMax);
    const k = 1 - nd / S.cam.dist;          // dolly toward the point under the cursor
    if (before) { S.cam.x = lerp(S.cam.x, before.x, k); S.cam.z = lerp(S.cam.z, before.z, k); }
    S.cam.dist = nd;
    S.dirty = true;
  }, { passive: false });
  cv.addEventListener("contextmenu", (e) => {
    const n = columnAt(e.clientX, e.clientY);
    if (!n) return;                      // the Vira-wide menu takes the stage
    e.preventDefault(); e.stopPropagation();
    showContextMenu(e.clientX, e.clientY, [
      { head: "Relief: " + n.name },
      { label: "Open profile", run: () => openPerson(n.id) },
      { label: "Show their ties", run: () => select(n, true) },
      { label: "Ask Vira about " + firstName(n.name) + "...",
        run: () => ctxAskVira(e.clientX, e.clientY,
          { component: "Relief", person: { id: n.id, name: n.name } }) },
    ]);
  });
  // keyboard: Escape clears, arrows nudge the table
  S.stage.tabIndex = 0;
  S.stage.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { clearSel(); S.stage.blur(); }
    if (e.key === "ArrowLeft") S.cam.yaw += 0.12;
    if (e.key === "ArrowRight") S.cam.yaw -= 0.12;
    if (e.key === "ArrowUp") S.cam.pitch = clamp(S.cam.pitch + 4, CAM.pitchMin, CAM.pitchMax);
    if (e.key === "ArrowDown") S.cam.pitch = clamp(S.cam.pitch - 4, CAM.pitchMin, CAM.pitchMax);
    S.dirty = true;
  });
}

// slide the ground under the cursor: a pixel of drag moves the target by
// however much ground a pixel covers at the current distance
function panBy(dx, dy) {
  const h = S.stage.clientHeight || 1;
  const worldPerPx = (2 * S.cam.dist * Math.tan(THREE.MathUtils.degToRad(CAM.fov / 2))) / h;
  const p = THREE.MathUtils.degToRad(S.cur.pitch);
  const fwd = { x: -Math.sin(S.cur.yaw), z: -Math.cos(S.cur.yaw) };
  const right = { x: Math.cos(S.cur.yaw), z: -Math.sin(S.cur.yaw) };
  const gy = dy * worldPerPx / Math.max(0.35, Math.sin(p));
  S.cam.x -= right.x * dx * worldPerPx - fwd.x * gy;
  S.cam.z -= right.z * dx * worldPerPx - fwd.z * gy;
  const lim = S.extent + 12;
  S.cam.x = clamp(S.cam.x, -lim, lim); S.cam.z = clamp(S.cam.z, -lim, lim);
}

function showTip(n, x, y) {
  const t = S.tipEl;
  t.style.display = "";
  t.innerHTML = "";
  t.appendChild(dom("div", "relief-tip-name", n.name));
  const sub = [n.title, n.company].filter(Boolean).join(" - ") || n.relationship_class || "";
  if (sub) t.appendChild(dom("div", "hint", sub));
  const r = S.stage.getBoundingClientRect();
  t.style.left = Math.min(r.width - 220, x - r.left + 14) + "px";
  t.style.top = Math.max(6, y - r.top - 40) + "px";
}
function hideTip() { S.tipEl.style.display = "none"; }

// ---------- selection ----------
function select(n, fly) {
  if (S.sel === n) return;
  S.sel = n;
  S.nb1 = new Set((S.adj.get(n.id) || []).map((t) => t.pid));
  S.nb2 = new Set();
  for (const p of S.nb1) for (const t of (S.adj.get(p) || [])) {
    if (t.pid !== n.id && !S.nb1.has(t.pid)) S.nb2.add(t.pid);
  }
  applyEmphasis();
  buildArcs(n);
  renderCard(n);
  if (fly) flyTo(n.x, n.z, Math.min(S.cam.dist, 18), { clearCard: true });
  S.dirty = true;
}

function clearSel() {
  if (!S.sel) return;
  S.sel = null; S.nb1 = new Set(); S.nb2 = new Set();
  applyEmphasis();
  buildArcs(null);
  S.cardEl.style.display = "none";
  S.dirty = true;
}

// Emphasis is a property of the whole stage, not of one mesh: the selected
// column lifts and glows, its direct ties keep their colour, two hops fade
// to half, and everyone else recedes toward the ground.
function applyEmphasis() {
  const q = S.matches;
  for (const n of S.nodes) {
    const m = n.mesh; if (!m) continue;
    const side = m.material[0], cap = m.material[1];
    let level = 1;                          // 1 full, .5 half, .18 receded
    if (S.sel) level = n === S.sel ? 1 : S.nb1.has(n.id) ? 1 : S.nb2.has(n.id) ? 0.55 : 0.16;
    if (q) level = q.has(n.id) ? Math.max(level, 1) : Math.min(level, 0.16);
    const isSel = n === S.sel;
    side.color.copy(n.baseColor).lerp(new THREE.Color(0x101110), 1 - level);
    side.emissive.set(isSel ? 0x3a2a16 : 0x000000);
    cap.color.setScalar(0.35 + 0.65 * level);
    tween(m.position, { y: isSel ? LIFT : 0 }, 420);
    m.castShadow = level > 0.3;
  }
}

function tween(obj, to, ms) {
  S.tweens = S.tweens.filter((t) => t.obj !== obj);
  const from = {}; for (const k in to) from[k] = obj[k];
  if (S.reduced || !ms) { Object.assign(obj, to); S.dirty = true; return; }
  S.tweens.push({ obj, from, to, t0: performance.now(), ms });
  S.dirty = true;
}

// ---------- the plaque ----------
async function renderCard(n) {
  const card = S.cardEl;
  const seq = ++S.detailSeq;
  card.style.display = "";
  card.innerHTML = "";
  const head = dom("div", "relief-card-head");
  const av = avatarNode(n.id, n.name, true, n.face != null);
  if (n.face) av.querySelector("img")?.setAttribute("src", "/api/atlas/face/" + n.id);
  head.appendChild(av);
  const mid = dom("div", "relief-card-name");
  const nm = dom("div", "click", n.name);
  nm.title = "Open the profile";
  nm.addEventListener("click", () => openPerson(n.id));
  mid.appendChild(nm);
  const sub = [n.title, n.company].filter(Boolean).join(" - ") || n.relationship_class || "";
  if (sub) mid.appendChild(dom("div", "hint", sub));
  head.appendChild(mid);
  const x = dom("button", "idea-del", "×");
  x.title = "Clear (Esc)";
  x.addEventListener("click", clearSel);
  head.appendChild(x);
  card.appendChild(head);

  const chips = dom("div", "relief-chips");
  const isl = S.islands.find((i) => i.band === n.band);
  if (isl) {
    const c = dom("span", "relief-chip", isl.label);
    c.style.borderColor = isl.color; c.style.color = isl.color;
    c.addEventListener("click", () => flyTo(isl.cx, isl.cz, Math.max(10, isl.r * 3.2)));
    chips.appendChild(c);
  }
  if (n.degree) chips.appendChild(dom("span", "relief-chip",
    ["1st", "2nd", "3rd"][n.degree - 1] || n.degree + "th"));
  const ego = S.egoW.get(n.id);
  if (ego) chips.appendChild(dom("span", "relief-chip you",
    ego.signals.map((s) => s.detail).join(" - ")));
  card.appendChild(chips);

  const body = dom("div", "relief-card-body");
  body.appendChild(dom("div", "hint", "Reading the record..."));
  card.appendChild(body);

  // the ties, from the graph we already hold - no round trip
  const ties = S.adj.get(n.id) || [];
  const list = dom("div", "relief-ties");
  list.appendChild(dom("div", "relief-h", ties.length
    ? `Tied to ${ties.length} ${ties.length === 1 ? "person" : "people"}`
    : "No ties in the graph - they connect only through you"));
  for (const t of ties.slice(0, 30)) {
    const o = S.byId.get(t.pid); if (!o) continue;
    const row = dom("div", "relief-tie");
    const nameRow = dom("div", "relief-tie-name", o.name);
    const wrap = dom("span", "relief-wwrap");
    const bar = dom("span", "relief-w");
    bar.style.width = Math.min(100, t.weight * 55) + "%";
    wrap.appendChild(bar); nameRow.appendChild(wrap);
    row.appendChild(nameRow);
    row.appendChild(dom("div", "relief-tie-why",
      t.narrative || t.signals.map((s) => s.detail).filter(Boolean).join(" - ")));
    row.title = "Fly to " + firstName(o.name);
    row.addEventListener("click", () => select(o, true));
    list.appendChild(row);
  }
  if (ties.length > 30) list.appendChild(dom("div", "hint", `+${ties.length - 30} more`));
  card.appendChild(list);

  // the record: one fetch, rendered into the body when it lands
  try {
    const d = await api("/api/person/" + n.id);
    if (seq !== S.detailSeq) return;
    body.innerHTML = "";
    const prof = d.profile || {};
    // the registry's activity stamps move with the CRM refresh; the
    // profile's last_exchange is a synthesis-time snapshot and reads stale
    const act = d.person?.activity || {};
    // n.last is the read-time overlay (crm._last_contact lifted by the live
    // chat.db read) - the freshest of the four, never the stale snapshot
    const lastIso = [n.last, act.imsg_last, act.email_last, d.chats?.[0]?.date_last]
      .filter((v) => typeof v === "string" && v).sort().pop() || "";
    if (lastIso) {
      const ago = agoText(lastIso);
      const row = dom("div", "relief-fact");
      row.appendChild(dom("span", "relief-k", "last spoke"));
      row.appendChild(dom("span", null, ago + (String(lastIso).length >= 10 ? " - " + String(lastIso).slice(0, 10) : "")));
      body.appendChild(row);
    }
    if (prof.relationship_summary) {
      const sents = String(prof.relationship_summary).split(/(?<=[.!?])\s+/).slice(0, 3).join(" ");
      body.appendChild(dom("p", "relief-summary", sents));
    }
    const loops = (prof.open_loops || []).filter((l) => (l.status || "open") === "open");
    if (loops.length) {
      body.appendChild(dom("div", "relief-h", `Open between you (${loops.length})`));
      for (const l of loops.slice(0, 3)) {
        const row = dom("div", "relief-loop");
        row.appendChild(dom("span", "relief-owe " + (l.owed_by === "me" ? "me" : "them"),
          l.owed_by === "me" ? "you owe" : "they owe"));
        row.appendChild(dom("span", null, l.what));
        body.appendChild(row);
      }
    }
    const hooks = prof.hooks || [];
    if (hooks.length) {
      body.appendChild(dom("div", "relief-h", "Worth raising"));
      for (const h of hooks.slice(0, 2)) {
        const row = dom("div", "relief-hook", h.angle || h.hook || h.text || "");
        if (h.detail) row.title = h.detail;
        body.appendChild(row);
      }
    }
    if (!body.childNodes.length)
      body.appendChild(dom("div", "hint", "No relationship read on file yet."));
    const open = dom("button", "fchip relief-open", "Open profile");
    open.addEventListener("click", () => openPerson(n.id));
    body.appendChild(open);
  } catch (e) {
    if (seq !== S.detailSeq) return;
    body.innerHTML = "";
    body.appendChild(dom("div", "hint", "Record unavailable - " + (e && e.message)));
  }
}

// ---------- chrome ----------
function bindChrome() {
  const search = document.getElementById("relief-search");
  search.addEventListener("input", () => {
    S.query = search.value.trim().toLowerCase();
    if (!S.query) { S.matches = null; applyEmphasis(); return; }
    S.matches = new Set(S.nodes.filter((n) =>
      n.name.toLowerCase().includes(S.query)
      || (n.company || "").toLowerCase().includes(S.query)).map((n) => n.id));
    applyEmphasis();
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && S.matches && S.matches.size) {
      const first = S.nodes.find((n) => S.matches.has(n.id));
      if (first) select(first, true);
    }
    if (e.key === "Escape") { search.value = ""; search.dispatchEvent(new Event("input")); search.blur(); }
  });
  document.getElementById("relief-home").addEventListener("click", () => {
    clearSel(); S.cam.yaw = 0.6; S.cam.pitch = 52; frameAll(false);
  });
  document.getElementById("relief-refresh").addEventListener("click", () => load(true));
}

function renderLenses() {
  const box = document.getElementById("relief-lenses");
  box.innerHTML = "";
  for (const l of (S.graph.lenses || [])) {
    const b = dom("button", "fchip" + (l.id === S.lens ? " on" : ""), l.label);
    b.title = l.blurb + (l.placed < l.total ? ` - ${l.placed} of ${l.total} placed` : "");
    b.addEventListener("click", () => setLens(l.id));
    box.appendChild(b);
  }
}

// Switching lens is a MIGRATION, not a rebuild: every column glides from
// where it stood to where the new grouping puts it, roads following, so the
// eye can track a person across the regroup.
function setLens(id) {
  if (id === S.lens) return;
  S.lens = id; lsSet("vira-relief-lens", id);
  renderLenses();
  layout(id);
  for (const n of S.nodes) {
    const m = n.mesh; if (!m) continue;
    m.material[0].color.set(new THREE.Color(n.color).multiplyScalar(0.72));
    n.baseColor = m.material[0].color.clone();
    m.material[1].map?.dispose();
    m.material[1].map = faceTexture(n);
    m.material[1].needsUpdate = true;
    const from = { x: n.x, z: n.z }, to = { x: n.tx, z: n.tz };
    n._mig = { from, to };
  }
  S.migration = { t0: performance.now(), ms: S.reduced ? 0 : 1100 };
  buildLabels();
  if (S.grid) { S.scene.remove(S.grid); S.grid.geometry.dispose(); S.grid.material.dispose(); }
  buildGrid();
  applyLight();
  if (S.sel) { applyEmphasis(); }
  frameAll(false);
  S.dirty = true;
}

// ---------- loop ----------
function wake() {
  if (S.running || !S.renderer || S.lost) return;
  S.running = true;
  S.lastT = performance.now();
  requestAnimationFrame(frame);
}
function sleep() { S.running = false; }

function frame(t) {
  if (!S.running) return;
  const dt = Math.min(0.05, (t - S.lastT) / 1000);
  S.lastT = t;
  let moving = false;

  // camera easing toward its targets
  const k = S.reduced ? 1 : 1 - Math.exp(-CAM.ease * dt);
  for (const key of ["x", "z", "pitch", "dist"]) {
    const d = S.cam[key] - S.cur[key];
    if (Math.abs(d) > 1e-3) { S.cur[key] += d * k; moving = true; } else S.cur[key] = S.cam[key];
  }
  // yaw: shortest way round
  let dy = S.cam.yaw - S.cur.yaw;
  dy = Math.atan2(Math.sin(dy), Math.cos(dy));
  if (Math.abs(dy) > 1e-3) { S.cur.yaw += dy * k; moving = true; } else S.cur.yaw = S.cam.yaw;

  // property tweens (lifts)
  if (S.tweens.length) {
    const keep = [];
    for (const tw of S.tweens) {
      const p = Math.min(1, (t - tw.t0) / tw.ms), e = easeInOut(p);
      for (const key in tw.to) tw.obj[key] = lerp(tw.from[key], tw.to[key], e);
      if (p < 1) keep.push(tw);
    }
    S.tweens = keep; moving = true;
  }
  // lens migration
  if (S.migration) {
    const p = S.migration.ms ? Math.min(1, (t - S.migration.t0) / S.migration.ms) : 1;
    const e = easeInOut(p);
    for (const n of S.nodes) {
      if (!n._mig) continue;
      n.x = lerp(n._mig.from.x, n._mig.to.x, e);
      n.z = lerp(n._mig.from.z, n._mig.to.z, e);
      n.mesh.position.x = n.x; n.mesh.position.z = n.z;
      n.mesh.position.y = (n === S.sel ? LIFT : 0) + Math.sin(p * Math.PI) * 0.9;
    }
    relayoutRoads();
    if (p >= 1) { S.migration = null; for (const n of S.nodes) n._mig = null; buildArcs(S.sel); }
    moving = true;
  }

  if (moving || S.dirty) {
    applyCamera();
    S.renderer.render(S.scene, S.camera);
    paintLabels();
    S.dirty = false;
  }
  requestAnimationFrame(frame);
}

// diagnostics: the camera and selection are readable, so behaviour can be
// measured instead of argued about
window.__relief = {
  state: () => ({ cam: { ...S.cam }, cur: { ...S.cur }, sel: S.sel?.id || null,
    lens: S.lens, nodes: S.nodes.length, islands: S.islands.map((i) => ({
      band: i.band, label: i.label, n: i.members.length, cx: i.cx, cz: i.cz, r: i.r })) }),
  select: (pid) => { const n = S.byId.get(pid); if (n) select(n, true); },
  setLens, flyTo, clear: clearSel,
};
window.reliefLoad = load;

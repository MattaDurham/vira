/* Orbits - the network as time.

   A second VIEW of the World window (2026-09-02; folded in from its own
   dock window the same day, owner's call - the galaxy and the orbits are
   two views of one module, toggled in the head). You are the sun. Every
   contact is a CARD in orbit around you, and the one thing the picture
   encodes that no force layout can is WHEN: the radius of a card's orbit
   is how long since you last spoke - today at the inner ring, this week,
   this month, this quarter, this year, then the long dark past the rim.
   Drift is visible as geometry: the people going quiet are the ones
   sliding outward, and "who am I losing?" is answered by looking at the
   outer rings. Around the ring the cards are grouped into WEDGES by the
   lens you pick (circles, groups, companies, locations), so a community is
   a slice of sky and its age structure reads across the slice. Ties are
   chords bent through the middle, the way a chord diagram draws them; the
   whole system turns slowly, because an orrery that does not move is a
   diagram.

   HOW IT MOVES: you stay at the centre. A drag SPINS the sky around you
   like a record on a platter (owner's call, 2026-09-02 - it used to slide
   the sky, which walked the sun off screen); the wheel zooms toward the
   cursor, so zooming in at the rim zooms in on the rim; a pinch zooms the
   same way and never pans. There is no orbit camera at all - the ORBIT is
   the picture. Clicking a card still centres that card (the panel docks
   beside it), and the next drag spins the sky about the sun from wherever
   it then sits, dropping the follow so the pivot stays put under the hand.

   WHAT A CLICK SHOWS: the card grows and the sky centres on it; its ties
   light up as chords to the people it is tied to, everyone else recedes;
   the panel beside the stage carries the dossier - who they are, the
   history between you, which ring they sit on and what that means, the
   relationship read, what is open, the hooks worth raising - and the ties
   as a list that flies the sky to each. The name opens the profile.

   Data is the Visual Network's own payload (/api/atlas, with the read-time
   `last` overlay) plus the person record on click. No new store. */
"use strict";

// ---------- design constants ----------
const R_IN = 110;                 // today's ring, world units
const R_OUT = 560;                // the rim: everything older than DMAX
const DMAX = 365 * 4;             // beyond four years the rings stop
const RINGS = [
  { days: 1, label: "today" },
  { days: 7, label: "this week" },
  { days: 31, label: "this month" },
  { days: 92, label: "this quarter" },
  { days: 365, label: "this year" },
  { days: 365 * 3, label: "years ago" },
];
const CARD_MIN = 22, CARD_MAX = 44;   // card width by activity
const TIE_FLOOR = 0.5;            // chords drawn at rest (the rest light on select)
const SPIN = 0.012;               // rad/s - the sky's own drift
// INERTIA - a released spin keeps turning like a record and winds down.
// FLING_DECAY is the exponential damping rate (velocity halves every ~0.28s);
// FLING_MIN is where the coast is considered stopped; FLING_MAX caps what a
// fast flick can hand over, so the longest coast is FLING_MAX / FLING_DECAY =
// 1.2 rad - a little over a quarter turn, deliberately "a little alive" rather
// than a wheel. Velocity is read off the last ~80ms of the drag
// (FLING_WINDOW_MS), so a hand that STOPS before letting go hands over zero.
const FLING_DECAY = 2.5, FLING_MIN = 0.02, FLING_MAX = 3.0, FLING_WINDOW_MS = 80;
const WEDGE_GAP = 2.2;            // empty slots between wedges
const BAND_COLORS = [
  "#a39c8d", "#7a8f9c", "#a9651b", "#7d8a74", "#a0715f",
  "#5d6a80", "#8a9a4a", "#7a5d75", "#4f8a86", "#b9a06a",
  "#8c4a3c", "#5e7480", "#9a7f5a", "#6c8c9a", "#a58a3c",
];
const UNPLACED = "#5a5b58";
const LENS_ORDER = ["groups", "circles", "companies", "locations"];

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const easeInOut = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
const TAU = Math.PI * 2;
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
function agoText(d) {
  if (d == null) return "no dated contact";
  if (d === 0) return "today";
  if (d === 1) return "yesterday";
  if (d < 30) return d + " days ago";
  if (d < 365) { const m = Math.max(1, Math.round(d / 30)); return m === 1 ? "1 month ago" : m + " months ago"; }
  const y = (d / 365).toFixed(1).replace(/\.0$/, "");
  return y === "1" ? "1 year ago" : y + " years ago";
}
function ringOf(d) {
  if (d == null) return null;
  for (const r of RINGS) if (d <= r.days) return r;
  return { days: Infinity, label: "long ago" };
}
// the radius a contact orbits at: log time, so a week and a year are both
// readable distances rather than a week vanishing into the middle
function radiusFor(d) {
  if (d == null) return R_OUT + 46;
  const t = Math.log1p(Math.min(d, DMAX)) / Math.log1p(DMAX);
  return R_IN + (R_OUT - R_IN) * t;
}
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// ---------- state ----------
const S = {
  stage: null, canvas: null, ctx: null, cardEl: null, tipEl: null, metaEl: null, emptyEl: null,
  graph: null, lens: "", nodes: [], byId: new Map(), edges: [], adj: new Map(), egoW: new Map(),
  wedges: [], spin: 0, spinV: 0, spinning: true, reduced: false,
  cam: { x: 0, y: 0, k: 1 }, cur: { x: 0, y: 0, k: 1 },
  sel: null, hover: null, nb1: new Set(), matches: null,
  loading: false, loadedGen: null,
  running: false, dirty: true, lastT: 0, migration: null,
  drag: null, pointers: new Map(), pinch: null,
  imgs: new Map(), detailSeq: 0, colors: {},
  W: 0, H: 0, dpr: 1,
};

// ---------- entry ----------
export async function load(force) {
  if (!S.stage) init();
  if (S.loading) return;
  if (S.graph && !force && S.graph.generated === S.loadedGen) { resize(); wake(); return; }
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

function init() {
  S.reduced = typeof REDUCED_MOTION !== "undefined" && REDUCED_MOTION;
  S.stage = document.getElementById("orbits-stage");
  S.canvas = document.getElementById("orbits-canvas");
  S.ctx = S.canvas.getContext("2d");
  S.cardEl = document.getElementById("orbits-card");
  S.tipEl = document.getElementById("orbits-tip");
  S.metaEl = document.getElementById("orbits-meta");
  S.emptyEl = document.getElementById("orbits-empty");
  S.lens = lsGet("vira-orbits-lens", "");
  if (!LENS_ORDER.includes(S.lens)) S.lens = "";
  S.spinning = !S.reduced && lsGet("vira-orbits-drift", true) !== false;
  S.colors = {
    text: cssVar("--text", "#cfcbc2"), dim: cssVar("--text-dim", "#8f8d85"),
    faint: cssVar("--text-faint", "#6a6864"), line: cssVar("--line", "#2f3034"),
    accent: cssVar("--accent-bright", "#a39c8d"), amber: cssVar("--oxidized", "#a9651b"),
    bg: cssVar("--bg-stage", "#060707"), glow: cssVar("--bg-stage-glow", "#0e100e"),
    head: cssVar("--text-head", "#d4d0c6"), warm: cssVar("--term-tool", "#d8c3af"),
    mono: cssVar("--mono", "ui-monospace, Menlo, monospace"),
    display: cssVar("--display", "Georgia, serif"),
    sans: cssVar("--sans", "Helvetica, Arial, sans-serif"),
  };
  bindPointer();
  bindChrome();
  new ResizeObserver(() => resize()).observe(S.stage);
  const io = new IntersectionObserver((ents) => ents.forEach((en) => en.isIntersecting ? wake() : sleep()));
  io.observe(S.stage);
  document.addEventListener("visibilitychange", () => document.hidden ? sleep() : wake());
  resize();
}

function resize() {
  const w = S.stage.clientWidth, h = S.stage.clientHeight;
  if (!w || !h) return;
  S.dpr = Math.min(2, window.devicePixelRatio || 1);
  S.W = w; S.H = h;
  S.canvas.width = Math.round(w * S.dpr); S.canvas.height = Math.round(h * S.dpr);
  S.dirty = true;
}

// ---------- graph -> orbits ----------
function setGraph(g) {
  S.graph = g;
  S.byId = new Map();
  const maxAct = Math.max(1, ...g.nodes.map((n) => n.act || 0));
  S.nodes = g.nodes.map((n) => {
    const d = daysAgo(n.last);
    const node = { ...n, days: d, r: radiusFor(d), a: 0, ax: 0, ay: 0, band: null, color: UNPLACED,
      w: CARD_MIN + (CARD_MAX - CARD_MIN) * Math.log1p(n.act || 0) / Math.log1p(maxAct) };
    node.h = node.w * 1.22;
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
  if (!S.lens) {
    const best = [...(g.lenses || [])].sort((a, b) => b.placed - a.placed)[0];
    S.lens = best ? best.id : "groups";
  }
  layout(S.lens, true);
  renderLenses();
  const dated = S.nodes.filter((n) => n.days != null).length;
  S.metaEl.textContent = `${S.nodes.length} people - ${S.edges.length} ties - ${dated} with a dated last contact`;
  S.cam = { x: 0, y: 0, k: 1 }; S.cur = { x: 0, y: 0, k: 1 };
  frameAll(true);
  wake();
}

function bandsFor(lensId) {
  const lens = (S.graph.lenses || []).find((l) => l.id === lensId) || (S.graph.lenses || [])[0];
  if (!lens) return { bands: [], nodeBand: {} };
  return { bands: lens.bands || [], nodeBand: lens.node_band || {} };
}

// Wedges: each band takes a slice of the circle proportional to its size,
// the biggest first from the top and clockwise; the unbanded share the last
// slice. Inside a wedge the members spread evenly by angle, most active
// first, so the loudest voices in a community sit at its leading edge.
function layout(lensId, snap) {
  const { bands, nodeBand } = bandsFor(lensId);
  const groups = new Map();
  bands.forEach((b, i) => groups.set(b.id, {
    band: b.id, label: b.label, color: BAND_COLORS[i % BAND_COLORS.length], members: [] }));
  const none = { band: "__none", label: "everyone else", color: UNPLACED, members: [] };
  for (const n of S.nodes) {
    const bid = nodeBand[n.id];
    const g = bid != null && groups.has(bid) ? groups.get(bid) : none;
    g.members.push(n); n.band = g.band; n.color = g.color;
  }
  const wedges = [...groups.values()].filter((w) => w.members.length)
    .sort((a, b) => b.members.length - a.members.length);
  if (none.members.length) wedges.push(none);
  const slots = wedges.reduce((s, w) => s + w.members.length + WEDGE_GAP, 0);
  const per = TAU / slots;
  let a = -Math.PI / 2;                        // start at the top
  for (const w of wedges) {
    w.a0 = a + per * (WEDGE_GAP / 2);
    w.members.sort((p, q) => (q.act || 0) - (p.act || 0));
    w.members.forEach((n, i) => {
      n.ta = w.a0 + per * (i + 0.5);
      // a small radial stagger keeps two same-ring neighbours from
      // stacking edge to edge
      n.tr = n.r + ((i % 3) - 1) * 9;
    });
    w.a1 = w.a0 + per * w.members.length;
    a += per * (w.members.length + WEDGE_GAP);
  }
  S.wedges = wedges;
  if (snap || S.reduced) {
    for (const n of S.nodes) { n.a = n.ta; n.rr = n.tr; }
    S.migration = null;
  } else {
    for (const n of S.nodes) n._mig = { a0: n.a, r0: n.rr, a1: n.ta, r1: n.tr };
    S.migration = { t0: performance.now(), ms: 1000 };
  }
  placeAll();
}

function placeAll() {
  const s = S.spin;
  for (const n of S.nodes) {
    n.ax = Math.cos(n.a + s) * n.rr;
    n.ay = Math.sin(n.a + s) * n.rr;
  }
}

// ---------- camera ----------
const sx = (wx) => (wx - S.cur.x) * S.cur.k + S.W / 2;
const sy = (wy) => (wy - S.cur.y) * S.cur.k + S.H / 2;
const wx = (px) => (px - S.W / 2) / S.cur.k + S.cur.x;
const wy = (py) => (py - S.H / 2) / S.cur.k + S.cur.y;

function frameAll(snap) {
  const R = R_OUT + 90;
  const k = Math.min(S.W, S.H) / (2 * R) || 1;
  Object.assign(S.cam, { x: 0, y: 0, k: clamp(k, 0.15, 6) });
  if (snap) Object.assign(S.cur, S.cam);
  S.dirty = true;
}

function flyTo(node, k, opts = {}) {
  const kk = clamp(k ?? Math.max(S.cam.k, 2.2), 0.15, 6);
  // the panel docks at the right edge, so a card we fly to is framed in the
  // space left of it: shift the target right by half the panel's width
  let dx = 0;
  if (opts.clearCard && S.cardEl.style.display !== "none") dx = (S.cardEl.offsetWidth / 2) / kk;
  S.cam.k = kk;
  S.cam.x = node.ax + dx; S.cam.y = node.ay;
  S.follow = node;                            // keep centred while the sky turns
  if (S.reduced) Object.assign(S.cur, S.cam);
  S.dirty = true;
}

// ---------- pointer ----------
// the pointer's angle about the sun (world origin) as drawn on screen
// angular velocity (rad/s) carried out of a drag: the summed turn over the
// trailing window divided by its span. Zero under reduced motion (a coast is
// motion the owner asked not to see), zero when the window is too short to
// say anything, zero when the hand came to rest before releasing.
function flingVelocity(samples) {
  if (S.reduced || !samples || samples.length < 2) return 0;
  const now = performance.now();
  if (now - samples[samples.length - 1].t > FLING_WINDOW_MS) return 0;   // paused, then let go
  const span = (samples[samples.length - 1].t - samples[0].t) / 1000;
  if (span < 0.016) return 0;
  let sum = 0; for (let i = 1; i < samples.length; i++) sum += samples[i].da;
  const v = sum / span;
  if (Math.abs(v) < FLING_MIN) return 0;
  return Math.max(-FLING_MAX, Math.min(FLING_MAX, v));
}

// the sun is the one thing a press MOVES rather than spins: everywhere else a
// drag turns the record, but grabbing yourself slides the whole sky around the
// screen. Radius mirrors the sun drawn in paint() (22 * k), with a small pad.
function onSun(clientX, clientY) {
  const r = S.canvas.getBoundingClientRect();
  const px = clientX - r.left, py = clientY - r.top;
  return Math.hypot(px - sx(0), py - sy(0)) <= 22 * S.cur.k + 4;
}

function sunAngle(px, py) {
  const r = S.canvas.getBoundingClientRect();
  return Math.atan2((py - r.top) - sy(0), (px - r.left) - sx(0));
}

function bindPointer() {
  const cv = S.canvas;
  cv.addEventListener("pointerdown", (e) => {
    cv.setPointerCapture(e.pointerId);
    S.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (S.pointers.size === 2) {
      const [a, b] = [...S.pointers.values()];
      S.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
      S.drag = null; return;
    }
    // a drag is a SPIN about the sun: remember the pointer's angle around
    // the sun's screen position and turn the sky by how much it changes
    S.drag = { x0: e.clientX, y0: e.clientY, moved: false, button: e.button,
      ang: sunAngle(e.clientX, e.clientY), samples: [],
      // ... unless the press landed ON the sun, which pans the view instead
      pan: onSun(e.clientX, e.clientY) ? { x: S.cam.x, y: S.cam.y } : null };
    S.spinV = 0;                            // a hand on the record stops it
  });
  cv.addEventListener("pointermove", (e) => {
    if (S.pointers.has(e.pointerId)) S.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (S.pinch && S.pointers.size === 2) {
      const [a, b] = [...S.pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      if (S.pinch.d > 0) zoomAt(mx, my, d / S.pinch.d);
      // zoom only: a pinch that also panned would walk the sun off centre
      S.pinch = { d, mx, my }; S.dirty = true;
      return;
    }
    if (S.drag) {
      const dx = e.clientX - S.drag.x0, dy = e.clientY - S.drag.y0;
      if (!S.drag.moved && Math.hypot(dx, dy) > 4) { S.drag.moved = true; S.follow = null; }
      if (S.drag.moved && S.drag.pan) {
        // the sky follows the hand exactly: cam AND cur, never eased, or the
        // view would lag behind the pointer that is dragging it
        const k = S.cur.k;
        S.cam.x = S.drag.pan.x - dx / k; S.cam.y = S.drag.pan.y - dy / k;
        S.cur.x = S.cam.x; S.cur.y = S.cam.y;
        S.dirty = true; hideTip();
        return;
      }
      if (S.drag.moved) {
        // incremental, wrapped: the pointer can circle the sun any number
        // of times and a jump across the -pi/pi seam must not flip the sky
        const a = sunAngle(e.clientX, e.clientY);
        let da = a - S.drag.ang; da = Math.atan2(Math.sin(da), Math.cos(da));
        S.spin += da; S.drag.ang = a;          // direct, never eased
        // keep a short trail of (time, angle delta) for the release velocity
        const now = performance.now(), sm = S.drag.samples;
        sm.push({ t: now, da });
        while (sm.length && now - sm[0].t > FLING_WINDOW_MS) sm.shift();
        S.dirty = true; hideTip();
      }
      return;
    }
    const n = hit(e.clientX, e.clientY);
    const want = n ? "pointer" : (onSun(e.clientX, e.clientY) ? "grab" : "");
    if (cv.style.cursor !== want) cv.style.cursor = want;
    if (n !== S.hover) { S.hover = n; S.dirty = true; }
    if (n) showTip(n, e.clientX, e.clientY); else hideTip();
  });
  const up = (e) => {
    S.pointers.delete(e.pointerId);
    if (S.pointers.size < 2) S.pinch = null;
    if (!S.drag) return;
    const d = S.drag; S.drag = null;
    if (d.moved && d.pan) return;              // a pan coasts nowhere
    if (d.moved) { S.spinV = flingVelocity(d.samples); if (S.spinV) wake(); return; }
    if (d.button !== 0) return;
    const n = hit(e.clientX, e.clientY);
    if (n) select(n, true); else clearSel();
  };
  cv.addEventListener("pointerup", up);
  cv.addEventListener("pointercancel", (e) => { S.pointers.delete(e.pointerId); S.pinch = null; S.drag = null; });
  cv.addEventListener("pointerleave", () => { S.hover = null; hideTip(); S.dirty = true; });
  cv.addEventListener("dblclick", (e) => { const n = hit(e.clientX, e.clientY); if (n) openPerson(n.id); });
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0016));
    S.follow = null;
  }, { passive: false });
  cv.addEventListener("contextmenu", (e) => {
    const n = hit(e.clientX, e.clientY);
    if (!n) return;
    e.preventDefault(); e.stopPropagation();
    showContextMenu(e.clientX, e.clientY, [
      { head: "Orbits: " + n.name },
      { label: "Open profile", run: () => openPerson(n.id) },
      { label: "Show their ties", run: () => select(n, true) },
      { label: "Ask Vira about " + firstName(n.name) + "...",
        run: () => ctxAskVira(e.clientX, e.clientY, { component: "Orbits", person: { id: n.id, name: n.name } }) },
    ]);
  });
  S.stage.tabIndex = 0;
  S.stage.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { clearSel(); S.stage.blur(); }
  });
}

// zoom toward a stage-local point (the cursor)
function zoomAt(px, py, f) {
  const k0 = S.cam.k, k1 = clamp(k0 * f, 0.15, 6);
  const wxp = (px - S.W / 2) / k0 + S.cam.x, wyp = (py - S.H / 2) / k0 + S.cam.y;
  S.cam.k = k1;
  S.cam.x = wxp - (px - S.W / 2) / k1; S.cam.y = wyp - (py - S.H / 2) / k1;
  S.dirty = true;
}

function hit(clientX, clientY) {
  const r = S.canvas.getBoundingClientRect();
  const px = clientX - r.left, py = clientY - r.top;
  let best = null, bestD = Infinity;
  for (const n of S.nodes) {
    const cx = sx(n.ax), cy = sy(n.ay);
    const sc = n === S.sel ? 1.6 : 1;
    const hw = n.w * S.cur.k * sc / 2 + 3, hh = n.h * S.cur.k * sc / 2 + 3;
    if (Math.abs(px - cx) <= hw && Math.abs(py - cy) <= hh) {
      const d = Math.hypot(px - cx, py - cy);
      if (d < bestD) { bestD = d; best = n; }
    }
  }
  return best;
}

function showTip(n, x, y) {
  const t = S.tipEl;
  t.style.display = ""; t.innerHTML = "";
  t.appendChild(dom("div", "orbits-tip-name", n.name));
  const sub = [n.title, n.company].filter(Boolean).join(" - ") || n.relationship_class || "";
  if (sub) t.appendChild(dom("div", "hint", sub));
  t.appendChild(dom("div", "orbits-tip-when", "last spoke " + agoText(n.days)));
  const r = S.stage.getBoundingClientRect();
  t.style.left = Math.min(r.width - 230, x - r.left + 14) + "px";
  t.style.top = Math.max(6, y - r.top - 48) + "px";
}
function hideTip() { S.tipEl.style.display = "none"; }

// ---------- selection ----------
function select(n, fly) {
  if (S.sel === n) return;
  S.sel = n;
  S.nb1 = new Set((S.adj.get(n.id) || []).map((t) => t.pid));
  renderCard(n);
  if (fly) flyTo(n, null, { clearCard: true });
  S.dirty = true;
}
function clearSel() {
  if (!S.sel) return;
  S.sel = null; S.nb1 = new Set(); S.follow = null;
  S.cardEl.style.display = "none";
  S.dirty = true;
}

// ---------- the panel ----------
async function renderCard(n) {
  const card = S.cardEl, seq = ++S.detailSeq;
  card.style.display = ""; card.innerHTML = "";
  const head = dom("div", "orbits-card-head");
  const av = avatarNode(n.id, n.name, true, n.face != null);
  if (n.face) av.querySelector("img")?.setAttribute("src", "/api/atlas/face/" + n.id);
  head.appendChild(av);
  const mid = dom("div", "orbits-card-name");
  const nm = dom("div", "click", n.name);
  nm.title = "Open the profile";
  nm.addEventListener("click", () => openPerson(n.id));
  mid.appendChild(nm);
  const sub = [n.title, n.company].filter(Boolean).join(" - ") || n.relationship_class || "";
  if (sub) mid.appendChild(dom("div", "hint", sub));
  head.appendChild(mid);
  const x = dom("button", "idea-del", "×");
  x.title = "Clear (Esc)"; x.addEventListener("click", clearSel);
  head.appendChild(x);
  card.appendChild(head);

  // the orbit line - the one fact this picture is built on
  const ring = ringOf(n.days);
  const orbit = dom("div", "orbits-orbit");
  orbit.appendChild(dom("span", "orbits-k", "orbit"));
  orbit.appendChild(dom("span", "orbits-ring", ring ? ring.label : "no dated contact"));
  orbit.appendChild(dom("span", "hint", n.days == null ? ""
    : `last spoke ${agoText(n.days)}${n.last ? " - " + n.last : ""}`));
  card.appendChild(orbit);

  const chips = dom("div", "orbits-chips");
  const w = S.wedges.find((x) => x.band === n.band);
  if (w) {
    const c = dom("span", "orbits-chip", w.label);
    c.style.borderColor = w.color; c.style.color = w.color;
    chips.appendChild(c);
  }
  const ego = S.egoW.get(n.id);
  if (ego) chips.appendChild(dom("span", "orbits-chip you", ego.signals.map((s) => s.detail).join(" - ")));
  card.appendChild(chips);

  const body = dom("div", "orbits-card-body");
  body.appendChild(dom("div", "hint", "Reading the record..."));
  card.appendChild(body);

  const ties = S.adj.get(n.id) || [];
  const list = dom("div", "orbits-ties");
  list.appendChild(dom("div", "orbits-h", ties.length
    ? `Tied to ${ties.length} ${ties.length === 1 ? "person" : "people"}`
    : "No ties in the graph - they connect only through you"));
  for (const t of ties.slice(0, 30)) {
    const o = S.byId.get(t.pid); if (!o) continue;
    const row = dom("div", "orbits-tie");
    const nameRow = dom("div", "orbits-tie-name", o.name);
    const when = dom("span", "orbits-tie-when", agoText(o.days));
    nameRow.appendChild(when);
    row.appendChild(nameRow);
    row.appendChild(dom("div", "orbits-tie-why",
      t.narrative || t.signals.map((s) => s.detail).filter(Boolean).join(" - ")));
    row.title = "Fly to " + firstName(o.name);
    row.addEventListener("click", () => select(o, true));
    list.appendChild(row);
  }
  if (ties.length > 30) list.appendChild(dom("div", "hint", `+${ties.length - 30} more`));
  card.appendChild(list);

  try {
    const d = await api("/api/person/" + n.id);
    if (seq !== S.detailSeq) return;
    body.innerHTML = "";
    const prof = d.profile || {};
    if (prof.relationship_summary) {
      const sents = String(prof.relationship_summary).split(/(?<=[.!?])\s+/).slice(0, 3).join(" ");
      body.appendChild(dom("p", "orbits-summary", sents));
    }
    const loops = (prof.open_loops || []).filter((l) => (l.status || "open") === "open");
    if (loops.length) {
      body.appendChild(dom("div", "orbits-h", `Open between you (${loops.length})`));
      for (const l of loops.slice(0, 3)) {
        const row = dom("div", "orbits-loop");
        row.appendChild(dom("span", "orbits-owe " + (l.owed_by === "me" ? "me" : "them"),
          l.owed_by === "me" ? "you owe" : "they owe"));
        row.appendChild(dom("span", null, l.what));
        body.appendChild(row);
      }
    }
    const hooks = prof.hooks || [];
    if (hooks.length) {
      body.appendChild(dom("div", "orbits-h", "Worth raising"));
      for (const h of hooks.slice(0, 2)) {
        const row = dom("div", "orbits-hook", h.angle || h.hook || h.text || "");
        if (h.detail) row.title = h.detail;
        body.appendChild(row);
      }
    }
    if (!body.childNodes.length) body.appendChild(dom("div", "hint", "No relationship read on file yet."));
    const open = dom("button", "fchip orbits-open", "Open profile");
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
  const search = document.getElementById("orbits-search");
  search.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    S.matches = q ? new Set(S.nodes.filter((n) => n.name.toLowerCase().includes(q)
      || (n.company || "").toLowerCase().includes(q)).map((n) => n.id)) : null;
    S.dirty = true;
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && S.matches && S.matches.size) {
      const first = S.nodes.find((n) => S.matches.has(n.id));
      if (first) select(first, true);
    }
    if (e.key === "Escape") { search.value = ""; search.dispatchEvent(new Event("input")); search.blur(); }
  });
  document.getElementById("orbits-home").addEventListener("click", () => { clearSel(); frameAll(false); });
  document.getElementById("orbits-refresh").addEventListener("click", () => load(true));
  const drift = document.getElementById("orbits-drift");
  const paint = () => { drift.classList.toggle("on", S.spinning); drift.textContent = S.spinning ? "Drifting" : "Still"; };
  drift.addEventListener("click", () => {
    S.spinning = !S.spinning; lsSet("vira-orbits-drift", S.spinning); paint(); S.dirty = true;
  });
  if (S.reduced) drift.style.display = "none";
  paint();
}

function renderLenses() {
  const box = document.getElementById("orbits-lenses");
  box.innerHTML = "";
  for (const l of (S.graph.lenses || [])) {
    const b = dom("button", "fchip" + (l.id === S.lens ? " on" : ""), l.label);
    b.title = l.blurb + (l.placed < l.total ? ` - ${l.placed} of ${l.total} placed` : "");
    b.addEventListener("click", () => setLens(l.id));
    box.appendChild(b);
  }
}
function setLens(id) {
  if (id === S.lens) return;
  S.lens = id; lsSet("vira-orbits-lens", id);
  renderLenses();
  layout(id, false);
  S.dirty = true;
}

// ---------- faces ----------
function faceFor(n) {
  if (!n.face) return null;
  let rec = S.imgs.get(n.id);
  if (!rec) {
    rec = { img: new Image(), ok: false };
    rec.img.onload = () => { rec.ok = true; S.dirty = true; };
    // a failed load costs the CANVAS face only - the panel loads its own
    // copy, so n.face stays set rather than dropping the avatar everywhere
    rec.img.onerror = () => { rec.ok = false; };
    rec.img.src = "/api/atlas/face/" + n.id;
    S.imgs.set(n.id, rec);
  }
  return rec.ok ? rec.img : null;
}

// ---------- drawing ----------
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
}

// a label written along an arc, flipped on the lower half so it reads
// left to right wherever the wedge sits
function textAlongArc(ctx, text, r, a0, a1, color) {
  const mid = (a0 + a1) / 2;
  const lower = Math.sin(mid) > 0;
  ctx.save();
  ctx.fillStyle = color;
  ctx.font = `600 ${Math.max(9, 10.5 * Math.min(1, S.cur.k))}px ${S.colors.mono}`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const chars = [...text.toUpperCase()];
  const widths = chars.map((c) => ctx.measureText(c).width + 1.6);
  const total = widths.reduce((s, w) => s + w, 0);
  const span = total / (r * S.cur.k);                 // radians the text covers
  if (span > (a1 - a0) * 1.15 && text.length > 6) { ctx.restore(); return false; }
  let a = lower ? mid + span / 2 : mid - span / 2;
  for (let i = 0; i < chars.length; i++) {
    const half = widths[i] / 2 / (r * S.cur.k);
    a += lower ? -half : half;
    const x = sx(Math.cos(a) * r), y = sy(Math.sin(a) * r);
    ctx.save(); ctx.translate(x, y); ctx.rotate(a + (lower ? -Math.PI / 2 : Math.PI / 2));
    ctx.fillText(chars[i], 0, 0); ctx.restore();
    a += lower ? -half : half;
  }
  ctx.restore();
  return true;
}

function draw() {
  const ctx = S.ctx, W = S.W, H = S.H, k = S.cur.k;
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const cx = sx(0), cy = sy(0);

  // the sky: a faint radial glow centred on you
  const grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, (R_OUT + 120) * k);
  grd.addColorStop(0, "rgba(207,203,194,0.07)");
  grd.addColorStop(0.35, "rgba(207,203,194,0.025)");
  grd.addColorStop(1, "rgba(207,203,194,0)");
  ctx.fillStyle = grd; ctx.fillRect(0, 0, W, H);

  // ring guides, labelled at the top
  ctx.lineWidth = 1;
  for (const rg of RINGS) {
    const r = radiusFor(rg.days) * k;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, TAU);
    ctx.strokeStyle = "rgba(207,203,194,0.10)"; ctx.stroke();
    if (k > 0.35) {
      ctx.fillStyle = S.colors.faint;
      ctx.font = `${Math.max(9, 10 * Math.min(1, k))}px ${S.colors.mono}`;
      ctx.textAlign = "center"; ctx.textBaseline = "bottom";
      ctx.fillText(rg.label.toUpperCase(), cx, cy - r - 3);
    }
  }
  // the rim of the known: past it, no dated contact
  ctx.setLineDash([3, 5]);
  ctx.beginPath(); ctx.arc(cx, cy, (R_OUT + 46) * k, 0, TAU);
  ctx.strokeStyle = "rgba(207,203,194,0.14)"; ctx.stroke();
  ctx.setLineDash([]);

  // wedge arcs + labels on the rim
  const rimR = R_OUT + 74;
  for (const w of S.wedges) {
    const a0 = w.a0 + S.spin, a1 = w.a1 + S.spin;
    ctx.beginPath(); ctx.arc(cx, cy, rimR * k, a0, a1);
    ctx.strokeStyle = w.color; ctx.globalAlpha = w.band === "__none" ? 0.35 : 0.8;
    ctx.lineWidth = 2; ctx.stroke(); ctx.globalAlpha = 1;
    const label = `${w.label} ${w.members.length}`;
    textAlongArc(ctx, label, rimR + 10, a0, a1, w.band === "__none" ? S.colors.faint : w.color);
  }
  ctx.lineWidth = 1;

  // chords: at rest only the strong ties, faint; a selection lights its own
  const selId = S.sel?.id;
  const drawChord = (a, b, alpha, width, color) => {
    const ax = sx(a.ax), ay = sy(a.ay), bx = sx(b.ax), by = sy(b.ay);
    const mx = (a.ax + b.ax) / 2 * 0.28, my = (a.ay + b.ay) / 2 * 0.28;   // bent toward you
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.quadraticCurveTo(sx(mx), sy(my), bx, by);
    ctx.strokeStyle = color; ctx.globalAlpha = alpha; ctx.lineWidth = width; ctx.stroke();
  };
  if (!selId) {
    for (const e of S.edges) {
      if (e.weight < TIE_FLOOR) continue;
      const a = S.byId.get(e.a), b = S.byId.get(e.b);
      const lit = S.hover && (S.hover.id === e.a || S.hover.id === e.b);
      drawChord(a, b, lit ? 0.7 : 0.05 + Math.min(0.08, (e.weight - TIE_FLOOR) * 0.05),
        lit ? 1.4 : 0.8, lit ? S.colors.warm : S.colors.text);
    }
  } else {
    for (const e of S.edges) {
      if (e.a !== selId && e.b !== selId) continue;
      const a = S.byId.get(e.a), b = S.byId.get(e.b);
      drawChord(a, b, 0.35 + Math.min(0.55, e.weight * 0.3), 1 + Math.min(2, e.weight), S.colors.warm);
    }
    // and the line home
    const s = S.sel;
    ctx.beginPath(); ctx.moveTo(sx(s.ax), sy(s.ay)); ctx.lineTo(cx, cy);
    ctx.strokeStyle = S.colors.amber; ctx.globalAlpha = 0.6; ctx.lineWidth = 1.2; ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // the sun - you
  const sunR = 22 * k;
  const sg = ctx.createRadialGradient(cx, cy, sunR * 0.4, cx, cy, sunR * 3.2);
  sg.addColorStop(0, "rgba(212,208,198,0.55)"); sg.addColorStop(1, "rgba(212,208,198,0)");
  ctx.fillStyle = sg; ctx.beginPath(); ctx.arc(cx, cy, sunR * 3.2, 0, TAU); ctx.fill();
  ctx.fillStyle = S.colors.head; ctx.beginPath(); ctx.arc(cx, cy, sunR, 0, TAU); ctx.fill();
  ctx.fillStyle = S.colors.bg;
  ctx.font = `600 ${Math.max(8, 10 * Math.min(1.4, k))}px ${S.colors.mono}`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText("YOU", cx, cy + 1);

  // cards, far to near (the selected one last so it sits on top)
  const order = [...S.nodes].sort((a, b) => (a === S.sel) - (b === S.sel) || a.w - b.w);
  const showNames = k > 1.35;
  for (const n of order) {
    const isSel = n === S.sel, isNb = S.nb1.has(n.id), isHover = n === S.hover;
    let alpha = 1;
    if (selId) alpha = isSel || isNb ? 1 : 0.14;
    if (S.matches) alpha = S.matches.has(n.id) ? 1 : Math.min(alpha, 0.12);
    const sc = isSel ? 1.6 : isHover ? 1.12 : 1;
    const w = n.w * k * sc, h = n.h * k * sc;
    const x = sx(n.ax) - w / 2, y = sy(n.ay) - h / 2;
    if (x + w < -20 || x > W + 20 || y + h < -20 || y > H + 20) continue;
    ctx.globalAlpha = alpha;
    if (isSel) {
      ctx.shadowColor = "rgba(217,119,87,0.55)"; ctx.shadowBlur = 24 * k;
    } else { ctx.shadowColor = "rgba(0,0,0,0.6)"; ctx.shadowBlur = 6 * k; ctx.shadowOffsetY = 2 * k; }
    ctx.fillStyle = n.color;
    roundRect(ctx, x, y, w, h, Math.max(2, 3 * k)); ctx.fill();
    ctx.shadowColor = "transparent"; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;
    const img = faceFor(n);
    ctx.save();
    roundRect(ctx, x, y, w, h, Math.max(2, 3 * k)); ctx.clip();
    if (img) {
      const s = Math.max(w / img.width, h / img.height);
      const iw = img.width * s, ih = img.height * s;
      ctx.drawImage(img, x + (w - iw) / 2, y + (h - ih) / 2, iw, ih);
    } else {
      ctx.fillStyle = "rgba(0,0,0,0.28)"; ctx.fillRect(x, y, w, h);
      ctx.fillStyle = "#e8e3d9";
      ctx.font = `600 ${Math.max(7, w * 0.38)}px ${S.colors.sans}`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(initialsOf(n.name), x + w / 2, y + h / 2 + 1);
    }
    ctx.restore();
    // the band rim
    ctx.strokeStyle = isSel ? S.colors.amber : n.color; ctx.lineWidth = isSel ? 2 : 1.2;
    roundRect(ctx, x, y, w, h, Math.max(2, 3 * k)); ctx.stroke();
    if (showNames || isSel || isHover || (selId && isNb)) {
      ctx.fillStyle = isSel ? S.colors.head : S.colors.text;
      ctx.font = `${isSel ? 600 : 400} ${Math.max(9, Math.min(13, 10.5 * k))}px ${S.colors.sans}`;
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      ctx.fillText(firstName(n.name) + (isSel || isHover ? " " + (n.name.split(" ").slice(1).join(" ")) : ""),
        x + w / 2, y + h + 3);
    }
    ctx.globalAlpha = 1;
  }
}

// ---------- loop ----------
function wake() { if (S.running) return; S.running = true; S.lastT = performance.now(); requestAnimationFrame(frame); }
function sleep() { S.running = false; }

function frame(t) {
  if (!S.running) return;
  const dt = Math.min(0.05, (t - S.lastT) / 1000); S.lastT = t;
  let moving = false;
  // the sky turns, unless a hand is on it
  if (S.spinning && !S.drag && !S.hover && !S.pinch) { S.spin += SPIN * dt; moving = true; }
  // the coast after a flung drag: spin on at the release velocity, winding
  // down exponentially until it is too slow to see
  if (S.spinV && !S.drag && !S.pinch) {
    S.spin += S.spinV * dt;
    S.spinV *= Math.exp(-FLING_DECAY * dt);
    if (Math.abs(S.spinV) < FLING_MIN) S.spinV = 0;
    moving = true;
  }
  if (S.migration) {
    const p = Math.min(1, (t - S.migration.t0) / S.migration.ms), e = easeInOut(p);
    for (const n of S.nodes) {
      if (!n._mig) continue;
      let da = n._mig.a1 - n._mig.a0; da = Math.atan2(Math.sin(da), Math.cos(da));
      n.a = n._mig.a0 + da * e; n.rr = lerp(n._mig.r0, n._mig.r1, e);
    }
    if (p >= 1) { S.migration = null; for (const n of S.nodes) n._mig = null; }
    moving = true;
  }
  if (moving || S.dirty) placeAll();
  if (S.follow) {              // stay centred on the card while the sky turns
    const dx = S.cardEl.style.display !== "none" ? (S.cardEl.offsetWidth / 2) / S.cam.k : 0;
    S.cam.x = S.follow.ax + dx; S.cam.y = S.follow.ay;
  }
  const kk = S.reduced ? 1 : 1 - Math.exp(-8 * dt);
  for (const key of ["x", "y", "k"]) {
    const d = S.cam[key] - S.cur[key];
    if (Math.abs(d) > 1e-4 * (key === "k" ? 1 : S.cur.k)) { S.cur[key] += d * kk; moving = true; } else S.cur[key] = S.cam[key];
  }
  if (moving || S.dirty) { draw(); S.dirty = false; }
  requestAnimationFrame(frame);
}

window.__orbits = {
  state: () => ({ cam: { ...S.cam }, cur: { ...S.cur }, spin: S.spin, spinV: S.spinV, spinning: S.spinning,
    sel: S.sel?.id || null, lens: S.lens, nodes: S.nodes.length,
    wedges: S.wedges.map((w) => ({ band: w.band, label: w.label, n: w.members.length, a0: w.a0, a1: w.a1 })),
    rings: S.nodes.reduce((m, n) => { const r = ringOf(n.days)?.label || "undated"; m[r] = (m[r] || 0) + 1; return m; }, {}) }),
  select: (pid) => { const n = S.byId.get(pid); if (n) select(n, true); },
  setLens, clear: clearSel, frameAll,
};
window.orbitsLoad = load;

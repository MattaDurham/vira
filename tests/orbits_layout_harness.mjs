// The Orbits geometry, driven for real: this imports the shipped
// static/orbits.js (a window stub is all it needs at import) and asserts the
// two rules of 2026-09-10 as measurements, not as source strings.
//
//  1. THE SUN NEVER LEAVES THE STAGE - clampToSun keeps its centre a
//     grab-sized pad inside every edge for any camera, and sunZoomCap yields
//     a zoom at which centring a card still leaves the sun inside the pad.
//  2. CARDS OVERLAP BUT NEVER BURY EACH OTHER - after arrange(), every pair
//     keeps minSep, and a rasterized paint at several spins finds a clear
//     strip on every card, because the separation is a centre DISTANCE and
//     the cards are screen-aligned boxes under a turning sky.
//
// Run by tests/test_orbits_view.py; exits non-zero on the first failure.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

globalThis.window = globalThis;
const here = path.dirname(new URL(import.meta.url).pathname);
const mod = await import(pathToFileURL(path.join(here, "..", "static", "orbits.js")));
const { cardFor, arrange, minSep, relaxCards, sunBox, clampToSun, sunZoomCap } = mod;
for (const f of [cardFor, arrange, minSep, relaxCards, sunBox, clampToSun, sunZoomCap])
  assert.equal(typeof f, "function", "orbits.js must export its geometry seams");

// deterministic pseudo-random, so a failure reproduces
let seed = 7;
const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };

// ---------- 1. the sun ----------
const sunAt = (cam, W, H) => ({ x: W / 2 - cam.x * cam.k, y: H / 2 - cam.y * cam.k });
for (const [W, H] of [[1280, 800], [402, 640], [3440, 1400], [200, 120]]) {
  for (let i = 0; i < 400; i++) {
    const k = 0.15 + rnd() * 5.85;
    const cam = { x: (rnd() - 0.5) * 8000, y: (rnd() - 0.5) * 8000, k };
    clampToSun(cam, W, H);
    const s = sunAt(cam, W, H), b = sunBox(k, W, H);
    const pad = Math.max(16, Math.min(48, 22 * k + 6));   // sunPad, restated
    if (W / 2 - pad > 0) {
      assert.ok(s.x >= pad - 1e-6 && s.x <= W - pad + 1e-6, `sun x ${s.x} outside pad ${pad} on ${W}x${H} k ${k}`);
      assert.ok(s.y >= pad - 1e-6 && s.y <= H - pad + 1e-6, `sun y ${s.y} outside pad ${pad} on ${W}x${H} k ${k}`);
      assert.ok(Math.abs(cam.x) <= b.x + 1e-9 && Math.abs(cam.y) <= b.y + 1e-9);
    } else {
      assert.equal(cam.x, 0); assert.equal(cam.y, 0);      // a stage too small for a pad pins the sun dead centre
    }
    // a camera already inside is left alone: the clamp is a bound, not a pull
    const inside = { x: b.x * 0.5, y: -b.y * 0.5, k };
    assert.equal(clampToSun(inside, W, H), false);
    assert.equal(inside.x, b.x * 0.5);
  }
}
// the zoom cap: centring a card at the capped zoom keeps the sun in its pad
// with no help from the clamp, wherever the card's own landing point lies
// outside the pad zone (a panel so wide that the card lands inside the pad
// is the clamp's case - no zoom can fix a landing point). Side-docked and
// bottom-docked panels alike, at the widths cardShift() really produces.
for (const [W, H] of [[1280, 800], [402, 640], [560, 420]]) {
  const side = { x: Math.min(330, 0.46 * W) / 2, y: 0 }, bottom = { x: 0, y: 0.55 * H / 2 };
  for (const shift of [{ x: 0, y: 0 }, side, bottom]) {
    for (let i = 0; i < 300; i++) {
      const a = rnd() * Math.PI * 2, r = 30 + rnd() * 600;
      const node = { ax: Math.cos(a) * r, ay: Math.sin(a) * r };
      let k = 2.2;
      for (let p = 0; p < 2; p++) k = Math.min(k, sunZoomCap(node, shift, k, W, H));
      k = Math.max(0.15, Math.min(6, k));
      const cam = { x: node.ax + shift.x / k, y: node.ay + shift.y / k, k };
      const pad = Math.max(16, Math.min(48, 22 * k + 6));
      const cx = W / 2 - shift.x, cy = H / 2 - shift.y;
      const landsClear = cx >= pad && cx <= W - pad && cy >= pad && cy <= H - pad;
      const inPad = (s) => s.x >= pad - 1e-6 && s.x <= W - pad + 1e-6 && s.y >= pad - 1e-6 && s.y <= H - pad + 1e-6;
      const before = sunAt(cam, W, H);            // the cap's own placement, unclamped
      if (landsClear && k > 0.15)
        assert.ok(inPad(before), `the cap alone should have held the sun: k ${k} sun ${before.x},${before.y} pad ${pad} card ${node.ax},${node.ay} shift ${shift.x},${shift.y} on ${W}x${H}`);
      clampToSun(cam, W, H);                      // and the backstop covers the rest
      const after = sunAt(cam, W, H);
      assert.ok(inPad(after), `sun at ${after.x},${after.y} outside pad ${pad} on ${W}x${H}`);
    }
  }
}
// and the cap only ever LOWERS the zoom: a card on the today ring keeps 2.2
{
  const near = { ax: 60, ay: -40 };
  assert.ok(sunZoomCap(near, { x: 165, y: 0 }, 2.2, 1280, 800) >= 2.2);
  const rim = { ax: 606, ay: 0 };
  assert.ok(sunZoomCap(rim, { x: 165, y: 0 }, 2.2, 1280, 800) < 1);
}

// ---------- 2. the cards ----------
// the separation really is enough at EVERY rotation of the offset
for (let i = 0; i < 2000; i++) {
  const a = { w: 22 + rnd() * 22 }, b = { w: 22 + rnd() * 22 };
  a.h = a.w * 1.22; b.h = b.w * 1.22;
  const sm = a.w <= b.w ? a : b, lg = sm === a ? b : a;
  const E = Math.max(14, 0.55 * sm.w);
  const D = minSep(a, b), th = rnd() * Math.PI * 2;
  const dx = Math.abs(Math.cos(th) * D), dy = Math.abs(Math.sin(th) * D);
  const ex = dx - (lg.w - sm.w) / 2, ey = dy - (lg.h - sm.h) / 2;
  assert.ok(Math.max(ex, ey) >= E - 1e-9, `offset ${D} at ${th} exposes only ${Math.max(ex, ey)} of ${E}`);
}

// a synthetic pile: many contacts on the same few days, three bands
function pile(n, sameDay) {
  const nodes = [];
  for (let i = 0; i < n; i++) {
    const days = sameDay ? [0, 1, 3][i % 3] : Math.floor(rnd() * 1500);
    const last = new Date(Date.now() - days * 86400000).toISOString();
    nodes.push({ id: "p" + i, name: "P " + i, act: Math.floor(rnd() * 400), last });
  }
  const bands = [{ id: "b0", label: "b0" }, { id: "b1", label: "b1" }];
  const node_band = {}; nodes.forEach((x, i) => { if (i % 5) node_band[x.id] = "b" + (i % 2); });
  return { nodes, bands, node_band };
}
// paint the cards small-under-large at zoom K for each spin and read back,
// per card, the worst case over spins of (a) the fraction of its face left
// clear and (b) the widest clear run along a row - the click target
function exposure(cards, K, spins) {
  const order = [...cards].sort((a, b) => a.w - b.w);      // the paint order, small under large
  const W = 1500 * K, H = W, worst = new Map(cards.map((c) => [c, { frac: 1, run: Infinity }]));
  for (const sp of spins) {
    const grid = new Int16Array(W * H).fill(-1), box = new Map();
    order.forEach((c, i) => {
      const cx = W / 2 + Math.cos(c.ta + sp) * c.tr * K, cy = H / 2 + Math.sin(c.ta + sp) * c.tr * K;
      const b = [Math.round(cx - c.w * K / 2), Math.round(cy - c.h * K / 2), Math.round(cx + c.w * K / 2), Math.round(cy + c.h * K / 2)];
      box.set(c, b);
      for (let y = b[1]; y < b[3]; y++) for (let x = b[0]; x < b[2]; x++) grid[y * W + x] = i;
    });
    order.forEach((c, i) => {
      const b = box.get(c); let own = 0, best = 0;
      for (let y = b[1]; y < b[3]; y++) {
        let run = 0;
        for (let x = b[0]; x < b[2]; x++) { if (grid[y * W + x] === i) { own++; run++; best = Math.max(best, run); } else run = 0; }
      }
      const r = worst.get(c);
      r.frac = Math.min(r.frac, own / ((b[2] - b[0]) * (b[3] - b[1]))); r.run = Math.min(r.run, best);
    });
  }
  return worst;
}
for (const [n, sameDay] of [[60, true], [200, false], [120, true]]) {
  const { nodes, bands, node_band } = pile(n, sameDay);
  const maxAct = Math.max(1, ...nodes.map((x) => x.act));
  const cards = nodes.map((x) => cardFor(x, maxAct));
  const wedges = arrange(cards, bands, node_band);
  assert.equal(wedges.reduce((s, w) => s + w.members.length, 0), n);
  // every pair keeps its separation - the guarantee, whatever it cost
  const pt = cards.map((c) => [Math.cos(c.ta) * c.tr, Math.sin(c.ta) * c.tr]);
  let viol = 0;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
    const d = Math.hypot(pt[i][0] - pt[j][0], pt[i][1] - pt[j][1]);
    if (d < minSep(cards[i], cards[j]) - 1e-6) viol++;
  }
  assert.equal(viol, 0, `${viol} pairs still closer than minSep in a pile of ${n}`);
  assert.equal(wedges.relax.unresolved, 0);
  // ... within the bounds where the pile allows it: a card leaves its time
  // radius by at most the round's slack (R_SLACK in round one - the 60-card
  // and random piles settle there; the 120-on-three-days pile cannot, and
  // widens rather than bury anyone), and its wedge by at most A_SLACK
  // slots (the gap is 2.2, so never onto a neighbouring wedge's arc)
  const { round, rSlack } = wedges.relax;
  if (n !== 120) assert.equal(round, 1, `a pile of ${n} should settle in round one`);
  assert.ok(rSlack >= 26 && rSlack <= 26 * 1.5 ** 3 + 1e-9);
  const slots = wedges.reduce((s, w) => s + w.members.length + 2.2, 0), per = Math.PI * 2 / slots;
  const wrap = (d) => Math.atan2(Math.sin(d), Math.cos(d));
  for (const w of wedges) for (const c of w.members) {
    assert.ok(Math.abs(c.tr - c.r) <= rSlack + 1e-6, `card left its time radius by ${Math.abs(c.tr - c.r)} (slack ${rSlack})`);
    const mid = (w.a0 + w.a1) / 2, off = wrap(c.ta - mid);
    assert.ok(off >= (w.a0 - mid) - per * 0.9 - 1e-6 && off <= (w.a1 - mid) + per * 0.9 + 1e-6, "card drifted past its wedge slack");
  }
  // and painted, under a turning sky, no card is buried: a clear strip on
  // each at every spin sampled
  const worst = exposure(cards, 1, [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]);
  const vals = [...worst.values()];
  const buried = vals.filter((v) => v.frac < 0.05).length;
  assert.equal(buried, 0, `${buried} cards under 5% exposed in a pile of ${n}`);
  const thin = vals.filter((v) => v.run < 8).length;     // narrower than 8px at zoom 1 is not a click target
  assert.equal(thin, 0, `${thin} cards with no 8px clear strip in a pile of ${n}`);
}
// the deal alone (no relaxation) DOES pile cards closer than minSep and
// DOES bury some on a dense same-day pile - so the pass above is
// load-bearing, not vacuous
{
  const { nodes, bands, node_band } = pile(120, true);
  const maxAct = Math.max(1, ...nodes.map((x) => x.act));
  const cards = nodes.map((x) => cardFor(x, maxAct));
  const wedges = arrange(cards, bands, node_band);
  // replay the raw slot deal: a wedge member's slot centre, no relaxation
  const slots = wedges.reduce((s, w) => s + w.members.length + 2.2, 0), per = Math.PI * 2 / slots;
  for (const w of wedges) w.members.forEach((c, i) => { c.ta = w.a0 + per * (i + 0.5); c.tr = c.r + ((i % 3) - 1) * 9; });
  let viol = 0;
  const pt = cards.map((c) => [Math.cos(c.ta) * c.tr, Math.sin(c.ta) * c.tr]);
  for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++)
    if (Math.hypot(pt[i][0] - pt[j][0], pt[i][1] - pt[j][1]) < minSep(cards[i], cards[j]) - 1e-6) viol++;
  assert.ok(viol > 0, "the unrelaxed deal was expected to break minSep on a same-day pile");
  const worst = exposure(cards, 1, [0, 1.0, 2.0]);
  assert.ok([...worst.values()].some((v) => v.run < 8 || v.frac < 0.05), "the unrelaxed deal was expected to bury cards on a same-day pile");
}
console.log("orbits layout harness: ok");

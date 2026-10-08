// Exercise the real load/poll path with a small DOM and synthetic API replies.
import assert from "node:assert/strict";

class Element {
  style = {}; classList = { toggle() {} }; children = [];
  clientWidth = 1100; clientHeight = 700; hidden = false;
  addEventListener() {}
  appendChild(child) { this.children.push(child); }
  getContext() { return {}; }
}
const elements = new Map();
globalThis.window = globalThis;
globalThis.document = {
  hidden: false, documentElement: {}, addEventListener() {},
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  createElement() { return new Element(); },
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
globalThis.lsGet = (key, fallback) => fallback;
globalThis.lsSet = () => {};
globalThis.ResizeObserver = class { observe() {} };
globalThis.IntersectionObserver = class { observe() {} };
globalThis.requestAnimationFrame = () => {};
const polls = [];
globalThis.startPoll = (tick) => { polls.push(tick); return { stop() {} }; };
let now = Date.parse("2026-01-02T00:00:00Z");
Date.now = () => now;
let requests = 0;
let reply = { status: "empty", building: true };
globalThis.api = async () => { requests++; if (reply instanceof Error) throw reply; return reply; };
const { load } = await import(new URL("../static/orbits.js", import.meta.url));
const graph = (count) => ({
  status: "ok", generated: "same-second", building: false, stale: false,
  nodes: Array.from({ length: count }, (_, i) => ({
    id: `p_${i}`, name: `Example ${i}`, act: i + 1, last: "2026-01-01",
  })), edges: [], ego_edges: [], lenses: [],
});

await load();
assert.equal(polls.length, 1);
assert.match(elements.get("orbits-empty").textContent, /Building/);
reply = graph(2);
now += 4000;
await polls[0]();
assert.equal(window.__orbits.state().nodes, 2, "a fresh install finishes without manual reload");
assert.equal(elements.get("orbits-empty").style.display, "none");

reply = graph(3);
await load();
assert.equal(window.__orbits.state().nodes, 3, "reopening fetches the reconnected CRM");
const before = window.__orbits.state().cam;
now += 30000;
await polls[0]();
assert.deepEqual(window.__orbits.state().cam, before, "unchanged polls keep the camera");

reply = { ...graph(3), stale: true, building: true };
now += 30000;
await polls[0]();
assert.match(elements.get("orbits-empty").textContent, /Updating/);
reply = graph(4);
now += 4000;
await polls[0]();
assert.equal(window.__orbits.state().nodes, 4, "a stale existing graph follows the rebuild");

const fetched = requests;
document.hidden = true;
now += 30000;
await polls[0]();
assert.equal(requests, fetched);
document.hidden = false;
elements.get("orbits-stage").hidden = true;
await polls[0]();
assert.equal(requests, fetched, "hidden Orbits do not poll");
elements.get("orbits-stage").hidden = false;
reply = new Error("offline");
await polls[0]();
assert.match(elements.get("orbits-empty").textContent, /unavailable/);
reply = graph(5);
now += 30000;
await polls[0]();
assert.equal(window.__orbits.state().nodes, 5, "temporary network failures recover");
console.log("orbits refresh harness: ok");

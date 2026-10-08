// Run the actual scene controller against a small DOM and deterministic frame clock.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(fn); }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  emit(type, extra = {}) { for (const fn of this.listeners.get(type) || []) fn({ target: this, ...extra }); }
}
let draws = 0, poses = [];
const context = { clearRect() {}, save() {}, restore() {}, rotate() {},
  transform() {}, scale() {},
  translate(x, y) { poses.push([x, y]); }, beginPath() {}, ellipse() {}, fill() {},
  stroke() {}, arc() {}, drawImage() { draws++; } };
class Element extends Target {
  constructor(tag) { super(); this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.style = { setProperty() {} }; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append(...children) { children.forEach(c => this.appendChild(c)); }
  prepend(c) { c.parent = this; this.children.unshift(c); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  contains(c) { return c === this || this.children.some(n => n.contains(c)); }
  closest() { return this.tagName === "button" || this.control ? this : null; }
  focus() {}
  matches(s) {
    if (s.startsWith(".")) return this.className === s.slice(1);
    if (s === "[data-scene]") return !!this.dataset.scene;
    if (s.startsWith("[data-scene=")) return this.dataset.scene === s.split('"')[1];
    return this.tagName === s;
  }
  querySelectorAll(s) { return this.children.flatMap(c => [...(c.matches(s) ? [c] : []), ...c.querySelectorAll(s)]); }
  querySelector(s) { return this.querySelectorAll(s)[0]; }
  set innerHTML(html) {
    // Only the fixed picker skeleton needs parsing in this harness.
    for (const cls of ["background-options", "background-simple", "background-motion", "background-feed", "background-status", "background-close"]) {
      const c = new Element(cls.includes("motion") || cls.includes("close") ? "button" : "div");
      c.className = cls; this.appendChild(c);
    }
    this.appendChild(new Element("input"));
  }
  getContext(kind) { return kind === "2d" ? context : null; }
}
const document = new Target(); document.body = new Element("body"); document.hidden = false;
document.createElement = tag => new Element(tag);
const trigger = new Element("button"); trigger.id = "background-btn"; document.body.appendChild(trigger);
document.getElementById = id => id === trigger.id ? trigger : undefined;
const reduced = new Target(); reduced.matches = false;
const windowEvents = new Target(), frames = new Map(), pendingImages = new Map();
let nextFrame = 0, clock = 0, stored, stops = 0, constellations = 0;
class Image {
  constructor() { this.width = 1600; this.height = 900; }
  set src(src) { pendingImages.set(src.split("/").pop(), this); }
}
const sandbox = { window: { ViraKoiPond: require("../static/koi-pond.js") }, document, matchMedia: () => reduced, Image,
  innerWidth: 1280, innerHeight: 720, devicePixelRatio: 1,
  requestAnimationFrame: fn => { frames.set(++nextFrame, fn); return nextFrame; },
  cancelAnimationFrame: id => frames.delete(id),
  addEventListener: (...args) => windowEvents.addEventListener(...args),
  removeEventListener: (...args) => windowEvents.removeEventListener(...args), console };
vm.runInNewContext(fs.readFileSync("static/desktop-backgrounds.js", "utf8"), sandbox);
const flush = () => new Promise(resolve => setImmediate(resolve));
const resolve = name => { const image = pendingImages.get(name); assert.ok(image, name); image.onload(); };
const panel = () => document.body.children.find(c => c.id === "background-picker");
const scene = id => panel().querySelector(`[data-scene="${id}"]`).emit("click");
const host = () => document.body.children.find(c => c.id === "desktop-background");
const frame = () => { clock += 40; const work = [...frames.values()]; frames.clear(); work.forEach(fn => fn(clock)); };
(async () => {
  sandbox.window.ViraBackgrounds.init({ read: () => ({ scene: "unknown", dim: 100 }),
    canFeed: target => target === document.body,
    write: (key, value) => { assert.equal(key, "vira-background"); stored = { ...value }; },
    constellation: () => { constellations++; return () => { stops++; }; } });
  assert.equal(constellations, 1); assert.equal(pendingImages.size, 0);
  scene("koi"); scene("redwoods");
  assert.equal(stops, 1);
  resolve("pond-perspective.jpg"); resolve("kohaku.webp"); resolve("ogon.webp");
  resolve("showa.webp"); resolve("shusui.webp"); await flush();
  assert.equal(host().children.length, 0, "a stale load must not install its animation");
  resolve("redwoods.jpg"); await flush();
  assert.equal(host().children.length, 1); assert.equal(frames.size, 1);
  assert.match(panel().querySelector(".background-status").textContent, /effects unavailable/);
  scene("koi"); await flush();
  assert.equal(frames.size, 1, "switching must dispose the old loop");
  assert.equal(document.listeners.get("visibilitychange").size, 1);
  assert.equal(windowEvents.listeners.get("resize").size, 1);
  frame(); frame(); assert.ok(draws > 0);
  const rendered = draws, motion = panel().querySelector(".background-motion");
  const before = poses.at(-1);
  motion.emit("click"); assert.equal(frames.size, 0); assert.equal(stored.paused, true);
  frame(); assert.equal(draws, rendered, "pause must stop painting");
  motion.emit("click"); frame();
  assert.deepEqual(poses.at(-1), before, "resume must retain the paused simulation time");
  assert.equal(frames.size, 1);
  let prevented = false;
  const click = { button: 0, clientX: 640, clientY: 360,
    preventDefault() { prevented = true; }, defaultPrevented: false };
  const prior = draws;
  document.emit("click", { ...click, target: trigger });
  assert.equal(draws, prior, "window controls must not feed or repaint the pond");
  document.emit("click", { ...click, target: document.body });
  assert.equal(prevented, true); assert.ok(draws > prior, "food and ripples paint immediately");
  prevented = false;
  document.emit("dblclick", { ...click, target: document.body });
  assert.equal(prevented, true, "rapid feeding must protect the desktop's double-click gesture");
  motion.emit("click");
  const pausedDraws = draws;
  document.emit("click", { ...click, target: document.body });
  assert.equal(draws, pausedDraws, "feeding must respect paused motion");
  motion.emit("click");
  document.hidden = true; document.emit("visibilitychange"); assert.equal(frames.size, 0);
  document.hidden = false; document.emit("visibilitychange"); assert.equal(frames.size, 1);
  reduced.matches = true; reduced.emit("change"); assert.equal(frames.size, 0);
  assert.equal(motion.disabled, true); assert.ok(host(), "reduced motion retains the scene");
  reduced.matches = false; reduced.emit("change"); assert.equal(frames.size, 1);
  const range = panel().querySelector("input"); range.value = "35"; range.emit("input");
  assert.equal(stored.dim, .35);
  scene("none"); assert.equal(host(), undefined); assert.equal(frames.size, 0);
  assert.equal(document.listeners.get("visibilitychange").size, 0);
  assert.equal(windowEvents.listeners.get("resize").size, 0);
  assert.equal(document.listeners.get("click").size, 0);
  assert.equal(document.listeners.get("dblclick").size, 0);
  scene("aurora"); pendingImages.get("aurora.jpg").onerror(); await flush();
  assert.match(panel().querySelector(".background-status").textContent, /Could not load/);
  scene("aurora"); resolve("aurora.jpg"); await flush(); assert.equal(frames.size, 1);
  scene("none");
  console.log("Scene races, cleanup, pause/resume, hidden tabs, reduced motion and load failures pass.");
})().catch(error => { console.error(error); process.exitCode = 1; });

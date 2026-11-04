"use strict";

// History (static/history.js), run for real in a small fake DOM with fetch
// stubbed: the pure pieces (periods, zoom fitting, packing, missed slots,
// day counts), then the Time map end to end - lanes, session bars split
// into active and open time, merge diamonds, automation ticks with failures
// and missed runs, layers, the pinned unfinished work, selecting a day by
// pointer, Group by, the inline trail, the detail and its confirmed Merge,
// the plain-words question with its chips, the rule filter, the narrow
// day strip - and the History view of a module and of an idea. app.js's
// `api` and `post` are consts, so nothing here stubs them: every request
// goes through the stubbed fetch, and each one is recorded.
// Run by tests/test_history_ui.py.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// ---------- a fake DOM: just what history.js touches ----------
class ClassList {
  constructor(node) { this.node = node; }
  get list() { return String(this.node.className || "").split(/\s+/).filter(Boolean); }
  contains(c) { return this.list.includes(c); }
  add(...cs) { cs.forEach((c) => { if (!this.contains(c)) this.node.className = [...this.list, c].join(" "); }); }
  remove(...cs) { this.node.className = this.list.filter((x) => !cs.includes(x)).join(" "); }
  toggle(c, on) { const want = on === undefined ? !this.contains(c) : !!on; if (want) this.add(c); else this.remove(c); return want; }
}
let docRoot = null;
class Node {
  constructor(tag, text) {
    this.tagName = String(tag || "").toUpperCase();
    this.className = "";
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.listeners = {};
    this.style = { setProperty(k, v) { this[k] = v; } };
    this.classList = new ClassList(this);
    this._text = text == null ? "" : String(text);
    this.parentNode = null;
    this.hidden = false;
    this.value = "";
    this.disabled = false;
    this.clientWidth = 0;
  }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === docRoot; }
  get offsetParent() { return this.isConnected ? {} : null; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "class") this.className = String(v);
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g, (_, x) => x.toUpperCase())] = String(v);
  }
  getAttribute(k) {
    if (k === "class") return this.className;
    if (k in this.attrs) return this.attrs[k];
    // like a browser, dataset is the data-* attributes
    if (k.startsWith("data-")) { const v = this.dataset[k.slice(5).replace(/-(\w)/g, (_, x) => x.toUpperCase())]; return v == null ? null : String(v); }
    return null;
  }
  removeAttribute(k) { delete this.attrs[k]; }
  _adopt(c) { if (!(c instanceof Node)) c = new Node("#text", String(c)); if (c.parentNode) c.remove(); c.parentNode = this; return c; }
  append(...cs) { cs.forEach((c) => this.children.push(this._adopt(c))); }
  appendChild(c) { this.append(c); return c; }
  insertBefore(c, ref) { const i = this.children.indexOf(ref); const n = this._adopt(c); if (i < 0) this.children.push(n); else this.children.splice(i, 0, n); return c; }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter((x) => x !== this); this.parentNode = null; } }
  replaceChildren(...cs) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this.append(...cs); }
  addEventListener(t, cb) { (this.listeners[t] ||= []).push(cb); }
  fire(t, ev = {}) { (this.listeners[t] || []).forEach((cb) => cb(Object.assign({ stopPropagation() {}, preventDefault() {}, target: this }, ev))); }
  click() { this.fire("click"); }
  // events bubble to the ancestors that listen, like a browser
  dispatch(t, ev = {}) {
    const e = Object.assign({ stopPropagation() { this._stop = true; }, preventDefault() {}, target: this }, ev);
    for (let n = this; n && !e._stop; n = n.parentNode) (n.listeners[t] || []).forEach((cb) => cb(e));
  }
  focus() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: Number(this.attrs.width) || this.clientWidth || 960, height: 100 }; }
  setPointerCapture() {}
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  matches(sel) {
    return sel.split(",").some((one) => {
      const m = one.trim().match(/^([a-z0-9]*)((?:\.[\w-]+)*)(?:\[([\w-]+)(?:="([^"]*)")?\])?$/i);
      if (!m) throw new Error("unsupported selector " + sel);
      const [, tag, classes, attr, val] = m;
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      for (const c of classes.split(".").filter(Boolean)) if (!this.classList.contains(c)) return false;
      if (attr) {
        const v = this.getAttribute(attr);
        if (v == null) return false;
        if (val !== undefined && v !== val) return false;
      }
      return true;
    });
  }
  closest(sel) { for (let n = this; n; n = n.parentNode) if (n.matches && n.tagName !== "#TEXT" && n.matches(sel)) return n; return null; }
  querySelectorAll(sel) { return this.all().filter((n) => n.tagName !== "#TEXT" && n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const body = new Node("body");
docRoot = body;
const document = {
  hidden: false,
  createElement: (t) => new Node(t),
  createElementNS: (ns, t) => new Node(t),
  createTextNode: (t) => new Node("#text", t),
  addEventListener() {},
};

// ---------- fixtures: one week ending today, local time ----------
const NOW = Date.now();
const H = 3600e3, DAY = 24 * H;
const d0 = new Date(NOW);
const TODAY = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate()).getTime();
const iso = (t) => new Date(t).toISOString();
// calendar days, not 24-hour steps: a week can hold a daylight-saving change
const add = (t, n) => { const d = new Date(t); return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n).getTime(); };
const ymd = (t) => { const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const at = (daysAgo, hour) => add(TODAY, -daysAgo) + hour * H;

const TIMEMAP = {
  lane: "project", start: iso(add(TODAY, -6)), end: iso(add(TODAY, 1)), revision: "r1",
  lanes: [
    { key: "vira", label: "vira",
      spans: [
        { id: "session:claude:a", title: "Ideas: live sessions on the card", status: "Merged", trail: "trail:A",
          start: iso(at(0, 1)), end: iso(at(0, 4)), app: "Claude Code", active_s: 3600 },
        { id: "session:vira:b", title: "History store", status: "Running", trail: "trail:B",
          start: iso(at(0, 5)), end: iso(at(0, 6)), app: "Vira", active_s: 600 },
        { id: "session:codex:c", title: "Count the tomatoes", status: "Answered", trail: "trail:C",
          start: iso(at(2, 9)), end: iso(at(2, 10)), app: "Codex", active_s: 0 }],
      points: [
        { id: "code:merged:vira:178", title: "Ideas: a session started from an idea shows live on the idea card",
          status: "Merged", trail: "trail:A", at: iso(at(0, 4.5)), prs: ["vira#178"], added: 56, removed: 13 },
        { id: "code:merged:vira:177", title: "Moments: relay a family event's best photos",
          status: "Merged", trail: "trail:M", at: iso(at(0, 2)), prs: ["vira#177"], added: 300, removed: 20 }],
      ticks: [
        { id: "run:program:p1", title: "Notes capture: 3 captured", status: "Ran", trail: "trail:P1", at: iso(at(0, 1)), end: iso(at(0, 1.1)), sub: "program" },
        { id: "run:program:p2", title: "Notes capture: failed", status: "Failed", trail: "trail:P2", at: iso(at(0, 2)), end: iso(at(0, 2.1)), sub: "program" }] },
    { key: "tiksi", label: "tiksi", spans: [],
      points: [{ id: "code:merged:tiksi:57", title: "Pin photos flat on surfaces", status: "Merged", trail: "trail:T",
                 at: iso(at(1, 15)), prs: ["tiksi#57"], added: 9, removed: 1 }],
      ticks: [{ id: "run:program:p1", title: "Notes capture: 3 captured", status: "Ran", trail: "trail:P1", at: iso(at(0, 1)), end: iso(at(0, 1.1)), sub: "program" }] },
  ],
};
const GRID = {
  span: "hour", start: iso(add(TODAY, -6)), end: iso(NOW), revision: "r1",
  columns: [{ start: iso(at(0, 3)), end: iso(at(0, 4)), label: "03:00" }],
  rows: [{ id: "program:notes", type: "program", name: "Notes capture", project: "TC-IL", schedules: [{ id: "rt", name: "Every hour", every_hours: 1 }],
           known_since: iso(at(6, 0)), runs: 2, failed: 1, missed: 1, last: iso(at(0, 2)),
           cells: [{ start: iso(at(0, 3)), end: iso(at(0, 4)), outcome: "missed", tone: "missed", runs: [], expected: 1, counts: {} },
                   { start: iso(at(0, 2)), end: iso(at(0, 3)), outcome: "Failed", tone: "fail", expected: 1, counts: { failed: 1 },
                     runs: [{ id: "run:program:p2", status: "Failed", at: iso(at(0, 2)), title: "Notes capture: failed" }] }] }],
};
const entry = (id, status, title, extra = {}) => Object.assign({
  id, type: "trail", kind: "request", status, title, at: iso(at(0, 4.5)), start: iso(at(0, 1)),
  project: "vira", projects: ["vira"], module: "work/ideas", modules: { "work/ideas": 2 }, prs: [],
  apps: ["Claude Code"], sessions: 1, kinds: { ask: 1, session: 1, code: 1 }, lines: { files: 2, added: 56, removed: 13 },
  cost_usd: 1.25, tokens: 120000, active_s: 3600, open: false, actions: [],
}, extra);
const STEPS = [
  { type: "event", kind: "ask", sub: "journal", status: "Answered", title: "Show the live session on the idea card", at: iso(at(0, 1)), end: "" },
  { type: "event", kind: "session", sub: "claude", status: "Merged", title: "Ideas: live sessions", at: iso(at(0, 1.2)), end: iso(at(0, 4)) },
  { type: "wait", kind: "wait", sub: "landing", status: "Answered", title: "Waited for your merge choice", at: iso(at(0, 4)), end: iso(at(0, 4.4)) },
  { type: "event", kind: "code", sub: "merged", status: "Merged", title: "Ideas: a session started from an idea", at: iso(at(0, 4.5)), end: "" },
];
const MERGE_ACT = { action: "merge", label: "Merge", method: "POST", url: "/api/history/action", body: { id: "trail:W", action: "merge", branch: "vira:claude/w" } };
const LIST = (group) => ({
  group, unit: "trail", total: 3, offset: 0, limit: 200, shown: 3, more: false, revision: "r1",
  statuses: { "Running": 0, "Waiting on you": 1, "Not merged": 0, "Merged": 2, "Answered": 0, "Ran": 0, "Failed": 0, "Dropped": 0, "Not started": 0 },
  unstarted_left_out: 5,
  groups: [{ key: ymd(TODAY), label: "Today", count: 3, entries: [
    entry("trail:A", "Merged", "Ideas: a session started from an idea shows live on the idea card", { prs: ["vira#178"], steps: STEPS }),
    entry("trail:M", "Merged", "Moments: relay a family event's best photos", { prs: ["vira#177"], steps: STEPS.slice(0, 2) }),
    entry("trail:W", "Waiting on you", "Follow up on the Work on blocks", { open: true, actions: [MERGE_ACT] }),
  ] }],
});
const PINNED = { group: "none", total: 1, shown: 1, more: false, statuses: { "Running": 1 },
  groups: [{ key: "all", label: "all", count: 1, entries: [entry("trail:B", "Running", "History store: shared base", { open: true })] }] };
const DETAIL = {
  entry: entry("trail:A", "Merged", "Ideas: a session started from an idea shows live on the idea card", { prs: ["vira#178"] }),
  focus: null,
  outcome: { status: "Merged", means: "Code reached main.",
             ended_by: { id: "code:merged:vira:178", kind: "code", sub: "merged", title: "Ideas: live", at: iso(at(0, 4.5)) },
             diff: { files: 2, added: 56, removed: 13, basis: "merged into main", by_file: [["static/app.js", 50, 10], ["server/ideas.py", 6, 3]] } },
  trail: STEPS.map((s, i) => Object.assign({ id: `s${i}`, links: {}, data: i === 1 ? { model: "claude-opus-5-5", active_s: 3600, cost_usd: 1.25 } : {} }, s)),
  touched: { modules: { "work/ideas": 2 }, projects: ["vira"], files: ["static/app.js", "server/ideas.py"], files_n: 2,
             made: [], contacts: [], branches: ["vira:claude/idea-live"], prs: ["vira#178"] },
  cost_time: { cost_usd: 1.25, cost_basis: "API list price for these tokens", cost_sessions_priced: 1, cost_sessions_unpriced: 0,
               tokens: 120000, active_s: 3600, wall_s: 12600,
               sessions: [{ id: "session:claude:a", app: "Claude Code", model: "claude-opus-5-5", start: iso(at(0, 1)), tokens: 120000, cost_usd: 1.25, active_s: 3600, status: "Merged", title: "Ideas: live" }] },
  actions: [{ action: "judge", label: "Judge", url: "/api/history/action", body: { id: "trail:A", action: "judge", job: "j1" } }],
  open: [{ what: "session", label: "Open session", job_id: "j1", event: "session:vira:j1" },
         { what: "pr", label: "Open pull request", url: "https://example.com/pull/178" }],
};
const RULES = { items: [{ id: "lr_a", rule: "Run the tests before calling it done", tier: 2, count: 2, when: ymd(TODAY),
  project: "vira", evidence: "skipped the suite", dismissed: false, history_ids: ["session:codex:c"], trail: "trail:C",
  sessions: [{ id: "session:codex:c", title: "Count the tomatoes", at: iso(at(2, 9)), trail: "trail:C", quote: "",
               actions: [{ action: "not_a_break", label: "Not a break", url: "/api/lessons/break", body: { rule_id: "lr_a", evidence_id: "e1", breaks: false } }] }],
  actions: [{ action: "guard", label: "Propose a guard", url: "/api/lessons/lr_a/propose", body: {} },
            { action: "dismiss", label: "Dismiss", url: "/api/lessons/lr_a/dismiss", body: { dismissed: true } }] }] };
const FACETS = { projects: [{ key: "vira", count: 9 }, { key: "tiksi", count: 2 }], modules: [{ key: "work/ideas", count: 4 }],
  statuses: ["Running", "Waiting on you", "Not merged", "Merged", "Answered", "Ran", "Failed", "Dropped"].map((k) => ({ key: k, means: k + " means" })) };

// ---------- the stubbed fetch: every request recorded ----------
const calls = [];
let actionPosts = 0;
const reply = (status, data) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(data) });
function fetchStub(url, opts = {}) {
  const u = new URL(url, "http://localhost:8390");
  const q = Object.fromEntries(u.searchParams.entries());
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ path: u.pathname, q, method: opts.method || "GET", body });
  switch (u.pathname) {
    case "/api/history/facets": return reply(200, FACETS);
    case "/api/history/timemap": return reply(200, JSON.parse(JSON.stringify(TIMEMAP)));
    case "/api/history/grid": return reply(200, GRID);
    case "/api/history/entry": return reply(200, DETAIL);
    case "/api/history/rules": return reply(200, RULES);
    case "/api/history/ask": return reply(200, { status: "model_unavailable", spec: null, fallback_source: "keywords",
      fallback: { view: "list", group: "request", lane: "project", span: "day",
                  filters: { since: ymd(TODAY), until: ymd(TODAY), project: "", module: "", status: [], kind: [], q: "moments" } } });
    case "/api/history/action":
      actionPosts += 1;
      if (!body.confirm) return reply(409, { detail: { needs_confirm: true, confirm: "Merge claude/w into live main?" } });
      return reply(200, { started: true, via: "branch.sh" });
    case "/api/history/for":
      if (q.idea) return reply(200, Object.assign({ for: "idea:" + q.idea }, DETAIL));
      return reply(200, { for: { module: q.module }, list: q.kind === "merged"
        ? { groups: [{ key: "d", entries: [LIST("day").groups[0].entries[1]] }] }
        : { groups: [{ key: "now", entries: [entry("trail:B", "Running", "History store", { at: iso(at(0, 6)) })] },
                     { key: "d", entries: [entry("trail:A", "Merged", "Newer", { at: iso(at(0, 4)) }), entry("trail:M", "Merged", "Older", { at: iso(at(1, 4)) })] }] } });
    case "/api/history":
      if (q.status === "Running,Waiting on you,Not merged" && q.group === "none" && !q.since) return reply(200, PINNED);
      if (q.group === "none" && q.automations === "1") {
        // the hits query: matches by the words asked
        const hits = q.q === "moments" ? [entry("trail:M", "Merged", "Moments")] : [];
        return reply(200, { group: "none", total: hits.length, shown: hits.length, more: false, groups: [{ key: "all", entries: hits }] });
      }
      return reply(200, LIST(q.group || "day"));
    default: return reply(404, { detail: "no such route " + u.pathname });
  }
}

const intervals = [];
const context = {
  window: { __VIRA_TEST__: true }, document, console, Date, JSON, Map, Set, Math, URL, Promise, Number, String, Array, Object,
  fetch: fetchStub,
  setInterval: (f) => { intervals.push(f); return intervals.length; }, clearInterval() {},
  setTimeout: (f, ms) => setTimeout(f, Math.min(ms || 0, 5)), clearTimeout: (t) => clearTimeout(t),
  toast: () => {}, lsGet: (k, d) => d, lsSet: () => {},
};
context.window.open = (u) => { context.opened = u; };
context.openSession = (jid) => { context.sessionOpened = jid; };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "history.js"), "utf8"), context, { filename: "history.js" });
const X = context.window.__VIRA_HISTORY_TESTS__;
const VH = context.window.ViraHistory;
assert.ok(X && VH, "history test hooks were not installed");
const plain = (v) => JSON.parse(JSON.stringify(v));
const flush = async (n = 8) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 6)); };
const last = (p) => calls.filter((c) => c.path === p).at(-1);

(async () => {
  // ---------- pure pieces ----------
  const [wa, wb] = X.period("week", TODAY);
  assert.equal(wb - wa, add(TODAY, 1) - add(TODAY, -6), "a week is the seven days ending on the anchor");
  assert.equal(X.ymd(wa), ymd(add(TODAY, -6)));
  const [da, db] = X.period("day", TODAY);
  assert.equal(db - da, add(TODAY, 1) - TODAY);
  assert.equal(X.period("month", TODAY)[1] - X.period("month", TODAY)[0], add(TODAY, 1) - add(TODAY, -29));
  assert.equal(X.fitZoom(TODAY, add(TODAY, 1)).zoom, "day");
  assert.equal(X.fitZoom(add(TODAY, -3), add(TODAY, 1)).zoom, "week");
  assert.equal(X.fitZoom(add(TODAY, -20), add(TODAY, 1)).zoom, "month");
  const packed = X.packRows([{ _a: 0, _b: 10 }, { _a: 5, _b: 8 }, { _a: 10, _b: 12 }, { _a: 6, _b: 7 }], 2);
  assert.deepEqual(plain(packed.placed.map((s) => s._r)), [0, 1, 0], "overlaps stack; a bar takes the first free row");
  assert.equal(packed.hidden, 1, "rows past the cap are counted, not dropped silently");
  assert.equal(X.missedSlots(GRID).length, 1);
  const days = X.dayCounts(X.prepMap(JSON.parse(JSON.stringify(TIMEMAP)), NOW), GRID, wa, wb);
  assert.equal(days.length, 7);
  const today = days[6];
  assert.equal(today.merges, 2);
  assert.equal(today.runs, 2, "a run that sits on two lanes counts once");
  assert.equal(today.failed, 1);
  assert.equal(today.missed, 1);
  assert.equal(today.live, 1, "the running session marks its day");
  assert.equal(X.stepLabel({ type: "event", kind: "code", sub: "merged" }), "Merged into main");
  assert.equal(X.stepLabel({ type: "event", kind: "session", sub: "codex" }), "Codex session");
  assert.match(X.stepLabel({ type: "wait", title: "Waited for your merge choice", at: iso(0), end: iso(24 * 60e3) }), /: 24m$/);
  assert.equal(X.spanLabel(TODAY, add(TODAY, 1)), new Date(TODAY).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }));

  // ---------- the Time map ----------
  const host = new Node("div");
  host.clientWidth = 1200;
  body.append(host);
  const map = VH.mountMap(host, {});
  await flush();
  const tm = last("/api/history/timemap");
  assert.equal(tm.q.since, ymd(add(TODAY, -6)), "it opens on the week ending today");
  assert.equal(tm.q.until, ymd(TODAY));
  assert.equal(tm.q.lane, "project");
  assert.equal(last("/api/history/grid").q.span, "hour", "the week's gaps are judged hour by hour");
  const svg = host.querySelector(".hx-svg");
  assert.ok(svg, "the map is drawn");
  const laneKeys = svg.querySelectorAll(".hx-lane").map((g) => g.getAttribute("data-lane"));
  assert.deepEqual(plain(laneKeys), ["vira", "tiksi", "Automations"], "one lane per project, then the Automations lane");
  assert.equal(svg.querySelectorAll(".hx-merge").length, 3, "every merge into main is a diamond");
  const bars = svg.querySelectorAll(".hx-bar");
  assert.deepEqual(plain(bars.map((b) => b.getAttribute("class").match(/a-(\w+)/)[1]).sort()), ["claude", "codex", "vira"], "bars are colored by app");
  const claudeBar = bars.find((b) => b.getAttribute("data-id") === "session:claude:a");
  const open = claudeBar.querySelector(".hx-open"), act = claudeBar.querySelector(".hx-active");
  assert.ok(act && Number(act.getAttribute("width")) < Number(open.getAttribute("width")), "active time is drawn inside the open time");
  assert.ok(bars.find((b) => b.getAttribute("data-id") === "session:vira:b").querySelector(".hx-cap"), "running work is capped at its end");
  assert.equal(svg.querySelectorAll(".hx-edge").length, 1, "the lane with running work is marked at the right edge");
  assert.equal(svg.querySelectorAll(".hx-tick").length, 2, "a run on two lanes is one tick");
  assert.equal(svg.querySelectorAll(".hx-tick.t-fail").length, 1, "a failed run is red");
  assert.equal(svg.querySelectorAll(".hx-miss").length, 1, "a missed scheduled run is a gap");
  assert.match(svg.querySelector(".hx-auto").textContent, /2 runs, 1 failed, 1 missed/);
  assert.ok(svg.querySelector(".hx-now"), "now is marked");

  // pinned unfinished work
  const pin = host.querySelector(".hx-pinned");
  assert.match(pin.textContent, /Unfinished, pinned/);
  assert.match(pin.textContent, /History store: shared base/);
  assert.equal(calls.find((c) => c.path === "/api/history" && c.q.status).q.status, "Running,Waiting on you,Not merged");

  // the list below: the whole week until a span is picked, with trails inline
  let lq = last("/api/history");
  assert.equal(lq.q.group, "day");
  assert.equal(lq.q.steps, "1");
  assert.equal(Date.parse(lq.q.since), add(TODAY, -6));
  const rowA = host.querySelector(".hx-list").querySelector('.hx-row[data-id="trail:A"]');
  assert.ok(rowA);
  assert.deepEqual(plain(rowA.querySelectorAll(".hx-step").map((s) => s.querySelector(".hx-step-k").textContent)),
    ["Asked", "Claude Code session", "Waited for your merge choice: 24m", "Merged into main"]);
  assert.match(rowA.textContent, /PR #178/);

  // click a day on the map: the list is that day
  const geoW = Number(svg.getAttribute("width"));
  const X0 = 138, X1 = geoW - 14;
  const xOf = (t) => X0 + (t - wa) / (wb - wa) * (X1 - X0);
  const col = svg.querySelector(".hx-col");
  col.dispatch("pointerdown", { clientX: xOf(TODAY + 12 * H), button: 0, pointerId: 1 });
  col.dispatch("pointerup", { clientX: xOf(TODAY + 12 * H), pointerId: 1 });
  await flush();
  lq = last("/api/history");
  assert.equal(Date.parse(lq.q.since), TODAY, "a click selects the local day under the pointer");
  assert.equal(Date.parse(lq.q.until), add(TODAY, 1));
  assert.match(host.querySelector(".hx-listhead").textContent, /Selected: /);
  assert.match(host.querySelector(".hx-listhead").textContent, /3 entries: 1 waiting on you, 2 merged/);
  // drag a range: snapped to hours in the week
  const col2 = host.querySelector(".hx-svg").querySelector(".hx-col");
  col2.dispatch("pointerdown", { clientX: xOf(add(TODAY, -2) + 3.2 * H), button: 0 });
  col2.dispatch("pointermove", { clientX: xOf(add(TODAY, -1) + 7.6 * H) });
  col2.dispatch("pointerup", { clientX: xOf(add(TODAY, -1) + 7.6 * H) });
  await flush();
  lq = last("/api/history");
  assert.equal(Date.parse(lq.q.since), add(TODAY, -2) + 3 * H, "a drag snaps its start down to the hour");
  assert.equal(Date.parse(lq.q.until), add(TODAY, -1) + 8 * H, "and its end up to the hour");
  map.selectSpan(TODAY, add(TODAY, 1));
  await flush();

  // Group by
  const gb = host.querySelector(".hx-groupby").querySelectorAll("button");
  assert.deepEqual(plain(gb.map((b) => b.textContent)), ["Day", "Request", "Module", "Project", "Automation", "Session"]);
  gb[1].click();
  await flush();
  assert.equal(last("/api/history").q.group, "request");

  // Not started: a ninth chip, off by default, counting the ideas left out
  const chip = (w) => host.querySelector(".hx-outcome").querySelectorAll(".hx-tog").find((b) => b.dataset.word === w);
  assert.equal(chip("Not started").getAttribute("aria-pressed"), "false");
  assert.equal(chip("Not started").querySelector(".hx-n").textContent, "5", "it counts the ideas left out");
  assert.equal(chip("Merged").querySelector(".hx-n").textContent, "2", "every word counts its entries");
  assert.match(host.querySelector(".hx-listhead").textContent, /5 ideas not started left out/);
  assert.equal(last("/api/history").q.status, undefined, "nothing asks for them by default");
  chip("Not started").click();
  await flush();
  assert.equal(last("/api/history").q.status, "Not started", "picking the chip asks for them");
  chip("Not started").click();
  await flush();

  // Layers: the shipped view is merges alone
  const tog = (label) => host.querySelector(".hx-ctl").querySelectorAll(".hx-tog").find((b) => b.textContent === label);
  tog("Shipped view").click();
  let s2 = host.querySelector(".hx-svg");
  assert.equal(s2.querySelectorAll(".hx-bar").length, 0);
  assert.equal(s2.querySelectorAll(".hx-tick").length, 0);
  assert.equal(s2.querySelectorAll(".hx-merge").length, 3, "the shipped view keeps every merge");
  assert.equal(tog("Shipped view").getAttribute("aria-pressed"), "true");
  tog("Shipped view").click();
  assert.equal(host.querySelector(".hx-svg").querySelectorAll(".hx-bar").length, 3, "and comes back");

  // the Automations lane opens the full grid
  host.querySelector(".hx-svg").querySelector(".hx-lanelab.auto").querySelector("text").dispatch("click");
  await flush();
  const grid = host.querySelector(".hx-gridbox");
  assert.equal(grid.hidden, false);
  assert.ok(grid.querySelector(".hx-gc.g-missed"), "the grid shows the missed cell");
  assert.equal(last("/api/history/grid").q.span, "hour", "a one-day span is a grid of hours");
  grid.querySelector(".hx-gc.g-fail").click();
  await flush();
  assert.equal(last("/api/history/entry").q.id, "run:program:p2", "a grid cell opens its run");

  // a merge diamond opens its detail: outcome and diff first, then the trail
  host.querySelector(".hx-svg").querySelectorAll(".hx-merge").find((m) => m.getAttribute("data-id") === "code:merged:vira:177").dispatch("click");
  await flush();
  assert.equal(last("/api/history/entry").q.id, "code:merged:vira:177");
  const detail = host.querySelector(".hx-detail");
  assert.equal(detail.hidden, false);
  const heads = detail.querySelectorAll("h4").map((h) => h.textContent);
  assert.deepEqual(plain(heads), ["Outcome", "Trail (3 steps)", "What it touched", "Cost and time"], "outcome leads, cost and time close");
  assert.match(detail.textContent, /\+56 -13 in 2 files \(merged into main\)/);
  assert.match(detail.textContent, /static\/app\.js/);
  const dbtn = (label) => detail.querySelectorAll("button").find((b) => b.textContent === label);
  dbtn("Open transcript").click();
  assert.equal(context.sessionOpened, "j1", "Open transcript opens the session replay");
  dbtn("Open pull request").click();
  assert.equal(context.opened, "https://example.com/pull/178");
  assert.ok(host.classList.contains("has-detail"));

  // Merge on a list row: the server's confirmation first, then the merge
  const rowW = host.querySelector(".hx-list").querySelector('.hx-row[data-id="trail:W"]');
  rowW.querySelectorAll("button").find((b) => b.textContent === "Merge").click();
  await flush();
  assert.equal(actionPosts, 1);
  assert.deepEqual(plain(last("/api/history/action").body), MERGE_ACT.body, "the entry's own body is sent");
  const confirm = rowW.querySelector(".hx-confirm");
  assert.match(confirm.textContent, /Merge claude\/w into live main\?/);
  confirm.querySelectorAll("button").find((b) => b.textContent === "Confirm merge").click();
  await flush();
  assert.equal(actionPosts, 2);
  assert.equal(last("/api/history/action").body.confirm, true);
  assert.match(rowW.textContent, /Merge: started through the branch\.sh/);

  // ---------- a plain-words question ----------
  const ask = host.querySelector(".hx-ask");
  ask.querySelector(".hx-askin").value = "what about moments today";
  ask.fire("submit");
  await flush(14);
  assert.deepEqual(plain(last("/api/history/ask").body), { q: "what about moments today" });
  const chips = host.querySelector(".hx-askchips").textContent;
  assert.match(chips, /Words: moments/);
  assert.match(chips, /When: /);
  assert.match(chips, /No model answered; read from the words alone\./);
  assert.equal(last("/api/history").q.group, "request", "the answer's grouping is used");
  assert.equal(last("/api/history").q.q, "moments");
  assert.equal(last("/api/history/timemap").q.since, ymd(TODAY), "the span jumps to fit the answer: one day");
  const lit = host.querySelector(".hx-svg");
  const m177 = lit.querySelectorAll(".hx-merge").find((m) => m.getAttribute("data-id") === "code:merged:vira:177");
  const m178 = lit.querySelectorAll(".hx-merge").find((m) => m.getAttribute("data-id") === "code:merged:vira:178");
  assert.match(m177.getAttribute("class"), /\bhit\b/, "a match lights up");
  assert.match(m178.getAttribute("class"), /\bfade\b/, "the rest fades");
  assert.match(host.querySelector(".hx-legend").textContent, /1 match lit on the map/);
  // a chip is removable, and removing it re-reads
  const before = calls.length;
  host.querySelector(".hx-askchips").querySelectorAll(".hx-achip").find((c) => /Words/.test(c.textContent)).querySelector("button").click();
  await flush();
  assert.ok(calls.length > before);
  assert.equal(last("/api/history").q.q, undefined, "the words are gone from the query");
  assert.doesNotMatch(host.querySelector(".hx-svg").querySelector(".hx-merge").getAttribute("class"), /fade/);

  // ---------- Sessions that broke a rule ----------
  host.querySelector(".hx-filters").querySelectorAll(".hx-tog").find((b) => b.textContent === "Sessions that broke a rule").click();
  await flush();
  assert.ok(calls.some((c) => c.path === "/api/history/rules"));
  const rule = host.querySelector(".hx-list").querySelector(".hx-rule");
  assert.match(rule.textContent, /Run the tests before calling it done/);
  const rbtn = rule.querySelectorAll("button").map((b) => b.textContent);
  assert.ok(rbtn.includes("Propose a guard") && rbtn.includes("Dismiss") && rbtn.includes("Not a break"));
  assert.equal(host.querySelector(".hx-listhead").textContent.includes("Sessions that broke a rule"), true);
  const codexBar = host.querySelector(".hx-svg").querySelectorAll(".hx-bar").find((b) => b.getAttribute("data-id") === "session:codex:c");
  // the rule's session is outside the one-day period now, so widen first
  host.querySelector(".hx-ctl").querySelectorAll(".hx-seg")[0].querySelectorAll("button")[1].click();
  await flush();
  const rb = host.querySelector(".hx-svg").querySelectorAll(".hx-bar");
  assert.match(rb.find((b) => b.getAttribute("data-id") === "session:codex:c").getAttribute("class"), /\bhit\b/);
  assert.match(rb.find((b) => b.getAttribute("data-id") === "session:claude:a").getAttribute("class"), /\bfade\b/);
  void codexBar;
  rule.querySelectorAll("button").find((b) => b.textContent === "Dismiss").click();
  await flush();
  assert.deepEqual(plain(calls.filter((c) => c.path === "/api/history/action").length), 2, "rule actions use their own routes");

  host.querySelector(".hx-filters").querySelectorAll(".hx-tog").find((b) => b.textContent === "Sessions that broke a rule").click();
  await flush();
  assert.equal(host.querySelector(".hx-list").querySelector(".hx-rule"), null, "turning the filter off brings the list back");

  // ---------- module lanes, and the open helper other screens call ----------
  host.querySelector(".hx-ctl").querySelectorAll(".hx-seg")[1].querySelectorAll("button")[1].click();
  await flush();
  assert.equal(last("/api/history/timemap").q.lane, "module");
  map.open({ entry: "pr:vira#178" });
  await flush();
  assert.equal(last("/api/history/entry").q.id, "pr:vira#178");
  // a module window's "Open in Fleet History" lands on that module's lanes
  map.open({ module: "work" });
  await flush();
  assert.equal(last("/api/history/timemap").q.module, "work");
  assert.equal(last("/api/history/timemap").q.lane, "module");
  assert.equal(last("/api/history").q.module, "work");
  map.state.f.module = "";

  // ---------- narrow screens: a day strip over the list ----------
  host.clientWidth = 400;
  map.state.zoom = "week";
  map.refresh();
  await flush();
  const strip = host.querySelector(".hx-strip");
  assert.ok(strip, "a phone gets the day strip, not the map");
  assert.equal(strip.querySelectorAll(".hx-sday").length, 7);
  assert.match(strip.querySelectorAll(".hx-sday")[6].textContent, /2 merged/);
  strip.querySelectorAll(".hx-sday")[5].click();
  await flush();
  assert.equal(Date.parse(last("/api/history").q.since), add(TODAY, -1));

  // ---------- a module window's History ----------
  const mhost = new Node("div");
  mhost.clientWidth = 600;
  body.append(mhost);
  VH.mountFor(mhost, { module: "work", title: "Work: history" });
  await flush();
  const fq = last("/api/history/for");
  assert.equal(fq.q.module, "work");
  assert.equal(fq.q.steps, "1");
  assert.equal(fq.q.since, ymd(add(TODAY, -29)), "a month");
  assert.equal(last("/api/history/timemap").q.module, "work");
  assert.ok(mhost.querySelector(".hx-forsvg"), "the module's own lane");
  assert.equal(mhost.querySelector(".hx-forsvg").querySelectorAll(".hx-cell").length, 30, "one cell per day of the month");
  assert.deepEqual(plain(mhost.querySelectorAll(".hx-row").map((r) => r.querySelector(".hx-row-title").textContent)),
    ["Older", "Newer", "History store"], "oldest first");
  mhost.querySelectorAll(".hx-tog").find((b) => b.textContent === "Merges only").click();
  await flush();
  assert.equal(last("/api/history/for").q.kind, "merged");
  assert.equal(mhost.querySelectorAll(".hx-row").length, 1);

  // ---------- an idea card's History ----------
  const ihost = new Node("div");
  body.append(ihost);
  VH.mountFor(ihost, { idea: "idea_a", title: "History of this idea" });
  await flush();
  assert.equal(last("/api/history/for").q.idea, "idea_a");
  assert.equal(ihost.querySelectorAll(".hx-ti").length, 4, "the whole trail, oldest first");
  assert.match(ihost.querySelectorAll(".hx-ti")[0].textContent, /Asked/);
  ihost.querySelectorAll(".hx-tog").find((b) => b.textContent === "Merges only").click();
  await flush();
  assert.equal(ihost.querySelectorAll(".hx-ti").length, 1);
  assert.match(ihost.querySelector(".hx-ti").textContent, /Merged into main/);

  // ---------- live: a moved revision re-reads what is on screen ----------
  const n0 = calls.filter((c) => c.path === "/api/history/timemap").length;
  X.Live.onRev("rev-1");
  X.Live.onRev("rev-1");
  await flush(4);
  assert.equal(calls.filter((c) => c.path === "/api/history/timemap").length, n0, "the first revision only arms it");
  X.Live.onRev("rev-2");
  await new Promise((r) => setTimeout(r, 40));
  await flush();
  assert.ok(calls.filter((c) => c.path === "/api/history/timemap").length > n0, "a moved revision re-reads the map");

  console.log("history UI: ok");
})().catch((e) => { console.error(e); process.exit(1); });

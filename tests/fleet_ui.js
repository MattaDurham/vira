"use strict";

// The Fleet window (static/fleet.js), run for real against synthetic
// snapshots in a small fake DOM: the pure pieces (which sessions show,
// lanes, counts, the timeline, the week, background calls, the live label)
// and the render path a live frame takes (strip, needs-you row, lanes,
// tiles, navigation). Run by tests/test_fleet.py.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// ---------- a fake DOM: just what fleet.js touches ----------
class ClassList {
  constructor(node) { this.node = node; }
  get list() { return this.node.className.split(/\s+/).filter(Boolean); }
  contains(c) { return this.list.includes(c); }
  add(c) { if (!this.contains(c)) this.node.className = [...this.list, c].join(" "); }
  remove(c) { this.node.className = this.list.filter((x) => x !== c).join(" "); }
  toggle(c, on) { const want = on === undefined ? !this.contains(c) : !!on; if (want) this.add(c); else this.remove(c); }
}
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
    this.parent = null;
    this.hidden = false;
    this.scrollTop = 0;
    this.offsetParent = {};
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  set innerHTML(v) { this._text = ""; this.children = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  // Like a browser: anything that is not a node is appended as its text,
  // so a stray null shows up as "null" here as it does on screen.
  _adopt(c) { if (!(c instanceof Node)) c = new Node("#text", String(c)); c.parent = this; return c; }
  append(...cs) { cs.forEach((c) => this.children.push(this._adopt(c))); }
  appendChild(c) { this.append(c); return c; }
  prepend(...cs) { this.children.unshift(...cs.map((c) => this._adopt(c))); }
  replaceChildren(...cs) { this.children = []; this.append(...cs); }
  addEventListener(t, cb) { (this.listeners[t] ||= []).push(cb); }
  click() { (this.listeners.click || []).forEach((cb) => cb({ stopPropagation() {}, target: this })); }
  focus() {}
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  matches(sel) {
    const m = sel.match(/^([a-z0-9]*)((?:\.[\w-]+)*)(\[data-([\w-]+)\])?$/i);
    if (!m) throw new Error("unsupported selector " + sel);
    const [, tag, classes, , data] = m;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    for (const c of classes.split(".").filter(Boolean)) if (!this.classList.contains(c)) return false;
    if (data && !(data.replace(/-(\w)/g, (_, x) => x.toUpperCase()) in this.dataset)) return false;
    return true;
  }
  querySelectorAll(sel) { return this.all().filter((n) => n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const root = new Node("div");
root.className = "fl-root";
const document = {
  hidden: false, activeElement: null,
  createElement: (t) => new Node(t),
  createElementNS: (ns, t) => new Node(t),
  createTextNode: (t) => new Node("#text", t),
  getElementById: (id) => (id === "fleet-root" ? root : null),
  addEventListener() {},
};
const timers = [];
const context = {
  window: { __VIRA_TEST__: true }, document, console, Date, JSON, Map, Set, Math,
  setInterval: (f) => { timers.push(f); return timers.length; }, setTimeout: () => 0, clearTimeout() {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "fleet.js"), "utf8"), context,
  { filename: "fleet.js" });
const F = context.window.__VIRA_FLEET_TESTS__;
assert.ok(F, "fleet test hooks were not installed");

const NOW = Date.now();
const MIN = 60e3, H = 3600e3, DAY = 24 * H;
const live = (id, project, status, extra = {}) => ({
  id, tool: "claude", surface: "app", project, title: `Session ${id}`, status, live: true,
  family: id, lastAt: NOW - MIN, createdAt: NOW - H, turnOpen: status === "Working",
  turnStartedAt: NOW - 2 * MIN,
  now: status === "Working" ? { name: "Bash", desc: "npm test", startedAt: NOW - 5000, kind: "testing" } : null,
  doing: status === "Working" ? "testing" : status === "Needs you" ? "waiting" : "idle",
  feed: [], plan: { done: 1, total: 3, items: [{ content: "a", status: "completed" },
    { content: "b", status: "in_progress" }, { content: "c", status: "pending" }] },
  question: null, context: { used: 100000, limit: 200000, compactions: 0 },
  agents: { live: [], total: 0 }, shells: [], model: "test-1", openUrl: `claude://claude.ai/epitaxy/${id}`,
  git: { branch: "claude/x", prs: [] }, ...extra,
});
const closed = (id, project, ago, extra = {}) => ({
  id, tool: "claude", surface: "app", project, title: `Closed ${id}`, status: "Closed", live: false,
  family: id, lastAt: NOW - ago, createdAt: NOW - ago - H, git: { prs: [] }, ...extra,
});
const ask = { kind: "ask", at: NOW - 30e3, questions: [{ question: "Which store?", header: "Store", multi: false,
  options: [{ label: "SQLite", description: "" }, { label: "JSON", description: "" }] }] };
const snap = {
  snapshotAt: NOW,
  totals: { open: 4, working: 1, needsYou: 1, jobs: 0, idle: 2, subagents: 0, shells: 0, all: 9, closed24: 1 },
  sessions: [
    live("w1", "alpha", "Working"),
    live("n1", "alpha", "Needs you", { question: ask }),
    live("i1", "beta", "Idle", { tool: "codex", surface: "app", openUrl: "codex://threads/i1" }),
    live("v1", "beta", "Idle", { tool: "vira", surface: "Implement", viraJob: "j1", openUrl: null }),
  ],
  nodes: [
    closed("c1", "alpha", 2 * H, { parentId: "w1", link: "fork", family: "w1" }),
    closed("c2", "gamma", 2 * DAY),
    closed("c3", "gamma", 5 * DAY),
  ],
  families: [{ id: "w1", root: "w1", title: "Feature W", project: "alpha", members: ["w1", "c1"], kind: "feature",
    state: "active", branches: [{ name: "claude/x", state: "open_pr", pr: { n: 4, state: "OPEN" } }],
    branchCount: 1, merged: 0, deleted: 0, needsWork: 1, open: 1, lastAt: NOW }],
  closedToday: [], closedTodayCount: 1, servers: [], instances: [],
  sources: { codex: { found: false, note: "Codex not found on this machine" } },
};

// ---------- pure pieces ----------
const idx = F.index(snap);
// Arrays made inside the VM have the VM's prototypes; compare plain copies.
const plain = (x) => JSON.parse(JSON.stringify(x));
const ids = (rows) => plain(rows.map((n) => n.id)).sort();
assert.ok(F.realWaiting(snap.sessions[1]));
assert.ok(!F.realWaiting(snap.sessions[0]));
assert.ok(!F.realWaiting({ live: true, question: { kind: "wrap", blocked: false } }), "an unblocked wrap-up is not waiting");
assert.ok(F.realWaiting({ live: true, question: { kind: "wrap", blocked: true } }));
assert.ok(!F.realWaiting({ live: false, question: ask }), "a closed session waits on nobody");

const f = (o = {}) => ({ tool: "all", project: "", mode: "24h", ...o });
assert.deepEqual(ids(F.visibleNodes(snap, idx, f({ mode: "linked" }), NOW)), ["c1", "i1", "n1", "v1", "w1"],
  "linked only: open sessions and their family");
assert.deepEqual(ids(F.visibleNodes(snap, idx, f({ mode: "3d" }), NOW)), ["c1", "c2", "i1", "n1", "v1", "w1"]);
assert.deepEqual(ids(F.visibleNodes(snap, idx, f({ tool: "codex" }), NOW)), ["i1"]);
assert.deepEqual(ids(F.visibleNodes(snap, idx, f({ project: "beta" }), NOW)), ["i1", "v1"]);

const lanes = F.laneGroups(F.visibleNodes(snap, idx, f(), NOW), idx);
assert.deepEqual(plain(lanes.map((l) => l.p)), ["alpha", "beta"], "lanes with open sessions first");
assert.equal(lanes[0].fams.find((x) => x.f.id === "w1").members.length, 2, "a family keeps its fork");

assert.deepEqual(plain(F.counts(snap, f())),
  { working: 1, needs: 1, jobs: 0, idle: 2, agents: 0, shells: 0 });
assert.equal(F.counts(snap, f({ tool: "vira" })).idle, 1, "counts follow the tool filter");

assert.equal(F.liveLabel(2000).text, "Live");
assert.match(F.liveLabel(20000).text, /^Paused, updated 20s ago$/);
assert.equal(F.liveLabel(20000, "stream").text, "Reconnecting");

// the timeline: landings and change-log rows, by mode, search and day
const repos = {
  viraRepo: "/r/alpha", repos: [
    { root: "/r/alpha", name: "alpha", landings: [
      { at: NOW - H, tool: "claude", branch: "claude/x", subject: "Merge branch 'claude/x'",
        pr: { n: 4, title: "Fleet window" }, files: 3, insertions: 40, deletions: 2 },
      { at: NOW - DAY - H, tool: "direct", branch: null, subject: "Fix a typo", pr: null, files: 1, insertions: 1, deletions: 1 }],
      changelog: [{ at: NOW - 2 * H, section: "Added", headline: "A window.", detail: "" }] },
    { root: "/r/beta", name: "beta", landings: [
      { at: NOW - 3 * H, tool: "codex", branch: "codex/y", subject: "Merge branch codex/y", pr: null, files: 2, insertions: 5, deletions: 0 }],
      changelog: [] }],
  viraLog: [{ date: "x", entries: [{ text: "Shipped the thing", kind: "ship", ts: new Date(NOW - 4 * H).toISOString(), source: "retro" }] }],
  calls: [{ folder: "alpha", kind: "library tagging", today: 2, week: 10, last: NOW - H },
          { folder: "beta", kind: "other", today: 0, week: 5, last: NOW - DAY }],
  week: [{ id: "w1", tool: "claude", project: "alpha", title: "W", start: NOW - 9 * DAY, end: NOW - 2 * DAY },
         { id: "i1", tool: "codex", project: "beta", title: "I", start: NOW - H, end: NOW, live: true }],
};
const tl = (o = {}) => ({ mode: "all", q: "", day: null, ...o });
assert.deepEqual(plain(F.timelineRows(repos, f(), tl()).map((r) => r.kind)),
  ["landing", "change", "landing", "log", "landing"], "newest first, every kind");
assert.deepEqual(plain(F.timelineRows(repos, f(), tl({ mode: "landed" })).map((r) => r.kind)), ["landing", "landing", "landing"]);
assert.deepEqual(plain(F.timelineRows(repos, f(), tl({ mode: "log" })).map((r) => r.kind)), ["change", "log"]);
assert.equal(F.timelineRows(repos, f(), tl({ q: "fleet window" })).length, 1, "search reads PR titles");
assert.equal(F.timelineRows(repos, f({ project: "beta" }), tl()).length, 1);
const days = F.landingDays(repos, f(), 7, NOW);
assert.equal(days.length, 7);
const total = days.reduce((a, d) => a + d.claude + d.codex + d.other + d.direct, 0);
assert.equal(total, 3, "every landing in range lands in one day's stack");
assert.ok(days.some((d) => d.titles.includes("#4 Fleet window")), "a bar's tooltip names the PR");

const bars = F.weekBars(repos.week, f(), NOW);
const start = F.weekStart(NOW);
assert.equal(new Date(start).getHours(), 0, "the week starts at a local midnight");
assert.equal(bars[0].x, 0, "a session older than the week is clipped to its start");
assert.ok(bars[1].x > 0.8 && bars[1].x < 1, "today sits in the last column");
const calls = F.callRows(repos.calls, f());
assert.equal(calls[0].weekW, 1, "the busiest row fills its cell");
assert.equal(calls[1].weekW, 0.5, "every row is on the same scale");

// ---------- the render path a live frame takes ----------
F.mount();
F.onFrame("fleet", snap);
const top = root.querySelector(".fl-counts");
const count = (key) => top.querySelector(`.c-${key}`);
assert.equal(count("working").querySelector("b").textContent, "1");
assert.ok(count("needs").classList.contains("alert"), "a waiting question lights its count");
assert.equal(root.querySelector(".fl-live-t").textContent, "Live");

const lanesBox = root.querySelector(".fl-lanes");
const laneNames = lanesBox.querySelectorAll(".fl-lane-name").map((n) => n.textContent);
assert.deepEqual(laneNames, ["alpha", "beta"]);
const asks = lanesBox.querySelector(".fl-asks");
assert.ok(asks, "the needs-you row is drawn");
assert.equal(asks.querySelector(".fl-qq").textContent, "Which store?");
assert.deepEqual(asks.querySelectorAll(".fl-opt").map((o) => o.textContent), ["SQLite", "JSON"]);
assert.equal(asks.querySelector(".fl-open").href, "claude://claude.ai/epitaxy/n1", "Open goes to the session");

const tile = F.tiles.get("w1");
assert.equal(tile.dataset.status, "Working");
assert.equal(tile.querySelector(".fl-tooln").textContent, "Testing", "the tile says what the call is for");
assert.equal(tile.querySelector(".fl-tooln").title, "Bash");
assert.equal(tile.querySelector(".fl-what").textContent, "npm test");
const icon = tile.querySelector(".fl-do");
assert.equal(icon.dataset.kind, "testing");
assert.ok(icon.querySelector("svg"), "the icon is drawn");
assert.equal(tile.querySelector(".fl-sc").textContent, "1/3");
assert.equal(tile.querySelectorAll(".fl-seg").length, 3);
assert.equal(F.chips.get("c1").querySelector(".fl-tt").textContent, "Closed c1", "a closed fork is a chip");
assert.equal(F.tiles.get("i1").querySelector(".fl-tool").textContent, "Codex app");

// The doing strip: open sessions grouped by what they are doing, in a
// fixed order, each card naming its sessions.
const strip = lanesBox.querySelector(".fl-doing");
assert.ok(strip, "the doing strip is drawn above the lanes");
const cards = strip.querySelectorAll(".fl-dcard");
assert.deepEqual(cards.map((c) => c.querySelector(".fl-dl").textContent), ["Waiting", "Testing", "Idle"]);
assert.equal(cards[2].querySelector(".fl-dc").textContent, "2");
assert.deepEqual(cards[1].querySelectorAll(".fl-dn").map((n) => n.textContent), ["Session w1"]);
assert.equal(F.doingStrip([]), null, "nothing open, no strip");

// A Vira job opens in Vira, not over a URL.
const viraOpen = F.tiles.has("v1") && lanesBox.querySelectorAll(".fl-fam").length > 0;
assert.ok(viraOpen);

// The next frame updates the same tile in place.
const next = JSON.parse(JSON.stringify(snap));
next.sessions[0].status = "Idle";
next.sessions[0].turnOpen = false;
next.sessions[0].now = null;
next.sessions[0].lastText = "All green.";
next.sessions[0].doing = "idle";
F.onFrame("fleet", next);
assert.equal(F.tiles.get("w1"), tile, "the tile is kept, so its bars animate");
assert.equal(tile.dataset.status, "Idle");
assert.equal(tile.querySelector(".fl-what").textContent, "All green.");
const idleIcon = tile.querySelector(".fl-do");
assert.equal(idleIcon.dataset.kind, "idle", "the icon follows the session");
F.onFrame("fleet", next);
assert.equal(tile.querySelector(".fl-do"), idleIcon, "an unchanged kind keeps its icon, so it keeps animating");
assert.equal(root.querySelector(".fl-doing").querySelectorAll(".fl-dcard").length, 2, "a kind nobody is doing leaves the strip");

// A doing card opens the list of those sessions.
F.onFrame("fleet", snap);
root.querySelector(".fl-doing").querySelectorAll(".fl-dtop")[1].click();
assert.equal(F.S.view, "list:do-testing");
assert.match(root.querySelector(".fl-sheet").textContent, /Session w1/);
F.back();

// A session's sheet: no stray "null", the agent's own words and each
// call's result in Activity, a failure marked, and shells not claimed as
// 0 when its process was never found.
const busy = JSON.parse(JSON.stringify(snap));
busy.sessions[0].feed = [
  { say: "263 related tests pass. Running the full suite.", at: NOW - 20e3 },
  { name: "Bash", desc: "Run the full unittest suite", at: NOW - 30e3, ms: 31, error: true,
    out: "exit 127: (eval):1: command not found: timeout", kind: "testing" },
  { name: "Read", desc: "app.js", at: NOW - 40e3, ms: 12, kind: "reading" }];
F.onFrame("fleet", busy);
F.go("w1");
const ses = root.querySelector(".fl-sheet");
assert.ok(!/null/.test(ses.textContent), "no null rendered as text");
assert.match(ses.querySelector(".fl-item.say").textContent, /Running the full suite/);
const failed = ses.querySelector(".fl-item.err");
assert.match(failed.textContent, /exit 127: \(eval\):1: command not found: timeout/);
assert.match(failed.querySelector(".b").textContent, /^failed \/ 31ms/);
assert.match(ses.textContent, /process not found/);
assert.equal(ses.querySelector(".fl-doing-tag").textContent, "Testing");
F.back();
F.onFrame("fleet", snap);

// Branch labels: a branch with no commits is not "merged", and its
// uncommitted files are counted.
assert.equal(F.branchLabel({ state: "fresh", dirty: 15 }), "no commits yet / 15 uncommitted files");
assert.equal(F.branchLabel({ state: "unmerged", ahead: 2, dirty: 1 }), "not merged / 2 commits ahead / 1 uncommitted file");
assert.equal(F.branchLabel({ state: "merged", dirty: 3 }), "merged");

// A count opens the list it counts; Back comes home.
count("needs").click();
assert.equal(F.S.view, "list:needs");
const sheet = root.querySelector(".fl-sheet");
assert.equal(sheet.hidden, false);
assert.match(sheet.textContent, /Waiting on you/);
assert.match(sheet.textContent, /Session n1/);
F.back();
assert.equal(F.S.view, null);
assert.equal(root.querySelector(".fl-lanes").hidden, false);

// The tool filter narrows the lanes.
F.S.tool = "codex";
F.render();
assert.deepEqual(root.querySelector(".fl-lanes").querySelectorAll(".fl-lane-name").map((n) => n.textContent), ["beta"]);
F.S.tool = "all";

// The repos frame feeds the other tabs.
F.onFrame("fleetrepos", repos);
F.S.tab = "calls";
F.render();
const rows = root.querySelector(".fl-lanes").querySelectorAll(".fl-tr");
assert.equal(rows.length, 3, "a header and two call rows");
assert.match(rows[1].textContent, /library tagging/);
F.S.tab = "sessions";

// A tick keeps the strip live without a new snapshot.
F.S.receivedAt = Date.now() - 60e3;
F.onFrame("tick");
assert.ok(Date.now() - F.S.receivedAt < 1000);

// A missing source says so in the bottom strip.
assert.match(root.querySelector(".fl-bottom").textContent, /Codex not found on this machine/);

// The Analyze read: its status wins the feature chip, a session it flags
// joins the needs-you row as a soft card, and the button says where it stands.
F.S.ai.read = { at: NOW - 5 * MIN, projects: { alpha: { status: "One feature open." } },
  features: { w1: { status: "stalled", label: "The W feature", next: "Merge PR 4" } },
  sessions: { i1: { needsUser: true, why: "Its tests fail" } } };
F.render();
const lanes2 = root.querySelector(".fl-lanes");
assert.equal(F.famStateChip(snap.families[0]).textContent, "Stalled");
assert.match(lanes2.querySelector(".fl-fam-ai").textContent, /Next: Merge PR 4/);
assert.match(lanes2.querySelector(".fl-ai-note").textContent, /One feature open\./);
const soft = lanes2.querySelectorAll(".fl-qcard").filter((c) => c.classList.contains("soft"));
assert.equal(soft.length, 1);
assert.match(soft[0].textContent, /Its tests fail/);
assert.match(soft[0].textContent, /From the AI read/);
const aiBox = root.querySelector(".fl-ai");
F.renderAi();
assert.match(aiBox.textContent, /Last read/);
assert.equal(aiBox.querySelector(".fl-ai-btn").textContent, "Analyze again");
F.S.ai.running = true;
F.S.ai.startedAt = Date.now();
F.renderAi();
assert.match(aiBox.textContent, /Reading your sessions/);
assert.equal(aiBox.querySelector(".fl-ai-btn"), null, "no second run while one is going");

console.log("fleet UI: ok");

// Attention's verbs, run against the REAL app.js functions in a VM (the
// tests/approve_builds_ui.js pattern). Work waiting on the owner lives in
// Attention only, so opening one of its branches or sessions opens that
// entry in Fleet > History (Work > Results retired 2026-11-04) and never
// raises the Work window.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
let appsOpened = [], workTabs = [], inspected = [], sessionsOpened = [];
const context = vm.createContext({
  console,
  $: () => null,
  openApp: (id) => appsOpened.push(id),
  setWorkTab: (tab) => workTabs.push(tab),
  setAttentionTab() {}, startPoll() {}, openNote() {},
  openSession: (id) => sessionsOpened.push(id),
  traceFlowRun() {}, openOrphanFocus() {}, revealOrphan() {},
  window: {
    // What History was asked to open, copied out as plain data (objects
    // built inside the VM carry its own Object prototype).
    openHistory: (o) => inspected.push(JSON.parse(JSON.stringify(o))),
  },
});
function section(start, end) {
  const at = app.indexOf(start);
  assert.ok(at >= 0, 'missing ' + start);
  return app.slice(at, app.indexOf(end, at));
}
vm.runInContext(section('function attnVerb(r) {', 'function attnKindLabel('), context);
vm.runInContext(section('const HISTORY_ALIAS = new Set(', '// Search and Brain folded'), context);
vm.runInContext(section('// A reference from an older screen', 'function workTabLoad('), context);
const verb = (row) => vm.runInContext('attnVerb', context)(row);

// A branch waiting to land opens that exact branch.
const branch = verb({ kind: 'orphan', orphan_branch: 'claude/example' });
assert.equal(branch.label, 'review');
branch.run();
assert.deepEqual(inspected, [{ entry: 'branch:claude/example' }]);

// A session row's button opens that session's window, and the card opens
// its full detail in the inspector.
const session = verb({ job_id: 'job-1234abcd', verb: 'watch' });
assert.equal(session.label, 'watch');
session.run();
assert.deepEqual(sessionsOpened, ['job-1234abcd']);
assert.equal(inspected.length, 1);
session.detail.run();
assert.deepEqual(inspected[1], { entry: 'job:job-1234abcd' });

// A row with one destination has no separate card action.
assert.equal(branch.detail, undefined);

// The rendered card: its button opens the session window, a click
// elsewhere on the card opens the full detail.
const node = (tag, cls = '', text = '') => ({
  tag, className: cls, textContent: text, children: [], listeners: {},
  get childElementCount() { return this.children.length; },
  appendChild(c) { this.children.push(c); return c; },
  setAttribute() {},
  addEventListener(type, fn) { this.listeners[type] = fn; },
});
let cardRun = null;
Object.assign(context, {
  el: node, briefDays: () => '', stageStripEl: () => null,
  cardAction: (_row, run) => { cardRun = run; },
});
vm.runInContext(section('function attnKindLabel(r) {', 'const attentionDecisionNodes'), context);
const sec = node('section');
vm.runInContext('attnRow', context)(sec, { job_id: 'job-5678efgh', verb: 'watch', title: 'x' });
const find = (n, pred) => pred(n) ? n
  : n.children.map((c) => find(c, pred)).find(Boolean);
const button = find(sec, (n) => n.tag === 'button');
assert.equal(button.textContent, 'watch');
button.listeners.click({ stopPropagation() {} });
assert.equal(sessionsOpened.at(-1), 'job-5678efgh');
const before = inspected.length;
cardRun();
assert.deepEqual(inspected[before], { entry: 'job:job-5678efgh' });
assert.equal(sessionsOpened.length, 2);

// Neither raised Work or switched its tab.
assert.deepEqual(appsOpened, []);
assert.deepEqual(workTabs, []);

console.log('attention inspect: passed');

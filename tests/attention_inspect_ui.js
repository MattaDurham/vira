// Attention's verbs, run against the REAL app.js functions in a VM (the
// tests/approve_builds_ui.js pattern). Work waiting on the owner lives in
// Attention only, so opening one of its branches or sessions shows the
// inspector over Attention and never raises the Work window behind it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
let appsOpened = [], workTabs = [], inspected = [], mounts = 0;
const context = vm.createContext({
  console,
  $: (sel) => (sel === '#work-results-root' ? {} : null),
  openApp: (id) => appsOpened.push(id),
  setWorkTab: (tab) => workTabs.push(tab),
  setAttentionTab() {}, startPoll() {}, openSession() {}, openNote() {},
  traceFlowRun() {}, openOrphanFocus() {}, revealOrphan() {},
  window: {
    // Refs built inside the VM carry its own Object prototype, so they are
    // copied out as plain data before comparing.
    ViraWorkResults: {
      mount: () => {
        mounts += 1;
        return { open: (ref) => inspected.push(JSON.parse(JSON.stringify(ref))) };
      },
    },
  },
});
function section(start, end) {
  const at = app.indexOf(start);
  assert.ok(at >= 0, 'missing ' + start);
  return app.slice(at, app.indexOf(end, at));
}
vm.runInContext(section('function attnVerb(r) {', 'function attnKindLabel('), context);
vm.runInContext(section('let workResultsController = null;', '$("#work-advanced")'), context);
const verb = (row) => vm.runInContext('attnVerb', context)(row);

// A branch waiting to land opens that exact branch.
const branch = verb({ kind: 'orphan', orphan_branch: 'claude/example' });
assert.equal(branch.label, 'review');
branch.run();
assert.deepEqual(inspected, [{ branch: 'claude/example' }]);

// A session row opens that exact session.
verb({ job_id: 'job-1234abcd' }).run();
assert.deepEqual(inspected[1], { job_id: 'job-1234abcd' });

// Neither raised Work or switched its tab, and the inspector mounted once.
assert.deepEqual(appsOpened, []);
assert.deepEqual(workTabs, []);
assert.equal(mounts, 1);

console.log('attention inspect: passed');

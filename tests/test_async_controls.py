"""Behavior regressions for request ownership and slow Config/poll responses."""
import shutil
import subprocess
import unittest
from pathlib import Path


HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
const section = (start, end) => {
  const first = app.indexOf(start), last = app.indexOf(end, first);
  assert(first >= 0 && last > first, `Missing section ${start}`);
  return app.slice(first, last);
};
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
let now = 100, timerId = 0;
const timers = new Map(), intervals = new Map(), events = {};
const setTimeout = (fn, delay) => { const id = ++timerId; timers.set(id, {fn, due: now + delay}); return id; };
const clearTimeout = (id) => timers.delete(id);
const setInterval = (fn) => { const id = ++timerId; intervals.set(id, fn); return id; };
const clearInterval = (id) => intervals.delete(id);
async function advance(ms) {
  const target = now + ms;
  for (;;) {
    const next = [...timers].filter(([,timer]) => timer.due <= target).sort((a,b) => a[1].due - b[1].due)[0];
    if (!next) break;
    const [id, timer] = next; now = timer.due; timers.delete(id); timer.fn(); await tick();
  }
  now = target; await tick();
}
class Control {
  constructor(text) {
    this.textContent = text; this.isConnected = true; this.dataset = {}; this.attributes = {};
    this.disabled = false; this.classes = new Set();
    this.classList = {add: (...names) => names.forEach((n) => this.classes.add(n)),
      remove: (...names) => names.forEach((n) => this.classes.delete(n)),
      contains: (name) => this.classes.has(name),
      toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name)};
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
}
const context = vm.createContext({console, setTimeout, clearTimeout, setInterval, clearInterval,
  performance: {now: () => now}, window: {},
  document: {addEventListener: (type, fn) => { events[type] = fn; }},
});
vm.runInContext(section('// ==================== Busy states -', '// a placeholder line'), context);
vm.runInContext(section('function startPoll(', '// ---------- whole-card activation'), context);
const click = (control, work = () => {}) => {
  const event = {button: 0, defaultPrevented: false, eventPhase: 1, target: {closest: () => control}};
  events.click(event); event.eventPhase = 2;
  try { work(); } finally { event.eventPhase = 0; }
};
const busy = (control) => control.classes.has('is-busy');

(async () => {
  // A disclosure with no request cannot own a poll, even at the same timestamp.
  const header = new Control('Research connected writable Settings');
  click(header);
  const unrelated = deferred(); context.busyTrack(unrelated.promise);
  await advance(250);
  assert(!busy(header));
  assert(!header.attributes['aria-busy']);

  // An owned request may show the wheel, but later polls cannot extend its life.
  const save = new Control('Save settings'), mutation = deferred(), poll = deferred();
  click(save, () => context.busyTrack(mutation.promise));
  context.busyTrack(poll.promise);
  await advance(225); assert(busy(save));
  mutation.resolve({saved: true}); await tick();
  const latePoll = deferred(); context.busyTrack(latePoll.promise);
  await advance(500);
  assert(!busy(save), 'unresolved background requests cannot keep a finished save spinning');
  assert(!save.attributes['aria-busy']);

  // Explicit ownership covers an intentional async chain without adopting polls.
  const chainControl = new Control('Refresh'), first = deferred(), second = deferred();
  const chain = context.busyWhile(chainControl, async () => { await first.promise; return second.promise; });
  await advance(1); assert(busy(chainControl));
  first.resolve(); await tick();
  context.busyTrack(unrelated.promise);
  await advance(600); assert(busy(chainControl), 'the explicit operation still owns its second step');
  second.resolve('done'); assert.equal(await chain, 'done');
  await advance(5); assert(!busy(chainControl));

  const optedOut = new Control('Advanced settings'); optedOut.dataset.busy = 'off';
  click(optedOut, () => context.busyTrack(unrelated.promise));
  await advance(250); assert(!busy(optedOut));

  // Rejected work clears its own indicator as well.
  const failed = new Control('Retry'), rejection = deferred();
  click(failed, () => context.busyTrack(rejection.promise));
  await advance(1); assert(busy(failed));
  rejection.reject(new Error('Request failed')); await tick(); await advance(500);
  assert(!busy(failed));

  // Polling is single-flight: ten timer ticks cannot queue ten slow HTTP reads.
  let reads = 0; const slowRead = deferred();
  const handle = context.startPoll(() => { reads++; return slowRead.promise; }, 5);
  const pollTick = intervals.get(handle._t);
  for (let n = 0; n < 10; n++) pollTick();
  assert.equal(reads, 1);
  slowRead.resolve(); await tick(); pollTick(); assert.equal(reads, 2);
  handle.stop(); await tick(); pollTick(); assert.equal(reads, 2);
  assert.equal(handle._t, null);

  // An in-flight stop prevents restart even after settlement or a queued tick.
  const finalRead = deferred(); let stoppedReads = 0;
  const stopped = context.startPoll(() => { stoppedReads++; return finalRead.promise; }, 5);
  const queuedTick = intervals.get(stopped._t);
  queuedTick(); stopped.stop(); finalRead.resolve(); await tick(); queuedTick();
  assert.equal(stoppedReads, 1);

  // Both synchronous failures and rejected promises leave the next tick usable.
  let errors = 0;
  const recover = context.startPoll(() => {
    errors++; if (errors === 1) throw new Error('sync');
    if (errors === 2) return Promise.reject(new Error('async'));
  }, 5);
  const recoverTick = intervals.get(recover._t);
  recoverTick(); recoverTick(); await tick(); recoverTick();
  assert.equal(errors, 3); recover.stop();
  const expires = context.startPoll(() => {}, 5, 10);
  await advance(11); assert.equal(expires._t, null);

  // Polling, SSE pokes, and manual refreshes all share one attention request.
  // Changes arriving during it coalesce to one trailing snapshot, while
  // ordinary polling does not create a self-sustaining refresh loop.
  const attentionCalls = [], attentionPaints = [];
  let activeReads = 0, peakReads = 0, cleanupPoke = false;
  const attention = vm.createContext({
    api: (url) => {
      assert.equal(url, '/api/attention');
      const call = deferred(); attentionCalls.push(call);
      activeReads++; peakReads = Math.max(peakReads, activeReads);
      return call.promise.finally(() => { activeReads--; });
    },
    alertRows: [], alertMin: new Set(), alertFront: null,
    saveAlertMin() {}, renderAlerts() {}, attnMaybeOpen() {},
    renderAttention: () => {
      attentionPaints.push(vm.runInContext('attnData.version', attention));
      if (cleanupPoke) {
        cleanupPoke = false;
        Promise.resolve().then(() => attention.refreshAlerts());
      }
    },
  });
  vm.runInContext(section('let attnData = null;', 'function alertPark('), attention);
  const snapshot = (version) => ({version, cards: [{card: {req_id: version}}]});
  const attentionWork = attention.refreshAlerts({changed: false});
  for (let n = 0; n < 10; n++) assert.equal(attention.refreshAlerts({changed: false}), attentionWork);
  assert.equal(attentionCalls.length, 1);
  for (let n = 0; n < 10; n++) assert.equal(attention.refreshAlerts(), attentionWork);
  attentionCalls[0].resolve(snapshot('initial')); await tick();
  assert.equal(attentionCalls.length, 2, 'a burst of changes requests exactly one trailing snapshot');
  for (let n = 0; n < 10; n++) attention.refreshAlerts({changed: false});
  attentionCalls[1].resolve(snapshot('latest')); await attentionWork;
  assert.equal(attentionCalls.length, 2, 'timer ticks do not extend the trailing refresh');
  assert.equal(vm.runInContext('attnData.version', attention), 'latest');

  // A real change during the trailing snapshot is not lost or overwritten.
  const newerWork = attention.refreshAlerts(); attention.refreshAlerts();
  attentionCalls[2].resolve(snapshot('before-change')); await tick();
  attention.refreshAlerts(); attention.refreshAlerts();
  attentionCalls[3].resolve(snapshot('during-change')); await tick();
  assert.equal(attentionCalls.length, 5);
  attentionCalls[4].resolve(snapshot('after-change')); await newerWork;
  assert.equal(vm.runInContext('attnData.version', attention), 'after-change');
  assert.equal(peakReads, 1, 'cross-caller refreshes never overlap HTTP requests');

  // Failed reads preserve decisions, release ownership, and do not prevent
  // either a queued event refresh or a later normal poll from succeeding.
  const beforeFailure = attention.alertRows;
  const failedAttention = attention.refreshAlerts();
  attentionCalls[5].reject(new Error('Attention unavailable')); await failedAttention;
  assert.equal(attention.alertRows, beforeFailure);
  assert.equal(vm.runInContext('attnData.version', attention), 'after-change');
  const recoveringAttention = attention.refreshAlerts({changed: false});
  attention.refreshAlerts();
  attentionCalls[6].reject(new Error('Transient failure')); await tick();
  assert.equal(attentionCalls.length, 8, 'an event queued before failure is still refreshed');
  assert.equal(attention.alertRows, beforeFailure);
  attentionCalls[7].resolve(snapshot('recovered')); await recoveringAttention;
  assert.equal(vm.runInContext('attnData.version', attention), 'recovered');
  assert.equal(peakReads, 1);
  assert.deepEqual(attentionPaints, ['initial', 'latest', 'before-change', 'during-change', 'after-change', 'recovered']);
  cleanupPoke = true;
  const cleanupWork = attention.refreshAlerts();
  attentionCalls[8].resolve(snapshot('before-cleanup-poke')); await tick();
  assert.equal(attentionCalls.length, 10, 'a change between paint and cleanup is not lost');
  attentionCalls[9].resolve(snapshot('after-cleanup-poke')); await cleanupWork;
  assert.equal(vm.runInContext('attnData.version', attention), 'after-cleanup-poke');

  // Saved feedback and enabled controls do not wait for a slow status refresh.
  const toasts = [], work = deferred(), refresh = deferred(); let refreshes = 0, applied = 0;
  const setup = vm.createContext({
    busyWhile: (_node, fn) => Promise.resolve(fn()),
    toast: (text) => toasts.push(text),
    loadSetup: () => { refreshes++; return refresh.promise; },
  });
  vm.runInContext(section('async function setupAct(', '// ---- render: the Config dashboard'), setup);
  const action = new Control('Save settings');
  let complete = false;
  const running = setup.setupAct(action, () => work.promise, () => 'Settings saved').then((result) => { complete = true; return result; });
  assert(action.disabled); assert.equal(action.textContent, 'working…');
  let duplicates = 0;
  assert.equal(await setup.setupAct(action, () => { duplicates++; }), null);
  assert.equal(duplicates, 0, 'a pending action cannot submit twice');
  work.resolve({id: 'research'}); await tick();
  assert(!action.disabled); assert.equal(action.textContent, 'Save settings');
  assert.deepEqual(toasts, ['Settings saved']); assert.equal(refreshes, 1);
  assert(!complete, 'the refresh is genuinely still pending');
  refresh.reject(new Error('Status timeout')); await running;
  assert.match(toasts.at(-1), /Action completed, but Config could not refresh/);
  const local = new Control('Save settings');
  const saved = await setup.setupAct(local, async () => ({id: 'local'}), () => 'Local saved',
    {refresh: false, onSaved: (result) => { applied++; assert.equal(result.id, 'local'); }});
  assert.equal(saved.id, 'local'); assert.equal(applied, 1); assert.equal(refreshes, 1);
  await setup.setupAct(local, async () => { throw new Error('Save refused'); }, () => 'Must not show',
    {refresh: false, onSaved: () => { applied++; }});
  assert.equal(applied, 1); assert.equal(toasts.at(-1), 'Save refused'); assert(!local.disabled);

  // Vault mutations alone have a deadline, covering a queued fetch or stalled
  // response body. The transport deliberately ignores abort in this harness.
  const vaultCalls = [];
  const mutations = vm.createContext({AbortController, setTimeout, clearTimeout,
    apiRaw: (url, options) => {
      const call = deferred(); vaultCalls.push({...call, url, options}); return call.promise;
    },
  });
  vm.runInContext(section('const VAULT_MUTATION_TIMEOUT_MS', 'function brainNoteCount('), mutations);
  const timerBaseline = timers.size;
  const successfulSave = mutations.brainVaultMutation('/api/vault/sources', {id: 'notes'});
  assert.equal(timers.size, timerBaseline + 1);
  assert.equal(vaultCalls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(vaultCalls[0].options.body), {id: 'notes'});
  vaultCalls[0].resolve({id: 'notes'}); await successfulSave;
  assert.equal(timers.size, timerBaseline, 'successful saves clear their deadline');
  assert(!vaultCalls[0].options.signal.aborted);

  const timedButton = new Control('Save settings');
  const timedSave = setup.setupAct(timedButton,
    () => mutations.brainVaultMutation('/api/vault/sources', {id: 'notes'}), () => 'Must not claim success',
    {refresh: false, onSaved: () => { applied++; }});
  await advance(14999); assert(timedButton.disabled);
  await advance(1); await timedSave;
  assert(vaultCalls[1].options.signal.aborted);
  assert(!timedButton.disabled); assert.equal(timedButton.textContent, 'Save settings');
  assert.match(toasts.at(-1), /not confirmed whether your vault settings were saved/);
  assert.match(toasts.at(-1), /Reopen Config to check before trying again/);
  assert.equal(vaultCalls.length, 2, 'timeouts never retry a mutation automatically');
  assert.equal(applied, 1, 'an uncertain result cannot update local configuration');
  assert.equal(timers.size, timerBaseline);
  vaultCalls[1].reject(new Error('Transport rejected after abort')); await tick();
  assert(!toasts.includes('Must not claim success'));

  const uncertainDisconnect = mutations.brainVaultMutation('/api/vault/sources/notes', undefined,
    {method: 'DELETE', uncertainty: 'the vault was disconnected'});
  const disconnectFailure = assert.rejects(uncertainDisconnect, /whether the vault was disconnected/);
  assert.equal(vaultCalls[2].options.method, 'DELETE');
  assert(!('body' in vaultCalls[2].options));
  await advance(15000); await disconnectFailure;
  vaultCalls[2].resolve({removed: true}); await tick();
  assert.equal(timers.size, timerBaseline);

  const refusedSave = mutations.brainVaultMutation('/api/vault/sources', {id: 'notes'});
  const refusal = assert.rejects(refusedSave, /Protected inbox/);
  vaultCalls[3].reject(new Error('Protected inbox')); await refusal;
  assert.equal(timers.size, timerBaseline, 'server rejection also clears the deadline');

  // Exercise the real click tracker + setupAct + bounded mutation together.
  // The raw request stays unresolved after abort; only the bounded operation
  // may belong to the button, otherwise its spinner survives the timeout.
  const rawStall = deferred(); let trackedTransports = 0;
  context.AbortController = AbortController;
  context.apiRaw = () => rawStall.promise;
  context.api = () => { trackedTransports++; return context.busyTrack(rawStall.promise); };
  context.toast = (text) => toasts.push(text);
  context.loadSetup = async () => { throw new Error('Unexpected full refresh'); };
  vm.runInContext(section('const VAULT_MUTATION_TIMEOUT_MS', 'function brainNoteCount('), context);
  vm.runInContext(section('async function setupAct(', '// ---- render: the Config dashboard'), context);
  const boundedButton = new Control('Save settings'); let boundedWork;
  click(boundedButton, () => {
    boundedWork = context.setupAct(boundedButton,
      () => context.brainVaultMutation('/api/vault/sources', {id: 'notes'}),
      () => 'Must not claim success', {refresh: false});
  });
  await advance(225); assert(busy(boundedButton));
  await advance(14780); await boundedWork;
  assert.equal(trackedTransports, 0, 'the unbounded transport cannot attach to the click');
  assert(!busy(boundedButton), 'timeout clears the actual indicator even when abort is ignored');
  assert(!boundedButton.disabled); assert.equal(boundedButton.textContent, 'Save settings');
  rawStall.reject(new Error('Late abort')); await tick();
  console.log('Busy ownership, Config actions, and single-flight polling passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


class AsyncControlTests(unittest.TestCase):
    def test_busy_ownership_saves_and_single_flight_polling(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "-e", HARNESS], cwd=root, capture_output=True,
            text=True, encoding="utf-8", timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

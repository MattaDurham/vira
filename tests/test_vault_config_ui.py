"""Run the actual vault Config renderer against old and current server payloads."""
import shutil
import subprocess
import unittest
from pathlib import Path


HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');

class Element {
  constructor(tag, className = '', text = '') {
    this.tag = tag; this.className = className; this._text = text;
    this.children = []; this.dataset = {}; this.attributes = {};
    this.checked = false; this.disabled = false; this.value = '';
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(' '); }
  set textContent(value) { this._text = value; this.children = []; }
  appendChild(child) {
    if (child.parentElement) {
      const siblings = child.parentElement.children;
      siblings.splice(siblings.indexOf(child), 1);
    }
    this.children.push(child); child.parentElement = this; return child;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  replaceChildren(...children) { this.children = []; this._text = ''; children.forEach((child) => this.appendChild(child)); }
}
const all = (node) => [node, ...node.children.flatMap(all)];
const tagged = (node, tag) => all(node).filter((n) => n.tag === tag);
const byClass = (node, name) => all(node).filter((n) => n.className.split(' ').includes(name));
const field = (node, name) => all(node).find((n) => n.name === name);
let posts = [], requests = [], loads = 0, toasts = [], choices = [], pickerCalls = [];
let deadlineId = 0;
const deadlines = new Map();
const choose = async (options) => { pickerCalls.push(options); return choices.shift() || {cancelled: true}; };
const mutationReply = (url, body) => {
  posts.push({url, body});
  if (url.endsWith('/default-destination')) return {default_destination: body.source_id};
  const normalized = {...source({id: body.id || 'new', primary: body.id === 'primary',
    contexts: [], write_dirs: [], protected_dirs: [], model_exclude_dirs: []}),
    ...body, root: body.path, read_only: !body.write_enabled};
  delete normalized.notes; delete normalized.notes_capped;
  return normalized;
};
const context = vm.createContext({
  AbortController,
  setTimeout: (callback) => { const id = ++deadlineId; deadlines.set(id, callback); return id; },
  clearTimeout: (id) => deadlines.delete(id),
  FolderPicker: { choose },
  el: (tag, cls, text) => new Element(tag, cls, text),
  fmtNum: (n) => String(n),
  CSS: { escape: (text) => text },
  document: { querySelector: () => null },
  setupSt: null, setupFlow: null, setupVaultRevision: 0,
  busyWhile: (_button, fn) => Promise.resolve(fn()),
  loadSetup: async () => { loads++; },
  toast: (text) => { toasts.push(text); },
  post: async (url, body) => mutationReply(url, body),
  api: async (url, options) => {
    if (options?.method === 'POST') return mutationReply(url, JSON.parse(options.body));
    requests.push({url, options});
    return options?.method === 'DELETE' ? {id: url.split('/').at(-1), removed: true} : {};
  },
});
context.apiRaw = context.api;
function section(start, end) {
  const first = app.indexOf(start);
  assert(first >= 0, `Missing application section ${start}`);
  const last = app.indexOf(end, first);
  assert(last > first, `Missing section boundary ${end}`);
  return app.slice(first, last);
}
vm.runInContext(section('async function setupAct(', '// ---- render: the Config dashboard'), context);
vm.runInContext(section('function setupRecount(', 'async function loadSetup('), context);
vm.runInContext(section('let brainOpenSource = null;', 'function cardMail('), context);
const render = (vault) => {
  const card = new Element('div');
  card.state = {vault, platform: 'mac'}; card.step = {};
  context.cardBrain(card, card.step, card.state);
  return card;
};
const legacy = {root: '/fixture/research', connected: true, sources: [
  {id: 'primary', name: 'Research', root: '/fixture/research', primary: true,
   connected: true, read_only: false, notes: 3, removable: true},
  {id: 'journal', name: 'Journal', root: '/fixture/journal', primary: false,
   connected: true, read_only: true, legacy: true, notes: 2, removable: true},
]};
const source = (overrides = {}) => ({
  id: 'primary', name: 'Research', root: '/fixture/research', primary: true,
  connected: true, read_only: false, removable: true, notes: 3,
  read_enabled: true, write_enabled: true, model_exposure: true,
  write_scope: 'selected', allow_publish: false, default_destination: false, purpose: 'Reference material',
  contexts: ['research'], capture_dir: 'inbox', write_dirs: ['inbox', 'wiki'],
  protected_dirs: ['wiki/canon'], model_exclude_dirs: ['private'], ...overrides,
});
const current = (overrides = {}) => ({
  root: '/fixture/research', policy_version: 2, default_destination: '',
  sources: [source()], ...overrides,
});
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function submit(form) {
  let prevented = false;
  form.onsubmit({preventDefault: () => { prevented = true; }});
  assert(prevented, 'Config form prevents browser navigation');
  await tick();
}
function assertBlocked(card) {
  assert.equal(tagged(card, 'form').length, 0, 'unsupported settings cannot be submitted');
  assert.equal(tagged(card, 'input').length, 0, 'unsupported settings cannot be edited');
  assert.deepEqual(tagged(card, 'button').map((n) => n.textContent), ['Check again']);
  assert.match(card.textContent, /Restart Vira/);
}

(async () => {
  // This is the real pre-policy shape: no permission/default fields at all.
  const oldCard = render(legacy);
  assertBlocked(oldCard);
  const states = byClass(oldCard, 'setup-prov-state').map((n) => n.textContent);
  assert.deepEqual(states, ['connected · writable', 'connected · read only']);
  assert(!oldCard.textContent.includes('reading off'), 'missing read_enabled is unknown, not off');
  assert(!oldCard.textContent.includes('model access off'), 'missing model_exposure is unknown, not off');
  await tagged(oldCard, 'button')[0].onclick();
  assert.equal(loads, 1, 'Check again re-reads setup through the existing refresh path');
  assert.equal(posts.length + requests.length, 0, 'legacy view cannot mutate vault configuration');

  // Even a capability marker cannot make a partial policy payload editable.
  assertBlocked(render({...legacy, policy_version: 1, default_destination: 'missing'}));
  assertBlocked(render({root: '', sources: []}));
  for (const policy_version of [0, 1, '2', null]) {
    assertBlocked(render(current({policy_version})));
  }
  for (const missing of ['model_exclude_dirs', 'purpose', 'id']) {
    const incomplete = source();
    delete incomplete[missing];
    assertBlocked(render(current({sources: [incomplete]})));
  }

  // Whole-vault writing requires the version-2 backend; older assets must
  // not submit an all-scope request that the old server would ignore.
  const priorCurrent = current();
  delete priorCurrent.policy_version;
  assertBlocked(render(priorCurrent));

  const supportedCard = render(current({sources: [source(), source({
    id: 'journal', name: 'Journal', root: '/fixture/journal', primary: false,
    write_enabled: false, read_only: true, contexts: [], write_dirs: [],
  })]}));
  const forms = byClass(supportedCard, 'vault-config-form');
  assert.equal(forms.length, 2);
  assert.equal(byClass(supportedCard, 'vault-config-add').length, 1);
  assert(tagged(supportedCard, 'summary').every((node) => node.dataset.busy === 'off'),
    'vault disclosure controls never enter network busy tracking');
  const form = forms[0];
  assert.equal(field(form, 'read_enabled').checked, true);
  assert.equal(field(form, 'write_enabled').checked, true);
  assert.equal(field(form, 'model_exposure').checked, true);
  assert.equal(field(form, 'allow_publish').checked, false);
  const fallback = tagged(byClass(form, 'vault-config-default')[0], 'input')[0];
  assert.equal(fallback.checked, false, 'rendering does not opt into a fallback');
  field(form, 'name').value = 'Research library';
  field(form, 'contexts').value = 'research, study';
  field(form, 'model_exposure').checked = false;
  await submit(form);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/vault/sources');
  const body = posts[0].body;
  assert.equal(body.write_scope, 'selected', 'saving other settings preserves existing restrictions');
  assert.equal(body.id, 'primary', 'saving primary edits it rather than adding another source');
  assert.equal(body.path, '/fixture/research');
  assert.equal(body.name, 'Research library');
  assert.deepEqual(Array.from(body.contexts), ['research', 'study']);
  assert.deepEqual(Array.from(body.write_dirs), ['inbox', 'wiki']);
  assert.deepEqual(Array.from(body.protected_dirs), ['wiki/canon']);
  assert.deepEqual(Array.from(body.model_exclude_dirs), ['private']);
  assert.equal(body.model_exposure, false);
  assert.equal(body.read_enabled, true);
  assert.equal(body.write_enabled, true);
  assert.equal(body.default_destination, false);
  assert.equal(loads, 1, 'saving uses its normalized response without a global Config refresh');
  assert.equal(supportedCard.state.vault.sources[0].name, 'Research library');
  assert.equal(supportedCard.state.vault.sources[0].notes, 3, 'same-root saves retain a known count');
  assert(byClass(supportedCard, 'setup-prov-name').some((node) => node.textContent === 'Research library'));
  assert(!byClass(supportedCard, 'vault-config-save')[0].disabled);

  const secondFallback = tagged(byClass(forms[1], 'vault-config-default')[0], 'input')[0];
  assert(secondFallback.disabled, 'a read-only vault cannot become a fallback');
  field(forms[1], 'write_enabled').checked = true;
  field(forms[1], 'write_enabled').onchange();
  assert.equal(secondFallback.disabled, false);
  assert.equal(field(forms[1], 'write_scope').value, 'all', 'enabling writing on an unconfigured vault needs no allowlist');
  secondFallback.checked = true;
  await submit(forms[1]);
  assert.equal(posts[1].body.id, 'journal');
  assert.equal(posts[1].body.default_destination, true);
  assert.equal(posts[1].body.write_enabled, true);
  assert.equal(supportedCard.state.vault.default_destination, 'journal');
  assert.equal(supportedCard.state.vault.sources[0].default_destination, false);
  assert.equal(supportedCard.state.vault.sources[1].default_destination, true);

  // Every folder uses a picker, including arrays with comma-bearing names.
  assert(!field(form, 'path'), 'root path cannot be manually typed');
  assert(!field(form, 'capture_dir'), 'capture folder cannot be manually typed');
  assert(!field(form, 'write_dirs'), 'folder lists cannot be manually typed');
  const protects = tagged(form, 'button').find((n) => n.attributes['aria-label'] === 'Add to protected folders');
  choices.push({path: '/fixture/research/Legal, final', relative: 'Legal, final'});
  await protects.onclick();
  assert.equal(pickerCalls.at(-1).root, '/fixture/research');
  assert.equal(pickerCalls.at(-1).allowRoot, false);
  await submit(form);
  assert.deepEqual(Array.from(posts.at(-1).body.protected_dirs), ['wiki/canon', 'Legal, final']);
  field(form, 'write_scope').value = 'all';
  field(form, 'write_scope').onchange();
  await submit(form);
  assert.equal(posts.at(-1).body.write_scope, 'all');
  assert.deepEqual(Array.from(posts.at(-1).body.protected_dirs), ['wiki/canon', 'Legal, final']);

  const restricted = render(current({sources: [source({write_enabled: false})]}));
  const restrictedForm = byClass(restricted, 'vault-config-form')[0];
  field(restrictedForm, 'write_enabled').checked = true;
  field(restrictedForm, 'write_enabled').onchange();
  await submit(restrictedForm);
  assert.equal(posts.at(-1).body.write_scope, 'selected');
  assert.deepEqual(Array.from(posts.at(-1).body.write_dirs), ['inbox', 'wiki']);

  // First and third vaults share the same simple connection form.
  const empty = render(current({root: '', sources: []}));
  const add = byClass(empty, 'vault-config-add')[0];
  const connect = tagged(add, 'button').find((n) => n.textContent === 'Connect vault');
  assert(connect.disabled, 'cannot connect before selecting a folder');
  const picker = byClass(add, 'vault-folder-choice')[0];
  await picker.onclick();
  assert(connect.disabled, 'cancel preserves empty selection');
  choices.push({path: '/fixture/Personal notes'});
  await picker.onclick();
  assert.equal(field(add, 'name').value, 'Personal notes');
  assert.equal(connect.disabled, false);
  await submit(add);
  assert.equal(posts.at(-1).url, '/api/vault/sources');
  assert.equal(posts.at(-1).body.path, '/fixture/Personal notes');
  assert.equal(posts.at(-1).body.write_enabled, true);
  assert.equal(posts.at(-1).body.read_enabled, true);
  assert.equal(posts.at(-1).body.write_scope, 'all');
  assert.equal(posts.at(-1).body.model_exposure, false, 'new private folders are not shared automatically');
  assert.equal(posts.at(-1).body.capture_dir, 'inbox');
  assert.equal(posts.at(-1).body.connect_only, true);
  assert.equal(loads, 1, 'connecting does not scan all vaults or probe every provider');
  assert.equal(empty.state.vault.sources.length, 1);
  assert.match(empty.textContent, /File count has not been checked yet/);
  choices.push({path: '/fixture/Second folder'});
  await picker.onclick();
  assert.equal(field(add, 'name').value, 'Second folder');
  field(add, 'name').value = 'My custom name';
  choices.push({path: '/fixture/Third folder'});
  await picker.onclick();
  assert.equal(field(add, 'name').value, 'My custom name');

  // A moved root cannot retain the old folder's count. Disconnect preserves
  // a missing default until the owner explicitly clears it.
  const movedCard = render(current({default_destination: 'primary', sources: [source({default_destination: true})]}));
  const movedForm = byClass(movedCard, 'vault-config-form')[0];
  choices.push({path: '/fixture/moved'});
  await byClass(movedForm, 'vault-folder-choice')[0].onclick();
  await submit(movedForm);
  assert.equal(movedCard.state.vault.root, '/fixture/moved');
  assert.equal(movedCard.state.vault.sources[0].notes, undefined);
  assert.equal(movedCard.state.vault.notes, null);
  assert.match(movedCard.textContent, /File count has not been checked yet/);
  await tagged(movedCard, 'button').find((node) => node.textContent === 'Disconnect').onclick();
  assert.equal(movedCard.state.vault.sources.length, 0);
  assert.equal(movedCard.state.vault.root, '');
  assert.equal(movedCard.state.vault.default_destination, 'primary');
  await tagged(movedCard, 'button').find((node) => node.textContent === 'Clear unavailable default').onclick();
  assert.equal(movedCard.state.vault.default_destination, '');
  assert.equal(loads, 1, 'disconnecting and clearing defaults also avoid full Config refresh');

  // An uncertain save leaves the current form and all draft edits in place,
  // shows an inline explanation, and cannot apply a late successful response.
  const timeoutCard = render(current());
  const timeoutForm = byClass(timeoutCard, 'vault-config-form')[0];
  field(timeoutForm, 'purpose').value = 'Keep this draft after a timeout';
  const fastApi = context.apiRaw;
  let finishLateSave;
  context.apiRaw = () => new Promise((resolve) => { finishLateSave = resolve; });
  await submit(timeoutForm);
  const timeoutSave = byClass(timeoutForm, 'vault-config-save')[0];
  assert(timeoutSave.disabled);
  assert.equal(deadlines.size, 1);
  for (const [id, callback] of [...deadlines]) { deadlines.delete(id); callback(); }
  await tick();
  assert(!timeoutSave.disabled);
  assert.equal(timeoutSave.textContent, 'Save settings');
  assert.equal(field(timeoutForm, 'purpose').value, 'Keep this draft after a timeout');
  assert.match(byClass(timeoutForm, 'vault-config-error')[0].textContent, /not confirmed whether your vault settings were saved/);
  assert.equal(byClass(timeoutCard, 'vault-config-form')[0], timeoutForm);
  finishLateSave(source({name: 'Late server response'})); await tick();
  assert.equal(timeoutCard.state.vault.sources[0].name, 'Research');
  assert.equal(deadlines.size, 0);
  context.apiRaw = fastApi;

  // The installed dashboard updates its step counts and gates from the saved
  // source, with no provider/status requests or fabricated note count.
  const installed = render(current({root: '', sources: []}));
  let paints = 0, unlocks = 0, gates = 0;
  context.setupSt = installed.state;
  context.setupFlow = {steps: [installed.step, {state: 'skipped'}], done: 0, total: 1};
  context.renderSetup = (flow, state) => { paints++; assert.equal(state, installed.state); };
  context.launchUnlocked = () => { unlocks++; };
  context.refreshGates = () => { gates++; };
  const savedSource = source(); delete savedSource.notes;
  context.brainApplyVaultChange(installed, installed.step, installed.state, savedSource);
  assert.equal(installed.state.vault.root, '/fixture/research');
  assert.equal(installed.state.vault.notes, null);
  assert.equal(context.setupFlow.done, 1);
  assert.equal(context.setupFlow.total, 1);
  assert.equal(context.setupFlow.complete, true);
  assert.deepEqual([paints, unlocks, gates], [1, 1, 1]);
  assert.equal(loads, 1);

  // Reopening Config while a save is pending replaces both status and flow.
  // The completed save must update that new dashboard and keep its newer
  // counts/other sources, rather than repainting the detached old card.
  const staleCard = render(current());
  staleCard.step.id = 'brain'; staleCard.step.state = 'done';
  context.setupSt = staleCard.state;
  context.setupFlow = {steps: [staleCard.step], done: 1, total: 1};
  const staleForm = byClass(staleCard, 'vault-config-form')[0];
  field(staleForm, 'name').value = 'Saved during reopen';
  let finishReopenedSave;
  context.apiRaw = () => new Promise((resolve) => { finishReopenedSave = resolve; });
  await submit(staleForm);
  const reopenedCard = render(current({sources: [source({notes: 27}), source({id: 'other',
    primary: false, name: 'Another vault', root: '/fixture/other', notes: 8})]}));
  reopenedCard.step.id = 'brain'; reopenedCard.step.state = 'todo';
  const reopenedFlow = {steps: [reopenedCard.step, {state: 'skipped'}], done: 0, total: 1};
  context.setupSt = reopenedCard.state; context.setupFlow = reopenedFlow;
  let currentPaints = 0;
  context.renderSetup = (flow, state) => {
    currentPaints++; assert.equal(flow, reopenedFlow); assert.equal(state, reopenedCard.state);
  };
  const reopenedResult = source({name: 'Saved during reopen'}); delete reopenedResult.notes;
  finishReopenedSave(reopenedResult); await tick();
  assert.equal(currentPaints, 1);
  assert.equal(reopenedCard.state.vault.sources[0].name, 'Saved during reopen');
  assert.equal(reopenedCard.state.vault.sources[0].notes, 27);
  assert.equal(reopenedCard.state.vault.sources[1].id, 'other');
  assert.equal(reopenedCard.step.state, 'done');
  assert.equal(reopenedFlow.done, 1);
  assert.equal(staleCard.state.vault.sources[0].name, 'Research', 'obsolete state is not the save target');
  assert.equal(byClass(staleCard, 'vault-config-form')[0], staleForm, 'detached card is not repainted');
  context.apiRaw = fastApi;

  // The opposite ordering matters too: a global status read begun before
  // the save must not overwrite it when the older snapshot arrives later.
  const statusCalls = [], statusPaints = [], pollCallbacks = [];
  const statusReader = vm.createContext({
    $: () => ({}), setupFlow: null,
    api: (path) => new Promise((resolve) => statusCalls.push({path, resolve})),
    loadSetupExtra: async () => ({}),
    renderSetup: (flow, state) => { statusReader.setupFlow = flow; statusPaints.push({flow, state}); },
    launchUnlocked() {}, refreshGates() {}, toast() {},
    startPoll: (callback) => { pollCallbacks.push(callback); return {stop() {}}; },
  });
  vm.runInContext(section('let setupPollTimer = null;', '// The config half of Setup'), statusReader);
  vm.runInContext(section('function pollSetup()', '// ---- progressive launch'), statusReader);
  vm.runInContext(section('function brainApplyVaultChange(', 'function brainPolicySupported('), statusReader);
  statusReader.seed = {vault: current(), dossiers: {running: false}};
  statusReader.seedFlow = {steps: [{id: 'brain', state: 'done', detail: 'Original'}], done: 1, total: 1};
  vm.runInContext('setupSt = seed; setupFlow = seedFlow;', statusReader);
  const loading = statusReader.loadSetup();
  assert.equal(statusCalls.length, 2);
  const savedWhileLoading = source({name: 'Saved before status reply', write_enabled: false, read_only: true});
  delete savedWhileLoading.notes;
  statusReader.brainApplyVaultChange(null, statusReader.seedFlow.steps[0], statusReader.seed, savedWhileLoading);
  const oldFlow = {steps: [{id: 'brain', state: 'done', detail: 'Old snapshot'}], done: 1, total: 1};
  const oldState = {vault: current(), dossiers: {running: false}, freshOtherStatus: 'keep this'};
  statusCalls[0].resolve(oldFlow); statusCalls[1].resolve(oldState);
  await loading;
  const afterRead = vm.runInContext('setupSt', statusReader);
  assert.equal(afterRead.vault.sources[0].name, 'Saved before status reply');
  assert.equal(afterRead.vault.sources[0].write_enabled, false);
  assert.equal(afterRead.freshOtherStatus, 'keep this', 'unrelated refreshed status still lands');
  assert.equal(statusReader.setupFlow.steps[0].state, 'todo');
  assert.equal(statusReader.setupFlow.done, 0);

  // Dossier polling uses the same protection, including a source removed
  // while its older status response is in flight.
  statusReader.pollSetup();
  const polling = pollCallbacks[0]({stop() {}});
  assert.equal(statusCalls.length, 4);
  statusReader.brainApplyVaultChange(null, statusReader.setupFlow.steps[0], afterRead,
    {id: 'primary', removed: true});
  statusCalls[2].resolve({steps: [{id: 'brain', state: 'done', detail: 'Old poll'}], done: 1, total: 1});
  statusCalls[3].resolve({vault: current(), dossiers: {running: false}, freshOtherStatus: 'poll status'});
  await polling;
  const afterPoll = vm.runInContext('setupSt', statusReader);
  assert.equal(afterPoll.vault.sources.length, 0);
  assert.equal(afterPoll.vault.root, '');
  assert.equal(afterPoll.freshOtherStatus, 'poll status');
  assert.equal(statusReader.setupFlow.steps[0].state, 'todo');
  assert.equal(statusReader.setupFlow.done, 0);
  console.log('Vault Config UI behavior passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


class VaultConfigUiTests(unittest.TestCase):
    def test_old_server_is_read_only_and_current_server_edits_policies(self):
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

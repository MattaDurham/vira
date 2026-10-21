// Exercise the real route and live-definition opener without real stores.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
const requests = [], opened = [], focused = [], tabs = [], messages = [];
const handlers = {};
const definition = {};
const roots = Object.fromEntries(['#find-define-root', '#find-mobile-define-root'].map(id => [id, {
  children: [], innerHTML: '', appendChild(node) { this.children.push(node); },
}]));
let response = async ({term}) => ({term, rung: 'vault', note: term + '.md'});
const context = vm.createContext({
  location: {hash: ''}, isDesktop: true, winState: {'find-define': {el: definition}},
  addEventListener(type, fn) { handlers[type] = fn; },
  selectedTextTarget() { throw new Error('An external lookup must not capture browser selection'); },
  findChatReleaseCompanion() {}, findChatTileWorkspace() {}, markSpawnedClear() {},
  openWindow(id) { opened.push(id); }, focusWin(el) { focused.push(el); },
  openApp(id) { opened.push(id); }, showFindChat: async () => {},
  findChatShowMobileTab(tab) { tabs.push(tab); },
  renderDefines() {}, defineDestinationPicker() {},
  $(id) { return roots[id]; }, el: (tag, cls, text) => ({textContent: text}),
  toast(message) { messages.push(message); },
  post(url, payload) { requests.push({url, payload}); return response(payload); },
});
function load(start, end) {
  const a = app.indexOf(start), b = app.indexOf(end, a);
  assert(a >= 0 && b > a);
  vm.runInContext(app.slice(a, b), context);
}
load('let defineCard = null;', 'function defineRows(');
load('async function openDefine(', '// The follow-up is');
load('const HASH_ROUTES =', '// ---------- module front doors');
function route(term) {
  context.location.hash = '#define/' + encodeURIComponent(term);
  return handlers.hashchange();
}
const state = key => vm.runInContext(key, context);

(async () => {
  // Accept the URL emitted by the actual macOS script as well as plain terms.
  const nativeURL = process.argv[2];
  if (nativeURL) {
    context.location.hash = new URL(nativeURL).hash;
    await context.routeHash();
    assert.equal(requests.at(-1).payload.term, 'C++ / naïve & 100%');
  }
  for (const term of ['Byzantine fault tolerance', 'C++', 'A/B', 'R&D', '100%', 'literal %2F', 'naïve', '日本語']) {
    await route(term);
    assert.equal(requests.at(-1).url, '/api/define');
    assert.equal(requests.at(-1).payload.term, term, 'the term survives one URL decode');
    assert.equal(requests.at(-1).payload.text, '');
    assert.equal(requests.at(-1).payload.path, '');
    assert.equal(requests.at(-1).payload.destination, '', 'normal vault destination rules apply');
    assert.equal(opened.at(-1), 'find-define', 'the existing live module opens');
    assert.equal(focused.at(-1), definition, 'the module is raised above existing windows');
    assert.equal(state('defineCard.term'), term);
    assert.equal(state('definePending'), '');
  }
  await route('  “fault\n tolerance,”  ');
  assert.equal(requests.at(-1).payload.term, 'fault tolerance');

  const before = requests.length;
  for (const hash of ['#define', '#define/', '#define/%E0%A4%A', '#define/' + 'x'.repeat(121),
    '#define/one two three four five six seven eight nine']) {
    context.location.hash = hash;
    await handlers.hashchange();
  }
  assert.equal(requests.length, before, 'invalid selections never invoke a lookup');
  assert(messages.some(m => /could not be read/.test(m)));
  assert(messages.some(m => /short phrase/.test(m)));

  context.location.hash = '#reader';
  context.routeHash();
  assert.equal(opened.at(-1), 'reader', 'existing deep links still work');

  context.isDesktop = false;
  await route('consensus');
  assert.equal(opened.at(-1), 'find');
  assert.equal(tabs.at(-1), 'define');
  assert.equal(state('defineCard.term'), 'consensus');
  context.isDesktop = true;

  response = async () => { throw new Error('Vira backend is unavailable'); };
  await route('unavailable');
  assert.equal(state('definePending'), '');
  assert(roots['#find-define-root'].children.some(n => /Vira backend is unavailable/.test(n.textContent)));

  // A second selection can arrive before the first model finishes.
  const pending = [];
  response = () => new Promise((resolve, reject) => pending.push({resolve, reject}));
  const old = route('old');
  const latest = route('latest');
  pending[1].resolve({term: 'latest'});
  await latest;
  pending[0].resolve({term: 'old'});
  await old;
  assert.equal(state('defineCard.term'), 'latest', 'an old result cannot replace the latest term');

  const failed = route('old failure');
  const good = route('current');
  pending[3].resolve({term: 'current'});
  await good;
  pending[2].reject(new Error('old request failed'));
  await failed;
  assert.equal(state('defineCard.term'), 'current', 'an old failure cannot erase the current definition');
  console.log('Definition links open the live module with intact terms and current results.');
})().catch(error => { console.error(error); process.exitCode = 1; });

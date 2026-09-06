// Browser-independent behavior checks; real application functions run in a VM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
class Element {
  constructor(tag = 'div', text = '') {
    this.tag = tag; this.textContent = text; this.children = []; this.dataset = {};
    this.listeners = {}; this._value = ''; this.disabled = false; this.checked = false;
    this.style = {}; this.classList = { toggle() {}, contains() { return false; } };
  }
  set innerHTML(value) { this.children = []; this._value = ''; }
  get innerHTML() { return ''; }
  appendChild(child) { this.children.push(child); return child; }
  insertBefore(child, before) { this.children.splice(this.children.indexOf(before), 0, child); }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get selectedOptions() { return this.children.filter((c) => c.value === this.value); }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  dispatchEvent(event) { for (const cb of this.listeners[event.type] || []) cb(event); }
  focus() { this.focused = true; }
  closest() { return null; }
  querySelector() { return this.children.find((c) => c.value === '__custom__'); }
}
const nodes = new Map();
const $ = (key) => { if (!nodes.has(key)) nodes.set(key, new Element()); return nodes.get(key); };
const el = (tag, cls, text) => new Element(tag, text);
const catalog = { active: 'openai', roster: [], providers: [
  { id: 'openai', label: 'OpenAI', connected: true, sessions: true, config_keys: { cli: 'openai_cli_model' },
    cli: [{ id: 'gpt-future', label: 'Future' }], cli_detail: 'Codex account catalog' },
  { id: 'anthropic', label: 'Claude', connected: true, sessions: true, config_keys: { cli: 'cli_model' },
    cli: [{ id: 'fable', label: 'Fable alias' }], cli_detail: 'CLI aliases' }
] };
let requests = [], posts = [], opened = [];
let jobSnapshot = { provider: 'openai', model: 'opus', status: 'running', live: true };
const context = vm.createContext({ $, el, console, setTimeout, clearTimeout,
  Event: class { constructor(type) { this.type = type; } },
  PERM_MODES: [{ id: 'manual', label: 'Manual' }, { id: 'bypassPermissions', label: 'Bypass' }],
  normPermMode: (m) => m, savedPermMode: () => 'manual',
  bindSheet: () => ({ open() {}, close() {} }),
  api: async (url) => {
    requests.push(url);
    if (url.startsWith('/api/jobs/')) return jobSnapshot;
    if (url.startsWith('/api/orphanwork/resume-prompt')) return {prompt:'Resume prepared work',cwd:'/tmp/worktree'};
    if (url.startsWith('/api/models')) return catalog;
    return { ai_provider: 'openai', openai_cli_model: 'gpt-future' };
  },
  post: async (url, payload) => { posts.push({ url, payload }); return { job_id: 'new-run' }; },
  openSession: (id) => opened.push(id), refreshJobs() {},
  prompt: () => null,
  document: { createTextNode: (text) => new Element('text', text) },
  instanceConfig: async () => ({}), renderAlerts() {}, loadOrphans() {}, toast() {},
  renderCCBanner: (host, job) => { host.textContent = context.sessionModelLabel(job); },
});
function section(start, end) { return app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start))); }
vm.runInContext(section('let _modelCat = null;', '// The Claude Code pixel-robot'), context);
vm.runInContext(section('// Preparing a session is local UI state.', 'let runAction = null;'), context);
vm.runInContext(section('// Preserve the resolved version:', '// The engine badge'), context);
vm.runInContext(section('function providerBadge(', 'let _instCfg'), context);
vm.runInContext(section('const activeTerms = {};', '// A resumed conversation continues'), context);
vm.runInContext(section('async function orphanResume(', '// Merge/discard'), context);
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  // Opening a session request pauses BEFORE any launch request.
  const pending = context.launchJob('Original instructions', '/tmp/example');
  await tick();
  assert.equal(posts.length, 0);
  assert.equal(opened.length, 0);
  assert.equal($('#launch-review-model').value, 'gpt-future');
  $('#launch-review-prompt').value = 'Read the code first.';
  $('#launch-review-extra').value = 'Focus on cancellation.';
  $('#launch-review-provider').value = 'anthropic';
  $('#launch-review-provider').onchange();
  $('#launch-review-model').value = 'fable';
  $('#launch-review-readonly').checked = true;
  $('#launch-review-go').onclick();
  assert.equal(await pending, 'new-run');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].payload.provider, 'anthropic');
  assert.equal(posts[0].payload.model, 'fable');
  assert.equal(posts[0].payload.mode, 'manual');
  assert.equal(posts[0].payload.read_only, true);
  assert.match(posts[0].payload.prompt, /Read the code first\.[\s\S]*Focus on cancellation\./);
  assert.equal(opened.length, 1);
  $('#launch-review-go').onclick();
  assert.equal(posts.length, 1, 'a double click cannot resolve twice');

  const cancelled = context.launchJob('Must never start', '/tmp/example');
  await tick();
  $('#launch-review-cancel').onclick();
  assert.equal(await cancelled, null);
  assert.equal(posts.length, 1);

  const refresh = context.reviewSessionLaunch({ prompt: 'Refresh check' });
  await tick();
  catalog.providers[0].cli.push({ id: 'gpt-new-release', label: 'New release' });
  await $('#launch-review-refresh').onclick();
  assert(requests.includes('/api/models?refresh=true'));
  assert($('#launch-review-model').children.some((o) => o.value === 'gpt-new-release'));
  assert.equal($('#launch-review-model').value, 'gpt-future', 'refresh preserves selection');
  vm.runInContext('launchReviewSheet.close()', context); // Escape uses this same close hook.
  assert.equal(await refresh, null);
  assert.equal(posts.length, 1);

  const orphan = context.orphanResume({key:'example',branch:'claude/example'});
  await tick();
  assert.equal(posts.length, 1, 'Resume prepares without posting a launch');
  assert.equal($('#launch-review-cwd').readOnly, true);
  $('#launch-review-extra').value = 'Review the open changes.';
  $('#launch-review-go').onclick();
  await orphan;
  assert.equal(posts.length, 2);
  assert.equal(posts[1].url, '/api/orphanwork/resume');
  assert.equal(posts[1].payload.key, 'example');
  assert.match(posts[1].payload.prompt, /Review the open changes/);

  assert.equal(context.sessionModelLabel({ provider: 'openai', model: 'opus' }),
    'Awaiting model confirmation');
  assert.equal(context.sessionModelLabel({ provider: 'openai', model: 'opus', model_used: 'gpt-future' }),
    'gpt-future');
  assert.equal(context.sessionModelLabel({ provider: 'anthropic', model_used: 'claude-fable-future' }),
    'claude-fable-future');
  const refs = Object.fromEntries(['banner', 'output', 'pending', 'composebar',
    'say', 'send', 'stopBtn', 'statusbar', 'led', 'scroller'].map((k) => [k, new Element()]));
  const term = context.createJobTerm('example', refs);
  await term.render();
  assert.equal(refs.banner.textContent, 'Awaiting model confirmation');
  jobSnapshot = { ...jobSnapshot, model_used: 'gpt-future' };
  await term.render();
  assert.equal(refs.banner.textContent, 'gpt-future', 'the already-open banner updates on model confirmation');
  assert(refs.statusbar.children.some((c) => c.textContent.includes('gpt-future')));
  console.log('Session launch UI behavior passed');
})().catch((e) => { console.error(e); process.exitCode = 1; });

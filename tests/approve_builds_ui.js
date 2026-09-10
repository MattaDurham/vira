// Approve & build behaviour, run against the REAL app.js functions in a VM
// (the tests/session_launch_ui.js pattern). What is pinned here would be
// invisible in a demo: that a build carries the idea's saved plan and says
// so, that the newest plan link wins, that a plan whose file is gone falls
// back to the idea alone honestly, that a planning pass is never fed the
// plan it replaces, and that the image paths still ride the build (the
// contract the retired circuit-path test used to hold).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
class Element {
  constructor(tag = 'div', text = '') {
    this.tag = tag; this.textContent = text; this.children = []; this.dataset = {};
    this.listeners = {}; this._value = ''; this.style = {}; this.disabled = false;
  }
  appendChild(child) { this.children.push(child); return child; }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get selectedOptions() { return this.children.filter((c) => c.value === this.value); }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  focus() { this.focused = true; }
}
const nodes = new Map();
const $ = (key) => { if (!nodes.has(key)) nodes.set(key, new Element()); return nodes.get(key); };
const el = (tag, cls, text) => new Element(tag, text);
let requests = [], posts = [], puts = [], opened = [];
const plans = {
  pl_bbb: { id: 'pl_bbb', title: 'Plan B', path: '/vault/plans/plan-b.md',
            markdown: '# Plan B\n\n1. Edit server/x.py\n2. Run the tests' },
  pl_gone: { id: 'pl_gone', title: 'Gone', path: '/vault/plans/gone.md',
             markdown: '', missing: true },
};
const context = vm.createContext({
  $, el, console, setTimeout, clearTimeout,
  localStorage: { getItem: () => null, setItem() {} },
  lsGet: (k, fb) => fb, lsSet() {},
  api: async (url) => {
    requests.push(url);
    const m = url.match(/^\/api\/plans\/(.+)$/);
    if (m) { if (!plans[m[1]]) throw new Error('404'); return plans[m[1]]; }
    return {};
  },
  post: async (url, payload) => { posts.push({ url, payload }); return { job_id: 'job-1234abcd' }; },
  put: async (url, payload) => { puts.push({ url, payload }); return { note: payload.note }; },
  openSession: (id) => opened.push(id), refreshJobs() {}, toast() {}, copyText() {},
  sessionQualityNote: () => '', projectPathsCache: {}, ideasCache: [], renderIdeas() {},
  bindSheet: () => ({ open() {}, close() {} }),
  reviewSessionLaunch: async () => {
    throw new Error('the review sheet must not open for a dispatch its own surface reviewed');
  },
});
function section(start, end) { return app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start))); }
vm.runInContext(section('const IDEA_PLAN_LINK', 'function ideaMatchesTags'), context);
vm.runInContext(section('function ideaExtraBlock(', '// ---------- actions ----------'), context);
vm.runInContext(section('function ideaAbout(', '// Preparing a session is local UI state.'), context);
vm.runInContext(section('async function launchJob(', 'let runAction = null;'), context);
const c = context;
const settings = (perm) => ({ cwd: '~/workspace/vira', model: '', provider: null,
                              extra: '', perm, fold: [] });
(async () => {
  // The newest plan link wins; a failed finalize carries no id at all.
  assert.equal(c.ideaPlanId({ note: 'x [plan pl_aaa: A] · replanned [plan pl_bbb: Plan B]' }), 'pl_bbb');
  assert.equal(c.ideaPlanId({ note: 'plan produced 2026-09-01 — see terminal' }), '');
  assert.equal(c.ideaPlanId({}), '');

  // The block: empty for nothing, whole for a plan, path named.
  assert.equal(c.ideaPlanBlock(null), '');
  assert.equal(c.ideaPlanBlock({ markdown: '   ' }), '');
  const blk = c.ideaPlanBlock(plans.pl_bbb);
  assert.match(blk, /THE PLAN\./);
  assert(blk.includes('/vault/plans/plan-b.md'));
  assert(blk.includes('1. Edit server/x.py'));

  // In the build prompt the plan sits after the quoted idea and BEFORE the
  // owner's extra instructions, which therefore win a disagreement.
  const it = { id: 'idea_1', text: 'Make the thing', note: '[plan pl_bbb: Plan B]',
               images: [{ path: '/abs/shot.png', name: 'shot.png' }] };
  const withPlan = c.ideaImplementPrompt(it, 'Keep it small', '~/workspace/vira',
                                         'bypassPermissions', [], plans.pl_bbb);
  const at = (s) => withPlan.indexOf(s);
  const order = [at('"""\nMake the thing\n"""'), at('THE PLAN.'),
                 at('Additional instructions from the owner'), at('Carry it out end to end:')];
  assert(order.every((i) => i >= 0), 'every section present: ' + order);
  assert.deepEqual([...order].sort((a, b) => a - b), order, 'sections in order: ' + order);
  assert(withPlan.includes('/abs/shot.png'), 'the image paths still ride the build');
  const without = c.ideaImplementPrompt(it, '', '~/workspace/vira', 'bypassPermissions', [], null);
  assert(!without.includes('THE PLAN.'));
  assert(!c.ideaPlanPrompt(it, '', '~/workspace/vira', []).includes('THE PLAN.'),
         'a planning pass is not fed the plan it replaces');

  // A build fetches the plan, carries it, names it, and stamps the idea.
  let out = await c.dispatchIdeaRun(it, 'implement', settings('bypassPermissions'));
  assert.equal(out.jid, 'job-1234abcd');
  assert.equal(out.plan.id, 'pl_bbb');
  assert(requests.includes('/api/plans/pl_bbb'));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/actions/run');
  const pl = posts[0].payload;
  assert(pl.prompt.includes('1. Edit server/x.py'));
  assert.equal(pl.idea_id, 'idea_1');
  assert.equal(pl.read_only, false);
  assert.equal(pl.publish_plan, false);
  assert.equal(pl.mode, 'bypassPermissions');
  assert.equal(pl.permission_mode, 'bypassPermissions');
  assert.match(pl.about, /Following the plan: Plan B/);
  assert.equal(pl.subject, 'Make the thing');
  assert.deepEqual(opened, ['job-1234abcd']);
  assert.equal(puts.length, 1);
  assert.match(puts[0].payload.note, /dispatched implement \d{4}-\d{2}-\d{2} from its plan \(job job-1234\)/);

  // A plan whose file is gone: the build proceeds from the idea alone, and
  // neither the prompt nor the stamp claims a plan it did not carry.
  requests = []; posts = []; puts = [];
  const gone = { id: 'idea_2', text: 'Other thing', note: '[plan pl_gone: Gone]' };
  out = await c.dispatchIdeaRun(gone, 'implement', settings('manual'));
  assert.equal(out.plan, null);
  assert(!posts[0].payload.prompt.includes('THE PLAN.'));
  assert(!puts[0].payload.note.includes('from its plan'));
  assert.equal(posts[0].payload.permission_mode, null, 'manual rung: the gate raises cards');

  // A dead plan read is the same honest fallback.
  requests = []; posts = []; puts = [];
  out = await c.dispatchIdeaRun({ id: 'idea_3', text: 'T', note: '[plan pl_nope: N]' },
                                'implement', settings('bypassPermissions'));
  assert.equal(out.plan, null);
  assert(!posts[0].payload.prompt.includes('THE PLAN.'));

  // A planning dispatch never reads the plan registry and stays read-only.
  requests = []; posts = [];
  out = await c.dispatchIdeaRun(it, 'plan', settings('bypassPermissions'));
  assert(!requests.some((u) => u.startsWith('/api/plans/')));
  assert.equal(posts[0].payload.read_only, true);
  assert.equal(posts[0].payload.publish_plan, true);
  assert.equal(out.plan, null);

  // One cwd ladder: the connected project wins over the last-used folder.
  c.projectPathsCache['Other'] = '/repos/other';
  assert.equal(c.ideaRunCwd({ project: 'Other' }), '/repos/other');
  assert.equal(c.ideaRunCwd({ project: 'Vira' }), '~/workspace/vira');
  console.log('Approve & build UI behavior passed');
})().catch((e) => { console.error(e); process.exitCode = 1; });

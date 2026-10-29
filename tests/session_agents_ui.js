// The terminal's subagent UI: lanes in the feed and the agents strip.
// Real app.js functions run in a VM against synthetic agents.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
function section(start, end) {
  const i = app.indexOf(start);
  assert.notEqual(i, -1, 'missing section start: ' + start);
  const j = app.indexOf(end, i);
  assert.notEqual(j, -1, 'missing section end: ' + end);
  return app.slice(i, j);
}
class Element {
  constructor(tag = 'div', cls = '', text = null) {
    this.tag = tag; this.className = cls || ''; this.children = []; this.dataset = {};
    this.listeners = {}; this.title = '';
    this._text = text == null ? '' : String(text);
    const self = this;
    this.classList = {
      toggle(c, on) {
        const has = self.className.split(' ').includes(c);
        const want = on === undefined ? !has : !!on;
        if (want && !has) self.className = (self.className + ' ' + c).trim();
        if (!want && has) self.className = self.className.split(' ').filter((x) => x !== c).join(' ');
      },
      add(c) { this.toggle(c, true); },
      contains(c) { return self.className.split(' ').includes(c); },
    };
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  set innerHTML(v) { this.children = []; this._text = ''; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(t, cb) { (this.listeners[t] ||= []).push(cb); }
  click() { (this.listeners.click || []).forEach((cb) => cb()); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) {
    if (sel === '[data-clock]') return this.all().filter((n) => n.dataset.clock);
    if (sel === '.agent-card.open') return this.all().filter((n) =>
      n.classList.contains('agent-card') && n.classList.contains('open'));
    throw new Error('unsupported selector ' + sel);
  }
}
const document = { createElement: (tag) => new Element(tag), createTextNode: (t) => new Element('#text', '', t) };
const el = (tag, cls, text) => new Element(tag, cls, text);
const ctx = vm.createContext({ document, el, console, Date, JSON, Map, Set });
vm.runInContext(section('function appendInline(', '// A subagent\'s line'), ctx);
vm.runInContext(section('// A subagent\'s line', '// Preserve the resolved version:'), ctx);
vm.runInContext(section('// ----- the agents strip', 'function createJobTerm('), ctx);
const find = (root, cls) => root.all().filter((n) => n.classList.contains(cls));

// lanes: the runner's exact line shape becomes a coloured lane
const agents = [
  { id: 'a', label: 'Verify the sources', type: 'Explore', status: 'running',
    started_t: Date.now() / 1000 - 75, tool_uses: 3, last: 'Read server/mail.py', report: '' },
  { id: 'b', label: 'Verify routing', type: 'Explore', status: 'completed',
    started_t: Date.now() / 1000 - 100, finished_t: Date.now() / 1000 - 10,
    tool_uses: 1, last: '', report: 'Routing **confirmed**.' },
];
const lanes = ctx.agentLaneIndex(agents);
const tool = ctx.renderTermLine('  ┊ Verify routing · → Bash: ls server', lanes);
assert.ok(tool.classList.contains('term-agent'));
assert.ok(tool.classList.contains('lane-1'), 'lane colour follows launch order: ' + tool.className);
assert.equal(find(tool, 'agent-tag')[0].textContent, 'Verify routing');
assert.equal(find(tool, 'tname')[0].textContent, 'Bash');
const text = ctx.renderTermLine('  ┊ Verify the sources · mail polls every 60s', lanes);
assert.ok(text.classList.contains('lane-0'));
assert.match(text.textContent, /mail polls every 60s/);
const stranger = ctx.renderTermLine('  ┊ Someone new · hello', lanes);
assert.ok(stranger.classList.contains('lane-2'), 'an unknown label still gets its own lane');
const main = ctx.renderTermLine('  → Bash: ls', lanes);
assert.ok(!main.classList.contains('term-agent'), 'main-agent lines are not lanes');

// a shell line: the agent's purpose first, the command dimmed after it
const labelled = ctx.renderTermLine('  → Bash: Run the full unittest suite  $ .venv/bin/python -m unittest', lanes);
assert.equal(find(labelled, 'tcmd')[0].textContent, '  $ .venv/bin/python -m unittest');
assert.match(labelled.textContent, /Bash: Run the full unittest suite/);
const laneShell = ctx.renderTermLine('  ┊ Verify routing · → Bash: List files  $ ls server', lanes);
assert.equal(find(laneShell, 'tcmd')[0].textContent, '  $ ls server');
assert.equal(find(ctx.renderTermLine('  → Bash: ls', lanes), 'tcmd').length, 0, 'an unlabelled command reads as before');
// a failed shell command, written by the runner under its line
const failLine = ctx.renderTermLine('    ↳ exit 127: (eval):1: command not found: timeout', lanes);
assert.ok(failLine.classList.contains('term-fail'), failLine.className);
// the model's reasoning line (runner.THINK_MARK): dim, neither a tool nor a lane
const think = ctx.renderTermLine('  \u273b I will read the parser before changing it.', lanes);
assert.ok(think.classList.contains('term-think'), think.className);
assert.ok(!think.classList.contains('term-text') && !think.classList.contains('term-agent'));
assert.match(think.textContent, /\u273b I will read the parser/);

// the strip: one card per agent, running ones say what they are doing
const box = new Element();
ctx.renderAgentStrip(box, { agents });
assert.ok(box.classList.contains('on'));
assert.equal(find(box, 'agents-head')[0].textContent, 'Agents · 1 of 2 running');
const cards = find(box, 'agent-card');
assert.equal(cards.length, 2);
assert.ok(cards[0].classList.contains('st-running') && cards[0].classList.contains('lane-0'));
assert.equal(find(cards[0], 'agent-last')[0].textContent, 'Read server/mail.py');
assert.match(find(cards[0], 'agent-meta')[0].textContent, /^running · 3 tool calls · 1m 1\ds$/);
assert.match(find(cards[1], 'agent-meta')[0].textContent, /^done · 1 tool call · 1m 30s$/);
// a finished agent's report opens from its row, and survives a re-render
const row = find(cards[1], 'agent-row')[0];
row.click();
assert.ok(cards[1].classList.contains('open'));
ctx.renderAgentStrip(box, { agents: agents.map((a) => a.id === 'a' ? { ...a, tool_uses: 4 } : a) });
assert.ok(find(box, 'agent-card')[1].classList.contains('open'), 'the poll must not collapse an open report');
// all reported
ctx.renderAgentStrip(box, { agents: agents.map((a) => ({ ...a, status: 'completed' })) });
assert.equal(find(box, 'agents-head')[0].textContent, 'Agents · all 2 reported');
// no agents: the strip is hidden
const empty = new Element();
ctx.renderAgentStrip(empty, { agents: [] });
assert.ok(!empty.classList.contains('on'));

// the pinned checklist: the session's own steps, numbered as the owner names them
const planBox = new Element();
const plan = { execution: { plan_steps: [
  { text: 'Read the parser', status: 'completed' },
  { text: 'Fix blank lines', status: 'in_progress' },
  { text: 'Run the tests', status: 'pending' }] } };
ctx.renderPlanStrip(planBox, plan);
assert.ok(planBox.classList.contains('on'));
assert.equal(find(planBox, 'plan-head')[0].textContent, 'Steps · 1 of 3 done');
const stepRows = find(planBox, 'plan-step');
assert.deepEqual(stepRows.map((n) => ['st-completed', 'st-in_progress', 'st-pending']
  .find((c) => n.classList.contains(c))), ['st-completed', 'st-in_progress', 'st-pending']);
assert.deepEqual(stepRows.map((n) => find(n, 'plan-num')[0].textContent), ['1.', '2.', '3.']);
assert.equal(find(stepRows[0], 'plan-mark')[0].textContent, '✓', 'a done step is ticked');
assert.equal(find(stepRows[1], 'plan-mark')[0].textContent, '▸', 'the step in progress is marked');
assert.equal(find(stepRows[2], 'plan-mark')[0].textContent, '○');
assert.equal(find(stepRows[1], 'plan-text')[0].textContent, 'Fix blank lines');
assert.equal(stepRows[1].title, 'Fix blank lines', 'a step cut to one line keeps its words');
// the 800ms poll does not rebuild an unchanged list
const built = planBox.children[0];
ctx.renderPlanStrip(planBox, JSON.parse(JSON.stringify(plan)));
assert.equal(planBox.children[0], built);
ctx.renderPlanStrip(planBox, { execution: { plan_steps: plan.execution.plan_steps
  .map((s) => ({ ...s, status: 'completed' })) } });
assert.equal(find(planBox, 'plan-head')[0].textContent, 'Steps · all 3 done');
// steps past the runner's cap are counted, never silently gone
ctx.renderPlanStrip(planBox, { execution: { plan_steps: plan.execution.plan_steps, plan_steps_more: 4 } });
assert.equal(find(planBox, 'plan-head')[0].textContent, 'Steps · 1 of 3 done · 4 more not shown');
// no list: the strip is hidden and empty
ctx.renderPlanStrip(planBox, { execution: {} });
assert.ok(!planBox.classList.contains('on'));
assert.equal(planBox.children.length, 0);
console.log('Session agents UI passed');

// Run the actual polling/render functions against controllable API responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');
let tick, stopped = 0, focused = 0, rendered = 0, response;
const pollStarts = [];
const context = vm.createContext({
  URLSearchParams,
  findChatSession: {id: 'chat-test', turns: [{status: 'pending'}]},
  findChatPending: false, findChatPollStop: null, findChatList: [],
  findChatRefs: {send: {}, input: {value: 'follow up', focus() { focused++; }}},
  startPoll(fn, ms, maxMs) { tick = fn; pollStarts.push({ms, maxMs}); return {stop() { stopped++; }}; },
  api: async () => response,
  renderFindChat() { rendered++; },
  el(tag, cls, text) {
    return {tag, cls, textContent: text, children: [],
      classList: {add() {}}, appendChild(c) { this.children.push(c); },
      querySelectorAll() { return []; }, addEventListener() {}};
  },
  mdToHtml: (text) => text,
  toast(message) { throw new Error(message); },
  errText: (error) => error.message,
});
function load(start, end) {
  const offset = app.indexOf(start);
  assert(offset >= 0 && app.indexOf(end, offset) > offset);
  vm.runInContext(app.slice(offset, app.indexOf(end, offset)), context);
}
load('function mergeFindChatHistory(', 'async function loadEarlierFindChat(');
load('async function loadEarlierFindChat(', 'const findChatVisible =');
load('function findChatEnriching(', 'function findChatRestartRequired(');
load('function chatAnswer(', '// Where a "looked at" card goes:');
load('// ----- the pinned checklist', 'function renderPlanStrip(');

// ----- the conversation keeps its nodes (text selection) -----
// The real renderFindChat against a small DOM that records every node taken
// out of the log. A selection lives in nodes: a node that leaves the log
// takes the selection with it, so "was it detached" is the whole question.
function selectionChecks() {
  const detached = [];
  function node(tag, cls, text) {
    return {tag, cls, textContent: text, kids: [], parent: null, style: {}, dataset: {},
      classList: {add() {}}, querySelectorAll() { return []; },
      addEventListener() {}, setAttribute() {},
      get children() { return this.kids; },
      remove() {
        if (!this.parent) return;
        this.parent.kids.splice(this.parent.kids.indexOf(this), 1);
        this.parent = null;
        detached.push(this);
      },
      appendChild(c) { return this.insertBefore(c, null); },
      insertBefore(c, ref) {
        c.remove();
        c.parent = this;
        this.kids.splice(ref ? this.kids.indexOf(ref) : this.kids.length, 0, c);
        return c;
      }};
  }
  const log = node('div', 'find-chat-log');
  Object.assign(log, {scrollTop: 0, scrollHeight: 0, clientHeight: 0});
  let selection = null, panels = 0, reply = null;
  const timers = [];
  const ctx = vm.createContext({
    URLSearchParams, el: node, mdToHtml: (text) => text,
    window: {getSelection: () => selection},
    document: {querySelectorAll: () => []},
    requestAnimationFrame: (fn) => fn(),
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    startPoll: (fn) => { ctx.tick = fn; return {stop() {}}; },
    api: async () => JSON.parse(JSON.stringify(reply)),
    findChatSession: null, findChatList: [], findChatLoaded: false,
    findChatPending: false, findChatPollStop: null,
    findChatRefs: {log, status: {}, stop: {}, steer: {}, scope: {}, mode: {},
      send: {}, empty: {style: {}}, input: {value: '', focus() {}}, chatPane: {}},
    findChatSessionLabel: () => 'Chat',
    acknowledgeFindChat() {},
    renderFindClouds() { panels++; }, renderFindRelated() {},
  });
  const part = (start, end) => {
    const offset = app.indexOf(start);
    assert(offset >= 0 && app.indexOf(end, offset) > offset, start);
    vm.runInContext(app.slice(offset, app.indexOf(end, offset)), ctx);
  };
  part('function mergeFindChatHistory(', 'async function loadEarlierFindChat(');
  part('let findChatEventTimer =', 'function acknowledgeFindChat(');
  part('function findChatAdopt(', 'async function loadFindChat(');
  part('function findChatEnriching(', 'function findChatRestartRequired(');
  part('// ----- the conversation keeps its nodes -----', '// One chat turn\'s answer.');
  part('function chatAnswer(', '// Where a "looked at" card goes:');
  part('// ----- the pinned checklist', 'function renderPlanStrip(');
  const chat = (second, extra = {}) => ({session: {id: 'c1', job_id: 'job-1', turns: [
    {id: 't1', sent_t: 1, status: 'done', question: 'First?', answer: 'First answer'},
    {id: 't2', sent_t: 2, question: 'Second?', ...second}], ...extra}});
  const answers = () => log.kids.filter((n) => n.cls === 'brain-msg vira')
    .map((n) => n.kids.find((k) => k.cls === 'chat-md')?.innerHTML);

  ctx.findChatAdopt(chat({status: 'done', answer: 'Second answer'}));
  const first = [...log.kids];
  assert.equal(first.length, 4, 'a question and an answer per turn');
  assert.deepEqual(answers(), ['First answer', 'Second answer']);

  // the same conversation drawn again leaves every node where it is
  detached.length = 0;
  ctx.findChatAdopt(chat({status: 'done', answer: 'Second answer'}));
  assert.deepEqual(log.kids, first, 'an unchanged conversation keeps its nodes');
  assert.equal(detached.length, 0, 'and none of them left the log on the way');

  // a turn that changed is redrawn; the turn above it is not touched
  ctx.findChatAdopt(chat({status: 'done', answer: 'Second answer, revised'}));
  assert.deepEqual(log.kids.slice(0, 2), first.slice(0, 2));
  assert.ok(!detached.includes(first[0]) && !detached.includes(first[1]),
    'the earlier turn never left the log');
  assert.deepEqual(answers(), ['First answer', 'Second answer, revised']);

  // the placeholder a send appends does not outlive the next draw
  const stray = log.appendChild(node('div', 'brain-msg vira thinking', 'Vira is looking'));
  ctx.renderFindChat();
  assert.ok(!log.kids.includes(stray));
  assert.equal(log.kids.length, 4);

  // a selection standing in a turn holds that turn back, changed or not...
  const held = [...log.kids];
  selection = {isCollapsed: false, containsNode: (n) => n === held[3]};
  detached.length = 0;
  ctx.findChatAdopt(chat({status: 'done', answer: 'Third version'}));
  assert.deepEqual(log.kids, held, 'the selected answer is not replaced under the selection');
  assert.equal(detached.length, 0);
  // ...and the turn catches up once the selection is let go
  selection = {isCollapsed: true, containsNode: () => false};
  ctx.findChatLetGo();
  assert.equal(timers.length, 1, 'letting go schedules the held redraw once');
  ctx.findChatLetGo();
  assert.equal(timers.length, 1);
  timers.shift()();
  assert.deepEqual(answers(), ['First answer', 'Third version']);

  // a press in the log is the start of a selection: nothing moves under it
  const pressed = [...log.kids];
  vm.runInContext('findChatPressed = true;', ctx);
  ctx.findChatAdopt(chat({status: 'done', answer: 'Fourth version'}));
  assert.deepEqual(log.kids, pressed);
  ctx.findChatLetGo();
  timers.shift()();
  assert.deepEqual(log.kids, pressed, 'still pressed: still held');
  vm.runInContext('findChatPressed = false;', ctx);
  ctx.findChatLetGo();
  timers.shift()();
  assert.deepEqual(answers(), ['First answer', 'Fourth version']);

  // An answer still being written redraws as it grows without disturbing
  // the settled answer above it.
  ctx.findChatAdopt(chat({status: 'pending', messages: [{phase: 'commentary', text: 'One'}]},
    {elapsed_s: 2}));
  const settled = log.kids.slice(0, 2), writing = log.kids[3];
  detached.length = 0;
  ctx.findChatAdopt(chat({status: 'pending', messages: [{phase: 'commentary', text: 'One'}]},
    {elapsed_s: 4}));
  assert.deepEqual(log.kids.slice(0, 2), settled);
  assert.ok(!detached.includes(settled[0]) && !detached.includes(settled[1]));
  assert.notEqual(log.kids[3], writing, 'the clock on a pending turn still advances');
  return {ctx, log, timers, detached, drawn: () => panels, setReply: (r) => { reply = r; }};
}

// A session waiting for a reply beats every two seconds and each beat pokes
// the page. A poke, or a poll, whose reply reads the same as what is on
// screen draws nothing.
async function quietRefreshChecks() {
  const {ctx, log, timers, detached, drawn, setReply} = selectionChecks();
  const idle = {session: {id: 'c1', job_id: 'job-1', turns: [
    {id: 't1', sent_t: 1, status: 'done', question: 'First?', answer: 'First answer'}]},
    sessions: [{id: 'c1', title: 'First?', turns: 1}]};
  ctx.findChatAdopt(JSON.parse(JSON.stringify(idle)));
  const before = drawn(), nodes = [...log.kids];
  detached.length = 0;
  setReply(idle);
  for (let beat = 0; beat < 3; beat++) {
    ctx.refreshFindChatFromEvent({kind: 'update', id: 'job-1'});
    await timers.shift()();
  }
  assert.equal(drawn(), before, 'a beat with nothing new draws nothing');
  assert.deepEqual(log.kids, nodes);
  assert.equal(detached.length, 0);
  setReply({...idle, sessions: [{id: 'c1', title: 'First?', turns: 1}, {id: 'c2', title: 'Other'}]});
  ctx.refreshFindChatFromEvent({kind: 'update', id: 'job-1'});
  await timers.shift()();
  assert.equal(drawn(), before + 1, 'a real change is still drawn');
  assert.deepEqual(log.kids, nodes, 'around the nodes already there');

  // the poll that follows panel work: same rule
  const loading = JSON.parse(JSON.stringify(idle));
  loading.session.turns[0].enrichment = {status: 'running'};
  ctx.findChatAdopt(JSON.parse(JSON.stringify(loading)));
  setReply(loading);
  const polled = drawn();
  await ctx.tick({stop() {}});
  await ctx.tick({stop() {}});
  assert.equal(drawn(), polled, 'a poll with nothing new draws nothing');
}

const handle = {stop() { stopped++; }};
function session(status, enrichment) {
  return {id: 'chat-test', turns: [{status, answer: 'Ready answer', enrichment}]};
}
(async () => {
  context.findChatWatch();
  assert.equal(context.findChatRefs.send.disabled, true);
  response = {session: session('done', {status: 'running'})};
  await tick(handle);
  assert.equal(context.findChatPending, false);
  assert.equal(context.findChatRefs.send.disabled, false, 'answer unlocks reply before panels finish');
  assert.equal(stopped, 0, 'panel results are still followed');
  assert.equal(focused, 1);
  const box = context.chatAnswer(response.session.turns[0], response.session);
  assert.equal(box.children[0].innerHTML, 'Ready answer');
  assert.match(box.children[1].textContent, /Updating sources/);
  await tick(handle);
  assert.equal(focused, 1, 'panel updates do not keep stealing focus');
  response = {session: session('done', {status: 'failed'})};
  await tick(handle);
  assert.equal(stopped, 1);
  assert.equal(context.findChatRefs.send.disabled, false);
  const failed = context.chatAnswer(response.session.turns[0], response.session);
  assert.equal(failed.children[0].innerHTML, 'Ready answer');
  assert.match(failed.children[1].textContent, /could not finish/);

  // The model's reasoning streams as a dim line while the turn is pending
  // and is not yet "the model's words"; once answered it folds above the
  // answer with the progress notes, and the answer stays first to read.
  const streaming = {id: 'chat-test', elapsed_s: 3, turns: [{status: 'pending', messages: [
    {phase: 'thinking', text: 'Check the parser before the tests.'}]}]};
  const live = context.chatAnswer(streaming.turns[0], streaming);
  assert.equal(live.children[0].cls, 'chat-think');
  assert.match(live.children[0].textContent, /^\u273b Check the parser/);
  assert.match(live.children[1].textContent, /^Preparing an answer/);
  const settled = {id: 'chat-test', turns: [{status: 'done', answer: 'Ready answer', messages: [
    {phase: 'thinking', text: 'Check the parser before the tests.'},
    {phase: 'commentary', text: 'Two records agree.'},
    {phase: 'final_answer', text: 'Ready answer'}]}]};
  const folded = context.chatAnswer(settled.turns[0], settled);
  assert.equal(folded.children[0].cls, 'chat-reasoning');
  assert.equal(folded.children[0].children[0].textContent, 'How Vira got here (2)');
  assert.equal(folded.children[0].children[1].cls, 'chat-think');
  assert.equal(folded.children[0].children[2].innerHTML, 'Two records agree.');
  assert.equal(folded.children[1].innerHTML, 'Ready answer', 'the answer follows the fold');

  // The session's own checklist sits under the status line while the turn
  // is pending: the same one the terminal pins above its reply box.
  const planned = {id: 'chat-test', elapsed_s: 5, execution: {plan_steps: [
    {text: 'Search the vault', status: 'completed'},
    {text: 'Read the two notes', status: 'in_progress'}]}, turns: [{status: 'pending'}]};
  const withPlan = context.chatAnswer(planned.turns[0], planned);
  assert.match(withPlan.children[0].textContent, /^Preparing an answer/);
  const check = withPlan.children[1];
  assert.equal(check.cls, 'plan-check', 'the checklist follows the status line');
  assert.equal(check.children[0].textContent, 'Steps · 1 of 2 done');
  assert.equal(check.children[2].cls, 'plan-step st-in_progress');
  assert.equal(check.children[2].children[0].textContent, '▸');
  assert.equal(check.children[2].children[2].textContent, 'Read the two notes');
  const unplanned = context.chatAnswer(planned.turns[0], {...planned, execution: {}});
  assert.ok(!unplanned.children.some((c) => c.cls === 'plan-check'), 'no list, no checklist');

  // Reopening an answered chat still follows unfinished panel work.
  context.findChatSession = session('done', {status: 'pending'});
  context.findChatWatch();
  assert.equal(context.findChatRefs.send.disabled, false);
  let resolve;
  context.api = () => new Promise((r) => { resolve = r; });
  const inflight = tick(handle);
  vm.runInContext('findChatSending = true; findChatPending = true;', context);
  resolve({session: session('done', {status: 'done'})});
  await inflight;
  assert.equal(context.findChatPending, true, 'a stale GET cannot unlock a send while its POST is in flight');
  vm.runInContext('findChatSending = false;', context);
  const overtaken = tick(handle);
  const afterSend = session('pending');
  context.findChatSession = afterSend;
  context.findChatPending = true;
  resolve({session: session('done', {status: 'done'})});
  await overtaken;
  assert.equal(context.findChatSession, afterSend, 'an old response cannot erase a newly sent turn');
  assert.equal(context.findChatPending, true);
  assert(rendered >= 3);

  // Poll the conversation being read, even when another tab changes the
  // server's active conversation. A wrong-session response must be ignored.
  context.findChatPollStop = null;
  context.findChatSession = {id: 'chat / scoped', turns: [{id: 't4', sent_t: 4, status: 'pending'}]};
  const scoped = context.findChatSession;
  const reads = [];
  context.api = async (url) => {
    reads.push(url);
    return {session: {id: 'other-chat', turns: [{status: 'done', answer: 'Unrelated'}]}};
  };
  context.findChatWatch();
  await tick(handle);
  assert.equal(reads.at(-1), '/api/vira/chat?session_id=chat%20%2F%20scoped');
  assert.equal(context.findChatSession, scoped, 'a server-active chat cannot replace the viewed chat');
  assert.equal(context.findChatPending, true);
  assert.equal(pollStarts.at(-1).maxMs, undefined, 'answer polling must not expire on an arbitrary timer');

  // Loading older pages preserves the current version of overlapping turns.
  // A later latest-page poll must preserve those loaded pages and their cursor.
  context.findChatSession = {id: 'paged-chat', history: {start: 2, before: 2, total: 4}, turns: [
    {id: 't3', sent_t: 3, status: 'done', answer: 'Current version'},
    {id: 't4', sent_t: 4, status: 'pending'},
  ]};
  context.api = async (url) => {
    reads.push(url);
    return {session: {id: 'paged-chat', history: {start: 0, before: null, total: 4}, turns: [
      {id: 't1', sent_t: 1, status: 'done', answer: 'First answer'},
      {id: 't2', sent_t: 2, status: 'done', answer: 'Second answer'},
      {id: 't3', sent_t: 3, status: 'done', answer: 'Stale overlapping version'},
    ]}};
  };
  await context.loadEarlierFindChat();
  const pageQuery = new URL(reads.at(-1), 'http://localhost').searchParams;
  assert.equal(pageQuery.get('session_id'), 'paged-chat');
  assert.equal(pageQuery.get('before'), '2');
  assert.equal(pageQuery.get('limit'), '30');
  assert.equal(context.findChatSession.turns.map((turn) => turn.id).join(','), 't1,t2,t3,t4');
  assert.equal(context.findChatSession.turns[2].answer, 'Current version');
  context.api = async () => ({session: {id: 'paged-chat', history: {start: 2, before: 2, total: 4}, turns: [
    {id: 't3', sent_t: 3, status: 'done', answer: 'Fresh current version'},
    {id: 't4', sent_t: 4, status: 'pending'},
  ]}});
  await tick(handle);
  assert.equal(context.findChatSession.turns.map((turn) => turn.id).join(','), 't1,t2,t3,t4');
  assert.equal(context.findChatSession.turns[0].answer, 'First answer');
  assert.equal(context.findChatSession.turns[2].answer, 'Fresh current version');
  assert.equal(context.findChatSession.history.start, 0);
  assert.equal(context.findChatSession.history.before, null, 'latest-page polls must not reset an exhausted older-page cursor');
  context.findChatSession.history.before = 2;
  context.api = () => new Promise((r) => { resolve = r; });
  const olderInflight = context.loadEarlierFindChat();
  const switched = {id: 'new-chat', turns: []};
  context.findChatSession = switched;
  resolve({session: {id: 'paged-chat', history: {start: 0, before: null}, turns: [{id: 'old', sent_t: 0}]}});
  await olderInflight;
  assert.equal(context.findChatSession, switched, 'a late history page cannot overwrite a newly opened conversation');

  // Until native Stop is acknowledged, repeated Stop or steering is refused
  // without consuming the follow-up text or making another request.
  const controls = [];
  let adopted = null;
  context.post = async (url, body) => { controls.push({url, body}); return {session: {id: 'control-chat'}}; };
  context.findChatAdopt = (value) => { adopted = value; };
  context.findChatSession = {id: 'control-chat', turns: [{status: 'pending', stop_requested_t: 123}]};
  context.findChatRefs.input.value = '  Keep this follow-up  ';
  await context.controlFindChat('steer');
  await context.controlFindChat('stop');
  assert.equal(controls.length, 0, 'controls must wait for native interruption acknowledgement');
  assert.equal(context.findChatRefs.input.value, '  Keep this follow-up  ');
  assert.equal(adopted, null);
  context.findChatSession.turns[0] = {status: 'stopped'};
  await context.controlFindChat('steer');
  assert.equal(controls.length, 1, 'the control path becomes usable after acknowledgement');
  assert.equal(controls[0].url, '/api/vira/chat/control-chat/control');
  assert.equal(controls[0].body.text, 'Keep this follow-up');
  assert.equal(context.findChatRefs.input.value, '');
  assert.equal(adopted.session.id, 'control-chat');

  // Exercise the real shared poller with fake timers: pending work schedules
  // recurring checks but no expiry timer. Completion, not elapsed time, stops it.
  const intervals = new Map(), expiries = [];
  let timerId = 0;
  context.setInterval = (fn) => { const id = ++timerId; intervals.set(id, fn); return id; };
  context.clearInterval = (id) => intervals.delete(id);
  context.setTimeout = (fn, delay) => { expiries.push({fn, delay}); return ++timerId; };
  context.clearTimeout = () => {};
  load('function startPoll(', '// ---------- whole-card activation');
  context.findChatPollStop = null;
  context.findChatSession = {id: 'long-running', turns: [{id: 'long-turn', status: 'pending', sent_t: 1}]};
  context.findChatRefs.input.value = 'Ready follow-up';
  let checks = 0, completed = false;
  context.api = async (url) => {
    checks++;
    assert.equal(url, '/api/vira/chat?session_id=long-running');
    return {session: {id: 'long-running', turns: [{id: 'long-turn', status: completed ? 'done' : 'pending'}]}};
  };
  context.findChatWatch();
  assert.equal(expiries.length, 0, 'no safety-expiry timer may abandon a pending answer');
  for (let n = 0; n < 12; n++) {
    for (const callback of intervals.values()) callback();
    await new Promise(setImmediate);
  }
  assert.equal(checks, 12);
  assert.equal(intervals.size, 1, 'pending work remains observable until a terminal response');
  completed = true;
  for (const callback of intervals.values()) callback();
  await new Promise(setImmediate);
  assert.equal(intervals.size, 0, 'a terminal answer stops the real poller');
  assert.equal(context.findChatRefs.send.disabled, false);
  await quietRefreshChecks();
  process.stdout.write('Chat answer delivery UI checks passed\n');
})().catch((error) => { console.error(error); process.exitCode = 1; });

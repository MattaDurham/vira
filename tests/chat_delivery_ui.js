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
  process.stdout.write('Chat answer delivery UI checks passed\n');
})().catch((error) => { console.error(error); process.exitCode = 1; });

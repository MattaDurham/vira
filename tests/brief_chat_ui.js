// The brief's chat bar: run the real functions from static/app.js against
// stub refs and a recorded API, and check what the owner would see and what
// the server would be asked to do.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');

function node(extra = {}) {
  return Object.assign({
    hidden: false, disabled: false, value: '', textContent: '', placeholder: '',
    innerHTML: '', scrollTop: 0, scrollHeight: 0, clientHeight: 0, children: [],
    focused: 0, listeners: {},
    appendChild(c) { this.children.push(c); },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    focus() { this.focused++; },
  }, extra);
}

function harness({ stored = null, today = '2026-11-09', replies = {} } = {}) {
  const refs = {
    '#brief-chat-form': node(), '#brief-chat-input': node(), '#brief-chat-send': node(),
    '#brief-chat-stop': node(), '#brief-chat-log': node(), '#brief-chat-head': node(),
    '#brief-chat-open': node(), '#brief-chat-new': node(),
  };
  // innerHTML = "" empties the log, as it does in a browser
  const log = refs['#brief-chat-log'];
  Object.defineProperty(log, 'innerHTML', {
    get() { return ''; }, set() { this.children = []; },
  });
  const store = new Map();
  if (stored) store.set('vira.briefChat', JSON.stringify(stored));
  const calls = [];
  const polls = [];
  const ctx = {
    $: (s) => refs[s] || null,
    el(tag, cls, text) { return { tag, cls, textContent: text, children: [] }; },
    lsGet(k, fb) { return store.has(k) ? JSON.parse(store.get(k)) : fb; },
    lsSet(k, v) { store.set(k, JSON.stringify(v)); },
    localStorage: { removeItem(k) { store.delete(k); } },
    Date: class extends Date {
      toLocaleDateString(loc) { return loc === 'en-CA' ? today : super.toLocaleDateString(loc); }
    },
    api: async (path) => { calls.push(['GET', path]); return replies.get ? replies.get(path) : {}; },
    post: async (path, body) => {
      calls.push(['POST', path, body]);
      const r = replies[path];
      if (r instanceof Error) throw r;
      return typeof r === 'function' ? r(body) : (r || {});
    },
    startPoll(fn, ms) { const h = { fn, ms, stopped: false, stop() { this.stopped = true; } }; polls.push(h); return h; },
    chatAnswer: (turn) => ({ cls: 'brain-msg vira', textContent: turn.answer || turn.status }),
    errText: (e) => e.message,
    toast(m) { ctx.toasts.push(m); },
    toasts: [],
    isDesktop: true, winState: {}, openWindow() {}, focusWin() {}, openApp() {},
    showFindChat() { ctx.findOpened = true; },
    encodeURIComponent,
  };
  vm.createContext(ctx);
  const start = app.indexOf("// ---------- the brief's chat bar ----------");
  const end = app.indexOf('$("#brief-refresh")', start);
  assert(start >= 0 && end > start, 'chat bar block found in app.js');
  vm.runInContext(app.slice(start, end), ctx);
  return { ctx, refs, calls, polls, store };
}

const pending = (id, q) => ({ session: { id, turns: [{ id: 't1', question: q, status: 'pending' }] } });
// Objects made inside the vm come from another realm; compare by value.
const same = (a, b, m) => assert.deepEqual(JSON.parse(JSON.stringify(a)), b, m);
const flush = () => new Promise((r) => setImmediate(r));

(async () => {
  // 1. A first message opens a fresh chat for today, marked as from the brief.
  {
    const h = harness({ replies: {
      '/api/vira/chat/new': { session: { id: 'chat_a', turns: [] } },
      '/api/vira/chat': (b) => pending(b.session_id, b.question),
      '/api/vira/chat/chat_a/control': pending('chat_a', 'Move the 9:30 to 10'),
    } });
    await flush();
    assert.equal(h.refs['#brief-chat-log'].hidden, true, 'no conversation yet, no log');
    assert.equal(h.refs['#brief-chat-send'].disabled, true, 'nothing typed, nothing to send');
    h.refs['#brief-chat-input'].value = 'Move the 9:30 to 10';
    h.refs['#brief-chat-input'].listeners.input();
    assert.equal(h.refs['#brief-chat-send'].disabled, false);
    await h.ctx.sendBriefChat();
    const posts = h.calls.filter((c) => c[0] === 'POST').map((c) => c[1]);
    same(posts, ['/api/vira/chat/new', '/api/vira/chat']);
    const sent = h.calls.find((c) => c[1] === '/api/vira/chat')[2];
    same(sent, { question: 'Move the 9:30 to 10', session_id: 'chat_a', origin: 'brief' });
    same(JSON.parse(h.store.get('vira.briefChat')), { day: '2026-11-09', id: 'chat_a' });
    assert.equal(h.refs['#brief-chat-input'].value, '', 'the bar clears once sent');
    assert.equal(h.refs['#brief-chat-log'].hidden, false);
    assert.equal(h.refs['#brief-chat-head'].hidden, false);
    assert.equal(h.refs['#brief-chat-log'].children[0].textContent, 'Move the 9:30 to 10');
    assert.equal(h.refs['#brief-chat-stop'].hidden, false, 'Stop shows while Vira works');
    assert.equal(h.refs['#brief-chat-send'].textContent, 'Send guidance');
    assert.equal(h.polls.length, 1, 'a pending turn is followed');

    // 2. Typing while it works steers the same turn; it never opens a second chat.
    h.refs['#brief-chat-input'].value = 'Only if Sam is free';
    await h.ctx.sendBriefChat();
    const steer = h.calls.at(-1);
    same(steer, ['POST', '/api/vira/chat/chat_a/control', { action: 'steer', text: 'Only if Sam is free' }]);

    // 3. The answer lands: polling stops, the bar is ready for the next message.
    h.ctx.api = async () => ({ session: { id: 'chat_a', turns: [{ id: 't1', question: 'Move the 9:30 to 10', status: 'done', answer: 'Moved.' }] } });
    await h.polls[0].fn(h.polls[0]);
    assert.equal(h.polls[0].stopped, true);
    assert.equal(h.refs['#brief-chat-stop'].hidden, true);
    assert.equal(h.refs['#brief-chat-send'].textContent, 'Send');
    assert.equal(h.refs['#brief-chat-log'].children[1].textContent, 'Moved.');
    assert.equal(h.refs['#brief-chat-input'].focused, 1);

    // 4. A follow-up joins the same chat without opening a new one.
    h.refs['#brief-chat-input'].value = 'And tell me when to leave';
    await h.ctx.sendBriefChat();
    assert.equal(h.calls.filter((c) => c[1] === '/api/vira/chat/new').length, 1);
    assert.equal(h.calls.at(-1)[2].session_id, 'chat_a');

    // 5. Open in chat continues this conversation in Find.
    h.ctx.briefChatSession = { id: 'chat_a', turns: [] };
    await h.ctx.openBriefChatInFind();
    same(h.calls.at(-1), ['POST', '/api/vira/chat/switch', { session_id: 'chat_a' }]);
    assert.equal(h.ctx.findOpened, true);
  }

  // 6. Yesterday's chat is not reopened: a new day starts a new conversation.
  {
    const h = harness({ stored: { day: '2026-11-08', id: 'chat_old' }, replies: {
      '/api/vira/chat/new': { session: { id: 'chat_new', turns: [] } },
      '/api/vira/chat': (b) => pending(b.session_id, b.question),
    } });
    await flush();
    assert.equal(h.calls.length, 0, 'nothing is loaded for a stale day');
    h.refs['#brief-chat-input'].value = 'What is first today?';
    await h.ctx.sendBriefChat();
    assert.equal(h.calls.find((c) => c[1] === '/api/vira/chat')[2].session_id, 'chat_new');
  }

  // 7. Today's chat is reloaded when the page opens.
  {
    const h = harness({ stored: { day: '2026-11-09', id: 'chat_today' }, replies: {
      get: () => ({ session: { id: 'chat_today', turns: [{ id: 't1', question: 'Hi', status: 'done', answer: 'Hello' }] } }),
    } });
    await flush();
    same(h.calls[0], ['GET', '/api/vira/chat?session_id=chat_today']);
    assert.equal(h.refs['#brief-chat-log'].hidden, false);
    assert.equal(h.refs['#brief-chat-log'].children[1].textContent, 'Hello');
  }

  // 8. A failed send keeps what was typed and says why.
  {
    const h = harness({ stored: { day: '2026-11-09', id: 'chat_x' }, replies: {
      get: () => ({ session: { id: 'chat_x', turns: [] } }),
      '/api/vira/chat': new Error('Vira is still answering the last message'),
    } });
    await flush();
    h.refs['#brief-chat-input'].value = 'Draft a reply to the school';
    await h.ctx.sendBriefChat();
    assert.equal(h.refs['#brief-chat-input'].value, 'Draft a reply to the school');
    const last = h.refs['#brief-chat-log'].children.at(-1);
    assert.equal(last.cls, 'brain-msg vira error');
    assert.match(last.textContent, /^Not sent: Vira is still answering/);
    assert.equal(h.refs['#brief-chat-send'].disabled, false, 'the owner can try again');
  }

  // 9. Stop asks the server to stop the running turn; New forgets today's chat.
  {
    const h = harness({ stored: { day: '2026-11-09', id: 'chat_s' }, replies: {
      get: () => pending('chat_s', 'Long job'),
      '/api/vira/chat/chat_s/control': { session: { id: 'chat_s', turns: [{ id: 't1', question: 'Long job', status: 'stopped' }] } },
    } });
    await flush();
    await h.ctx.stopBriefChat();
    same(h.calls.at(-1), ['POST', '/api/vira/chat/chat_s/control', { action: 'stop' }]);
    h.ctx.startNewBriefChat();
    assert.equal(h.store.has('vira.briefChat'), false);
    assert.equal(h.refs['#brief-chat-log'].hidden, true);
  }
  console.log('brief chat ui ok');
})().catch((e) => { console.error(e); process.exit(1); });

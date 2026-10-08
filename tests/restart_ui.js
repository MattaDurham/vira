// Execute the shipped restart UI, with synthetic work and no real server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
class Node {
  constructor(text = '') { this.textContent = text; this.children = []; this.hidden = false; this.disabled = false; }
  append(...items) { this.children.push(...items); for (const item of items) if (item.id) nodes.set('#' + item.id, item); }
  appendChild(item) { this.append(item); }
  replaceChildren() { this.children = []; }
  remove() { nodes.delete('#' + this.id); }
  addEventListener() {}
  focus() {}
}
const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
const idle = { boot_id: 'first', phase: 'idle', mode: 'when_idle', operation: 'restart', available: true,
               error: '', unavailable_reason: '', activities: [] };
let current = {...idle}, posts = [], reloads = 0, opened = 0;
const sandbox = { $, el: (tag, cls, text) => new Node(text), Date, document: { body: new Node(), activeElement: new Node() },
  bindSheet: () => ({open: () => opened++, close: () => opened--}), startPoll: () => {},
  api: async () => current, errText: error => error.message,
  post: async (url, body) => { posts.push({url, body});
    current = {...current, phase: url.endsWith('cancel') ? 'idle' : 'waiting', ...body}; return current; },
  location: {reload: () => reloads++} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('static/restart.js', 'utf8'), sandbox);
async function run() {
  await sandbox.openRestart();
  assert.equal(opened, 1);
  assert.equal(posts.length, 0, 'opening confirmation must not restart');
  assert.equal($('#restart-now').disabled, false);
  assert.equal($('#restart-wait').hidden, true);
  current = {...idle, activities: [{title: '<script>Example build</script>', detail: 'Detached session continues.'}]};
  await sandbox.refreshRestart();
  assert.equal($('#restart-wait').hidden, false);
  assert.equal($('#restart-activities').children[0].children[0].textContent, '<script>Example build</script>');
  await sandbox.submitRestart('when_idle');
  assert.equal(posts[0].body.mode, 'when_idle');
  assert.equal($('#restart-now').disabled, true);
  assert.equal($('#restart-cancel-queued').hidden, false);
  assert.ok(nodes.has('#restart-queue-banner'));
  await $('#restart-cancel-queued').onclick();
  assert.equal(posts[1].url, '/api/restart/cancel');
  assert.equal($('#restart-now').disabled, false);
  await sandbox.openRestart('update');
  await sandbox.submitRestart('now');
  assert.equal(posts[2].body.operation, 'update');
  await sandbox.refreshRestart();
  assert.equal(reloads, 0, 'old process responding must not reload');
  current = {...idle, boot_id: 'second'};
  await sandbox.refreshRestart();
  assert.equal(reloads, 1, 'new process must reload');
  // An unavailable supervisor cannot be bypassed by the UI.
  current = {...idle, available: false, unavailable_reason: 'Startup service required'};
  sandbox.renderRestart(current);
  const before = posts.length;
  await sandbox.submitRestart('now');
  assert.equal(posts.length, before);
  assert.equal($('#restart-now').disabled, true);
}
run().catch(error => { console.error(error); process.exitCode = 1; });

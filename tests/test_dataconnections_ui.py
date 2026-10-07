"""Exercise the actual connection forms, including stale inspection replies."""
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
  constructor(tag, cls = '', text = '') {
    this.tag = tag; this.className = cls; this._text = text;
    this.children = []; this.value = ''; this.disabled = false; this.isConnected = true;
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; this._text = ''; }
  setAttribute() {}
  get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
  set textContent(text) { this._text = text; this.children = []; }
}
const all = n => [n, ...n.children.flatMap(all)];
const button = (n, title) => all(n).find(c => c.tag === 'button' && c.textContent === title);
let replies = [], choices = [], posts = [], reloads = 0;
const context = vm.createContext({
  el: (tag, cls, text) => new Element(tag, cls, text),
  document: {createTextNode: text => new Element('text', '', text)},
  FolderPicker: {choose: async () => choices.shift()},
  location: {reload: () => reloads++},
  errText: e => e.message,
  post: async (url, body) => {
    posts.push({url, body}); const reply = replies.shift();
    if (reply instanceof Error) throw reply;
    return typeof reply === 'function' ? reply() : reply;
  },
  dashJump: () => {},
  setupExtra: {connections: {version: 1,
    crm: {root: '/fixture/crm', available: true, registry_present: true},
    self_record: {root: '/fixture/self', explicit: true, career_ready: false,
      canon: '/fixture/self/canon/MASTER_HISTORY.md', analysis: '/fixture/analysis', packages: '/fixture/packages'},
    reader_sources: [{path: '/fixture/documents', label: 'Examples', available: false}]}},
});
function section(start, end) {
  const first = app.indexOf(start), last = app.indexOf(end, first);
  assert(first >= 0 && last > first); return app.slice(first, last);
}
vm.runInContext(section('function brainFolderControl(', 'function brainFolderList('), context);
vm.runInContext(section('function connectionSummary(', 'function cardDossiers('), context);
const plan = (request = {kind: 'crm', path: '/fixture/crm'}) => ({valid: true, errors: [], warnings: ['Preserves IDs'],
  revision: 'reviewed', request, summary: {root: request.path, people: 2, profiles: 1}});
const render = () => {const host = new Element('div'); context.dataConnectionForm(host, 'crm', '/fixture/crm'); return host;};
(async () => {
  const card = new Element('div'); context.cardDataConnections(card);
  assert.match(card.textContent, /Brain's protected folders apply to Brain writes/);
  assert.match(card.textContent, /Reader reads connected folders/);
  assert.match(card.textContent, /unavailable/);
  const form = render();
  const inspect = button(form, 'Inspect folder'), save = button(form, 'Connect folder');
  assert(save.disabled, 'cannot connect without inspecting');
  replies.push({valid: false, errors: ['Duplicate IDs'], warnings: []});
  await inspect.onclick(); assert(save.disabled); assert.match(form.textContent, /Duplicate IDs/);
  replies.push(plan()); await inspect.onclick(); assert(!save.disabled); assert.match(form.textContent, /Dossiers:  1/);
  const select = all(form).filter(n => n.tag === 'select')[1];
  select.value = 'follow'; select.onchange();
  assert(save.disabled, 'a changed self policy invalidates inspection');
  let finish;
  replies.push(() => new Promise(resolve => {finish = resolve;}));
  const pending = inspect.onclick();
  choices.push({path: '/fixture/another'});
  await all(form).find(n => n.className === 'vault-folder-choice').onclick();
  finish(plan()); await pending;
  assert(save.disabled, 'an old inspection cannot enable a changed folder');
  assert(!form.textContent.includes('Preserves IDs'));
  replies.push(plan({kind: 'crm', path: '/fixture/another', self_choice: 'follow'}));
  await inspect.onclick(); replies.push(new Error('The configuration changed'));
  await save.onclick(); assert(save.disabled); assert.match(form.textContent, /Inspect again/);
  replies.push(plan()); await inspect.onclick(); replies.push({connected: true});
  await save.onclick(); assert.equal(reloads, 1, 'saved locations reload stale data windows');
  const applied = posts.at(-1).body;
  assert.equal(applied.revision, 'reviewed'); assert.equal(applied.request.path, '/fixture/crm');
  const disconnected = new Element('div');
  context.dataConnectionForm(disconnected, 'reader', '/fixture/documents', {removal: {path: '/fixture/documents'}});
  assert(button(disconnected, 'Disconnect folder').disabled);
  replies.push(plan({kind: 'reader', path: '/fixture/documents', mode: 'disconnect'}));
  await button(disconnected, 'Review disconnection').onclick();
  assert(!button(disconnected, 'Disconnect folder').disabled);
  assert.match(disconnected.textContent, /files stay in place/);
  context.setupExtra.connections = {error: 'Unavailable server'};
  const oldServer = new Element('div'); context.cardDataConnections(oldServer);
  assert.match(oldServer.textContent, /Unavailable server/);
  assert.equal(all(oldServer).filter(n => n.tag === 'button').length, 0);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


class ConnectionUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_connection_forms(self):
        result = subprocess.run([shutil.which("node"), "-e", HARNESS],
                                cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

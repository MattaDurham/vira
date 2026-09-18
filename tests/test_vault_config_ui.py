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
}
const all = (node) => [node, ...node.children.flatMap(all)];
const tagged = (node, tag) => all(node).filter((n) => n.tag === tag);
const byClass = (node, name) => all(node).filter((n) => n.className.split(' ').includes(name));
const field = (node, name) => all(node).find((n) => n.name === name);
let posts = [], requests = [], loads = 0, toasts = [];
const context = vm.createContext({
  el: (tag, cls, text) => new Element(tag, cls, text),
  fmtNum: (n) => String(n),
  CSS: { escape: (text) => text },
  document: { querySelector: () => null },
  loadSetup: async () => { loads++; },
  toast: (text) => { toasts.push(text); },
  post: async (url, body) => {
    posts.push({url, body}); return {id: body.id || 'new', name: body.name || 'Vault'};
  },
  api: async (url, options) => { requests.push({url, options}); return {}; },
});
function section(start, end) {
  const first = app.indexOf(start);
  assert(first >= 0, `Missing application section ${start}`);
  const last = app.indexOf(end, first);
  assert(last > first, `Missing section boundary ${end}`);
  return app.slice(first, last);
}
vm.runInContext(section('async function setupAct(', '// ---- render: the Config dashboard'), context);
vm.runInContext(section('let brainOpenSource = null;', 'function cardMail('), context);
const render = (vault) => {
  const card = new Element('div');
  context.cardBrain(card, {}, {vault, platform: 'mac'});
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
  allow_publish: false, default_destination: false, purpose: 'Reference material',
  contexts: ['research'], capture_dir: 'inbox', write_dirs: ['inbox', 'wiki'],
  protected_dirs: ['wiki/canon'], model_exclude_dirs: ['private'], ...overrides,
});
const current = (overrides = {}) => ({
  root: '/fixture/research', policy_version: 1, default_destination: '',
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
  for (const policy_version of [0, '1', null]) {
    assertBlocked(render(current({policy_version})));
  }
  for (const missing of ['model_exclude_dirs', 'purpose', 'id']) {
    const incomplete = source();
    delete incomplete[missing];
    assertBlocked(render(current({sources: [incomplete]})));
  }

  // The policy-aware server predates the explicit version marker. Its complete
  // payload still works, so a frontend-only update does not block real setup.
  const priorCurrent = current();
  delete priorCurrent.policy_version;
  assert.equal(byClass(render(priorCurrent), 'vault-config-form').length, 1);

  const supportedCard = render(current({sources: [source(), source({
    id: 'journal', name: 'Journal', root: '/fixture/journal', primary: false,
    write_enabled: false, read_only: true, contexts: [], write_dirs: [],
  })]}));
  const forms = byClass(supportedCard, 'vault-config-form');
  assert.equal(forms.length, 2);
  assert.equal(byClass(supportedCard, 'vault-config-add').length, 1);
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
  assert.equal(loads, 2, 'saving refreshes the configuration');

  const secondFallback = tagged(byClass(forms[1], 'vault-config-default')[0], 'input')[0];
  assert(secondFallback.disabled, 'a read-only vault cannot become a fallback');
  field(forms[1], 'write_enabled').checked = true;
  field(forms[1], 'write_enabled').onchange();
  assert.equal(secondFallback.disabled, false);
  field(forms[1], 'write_dirs').value = 'inbox';
  secondFallback.checked = true;
  await submit(forms[1]);
  assert.equal(posts[1].body.id, 'journal');
  assert.equal(posts[1].body.default_destination, true);
  assert.equal(posts[1].body.write_enabled, true);

  // A new installation with a current server still has its connection forms.
  const empty = render(current({root: '', sources: []}));
  assert(tagged(empty, 'button').some((n) => n.textContent === 'Use this vault'));
  const add = byClass(empty, 'vault-config-add')[0];
  const addInputs = tagged(add, 'input');
  addInputs[0].value = 'Journal'; addInputs[1].value = '/fixture/journal';
  await submit(add);
  assert.equal(posts[2].url, '/api/vault/sources');
  assert.equal(posts[2].body.path, '/fixture/journal');
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

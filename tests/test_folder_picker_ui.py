"""Exercise the shipped folder dialog against deferred filesystem responses."""
import shutil
import subprocess
import unittest
from pathlib import Path


HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let document;
class Element {
  constructor(tag) {
    this.tag = tag; this.className = ''; this.children = []; this.attributes = {};
    this.listeners = {}; this.disabled = false; this.hidden = false; this.open = false;
    this.value = ''; this.checked = false; this.tabIndex = 0; this._text = '';
  }
  get textContent() { return this._text + this.children.map((n) => n.textContent).join(' '); }
  set textContent(value) { this._text = value; this.replaceChildren(); }
  get firstChild() { return this.children[0]; }
  get isConnected() { return this === document.body || !!this.parentElement?.isConnected; }
  appendChild(child) { this.children.push(child); child.parentElement = this; return child; }
  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = null;
    this.children = []; for (const child of children) this.appendChild(child);
  }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((n) => n !== this);
    this.parentElement = null;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatchEvent(event) { for (const listener of this.listeners[event.type] || []) listener(event); }
  click() { if (!this.disabled) this.dispatchEvent({type: 'click'}); }
  focus() { document.activeElement = this; }
  contains(item) { return item === this || this.children.some((n) => n.contains(item)); }
  querySelectorAll(selector) {
    const tags = selector.split(', ').filter((s) => !s.startsWith('['));
    return all(this).slice(1).filter((n) => tags.includes(n.tag));
  }
  getClientRects() {
    for (let p = this; p; p = p.parentElement) if (p.hidden) return [];
    return this.isConnected ? [1] : [];
  }
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatchEvent({type: 'close'}); }
}
const all = (node) => [node, ...node.children.flatMap(all)];
const cls = (node, name) => all(node).find((n) => n.className.split(' ').includes(name));
const button = (node, text) => all(node).find((n) => n.tag === 'button' && n.textContent.trim() === text);
document = {body: new Element('body'), createElement: (tag) => new Element(tag), activeElement: null};
const trigger = document.body.appendChild(new Element('button'));
trigger.focus();
const listeners = {};
const window = {
  addEventListener(type, listener) { (listeners[type] ||= new Set()).add(listener); },
  removeEventListener(type, listener) { listeners[type]?.delete(listener); },
};
const key = (key, shiftKey = false) => {
  let prevented = false, stopped = false;
  const event = {key, shiftKey, preventDefault() { prevented = true; }, stopImmediatePropagation() { stopped = true; }};
  for (const listener of listeners.keydown || []) listener(event);
  return {prevented, stopped};
};
const calls = [];
const fetch = (url, options) => new Promise((resolve, reject) => {
  // Deliberately ignore abort here: the picker must release its loading state
  // even if a stalled transport never settles after its signal is aborted.
  calls.push({url, options, respond: resolve, reject,
    resolve: (data, ok = true) => resolve({ok, status: ok ? 200 : 404, json: async () => data})});
});
const timers = new Map();
let now = 0, nextTimer = 0;
const setTimeout = (callback, delay) => {
  const id = ++nextTimer; timers.set(id, {callback, at: now + delay}); return id;
};
const clearTimeout = (id) => timers.delete(id);
const context = vm.createContext({window, document, fetch, AbortController, URLSearchParams, console,
  setTimeout, clearTimeout});
vm.runInContext(fs.readFileSync('static/folder-picker.js', 'utf8'), context);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const elapse = async (milliseconds) => {
  now += milliseconds;
  for (const [id, timer] of [...timers]) {
    if (timer.at <= now) { timers.delete(id); timer.callback(); }
  }
  await tick();
};
const choose = (options) => window.FolderPicker.choose(options);
const dialog = () => document.body.children.find((n) => n.tag === 'dialog');
const listing = (path = '/home/demo', overrides = {}) => ({
  path, name: path.split('/').pop(), root: null, relative: null, parent: '/home',
  ancestors: [{name: 'Home', path: '/home/demo'}, {name: path.split('/').pop(), path}],
  places: [{name: 'Home', path: '/home/demo'}, {name: 'Documents', path: '/home/demo/Documents'}],
  folders: [{name: 'Notes', path: path + '/Notes'}, {name: '<script> folder', path: path + '/odd'}],
  can_create: true, create_disabled_reason: '', ...overrides,
});
const reply = async (data, ok = true, call = calls.at(-1)) => { call.resolve(data, ok); await tick(); };
const submit = async (form) => { form.dispatchEvent({type: 'submit', preventDefault() {}}); await tick(); };

(async () => {
  // A not-yet-loaded path is never selectable; folder names remain literal text.
  let result = choose({title: 'Add a vault'});
  let current = dialog();
  assert(current.open);
  assert(button(current, 'Select this folder').disabled);
  assert.equal(calls.at(-1).url, '/api/folders', 'an empty query uses the canonical URL without a trailing ?');
  await reply(listing());
  assert.equal(cls(current, 'folder-picker-folders').children.length, 2);
  assert.equal(cls(current, 'folder-picker-folder-name').textContent, 'Notes');
  assert(!all(current).some((n) => n.tag === 'script'));
  assert(!all(current).some((n) => n.tag === 'input' && n.value.includes('/home')),
    'paths are displayed, never manually editable');
  const search = cls(current, 'folder-picker-search');
  search.value = 'notes'; search.dispatchEvent({type: 'input'});
  assert.equal(cls(current, 'folder-picker-folders').children.length, 1);
  cls(current, 'folder-picker-row').click();
  assert(button(current, 'Select this folder').disabled);
  assert.equal(new URL(calls.at(-1).url, 'http://localhost').searchParams.get('path'), '/home/demo/Notes');
  await reply(listing('/home/demo/Notes', {folders: []}));
  assert.match(current.textContent, /No subfolders here/);
  button(current, 'Select this folder').click();
  assert.equal((await result).path, '/home/demo/Notes');
  assert.equal(dialog(), undefined);
  assert.equal(document.activeElement, trigger, 'selection restores the opening control');

  // Root restrictions use the server's canonical relative path, including symlinks.
  result = choose({root: '/link/vault', allowRoot: false});
  current = dialog();
  await reply(listing('/real/vault', {root: '/real/vault', relative: '.', parent: null}));
  assert(button(current, 'Select this folder').disabled);
  assert(button(current, 'Up one level').disabled);
  assert.match(current.textContent, /Choose a folder inside this vault/);
  cls(current, 'folder-picker-row').click();
  assert.equal(new URL(calls.at(-1).url, 'http://localhost').searchParams.get('root'), '/real/vault');
  await reply(listing('/real/vault/Notes', {root: '/real/vault', relative: 'Notes', folders: []}));
  button(current, 'Select this folder').click();
  assert.equal((await result).relative, 'Notes');

  // Cancelling while a read is in flight cannot later re-open the popup.
  result = choose(); current = dialog();
  const cancelledRead = calls.at(-1);
  const escape = key('Escape');
  assert(escape.prevented && escape.stopped, 'Escape must not also close the app behind the popup');
  assert.equal((await result).cancelled, true);
  assert(cancelledRead.options.signal.aborted);
  await reply(listing(), true, cancelledRead);
  assert.equal(dialog(), undefined);
  assert.equal(document.activeElement, trigger);

  // Opening a second picker cancels the first and ignores its late response.
  const old = choose(); const oldRead = calls.at(-1);
  result = choose({title: 'Latest picker'}); current = dialog();
  assert.equal((await old).cancelled, true);
  await reply(listing('/home/new'));
  await reply(listing('/home/stale'), true, oldRead);
  assert.equal(cls(current, 'folder-picker-selection').children[1].textContent, '/home/new');
  const select = button(current, 'Select this folder');
  select.focus();
  assert(key('Tab').prevented);
  assert.equal(document.activeElement, button(current, 'Close'));
  assert(key('Tab', true).prevented);
  assert.equal(document.activeElement, select);
  button(current, 'Cancel').click(); await result;

  // Failure keeps Select disabled and offers a path-free recovery to Home.
  result = choose({path: '/missing'}); current = dialog();
  await reply({detail: 'That folder no longer exists.'}, false);
  assert(button(current, 'Select this folder').disabled);
  assert.match(current.textContent, /That folder no longer exists/);
  button(current, 'Go to home folder').click();
  await reply(listing());
  button(current, 'Cancel').click(); await result;

  // A stalled fetch cannot keep Opening folder... forever, even if abort is
  // ignored by the transport. Retry owns the dialog; old replies cannot paint.
  result = choose({path: '/slow'}); current = dialog();
  const stalledRead = calls.at(-1);
  assert(!button(current, 'Close').disabled);
  assert(!button(current, 'Cancel').disabled);
  await elapse(10000);
  assert(stalledRead.options.signal.aborted);
  assert.equal(current.attributes['aria-busy'], 'false');
  assert(!current.textContent.includes('Opening folder...'));
  assert.match(current.textContent, /took too long/);
  assert(button(current, 'Select this folder').disabled);
  assert(!button(current, 'Try again').disabled);
  button(current, 'Try again').click();
  const retryRead = calls.at(-1);
  assert.notEqual(retryRead, stalledRead);
  assert.equal(new URL(retryRead.url, 'http://localhost').searchParams.get('path'), '/slow');
  await reply(listing('/stale'), true, stalledRead);
  assert.equal(current.attributes['aria-busy'], 'true', 'late response cannot finish the retry');
  assert(button(current, 'Select this folder').disabled);
  await reply(listing('/slow'), true, retryRead);
  button(current, 'Select this folder').click();
  assert.equal((await result).path, '/slow');

  // Receiving headers is insufficient: the deadline also bounds a stalled body.
  result = choose(); current = dialog();
  const stalledBody = calls.at(-1);
  let finishBody;
  stalledBody.respond({ok: true, status: 200, json: () => new Promise((resolve) => { finishBody = resolve; })});
  await tick();
  await elapse(10000);
  assert.match(current.textContent, /took too long/);
  assert.equal(current.attributes['aria-busy'], 'false');
  finishBody(listing('/body-too-late')); await tick();
  assert(button(current, 'Select this folder').disabled);
  button(current, 'Cancel').click(); await result;

  // A timeout within a vault is not evidence the folder disappeared. It must
  // offer a retry immediately, without another automatic request to the root.
  result = choose({root: '/home/vault', path: '/home/vault/Notes'}); current = dialog();
  const beforeTimeout = calls.length;
  await elapse(10000);
  assert.equal(calls.length, beforeTimeout);
  assert(!current.textContent.includes('previous folder is unavailable'));
  assert(!button(current, 'Try again').disabled);
  button(current, 'Cancel').click(); await result;

  // Closing a stalled request removes its timer. A fresh picker gets its own
  // complete deadline, and an obsolete timeout cannot mutate it.
  result = choose(); current = dialog();
  const cancelledStall = calls.at(-1);
  await elapse(9000);
  button(current, 'Close').click();
  assert.equal((await result).cancelled, true);
  await tick();
  assert.equal(timers.size, 0);
  assert(cancelledStall.options.signal.aborted);
  result = choose(); current = dialog();
  await elapse(1000);
  assert.equal(current.attributes['aria-busy'], 'true');
  await reply(listing('/new-picker'));
  button(current, 'Cancel').click(); await result;

  // Unsupported scoped names stay browsable but cannot be submitted as policies.
  result = choose({root: '/home/vault', path: '/home/vault/invalid:name'}); current = dialog();
  await reply(listing('/home/vault/invalid:name', {root: '/home/vault', relative: 'invalid:name',
    parent: '/home/vault', folders: [], selectable: false,
    selection_disabled_reason: 'This folder name is not supported in vault settings.'}));
  assert(button(current, 'Select this folder').disabled);
  const selectionReason = cls(current, 'folder-picker-selection-reason');
  assert(!selectionReason.hidden);
  assert.match(selectionReason.textContent, /not supported in vault settings/);
  assert(!cls(current, 'folder-picker-empty').textContent.includes('You can select this folder'));
  button(current, 'Select this folder').click();
  assert.equal(dialog(), current, 'disabled selection cannot resolve the picker');
  button(current, 'Up one level').click();
  await reply(listing('/home/vault', {root: '/home/vault', relative: '.', parent: null, selectable: true}));
  assert(selectionReason.hidden, 'valid navigation clears the selection restriction');
  assert(!button(current, 'Select this folder').disabled);
  button(current, 'Select this folder').click();
  assert.equal((await result).relative, '.');

  // A missing capture inbox recovers within its vault, without losing the scope.
  result = choose({root: '/home/vault', path: '/home/vault/inbox'}); current = dialog();
  await reply({detail: 'Missing folder'}, false);
  assert.equal(new URL(calls.at(-1).url, 'http://localhost').searchParams.get('path'), '/home/vault');
  await reply(listing('/home/vault', {root: '/home/vault', relative: '.', parent: null}));
  assert.match(current.textContent, /previous folder is unavailable/);
  button(current, 'Cancel').click(); await result;

  // Creation uses a single name and the loaded parent; invalid input never posts.
  result = choose({root: '/home/vault'}); current = dialog();
  await reply(listing('/home/vault', {root: '/home/vault', relative: '.', parent: null}));
  button(current, 'New folder').click();
  const form = cls(current, 'folder-picker-create');
  const name = all(form).find((n) => n.name === 'folder_name');
  assert.equal(document.activeElement, name);
  name.value = '../elsewhere';
  const beforeInvalid = calls.length;
  await submit(form);
  assert.equal(calls.length, beforeInvalid);
  assert.match(form.textContent, /without slashes/);
  name.value = 'Personal';
  await submit(form);
  assert.equal(calls.at(-1).url, '/api/folders');
  assert.equal(calls.at(-1).options.method, 'POST');
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), {parent: '/home/vault', name: 'Personal', root: '/home/vault'});
  assert(button(current, 'Select this folder').disabled);
  await reply({detail: 'A folder with that name already exists.'}, false);
  assert.match(form.textContent, /already exists/);
  assert(!button(current, 'Select this folder').disabled, 'creation error retains the valid parent selection');
  name.value = 'Journal'; await submit(form);
  await reply(listing('/home/vault/Journal', {root: '/home/vault', relative: 'Journal', folders: []}));
  assert(form.hidden);
  assert.match(current.textContent, /Folder created/);
  button(current, 'Select this folder').click();
  assert.equal((await result).relative, 'Journal');

  // A stalled create may have reached the server. Release the form and offer
  // a read-only refresh instead of automatically posting the mutation again.
  result = choose({root: '/home/vault'}); current = dialog();
  await reply(listing('/home/vault', {root: '/home/vault', relative: '.', parent: null}));
  button(current, 'New folder').click();
  const stalledCreateForm = cls(current, 'folder-picker-create');
  all(stalledCreateForm).find((n) => n.name === 'folder_name').value = 'New notes';
  await submit(stalledCreateForm);
  const stalledCreate = calls.at(-1);
  const countBeforeCreateTimeout = calls.length;
  await elapse(10000);
  assert.equal(calls.length, countBeforeCreateTimeout, 'timed-out creates are never retried automatically');
  assert(stalledCreate.options.signal.aborted);
  assert.equal(current.attributes['aria-busy'], 'false');
  assert.match(stalledCreateForm.textContent, /not confirmed whether the folder was created/);
  button(current, 'Refresh folders').click();
  const refreshRead = calls.at(-1);
  assert.equal(refreshRead.options.method, undefined, 'recovery only reads the parent');
  assert.equal(new URL(refreshRead.url, 'http://localhost').searchParams.get('path'), '/home/vault');
  await reply(listing('/home/vault/New notes', {relative: 'New notes'}), true, stalledCreate);
  assert.equal(current.attributes['aria-busy'], 'true', 'late create reply cannot interrupt refresh');
  await reply(listing('/home/vault', {root: '/home/vault', relative: '.'}), true, refreshRead);
  button(current, 'Cancel').click(); await result;

  // Close cancels a create request too; it cannot reopen or populate a dialog.
  result = choose(); current = dialog();
  await reply(listing());
  button(current, 'New folder').click();
  const closingForm = cls(current, 'folder-picker-create');
  all(closingForm).find((n) => n.name === 'folder_name').value = 'Pending';
  await submit(closingForm);
  const closingCreate = calls.at(-1);
  button(current, 'Close').click();
  assert.equal((await result).cancelled, true);
  await tick();
  assert(closingCreate.options.signal.aborted);
  assert.equal(timers.size, 0);
  await reply(listing('/home/demo/Pending'), true, closingCreate);
  assert.equal(dialog(), undefined);

  // Sandboxes still browse; creation explains its disabled state.
  result = choose(); current = dialog();
  await reply(listing('/home/demo', {can_create: false, create_disabled_reason: 'Folder creation is unavailable in this sandbox.'}));
  assert(button(current, 'New folder').disabled);
  assert.match(current.textContent, /unavailable in this sandbox/);
  assert(!button(current, 'Select this folder').disabled);
  button(current, 'Select this folder').click(); await result;
  assert.equal(listeners.keydown.size, 0, 'closing removes every global listener');
  assert.equal(timers.size, 0, 'completed and cancelled requests leave no deadline timers');
  console.log('Folder picker UI behavior passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


class FolderPickerUiTests(unittest.TestCase):
    def test_folder_navigation_selection_creation_and_cancellation(self):
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

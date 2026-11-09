// The Concept Cloud keeps its layout across chat polls: the same concepts in
// the same box are not redrawn, and a fresh draw puts them in the same places.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('static/app.js', 'utf8');

function node(tag, cls, text) {
  return {tag, className: cls, textContent: text, style: {}, children: [],
    appendChild(c) { this.children.push(c); }, addEventListener() {}};
}
function box(w, h) {
  let html = '';
  return {clientWidth: w, clientHeight: h, children: [], builds: 0,
    get firstChild() { return this.children[0] || (html ? {} : null); },
    get innerHTML() { return html; },
    set innerHTML(v) { html = v; this.children = []; if (v === '') this.builds++; },
    appendChild(c) { this.children.push(c); }};
}
const context = vm.createContext({
  document: {createElement: () => ({getContext: () => ({
    font: '', measureText: (s) => ({width: s.length * 7})})})},
  WeakMap, ResizeObserver: class { observe() {} disconnect() {} },
  el: node, openNote() {}, openFindQuery() {}, findChatSession: null,
});
const a = app.indexOf('const FIND_CLOUD_CTX'), b = app.indexOf('function renderFindClouds()');
assert(a >= 0 && b > a);
vm.runInContext(app.slice(a, b), context);

const terms = ['Accelerando', 'Charles Stross', 'technological singularity', 'Economics 2.0',
  'Manfred Macx', 'Matrioshka brain', 'Moonshots', 'Peter Diamandis'];
context.findChatSession = {concepts: terms.map((term, i) =>
  ({term, weight: 1 - i * 0.1, turns: 1, primary_path: ''}))};
const where = (t) => t.children.map((c) => [c.textContent, c.style.left, c.style.top]);

const first = box(460, 560);
context.renderFindCloudInto(first);
assert.equal(first.builds, 1);
assert(first.children.length > 0, 'the cloud draws its concepts');
const placed = where(first);
for (let poll = 0; poll < 5; poll++) context.renderFindCloudInto(first);
assert.equal(first.builds, 1, 'a poll with unchanged concepts does not redraw');
assert.deepEqual(where(first), placed);

const again = box(460, 560);
context.renderFindCloudInto(again);
assert.deepEqual(where(again), placed, 'the same concepts land in the same places');

context.findChatSession.concepts.push({term: 'The Innermost Loop', weight: 0.2, turns: 1});
context.renderFindCloudInto(first);
assert.equal(first.builds, 2, 'a new concept redraws the cloud');
first.clientWidth = 600;
context.renderFindCloudInto(first);
assert.equal(first.builds, 3, 'a resized box redraws the cloud');
console.log('concept cloud ui ok');

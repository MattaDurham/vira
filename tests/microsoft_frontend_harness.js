"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag, cls = "", text = "") {
    this.tag = tag; this.className = cls || ""; this.textContent = text || "";
    this.children = []; this.parentNode = null; this.attributes = {}; this.dataset = {};
    this.value = ""; this.disabled = false; this.isConnected = true;
    this.classList = {add() {}, remove() {}};
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(e => e !== this);
    this.parentNode = null;
  }
  appendChild(child) { child.remove(); this.children.push(child); child.parentNode = this; return child; }
  append(...children) { children.forEach(e => this.appendChild(e)); }
  replaceChildren() { this.children.forEach(e => { e.parentNode = null; }); this.children = []; }
  insertBefore(child, before) { child.remove(); this.children.splice(this.children.indexOf(before), 0, child); child.parentNode = this; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  focus() {}
}
function all(root) { return [root, ...root.children.flatMap(all)]; }
function button(root, text) {
  const result = all(root).find(e => e.tag === "button" && e.textContent === text);
  assert.ok(result, "Missing button: " + text); return result;
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const client = "00000000-0000-4000-8000-000000000001";
const tenant = "00000000-0000-4000-8000-000000000003";
let saved = {configured:false, error:""}, polls = [], calls = [];
const context = {
  el: (tag, cls, text) => new Element(tag, cls, text),
  mkInput: () => new Element("input"), location:{hostname:"localhost"},
  window:{open() { throw new Error("Embedded browser popup must not be used"); }},
  errText:e => e.message, refreshMail() {}, copyText() {},
  api: async url => url.endsWith("browser/status") ? {connected:true,email:"casey@example.com"} : saved,
  post: async (url, body) => {
    calls.push({url,body});
    if (url.endsWith("setup/prepare")) return {stage:"register",registration:saved,candidates:[],issues:[],truncated:false};
    if (url.endsWith("/registration")) saved = {configured:true,client_id:body.client_id,tenant:body.tenant};
    return {opened:true};
  },
  startPoll: fn => { const h = {stop() {h.stopped = true;}}; polls.push({fn,h}); return h; },
};
const source = fs.readFileSync(path.join(__dirname,"../static/app.js"),"utf8");
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf("function graphRegistrationForm("), source.indexOf("// WhatsApp card:")), context);
(async () => {
  const form = context.graphAddForm(); await flush();
  await button(form,"Connect Microsoft").onclick();
  assert.ok(all(form).some(e => e.textContent === "Step 1 of 3 · Create the app registration"));
  await button(form,"Registration created — continue").onclick();
  assert.ok(all(form).some(e => e.textContent.includes("Copy both IDs")));
  assert.equal(calls.filter(c => c.url.endsWith("/registration")).length,0);
  const input = name => all(form).find(e => e.attributes["aria-label"] === name);
  input("Setup application (client) ID").value = client;
  input("Setup directory (tenant) ID").value = tenant;
  await button(form,"Registration created — continue").onclick();
  await button(form,"Open API permissions").onclick();
  assert.equal(calls.at(-1).body.step,"permissions");
  await button(form,"Permissions added — continue").onclick();
  assert.ok(all(form).some(e => e.tag === "code" && e.textContent === "http://localhost/api/mail/graph/browser/callback"));
  await button(form,"Back").onclick();
  await button(form,"Back").onclick();
  assert.equal(input("Setup application (client) ID").value,client);
  await button(form,"Registration created — continue").onclick();
  await button(form,"Permissions added — continue").onclick();
  await button(form,"Save and sign in").onclick(); await flush();
  assert.equal(saved.client_id,client);
  assert.ok(calls.some(c => c.url.endsWith("browser/system")));
  const poll = polls.at(-1); await poll.fn(poll.h);
  assert.ok(poll.h.stopped);
  assert.ok(all(form).some(e => e.textContent === "casey@example.com connected. Mail and calendar are ready."));
  assert.equal(all(form).filter(e => e.className === "graph-setup-guide").length,0);
  process.stdout.write("Guided setup and system-browser confirmation passed.\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });

"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag, cls = "", text = "") {
    this.tag = tag; this.className = cls || ""; this.textContent = text || "";
    this.children = []; this.parentNode = null; this.attributes = {}; this.dataset = {};
    this.value = ""; this.listeners = {}; this.disabled = false; this.isConnected = true;
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(e => e !== this);
    this.parentNode = null;
  }
  appendChild(child) { child.remove(); this.children.push(child); child.parentNode = this; return child; }
  append(...children) { children.forEach(e => this.appendChild(e)); }
  replaceChildren(...children) { this.children.forEach(e => { e.parentNode = null; }); this.children = []; this.append(...children); }
  get firstChild() { return this.children[0]; }
  querySelector(selector) { return all(this).find(e => selector === "input" ? e.tag === "input" : e.className.split(" ").includes(selector.slice(1))); }
  insertBefore(child, before) { child.remove(); this.children.splice(this.children.indexOf(before), 0, child); child.parentNode = this; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatchEvent(event) { (this.listeners[event.type] || []).forEach(fn => fn(event)); }
  focus() {}
}
function all(root) { return [root, ...root.children.flatMap(all)]; }
function button(root, text) {
  const result = all(root).find(e => e.tag === "button" && (e.textContent === text || e.attributes["aria-label"] === text));
  assert.ok(result, "Missing button: " + text); return result;
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const client = "00000000-0000-4000-8000-000000000001";
const tenant = "00000000-0000-4000-8000-000000000003";
let saved = {configured:false, error:""}, polls = [], calls = [], copied = [];
const context = {
  el: (tag, cls, text) => new Element(tag, cls, text),
  mkInput: () => new Element("input"), location:{hostname:"localhost"},
  window:{open() { throw new Error("Embedded browser popup must not be used"); }},
  errText:e => e.message, refreshMail() {}, copyText(value) { copied.push(value); }, Event,
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
vm.runInContext(source.slice(source.indexOf("function mailAddForms("), source.indexOf("function imapAddForm(")), context);
vm.runInContext(source.slice(source.indexOf("function toggleReconnect("), source.indexOf("// Paint a probe/reconnect result")), context);
vm.runInContext(source.slice(source.indexOf("function graphReconnectForm("), source.indexOf("function mailAddForms(")), context);
vm.runInContext(source.slice(source.indexOf("function graphRegistrationForm("), source.indexOf("// WhatsApp card:")), context);
(async () => {
  const form = context.graphAddForm(); await flush();
  assert.equal(all(form).filter(e => e.tag === "button" && e.textContent === "Connect Microsoft").length, 1);
  assert.ok(!all(form).some(e => e.textContent === "Let Vira set this up"));
  await form.start();
  assert.equal(calls.at(-1).url,"/api/mail/graph/setup/open");
  assert.equal(calls.at(-1).body.step,"register");
  assert.ok(all(form).some(e => e.textContent === "Step 1 of 3 · Register Vira"));
  assert.ok(all(form).find(e => e.className === "graph-registration").hidden);
  await button(form,"Continue").onclick();
  assert.ok(all(form).some(e => e.textContent.includes("Paste both IDs")));
  assert.equal(calls.filter(c => c.url.endsWith("/registration")).length,0);
  const input = name => all(form).find(e => e.attributes["aria-label"] === name);
  let prevented = false;
  input("Setup application (client) ID").dispatchEvent({type:"paste",preventDefault() {prevented=true;},clipboardData:{getData:()=>
    "Application (client) ID: " + client + "\nObject ID: 00000000-0000-4000-8000-000000000099\nDirectory (tenant) ID: " + tenant}});
  assert.ok(prevented);
  assert.equal(input("Setup application (client) ID").value,client);
  assert.equal(input("Setup directory (tenant) ID").value,tenant);
  await button(form,"Copy Name").onclick(); assert.equal(copied.at(-1),"Vira");
  await button(form,"Continue").onclick();
  assert.equal(calls.at(-1).body.step,"permissions");
  assert.ok(all(form).some(e => e.textContent.includes("send mail when you click Send")));
  await button(form,"Copy Mail.Send").onclick(); assert.equal(copied.at(-1),"Mail.Send");
  await button(form,"Continue").onclick();
  assert.equal(calls.at(-1).body.step,"authentication");
  await button(form,"Copy Callback").onclick();
  assert.equal(copied.at(-1),"http://localhost/api/mail/graph/browser/callback");
  await button(form,"Back").onclick(); await button(form,"Back").onclick();
  assert.equal(input("Setup application (client) ID").value,client);
  await button(form,"Continue").onclick(); await button(form,"Continue").onclick();
  await button(form,"Save and sign in").onclick(); await flush();
  assert.equal(saved.client_id,client);
  assert.ok(calls.some(c => c.url.endsWith("browser/system")));
  const poll = polls.at(-1); await poll.fn(poll.h);
  assert.ok(poll.h.stopped);
  assert.ok(all(form).some(e => e.textContent === "casey@example.com connected. Mail and calendar are ready."));
  assert.equal(all(form).filter(e => e.className === "graph-setup-guide").length,0);
  assert.equal(button(form,"Connect Microsoft").hidden,false);
  const review = context.graphAddForm(); await flush();
  await button(review,"Review registration and permissions").onclick();
  assert.equal(calls.at(-1).body.step,"permissions");
  assert.ok(!all(review).some(e => e.textContent === "Step 1 of 3 · Register Vira"));
  await button(review,"Cancel setup").onclick();
  assert.equal(button(review,"Connect Microsoft").hidden,false);
  const first = new Element("input"), second = new Element("input");
  context.graphPasteIds(first,second);
  first.dispatchEvent({type:"paste",preventDefault() {},clipboardData:{getData:()=>client + "\n" + tenant}});
  assert.equal(first.value,client); assert.equal(second.value,tenant);
  first.value=""; second.value="";
  first.dispatchEvent({type:"paste",preventDefault() {throw new Error("Ambiguous paste consumed");},clipboardData:{getData:()=>client + "\n" + tenant + "\n" + client}});
  assert.equal(first.value,""); assert.equal(second.value,"");
  // Exercise the actual provider and reconnect entry points: neither needs
  // a second Connect click to begin setup or sign-in.
  const card = new Element("section");
  context.mailAddForms(card,{mail:{accounts:[]}});
  let before = calls.length;
  button(card,"Microsoft 365").onclick(); await flush();
  assert.ok(calls.slice(before).some(c => c.url.endsWith("browser/system")));
  const account = new Element("div"), host = new Element("div","acct-form-host");
  account.appendChild(host); before = calls.length;
  context.toggleReconnect(account,{kind:"graph",email:"casey@example.com"}); await flush();
  assert.ok(calls.slice(before).some(c => c.url.endsWith("browser/system")));
  context.toggleReconnect(account,{kind:"graph",email:"casey@example.com"});
  assert.equal(host.children.length,0);
  process.stdout.write("Automatic setup pages, copy controls, ID autofill and sign-in confirmation passed.\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });

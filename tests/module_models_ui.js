// Real picker lifecycle with a minimal DOM: no browser, provider, or owner data.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag = "div", cls = "", text = "") {
    this.tagName = tag.toUpperCase(); this.className = cls || "";
    this.textContent = text; this.children = []; this.dataset = {};
    this.listeners = {}; this.attributes = {}; this.style = {};
    this.value = ""; this.disabled = false; this.parentNode = null;
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  set innerHTML(value) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  matches(selector) {
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return this.className.split(" ").includes(selector.slice(1));
    if (selector === "[data-module-model]") return this.dataset.moduleModel !== undefined;
    return this.tagName.toLowerCase() === selector;
  }
  closest(selector) {
    for (let node = this; node; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  async emit(type, event = {}) {
    for (const callback of this.listeners[type] || []) await callback({ type, target: this, ...event });
  }
  focus() { this.focused = true; }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    this.parentNode = null;
  }
  get isConnected() { return this === body || !!this.parentNode?.isConnected; }
  getBoundingClientRect() { return { left: 12, top: 20, bottom: 40, height: 240 }; }
}

const body = new Element("body");
const el = (tag, cls, text) => new Element(tag, cls, text);
const view = (id) => {
  const node = el("section", "view"); node.id = "view-" + id;
  node.appendChild(el("div", "section-head")); body.appendChild(node); return node;
};
const findView = view("find"), peopleView = view("people"), feedView = view("feed");
peopleView.appendChild(el("div", "section-head")); // Networking has its own heading.
const plainView = view("research");
let modules = [
  { id: "find", windows: ["find"], kind: "completion", title: "Find",
    description: "Used for new answers.", selection: null,
    effective: { provider: "anthropic", backend: "cli", model: "fable" } },
  { id: "people", windows: ["people", "feed"], kind: "completion", title: "People",
    selection: null, effective: { provider: "anthropic", backend: "cli", model: "fable" } },
];
const catalog = { roster: [], providers: [
  { id: "anthropic", label: "Claude", connected: true, auth: "signed_in", sessions: true,
    has_key: true, cli: [{ id: "fable", label: "Fable" }], api: [{ id: "claude-api", label: "API model" }] },
  { id: "openai", label: "OpenAI", connected: true, auth: "signed_in", sessions: true,
    has_key: false, cli: [{ id: "gpt-future", label: "Future" }], api: [{ id: "api-only" }] },
  { id: "google", label: "Google", connected: true, auth: "key", sessions: true,
    has_key: true, cli: [], api: [{ id: "gemini-future" }] },
  { id: "disabled", label: "Disabled", connected: true, disabled: true, auth: "signed_in",
    sessions: true, cli: [{ id: "blocked" }] },
  { id: "logged-out", label: "Logged out", connected: false, auth: "logged_out",
    sessions: true, cli: [{ id: "unavailable" }] },
] };
const requests = [], writes = [], notices = [], refreshes = [];
let failSave = false, failCatalog = false, nextPrompt = null;
const clone = (value) => JSON.parse(JSON.stringify(value));
const context = vm.createContext({
  window: {}, document: {
    querySelectorAll: (selector) => body.querySelectorAll(selector),
    getElementById: (id) => body.querySelector("#" + id),
  }, el, innerHeight: 900,
  closeCtxPops() { body.querySelectorAll(".ctx-pop").forEach((pop) => pop.remove()); },
  placeCtxPop(pop) { body.appendChild(pop); },
  modelCatalog: async (refresh) => {
    refreshes.push(!!refresh);
    return failCatalog ? { error: "Catalog unavailable" } : clone(catalog);
  },
  api: async (url, opts = {}) => {
    requests.push({ url, method: opts.method || "GET" });
    if (url === "/api/module-models") return { modules: clone(modules) };
    assert.equal(opts.method, "DELETE", "opening the picker must only read module settings");
    writes.push({ url, method: "DELETE" });
    if (failSave) throw new Error("Storage unavailable");
    modules.find((m) => url.endsWith("/" + m.id)).selection = null;
    return {};
  },
  put: async (url, payload) => {
    writes.push({ url, method: "PUT", payload: clone(payload) });
    if (failSave) throw new Error("Storage unavailable");
    modules.find((m) => url.endsWith("/" + m.id)).selection = clone(payload);
    return {};
  },
  toast: (text) => notices.push(text), prompt: () => nextPrompt,
});
vm.runInContext(fs.readFileSync("static/module-models.js", "utf8"), context);
const models = context.window.ModuleModels;
const currentPopup = () => body.querySelector(".module-model-pop");
const control = (text) => currentPopup().querySelectorAll("button").find((b) => b.textContent === text);
const key = (choice) => JSON.stringify(choice);
const gpt = { provider: "openai", backend: "cli", model: "gpt-future" };

(async () => {
  // Discovery follows actual catalog authentication, capability, and transport.
  const completion = models.choices(catalog, { kind: "completion" });
  assert(completion.some((r) => r.selection.model === "gpt-future"));
  assert(completion.some((r) => r.selection.model === "gemini-future"));
  assert(!completion.some((r) => ["blocked", "unavailable", "api-only"].includes(r.selection.model)));
  const session = models.choices(catalog, { kind: "session" });
  assert(!session.some((r) => r.selection.provider === "anthropic" && r.selection.backend === "api"));
  assert(session.some((r) => r.selection.provider === "google" && r.selection.backend === "api"));
  const curated = models.choices({ ...catalog, roster: ["gpt-future"] }, { kind: "completion" });
  assert(curated.filter((r) => !r.custom).every((r) => r.selection.model === "gpt-future"));
  assert(curated.some((r) => r.custom), "a curated roster retains custom model entry");

  await models.load(); await models.load();
  assert.equal(findView.querySelectorAll("[data-module-model]").length, 1, "refresh does not duplicate controls");
  assert.equal(peopleView.querySelectorAll("[data-module-model]").length, 2, "both People tabs expose the shared choice");
  assert.equal(plainView.querySelectorAll("[data-module-model]").length, 0);
  const anchor = findView.querySelector("button");
  assert.match(anchor.textContent, /fable/);
  await models.open("find", anchor);
  assert.equal(writes.length, 0, "opening a picker never changes preferences or starts work");
  assert(requests.every((r) => r.url.startsWith("/api/module-models")));
  let select = currentPopup().querySelector("select");
  select.value = key(gpt); await select.emit("change");
  assert.equal(writes.length, 0, "an unsaved selection is local UI state");
  await control("Save").emit("click");
  assert.deepEqual(writes[0], { url: "/api/module-models/find", method: "PUT", payload: gpt });
  assert.equal(currentPopup(), null);
  assert.match(anchor.textContent, /openai.*gpt-future/);
  assert(anchor.focused);

  // Saved values absent from a fresh catalog cannot silently become another model.
  modules[0].selection = { provider: "openai", backend: "cli", model: "retired-id" };
  await models.open("find", anchor);
  select = currentPopup().querySelector("select");
  assert.equal(select.value, key(modules[0].selection));
  assert(select.querySelectorAll("option").some((o) => /retired-id.*unverified/.test(o.textContent)));
  await control("Refresh models").emit("click");
  assert.equal(select.value, key(modules[0].selection));
  assert(refreshes.includes(true));
  await control("Cancel").emit("click");
  assert.equal(writes.length, 1);

  // Failure leaves the current effective preference intact and permits retry.
  await models.open("find", anchor);
  const priorLabel = anchor.textContent;
  select = currentPopup().querySelector("select");
  select.value = key(gpt); await select.emit("change"); failSave = true;
  await control("Save").emit("click");
  assert.match(currentPopup().querySelector(".module-model-error").textContent, /Storage unavailable/);
  assert.equal(anchor.textContent, priorLabel);
  assert.equal(control("Save").disabled, false);
  assert.equal(select.disabled, false);
  failSave = false; await control("Save").emit("click");
  assert.equal(currentPopup(), null);

  // Reset removes only this module's override and returns to the inherited default.
  await models.open("find", anchor);
  select = currentPopup().querySelector("select"); select.value = ""; await select.emit("change");
  await control("Save").emit("click");
  assert.deepEqual(writes.at(-1), { url: "/api/module-models/find", method: "DELETE" });
  assert.equal(modules[0].selection, null);

  // Custom input keeps the chosen provider and transport, including API providers.
  await models.open("find", anchor);
  select = currentPopup().querySelector("select");
  nextPrompt = "gemini-unlisted";
  select.value = "custom:" + key({ provider: "google", backend: "api", model: "" });
  await select.emit("change");
  await control("Save").emit("click");
  assert.deepEqual(writes.at(-1).payload, { provider: "google", backend: "api", model: "gemini-unlisted" });

  // Incoming and People are the same setting on desktop, mobile, and alias controls.
  await models.open("feed", feedView.querySelector("button"));
  select = currentPopup().querySelector("select"); select.value = key(gpt); await select.emit("change");
  await control("Save").emit("click");
  assert.equal(writes.at(-1).url, "/api/module-models/people");
  assert.equal(peopleView.querySelector("button").textContent, feedView.querySelector("button").textContent);
  const mobileItem = models.contextItem(feedView, 20, 30);
  assert.match(mobileItem.hint, /gpt-future/);
  const desktop = el("div", "fwin"); desktop.dataset.wid = "feed";
  assert.equal(models.contextItem(desktop, 20, 30).hint, mobileItem.hint);
  assert.equal(models.contextItem(plainView, 20, 30), null);

  // A catalog failure remains visible and retry is read-only.
  const priorWrites = writes.length; failCatalog = true;
  await models.open("find", anchor);
  assert.match(currentPopup().querySelector(".ctx-note").textContent, /Catalog unavailable/);
  assert(control("Retry"));
  failCatalog = false; await control("Retry").emit("click");
  assert(currentPopup().querySelector("select"));
  let stopped = false;
  await currentPopup().emit("keydown", { key: "Escape", stopPropagation() { stopped = true; } });
  assert(stopped); assert.equal(currentPopup(), null); assert.equal(writes.length, priorWrites);
  console.log("Module model picker lifecycle passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });

"use strict";

// Run the shipped script against a small DOM and mocked HTTP boundary. No
// real sources, notification channels or calendars are available here.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parentNode = null;
    this.dataset = {}; this.attributes = {}; this.listeners = {};
    this.className = ""; this.textContent = ""; this.style = {};
    this.disabled = false; this.open = false;
    this.classList = { add() {}, remove() {}, contains: (cls) => this.className.split(" ").includes(cls) };
  }
  appendChild(child) {
    child.remove(); this.children.push(child); child.parentNode = this; return child;
  }
  prepend(child) {
    child.remove(); this.children.unshift(child); child.parentNode = this;
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((n) => n !== this);
    this.parentNode = null;
  }
  replaceChildren() { this.children.forEach((n) => { n.parentNode = null; }); this.children = []; }
  get childElementCount() { return this.children.length; }
  get parentElement() { return this.parentNode; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async emit(name) {
    for (const fn of this.listeners[name] || []) await fn({ preventDefault() {} });
  }
  querySelectorAll(selector) {
    const out = [];
    const matches = (n) => selector === "[data-assistant-id]" ? !!n.dataset.assistantId
      : selector.startsWith(".") ? n.className.split(" ").includes(selector.slice(1))
      : selector === n.tag;
    const visit = (n) => n.children.forEach((c) => { if (matches(c)) out.push(c); visit(c); });
    visit(this); return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getClientRects() { return [1]; }
  scrollIntoView(options) { this.scroll = options; }
}

const host = new Element("section"), pane = new Element("div");
const fixture = {
  enabled: true, active: true, worker_running: true, notification_ready: true,
  passive: false, fixture: false, last_success: "2030-01-01T12:00:00+00:00",
  settings: { assistant_enabled: true, assistant_contact_updates: true,
    mail_body_index: false,
    assistant_notify: false, assistant_quiet_start: 22, assistant_quiet_end: 8,
    assistant_timezone: "", assistant_notify_daily_cap: 3, assistant_stale_days: 3,
    assistant_due_soon_hours: 24, assistant_catchup_days: 14,
    assistant_calendar_work_start: 9, assistant_calendar_work_end: 17,
    assistant_calendar_block_minutes: 30,
    assistant_calendar_id: "", assistant_calendar_name: "Personal", assistant_calendar_auto_create: false },
  reminders: [{ id: "r1", status: "open", person_id: "sample", person_name: "Example Contact",
    owed_by: "me", what: "Send the agenda", priority: "high", due: "2030-01-01",
    evidence: [{ quote: "I will send the agenda tomorrow", channel: "email", when: "2029-12-31" }] }],
  calendar: { destinations: { available: true, default_id: "personal-id", selection: "configured_name",
    selected: { id: "personal-id", name: "Personal", writable: true, is_default: true },
    calendars: [{ id: "personal-id", name: "Personal", writable: true, is_default: true },
      { id: "work-id", name: "Work", writable: true, is_default: false },
      { id: "holidays-id", name: "Holidays", writable: false, is_default: false }] },
    drafts: [{ id: "c1", title: "Plan the week", status: "suggested",
    owner_only: true, start: "2030-01-02T10:00:00+00:00", end: "2030-01-02T11:00:00+00:00",
    can_create: true, can_export: true, attendees: [], quote: "Block my calendar tomorrow from 10am to 11am" },
  { id: "c2", title: "Meet Example Contact", status: "suggested", owner_only: false,
    attendees: ["Example Contact"], can_create: false, can_export: true }] },
  contact: { index_available: false, mail_body_index: false, errors: [] },
  coverage: ["Email body indexing is off."],
};
let snapshot = structuredClone(fixture), getError = "", actionResult = null, tick;
let requests = [], posts = [], opened = [];
const document = { hidden: false, createElement: (tag) => new Element(tag),
  querySelector: (selector) => selector === "#assistant-body" ? host : selector === "#attention-day-pane" ? pane : null };
const context = vm.createContext({ document,
  window: { matchMedia: () => ({ matches: true }) },
  setInterval: (callback) => { tick = callback; }, setTimeout: () => {},
  api: async (url) => { requests.push(url); if (getError) throw Error(getError);
    return structuredClone(url.startsWith("/api/assistant/calendars") ? snapshot.calendar.destinations : snapshot); },
  post: async (url, data) => {
    posts.push({ url, data: JSON.parse(JSON.stringify(data)) });
    if (url.endsWith("/config")) { snapshot.settings = { ...snapshot.settings, ...data }; return structuredClone(snapshot.settings); }
    if (data.action === "done" || data.action === "snooze") {
      snapshot.reminders = [];
      return { status: data.action === "done" ? "closed" : "snoozed" };
    }
    if (data.action === "date") {
      snapshot.reminders[0] = { ...snapshot.reminders[0], deadline_review: null, due: data.due, stage: "due" };
      return { id: snapshot.reminders[0].id, status: "open", due: data.due };
    }
    snapshot.calendar.drafts[0] = { ...snapshot.calendar.drafts[0], ...actionResult };
    return actionResult;
  },
  openPerson: (id) => opened.push(id), openApp: (id) => opened.push(id), refreshAlerts() {},
});
const root = path.resolve(__dirname, "..");
vm.runInContext(fs.readFileSync(path.join(root, "static", "assistant.js"), "utf8"), context);
const ui = context.window.ViraAssistant;
const text = (n = host) => [n.textContent, ...n.children.map(text)].join(" ");
const button = (label, parent = host) => parent.querySelectorAll("button").find((n) => n.textContent === label);
const field = (name) => [...host.querySelectorAll("input"), ...host.querySelectorAll("select")].find((n) => n.name === name);
const card = (id) => host.querySelectorAll("[data-assistant-id]").find((n) => n.dataset.assistantId === id);

(async () => {
  await ui.load();
  assert.deepEqual(requests, ["/api/assistant"]);
  assert.equal(posts.length, 0, "viewing status cannot write anything");
  assert.match(text(), /Running/);
  assert.match(text(), /Message index Unavailable/);
  assert.match(text(), /Email body indexing Off/);
  assert.equal(button("Create my event", card("c2")), undefined, "meetings cannot offer personal creation");
  assert.match(text(card("c2")), /Add invitees in your calendar/);

  const evidence = card("r1").querySelector(".assistant-evidence");
  evidence.open = true;
  await ui.load();
  assert.equal(card("r1").querySelector(".assistant-evidence"), evidence, "unchanged polling preserves the open evidence DOM");

  const editingForm = host.querySelector("form");
  field("assistant_calendar_id").value = "id:work-id";
  await editingForm.emit("input");
  snapshot.contact.errors = [{ person_id: "sample", error: "ValueError", retry_at: 1900000000 }];
  await ui.load();
  assert.equal(host.querySelector("form"), editingForm, "polling preserves unsaved settings");
  assert.match(text(), /Needs attention/);
  assert.equal(card("r1").querySelector(".assistant-evidence").open, true);
  await button("Open affected contact").emit("click");
  assert.deepEqual(opened, ["sample"]);

  const done = button("Done", card("r1"));
  await done.emit("click");
  await done.emit("click");
  assert.equal(posts.length, 1, "repeated action gestures submit only once");
  assert.deepEqual(posts[0], { url: "/api/assistant/reminders/r1", data: { action: "done" } });
  assert.equal(card("r1"), undefined, "completed reminders update even while settings are edited");
  assert.match(text(), /Commitment marked done\./);
  assert.equal(field("assistant_calendar_id").value, "id:work-id");
  await button("Cancel edits").emit("click");
  assert.equal(field("assistant_calendar_id").value, "saved-name");
  assert.equal(field("assistant_calendar_name"), undefined, "calendar destinations never require typing an exact name");

  field("assistant_notify").checked = true;
  await host.querySelector("form").emit("input");
  assert.equal(posts.length, 1, "editing a checkbox alone cannot enable texts");
  await host.querySelector("form").emit("submit");
  assert.equal(posts[1].url, "/api/assistant/config");
  assert.equal(posts[1].data.assistant_notify, true);
  assert.equal(posts[1].data.mail_body_index, false);
  assert.equal(typeof posts[1].data.assistant_catchup_days, "number");
  assert.equal(posts[1].data.assistant_calendar_work_start, 9);
  assert.equal(posts[1].data.assistant_calendar_work_end, 17);
  assert.equal(posts[1].data.assistant_calendar_block_minutes, 30);
  assert.equal(posts[1].data.assistant_calendar_id, "");
  assert.equal(posts[1].data.assistant_calendar_name, "Personal", "saving unrelated settings preserves a legacy destination");
  assert.match(text(), /Assistant settings saved/);
  const beforeEmailOptIn = posts.length;
  field("mail_body_index").checked = true;
  await host.querySelector("form").emit("input");
  assert.equal(posts.length, beforeEmailOptIn, "email body indexing changes only after saving");
  await host.querySelector("form").emit("submit");
  assert.equal(posts.at(-1).url, "/api/assistant/config");
  assert.equal(posts.at(-1).data.mail_body_index, true);

  actionResult = { status: "uncertain", reason: "Check Calendar.app before retrying", can_create: false };
  await button("Create my event", card("c1")).emit("click");
  assert.match(text(host.querySelector(".assistant-feedback")), /Check Calendar.app/);
  assert.doesNotMatch(text(host.querySelector(".assistant-feedback")), /Event created/);
  assert.equal(button("Create my event", card("c1")), undefined);

  snapshot = structuredClone(fixture);
  await ui.load();
  actionResult = { status: "created", event_calendar: "Personal", can_create: false };
  await button("Create my event", card("c1")).emit("click");
  assert.match(text(), /Event created in Personal/);
  snapshot.reminders = structuredClone(fixture.reminders);
  await ui.load();
  await button("Snooze 1 day", card("r1")).emit("click");
  assert.match(text(), /snoozed for one day/);

  getError = "temporarily offline";
  await ui.load();
  assert.match(text(), /Assistant status unavailable: temporarily offline/);
  assert.match(text(), /snoozed for one day/, "a read failure must not erase the confirmed action result");
  getError = "";
  snapshot.active = false;
  await ui.load();
  assert.match(text(), /Inactive/);
  assert.equal(host.querySelector(".assistant-load-error"), null);
  snapshot.active = true;
  snapshot.calendar.error = "Calendar drafts need repair.";
  await ui.load();
  assert.match(text(host.querySelector(".assistant-head")), /Needs attention/);
  assert.match(text(), /Calendar drafts need repair/);
  delete snapshot.calendar.error;
  snapshot.settings.assistant_contact_updates = false;
  await ui.load();
  assert.match(text(), /Contact facts and summaries are paused/);
  assert.match(text(), /still checks messages for commitments/);
  assert.doesNotMatch(text(), /Reminders use the profiles already saved/);
  snapshot.settings.assistant_contact_updates = true;
  snapshot.passive = true;
  await ui.load();
  assert.match(text(), /Preview instance/);
  assert.equal(button("Save assistant settings").disabled, true);
  assert.equal(button("Dismiss", card("c2")).disabled, true);
  const beforePreviewSubmit = posts.length;
  await host.querySelector("form").emit("submit");
  assert.equal(posts.length, beforePreviewSubmit, "fixture form submission is guarded even without a mouse click");
  await ui.reveal("c2");
  assert.equal(card("c2").scroll.behavior, "auto", "reveal respects reduced motion");
  snapshot.reminders = Array.from({length: 12}, (_, i) => ({ ...fixture.reminders[0], id: "many" + i }));
  await ui.load();
  assert.equal(host.querySelector(".assistant-more").open, false);
  assert.match(text(), /Show 4 more commitments/);
  await ui.reveal("many11");
  assert.equal(host.querySelector(".assistant-more").open, true, "Attention deep links reveal reminders beyond the initial list");
  snapshot.reminders = [{ ...fixture.reminders[0], person_id: null, person_name: "Inbox", subject_key: "service:sample" }];
  snapshot.calendar.drafts = [{ ...fixture.calendar.drafts[0], schedule_kind: "commitment",
    time_chosen_by: "assistant", due: "2030-01-03", person_name: "Inbox",
    evidence: [{ quote: "Please send the agenda by January 3", channel: "email" }] }];
  await ui.load();
  assert.match(text(card("r1")), /Inbox/);
  assert.equal(button("Inbox", card("r1")), undefined, "a service commitment has no fake contact link");
  assert.match(text(card("c1")), /Time chosen by Vira/);
  assert.match(text(card("c1")), /Task due 2030-01-03/);
  assert.match(text(card("c1")), /Please send the agenda by January 3/);
  assert.doesNotMatch(text(card("c1")), /Deadline corrected by you/);
  snapshot.calendar.drafts[0].due = "2030-01-04";
  snapshot.calendar.drafts[0].deadline_authority = "owner";
  await ui.load();
  assert.match(text(card("c1")), /Task due 2030-01-04/);
  assert.match(text(card("c1")), /Deadline corrected by you/);
  assert.match(text(card("c1")), /Please send the agenda by January 3/, "owner corrections preserve original source evidence");
  snapshot.calendar.drafts[0].start = "";
  snapshot.calendar.drafts[0].end = "";
  await ui.load();
  assert.match(text(card("c1")), /has not found a time/);
  snapshot.passive = false;
  snapshot.reminders[0] = { ...snapshot.reminders[0], stage: "review",
    deadline_review: {text: "next Friday", proposed: "2030-01-04", reason: "Confirm which Friday was intended."} };
  await ui.load();
  assert.match(text(card("r1")), /Original timing: next Friday/);
  assert.match(text(card("r1")), /Proposed date: 2030-01-04/);
  assert.match(text(card("r1")), /Confirm which Friday/);
  const beforeDate = posts.length;
  await card("r1").querySelector("form").emit("submit");
  assert.equal(posts.length, beforeDate, "an unresolved proposed date is never silently accepted");
  const due = card("r1").querySelector("input");
  assert.equal(due.value, "", "owner correction is an explicit date choice");
  due.value = "2030-01-11";
  await due.emit("input");
  snapshot.last_run = "2030-01-01T12:01:00+00:00";
  await ui.load();
  assert.equal(card("r1").querySelector("input").value, "2030-01-11", "polling preserves an unfinished deadline correction");
  await card("r1").querySelector("form").emit("submit");
  assert.deepEqual(posts.at(-1), {url:"/api/assistant/reminders/r1",data:{action:"date",due:"2030-01-11"}});
  assert.match(text(), /Deadline set to 2030-01-11/);
  assert.equal(card("r1").querySelector(".assistant-deadline"), null);

  snapshot = structuredClone(fixture);
  await ui.load();
  const destination = field("assistant_calendar_id");
  assert.match(text(destination), /Auto - Personal \(system default\)/);
  assert.equal(destination.children.find((o) => o.value === "id:holidays-id").disabled, true);
  destination.value = "id:work-id";
  await destination.emit("change");
  field("assistant_calendar_block_minutes").value = "45";
  const editsBeforeRefresh = host.querySelector("form");
  snapshot.calendar.destinations.calendars.push({id:"new-id",name:"New calendar",writable:true});
  const beforeDiscovery = posts.length;
  await button("Refresh calendars").emit("click");
  assert.equal(requests.at(-1), "/api/assistant/calendars?refresh=true");
  assert.equal(posts.length, beforeDiscovery, "discovering destinations never saves a setting");
  assert.equal(host.querySelector("form"), editsBeforeRefresh);
  assert.equal(field("assistant_calendar_id").value, "id:work-id");
  assert.equal(field("assistant_calendar_block_minutes").value, "45");
  assert.match(text(destination), /New calendar/);
  await ui.load();
  assert.equal(host.querySelector("form"), editsBeforeRefresh, "polling preserves an unsaved destination choice");
  await host.querySelector("form").emit("submit");
  assert.equal(posts.at(-1).data.assistant_calendar_id, "work-id");
  assert.equal(posts.at(-1).data.assistant_calendar_name, "", "an explicit ID selection clears the legacy typed name");
  field("assistant_calendar_id").value = "";
  await field("assistant_calendar_id").emit("change");
  await host.querySelector("form").emit("submit");
  assert.equal(posts.at(-1).data.assistant_calendar_id, "");
  assert.equal(posts.at(-1).data.assistant_calendar_name, "", "Auto saves blank settings, never an inferred ID");
  snapshot.settings.assistant_calendar_id = "gone-id";
  snapshot.calendar.destinations = { available: false, calendars: [], selected: null, error: "Calendar access is unavailable." };
  await ui.load();
  assert.equal(field("assistant_calendar_id").value, "id:gone-id");
  assert.match(text(), /Saved calendar \(unavailable\)/);
  assert.match(text(), /Calendar access is unavailable/);
  snapshot.settings.assistant_calendar_id = "";
  snapshot.calendar.destinations = { available: true, default_id: "", selected: null,
    calendars: fixture.calendar.destinations.calendars.filter((c) => c.writable).map((c) => ({...c,is_default:false})) };
  await ui.load();
  assert.match(text(), /No system default was found. Choose a writable calendar above/);

  const count = requests.length;
  document.hidden = true;
  tick();
  assert.equal(requests.length, count, "hidden tabs do not poll");
  document.hidden = false;
  tick();
  assert.equal(requests.length, count + 1);

  // The focused page loads the exact same component without desktop boot.
  const landing = fs.readFileSync(path.join(root, "static", "assistant-page.html"), "utf8");
  assert.deepEqual([...landing.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]),
    ["/assistant-page.js", "/assistant.js"]);
  let ready;
  const landingRequests = [], navigations = [];
  const landingContext = vm.createContext({
    document: { addEventListener: (name, callback) => { assert.equal(name, "DOMContentLoaded"); ready = callback; } },
    window: { ViraAssistant: { load: () => vm.runInContext('api("/api/assistant")', landingContext) } },
    fetch: async (url, options) => { landingRequests.push({url, options}); return {ok:true,json:async()=>({})}; },
    location: { assign: (url) => navigations.push(url) },
  });
  vm.runInContext(fs.readFileSync(path.join(root, "static", "assistant-page.js"), "utf8"), landingContext);
  assert.equal(landingRequests.length, 0);
  ready();
  assert.deepEqual(landingRequests.map((r) => r.url), ["/api/assistant"], "focused boot never fetches unrelated modules");
  vm.runInContext('openPerson("sample/id"); openApp("setup")', landingContext);
  assert.deepEqual(navigations, ["/#person/sample%2Fid", "/#setup/notifications"]);
})().catch((error) => { console.error(error); process.exitCode = 1; });

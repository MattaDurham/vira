/* Pins are views of executive reminders. This module stores no task text,
   accepts only drags started here, and uses the existing reminder actions. */
"use strict";

window.ViraReminderStickies = (() => {
  const PATH = "/api/reminder-stickies";
  const MIME = "application/x-vira-reminder";
  const desktop = () => document.body.classList.contains("desktop");
  const cards = new Map();
  const eligible = new Set();
  const busy = new Set();
  const positionWrites = new Map();
  let items = [];
  let healthy = false;
  let readOnly = false;
  let actionsReadOnly = false;
  let loading = null;
  let drag = null;
  let moving = null;
  let refreshTimer = 0;
  let observer = null;
  let layer, list, toggle, live;
  const node = (tag, cls, text) => {
    const out = document.createElement(tag);
    if (cls) out.className = cls;
    if (text != null) out.textContent = text;
    return out;
  };
  const button = (label, fn, cls = "") => {
    const out = node("button", "reminder-sticky-button " + cls, label);
    out.type = "button";
    out.addEventListener("click", (event) => { event.stopPropagation(); fn(out); });
    return out;
  };
  const date = (value) => {
    if (!value) return "";
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString([], {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  };
  async function request(path = PATH, method = "GET", body) {
    const response = await fetch(path, { method, credentials: "same-origin",
      ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not update pinned reminders.");
    return data;
  }
  function announce(text) {
    if (live) live.textContent = text;
    if (typeof toast === "function") toast(text);
  }
  function clamp(geometry) {
    const width = Math.max(220, Math.min(600, geometry.width || 288, innerWidth - 24));
    const height = Math.max(180, Math.min(800, geometry.height || 250, innerHeight - 120));
    return { width, height,
      x: Math.max(12, Math.min(geometry.x || 0, innerWidth - width - 12)),
      y: Math.max(58, Math.min(geometry.y || 0, innerHeight - height - 66)) };
  }
  function position(card, geometry) {
    const p = clamp(geometry);
    card.style.left = p.x + "px";
    card.style.top = p.y + "px";
    card.style.width = p.width + "px";
    card.style.height = p.height + "px";
  }
  function raise(card) {
    if (typeof focusWin === "function" && desktop()) focusWin(card);
  }
  function focus(id) {
    const card = cards.get(id);
    if (!card) return;
    if (!desktop()) {
      layer.classList.add("expanded");
      toggle.setAttribute("aria-expanded", "true");
    }
    raise(card);
    card.querySelector(".reminder-sticky-grip")?.focus({ preventScroll: true });
    card.classList.add("reminder-sticky-flash");
    setTimeout(() => card.classList.remove("reminder-sticky-flash"), 1200);
  }
  function openSpace(target) {
    if (!desktop() || !(target instanceof Element)
        || document.body.classList.contains("focus-mode")) return false;
    if (target.closest(".fwin, .panel, .sheet, .dock, .topbar, .launchpad, .view, "
        + ".reminder-sticky, .ctx-menu, .ctx-pop, .alert-layer, #firstrun, "
        + "[role=dialog], button, a, input, textarea, select, [contenteditable]")) return false;
    return target === document.body || target === document.documentElement
      || target.matches("canvas, .constellation, #constellation, #reminder-stickies, .reminder-stickies-list");
  }
  async function pin(id, geometry) {
    if (!healthy || readOnly || !eligible.has(id)) return;
    if (cards.has(id)) { focus(id); return; }
    if (busy.has(id)) return;
    busy.add(id);
    try {
      const offset = items.length % 8 * 26;
      const rect = clamp(geometry || { x: innerWidth - 324 - offset, y: 94 + offset });
      await request(PATH, "POST", { reminder_id: id, ...rect });
      await refresh(true);
      focus(id);
      announce("Reminder pinned. It stays connected to the original task.");
    } catch (error) { announce(error.message); }
    finally { busy.delete(id); render(); }
  }
  async function unpin(id) {
    if (busy.has(id)) return;
    busy.add(id);
    try {
      await request(PATH + "/" + encodeURIComponent(id), "DELETE");
      items = items.filter((item) => item.id !== id);
      render();
      announce("Reminder unpinned. The task is unchanged.");
    } catch (error) { announce(error.message); }
    finally { busy.delete(id); }
  }
  async function savePosition(id, geometry) {
    const item = items.find((row) => row.id === id);
    if (!item) return;
    Object.assign(item, geometry);
    busy.add(id);
    const prior = positionWrites.get(id) || Promise.resolve();
    const write = prior.catch(() => {}).then(() => request(PATH + "/" + encodeURIComponent(id), "PUT", geometry));
    positionWrites.set(id, write);
    try { await write; }
    catch (error) { announce("Position was not saved. " + error.message); }
    finally {
      if (positionWrites.get(id) === write) { positionWrites.delete(id); busy.delete(id); refresh(); }
    }
  }
  async function act(id, action) {
    if (!healthy || readOnly || actionsReadOnly || busy.has(id)) return;
    const item = items.find((row) => row.id === id);
    if (!item || !["open", "snoozed"].includes(item.state)) return;
    busy.add(id);
    cards.get(id)?.querySelectorAll(".reminder-sticky-task-action").forEach((b) => { b.disabled = true; });
    try {
      const result = await request("/api/assistant/reminders/" + encodeURIComponent(id), "POST",
        { action, ...(action === "snooze" ? { hours: 24 } : {}) });
      // Keep the acknowledged state if the subsequent source read fails.
      item.state = result.status;
      item.reminder = { ...item.reminder, status: result.status };
      announce(result.status === "closed" ? "Reminder completed." : "Reminder snoozed for one day.");
      busy.delete(id);
      cards.get(id)?.remove(); cards.delete(id);
      render();
      await refresh(true);
      window.ViraAssistant?.load(true);
      if (typeof refreshAlerts === "function") refreshAlerts();
    } catch (error) { announce(error.message); }
    finally { busy.delete(id); render(); }
  }
  async function source(item) {
    if (["closed", "snoozed"].includes(item.state)) {
      const details = cards.get(item.id)?.querySelector("details");
      if (details) details.open = true;
      return;
    }
    if (typeof openApp === "function") openApp("brief");
    await window.ViraAssistant?.reveal(item.id);
    const original = [...document.querySelectorAll("#assistant-body [data-assistant-id]")]
      .find((card) => card.dataset.assistantId === item.id);
    const evidence = original?.querySelector(".assistant-evidence");
    if (evidence) evidence.open = true;
  }
  function track(handle, card, item, resize) {
    handle.addEventListener("pointerdown", (event) => {
      if (!desktop() || event.button !== 0 || readOnly || moving) return;
      event.preventDefault(); event.stopPropagation();
      raise(card);
      const start = clamp(item);
      moving = { id: item.id, pointer: event.pointerId, x: event.clientX, y: event.clientY, start, geometry: start };
      handle.setPointerCapture(event.pointerId);
      card.classList.add("is-moving");
    });
    handle.addEventListener("pointermove", (event) => {
      if (!moving || moving.id !== item.id || moving.pointer !== event.pointerId) return;
      const dx = event.clientX - moving.x, dy = event.clientY - moving.y;
      moving.geometry = clamp(resize
        ? { ...moving.start, width: moving.start.width + dx, height: moving.start.height + dy }
        : { ...moving.start, x: moving.start.x + dx, y: moving.start.y + dy });
      position(card, moving.geometry);
    });
    const finish = (event) => {
      if (!moving || moving.id !== item.id || moving.pointer !== event.pointerId) return;
      const last = moving;
      moving = null;
      card.classList.remove("is-moving");
      if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      if (event.type === "pointercancel") { position(card, item); return; }
      savePosition(item.id, last.geometry);
    };
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
    handle.addEventListener("keydown", (event) => {
      if (!desktop() || readOnly || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault(); event.stopPropagation();
      const amount = event.shiftKey ? 40 : 12;
      const dx = event.key === "ArrowLeft" ? -amount : event.key === "ArrowRight" ? amount : 0;
      const dy = event.key === "ArrowUp" ? -amount : event.key === "ArrowDown" ? amount : 0;
      const start = clamp(item);
      const geometry = clamp(resize ? { ...start, width: start.width + dx, height: start.height + dy }
        : { ...start, x: start.x + dx, y: start.y + dy });
      position(card, geometry);
      savePosition(item.id, geometry);
    });
  }
  function build(item) {
    const reminder = item.reminder;
    const card = node("article", "reminder-sticky state-" + item.state);
    card.dataset.reminderId = item.id;
    card.setAttribute("aria-label", reminder?.what || "Pinned reminder");
    const head = node("div", "reminder-sticky-head");
    const grip = button("Pinned reminder", () => {}, "reminder-sticky-grip");
    grip.title = "Drag to move; use arrow keys to move, Shift for larger steps";
    head.append(grip, button("Unpin", () => unpin(item.id), "reminder-sticky-unpin"));
    card.appendChild(head);
    const body = node("div", "reminder-sticky-body");
    const state = item.state === "closed" ? "Completed"
      : item.state === "snoozed" ? "Snoozed" + (reminder?.snoozed_until ? " until " + date(reminder.snoozed_until) : " for one day")
      : item.state === "missing" ? "Original reminder is no longer available"
      : item.state === "unavailable" ? "Could not read the original reminder"
      : reminder?.stage === "overdue" ? "Overdue" : "Open";
    const status = node("p", "reminder-sticky-status", state);
    status.setAttribute("role", "status");
    body.append(status, node("h3", "reminder-sticky-title", reminder?.what || "Reminder unavailable"));
    if (reminder?.due) body.appendChild(node("p", "reminder-sticky-due", "Due " + date(reminder.due)));
    if (reminder) {
      const evidence = Array.isArray(reminder.evidence) ? reminder.evidence : [];
      const origin = [reminder.person_name, evidence[0]?.channel === "imessage" ? "text" : evidence[0]?.channel].filter(Boolean).join(" · ");
      const details = node("details", "reminder-sticky-evidence");
      details.appendChild(node("summary", "", origin || "Source evidence"));
      evidence.slice(0, 3).forEach((entry) => {
        if (entry.quote) details.appendChild(node("blockquote", "", entry.quote));
      });
      if (!evidence.length) details.appendChild(node("p", "", "This task is recorded in the contact or commitment list."));
      body.appendChild(details);
      if (["open", "snoozed"].includes(item.state))
        body.appendChild(button(item.state === "snoozed" ? "Show source evidence" : "Open source in Attention",
          () => source(item), "reminder-sticky-source"));
    }
    if (!healthy) body.appendChild(node("p", "reminder-sticky-error", "Could not refresh. Showing the last known state."));
    card.appendChild(body);
    const actions = node("div", "reminder-sticky-actions");
    if (["open", "snoozed"].includes(item.state)) {
      const done = button("Done", () => act(item.id, "done"), "reminder-sticky-task-action");
      const snooze = button("Snooze 1 day", () => act(item.id, "snooze"), "reminder-sticky-task-action");
      done.disabled = snooze.disabled = !healthy || readOnly || actionsReadOnly || busy.has(item.id);
      if (actionsReadOnly) done.title = snooze.title = "Task changes are disabled in a preview instance";
      actions.append(done, snooze);
    } else actions.appendChild(node("span", "", item.state === "closed" ? "Task complete. Unpin when ready." : "Unpin, or check the source later."));
    card.appendChild(actions);
    const resize = button("Resize", () => {}, "reminder-sticky-resize");
    resize.title = "Drag to resize; use arrow keys, Shift for larger steps";
    card.appendChild(resize);
    track(grip, card, item, false);
    track(resize, card, item, true);
    card.addEventListener("pointerdown", () => raise(card));
    card.addEventListener("dblclick", (event) => event.stopPropagation());
    card.addEventListener("contextmenu", (event) => event.stopPropagation());
    position(card, item);
    return card;
  }
  function render() {
    if (!list) return;
    const wanted = new Set(items.map((item) => item.id));
    for (const [id, card] of cards) if (!wanted.has(id)) { card.remove(); cards.delete(id); }
    for (const item of items) {
      if (moving?.id === item.id || busy.has(item.id) && cards.has(item.id)) continue;
      const signature = JSON.stringify([item, healthy, actionsReadOnly, busy.has(item.id)]);
      let card = cards.get(item.id);
      if (card?.dataset.signature !== signature) {
        const old = card;
        const evidenceOpen = old?.querySelector("details")?.open;
        const active = old?.contains(document.activeElement) ? document.activeElement.className : "";
        card = build(item);
        card.dataset.signature = signature;
        if (old) {
          card.style.zIndex = old.style.zIndex;
          old.replaceWith(card);
        } else list.appendChild(card);
        if (evidenceOpen && card.querySelector("details")) card.querySelector("details").open = true;
        if (active) [...card.querySelectorAll("button")].find((b) => b.className === active)?.focus({ preventScroll: true });
        cards.set(item.id, card);
      }
    }
    toggle.textContent = "Pinned reminders (" + items.length + ")";
    toggle.hidden = items.length === 0;
    decorate();
  }
  function decorate() {
    for (const card of document.querySelectorAll("#assistant-body .assistant-item[data-assistant-id]")) {
      const id = card.dataset.assistantId;
      if (!eligible.has(id)) continue;
      card.draggable = desktop() && healthy && !readOnly;
      card.classList.add("reminder-pinnable");
      let pinButton = card.querySelector(".reminder-pin-action");
      if (!pinButton) {
        pinButton = button("Pin to desktop", () => pin(id), "reminder-pin-action");
        pinButton.title = "Pin this reminder, or drag the card onto empty desktop space";
        (card.querySelector(".assistant-actions") || card).appendChild(pinButton);
      }
      const label = cards.has(id) ? "Show pinned reminder" : "Pin to desktop";
      if (pinButton.textContent !== label) pinButton.textContent = label;
      pinButton.disabled = !healthy || readOnly;
    }
  }
  async function refresh(force = false) {
    if (loading) {
      if (!force) return loading;
      await loading;
    }
    loading = request().then((data) => {
      readOnly = !!data.read_only;
      actionsReadOnly = !!data.actions_read_only;
      healthy = !data.error && !readOnly;
      eligible.clear();
      (data.eligible_ids || []).forEach((id) => eligible.add(id));
      // Geometry may be mid-gesture; keep the item used by its drag handlers.
      items = (data.items || []).map((item) => (moving?.id === item.id || busy.has(item.id))
        ? items.find((old) => old.id === item.id) || item : item);
      render();
    }).catch((error) => {
      healthy = false;
      render();
      if (items.length) live.textContent = error.message;
    }).finally(() => { loading = null; });
    return loading;
  }
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refresh(), 800);
  }
  function start() {
    if (layer) return;
    layer = node("aside", "reminder-stickies");
    layer.id = "reminder-stickies";
    layer.setAttribute("aria-label", "Pinned reminders");
    toggle = button("Pinned reminders", () => {
      const expanded = layer.classList.toggle("expanded");
      toggle.setAttribute("aria-expanded", String(expanded));
    }, "reminder-stickies-toggle");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", "reminder-stickies-list");
    toggle.hidden = true;
    list = node("div", "reminder-stickies-list"); list.id = "reminder-stickies-list";
    live = node("div", "reminder-stickies-announcement");
    live.setAttribute("role", "status"); live.setAttribute("aria-live", "polite");
    layer.append(toggle, list, live); document.body.appendChild(layer);
    document.addEventListener("dragstart", (event) => {
      const card = event.target.closest?.(".reminder-pinnable");
      const id = card?.dataset.assistantId;
      if (!id || !eligible.has(id) || !healthy || !desktop() || readOnly) return;
      if (event.target.closest("a, input, textarea, select, [contenteditable]")) { event.preventDefault(); return; }
      drag = { id, token: String(Date.now()) + ":" + Math.random() };
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData(MIME, drag.token);
      document.body.classList.add("reminder-dragging");
    });
    document.addEventListener("dragover", (event) => {
      if (!drag || !openSpace(event.target)) return;
      event.preventDefault(); event.dataTransfer.dropEffect = "copy";
    });
    document.addEventListener("drop", (event) => {
      if (!drag || !openSpace(event.target) || event.dataTransfer.getData(MIME) !== drag.token) return;
      event.preventDefault(); event.stopPropagation();
      pin(drag.id, { x: event.clientX - 30, y: event.clientY - 20, width: 288, height: 250 });
      drag = null; document.body.classList.remove("reminder-dragging");
    });
    document.addEventListener("dragend", () => { drag = null; document.body.classList.remove("reminder-dragging"); });
    const host = document.querySelector("#assistant-body");
    if (host) {
      observer = new MutationObserver((records) => {
        decorate();
        if (records.some((record) => [...record.addedNodes, ...record.removedNodes]
          .some((n) => n.nodeType === 1 && (n.matches?.("[data-assistant-id]") || n.querySelector?.("[data-assistant-id]"))))) scheduleRefresh();
      });
      observer.observe(host, { childList: true, subtree: true });
    }
    window.addEventListener("resize", () => {
      for (const item of items) if (cards.has(item.id)) position(cards.get(item.id), item);
    });
    window.addEventListener("focus", () => refresh());
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    setInterval(() => { if (!document.hidden && !moving) refresh(); }, 15000);
    refresh();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
  return { refresh, pin, focus };
})();

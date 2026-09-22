/* The executive assistant lives in Attention > Day. A refresh only reads;
   settings and actions are submitted by an explicit owner gesture. */
"use strict";

window.ViraAssistant = (() => {
  let latest = null;
  let pending = null;
  let generation = 0;
  let settingsOpen = null;
  let dirty = false;
  let painted = "";
  let feedback = null;
  const dateEdits = new Map();
  const emailViews = new Map();
  const expandedResources = new Set();
  const copyStates = new Map();
  const host = () => document.querySelector("#assistant-body");
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const plain = (value) => typeof value === "string" ? value : "";
  const date = (value, withZone = false) => {
    if (!value) return "Not yet";
    // A date-only deadline is a calendar day, not midnight in UTC.
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
    const d = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString([], {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      ...(withZone ? { timeZoneName: "short" } : {}),
    });
  };
  async function request(path, data) {
    try {
      return data === undefined ? await api(path) : await post(path, data);
    } catch (e) {
      let message = e.message || "Request failed";
      try { message = JSON.parse(message).detail || message; } catch (_) {}
      throw new Error(typeof message === "string" ? message : "Request failed");
    }
  }
  function button(label, run, disabled = false) {
    const b = node("button", "btn small", label);
    b.type = "button";
    b.disabled = disabled;
    b.addEventListener("click", run);
    return b;
  }
  function notice(parent, text, warning = false) {
    if (text) parent.appendChild(node("p", "assistant-note"
      + (warning ? " assistant-warning" : ""), text));
  }
  function resultMessage(result, action) {
    if (action === "done" && result.status === "closed") return "Commitment marked done.";
    if (action === "snooze" && result.status === "snoozed") return "Reminder snoozed for one day.";
    if (action === "date" && result.due) return "Deadline set to " + date(result.due) + ".";
    if (action === "dismiss" && result.status === "dismissed") return "Calendar suggestion dismissed.";
    if (result.status === "created") return "Event created in " + (result.event_calendar || "your calendar") + ".";
    return result.reason || "Action recorded. Review the current status below.";
  }
  async function act(b, path, data, box) {
    if (b.disabled) return;
    b.disabled = true;
    box.querySelector(".assistant-action-error")?.remove();
    try {
      const result = await request(path, data);
      if (data.action === "date") dateEdits.delete(decodeURIComponent(path.split("/").pop()));
      // Keep the acknowledged result even if the following status GET fails.
      if (latest && ["closed", "snoozed"].includes(result.status)) {
        const id = decodeURIComponent(path.split("/").pop());
        latest = { ...latest, reminders: (latest.reminders || []).filter((r) => r.id !== id) };
      } else if (latest && result.id && latest.calendar) {
        latest = { ...latest, calendar: { ...latest.calendar,
          drafts: (latest.calendar.drafts || []).map((d) => d.id === result.id ? result : d) } };
      }
      feedback = { text: resultMessage(result, data.action),
        warning: ["blocked", "uncertain", "creating"].includes(result.status) };
      await load(true);
      if (typeof refreshAlerts === "function") refreshAlerts();
    } catch (e) {
      const error = node("p", "assistant-action-error", e.message);
      error.setAttribute("role", "alert");
      box.appendChild(error);
      b.disabled = false;
    }
  }
  function field(form, key, label, type, value, options = {}) {
    const wrap = node("label", type === "checkbox"
      ? "assistant-check" : "field", type === "checkbox" ? "" : label);
    const input = node("input");
    input.name = key;
    input.type = type;
    if (type === "checkbox") input.checked = !!value;
    else input.value = value == null ? "" : String(value);
    Object.entries(options).forEach(([k, v]) => input.setAttribute(k, v));
    wrap.appendChild(input);
    if (type === "checkbox") wrap.appendChild(node("span", "", label));
    form.appendChild(wrap);
    return input;
  }
  // One picker per lane. "" is the personal calendar (the default and the
  // fallback); "kids" and "family" each name their own calendar and fall
  // back to personal when unset or missing, which the status line says.
  const LANE_PICKERS = {
    "": { label: "Calendar for my own events", idKey: "assistant_calendar_id", nameKey: "assistant_calendar_name" },
    kids: { label: "Calendar for the kids (school, classes, playdates, childcare - for my information)",
      idKey: "assistant_calendar_kids_id", nameKey: "assistant_calendar_kids_name" },
    family: { label: "Calendar for family plans I attend",
      idKey: "assistant_calendar_family_id", nameKey: "assistant_calendar_family_name" },
  };
  function calendarDestination(form, data, lane = "") {
    const saved = data.settings || {};
    const picker = LANE_PICKERS[lane];
    const box = node("div", "assistant-calendar-choice");
    const wrap = node("label", "field", picker.label);
    const select = node("select");
    select.name = picker.idKey;
    wrap.appendChild(select);
    box.appendChild(wrap);
    const status = node("div", "assistant-calendar-status");
    box.appendChild(status);
    let choices = new Map();
    let initial = { id: saved[picker.idKey] || "",
      name: saved[picker.idKey] ? "" : saved[picker.nameKey] || "" };
    const value = () => choices.get(select.value) || initial;
    function populate(destinations) {
      const keep = value();
      const d = destinations || {};
      const calendars = (d.calendars || []).filter((c) => c.id && c.name);
      choices = new Map();
      select.replaceChildren();
      const option = (key, label, choice, disabled = false) => {
        const row = node("option", "", label);
        row.value = key;
        row.disabled = disabled;
        select.appendChild(row);
        choices.set(key, choice);
      };
      const systemDefault = calendars.find((c) => c.id === d.default_id && c.writable);
      const sole = calendars.filter((c) => c.writable);
      const auto = lane ? "personal calendar (fallback)"
        : systemDefault ? systemDefault.name + " (system default)"
        : d.available && sole.length === 1 ? sole[0].name + " (only writable calendar)" : "system default";
      option("", (lane ? "Not set - " : "Auto - ") + auto, { id: "", name: "" });
      if (keep.name) option("saved-name", "Keep " + keep.name + " (saved destination)", keep);
      calendars.forEach((c) => option("id:" + c.id,
        c.name + (c.is_default ? " (system default)" : "") + (c.writable ? "" : " (read only)"),
        { id: c.id, name: "" }, !c.writable));
      if (keep.id && !calendars.some((c) => c.id === keep.id))
        option("id:" + keep.id, "Saved calendar (unavailable)", keep);
      select.value = keep.id ? "id:" + keep.id : keep.name ? "saved-name" : "";
      initial = keep;
      status.replaceChildren();
      if (lane) {
        const row = (d.lanes || {})[lane] || {};
        if (row.selected?.name) notice(status, "Current " + lane + " calendar: " + row.selected.name + ".");
        else if (keep.name || keep.id) notice(status, row.reason || ("The saved " + lane + " calendar is unavailable; " + lane + " events use the personal calendar."), true);
        else notice(status, lane + " events use the personal calendar until a calendar is chosen here.");
        return;
      }
      if (d.selected?.name) notice(status, "Current destination: " + d.selected.name + ".");
      if (keep.name && !d.selected) notice(status, "Saved destination: " + keep.name + ".");
      const problem = d.error || d.reason;
      if (problem) notice(status, problem, true);
      else if (!d.available) notice(status, "Calendar destinations are unavailable. Refresh to check access.", true);
      else if (!calendars.some((c) => c.writable)) notice(status, "No writable calendars are available.", true);
      else if (keep.id && !calendars.some((c) => c.id === keep.id && c.writable))
        notice(status, "The saved calendar is unavailable for new events. Choose another destination.", true);
      else if (!keep.id && !keep.name && !systemDefault && sole.length !== 1)
        notice(status, "No system default was found. Choose a writable calendar above.", true);
      notice(status, "Auto uses the system default, or the only writable calendar when no default is available.");
    }
    populate(data.calendar?.destinations);
    select.addEventListener("change", () => { dirty = true; });
    const refresh = button("Refresh calendars", async () => {
      if (refresh.disabled) return;
      refresh.disabled = true;
      box.querySelector(".assistant-action-error")?.remove();
      try {
        const destinations = await request("/api/assistant/calendars?refresh=true");
        if (latest) latest = { ...latest, calendar: { ...latest.calendar, destinations } };
        populate(destinations);
      } catch (e) {
        const error = node("p", "assistant-action-error", "Calendar destinations unavailable: " + e.message);
        error.setAttribute("role", "alert");
        box.appendChild(error);
      } finally { refresh.disabled = false; }
    });
    box.appendChild(refresh);
    form.appendChild(box);
    return (updates) => {
      const chosen = value();
      updates[picker.idKey] = chosen.id;
      updates[picker.nameKey] = chosen.name;
    };
  }
  function settingsPanel(data) {
    const details = node("details", "assistant-settings");
    details.open = settingsOpen == null ? false : settingsOpen;
    details.addEventListener("toggle", () => { settingsOpen = details.open; });
    details.appendChild(node("summary", "", "Assistant settings"));
    const form = node("form", "assistant-form");
    const s = data.settings || {};
    const values = {};
    const add = (key, label, type, options) => {
      values[key] = field(form, key, label, type, s[key], options);
    };
    add("assistant_enabled", "Run the assistant in the background", "checkbox");
    add("assistant_contact_updates", "Update contact profiles from new messages", "checkbox");
    add("mail_body_index", "Include full email bodies and sent replies", "checkbox");
    add("assistant_notify", "Text me when a commitment needs attention", "checkbox");
    notice(form, "Texts use the owner notification channel in Config. "
      + "Quiet hours and a daily limit keep reminders bounded.");
    const channelActions = node("div", "assistant-actions");
    channelActions.appendChild(button("Open notification settings", () => {
      openApp("setup");
      if (typeof dashJump === "function") dashJump("notifications");
    }));
    form.appendChild(channelActions);
    add("assistant_quiet_start", "Quiet hours start (0–23)", "number", { min: 0, max: 23, required: "" });
    add("assistant_quiet_end", "Quiet hours end (0–23)", "number", { min: 0, max: 23, required: "" });
    add("assistant_timezone", "Assistant time zone", "text", { placeholder: "Server local time, or an IANA time zone" });
    add("assistant_notify_daily_cap", "Maximum reminder texts per day", "number", { min: 1, max: 20, required: "" });
    add("assistant_stale_days", "Flag an undated commitment after this many days", "number", { min: 1, max: 90, required: "" });
    add("assistant_due_soon_hours", "Remind me this many hours before a deadline", "number", { min: 1, max: 168, required: "" });
    add("assistant_catchup_days", "Check this many recent days when first enabled", "number", { min: 1, max: 90, required: "" });
    const saveDestination = calendarDestination(form, data);
    const saveKidsDestination = calendarDestination(form, data, "kids");
    const saveFamilyDestination = calendarDestination(form, data, "family");
    add("assistant_kids_names", "Children's first names (comma-separated)", "text",
      { placeholder: "An event naming one of them files on the kids calendar" });
    notice(form, "Kid and family events Vira reads out of messages and mail land on those calendars "
      + "as entries for you only; nobody is invited. Either lane falls back to the personal calendar.");
    add("assistant_calendar_work_start", "Weekday work hours start (0–23)", "number", { min: 0, max: 23, required: "" });
    add("assistant_calendar_work_end", "Weekday work hours end (1–24)", "number", { min: 1, max: 24, required: "" });
    add("assistant_calendar_block_minutes", "Personal task block length (minutes)", "number", { min: 15, max: 240, required: "" });
    notice(form, "Personal task blocks use these working hours and the assistant time zone above.");
    add("assistant_calendar_auto_create", "Create events automatically when I am the only attendee", "checkbox");
    notice(form, "Vira can reserve free personal work time before a task's deadline, "
      + "create an event from your explicit solo scheduling request, or file a kids or "
      + "family entry whose date and times are quoted in the source. "
      + "Meetings with others on your own calendar stay as suggestions for you to invite. "
      + "Equal quiet-hour values turn quiet hours off.");
    form.addEventListener("input", () => { dirty = true; });
    const actions = node("div", "assistant-actions");
    const save = node("button", "btn primary", "Save assistant settings");
    save.type = "submit";
    save.disabled = !!data.fixture;
    actions.appendChild(save);
    actions.appendChild(button("Cancel edits", () => {
      dirty = false;
      painted = "";
      render(latest);
    }));
    form.appendChild(actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (save.disabled) return;
      const updates = {};
      Object.entries(values).forEach(([key, input]) => {
        updates[key] = input.type === "checkbox" ? input.checked
          : input.type === "number" ? Number(input.value) : input.value.trim();
      });
      saveDestination(updates);
      saveKidsDestination(updates);
      saveFamilyDestination(updates);
      save.disabled = true;
      form.querySelector(".assistant-action-error")?.remove();
      try {
        const saved = await request("/api/assistant/config", updates);
        latest = { ...latest, settings: saved, enabled: saved.assistant_enabled };
        dirty = false;
        feedback = { text: "Assistant settings saved." };
        await load(true);
      } catch (e) {
        const error = node("p", "assistant-action-error", e.message);
        error.setAttribute("role", "alert");
        form.appendChild(error);
        save.disabled = false;
      }
    });
    details.appendChild(form);
    return details;
  }
  function evidence(parent, value) {
    const rows = Array.isArray(value) ? value : value ? [value] : [];
    const text = rows.map((e) => typeof e === "string" ? e
      : [e.quote || e.text || e.what, e.channel, e.when]
        .filter(Boolean).join(" · ")).filter(Boolean).join("\n\n");
    if (!text) return;
    const more = node("details", "assistant-evidence");
    more.appendChild(node("summary", "", "Why this is here"));
    more.appendChild(node("p", "", text));
    parent.appendChild(more);
  }
  function externalResource(resource) {
    if (typeof resource?.url !== "string" || /[\u0000-\u0020\u007f]/.test(resource.url)) return null;
    try {
      const url = new URL(resource.url);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      return { ...resource, url: url.href, domain: url.hostname };
    } catch (_) { return null; }
  }
  function emailPath(resource) {
    if (resource?.kind !== "email" || resource.url || !plain(resource.account).trim()) return "";
    const params = new URLSearchParams();
    [["account", resource.account], ["rowid", resource.rowid],
      ["mid", resource.message_id], ["graph_id", resource.graph_id]].forEach(([key, value]) => {
      if (value != null && String(value).trim()) params.set(key, String(value));
    });
    return params.has("rowid") || params.has("mid") || params.has("graph_id")
      ? "/api/mail/message?" + params.toString() : "";
  }
  function resourceLink(resource) {
    const row = node("div", "assistant-resource");
    const label = resource.kind === "payment" ? "Open payment page"
      : plain(resource.label) || (resource.kind === "mailbox" ? "Open mailbox" : "Open link");
    const link = node("a", "btn small", label);
    link.href = resource.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.referrerPolicy = "no-referrer";
    row.appendChild(link);
    row.appendChild(node("span", "assistant-resource-context", resource.domain));
    return row;
  }
  function resourceList(parent, resources, scope, renderRow) {
    const more = resources.length > 3 ? node("details", "assistant-resource-more") : null;
    if (more) {
      more.open = expandedResources.has(scope);
      more.appendChild(node("summary", "", "Show " + (resources.length - 3) + " more actions"));
      more.addEventListener("toggle", () => {
        if (more.open) expandedResources.add(scope); else expandedResources.delete(scope);
      });
    }
    resources.forEach((resource, index) => (index >= 3 && more ? more : parent).appendChild(renderRow(resource)));
    if (more) parent.appendChild(more);
  }
  function emailContent(message, scope) {
    const content = node("div", "assistant-email-content");
    content.appendChild(node("h6", "assistant-email-subject", plain(message.subject) || "Email"));
    const sender = [plain(message.from_name), plain(message.from_addr)].filter(Boolean).join(" · ");
    if (sender) notice(content, "From: " + sender);
    if (message.account) notice(content, "Account: " + plain(message.account));
    if (message.when) notice(content, date(message.when, true));
    const seen = new Set();
    const links = [...(Array.isArray(message.links) ? message.links : []),
      ...(Array.isArray(message.mail_actions) ? message.mail_actions : [])]
      .map(externalResource).filter((resource) => {
        if (!resource || seen.has(resource.url)) return false;
        seen.add(resource.url);
        return true;
      });
    if (links.length) {
      const actions = node("div", "assistant-email-links");
      actions.appendChild(node("h6", "assistant-resource-title", "Links in this email"));
      resourceList(actions, links, scope + ":links", resourceLink);
      content.appendChild(actions);
    }
    const text = node("div", "assistant-email-text", plain(message.text) || "No readable text was returned for this email.");
    text.tabIndex = 0;
    text.setAttribute("aria-label", "Email text");
    content.appendChild(text);
    return content;
  }
  function emailResource(resource, reminderId, preview) {
    const path = emailPath(resource);
    const key = JSON.stringify([reminderId, resource.account, resource.source_id || path]);
    if (!emailViews.has(key)) emailViews.set(key, { reminderId, open: false, loading: false, error: "", message: null });
    const state = emailViews.get(key);
    const item = node("div", "assistant-email-resource");
    const row = node("div", "assistant-resource");
    const view = node("div", "assistant-email-preview");
    async function fetchEmail() {
      if (preview || state.loading) return;
      state.loading = true;
      state.error = "";
      state.render();
      try {
        const message = await request(path);
        if (!message || typeof message !== "object") throw new Error("The email response was unreadable.");
        state.message = message;
        state.content = null;
      } catch (e) {
        state.error = e.message;
      } finally {
        state.loading = false;
        state.render();
      }
    }
    const open = button("Open email", async () => {
      if (open.disabled) return;
      state.open = !state.open;
      state.render();
      if (state.open && !state.message && !state.error) await fetchEmail();
    }, preview);
    row.appendChild(open);
    row.appendChild(node("span", "assistant-resource-context", resource.account));
    item.appendChild(row);
    if (resource.subject) notice(item, plain(resource.subject));
    if (preview) notice(item, "Open the connected instance to read this email.");
    item.appendChild(view);
    state.render = () => {
      open.textContent = state.open ? "Hide email" : plain(resource.label) || "Open email";
      open.setAttribute("aria-expanded", String(state.open));
      view.replaceChildren();
      if (!state.open) return;
      if (state.loading) {
        const loading = node("p", "assistant-note", "Opening email...");
        loading.setAttribute("role", "status");
        view.appendChild(loading);
      } else if (state.error) {
        const error = node("p", "assistant-action-error", "Email could not be opened: " + state.error);
        error.setAttribute("role", "alert");
        view.appendChild(error);
        view.appendChild(button("Try opening email again", fetchEmail, preview));
      } else if (state.message) {
        if (!state.content) state.content = emailContent(state.message, key);
        view.appendChild(state.content);
      }
    };
    state.render();
    return item;
  }
  function copyDetails(section, reminder) {
    if (!copyStates.has(reminder.id)) copyStates.set(reminder.id, { loading: false, text: "", error: false });
    const state = copyStates.get(reminder.id);
    const row = node("div", "assistant-resource-copy");
    const status = node("span", "assistant-resource-context");
    status.setAttribute("role", "status");
    const copy = button("Copy details", async () => {
      if (state.loading) return;
      state.loading = true;
      state.text = "Copying...";
      state.error = false;
      state.render();
      try {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard is unavailable in this browser.");
        const lines = [plain(reminder.what)];
        if (reminder.due) lines.push("Due: " + reminder.due);
        const evidence = (Array.isArray(reminder.evidence) ? reminder.evidence : [])
          .map((e) => typeof e === "string" ? e : plain(e.quote)).filter(Boolean);
        if (evidence.length) lines.push("", "Source:", ...evidence);
        const resources = [...(Array.isArray(reminder.resources) ? reminder.resources : [])];
        for (const email of emailViews.values()) {
          if (email.reminderId !== reminder.id || !email.message) continue;
          for (const key of ["links", "mail_actions"])
            if (Array.isArray(email.message[key])) resources.push(...email.message[key]);
        }
        const links = [...new Set(resources.map(externalResource).filter(Boolean).map((r) => r.url))];
        if (links.length) lines.push("", "Links:", ...links);
        await navigator.clipboard.writeText(lines.join("\n"));
        state.text = "Details copied.";
      } catch (e) {
        state.text = "Could not copy details: " + e.message;
        state.error = true;
      } finally { state.loading = false; state.render(); }
    });
    state.render = () => {
      copy.disabled = state.loading;
      status.textContent = state.text;
      status.className = "assistant-resource-context" + (state.error ? " assistant-warning" : "");
    };
    state.render();
    row.appendChild(copy);
    row.appendChild(status);
    section.appendChild(row);
  }
  function actionResources(card, reminder, data) {
    const resources = (Array.isArray(reminder.resources) ? reminder.resources : [])
      .map((resource) => externalResource(resource) || (emailPath(resource) ? resource : null)).filter(Boolean);
    const section = node(resources.length ? "div" : "details", "assistant-resources");
    section.appendChild(node(resources.length ? "h6" : "summary", "assistant-resource-title",
      resources.length ? "Take action" : "More details"));
    resourceList(section, resources, reminder.id, (resource) => resource.url
      ? resourceLink(resource) : emailResource(resource, reminder.id, !!data.fixture));
    notice(section, plain(reminder.resources_note));
    copyDetails(section, reminder);
    card.appendChild(section);
  }
  function deadlineReview(card, row, data) {
    const review = row.deadline_review;
    if (!review) return;
    const box = node("div", "assistant-deadline");
    notice(box, review.text ? "Original timing: " + review.text : "The deadline needs your review.");
    if (review.proposed) notice(box, "Proposed date: " + date(review.proposed));
    notice(box, review.reason, true);
    const form = node("form", "assistant-date-form");
    // Keep an unfinished correction during the background status poll.
    const input = field(form, "due", "Due date", "date", dateEdits.get(row.id) || "", { required: "" });
    input.addEventListener("input", () => dateEdits.set(row.id, input.value));
    const save = node("button", "btn small", "Set due date");
    save.type = "submit";
    save.disabled = !!data.fixture;
    form.appendChild(save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (save.disabled) return;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(input.value)) {
        box.querySelector(".assistant-action-error")?.remove();
        const error = node("p", "assistant-action-error", "Choose a due date first.");
        error.setAttribute("role", "alert");
        box.appendChild(error);
        return;
      }
      await act(save, "/api/assistant/reminders/" + encodeURIComponent(row.id),
        { action: "date", due: input.value }, box);
    });
    box.appendChild(form);
    card.appendChild(box);
  }
  function reminders(data, body) {
    const rows = (data.reminders || []).filter((r) => r.status !== "done");
    const section = node("div", "assistant-section");
    section.appendChild(node("h4", "", "Commitments to catch"));
    if (!rows.length) notice(section, data.enabled
      ? "No open commitments in the current profiles. Coverage below shows what was checked."
      : "Enable the assistant to start watching new messages and open commitments.");
    const extra = rows.length > 8 ? node("details", "assistant-more") : null;
    if (extra) {
      notice(section, rows.length + " open commitments, with the most urgent first.");
      extra.appendChild(node("summary", "", "Show " + (rows.length - 8) + " more commitments"));
    }
    rows.forEach((r, index) => {
      const card = node("article", "assistant-item");
      card.dataset.assistantId = r.id;
      const meta = node("div", "assistant-item-meta");
      const label = { overdue: "Overdue", due: "Due soon", stale: "Needs follow-up", open: "Open", review: "Check deadline" }[r.stage];
      meta.appendChild(node("span", "assistant-tag", label || "Follow up"));
      if (r.due) meta.appendChild(node("span", "", "Due " + date(r.due)));
      if (r.owed_by) meta.appendChild(node("span", "", r.owed_by === "me" ? "On you" : "Waiting on them"));
      if (r.status === "snoozed") meta.appendChild(node("span", "", "Snoozed until " + date(r.snoozed_until)));
      card.appendChild(meta);
      card.appendChild(node("h5", "", r.what || "Review this commitment"));
      if (!r.person_id && r.person_name) notice(card, r.person_name);
      notice(card, r.reason);
      deadlineReview(card, r, data);
      actionResources(card, r, data);
      evidence(card, r.evidence);
      const actions = node("div", "assistant-actions");
      if (r.person_id) actions.appendChild(button(r.person_name || "Open contact", () => openPerson(r.person_id)));
      [ ["Done", { action: "done" }], ["Snooze 1 day", { action: "snooze", hours: 24 }] ]
        .forEach(([label, payload]) => {
          const b = button(label, () => act(b, "/api/assistant/reminders/"
            + encodeURIComponent(r.id), payload, card), !!data.fixture);
          actions.appendChild(b);
        });
      card.appendChild(actions);
      (index >= 8 && extra ? extra : section).appendChild(card);
    });
    if (extra) section.appendChild(extra);
    body.appendChild(section);
  }
  function calendar(data, body) {
    const cal = data.calendar || {};
    const rows = (cal.drafts || []).filter((d) => d.status !== "dismissed");
    const section = node("div", "assistant-section");
    section.appendChild(node("h4", "", "Calendar suggestions"));
    notice(section, cal.last_error || cal.error, true);
    if (!rows.length) notice(section, "Suggested events appear here when a message contains a scheduling request.");
    rows.forEach((d) => {
      const card = node("article", "assistant-item");
      card.dataset.assistantId = d.id;
      const meta = node("div", "assistant-item-meta");
      const label = { suggested: "Suggested", blocked: "Needs review", creating: "Creating",
        uncertain: "Check calendar", created: "Created" }[d.status];
      meta.appendChild(node("span", "assistant-tag", label || "Suggested"));
      meta.appendChild(node("span", "", d.lane === "kids" ? "Kids calendar" : d.lane === "family" ? "Family calendar"
        : d.owner_only ? "Only you" : "Meeting suggestion"));
      card.appendChild(meta);
      card.appendChild(node("h5", "", d.title || "Calendar event"));
      if (d.start) notice(card, date(d.start, true) + (d.end ? " – " + date(d.end, true) : ""));
      if (d.time_chosen_by === "assistant") {
        notice(card, d.start ? "Time chosen by Vira to work on this task."
          : "Vira has not found a time for this task yet.");
        if (d.due) notice(card, "Task due " + date(d.due));
        if (d.deadline_authority === "owner") notice(card, "Deadline corrected by you.");
        if (d.person_name) notice(card, "For " + d.person_name);
      }
      if ((d.attendees || []).length) notice(card, "With " + d.attendees.join(", "));
      if (d.location) notice(card, "Where: " + d.location);
      if (d.description) notice(card, d.description);
      notice(card, d.reason, ["blocked", "uncertain"].includes(d.status));
      if (d.status === "uncertain") notice(card, "Check your calendar before trying again; creation could not be confirmed.", true);
      if (d.status === "created") notice(card, "Created in " + (d.event_calendar || "your calendar")
        + (d.event_lane && d.event_lane !== "personal" ? " (" + d.event_lane + " calendar)" : ""));
      evidence(card, d.schedule_kind === "commitment" ? d.evidence || d.quote : d.quote || d.evidence);
      const actions = node("div", "assistant-actions");
      const path = "/api/assistant/calendar/" + encodeURIComponent(d.id);
      if (d.can_create) {
        const create = button("Create my event", () => act(create, path,
          { action: "create" }, card), !!data.fixture);
        actions.appendChild(create);
      }
      if (d.can_export) {
        const link = node("a", "btn small", "Download calendar file");
        link.href = path + ".ics";
        link.download = "event.ics";
        actions.appendChild(link);
      }
      if (!["created", "creating"].includes(d.status)) {
        const dismiss = button("Dismiss", () => act(dismiss, path,
          { action: "dismiss" }, card), !!data.fixture);
        actions.appendChild(dismiss);
      }
      if (actions.childElementCount) card.appendChild(actions);
      if (d.can_export && !d.owner_only && !["kids", "family"].includes(d.lane)) notice(card, "The calendar file is a draft. Add invitees in your calendar when you are ready to invite them.");
      section.appendChild(card);
    });
    body.appendChild(section);
  }
  function coverage(data, body) {
    const c = data.contact || {};
    const details = node("details", "assistant-coverage");
    details.appendChild(node("summary", "", "Coverage and recent activity"));
    const facts = node("dl", "assistant-facts");
    const fact = (label, value) => {
      facts.appendChild(node("dt", "", label));
      facts.appendChild(node("dd", "", value));
    };
    fact("Last assistant scan", date(data.last_run));
    fact("Last successful assistant scan", date(data.last_success));
    fact("Last contact processing pass", date(c.last_success));
    if (c.pending_contacts != null) fact("Contacts waiting", String(c.pending_contacts));
    if (c.pending_messages != null) fact("Messages waiting", String(c.pending_messages));
    if (c.index_available != null) fact("Message index", c.index_available ? "Available" : "Unavailable");
    if (c.mail_body_index != null) fact("Email body indexing", c.mail_body_index ? "Enabled" : "Off");
    const counters = c.counters || {};
    if (counters.processed != null) fact("Messages processed", String(counters.processed));
    [["facts", "Contact facts added"], ["loops", "Commitments found"],
      ["closed", "Commitments resolved"], ["summary", "Profile summaries updated"]]
      .forEach(([key, label]) => {
        if (counters[key] != null) fact(label, String(counters[key]));
      });
    details.appendChild(facts);
    const cov = data.coverage;
    if (typeof cov === "string") notice(details, cov);
    else if (Array.isArray(cov)) cov.forEach((x) => notice(details, plain(x)));
    else if (cov && typeof cov === "object") Object.entries(cov).forEach(([key, value]) => {
      if (["string", "number", "boolean"].includes(typeof value))
        notice(details, key.replace(/_/g, " ") + ": " + String(value));
      else if (Array.isArray(value)) value.forEach((x) => notice(details, plain(x)));
    });
    (c.errors || []).forEach((entry) => {
      const row = node("div", "assistant-item");
      notice(row, "A contact update needs another attempt (" + entry.error + "). "
        + (entry.retry_at ? "Next attempt after " + date(entry.retry_at) + "." : "Waiting to retry."), true);
      if (entry.person_id) row.appendChild(button("Open affected contact", () => openPerson(entry.person_id)));
      details.appendChild(row);
    });
    body.appendChild(details);
  }
  function render(data) {
    const body = host();
    if (!body || !data) return;
    const ids = new Set((data.reminders || []).map((row) => row.id));
    for (const [key, state] of emailViews) if (!ids.has(state.reminderId)) emailViews.delete(key);
    for (const key of copyStates.keys()) if (!ids.has(key)) copyStates.delete(key);
    const signature = JSON.stringify([data, feedback]);
    if (signature === painted && !body.querySelector(".assistant-load-error")) return;
    painted = signature;
    // A poll can update commitments while a settings edit stays intact.
    const editing = dirty ? body.querySelector(".assistant-settings") : null;
    const coverageOpen = body.querySelector(".assistant-coverage")?.open;
    const moreOpen = body.querySelector(".assistant-more")?.open;
    const evidenceOpen = new Set([...body.querySelectorAll("[data-assistant-id]")]
      .filter((card) => card.querySelector(".assistant-evidence")?.open)
      .map((card) => card.dataset.assistantId));
    body.replaceChildren();
    const head = node("div", "assistant-head");
    head.appendChild(node("h3", "", "Reminders & follow-through"));
    const state = data.fixture ? "Sample data"
      : !data.enabled ? "Paused" : data.last_error || data.contact?.last_error || data.contact?.errors?.length
        || data.calendar?.error || data.calendar?.last_error
      ? "Needs attention" : !data.worker_running ? "Worker not running"
      : data.active === false ? "Inactive" : data.last_success ? "Running" : "Waiting for first successful scan";
    head.appendChild(node("span", "assistant-tag", state));
    head.appendChild(button("Refresh", () => load(true)));
    body.appendChild(head);
    if (feedback) {
      const message = node("p", "assistant-feedback" + (feedback.warning ? " assistant-warning" : ""), feedback.text);
      message.setAttribute("role", "status");
      body.appendChild(message);
    }
    if (data.fixture) notice(body,
      "Sample data does not process messages, send texts, or create calendar events.");
    notice(body, data.last_error, true);
    notice(body, data.contact?.last_error, true);
    if (data.contact?.errors?.length) notice(body,
      data.contact.errors.length + " contact update(s) are waiting to retry. Details are in Coverage and recent activity.", true);
    if (data.enabled && !data.worker_running && !data.fixture)
      notice(body, "The assistant worker is not running. Review server health before relying on reminders.", true);
    if (data.settings?.assistant_notify && !data.notification_ready)
      notice(body, "Reminder texts are enabled, but the owner notification channel is not ready. Configure it in Config.", true);
    if (data.enabled && data.settings?.assistant_contact_updates === false)
      notice(body, "Contact facts and summaries are paused. Vira still checks messages for commitments.");
    reminders(data, body);
    calendar(data, body);
    body.appendChild(editing || settingsPanel(data));
    coverage(data, body);
    body.querySelector(".assistant-coverage").open = !!coverageOpen;
    if (body.querySelector(".assistant-more")) body.querySelector(".assistant-more").open = !!moreOpen;
    body.querySelectorAll("[data-assistant-id]").forEach((card) => {
      if (evidenceOpen.has(card.dataset.assistantId) && card.querySelector(".assistant-evidence"))
        card.querySelector(".assistant-evidence").open = true;
    });
  }
  async function load(force = false) {
    if (!host()) return;
    if (pending && !force) return pending;
    const token = ++generation;
    pending = request("/api/assistant").then((data) => {
      if (token !== generation) return data;
      latest = data;
      render(data);
      return data;
    }).catch((e) => {
      if (token !== generation || !host()) return;
      render(latest);
      host().querySelector(".assistant-load-error")?.remove();
      const error = node("p", "assistant-load-error assistant-warning",
        "Assistant status unavailable: " + e.message);
      error.setAttribute("role", "status");
      host().prepend(error);
    }).finally(() => { if (token === generation) pending = null; });
    return pending;
  }
  async function reveal(id) {
    await load();
    const body = host();
    if (!body) return;
    const target = [...body.querySelectorAll("[data-assistant-id]")]
      .find((n) => n.dataset.assistantId === id) || body;
    if (target.parentElement?.classList.contains("assistant-more")) target.parentElement.open = true;
    target.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
    target.classList.add("assistant-revealed");
    setTimeout(() => target.classList.remove("assistant-revealed"), 5000);
  }
  setInterval(() => {
    const pane = document.querySelector("#attention-day-pane");
    if (!document.hidden && pane && pane.style.display !== "none"
        && host()?.getClientRects().length) load();
  }, 30000);
  return { load, reveal };
})();

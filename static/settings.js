/* One renderer for every settings form in Config.

   The table of keys lives on the server (server/settingschema.py) and
   arrives through GET /api/settings with each key's group, label, help,
   type, default and current value. A card asks for a group or a list of
   keys and gets a form; Save posts only the keys that changed to
   POST /api/settings, the one validated writer. Nothing here knows what
   a key means: add a row to the table and it appears. */
"use strict";

window.ViraSettings = (() => {
  let cache = null;
  let pending = null;
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  async function request(path, body) {
    const res = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let message = res.statusText;
      try { message = (await res.json()).detail || message; } catch (_) {}
      throw new Error(typeof message === "string" ? message : "Request failed");
    }
    return res.json();
  }
  async function load(force = false) {
    if (cache && !force) return cache;
    if (pending && !force) return pending;
    pending = request("/api/settings").then((data) => {
      cache = data;
      pending = null;
      return data;
    }, (e) => { pending = null; throw e; });
    return pending;
  }
  function rows(data) {
    const out = [];
    (data.groups || []).forEach((g) => (g.settings || []).forEach((s) => out.push(s)));
    return out;
  }
  function pick(data, opts) {
    const all = rows(data);
    if (opts.keys) {
      const want = new Set(opts.keys);
      return all.filter((s) => want.has(s.key));
    }
    return all.filter((s) => (!opts.group || s.group === opts.group)
      && !s.control
      && (opts.advanced == null ? !s.advanced : !!s.advanced === !!opts.advanced));
  }
  function fmtDefault(s) {
    const d = s.default;
    if (d === null || d === undefined || d === "") return "empty";
    if (Array.isArray(d)) return d.length ? d.join(", ") : "empty";
    if (typeof d === "object") return Object.keys(d).length ? JSON.stringify(d) : "empty";
    return String(d);
  }
  // One control per type. The input carries data-key; read() returns the
  // typed value and throws a readable error for text that is not valid.
  function control(s) {
    const wrap = node("label", s.type === "bool" ? "assistant-check" : "field",
      s.type === "bool" ? "" : s.label);
    let input;
    if (s.type === "bool") {
      input = node("input"); input.type = "checkbox"; input.checked = !!s.value;
      wrap.appendChild(input);
      const words = node("span");
      words.appendChild(node("span", "", s.label));
      if (s.help) words.appendChild(node("span", "settings-help", s.help));
      wrap.appendChild(words);
    } else if (s.type === "enum") {
      input = node("select");
      (s.choices || []).forEach((c) => { const o = node("option", "", c); o.value = c; input.appendChild(o); });
      input.value = s.value == null ? "" : String(s.value);
      wrap.appendChild(input);
    } else if (s.type === "json") {
      input = node("textarea");
      input.value = JSON.stringify(s.value == null ? s.default : s.value, null, 1);
      input.spellcheck = false;
      wrap.appendChild(input);
    } else {
      input = node("input");
      input.type = s.type === "int" || s.type === "float" ? "number" : "text";
      if (s.type === "int") input.step = "1";
      if (s.type === "float") input.step = "any";
      if (s.range) { input.min = s.range[0]; input.max = s.range[1]; }
      input.value = s.type === "list" ? (s.value || []).join(", ")
        : s.value == null ? "" : String(s.value);
      input.spellcheck = false;
      input.autocomplete = "off";
      if (s.type === "tz") input.placeholder = "This computer's local time";
      wrap.appendChild(input);
    }
    if (s.type !== "bool") {
      const help = node("span", "settings-help");
      if (s.help) help.appendChild(document.createTextNode(s.help + " "));
      help.appendChild(node("span", "settings-default", "Default: " + fmtDefault(s) + "."));
      wrap.appendChild(help);
    }
    input.dataset.key = s.key;
    const read = () => {
      if (s.type === "bool") return input.checked;
      const raw = input.value.trim();
      if (s.type === "int") {
        if (raw === "") throw new Error(s.label + " needs a whole number");
        if (!/^-?\d+$/.test(raw)) throw new Error(s.label + " must be a whole number");
        return Number(raw);
      }
      if (s.type === "float") {
        if (raw === "") throw new Error(s.label + " needs a number");
        const n = Number(raw);
        if (!Number.isFinite(n)) throw new Error(s.label + " must be a number");
        return n;
      }
      if (s.type === "list") return raw ? raw.split(",").map((x) => x.trim()).filter(Boolean) : [];
      if (s.type === "json") {
        if (raw === "") return s.default == null ? {} : s.default;
        try { return JSON.parse(raw); } catch (_) { throw new Error(s.label + " must be valid JSON"); }
      }
      return raw;
    };
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    return { wrap, input, read, changed: () => !same(read(), s.value) };
  }
  /* form(container, {group | keys, advanced, title, intro, onSaved, grouped})
     grouped=true adds a subheading per group (the Advanced card). */
  function form(container, opts = {}) {
    const host = node("div", "settings-host");
    container.appendChild(host);
    host.appendChild(node("p", "hint waiting", "Loading settings..."));
    load(opts.fresh).then((data) => {
      host.replaceChildren();
      const settings = pick(data, opts);
      if (!settings.length) { host.appendChild(node("p", "hint", "Nothing to set here.")); return; }
      const f = node("form", "settings-form");
      if (opts.intro) f.appendChild(node("p", "settings-note", opts.intro));
      const controls = [];
      let lastGroup = null;
      const titles = {};
      (data.groups || []).forEach((g) => { titles[g.id] = g.title; });
      settings.forEach((s) => {
        if (opts.grouped && s.group !== lastGroup) {
          f.appendChild(node("div", "settings-sub", titles[s.group] || s.group));
          lastGroup = s.group;
        }
        const c = control(s);
        controls.push(c);
        f.appendChild(c.wrap);
      });
      const error = node("p", "settings-error"); error.setAttribute("role", "alert");
      const actions = node("div", "settings-actions");
      const save = node("button", "btn primary", opts.saveLabel || "Save");
      save.type = "submit";
      const saved = node("span", "settings-saved", "");
      actions.appendChild(save); actions.appendChild(saved);
      f.appendChild(error); f.appendChild(actions);
      f.addEventListener("input", () => { saved.textContent = ""; });
      f.addEventListener("submit", async (event) => {
        event.preventDefault();
        error.textContent = ""; saved.textContent = "";
        const updates = {};
        try {
          controls.forEach((c) => { if (c.changed()) updates[c.input.dataset.key] = c.read(); });
        } catch (e) { error.textContent = e.message; return; }
        if (!Object.keys(updates).length) { saved.textContent = "Nothing changed."; return; }
        save.disabled = true;
        try {
          const out = await request("/api/settings", updates);
          cache = null;
          saved.textContent = "Saved.";
          const data2 = await load(true);
          rows(data2).forEach((s) => {
            const c = controls.find((x) => x.input.dataset.key === s.key);
            if (c) c.base = s.value;
          });
          if (opts.onSaved) opts.onSaved(out, updates);
          if (typeof toast === "function") toast("Settings saved");
        } catch (e) {
          error.textContent = e.message;
        } finally { save.disabled = false; }
      });
      host.appendChild(f);
    }).catch((e) => {
      host.replaceChildren(node("p", "hint warn", "Settings could not be loaded: " + e.message));
    });
    return host;
  }
  function when(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  /* workers(container): one row per background worker with its switch
     (a settings key), cadence, last run and last error. */
  function workers(container) {
    const host = node("div", "worker-list");
    container.appendChild(host);
    host.appendChild(node("p", "hint waiting", "Reading background work..."));
    const paint = (data) => {
      host.replaceChildren();
      (data.workers || []).forEach((w) => {
        const row = node("div", "worker-row");
        const sw = node("div", "worker-switch" + (w.enabled_key && typeof w.enabled === "boolean" ? "" : " none"));
        if (w.enabled_key && typeof w.enabled === "boolean") {
          const input = node("input"); input.type = "checkbox"; input.checked = w.enabled;
          input.title = "Setting: " + w.enabled_key;
          input.onchange = async () => {
            input.disabled = true;
            try {
              await request("/api/settings", { [w.enabled_key]: input.checked });
              cache = null;
              if (typeof toast === "function") toast(w.title + (input.checked ? " on" : " off"));
            } catch (e) {
              input.checked = !input.checked;
              if (typeof toast === "function") toast("Could not save: " + e.message);
            } finally { input.disabled = false; }
          };
          sw.appendChild(input);
        }
        row.appendChild(sw);
        row.appendChild(node("div", "worker-title", w.title));
        const meta = [w.cadence, "last run " + when(w.last_run), w.detail].filter(Boolean).join(" · ");
        row.appendChild(node("div", "worker-meta", meta));
        if (w.last_error) row.appendChild(node("div", "worker-error", w.last_error));
        host.appendChild(row);
      });
      host.appendChild(node("p", "hint", "A switch takes effect on the worker's next tick. Cadences are under Advanced settings."));
    };
    request("/api/workers").then(paint).catch((e) => {
      host.replaceChildren(node("p", "hint warn", "Background work could not be read: " + e.message));
    });
    return host;
  }
  function values(data) {
    const out = {};
    rows(data || {}).forEach((s) => { out[s.key] = s.value; });
    return out;
  }
  return { load, form, workers, values, invalidate: () => { cache = null; } };
})();

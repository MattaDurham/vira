/* History - the Time map in Fleet, and the History view inside every
   module window and on every idea card.

   Everything here reads one store through /api/history
   (server/historyroutes.py; modules/fleet/history/README.md has the API).
   Fleet's History tab opens on a time map of the week: one lane per
   project (or per module), sessions as bars colored by app with their
   active time drawn solid inside the time they were open, merges into
   main as diamonds, and automation runs as ticks on one Automations lane
   with failures in red and missed scheduled runs as gaps. Select a day
   (click) or any range (drag) to list it below the map, grouped the way
   the question needs. A plain-words question lights its matches on the
   map. The detail panel leads with the outcome and diff, then the whole
   trail, what it touched, cost and time, and the actions.

   No written story and no summaries: titles, words and numbers taken
   from the records, as the server gives them.

   Live: while a view is on screen it holds /api/history/stream open and
   re-reads what it shows when the store's revision moves. */
"use strict";

(() => {
  // The nine status words. "Not started" (an idea no session has started)
  // is left out of every list until its chip is picked.
  const STATUSES = ["Running", "Waiting on you", "Not merged", "Merged", "Answered", "Ran", "Failed", "Dropped",
                    "Not started"];
  const TONE = { "Running": "live", "Waiting on you": "wait", "Not merged": "open", "Merged": "merged",
                 "Answered": "ok", "Ran": "ok", "Failed": "fail", "Dropped": "dropped", "Not started": "idle" };
  const UNFINISHED = ["Running", "Waiting on you", "Not merged"];
  const GROUPS = [["day", "Day"], ["request", "Request"], ["module", "Module"], ["project", "Project"],
                  ["automation", "Automation"], ["session", "Session"]];
  const ZOOMS = [["day", "Day"], ["week", "Week"], ["month", "Month"]];
  const APP_LABEL = { vira: "Vira", claude: "Claude Code", codex: "Codex", other: "Other" };
  const KIND_WORD = { ask: "Asked", session: "Session", code: "Code", run: "Ran", made: "Made",
                      answer: "Answer", decide: "Decided", tell: "Vira told you", wait: "Waited", more: "" };
  const SUB_WORD = { commit: "Commit", merged: "Merged into main", pr_opened: "Pull request opened",
                     not_merged: "Left not merged", merged_branch: "Branch merged", discarded: "Discarded",
                     journal: "Journal note", idea: "Idea", chat: "Chat", dispatch: "Dispatched",
                     flow: "Flow run", step: "Flow step", program: "Program run", contacts: "Contacts sync",
                     retro: "Retro", note: "Note", document: "Document", contact: "Contact",
                     landing: "Merge choice", approval: "Approval", review: "Review", quiet: "Quiet" };
  const STORE = "vira-history";
  const DAY = 864e5, HOUR = 36e5, MIN = 6e4;
  const NARROW = 560;            // under this width (a phone) the map falls back to a day strip and the list
  const ROW_H = 8, ROW_GAP = 2, MAX_ROWS = 6;
  const LBL_W = 138;
  const PINNED_LIMIT = 60;       // unfinished entries read for the pin; the rest are one click away
  const WIDE = 1000;             // from this width the detail sits beside the map
  const PIN_SHOWN = 4;          // pinned rows shown before "Show all"

  // ---------- small helpers ----------
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  };
  const btn = (cls, text, onClick, title) => {
    const b = el("button", cls, text);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", (e) => { if (e && e.stopPropagation) e.stopPropagation(); onClick(e); });
    return b;
  };
  const SVGNS = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs, text) => {
    const n = document.createElementNS(SVGNS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v != null) n.setAttribute(k, String(v)); });
    if (text != null) n.textContent = String(text);
    return n;
  };
  const svTitle = (node, text) => { node.append(sv("title", null, text)); return node; };
  const qs = (params) => Object.entries(params)
    .filter(([, v]) => v != null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(Array.isArray(v) ? v.join(",") : v)}`).join("&");
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      const d = data && data.detail;
      throw new Error(typeof d === "string" ? d : `Request failed (${r.status})`);
    }
    return data;
  }
  async function postJSON(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body || {}) });
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return { ok: r.ok, status: r.status, data };
  }
  const store = () => {
    try { return (typeof lsGet === "function" ? lsGet(STORE, {}) : {}) || {}; } catch (e) { return {}; }
  };
  const persist = (patch) => {
    try { if (typeof lsSet === "function") lsSet(STORE, Object.assign(store(), patch)); } catch (e) { /* storage unavailable */ }
  };
  const say = (text) => { try { if (typeof toast === "function") toast(text); } catch (e) { /* no shell */ } };

  // ---------- time ----------
  const ts = (iso) => { const t = iso ? Date.parse(iso) : NaN; return Number.isFinite(t) ? t : 0; };
  const dayStart = (ms) => { const d = new Date(ms); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); };
  const addDays = (ms, n) => { const d = new Date(ms); return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n).getTime(); };
  const ymd = (ms) => {
    const d = new Date(ms);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  const fromYmd = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
    return m ? new Date(+m[1], +m[2] - 1, +m[3]).getTime() : 0;
  };
  const fmtDay = (ms) => new Date(ms).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  const fmtShort = (ms) => new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const fmtTime = (ms) => new Date(ms).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const fmtWhen = (ms) => !ms ? "time not recorded" : dayStart(ms) === dayStart(Date.now()) ? fmtTime(ms)
    : `${fmtShort(ms)}, ${fmtTime(ms)}`;
  const fmtDur = (s) => {
    s = Math.max(0, Math.round(Number(s) || 0));
    if (s < 60) return `${s}s`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}h ${m % 60}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  };
  const fmtCost = (c) => c == null ? "" : c === 0 ? "$0" : c < 0.01 ? "under $0.01" : `$${c.toFixed(c < 10 ? 2 : 0)}`;
  const fmtTok = (n) => !n ? "" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M tokens` : n >= 1000 ? `${Math.round(n / 1000)}k tokens` : `${n} tokens`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  const spanLabel = (a, b) => {
    if (!a || !b) return "";
    if (b - a === DAY && dayStart(a) === a) return fmtDay(a);
    if (dayStart(a) === a && dayStart(b) === b) return `${fmtDay(a)} to ${fmtDay(b - DAY)}`;
    if (dayStart(a) === dayStart(b - 1)) return `${fmtDay(a)}, ${fmtTime(a)} to ${fmtTime(b)}`;
    return `${fmtShort(a)} ${fmtTime(a)} to ${fmtShort(b)} ${fmtTime(b)}`;
  };
  const appKey = (a) => /codex/i.test(a || "") ? "codex" : /claude/i.test(a || "") ? "claude"
    : /vira/i.test(a || "") ? "vira" : "other";
  const prNum = (p) => { const m = /#(\d+)$/.exec(p || ""); return m ? `#${m[1]}` : p; };
  const prText = (prs) => (prs || []).map((p) => `PR ${prNum(p)}`).join(", ");

  // A period by zoom, ending on the anchor day.
  function period(zoom, anchor) {
    const end = addDays(anchor, 1);
    if (zoom === "day") return [anchor, end];
    if (zoom === "month") return [addDays(anchor, -29), end];
    return [addDays(anchor, -6), end];
  }
  // The zoom that fits a span, and the anchor that puts its end in view.
  function fitZoom(a, b) {
    const days = Math.max(1, Math.round((dayStart(b - 1) - dayStart(a)) / DAY) + 1);
    return { zoom: days <= 1 ? "day" : days <= 7 ? "week" : "month",
             anchor: Math.min(dayStart(Date.now()), Math.max(dayStart(b - 1), addDays(dayStart(a), days <= 7 ? 6 : 29))) };
  }

  // Sessions packed into rows: each bar takes the first row free by then.
  function packRows(spans, maxRows) {
    const ends = [];
    let hidden = 0;
    const placed = [];
    spans.slice().sort((x, y) => x._a - y._a).forEach((s) => {
      let r = ends.findIndex((end) => end <= s._a);
      if (r < 0) { r = ends.length; ends.push(0); }
      ends[r] = Math.max(s._b, s._a + 1);
      if (r >= maxRows) { hidden += 1; return; }
      placed.push(Object.assign(s, { _r: r }));
    });
    return { placed, rows: Math.min(ends.length, maxRows), hidden };
  }

  // Map items carry ISO times; give each a numeric span once.
  function prepMap(map, now) {
    (map.lanes || []).forEach((ln) => {
      (ln.spans || []).forEach((s) => {
        s._a = ts(s.start);
        const end = ts(s.end) || s._a;
        s._b = s.status === "Running" ? Math.max(end, now) : Math.max(end, s._a);
      });
      (ln.points || []).forEach((p) => { p._a = ts(p.at); });
      (ln.ticks || []).forEach((t) => { t._a = ts(t.at); t._b = Math.max(ts(t.end) || t._a, t._a); });
    });
    return map;
  }
  // Every automation run in the period, once, whichever lanes it sat on.
  function allTicks(map) {
    const seen = new Map();
    (map.lanes || []).forEach((ln) => (ln.ticks || []).forEach((t) => { if (!seen.has(t.id)) seen.set(t.id, t); }));
    return [...seen.values()].sort((a, b) => a._a - b._a);
  }
  // Missed scheduled slots from the status grid, as [start, end, row name].
  function missedSlots(grid) {
    const out = [];
    ((grid && grid.rows) || []).forEach((r) => (r.cells || []).forEach((c) => {
      if (c.outcome === "missed") out.push({ a: ts(c.start), b: ts(c.end), name: r.name, row: r.id });
    }));
    return out;
  }
  // Per-day counts for the strip and the month calendar.
  function dayCounts(map, grid, a, b) {
    const days = [];
    for (let d = a; d < b; d = addDays(d, 1)) days.push({ a: d, b: addDays(d, 1), sessions: 0, merges: 0, runs: 0, failed: 0, missed: 0, openH: 0, live: 0 });
    const find = (t) => days.find((d) => t >= d.a && t < d.b);
    (map && map.lanes || []).forEach((ln) => {
      (ln.spans || []).forEach((s) => days.forEach((d) => {
        if (s._a < d.b && s._b >= d.a) {
          d.sessions += 1;
          d.openH += (Math.min(s._b, d.b) - Math.max(s._a, d.a)) / HOUR;
          if (UNFINISHED.includes(s.status)) d.live += 1;
        }
      }));
      (ln.points || []).forEach((p) => { const d = find(p._a); if (d) d.merges += 1; });
    });
    if (map) allTicks(map).forEach((t) => { const d = find(t._a); if (d) { d.runs += 1; if (t.status === "Failed") d.failed += 1; } });
    missedSlots(grid).forEach((m) => { const d = find(m.a); if (d) d.missed += 1; });
    return days;
  }

  // ---------- shared pieces: status word, entry row, trail steps ----------
  function pill(status) {
    const p = el("span", `hx-st t-${TONE[status] || "none"}`, status || "Recorded");
    p.title = status || "";
    return p;
  }
  function stepLabel(s) {
    if (s.type === "more") return s.title;
    if (s.type === "gap") return `Nothing for ${fmtDur((ts(s.end) - ts(s.at)) / 1000)}`;
    if (s.type === "wait") return s.end ? `${s.title}: ${fmtDur((ts(s.end) - ts(s.at)) / 1000)}` : s.title;
    const sub = SUB_WORD[s.sub];
    if (s.kind === "code" && sub) return sub;
    if (s.kind === "session") return `${APP_LABEL[appKey(s.sub)] || "Session"} session`;
    return KIND_WORD[s.kind] || s.kind;
  }
  // A request's trail drawn inline: one chip per step, in order.
  function stepsStrip(steps) {
    const box = el("ol", "hx-steps");
    box.setAttribute("aria-label", "Trail");
    (steps || []).forEach((s) => {
      const li = el("li", `hx-step k-${s.type === "event" ? s.kind : s.type} t-${TONE[s.status] || "none"}${s.sub === "merged" ? " is-merge" : ""}`);
      li.append(el("span", "hx-step-k", stepLabel(s)));
      if (s.at && s.type !== "more") li.append(el("span", "hx-step-t", fmtWhen(ts(s.at))));
      li.title = `${stepLabel(s)}${s.title && s.type === "event" ? ": " + s.title : ""}${s.status ? " (" + s.status + ")" : ""}`;
      box.append(li);
    });
    return box;
  }
  function metaLine(e) {
    const bits = [];
    const projects = (e.projects && e.projects.length ? e.projects : [e.project]).filter(Boolean);
    if (projects.length) bits.push(projects.join(", "));
    if (e.module) bits.push(e.module);
    if (e.apps && e.apps.length) bits.push(e.apps.join(", "));
    else if (e.sub && e.type === "event") bits.push(SUB_WORD[e.sub] || e.sub);
    if (e.prs && e.prs.length) bits.push(prText(e.prs));
    const t = ts(e.at);
    if (t) bits.push(fmtWhen(t));
    const l = e.lines;
    if (l && (l.added || l.removed)) bits.push(`+${l.added || 0} -${l.removed || 0} in ${plural(l.files || 0, "file")}`);
    if (e.counts && typeof e.counts === "object") {
      const c = Object.entries(e.counts).filter(([, v]) => typeof v === "number" && v).map(([k, v]) => `${v} ${k}`);
      if (c.length) bits.push(c.slice(0, 4).join(", "));
    }
    if (e.cost_usd) bits.push(fmtCost(e.cost_usd));
    if (e.active_s) bits.push(`${fmtDur(e.active_s)} active`);
    return bits.join(" / ");
  }
  // One row for every kind of entry, in every grouping and every view.
  function entryRow(e, ctx) {
    const row = el("article", `hx-row t-${TONE[e.status] || "none"}${ctx.picked === e.id ? " on" : ""}${e.open ? " is-open" : ""}`);
    row.dataset.id = e.id;
    if (e.trail) row.dataset.trail = e.trail;
    const head = el("div", "hx-row-head");
    head.append(pill(e.status));
    const title = btn("hx-row-title", e.title || "Untitled", () => ctx.pick(e.id), "Open its detail");
    head.append(title);
    row.append(head, el("div", "hx-row-meta", metaLine(e)));
    if (e.steps && e.steps.length && !ctx.noSteps) row.append(stepsStrip(e.steps));
    if ((e.actions || []).length && ctx.actions !== false) {
      const acts = el("div", "hx-row-acts");
      e.actions.forEach((a) => acts.append(btn(`hx-btn${a.action === "merge" ? " pri" : ""}`, a.label,
        (ev) => ctx.act(a, acts, ev), a.label)));
      row.append(acts);
    }
    row.addEventListener("click", (ev) => {
      if (ev.target.closest && ev.target.closest("button, a, input, textarea, select")) return;
      ctx.pick(e.id);
    });
    return row;
  }

  // ---------- acting on an entry: the server's own confirmations ----------
  // Merge and Discard answer 409 with the confirm text until confirmed;
  // Resume answers with the prompt to review first.
  async function runAction(a, box, done) {
    const url = a.url || "/api/history/action";
    const body = Object.assign({}, a.body || {});
    const note = (text, cls) => {
      box.querySelectorAll(".hx-act-note").forEach((n) => n.remove());
      if (text) box.append(el("div", `hx-act-note ${cls || ""}`, text));
    };
    const send = async (extra) => {
      note("Working...", "dim");
      let r;
      try { r = await postJSON(url, Object.assign({}, body, extra)); }
      catch (err) { note(`Could not reach Vira: ${err.message || err}`, "bad"); return; }
      const d = r.data || {};
      const det = d.detail && typeof d.detail === "object" ? d.detail : null;
      if (r.status === 409 && det && det.needs_confirm) { confirmBox(det.confirm || `${a.label}?`, extra); return; }
      if (!r.ok) { note(typeof d.detail === "string" ? d.detail : `${a.label} failed (${r.status})`, "bad"); return; }
      if (d.needs_prompt) { promptBox(d.prompt || "", d.cwd || ""); return; }
      const what = d.via ? `${a.label}: started through the ${d.via}` : d.job_id ? `${a.label}: session started`
        : d.run_id ? `${a.label}: run started` : d.judge_job_id ? "Judge started" : d.started ? `${a.label}: started` : `${a.label}: done`;
      note(what, "ok");
      say(what);
      if (done) done(d);
    };
    const clear = () => box.querySelectorAll(".hx-confirm").forEach((n) => n.remove());
    const confirmBox = (text, extra) => {
      clear(); note("");
      const c = el("div", "hx-confirm");
      c.append(el("p", null, text),
        btn("hx-btn", "Cancel", () => c.remove()),
        btn("hx-btn pri", `Confirm ${a.label.toLowerCase()}`, () => { c.remove(); send(Object.assign({}, extra, { confirm: true })); }));
      box.append(c);
    };
    const promptBox = (prompt, cwd) => {
      clear(); note("");
      const c = el("div", "hx-confirm hx-prompt");
      c.append(el("p", null, `Review the prompt the session resumes with${cwd ? ` (in ${cwd})` : ""}:`));
      const ta = el("textarea", "hx-ta");
      ta.value = prompt;
      ta.rows = 6;
      c.append(ta, btn("hx-btn", "Cancel", () => c.remove()),
        btn("hx-btn pri", "Start session", () => { const p = ta.value.trim(); if (!p) return; c.remove(); send({ prompt: p }); }));
      box.append(c);
    };
    if (a.action === "resume" && !body.prompt) { send({}); return; }
    send({});
  }

  // ---------- the detail: outcome and diff, trail, touched, cost and time ----------
  function section(title, ...kids) {
    const s = el("section", "hx-dsec");
    s.append(el("h4", null, title), ...kids.filter(Boolean));
    return s;
  }
  function openThing(o) {
    if (o.job_id && typeof openSession === "function") { openSession(o.job_id); return; }
    if (o.job_id && typeof window.openJob === "function") { window.openJob(o.job_id); return; }
    if (o.url) { window.open(o.url, "_blank", "noopener"); return; }
    if (o.path && typeof copyText === "function") { copyText(o.path); say("Folder path copied"); }
  }
  function openLabel(o) {
    if (o.what === "session") return "Open transcript";
    return o.label || "Open";
  }
  function renderDetail(box, d, ctx) {
    const e = d.entry || {};
    box.replaceChildren();
    const head = el("div", "hx-dhead");
    const top = el("div", "hx-dtop");
    top.append(pill(e.status));
    if (ctx.close) top.append(btn("hx-x", "Close", ctx.close, "Close the detail"));
    head.append(top, el("h3", null, e.title || "Untitled"), el("div", "hx-row-meta", metaLine(e)));
    box.append(head);
    // Outcome and diff first.
    const o = d.outcome || {};
    const out = [];
    out.push(el("p", "hx-means", o.means || ""));
    if (o.ended_by) out.push(el("p", "hx-ended", `Ended by: ${o.ended_by.title || SUB_WORD[o.ended_by.sub] || o.ended_by.kind}, ${fmtWhen(ts(o.ended_by.at))}`));
    const df = o.diff;
    if (df && (df.files || df.added || df.removed)) {
      out.push(el("p", "hx-diffsum", `+${df.added || 0} -${df.removed || 0} in ${plural(df.files || 0, "file")}${df.basis ? ` (${df.basis})` : ""}`));
      const files = df.by_file || [];
      if (files.length) {
        const t = el("table", "hx-files");
        files.slice(0, 60).forEach(([path, add, rem]) => {
          const tr = el("tr");
          tr.append(el("td", "hx-fp", path), el("td", "hx-add", `+${add || 0}`), el("td", "hx-rem", `-${rem || 0}`));
          t.append(tr);
        });
        out.push(t);
        if (files.length > 60) out.push(el("p", "hx-dim", `${files.length - 60} more files`));
      }
    } else out.push(el("p", "hx-dim", "No code changed on record."));
    box.append(section("Outcome", ...out));
    // Actions sit with the outcome: what to do about it.
    const actBox = el("div", "hx-dacts");
    (d.actions || []).forEach((a) => actBox.append(btn(`hx-btn${a.action === "merge" ? " pri" : ""}`, a.label,
      (ev) => runAction(a, actBox, () => ctx.refresh && ctx.refresh()), a.label)));
    const seenOpen = new Set();
    (d.open || []).forEach((o2) => {
      const key = o2.job_id || o2.url || o2.path || o2.label;
      if (seenOpen.has(key)) return;
      seenOpen.add(key);
      // several sessions each open their own transcript: say whose by its start
      const many = (d.open || []).filter((x) => x.what === "session").length > 1;
      const at = many && o2.what === "session" ? (d.trail || []).find((s) => s.id === o2.event) : null;
      actBox.append(btn("hx-btn", at && at.at ? `${openLabel(o2)}, ${fmtWhen(ts(at.at))}` : openLabel(o2),
        () => openThing(o2), (at && at.title) || o2.url || o2.label));
    });
    if (actBox.childNodes.length) box.append(actBox);
    // The whole trail.
    const tl = el("ol", "hx-trail");
    (d.trail || []).forEach((s) => {
      const li = el("li", `hx-ti k-${s.type === "event" ? s.kind : s.type} t-${TONE[s.status] || "none"}${s.sub === "merged" ? " is-merge" : ""}`);
      if (d.focus && d.focus.id === s.id) li.classList.add("focus");
      li.append(el("span", "hx-ti-t", s.at ? fmtWhen(ts(s.at)) : ""));
      const body = el("span", "hx-ti-b");
      body.append(el("b", null, stepLabel(s)));
      if (s.type === "event" && s.title) body.append(document.createTextNode(` ${s.title}`));
      const dd = s.data || {};
      const extra = [];
      if (s.end && s.type === "event") extra.push(`until ${fmtWhen(ts(s.end))}`);
      if (dd.model) extra.push(dd.model);
      if (dd.active_s) extra.push(`${fmtDur(dd.active_s)} active`);
      if (dd.cost_usd) extra.push(fmtCost(dd.cost_usd));
      if (dd.diff && (dd.diff.added || dd.diff.removed)) extra.push(`+${dd.diff.added || 0} -${dd.diff.removed || 0}`);
      if (dd.errors) extra.push(`${dd.errors} errors`);
      if (s.status) extra.push(s.status);
      if (extra.length) body.append(el("span", "hx-ti-x", extra.join(" / ")));
      li.append(body);
      tl.append(li);
    });
    box.append(section(`Trail (${plural((d.trail || []).filter((s) => s.type === "event").length, "step")})`, tl));
    // What it touched.
    const t = d.touched || {};
    const touched = [];
    const chipRow = (label, items, onClick) => {
      if (!items || !items.length) return null;
      const r = el("div", "hx-chiprow");
      r.append(el("span", "hx-lbl", label));
      items.forEach((it) => r.append(onClick ? btn("hx-chip", it, () => onClick(it), `Filter by ${it}`) : el("span", "hx-chip", it)));
      return r;
    };
    touched.push(chipRow("Modules", Object.keys(t.modules || {}), ctx.filterModule));
    touched.push(chipRow("Projects", t.projects || [], ctx.filterProject));
    touched.push(chipRow("Branches", (t.branches || []).map((b) => b.replace(/^[^:]*:/, ""))));
    touched.push(chipRow("Pull requests", (t.prs || []).map(prNum)));
    if ((t.made || []).length) touched.push(chipRow("Made", t.made.map((m) => m.title || m.id || String(m))));
    if ((t.files || []).length) {
      const ul = el("ul", "hx-flist");
      t.files.slice(0, 40).forEach((f) => ul.append(el("li", null, f)));
      touched.push(el("div", "hx-lbl", `Files (${t.files_n || t.files.length})`), ul);
    }
    const tk = touched.filter(Boolean);
    box.append(section("What it touched", ...(tk.length ? tk : [el("p", "hx-dim", "Nothing recorded.")])));
    // Cost and time.
    const c = d.cost_time || {};
    const ct = [];
    const nums = el("dl", "hx-nums");
    const kv = (k, v) => { if (v) nums.append(el("dt", null, k), el("dd", null, v)); };
    kv("Cost", c.cost_usd != null ? `${fmtCost(c.cost_usd)}${c.cost_sessions_unpriced ? ` (${plural(c.cost_sessions_unpriced, "session")} with no price)` : ""}` : "");
    kv("Tokens", c.tokens ? c.tokens.toLocaleString("en-US") : "");
    kv("Active", c.active_s ? fmtDur(c.active_s) : "");
    kv("Start to end", c.wall_s ? fmtDur(c.wall_s) : "");
    ct.push(nums);
    if (c.cost_basis && c.cost_usd) ct.push(el("p", "hx-dim", c.cost_basis));
    (c.sessions || []).forEach((s) => {
      const r = el("div", "hx-sess");
      r.append(el("span", `hx-app a-${appKey(s.app)}`, APP_LABEL[appKey(s.app)] || s.app || ""),
        el("span", "hx-sess-t", s.title || ""),
        el("span", "hx-dim", [s.model, s.active_s ? `${fmtDur(s.active_s)} active` : "", s.cost_usd ? fmtCost(s.cost_usd) : "",
          s.start ? fmtWhen(ts(s.start)) : ""].filter(Boolean).join(" / ")));
      ct.push(r);
    });
    box.append(section("Cost and time", ...ct));
  }

  // ---------- the live connection, shared by every mounted view ----------
  const Live = {
    views: new Set(), es: null, rev: null, timer: null,
    add(v) { this.views.add(v); this.watch(); if (!this.timer) this.timer = setInterval(() => this.watch(), 3000); },
    remove(v) { this.views.delete(v); this.watch(); },
    anyVisible() { return [...this.views].some((v) => v.visible()); },
    watch() {
      // A History panel on a card that was re-rendered away is gone for good.
      this.views.forEach((v) => { if (v.transient && !v.root.isConnected) this.views.delete(v); });
      if (this.anyVisible()) this.connect(); else this.disconnect();
      this.views.forEach((v) => { if (v.stale && v.visible()) { v.stale = false; v.refresh(); } });
    },
    connect() {
      if (this.es || typeof EventSource !== "function") return;
      const es = new EventSource("/api/history/stream");
      this.es = es;
      es.addEventListener("rev", (ev) => {
        let d = null;
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        this.onRev(d && d.revision);
      });
      es.onerror = () => { /* EventSource reconnects by itself */ };
    },
    disconnect() { if (this.es) { this.es.close(); this.es = null; } },
    onRev(rev) {
      if (!rev) return;
      const moved = this.rev && rev !== this.rev;
      this.rev = rev;
      if (!moved) return;
      this.views.forEach((v) => { if (v.visible()) v.refreshSoon(); else v.stale = true; });
    },
  };

  // =====================================================================
  // The Time map: Fleet's History tab.
  // =====================================================================
  function mountMap(root, opts) {
    opts = opts || {};
    const saved = store();
    const st = {
      zoom: ZOOMS.some(([k]) => k === saved.zoom) ? saved.zoom : "week",
      anchor: dayStart(Date.now()),
      lane: saved.lane === "module" ? "module" : "project",
      layers: Object.assign({ sessions: true, merges: true, automations: true }, saved.layers || {}),
      group: GROUPS.some(([k]) => k === saved.group) ? saved.group : "day",
      sel: null,                  // {a, b}: the selected span, ms
      f: { project: "", module: "", status: [], kind: [], q: "", rules: false },
      ask: null,                  // {q, source, note, view}
      hits: null,                 // Set of trail ids the filters match, or null
      rules: null,                // {sessions: Set, items: [...]}, read when the filter is on
      gridOpen: false,
      picked: null, detail: null,
      map: null, grid: null, list: null, pinned: null, facets: null,
      loading: 0, error: "", seq: 0, offset: 0,
    };
    const view = { stale: false, visible: () => !!root.isConnected && root.offsetParent !== null && !(typeof document !== "undefined" && document.hidden) };
    let soon = null;
    view.refreshSoon = () => { clearTimeout(soon); soon = setTimeout(() => view.refresh(), 800); };

    root.classList.add("hx-root");
    root.replaceChildren();
    const bar = el("div", "hx-ctl");
    const askRow = el("form", "hx-ask");
    const filt = el("div", "hx-filters");
    const body = el("div", "hx-body");
    const main = el("div", "hx-main");
    const aside = el("aside", "hx-detail");
    aside.hidden = true;
    const pinned = el("section", "hx-pinned");
    const mapBox = el("div", "hx-map");
    const gridBox = el("div", "hx-gridbox");
    gridBox.hidden = true;
    const listHead = el("div", "hx-listhead");
    const list = el("div", "hx-list");
    main.append(pinned, mapBox, gridBox, listHead, list);
    body.append(main, aside);
    root.append(bar, askRow, filt, body);

    // ----- the ask box
    const askIn = el("input", "hx-askin");
    askIn.type = "search";
    askIn.placeholder = "Ask in plain words: what shipped this week? did notes capture run yesterday?";
    askIn.setAttribute("aria-label", "Ask about history in plain words");
    const askGo = el("button", "hx-btn pri", "Ask");
    askGo.type = "submit";
    const askChips = el("div", "hx-askchips");
    askRow.append(askIn, askGo, askChips);
    askRow.addEventListener("submit", (ev) => { ev.preventDefault(); ask(askIn.value); });

    const narrow = () => (root.clientWidth || (typeof window !== "undefined" ? window.innerWidth : 1200)) < NARROW;

    // ----- controls
    function seg(name, options, current, onPick) {
      const g = el("div", "hx-seg");
      g.setAttribute("role", "group");
      g.setAttribute("aria-label", name);
      options.forEach(([val, label]) => {
        const b = btn(null, label, () => onPick(val));
        b.setAttribute("aria-pressed", String(val === current));
        b.dataset.val = val;
        g.append(b);
      });
      return g;
    }
    function toggle(label, on, onFlip, cls, title) {
      const b = btn(`hx-tog${on ? " on" : ""}${cls ? " " + cls : ""}`, label, onFlip, title);
      b.setAttribute("aria-pressed", String(!!on));
      return b;
    }
    function shippedOnly() { return st.layers.merges && !st.layers.sessions && !st.layers.automations; }
    function renderBar() {
      const [a, b] = period(st.zoom, st.anchor);
      const today = dayStart(Date.now());
      const pan = el("div", "hx-pan");
      const step = st.zoom === "day" ? 1 : st.zoom === "week" ? 7 : 30;
      pan.append(btn("hx-btn", "Earlier", () => { st.anchor = addDays(st.anchor, -step); reload(); }, "Show the period before"),
        btn("hx-btn", "Today", () => { st.anchor = today; reload(); }, "Back to today"),
        btn("hx-btn", "Later", () => { st.anchor = Math.min(today, addDays(st.anchor, step)); reload(); }, "Show the period after"));
      pan.querySelectorAll("button")[2].disabled = st.anchor >= today;
      const lab = el("span", "hx-period", spanLabel(a, b));
      const layers = el("div", "hx-layers");
      layers.append(el("span", "hx-lbl", "Show"),
        toggle("Sessions", st.layers.sessions, () => flip("sessions")),
        toggle("Merges", st.layers.merges, () => flip("merges")),
        toggle("Automations", st.layers.automations, () => flip("automations")),
        toggle("Shipped view", shippedOnly(), () => {
          st.layers = shippedOnly() ? { sessions: true, merges: true, automations: true } : { sessions: false, merges: true, automations: false };
          persist({ layers: st.layers }); renderBar(); drawMap();
        }, "ship", "Only merges into main: what shipped"));
      bar.replaceChildren(
        seg("Zoom", ZOOMS, st.zoom, (z) => { st.zoom = z; persist({ zoom: z }); reload(); }),
        pan, lab,
        seg("Lanes", [["project", "Projects"], ["module", "Modules"]], st.lane, (l) => { st.lane = l; persist({ lane: l }); reload(); }),
        layers);
    }
    function flip(k) {
      st.layers[k] = !st.layers[k];
      persist({ layers: st.layers });
      renderBar();
      drawMap();
    }
    function renderFilters() {
      const fac = st.facets || {};
      const outcome = el("div", "hx-outcome");
      outcome.setAttribute("role", "group");
      outcome.setAttribute("aria-label", "Outcome");
      outcome.append(el("span", "hx-lbl", "Outcome"));
      // Each word with how many entries in the span carry it; Not started
      // counts the ideas the list is leaving out until it is picked.
      const counts = (st.list && st.list.statuses) || null;
      STATUSES.forEach((s) => {
        const on = st.f.status.includes(s);
        const means = ((fac.statuses || []).find((x) => x.key === s) || {}).means || "";
        const n = !counts ? null : s === "Not started" && !on
          ? (st.list.unstarted_left_out != null ? st.list.unstarted_left_out : counts[s]) : counts[s];
        const t = toggle(s, on, () => {
          st.f.status = on ? st.f.status.filter((x) => x !== s) : st.f.status.concat(s);
          filtersChanged();
        }, `t-${TONE[s]}`, means);
        t.dataset.word = s;
        if (n != null) t.append(el("span", "hx-n", String(n)));
        outcome.append(t);
      });
      const pick = (label, key, items) => {
        const sel = el("select", "hx-select");
        sel.setAttribute("aria-label", label);
        const all = el("option", null, `All ${label.toLowerCase()}s`);
        all.value = "";
        sel.append(all);
        const keys = items.map((x) => x.key);
        if (st.f[key] && !keys.includes(st.f[key])) keys.unshift(st.f[key]);
        keys.forEach((k) => { const o = el("option", null, k); o.value = k; sel.append(o); });
        sel.value = st.f[key];
        sel.addEventListener("change", () => { st.f[key] = sel.value; filtersChanged(); });
        return sel;
      };
      const modules = (fac.modules || []).slice().sort((x, y) => String(x.key).localeCompare(String(y.key)));
      const rules = toggle("Sessions that broke a rule", st.f.rules, () => { st.f.rules = !st.f.rules; filtersChanged(); }, "rule",
        "Sessions where a rule was broken, with dismiss and propose-a-guard");
      const clear = btn("hx-lk", "Clear filters", () => {
        st.f = { project: "", module: "", status: [], kind: [], q: "", rules: false };
        st.ask = null; askIn.value = "";
        filtersChanged();
      });
      const any = st.f.project || st.f.module || st.f.status.length || st.f.kind.length || st.f.q || st.f.rules;
      clear.hidden = !any;
      filt.replaceChildren(outcome, pick("Project", "project", fac.projects || []), pick("Module", "module", modules), rules, clear);
    }
    function renderAskChips() {
      const chips = [];
      const chip = (label, onRemove) => {
        const c = el("span", "hx-achip", label);
        c.append(btn("hx-achip-x", "x", onRemove, `Remove: ${label}`));
        chips.push(c);
      };
      if (st.ask) {
        if (st.sel && st.ask.span) chip(`When: ${spanLabel(st.sel.a, st.sel.b)}`, () => { st.ask.span = false; st.sel = null; filtersChanged(); });
        if (st.f.project) chip(`Project: ${st.f.project}`, () => { st.f.project = ""; filtersChanged(); });
        if (st.f.module) chip(`Module: ${st.f.module}`, () => { st.f.module = ""; filtersChanged(); });
        st.f.status.forEach((s) => chip(`Outcome: ${s}`, () => { st.f.status = st.f.status.filter((x) => x !== s); filtersChanged(); }));
        st.f.kind.forEach((k) => chip(`Kind: ${k}`, () => { st.f.kind = st.f.kind.filter((x) => x !== k); filtersChanged(); }));
        if (st.f.q) chip(`Words: ${st.f.q}`, () => { st.f.q = ""; filtersChanged(); });
      } else {
        st.f.kind.forEach((k) => chip(`Kind: ${k}`, () => { st.f.kind = st.f.kind.filter((x) => x !== k); filtersChanged(); }));
        if (st.f.q) chip(`Words: ${st.f.q}`, () => { st.f.q = ""; filtersChanged(); });
      }
      const note = st.ask && st.ask.note ? el("span", "hx-asknote", st.ask.note) : null;
      askChips.replaceChildren(...chips, ...(note ? [note] : []));
    }
    function filtersChanged() {
      renderFilters();
      renderAskChips();
      loadHits().then(() => { drawMap(); });
      loadRules().then(() => drawMap());
      loadList();
    }

    // ----- loading
    const listParams = (extra) => Object.assign({
      project: st.f.project, module: st.f.module, status: st.f.status, kind: st.f.kind, q: st.f.q,
    }, extra || {});
    async function loadMap() {
      const [a, b] = period(st.zoom, st.anchor);
      const seq = ++st.seq;
      const span = st.zoom === "month" ? "day" : "hour";
      try {
        const [map, grid] = await Promise.all([
          getJSON(`/api/history/timemap?${qs({ since: ymd(a), until: ymd(b - DAY), lane: st.lane, project: st.f.project, module: st.f.module })}`),
          getJSON(`/api/history/grid?${qs({ span, since: ymd(a), until: ymd(b - DAY), project: st.f.project })}`).catch(() => null),
        ]);
        if (seq !== st.seq) return;
        st.map = prepMap(map, Date.now());
        st.grid = grid;
        st.error = "";
      } catch (err) {
        if (seq !== st.seq) return;
        st.error = `Could not read the time map: ${err.message || err}`;
      }
      drawMap();
      if (st.gridOpen) drawGrid();
    }
    async function loadHits() {
      const narrowing = st.f.q || st.f.kind.length || st.f.status.length;
      if (!narrowing) { st.hits = null; return; }
      const [a, b] = period(st.zoom, st.anchor);
      try {
        const out = await getJSON(`/api/history?${qs(listParams({ group: "none", since: new Date(a).toISOString(), until: new Date(b).toISOString(), automations: 1, limit: 1000 }))}`);
        const set = new Set();
        (out.groups || []).forEach((g) => (g.entries || []).forEach((e) => set.add(e.id)));
        st.hits = set;
      } catch (err) { st.hits = null; }
    }
    // "Sessions that broke a rule": the lessons record which sessions broke
    // which rule; the map and the list narrow to those sessions.
    async function loadRules() {
      if (!st.f.rules) { st.rules = null; return; }
      try {
        const out = await getJSON("/api/history/rules");
        st.rules = { items: out.items || [], sessions: new Set((out.items || []).flatMap((r) => r.history_ids || [])),
                     trails: new Set((out.items || []).map((r) => r.trail).filter(Boolean)) };
      } catch (err) { st.rules = { items: [], sessions: new Set(), trails: new Set(), error: err.message || String(err) }; }
    }
    async function loadPinned() {
      try {
        st.pinned = await getJSON(`/api/history?${qs({ group: "none", status: UNFINISHED, limit: PINNED_LIMIT })}`);
      } catch (err) { st.pinned = { error: err.message || String(err) }; }
      drawPinned();
    }
    async function loadFacets() {
      try { st.facets = await getJSON("/api/history/facets"); } catch (err) { st.facets = st.facets || {}; }
      renderFilters();
    }
    async function loadList(more) {
      const seq = ++st.listSeq || (st.listSeq = 1);
      if (!more) st.offset = 0;
      if (st.f.rules) { await loadRules(); if (seq !== st.listSeq) return; drawRules(); return; }
      const [pa, pb] = period(st.zoom, st.anchor);
      const a = st.sel ? st.sel.a : pa, b = st.sel ? st.sel.b : pb;
      const params = listParams({ group: st.group, since: new Date(a).toISOString(), until: new Date(b).toISOString(),
                                  steps: 1, limit: 200, offset: st.offset });
      if (!more) list.replaceChildren(el("p", "hx-dim", "Reading..."));
      try {
        const out = await getJSON(`/api/history?${qs(params)}`);
        if (seq !== st.listSeq) return;
        if (more && st.list) {
          out.groups.forEach((g) => {
            const have = st.list.groups.find((x) => x.key === g.key);
            if (have) have.entries = have.entries.concat(g.entries); else st.list.groups.push(g);
          });
          st.list.more = out.more; st.list.shown += out.shown;
        } else st.list = out;
        drawList();
        renderFilters();
        if (st.ask && st.ask.view === "entry" && !st.picked) {
          const first = (out.groups[0] || {}).entries && out.groups[0].entries[0];
          if (first) pick(first.id);
          st.ask.view = "list";
        }
      } catch (err) {
        if (seq !== st.listSeq) return;
        list.replaceChildren(el("p", "hx-err", `Could not read the list: ${err.message || err}`));
      }
    }
    async function reload() {
      renderBar();
      await Promise.all([loadMap(), loadHits()]);
      drawMap();
      loadList();
    }
    view.refresh = () => { loadMap(); loadPinned(); loadList(); if (st.picked) loadDetail(st.picked, true); };

    // ----- the pinned unfinished work
    function drawPinned() {
      const p = st.pinned;
      if (!p || p.error) { pinned.replaceChildren(...(p && p.error ? [el("p", "hx-err", p.error)] : [])); return; }
      const rows = (p.groups || []).flatMap((g) => g.entries || []);
      const order = (e) => UNFINISHED.indexOf(e.status);
      rows.sort((x, y) => order(x) - order(y) || ts(y.at) - ts(x.at));
      if (!rows.length) { pinned.replaceChildren(); return; }
      const c = p.statuses || {};
      const head = el("div", "hx-pin-head");
      const fold = saved.pinFold === true;
      head.append(el("b", null, "Unfinished, pinned"),
        el("span", "hx-dim", UNFINISHED.filter((s) => c[s]).map((s) => `${c[s]} ${s.toLowerCase()}`).join(" / ")),
        btn("hx-lk", fold ? "Show" : "Hide", () => { saved.pinFold = !fold; persist({ pinFold: !fold }); drawPinned(); }));
      const box = el("div", "hx-pin-rows");
      box.hidden = fold;
      // Compact, so the map stays in view: one line of chips (status word
      // and title, Running first; a chip opens its detail) until "Show all"
      // lists them as full rows.
      if (st.pinAll) {
        const pinCtx = Object.create(ctx);
        pinCtx.noSteps = true;
        rows.forEach((e) => box.append(entryRow(e, pinCtx)));
      } else {
        const strip = el("div", "hx-pinchips");
        rows.slice(0, PIN_SHOWN).forEach((e) => {
          const c = btn(`hx-pinchip t-${TONE[e.status] || "none"}`, null, () => pick(e.id), `${e.status}: ${e.title}`);
          c.append(pill(e.status), el("span", "hx-pinchip-t", e.title || "Untitled"));
          strip.append(c);
        });
        box.append(strip);
      }
      if (rows.length > PIN_SHOWN || st.pinAll) box.append(btn("hx-lk", st.pinAll ? "Show fewer" : `Show all ${rows.length}`, () => {
        st.pinAll = !st.pinAll; drawPinned();
      }));
      if (p.more) box.append(btn("hx-lk", `${p.total - p.shown} more unfinished: list them`, () => {
        st.f.status = UNFINISHED.slice(); st.sel = null; filtersChanged();
      }));
      pinned.replaceChildren(head, box);
    }

    // ----- the map
    let geo = null;     // the last drawing's geometry, for selecting by pointer
    function drawMap() {
      if (narrow()) { drawStrip(); return; }
      const map = st.map;
      if (!map) { mapBox.replaceChildren(el("p", st.error ? "hx-err" : "hx-dim", st.error || "Reading the time map...")); return; }
      const [T0, T1] = period(st.zoom, st.anchor);
      const W = Math.max(NARROW, mapBox.clientWidth || 960);
      const X0 = LBL_W, X1 = W - 14;
      const x = (t) => X0 + (Math.min(Math.max(t, T0), T1) - T0) / (T1 - T0) * (X1 - X0);
      const tAt = (px) => T0 + (Math.min(Math.max(px, X0), X1) - X0) / (X1 - X0) * (T1 - T0);
      const now = Date.now();
      const svg = sv("svg", { class: "hx-svg", width: W, role: "img", "aria-label": `Time map, ${spanLabel(T0, T1)}` });
      const bg = sv("g", { class: "hx-bgcols" });
      const axis = sv("g", { class: "hx-axis" });
      const fg = sv("g", { class: "hx-items" });
      svg.append(bg, axis, fg);
      const lanesG = sv("g", { class: "hx-lanes" });
      fg.append(lanesG);
      // Columns: days (week, month) or hours (day).
      const cols = [];
      if (st.zoom === "day") for (let h = 0; h < 24; h++) cols.push([T0 + h * HOUR, T0 + (h + 1) * HOUR]);
      else for (let d = T0; d < T1; d = addDays(d, 1)) cols.push([d, addDays(d, 1)]);
      const AX = 24;
      cols.forEach(([a, b], i) => {
        const xa = x(a), xb = x(b);
        const wd = new Date(a).getDay();
        let label;
        if (st.zoom === "day") label = i % (W < 900 ? 3 : 2) === 0 ? new Date(a).toLocaleTimeString("en-US", { hour: "numeric" }) : "";
        else if (st.zoom === "week") label = fmtDay(a);
        else label = new Date(a).getDate() === 1 || i === 0 ? fmtShort(a) : String(new Date(a).getDate());
        const selOn = st.sel && a >= st.sel.a && b <= st.sel.b;
        bg.append(sv("rect", { x: xa, y: 0, width: Math.max(0, xb - xa), height: "100%",
          class: `hx-col${(wd === 0 || wd === 6) && st.zoom !== "day" ? " wkend" : ""}${selOn ? " sel" : ""}`, "data-day": ymd(a) }));
        bg.append(sv("line", { x1: xa, x2: xa, y1: 0, y2: "100%", class: "hx-grid" }));
        if (label) axis.append(sv("text", { x: st.zoom === "month" ? (xa + xb) / 2 : st.zoom === "day" ? xa + 2 : (xa + xb) / 2, y: 15,
          "text-anchor": st.zoom === "day" ? "start" : "middle", class: `hx-axt${dayStart(a) === dayStart(now) && st.zoom !== "day" ? " today" : ""}` }, label));
      });
      let y = AX + 6;
      const hitCls = (item) => {
        const tid = item.trail || item.id;
        let fade = false, hit = false;
        if (st.hits) { if (st.hits.has(tid)) hit = true; else fade = true; }
        if (st.rules) { if (st.rules.sessions.has(item.id) || st.rules.trails.has(tid)) hit = true; else fade = true; }
        if (st.picked && (st.picked === item.id || st.picked === tid)) hit = true;
        return `${hit ? " hit" : ""}${fade && !hit ? " fade" : ""}`;
      };
      const lanes = (map.lanes || []).filter((ln) => (st.layers.sessions && (ln.spans || []).length)
        || (st.layers.merges && (ln.points || []).length));
      const items = [];       // drawn items with their trail, for counting matches
      lanes.forEach((ln, li) => {
        const top = y;
        const g = sv("g", { class: "hx-lane", "data-lane": ln.key });
        const spans = st.layers.sessions ? (ln.spans || []) : [];
        const points = st.layers.merges ? (ln.points || []) : [];
        const live = spans.filter((s) => UNFINISHED.includes(s.status) && s.status !== "Not merged");
        // The catch-all lane (no folder, no module) is drawn last and folded
        // to one row; its label opens and closes it instead of filtering.
        const catchAll = !!ln.catch_all;
        const folded = catchAll && !st.catchOpen;
        const lab = sv("g", catchAll ? { class: "hx-lanelab fold", "data-fold": "1" } : { class: "hx-lanelab", "data-lanekey": ln.key });
        const name = ln.label.length > 18 ? ln.label.slice(0, 17) + "..." : ln.label;
        lab.append(svTitle(sv("text", { x: 6, y: top + 11, class: "hx-ln" }, catchAll ? `${name} ${folded ? "+" : "-"}` : name),
          catchAll ? `${ln.label}: work not tied to one ${st.lane}. Click to ${folded ? "show all its rows" : "fold it to one row"}`
            : `${ln.label}: click to filter to this ${st.lane}`));
        // counts one per line, so they stay inside the label column
        if (spans.length) lab.append(sv("text", { x: 6, y: top + 24, class: "hx-lnsub" }, plural(spans.length, "session")));
        if (points.length) lab.append(sv("text", { x: 6, y: top + (spans.length ? 36 : 24), class: "hx-lnsub" }, plural(points.length, "merge")));
        g.append(lab);
        let laneH;
        if (st.zoom === "month") {
          // A calendar of days: each cell shaded by how long sessions were open, merges counted in it.
          const cellTop = top + 2, cellH = 26;
          cols.forEach(([a, b]) => {
            const ss = spans.filter((s) => s._a < b && s._b >= a);
            const ms = points.filter((p) => p._a >= a && p._a < b);
            const hrs = ss.reduce((t, s) => t + (Math.min(s._b, b) - Math.max(s._a, a)) / HOUR, 0);
            const lvl = !ss.length ? 0 : hrs < 2 ? 1 : hrs < 8 ? 2 : hrs < 24 ? 3 : 4;
            const xa = x(a) + 1.5, w = Math.max(2, x(b) - x(a) - 3);
            if (st.layers.sessions) {
              const r = sv("rect", { x: xa, y: cellTop, width: w, height: cellH, rx: 3, class: `hx-cell l${lvl}`, "data-day": ymd(a) });
              svTitle(r, `${fmtDay(a)}: ${plural(ss.length, "session")}, ${Math.round(hrs)} h open${ms.length ? `, ${plural(ms.length, "merge")}` : ""}`);
              g.append(r);
            }
            if (ms.length) {
              const cx = xa + w / 2, cy = cellTop + cellH / 2;
              const dm = sv("path", { d: `M${cx} ${cy - 6} L${cx + 6} ${cy} L${cx} ${cy + 6} L${cx - 6} ${cy} Z`,
                class: `hx-merge${hitCls(ms[0])}`, "data-id": ms[0].id, "data-trail": ms[0].trail || "" });
              svTitle(dm, ms.map((m) => `${m.title}${m.prs && m.prs.length ? ` (${prText(m.prs)})` : ""}`).join("\n"));
              g.append(dm);
              if (ms.length > 1) g.append(sv("text", { x: cx + 8, y: cy + 4, class: "hx-cnt" }, String(ms.length)));
              ms.forEach((m) => items.push(m));
            }
            ss.forEach((s) => items.push(s));
          });
          laneH = cellH + 8;
        } else {
          const packed = packRows(spans.map((s) => s), folded ? 1 : MAX_ROWS);
          const rowsTop = top + 4;
          packed.placed.forEach((s) => {
            const xa = x(s._a), xb = Math.max(xa + 2.5, x(s._b));
            const yy = rowsTop + s._r * (ROW_H + ROW_GAP);
            const ak = appKey(s.app);
            const sg = sv("g", { class: `hx-bar a-${ak} t-${TONE[s.status] || "none"}${hitCls(s)}`, "data-id": s.id, "data-trail": s.trail || "" });
            sg.append(sv("rect", { x: xa, y: yy, width: xb - xa, height: ROW_H, rx: 2, class: "hx-open" }));
            // Active time drawn solid inside the time the session was open.
            const act = Math.min(s._b - s._a, (s.active_s || 0) * 1000);
            if (act > 0) sg.append(sv("rect", { x: xa, y: yy, width: Math.max(1.5, x(s._a + act) - xa), height: ROW_H, rx: 2, class: "hx-active" }));
            if (s.status === "Running" || s.status === "Waiting on you") sg.append(sv("circle", { cx: xb, cy: yy + ROW_H / 2, r: 3.5, class: "hx-cap" }));
            svTitle(sg, `${s.title}\n${APP_LABEL[ak]} / ${s.status}\n${fmtWhen(s._a)} to ${s.status === "Running" ? "now" : fmtWhen(s._b)}`
              + `${s.active_s ? `\n${fmtDur(s.active_s)} active of ${fmtDur((s._b - s._a) / 1000)} open` : ""}`);
            g.append(sg);
            items.push(s);
          });
          const rows = Math.max(packed.rows, 1);
          const my = rowsTop + rows * (ROW_H + ROW_GAP) + 7;
          points.forEach((p) => {
            const cx = x(p._a);
            const dm = sv("path", { d: `M${cx} ${my - 5} L${cx + 5} ${my} L${cx} ${my + 5} L${cx - 5} ${my} Z`,
              class: `hx-merge${hitCls(p)}`, "data-id": p.id, "data-trail": p.trail || "" });
            svTitle(dm, `${p.title}${p.prs && p.prs.length ? `\n${prText(p.prs)}` : ""}\nMerged into main ${fmtWhen(p._a)}\n+${p.added || 0} -${p.removed || 0}`);
            g.append(dm);
            items.push(p);
          });
          if (packed.hidden) g.append(sv("text", { x: X1, y: rowsTop + rows * (ROW_H + ROW_GAP) - 1, "text-anchor": "end", class: "hx-cnt" },
            `+${packed.hidden} more`));
          laneH = Math.max(spans.length && points.length ? 42 : 30, (points.length ? my + 8 : my - 4) - top);
        }
        // Running and waiting work marked at the right edge.
        if (live.length && now >= T0) {
          const ex = Math.min(x(now), X1);
          const waiting = live.filter((s) => s.status === "Waiting on you").length;
          const mk = sv("g", { class: `hx-edge${waiting ? " wait" : " live"}` });
          mk.append(sv("rect", { x: X1 + 2, y: top + 2, width: 6, height: laneH - 6, rx: 2 }));
          svTitle(mk, live.map((s) => `${s.status}: ${s.title}`).join("\n"));
          g.append(mk);
          if (ex < X1) g.append(sv("line", { x1: ex, x2: ex, y1: top, y2: top + laneH - 4, class: "hx-nowtick" }));
        }
        lanesG.append(g);
        y = top + laneH + 4;
        lanesG.append(sv("line", { x1: 0, x2: X1, y1: y - 2, y2: y - 2, class: "hx-sep" }));
      });
      if (!lanes.length) {
        lanesG.append(sv("text", { x: X0 + 8, y: y + 14, class: "hx-lnsub" },
          st.layers.sessions || st.layers.merges ? "Nothing recorded in this period." : "Sessions and merges are hidden."));
        y += 26;
      }
      // The Automations lane.
      if (st.layers.automations) {
        const top = y + 2;
        const ticks = allTicks(map).filter((t) => t._a >= T0 && t._a < T1);
        const missed = missedSlots(st.grid).filter((m) => m.a >= T0 && m.a < T1);
        const failed = ticks.filter((t) => t.status === "Failed").length;
        const g = sv("g", { class: "hx-lane hx-auto", "data-lane": "Automations" });
        const lab = sv("g", { class: "hx-lanelab auto", "data-auto": "1" });
        lab.append(svTitle(sv("text", { x: 6, y: top + 11, class: "hx-ln" }, `Automations ${st.gridOpen ? "-" : "+"}`), "Open the status grid for this span"));
        lab.append(sv("text", { x: 6, y: top + 24, class: "hx-lnsub" },
          [plural(ticks.length, "run"), failed ? `${failed} failed` : "", missed.length ? `${missed.length} missed` : ""].filter(Boolean).join(", ")));
        g.append(lab);
        const th = 14, ty = top + 4;
        if (st.zoom === "month") {
          cols.forEach(([a, b]) => {
            const ds = ticks.filter((t) => t._a >= a && t._a < b);
            const fl = ds.filter((t) => t.status === "Failed").length;
            const ms = missed.filter((m) => m.a >= a && m.a < b).length;
            const xa = x(a) + 1.5, w = Math.max(2, x(b) - x(a) - 3);
            const r = sv("rect", { x: xa, y: ty, width: w, height: th + 6, rx: 3, "data-day": ymd(a),
              class: `hx-acell${fl ? " fail" : ds.length ? " ok" : ""}${ms ? " missed" : ""}` });
            svTitle(r, `${fmtDay(a)}: ${plural(ds.length, "run")}${fl ? `, ${fl} failed` : ""}${ms ? `, ${ms} missed` : ""}`);
            g.append(r);
          });
        } else {
          missed.forEach((m) => {
            const xa = x(m.a), w = Math.max(2, x(m.b) - xa);
            g.append(svTitle(sv("rect", { x: xa, y: ty + th + 3, width: w, height: 4, class: "hx-miss" }),
              `Missed: ${m.name}, ${fmtWhen(m.a)}`));
          });
          ticks.forEach((t) => {
            const xa = x(t._a), w = Math.max(st.zoom === "day" ? 2.5 : 1.5, x(t._b) - xa);
            const r = sv("rect", { x: xa, y: ty, width: w, height: th, class: `hx-tick t-${TONE[t.status] || "none"}${hitCls(t)}`,
              "data-id": t.id, "data-trail": t.trail || "" });
            svTitle(r, `${t.title}\n${t.status} / ${fmtWhen(t._a)}`);
            g.append(r);
            items.push(t);
          });
        }
        lanesG.append(g);
        y = top + th + 16;
      }
      // Now.
      if (now >= T0 && now < T1) {
        const nx = x(now);
        fg.append(sv("line", { x1: nx, x2: nx, y1: AX - 4, y2: y, class: "hx-now" }));
        fg.append(sv("text", { x: nx, y: AX - 6, "text-anchor": "middle", class: "hx-nowt" }, "now"));
      }
      // The selection.
      const selR = sv("rect", { class: "hx-selrect", y: AX - 2, height: Math.max(0, y - AX + 2), x: 0, width: 0 });
      if (st.sel && st.sel.b > T0 && st.sel.a < T1) {
        selR.setAttribute("x", x(st.sel.a));
        selR.setAttribute("width", Math.max(1, x(st.sel.b) - x(st.sel.a)));
      } else selR.setAttribute("visibility", "hidden");
      fg.append(selR);
      const H = y + 8;
      svg.setAttribute("height", H);
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      // Legend.
      const leg = el("div", "hx-legend");
      [["a-vira", "Vira session"], ["a-claude", "Claude Code"], ["a-codex", "Codex"]].forEach(([c, t]) => {
        const s = el("span", "hx-key");
        s.append(el("i", `hx-sw ${c}`), document.createTextNode(t));
        leg.append(s);
      });
      [["hx-sw act", "solid: active"], ["hx-sw dia", "merged into main"], ["hx-sw tk", "automation run"],
       ["hx-sw tk fail", "failed"], ["hx-sw gap", "missed run"]].forEach(([c, t]) => {
        const s = el("span", "hx-key");
        s.append(el("i", c), document.createTextNode(t));
        leg.append(s);
      });
      if (st.hits || st.rules) {
        const n = items.filter((it) => (st.hits && st.hits.has(it.trail || it.id)) || (st.rules && (st.rules.sessions.has(it.id) || st.rules.trails.has(it.trail)))).length;
        leg.append(el("span", "hx-hitn", `${plural(n, "match", "matches")} lit on the map`));
      }
      mapBox.replaceChildren(svg, leg);
      geo = { svg, x, tAt, T0, T1, X0, X1, selR, AX, y };
      bindMap(svg);
    }
    // Click selects a day; drag selects any range; an item opens its detail.
    function bindMap(svg) {
      let drag = null;
      const px = (ev) => {
        const r = svg.getBoundingClientRect();
        const scale = r.width ? (Number(svg.getAttribute("width")) || r.width) / r.width : 1;
        return (ev.clientX - r.left) * scale;
      };
      svg.addEventListener("pointerdown", (ev) => {
        if (ev.button != null && ev.button !== 0) return;
        const t = ev.target;
        if (t.closest && (t.closest("[data-id]") || t.closest(".hx-lanelab"))) return;
        drag = { x0: px(ev), x1: px(ev) };
        try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* not capturable */ }
      });
      svg.addEventListener("pointermove", (ev) => {
        if (!drag || !geo) return;
        drag.x1 = px(ev);
        const a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1);
        if (b - a > 4) {
          geo.selR.removeAttribute("visibility");
          geo.selR.setAttribute("x", Math.max(a, geo.X0));
          geo.selR.setAttribute("width", Math.max(0, Math.min(b, geo.X1) - Math.max(a, geo.X0)));
        }
      });
      svg.addEventListener("pointerup", (ev) => {
        if (!drag || !geo) return;
        const d = drag;
        drag = null;
        const a = Math.min(d.x0, d.x1), b = Math.max(d.x0, d.x1);
        if (b - a <= 4) {
          if (a < geo.X0) return;
          const day = dayStart(geo.tAt(a));
          selectSpan(day, addDays(day, 1));
          return;
        }
        const snap = st.zoom === "month" ? DAY : st.zoom === "week" ? HOUR : 15 * MIN;
        let ta = geo.tAt(a), tb = geo.tAt(b);
        if (snap === DAY) { ta = dayStart(ta); tb = addDays(dayStart(tb - 1), 1); }
        else { ta = Math.floor(ta / snap) * snap; tb = Math.ceil(tb / snap) * snap; }
        selectSpan(ta, tb);
      });
      svg.addEventListener("click", (ev) => {
        const t = ev.target;
        const item = t.closest && t.closest("[data-id]");
        if (item) { pick(item.getAttribute("data-id")); return; }
        const lab = t.closest && t.closest(".hx-lanelab");
        if (lab && lab.getAttribute("data-fold")) { st.catchOpen = !st.catchOpen; drawMap(); return; }
        if (lab && lab.getAttribute("data-auto")) { st.gridOpen = !st.gridOpen; gridBox.hidden = !st.gridOpen; if (st.gridOpen) drawGrid(); drawMap(); return; }
        if (lab) {
          const key = lab.getAttribute("data-lanekey");
          if (st.lane === "project") st.f.project = st.f.project === key ? "" : key;
          else st.f.module = st.f.module === key ? "" : key;
          filtersChanged();
          loadMap();
        }
      });
    }
    function selectSpan(a, b) {
      st.sel = st.sel && st.sel.a === a && st.sel.b === b ? null : { a, b };
      if (st.ask) st.ask.span = !!st.sel && st.ask.span;
      drawMap();
      renderAskChips();
      if (st.gridOpen) drawGrid();
      loadList();
    }

    // ----- narrow screens: a day strip over the list
    function drawStrip() {
      const [T0, T1] = period(st.zoom === "day" ? "week" : st.zoom === "month" ? "week" : st.zoom, st.anchor);
      const days = dayCounts(st.map, st.grid, T0, T1);
      const strip = el("div", "hx-strip");
      strip.setAttribute("role", "group");
      strip.setAttribute("aria-label", "Days");
      days.forEach((d) => {
        const on = st.sel && st.sel.a === d.a && st.sel.b === d.b;
        const b = btn(`hx-sday${on ? " on" : ""}${d.failed ? " fail" : ""}${d.live ? " live" : ""}`, null, () => selectSpan(d.a, d.b), fmtDay(d.a));
        b.append(el("b", null, new Date(d.a).toLocaleDateString("en-US", { weekday: "short" })),
          el("span", null, String(new Date(d.a).getDate())),
          el("i", "hx-sm", d.merges ? `${d.merges} merged` : ""),
          el("i", "hx-ss", d.sessions ? plural(d.sessions, "session") : ""),
          el("i", `hx-sa${d.failed ? " fail" : ""}`, d.runs ? `${d.runs} runs${d.failed ? `, ${d.failed} failed` : ""}${d.missed ? `, ${d.missed} missed` : ""}` : ""));
        strip.append(b);
      });
      mapBox.replaceChildren(strip);
      geo = null;
    }

    // ----- the full status grid under the Automations lane
    async function drawGrid() {
      const [pa, pb] = period(st.zoom, st.anchor);
      const a = st.sel ? st.sel.a : pa, b = st.sel ? st.sel.b : pb;
      const span = b - a <= 2 * DAY ? "hour" : "day";
      gridBox.replaceChildren(el("p", "hx-dim", "Reading the status grid..."));
      let g;
      try {
        g = await getJSON(`/api/history/grid?${qs({ span, since: span === "day" ? ymd(a) : new Date(a).toISOString(),
          until: span === "day" ? ymd(b - 1) : new Date(b).toISOString(), project: st.f.project })}`);
      } catch (err) { gridBox.replaceChildren(el("p", "hx-err", `Could not read the grid: ${err.message || err}`)); return; }
      gridBox.replaceChildren(renderGrid(g, { pick, onClose: () => { st.gridOpen = false; gridBox.hidden = true; drawMap(); },
        title: `Automations, ${spanLabel(a, b)}` }));
    }

    // ----- the list
    function drawListHead(out) {
      const [pa, pb] = period(st.zoom, st.anchor);
      const a = st.sel ? st.sel.a : pa, b = st.sel ? st.sel.b : pb;
      const lab = el("div", "hx-selected");
      lab.append(el("b", null, st.f.rules ? "Sessions that broke a rule" : st.sel ? `Selected: ${spanLabel(a, b)}` : `All of ${spanLabel(a, b)}`));
      if (st.sel) lab.append(btn("hx-lk", "Clear selection", () => selectSpan(st.sel.a, st.sel.b)));
      if (out && out.statuses) {
        const c = Object.entries(out.statuses).filter(([, n]) => n).map(([s, n]) => `${n} ${s.toLowerCase()}`);
        lab.append(el("span", "hx-dim", `${plural(out.total, "entry", "entries")}${c.length ? ": " + c.join(", ") : ""}`
          + (out.unstarted_left_out ? `; ${plural(out.unstarted_left_out, "idea")} not started left out` : "")));
      }
      const parts = [lab];
      if (!st.f.rules) {
        const gb = el("div", "hx-groupby");
        gb.append(el("span", "hx-lbl", "Group by"), seg("Group by", GROUPS, st.group, (g) => { st.group = g; persist({ group: g }); loadList(); }));
        parts.push(gb);
      }
      listHead.replaceChildren(...parts);
    }
    function drawList() {
      const out = st.list;
      drawListHead(out);
      if (!out) return;
      const frag = [];
      if (!out.groups.length) frag.push(el("p", "hx-dim", "Nothing in this span matches."));
      out.groups.forEach((g) => {
        const sec = el("section", `hx-group${g.key === "now" ? " now" : ""}`);
        const h = el("h4", "hx-gh");
        h.append(el("span", null, g.label || g.key || "No name"), el("span", "hx-dim", String(g.count)));
        sec.append(h);
        g.entries.forEach((e) => sec.append(entryRow(e, ctx)));
        frag.push(sec);
      });
      if (out.more) frag.push(btn("hx-btn", "Show more", () => { st.offset = (st.offset || 0) + 200; loadList(true); }));
      list.replaceChildren(...frag);
    }
    // The rule filter's list: each broken rule with its sessions, and its own actions.
    function drawRules() {
      drawListHead(null);
      const r = st.rules || { items: [] };
      if (r.error) { list.replaceChildren(el("p", "hx-err", `Could not read the broken rules: ${r.error}`)); return; }
      if (!r.items.length) { list.replaceChildren(el("p", "hx-dim", "No session has broken a rule on record.")); return; }
      list.replaceChildren(...r.items.map((it) => ruleRow(it, ctx, () => { loadRules().then(() => { drawRules(); drawMap(); }); })));
    }

    // ----- the detail
    async function loadDetail(id, quiet) {
      if (!quiet) {
        aside.hidden = false;
        aside.replaceChildren(el("p", "hx-dim", "Reading..."));
        // above the map in a narrow window: bring it into view
        if (!root.classList.contains("hx-wide") && aside.scrollIntoView) aside.scrollIntoView({ block: "start", behavior: "smooth" });
      }
      try {
        const d = await getJSON(`/api/history/entry?${qs({ id })}`);
        if (st.picked !== id) return;
        st.detail = d;
        renderDetail(aside, d, ctx);
        aside.hidden = false;
        root.classList.add("has-detail");
      } catch (err) {
        if (st.picked !== id) return;
        aside.replaceChildren(btn("hx-x", "Close", closeDetail), el("p", "hx-err", `Could not read this entry: ${err.message || err}`));
      }
    }
    function pick(id) {
      st.picked = id;
      list.querySelectorAll(".hx-row.on").forEach((n) => n.classList.remove("on"));
      list.querySelectorAll(`.hx-row[data-id="${typeof CSS !== "undefined" && CSS.escape ? CSS.escape(id) : String(id).replace(/"/g, '\\"')}"]`).forEach((n) => n.classList.add("on"));
      loadDetail(id);
      drawMap();
    }
    function closeDetail() {
      st.picked = null; st.detail = null;
      aside.hidden = true;
      root.classList.remove("has-detail");
      drawMap();
    }
    const ctx = {
      get picked() { return st.picked; },
      pick, close: closeDetail,
      act: (a, box) => runAction(a, box, () => view.refresh()),
      refresh: () => view.refresh(),
      filterModule: (m) => { st.f.module = m; filtersChanged(); loadMap(); },
      filterProject: (p) => { st.f.project = p; filtersChanged(); loadMap(); },
    };

    // ----- the plain-words question
    async function ask(q) {
      q = String(q || "").trim();
      if (!q) return;
      askChips.replaceChildren(el("span", "hx-asknote", "Reading the question..."));
      let r;
      try { r = await postJSON("/api/history/ask", { q }); }
      catch (err) { askChips.replaceChildren(el("span", "hx-err", `Could not ask: ${err.message || err}`)); return; }
      const d = r.data || {};
      if (!r.ok) { askChips.replaceChildren(el("span", "hx-err", typeof d.detail === "string" ? d.detail : `Ask failed (${r.status})`)); return; }
      const spec = d.spec || d.fallback;
      if (!spec) { askChips.replaceChildren(el("span", "hx-err", "The question could not be read.")); return; }
      const note = d.spec ? "" : d.status === "model_unavailable" ? "No model answered; read from the words alone."
        : "The model's answer could not be used; read from the words alone.";
      await applySpec(spec, { q, note });
    }
    async function applySpec(spec, meta) {
      const f = spec.filters || {};
      st.f = { project: f.project || "", module: f.module || "",
               status: (f.status || []).filter((s) => STATUSES.includes(s)), kind: f.kind || [], q: f.q || "", rules: false };
      st.ask = { q: meta.q, note: meta.note || "", view: spec.view, span: !!(f.since || f.until) };
      if (spec.group && GROUPS.some(([k]) => k === spec.group)) st.group = spec.group;
      if (spec.lane === "module" || spec.lane === "project") st.lane = spec.lane;
      st.gridOpen = spec.view === "grid";
      gridBox.hidden = !st.gridOpen;
      st.picked = null; aside.hidden = true; root.classList.remove("has-detail");
      if (f.since || f.until) {
        const a = fromYmd(f.since) || fromYmd(f.until);
        const b = addDays(fromYmd(f.until) || fromYmd(f.since), 1);
        st.sel = { a, b };
        Object.assign(st, fitZoom(a, b));
      } else {
        // No dates in the question: fit the span to where the matches are.
        st.sel = null;
        try {
          const out = await getJSON(`/api/history?${qs(listParams({ group: "none", automations: 1, limit: 1000 }))}`);
          const rows = (out.groups || []).flatMap((g) => g.entries || []);
          if (rows.length) {
            const lo = Math.min(...rows.map((e) => ts(e.start || e.at) || ts(e.at)));
            const hi = Math.max(...rows.map((e) => ts(e.at)));
            const a = dayStart(lo), b = addDays(dayStart(hi), 1);
            if (b - a <= 30 * DAY) { st.sel = { a, b }; st.ask.span = true; }
            Object.assign(st, fitZoom(Math.max(a, b - 30 * DAY), b));
          }
        } catch (err) { /* the map keeps its period */ }
      }
      persist({ group: st.group, lane: st.lane, zoom: st.zoom });
      renderBar();
      renderFilters();
      renderAskChips();
      await Promise.all([loadMap(), loadHits()]);
      drawMap();
      if (st.gridOpen) drawGrid();
      loadList();
    }

    // ----- startup and resize
    // The detail sits beside the map only when there is room for both;
    // in a narrower window it sits above the list. The map follows its own
    // box, which the detail opening or closing resizes.
    const fitWidth = () => root.classList.toggle("hx-wide", (root.clientWidth || 0) >= WIDE);
    fitWidth();
    if (typeof ResizeObserver === "function") {
      let lastW = 0;
      new ResizeObserver(() => {
        fitWidth();
        const w = mapBox.clientWidth;
        if (Math.abs(w - lastW) < 8) return;
        lastW = w;
        drawMap();
      }).observe(mapBox);
      new ResizeObserver(fitWidth).observe(root);
    }
    renderBar();
    renderFilters();
    renderAskChips();
    loadFacets();
    loadPinned();
    reload();
    Live.add(view);

    // What other screens open History with: a day, a question, an entry.
    const api = {
      root, state: st, refresh: view.refresh, pick, ask, applySpec, selectSpan,
      open(o) {
        o = o || {};
        if (o.day) { const a = fromYmd(o.day); if (a) { st.anchor = Math.max(st.anchor, a); selectSpan(a, addDays(a, 1)); } }
        if (o.q) { askIn.value = o.q; ask(o.q); }
        if (o.module || o.project) {
          // a module window's "Open in Fleet History": its lanes, filtered to it
          if (o.module) { st.f.module = o.module; st.lane = "module"; }
          if (o.project) st.f.project = o.project;
          filtersChanged();
          reload();
        }
        if (o.rules) { st.f.rules = true; filtersChanged(); }
        if (o.status) { st.f.status = [].concat(o.status).filter((s) => STATUSES.includes(s)); filtersChanged(); }
        if (o.entry) pick(o.entry);
        if (o.grid) { st.gridOpen = true; gridBox.hidden = false; drawGrid(); drawMap(); }
      },
      destroy() { Live.remove(view); },
    };
    return api;
  }

  // ---------- the automation status grid (Time map and module views) ----------
  function renderGrid(g, ctx) {
    const box = el("div", "hx-grid");
    const head = el("div", "hx-gridhead");
    head.append(el("b", null, ctx.title || "Automations"),
      el("span", "hx-dim", `${plural((g.rows || []).length, "automation")} / one cell per ${g.span === "hour" ? "hour" : "day"}`));
    if (ctx.onClose) head.append(btn("hx-lk", "Close grid", ctx.onClose));
    box.append(head);
    const rows = g.rows || [];
    if (!rows.length) { box.append(el("p", "hx-dim", "No automation ran in this span.")); return box; }
    const tbl = el("div", "hx-gtable");
    tbl.style.setProperty("--cols", String((g.columns || []).length));
    const hr = el("div", "hx-grow hx-ghead");
    hr.append(el("span", "hx-gname", ""));
    const cells = el("span", "hx-gcells");
    (g.columns || []).forEach((c, i) => {
      const n = g.columns.length;
      const show = g.span === "day" || n <= 24 || i % Math.ceil(n / 24) === 0;
      cells.append(el("i", "hx-gcol", show ? c.label : ""));
    });
    hr.append(cells, el("span", "hx-gsum", ""));
    tbl.append(hr);
    rows.forEach((r) => {
      const row = el("div", "hx-grow");
      const name = el("span", "hx-gname", r.name);
      name.title = `${r.name}${r.project ? ` (${r.project})` : ""}${(r.schedules || []).length ? `\n${r.schedules.map((s) => s.name).join(", ")}` : "\nNo schedule on record"}\nKnown since ${fmtWhen(ts(r.known_since))}`;
      const cs = el("span", "hx-gcells");
      (r.cells || []).forEach((c) => {
        const cell = el("button", `hx-gc g-${c.tone}`);
        cell.type = "button";
        const runs = c.runs || [];
        cell.title = `${r.name}, ${fmtWhen(ts(c.start))}: ${c.outcome === "missed" ? "missed a scheduled run" : c.outcome === "none" ? "nothing due" : `${plural(runs.length, "run")}, ${c.outcome}`}`
          + `${Object.keys(c.counts || {}).length ? "\n" + Object.entries(c.counts).map(([k, v]) => `${v} ${k}`).join(", ") : ""}`;
        cell.setAttribute("aria-label", cell.title);
        if (runs.length) cell.addEventListener("click", () => ctx.pick(runs[runs.length - 1].id));
        else cell.disabled = true;
        if (runs.length > 1) cell.textContent = String(runs.length);
        cs.append(cell);
      });
      row.append(name, cs, el("span", "hx-gsum", [plural(r.runs, "run"), r.failed ? `${r.failed} failed` : "", r.missed ? `${r.missed} missed` : ""].filter(Boolean).join(", ")));
      tbl.append(row);
    });
    box.append(tbl);
    return box;
  }

  // ---------- one broken rule: its sessions, dismiss, propose a guard ----------
  function ruleRow(it, ctx, after) {
    const row = el("article", "hx-row hx-rule");
    const head = el("div", "hx-row-head");
    head.append(el("span", "hx-st t-fail", "Broke a rule"), el("span", "hx-row-title static", it.rule || it.title || "A rule"));
    row.append(head);
    row.append(el("div", "hx-row-meta", [it.project, it.when ? fmtWhen(ts(it.when)) : "", it.count > 1 ? `${it.count} times` : ""].filter(Boolean).join(" / ")));
    if (it.evidence) row.append(el("p", "hx-quote", it.evidence));
    (it.sessions || []).forEach((s) => {
      const line = el("div", "hx-chiprow");
      line.append(btn("hx-chip", s.title || "Session", () => ctx.pick(s.id), "Open the session's entry"),
        el("span", "hx-dim", s.at ? fmtWhen(ts(s.at)) : ""));
      (s.actions || []).forEach((a) => line.append(btn("hx-lk", a.label, () => runAction(a, line, after), a.label)));
      if (s.quote) line.append(el("span", "hx-quote", s.quote));
      row.append(line);
    });
    const acts = el("div", "hx-row-acts");
    (it.actions || []).forEach((a) => acts.append(btn(`hx-btn${a.action === "guard" ? " pri" : ""}`, a.label, () => runAction(a, acts, after), a.label)));
    if (acts.childNodes.length) row.append(acts);
    return row;
  }

  // =====================================================================
  // The History view inside a module window and on an idea card.
  // =====================================================================
  // opts: {module} | {idea} | {project} | {ref}; title for the heading.
  function mountFor(root, opts) {
    opts = opts || {};
    const st = { mergesOnly: false, data: null, lane: null, picked: null, error: "" };
    const view = { stale: false, transient: true, root, visible: () => !!root.isConnected && root.offsetParent !== null };
    let soon = null;
    view.refreshSoon = () => { clearTimeout(soon); soon = setTimeout(() => view.refresh(), 800); };
    root.classList.add("hx-for");
    root.replaceChildren();
    const head = el("div", "hx-forhead");
    const laneBox = el("div", "hx-forlane");
    const listBox = el("div", "hx-forlist");
    // opts.detailHost: a panel outside this view (a module window's right
    // side) that the detail opens in; opts.onDetail(open) hears it open and close.
    const detail = opts.detailHost || el("div", "hx-fordetail");
    const showDetail = (on) => { detail.hidden = !on; if (opts.onDetail) opts.onDetail(on); };
    detail.hidden = true;
    root.append(head, laneBox, ...(opts.detailHost ? [] : [detail]), listBox);
    const today = dayStart(Date.now());
    const T0 = addDays(today, -29), T1 = addDays(today, 1);
    const whole = !!(opts.module || (opts.project && !opts.idea && !opts.ref));

    function drawHead() {
      const tog = btn(`hx-tog${st.mergesOnly ? " on" : ""}`, "Merges only", () => { st.mergesOnly = !st.mergesOnly; view.refresh(); });
      tog.setAttribute("aria-pressed", String(st.mergesOnly));
      const open = btn("hx-lk", "Open in Fleet History", () => {
        if (typeof window.openHistory !== "function") return;
        if (whole) window.openHistory({ module: opts.module, project: opts.project });
        else if (st.data && st.data.entry) window.openHistory({ entry: st.data.entry.id });
      });
      head.replaceChildren(el("b", null, opts.title || "History"),
        el("span", "hx-dim", whole ? "Last 30 days, oldest first" : "Every step, oldest first"), tog, open);
    }
    // The thing's own lane over a month: one cell per day.
    function drawLane(spans, points, ticks) {
      const W = Math.max(300, laneBox.clientWidth || 600);
      const X0 = 4, X1 = W - 4, H = 44;
      const x = (t) => X0 + (Math.min(Math.max(t, T0), T1) - T0) / (T1 - T0) * (X1 - X0);
      const svg = sv("svg", { class: "hx-svg hx-forsvg", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
                              "aria-label": "The last 30 days" });
      for (let d = T0; d < T1; d = addDays(d, 1)) {
        const b = addDays(d, 1);
        const ss = spans.filter((s) => s._a < b && s._b >= d);
        const ms = points.filter((p) => p._a >= d && p._a < b);
        const tk = ticks.filter((t) => t._a >= d && t._a < b);
        const hrs = ss.reduce((t, s) => t + (Math.min(s._b, b) - Math.max(s._a, d)) / HOUR, 0);
        const lvl = !ss.length ? 0 : hrs < 2 ? 1 : hrs < 8 ? 2 : hrs < 24 ? 3 : 4;
        const xa = x(d) + 1, w = Math.max(2, x(b) - x(d) - 2);
        const r = sv("rect", { x: xa, y: 4, width: w, height: 22, rx: 3, class: `hx-cell l${st.mergesOnly ? 0 : lvl}` });
        svTitle(r, `${fmtDay(d)}: ${plural(ss.length, "session")}${ms.length ? `, ${plural(ms.length, "merge")}` : ""}${tk.length ? `, ${plural(tk.length, "run")}` : ""}`);
        svg.append(r);
        if (ms.length) {
          const cx = xa + w / 2;
          svg.append(svTitle(sv("path", { d: `M${cx} 9 L${cx + 6} 15 L${cx} 21 L${cx - 6} 15 Z`, class: "hx-merge", "data-id": ms[0].id }),
            ms.map((m) => m.title).join("\n")));
        }
        if (tk.length && !st.mergesOnly) svg.append(sv("rect", { x: xa, y: 29, width: w, height: 4,
          class: `hx-acell${tk.some((t) => t.status === "Failed") ? " fail" : " ok"}` }));
        if (new Date(d).getDate() === 1 || d === T0) svg.append(sv("text", { x: xa, y: 42, class: "hx-lnsub" }, fmtShort(d)));
      }
      svg.addEventListener("click", (ev) => {
        const it = ev.target.closest && ev.target.closest("[data-id]");
        if (it) pickLocal(it.getAttribute("data-id"));
      });
      laneBox.replaceChildren(svg);
    }
    async function pickLocal(id) {
      st.picked = id;
      showDetail(true);
      detail.scrollTop = 0;
      detail.replaceChildren(el("p", "hx-dim", "Reading..."));
      try {
        const d = await getJSON(`/api/history/entry?${qs({ id })}`);
        if (st.picked !== id) return;
        renderDetail(detail, d, { close: () => { st.picked = null; showDetail(false); }, refresh: view.refresh });
      } catch (err) { detail.replaceChildren(el("p", "hx-err", `Could not read this entry: ${err.message || err}`)); }
    }
    const ctx = { get picked() { return st.picked; }, pick: pickLocal, act: (a, box) => runAction(a, box, () => view.refresh()) };

    async function loadWhole() {
      const p = { module: opts.module, project: opts.project, since: ymd(T0), until: ymd(today), steps: 1 };
      const [forOut, map] = await Promise.all([
        getJSON(`/api/history/for?${qs(Object.assign({}, p, st.mergesOnly ? { kind: "merged" } : {}))}`),
        getJSON(`/api/history/timemap?${qs({ since: ymd(T0), until: ymd(today), lane: opts.module ? "module" : "project", module: opts.module, project: opts.project })}`),
      ]);
      prepMap(map, Date.now());
      const mine = (map.lanes || []).filter((ln) => opts.module ? (ln.key === opts.module || String(ln.key).startsWith(opts.module + "/") || ln.key === "Automations") : true);
      const spans = mine.flatMap((ln) => ln.spans || []), points = mine.flatMap((ln) => ln.points || []);
      drawLane(dedupe(spans), dedupe(points), allTicks({ lanes: mine }));
      const rows = (forOut.list.groups || []).flatMap((g) => g.entries || []);
      const uniq = dedupe(rows).sort((a, b) => ts(a.at) - ts(b.at));
      listBox.replaceChildren(...(uniq.length ? uniq.map((e) => entryRow(e, ctx)) : [el("p", "hx-dim", st.mergesOnly ? "No merges into main in the last 30 days." : "Nothing recorded in the last 30 days.")]));
    }
    async function loadOne() {
      const p = opts.idea ? { idea: opts.idea } : opts.ref ? { ref: opts.ref } : opts;
      let d;
      try { d = await getJSON(`/api/history/for?${qs(p)}`); }
      catch (err) {
        laneBox.replaceChildren();
        listBox.replaceChildren(el("p", "hx-dim", /no history/i.test(err.message || "") ? "No history recorded for this yet." : `Could not read its history: ${err.message || err}`));
        return;
      }
      st.data = d;
      const items = (d.trail || []).map((s) => Object.assign({}, s, { _a: ts(s.at), _b: Math.max(ts(s.end) || ts(s.at), ts(s.at)) }));
      drawLane(items.filter((s) => s.kind === "session"), items.filter((s) => s.kind === "code" && s.sub === "merged"),
        items.filter((s) => s.kind === "run"));
      const e = d.entry || {};
      const sum = el("div", "hx-forsum");
      sum.append(pill(e.status), el("b", null, e.title || ""), el("span", "hx-dim", metaLine(e)));
      const acts = el("div", "hx-row-acts");
      (d.actions || []).forEach((a) => acts.append(btn(`hx-btn${a.action === "merge" ? " pri" : ""}`, a.label, () => runAction(a, acts, () => view.refresh()))));
      (d.open || []).forEach((o) => acts.append(btn("hx-btn", openLabel(o), () => openThing(o))));
      const steps = items.filter((s) => !st.mergesOnly || (s.kind === "code" && s.sub === "merged"));
      const ol = el("ol", "hx-trail");
      steps.forEach((s) => {
        const li = el("li", `hx-ti k-${s.type === "event" ? s.kind : s.type} t-${TONE[s.status] || "none"}${s.sub === "merged" ? " is-merge" : ""}`);
        li.append(el("span", "hx-ti-t", s.at ? fmtWhen(ts(s.at)) : ""));
        const b = el("span", "hx-ti-b");
        b.append(el("b", null, stepLabel(s)));
        if (s.type === "event" && s.title) b.append(document.createTextNode(` ${s.title}`));
        if (s.status) b.append(el("span", "hx-ti-x", s.status));
        li.append(b);
        if (s.type === "event") { li.classList.add("click"); li.addEventListener("click", () => pickLocal(s.id)); }
        ol.append(li);
      });
      listBox.replaceChildren(sum, ...(acts.childNodes.length ? [acts] : []),
        steps.length ? ol : el("p", "hx-dim", st.mergesOnly ? "Nothing reached main yet." : "No steps recorded."));
    }
    view.refresh = async () => {
      drawHead();
      try { if (whole) await loadWhole(); else await loadOne(); }
      catch (err) { listBox.replaceChildren(el("p", "hx-err", `Could not read history: ${err.message || err}`)); }
    };
    view.refresh();
    Live.add(view);
    return { root, refresh: view.refresh, destroy: () => Live.remove(view), state: st };
  }
  const dedupe = (rows) => { const m = new Map(); rows.forEach((r) => { if (!m.has(r.id)) m.set(r.id, r); }); return [...m.values()]; };

  window.ViraHistory = { mountMap, mountFor, renderGrid, renderDetail, entryRow, STATUSES };
  if (window.__VIRA_TEST__) {
    window.__VIRA_HISTORY_TESTS__ = { period, fitZoom, packRows, prepMap, allTicks, missedSlots, dayCounts,
      spanLabel, stepLabel, metaLine, runAction, Live, ymd, fromYmd, dayStart, addDays };
  }
})();

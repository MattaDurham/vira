/* Fleet - every agent session on this machine, live.
   server/fleet.py collects Claude Code, Codex and Vira's own jobs across
   every project, plus the repository picture around them; this file owns
   the #view-fleet surface. The layout follows the Session Fleet page it
   replaced: a status strip, the questions waiting on the owner, then one
   lane per project with live sessions as tiles and closed ones as chips,
   families drawn as small trees. Beside the sessions: the timeline of
   what landed, the last 7 days, the background model calls, and the dev
   servers and Vira instances left running.

   Live: while the window is on screen it holds /api/fleet/stream open
   and the server pushes a snapshot when anything changes (a tick when
   nothing does). Off screen, or with the tab hidden, the stream closes,
   and the server's collector stops with it. The shell registers the
   window and calls loadFleet; everything else lives here. */
"use strict";

(() => {
  // Lane colours: muted, still tellable apart on the dark ground.
  const PAL = ["#7fa6c9", "#8fbf8a", "#c9a66b", "#a991c9", "#c98f8f",
               "#6fbfb6", "#c9b86b", "#c98fb4"];
  const TOOL = { claude: "Claude", codex: "Codex", vira: "Vira" };
  const STORE = "vira-fleet";
  const LIVE_FRESH_MS = 8000;      // older than this, the strip says Paused
  const DAY = 86400e3;
  const STATUS_KEY = { "Working": "working", "Needs you": "needs", "Jobs running": "jobs",
                       "Idle": "idle", "Closed": "closed" };
  const BRANCH_LABEL = { open_pr: "open PR", unmerged: "not merged", merged: "merged",
                         fresh: "no commits yet", deleted: "deleted", unknown: "unknown" };
  const STATE_LABEL = { active: "In progress", unmerged: "Not merged", done: "Done",
                        no_code: "No code", shipped: "Shipped", in_progress: "In progress",
                        stalled: "Stalled", abandoned: "Abandoned" };
  const LISTS = {
    working: { title: "Working now", test: (n) => n.live && n.status === "Working" },
    needs: { title: "Waiting on you", test: (n) => realWaiting(n) },
    jobs: { title: "Jobs running", test: (n) => n.live && n.status === "Jobs running" },
    idle: { title: "Open and idle", test: (n) => n.live && n.status === "Idle" },
    agents: { title: "Sessions with subagents running", test: (n) => n.live && n.agents.live.length > 0 },
    shells: { title: "Sessions with shell commands running", test: (n) => n.live && n.shells.length > 0 },
    closed24: { title: "Closed in the last 24 hours",
                test: (n) => !n.live && n.lastAt && now() - n.lastAt < DAY },
  };

  const S = {
    data: null, repos: null, es: null, receivedAt: 0, skew: 0, error: null,
    tool: "all", project: "", mode: "24h", tab: "sessions",
    view: null, stack: [], tl: { mode: "all", q: "", day: null },
    seenFeed: new Set(), mounted: false, root: null, ticker: null, watch: null,
    // The Analyze read: the last saved result, and a run in flight.
    ai: { read: null, running: false, startedAt: null, error: null },
  };
  const tiles = new Map(), chips = new Map();
  const projColor = new Map();

  // ---------- small helpers ----------
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const btn = (cls, text, onClick, title) => {
    const b = el("button", cls, text);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", (e) => { if (e && e.stopPropagation) e.stopPropagation(); onClick(e); });
    return b;
  };
  const ext = (cls, text, href) => {
    const a = el("a", cls, text);
    a.href = href;
    if (/^https:/.test(href)) { a.target = "_blank"; a.rel = "noopener"; }
    a.addEventListener("click", (e) => { if (e && e.stopPropagation) e.stopPropagation(); });
    return a;
  };
  const safeUrl = (u) => typeof u === "string" && /^(https:\/\/|claude:\/\/|codex:\/\/)/.test(u);
  const store = () => {
    try { return (typeof lsGet === "function" ? lsGet(STORE, {}) : {}) || {}; } catch (e) { return {}; }
  };
  const save = () => {
    try {
      if (typeof lsSet === "function") lsSet(STORE, { tool: S.tool, project: S.project, mode: S.mode, tab: S.tab });
    } catch (e) { /* storage unavailable */ }
  };

  // ---------- formatting ----------
  const now = () => Date.now() - S.skew;
  const clock = (ms) => {
    if (ms == null || ms < 0) return "-";
    const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h >= 48) return `${Math.floor(h / 24)}d ${h % 24}h`;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
  };
  const ago = (ms) => {
    if (ms == null) return "-";
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };
  const up = (ms) => {
    const s = Math.floor(Math.max(0, ms) / 1000);
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  };
  const tok = (n) => n == null ? "-" : n >= 1e6 ? `${(n / 1e6).toFixed(n % 1e6 ? 1 : 0)}M`
    : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
  const dur = (ms) => ms == null ? "" : ms < 1000 ? `${ms}ms`
    : ms < 60000 ? `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s` : clock(ms);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  const pct = (c) => c && c.used && c.limit ? Math.min(1, c.used / c.limit) : 0;
  const fuelClass = (p) => p >= 0.85 ? "crit" : p >= 0.7 ? "warn" : "";
  const prLabel = (p) => `#${p.n} ${p.state === "MERGED" ? "merged" : p.state === "OPEN" ? "open" : "closed"}`;
  const dayKey = (ms) => {
    const d = new Date(ms);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  const dayLabel = (key) => {
    const [y, m, d] = key.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  };
  const since = (node, t) => {
    delete node.dataset.ago;
    if (t) { node.dataset.since = t; node.textContent = clock(now() - t); }
    else { delete node.dataset.since; node.textContent = "-"; }
  };
  const agoAt = (node, t) => {
    delete node.dataset.since;
    if (t) { node.dataset.ago = t; node.textContent = ago(now() - t); }
    else { delete node.dataset.ago; node.textContent = "-"; }
  };

  // ---------- project colours: stable per name, no two shown projects alike ----------
  const hash = (s) => { let h = 2166136261; for (const c of String(s)) h = Math.imul(h ^ c.codePointAt(0), 16777619); return h >>> 0; };
  function assignColors(projects) {
    const used = new Set(projColor.values());
    [...projects].sort().forEach((p) => {
      if (projColor.has(p)) return;
      let i = hash(p) % PAL.length;
      for (let k = 0; k < PAL.length && used.has(i); k++) i = (i + 1) % PAL.length;
      projColor.set(p, i);
      used.add(i);
    });
  }
  const pc = (p) => PAL[projColor.get(p) ?? (hash(p) % PAL.length)];

  // ---------- pure data helpers (exposed to the test harness) ----------

  // Waiting on the owner only when the session says so right now: an open
  // question, a plan to approve, or a turn that ended blocked on them.
  function realWaiting(n) {
    return !!(n && n.live && n.question && (n.question.kind === "ask" || n.question.kind === "plan"
      || (n.question.kind === "wrap" && n.question.blocked)));
  }

  function index(d) {
    const all = [...(d.sessions || []), ...(d.nodes || [])];
    return { all, byId: new Map(all.map((n) => [n.id, n])),
             famById: new Map((d.families || []).map((f) => [f.id, f])) };
  }

  function passes(n, f) {
    if (f.tool && f.tool !== "all" && n.tool !== f.tool) return false;
    if (f.project && n.project !== f.project) return false;
    return true;
  }

  // Which sessions the lanes draw: every open one, closed ones by the
  // Closed control (linked only, last 24h, last 3 days), and the parents
  // that connect a shown session to its family.
  function visibleNodes(d, idx, f, t) {
    const cutoff = f.mode === "24h" ? DAY : f.mode === "3d" ? 3 * DAY : 0;
    const open = (d.sessions || []).filter((s) => passes(s, f));
    const openFams = new Set(open.map((s) => s.family));
    const keep = new Set(open.map((s) => s.id));
    (d.nodes || []).forEach((n) => {
      if (!passes(n, f)) return;
      if (openFams.has(n.family) || (cutoff && n.lastAt && t - n.lastAt < cutoff)) keep.add(n.id);
    });
    [...keep].forEach((id) => {
      let p = idx.byId.get(id)?.parentId;
      while (p && idx.byId.has(p) && !keep.has(p)) { keep.add(p); p = idx.byId.get(p).parentId; }
    });
    return idx.all.filter((n) => keep.has(n.id));
  }

  // Shown sessions grouped into lanes (project) and families, busiest first.
  function laneGroups(shown, idx) {
    const byFam = new Map();
    shown.forEach((n) => { const k = n.family || n.id; if (!byFam.has(k)) byFam.set(k, []); byFam.get(k).push(n); });
    const lanes = new Map();
    for (const [fid, members] of byFam) {
      const f = idx.famById.get(fid) || { id: fid, title: members[0].title, project: members[0].project,
        branches: [], branchCount: 0, merged: 0, deleted: 0, needsWork: 0, members: members.map((m) => m.id),
        kind: "session", state: "no_code" };
      const project = members[0].project || f.project;
      if (!lanes.has(project)) lanes.set(project, []);
      lanes.get(project).push({ f, members, open: members.filter((m) => m.live).length,
        lastAt: Math.max(...members.map((m) => m.lastAt || 0)) });
    }
    return [...lanes.entries()].map(([p, fams]) => ({
      p, fams: fams.sort((a, b) => b.open - a.open || b.lastAt - a.lastAt),
      open: fams.reduce((a, x) => a + x.open, 0), lastAt: Math.max(...fams.map((x) => x.lastAt)),
    })).sort((a, b) => b.open - a.open || b.lastAt - a.lastAt);
  }

  // Counts for the strip, under the current filters.
  function counts(d, f) {
    const t = { working: 0, needs: 0, jobs: 0, idle: 0, agents: 0, shells: 0 };
    (d.sessions || []).filter((s) => passes(s, f)).forEach((s) => {
      const k = STATUS_KEY[s.status];
      if (k && k in t) t[k] += 1;
      t.agents += (s.agents && s.agents.live || []).length;
      t.shells += (s.shells || []).length;
    });
    return t;
  }

  // The timeline's rows: landings and change-log entries for the shown
  // repositories, newest first, filtered by mode, search and day.
  function timelineRows(repos, f, tl) {
    const rows = [];
    (repos && repos.repos || []).forEach((r) => {
      if (f.project && r.name !== f.project) return;
      (r.landings || []).forEach((l) => rows.push({ kind: "landing", at: l.at, repo: r.name, l }));
      (r.changelog || []).forEach((c) => rows.push({ kind: "change", at: c.at, repo: r.name, c }));
      if (repos.viraRepo && r.root === repos.viraRepo) {
        (repos.viraLog || []).forEach((g) => g.entries.forEach((e) => {
          const at = Date.parse(e.ts || g.date);
          if (at) rows.push({ kind: "log", at, repo: r.name, e });
        }));
      }
    });
    const q = (tl.q || "").trim().toLowerCase();
    return rows.filter((x) => {
      if (tl.mode === "landed" && x.kind !== "landing") return false;
      if (tl.mode === "log" && x.kind === "landing") return false;
      if (tl.day && dayKey(x.at) !== tl.day) return false;
      if (!q) return true;
      const text = x.kind === "landing"
        ? `${x.l.subject} ${x.l.branch || ""} ${x.l.pr ? `#${x.l.pr.n} ${x.l.pr.title || ""}` : ""}`
        : x.kind === "change" ? `${x.c.section} ${x.c.headline} ${x.c.detail}` : `${x.e.kind} ${x.e.text}`;
      return text.toLowerCase().includes(q);
    }).sort((a, b) => b.at - a.at);
  }

  // Landings per local day for the stacked bars: {day, claude, codex, other, direct, titles}.
  function landingDays(repos, f, days, t) {
    const out = new Map();
    for (let i = days - 1; i >= 0; i--) {
      const k = dayKey(t - i * DAY);
      out.set(k, { day: k, claude: 0, codex: 0, other: 0, direct: 0, titles: [] });
    }
    (repos && repos.repos || []).forEach((r) => {
      if (f.project && r.name !== f.project) return;
      (r.landings || []).forEach((l) => {
        const d = out.get(dayKey(l.at));
        if (!d) return;
        d[l.tool in d ? l.tool : "direct"] += 1;
        d.titles.push(l.pr && l.pr.title ? `#${l.pr.n} ${l.pr.title}` : (l.subject || ""));
      });
    });
    return [...out.values()];
  }

  // The local midnight that starts the 7-day view: six days back, so the
  // seven columns are seven calendar days and today is the last.
  function weekStart(t) {
    const d = new Date(t - 6 * DAY);
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  }

  // The 7-day view: one bar per session, x as fractions of the week.
  function weekBars(week, f, t) {
    const start = weekStart(t);
    return (week || []).filter((w) => passes(w, f) && (w.end || w.start) > start).map((w) => {
      const a = Math.max(start, w.start || w.end), b = Math.min(t, Math.max(w.end || a, a + 60e3));
      return { ...w, x: (a - start) / (7 * DAY), w: Math.max(0.004, (b - a) / (7 * DAY)) };
    });
  }

  // Background calls on one scale.
  function callRows(calls, f) {
    const rows = (calls || []).filter((c) => !f.project || c.folder === f.project);
    const max = Math.max(1, ...rows.map((c) => c.week));
    return rows.map((c) => ({ ...c, todayW: c.today / max, weekW: c.week / max }));
  }

  // What the live indicator says, given how old the last frame is.
  function liveLabel(age, error) {
    if (error && age > LIVE_FRESH_MS) return { text: "Reconnecting", cls: "bad" };
    if (age < LIVE_FRESH_MS) return { text: "Live", cls: "on" };
    return { text: `Paused, updated ${ago(age)}`, cls: "" };
  }

  // ---------- the Analyze read ----------
  const aiFeature = (id) => S.ai.read && S.ai.read.features && S.ai.read.features[id];
  const aiSession = (id) => S.ai.read && S.ai.read.sessions && S.ai.read.sessions[id];
  const aiProject = (name) => S.ai.read && S.ai.read.projects && S.ai.read.projects[name];
  function aiTag() { return el("span", "fl-ai-tag", "AI"); }
  function aiNote(text) {
    if (!text) return null;
    const n = el("div", "fl-ai-note");
    n.append(aiTag(), el("span", null, text));
    return n;
  }
  async function loadAi() {
    try {
      const r = await fetch("/api/fleet/analysis");
      if (!r.ok) return;
      const body = await r.json();
      S.ai.read = body.read || null;
      if (body.running && !S.ai.running) { S.ai.running = true; S.ai.startedAt = body.startedAt; }
      renderAi();
      render();
    } catch (e) { /* the button still works */ }
  }
  async function runAi() {
    if (S.ai.running) return;
    S.ai = { ...S.ai, running: true, startedAt: Date.now(), error: null };
    renderAi();
    try {
      const r = await fetch("/api/fleet/analyze", { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || `The read failed (${r.status}).`);
      S.ai.read = body;
    } catch (e) {
      S.ai.error = (e && e.message) || "The read failed. Try again.";
    }
    S.ai.running = false;
    S.ai.startedAt = null;
    renderAi();
    render();
  }
  function renderAi() {
    const box = S.root && q1(S.root, "fl-ai");
    if (!box) return;
    const a = S.ai;
    const sig = JSON.stringify([a.running, a.startedAt, a.error, a.read && a.read.at]);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    const state = el("span", "fl-ai-state");
    state.append(aiTag(), document.createTextNode(" "));
    if (a.running) {
      state.append(document.createTextNode("Reading your sessions "));
      const t = el("b", "fl-num");
      since(t, a.startedAt);
      state.append(t);
      box.replaceChildren(state);
      return;
    }
    if (a.read && a.read.at) {
      state.append(document.createTextNode("Last read "));
      const t = el("b");
      agoAt(t, a.read.at);
      state.append(t);
      if (a.read.dropped) state.append(document.createTextNode(` (${plural(a.read.dropped, "older feature")} left out to fit)`));
    } else state.append(document.createTextNode("No AI read yet"));
    const parts = [state];
    if (a.error) parts.push(el("span", "fl-ai-err", a.error));
    parts.push(btn("fl-ai-btn", a.read && a.read.at ? "Analyze again" : "Analyze", runAi,
      "One model call reads every project, feature and session shown and writes a plain status for each. Uses your model usage; it never runs by itself."));
    box.replaceChildren(...parts);
  }

  // ---------- navigation: every view is a key; Back walks the trail ----------
  const viewKind = (k) => !k ? "home" : (k.match(/^(fam|proj|list|srv|inst):/) || [null, "session"])[1];
  function viewLabel(k) {
    const d = S.data;
    if (!k) return "All sessions";
    if (!d) return "previous";
    const idx = index(d), kind = viewKind(k), rest = k.slice(k.indexOf(":") + 1);
    if (kind === "session") return idx.byId.get(k)?.title || "session";
    if (kind === "fam") return idx.famById.get(rest)?.title || "feature";
    if (kind === "proj") return rest;
    if (kind === "srv") return `PID ${rest}`;
    if (kind === "inst") return `:${rest}`;
    return LISTS[rest]?.title || "list";
  }
  function go(key) {
    if (key === S.view) return;
    S.stack.push(S.view);
    if (S.stack.length > 30) S.stack.shift();
    S.view = key;
    render();
    if (S.root) { const sh = S.root.querySelector(".fl-sheet"); if (sh) sh.scrollTop = 0; }
  }
  function back() { S.view = S.stack.length ? S.stack.pop() : null; render(); }
  function home() { S.view = null; S.stack = []; render(); }

  function navRow(crumbs) {
    const r = el("div", "fl-navrow");
    if (S.stack.length) r.append(btn("fl-navbtn", `Back to ${viewLabel(S.stack[S.stack.length - 1])}`, back));
    r.append(btn("fl-navbtn quiet", "All sessions", home));
    if (crumbs && crumbs.length) {
      const c = el("span", "fl-crumbs");
      crumbs.forEach(([label, key], i) => {
        if (i) c.append(el("span", "fl-sep", "/"));
        c.append(key ? btn("fl-lk", label, () => go(key)) : el("span", null, label));
      });
      r.append(c);
    }
    r.append(el("span", "fl-hint", "Esc goes back"));
    return r;
  }

  // ---------- links out ----------
  function openLinks(n) {
    const wrap = el("span", "fl-links");
    if (n.viraJob) {
      wrap.append(btn("fl-open", "Open in Vira", () => {
        if (typeof window.openJob === "function") window.openJob(n.viraJob);
      }));
      return wrap;
    }
    if (n.openUrl && safeUrl(n.openUrl)) {
      wrap.append(ext("fl-open", n.tool === "codex" ? "Open in Codex" : "Open in Claude Code", n.openUrl));
      const c = btn("fl-copy", "Copy link", () => {
        if (typeof copyText === "function") copyText(n.openUrl);
        c.textContent = "Copied";
        setTimeout(() => { c.textContent = "Copy link"; }, 1500);
      });
      wrap.append(c);
    }
    return wrap;
  }
  function gitRow(node, n) {
    node.replaceChildren();
    const g = n.git || {};
    const branch = g.branch || n.branch;
    if (branch && !["main", "master"].includes(branch)) {
      node.append(btn("fl-br", branch, () => go(n.family && famIsFeature(n.family) ? `fam:${n.family}` : n.id),
        `${branch}${g.worktree ? ` (worktree ${g.worktree})` : ""}`));
    }
    (g.prs || n.prs || []).slice(-2).forEach((p) => node.append(safeUrl(p.url)
      ? ext(`fl-pr ${p.state}`, prLabel(p), p.url) : el("span", `fl-pr ${p.state}`, prLabel(p))));
  }
  const famIsFeature = (fid) => S.data && (S.data.families || []).some((f) => f.id === fid && f.kind === "feature");
  function clickable(node, key) {
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.addEventListener("click", () => go(key));
    node.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target === node) { e.preventDefault(); go(key); }
    });
  }
  const toolTag = (n) => el("span", `fl-tool t-${n.tool}`, `${TOOL[n.tool] || n.tool}${n.surface && n.tool !== "vira" ? ` ${n.surface}` : ""}`);

  // ---------- what a session is doing: one small animated icon per kind ----------
  // The server names the kind (fleet.doing_of). The icons are drawn SVG,
  // not GIFs: they take the theme's colours, stay sharp at any zoom, and
  // hold a still frame under reduced motion. The moving parts carry the
  // classes fleet.css animates. Each shape: [tag, attributes].
  const C = (cx, cy, r, cls) => ["circle", { cx, cy, r, class: cls }];
  const L = (x1, y1, x2, y2, cls) => ["line", { x1, y1, x2, y2, class: cls }];
  const DOING = {
    waiting: ["Waiting", [C(12, 12, 3, "fill"), C(12, 12, 3, "ping")]],
    testing: ["Testing", [C(3.5, 12, 1.7, "fill t1"), C(8, 12, 1.7, "fill t2"), C(12.5, 12, 1.7, "fill t3"),
      ["path", { d: "M16 12.5l2.3 2.3 4.2-5.3", class: "t4" }]]],
    editing: ["Editing", [L(4, 6, 20, 6), L(4, 11, 17, 11), L(4, 16, 14, 16, "type"), L(15.5, 13.5, 15.5, 18.5, "caret")]],
    reading: ["Reading", [["rect", { x: 5, y: 3, width: 14, height: 18, rx: 2 }], L(8.5, 8, 15.5, 8, "thin"),
      L(8.5, 12, 15.5, 12, "thin"), L(8.5, 16, 13, 16, "thin"), ["rect", { x: 6.5, y: 6, width: 11, height: 4, class: "scan" }]]],
    researching: ["Researching", [["g", { class: "hunt" }, [C(10, 10, 5), L(13.7, 13.7, 19, 19)]]]],
    shipping: ["Shipping", [L(5, 3.5, 19, 3.5), ["g", { class: "push" }, [L(12, 20, 12, 8), ["path", { d: "M7.5 12L12 7.5l4.5 4.5" }]]]]],
    delegating: ["Delegating", [C(5, 12, 2.4, "fill"), C(19, 5, 2), C(19, 12, 2), C(19, 19, 2),
      L(7.5, 11, 17, 5.8, "flow"), L(7.5, 12, 17, 12, "flow"), L(7.5, 13, 17, 18.2, "flow")]],
    running: ["Running", [["path", { d: "M5 7.5l4.5 4.5L5 16.5" }], L(12, 17, 19, 17, "caret")]],
    thinking: ["Thinking", [C(6, 12, 2, "fill b1"), C(12, 12, 2, "fill b2"), C(18, 12, 2, "fill b3")]],
    idle: ["Idle", [["path", { d: "M15.5 4.5a7.5 7.5 0 1 0 4 12.5 6 6 0 0 1-4-12.5z" }]]],
  };
  const DOING_ORDER = ["waiting", "testing", "editing", "reading", "researching", "shipping",
                       "delegating", "running", "thinking", "idle"];
  const doingKind = (n) => (n && DOING[n.doing] ? n.doing : n && n.live ? "running" : "idle");
  const doingLabel = (kind) => (DOING[kind] || DOING.running)[0];
  function svgShape([tag, attrs, kids]) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, String(v));
    (kids || []).forEach((k) => node.append(svgShape(k)));
    return node;
  }
  function doingIcon(kind) {
    const k = DOING[kind] ? kind : "running";
    const box = el("span", `fl-do k-${k}`);
    box.dataset.kind = k;
    box.setAttribute("aria-hidden", "true");
    box.append(svgShape(["svg", { viewBox: "0 0 24 24" }, DOING[k][1]]));
    return box;
  }
  // Every frame re-attaches the board's nodes, and a browser restarts a
  // CSS animation on re-attach, so each icon would snap back to its first
  // frame every two seconds. Pinning each one to the page clock makes a
  // restart land on the phase it already had.
  function syncIcons() {
    if (typeof document.getAnimations !== "function") return;
    for (const a of document.getAnimations()) {
      if (String(a.animationName || "").startsWith("fl-do-") && a.startTime !== 0) a.startTime = 0;
    }
  }
  // Swap the icon only when the kind changes, so it keeps its place.
  function setDoingIcon(holder, kind) {
    const cur = holder.children[0];
    if (cur && cur.dataset && cur.dataset.kind === kind) return;
    holder.replaceChildren(doingIcon(kind));
  }
  for (const k of DOING_ORDER) {
    LISTS[`do-${k}`] = { title: doingLabel(k), test: (n) => n.live && doingKind(n) === k };
  }

  // ---------- live tiles and closed chips (kept, updated in place) ----------
  function makeTile(id) {
    const b = el("div", "fl-tile");
    b.dataset.node = id;
    clickable(b, id);
    const row1 = el("div", "fl-row");
    const chip = el("span", "fl-chip");
    chip.append(el("span", "fl-dot"), el("span", "fl-st"));
    row1.append(chip, el("span", "fl-ttag"), el("span", "fl-clock fl-num"));
    const now1 = el("div", "fl-now");
    now1.append(el("span", "fl-doh"), el("span", "fl-tooln"), el("span", "fl-what"), el("span", "fl-t fl-num"));
    const steps = el("div", "fl-steps");
    steps.append(el("div", "fl-segs"), el("span", "fl-sc fl-num"));
    const fuel = el("div", "fl-fuel");
    const tank = el("div", "fl-tank");
    tank.append(el("i"));
    fuel.append(el("span", null, "Context"), tank, el("span", "fl-fv fl-num"));
    const meters = el("div", "fl-meters");
    for (const [cls, label] of [["ag", "agents"], ["sh", "shells"], ["tc", "tool calls"]]) {
      const m = el("span", `fl-m ${cls}`);
      m.append(el("b"), document.createTextNode(label));
      meters.append(m);
    }
    meters.append(el("span", "fl-mx fl-num"));
    b.append(row1, el("div", "fl-name"), el("div", "fl-gitrow"), now1, steps, fuel, meters);
    tiles.set(id, b);
    return b;
  }
  const q1 = (root, cls) => root.querySelector(`.${cls}`);
  function segs(box, plan) {
    const items = plan ? plan.items : [];
    if (box.children.length !== items.length) box.replaceChildren(...items.map(() => el("i", "fl-seg")));
    items.forEach((it, i) => {
      box.children[i].className = `fl-seg${it.status === "completed" ? " done" : it.status === "in_progress" ? " cur" : ""}`;
    });
  }
  function questionText(q) {
    if (!q) return "";
    if (q.kind === "ask") return (q.questions[0] && q.questions[0].question) || "A question for you";
    if (q.kind === "plan") return "Approve the plan";
    return q.ask || "";
  }
  function nowLine(node, s) {
    const tool = q1(node, "fl-tooln"), what = q1(node, "fl-what"), t = q1(node, "fl-t");
    const kind = doingKind(s);
    node.classList.toggle("quiet", !(s.turnOpen && s.now));
    setDoingIcon(q1(node, "fl-doh"), kind);
    tool.title = s.turnOpen && s.now ? s.now.name : "";
    if (s.status === "Needs you") {
      tool.textContent = "Waiting";
      what.textContent = questionText(s.question) || "Waiting for your answer";
      since(t, (s.question && s.question.at) || s.lastAt);
      return;
    }
    if (s.turnOpen && s.now) { tool.textContent = doingLabel(kind); what.textContent = s.now.desc || ""; since(t, s.now.startedAt); return; }
    if (s.turnOpen) {
      tool.textContent = "Thinking";
      what.textContent = s.feed && s.feed[0] ? `after ${s.feed[0].name}` : "working out the next step";
      since(t, s.lastAt);
      return;
    }
    const live = s.agents.live[0];
    if (live) { tool.textContent = "Delegating"; what.textContent = live.what; since(t, live.startedAt); return; }
    if (s.shells[0]) { tool.textContent = doingLabel(kind); what.textContent = s.shells[0].cmd; since(t, s.shells[0].startedAt); return; }
    tool.textContent = "Done";
    what.textContent = (s.summary && s.summary.detail) || s.lastText || "Finished its last request";
    agoAt(t, s.lastAt);
  }
  function updateTile(b, s) {
    b.dataset.status = s.status;
    b.setAttribute("aria-label", `${s.title}, ${s.status}`);
    q1(b, "fl-st").textContent = s.status;
    const tt = q1(b, "fl-ttag");
    tt.replaceChildren(toolTag(s));
    q1(b, "fl-name").textContent = s.title;
    const ck = q1(b, "fl-clock");
    if (s.turnOpen || s.status === "Needs you") since(ck, s.turnStartedAt);
    else { delete ck.dataset.since; delete ck.dataset.ago; ck.textContent = ""; }
    gitRow(q1(b, "fl-gitrow"), s);
    nowLine(q1(b, "fl-now"), s);
    const st = q1(b, "fl-steps");
    st.classList.toggle("none", !s.plan);
    segs(q1(st, "fl-segs"), s.plan);
    q1(st, "fl-sc").textContent = s.plan ? `${s.plan.done}/${s.plan.total}` : "";
    const fuel = q1(b, "fl-fuel");
    const hasCtx = s.context && s.context.used;
    fuel.classList.toggle("none", !hasCtx);
    const p = pct(s.context), fill = q1(fuel, "fl-tank").children[0];
    fill.style.width = `${(p * 100).toFixed(1)}%`;
    fill.className = fuelClass(p);
    q1(fuel, "fl-fv").textContent = hasCtx ? `${tok(s.context.used)} / ${tok(s.context.limit)}` : "";
    const ag = q1(b, "ag");
    ag.children[0].textContent = String(s.agents.live.length);
    ag.classList.toggle("lit", s.agents.live.length > 0);
    ag.title = `${s.agents.total} spawned in this session`;
    const sh = q1(b, "sh");
    sh.children[0].textContent = String(s.shells.length);
    sh.classList.toggle("lit", s.shells.length > 0);
    q1(b, "tc").children[0].textContent = String(s.turnTools || 0);
    q1(b, "fl-mx").textContent = s.model || "";
  }
  function makeChip(id) {
    const b = el("div", "fl-cnode");
    b.dataset.node = id;
    b.dataset.status = "Closed";
    clickable(b, id);
    const ct = el("div", "fl-ct");
    ct.append(el("span", "fl-dot"), el("span", "fl-tt"));
    b.append(ct, el("div", "fl-cm"));
    chips.set(id, b);
    return b;
  }
  function updateChip(b, n) {
    q1(b, "fl-tt").textContent = n.title;
    b.setAttribute("aria-label", `${n.title}, closed`);
    const cm = q1(b, "fl-cm");
    cm.replaceChildren(toolTag(n));
    const when = el("span");
    agoAt(when, n.lastAt);
    cm.append(when);
    const g = n.git || {};
    (g.prs || []).slice(-2).forEach((p) => cm.append(safeUrl(p.url) ? ext(`fl-pr ${p.state}`, prLabel(p), p.url)
      : el("span", `fl-pr ${p.state}`, prLabel(p))));
    if (!(g.prs || []).length && g.branch) cm.append(el("span", "fl-mono", g.branch.replace(/^(claude|codex)\//, "")));
  }
  function nodeEl(n) {
    if (n.live) { const b = tiles.get(n.id) || makeTile(n.id); updateTile(b, n); return b; }
    const c = chips.get(n.id) || makeChip(n.id);
    updateChip(c, n);
    return c;
  }

  // ---------- the top strip ----------
  function renderTop(box, d) {
    const t = counts(d, S);
    const items = [["working", "Working", t.working], ["needs", "Needs you", t.needs], ["jobs", "Jobs running", t.jobs],
                   ["idle", "Idle", t.idle], ["agents", "Subagents", t.agents], ["shells", "Shells", t.shells]];
    box.replaceChildren(...items.map(([key, label, n]) => {
      const c = btn(`fl-count c-${key}${n ? "" : " zero"}${key === "needs" && n ? " alert" : ""}`, null,
        () => go(`list:${key}`), `Show: ${LISTS[key].title}`);
      c.append(el("span", "fl-sw"), el("b", null, String(n)), document.createTextNode(label));
      return c;
    }));
  }
  function setLive() {
    if (!S.root) return;
    const n = q1(S.root, "fl-live");
    if (!n) return;
    const age = S.receivedAt ? Date.now() - S.receivedAt : Infinity;
    const l = S.data ? liveLabel(age, S.error) : { text: S.error ? "Reconnecting" : "Connecting", cls: S.error ? "bad" : "" };
    n.className = `fl-live ${l.cls}`;
    q1(n, "fl-live-t").textContent = l.text;
  }

  function seg(name, options, current, onPick) {
    const g = el("div", "fl-seg-ctl");
    g.setAttribute("role", "group");
    g.setAttribute("aria-label", name);
    options.forEach(([val, label]) => {
      const b = btn(null, label, () => onPick(val));
      b.setAttribute("aria-pressed", String(val === current));
      g.append(b);
    });
    return g;
  }
  function renderControls(box, d) {
    const projects = [...new Set([...(d.sessions || []), ...(d.nodes || [])].map((n) => n.project))]
      .concat((S.repos && S.repos.repos || []).map((r) => r.name));
    const sel = el("select", "fl-select");
    sel.setAttribute("aria-label", "Project");
    const all = el("option", null, "All projects");
    all.value = "";
    sel.append(all);
    [...new Set(projects)].filter(Boolean).sort((a, b) => a.localeCompare(b)).forEach((p) => {
      const o = el("option", null, p);
      o.value = p;
      if (p === S.project) o.selected = true;
      sel.append(o);
    });
    sel.value = S.project;
    sel.addEventListener("change", () => { S.project = sel.value; save(); render(); });
    const pick = (k) => (v) => { S[k] = v; if (k === "tab") S.view = null; save(); render(); };
    // Rebuilt only when something it shows changed: a control replaced
    // under the pointer between press and release loses the click.
    const sig = JSON.stringify([S.tab, S.tool, S.mode, S.project, [...new Set(projects)]]);
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.replaceChildren(
      seg("Show", [["sessions", "Sessions"], ["timeline", "Timeline"], ["week", "Last 7 days"], ["calls", "Background calls"]], S.tab, pick("tab")),
      seg("Tool", [["all", "All"], ["claude", "Claude"], ["codex", "Codex"], ["vira", "Vira jobs"]], S.tool, pick("tool")),
      sel,
      ...(S.tab === "sessions" ? [el("span", "fl-lbl", "Closed"),
        seg("Closed sessions", [["linked", "Linked only"], ["24h", "Last 24 h"], ["3d", "Last 3 days"]], S.mode, pick("mode"))] : []));
  }

  // ---------- live sessions grouped by what they are doing ----------
  // "Those three are testing, those two are editing": one card per kind,
  // its icon, a count, and the first few sessions by name. Cards are kept
  // and updated in place so their icons keep animating across frames.
  const doingCards = new Map();
  function doingStrip(shown) {
    const groups = new Map();
    shown.filter((n) => n.live).forEach((n) => {
      const k = doingKind(n);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(n);
    });
    if (!groups.size) return null;
    const wrap = S.doingWrap || (S.doingWrap = el("section", "fl-doing"));
    wrap.setAttribute("aria-label", "What the open sessions are doing");
    wrap.replaceChildren(...DOING_ORDER.filter((k) => groups.has(k)).map((k) => {
      let card = doingCards.get(k);
      if (!card) {
        card = el("div", `fl-dcard k-${k}`);
        const top = btn("fl-dtop", null, () => go(`list:do-${k}`), `Show every session: ${doingLabel(k)}`);
        top.append(doingIcon(k), el("span", "fl-dl", doingLabel(k)), el("b", "fl-dc fl-num"));
        card.append(top, el("div", "fl-dnames"));
        doingCards.set(k, card);
      }
      const ns = groups.get(k).sort((a, b) => (b.lastAt || 0) - (a.lastAt || 0));
      q1(card, "fl-dc").textContent = String(ns.length);
      q1(card, "fl-dnames").replaceChildren(...ns.slice(0, 3).map((n) => {
        const b = btn("fl-dn", n.title, () => go(n.id), n.title);
        b.style.setProperty("--pc", pc(n.project));
        return b;
      }), ...(ns.length > 3 ? [el("span", "fl-dmore", `+${ns.length - 3} more`)] : []));
      return card;
    }));
    return wrap;
  }

  // ---------- questions waiting on the owner ----------
  function askStrip(shown) {
    const waiting = shown.filter(realWaiting);
    // Sessions the AI read says need the owner, and that are not already
    // asking: shown apart, and marked as the read's opinion.
    const flagged = shown.filter((n) => !realWaiting(n) && aiSession(n.id) && aiSession(n.id).needsUser);
    if (!waiting.length && !flagged.length) return null;
    const wrap = el("section", "fl-asks");
    const h = el("div", "fl-asks-h", "Waiting on you");
    h.append(el("span", null, [waiting.length ? plural(waiting.length, "question") : "",
      flagged.length ? `${flagged.length} more by the AI read` : ""].filter(Boolean).join(" / ")));
    const row = el("div", "fl-asks-row");
    waiting.forEach((n) => {
      const c = el("div", "fl-qcard");
      clickable(c, n.id);
      const top = el("div", "fl-qtop");
      const p = el("span", "fl-qp", n.project);
      p.style.color = pc(n.project);
      const w = el("span", "fl-qw fl-num");
      since(w, n.question.at || n.lastAt);
      top.append(p, toolTag(n), w);
      c.append(top, el("div", "fl-qs", n.title), el("div", "fl-qq", questionText(n.question)));
      const q = n.question;
      const opts = el("div", "fl-opts");
      if (q.kind === "ask") (q.questions[0]?.options || []).forEach((o) => opts.append(el("span", "fl-opt", o.label)));
      else if (q.kind === "wrap") (q.actions || []).forEach((a) => opts.append(el("span", "fl-opt", a.label)));
      else opts.append(el("span", "fl-opt", "Approve or change the plan"));
      if (opts.children.length) c.append(opts);
      const act = el("div", "fl-qact");
      act.append(openLinks(n));
      c.append(act);
      row.append(c);
    });
    flagged.forEach((n) => {
      const c = el("div", "fl-qcard soft");
      clickable(c, n.id);
      const top = el("div", "fl-qtop");
      const p = el("span", "fl-qp", n.project);
      p.style.color = pc(n.project);
      top.append(p, toolTag(n));
      const note = el("div", "fl-qnote");
      note.append(aiTag(), document.createTextNode(" From the AI read, not a question the session asked"));
      c.append(top, el("div", "fl-qs", n.title), el("div", "fl-qq", aiSession(n.id).why || "May need you"), note);
      const act = el("div", "fl-qact");
      act.append(openLinks(n));
      c.append(act);
      row.append(c);
    });
    wrap.append(h, row);
    return wrap;
  }

  // ---------- lanes, families and trees ----------
  function famStateChip(f) {
    const ai = aiFeature(f.id);
    const key = ai && ai.status ? ai.status : f.state;
    const chip = el("span", `fl-state s-${key}`, key === "unmerged" ? `${f.needsWork} not merged` : STATE_LABEL[key] || key);
    if (ai && ai.status) chip.title = "From the AI read";
    return chip;
  }
  function famCounts(f) {
    const parts = [];
    if (f.members.length > 1) parts.push(plural(f.members.length, "session"));
    if (f.branchCount) {
      parts.push(plural(f.branchCount, "branch", "branches"));
      if (f.merged) parts.push(`${f.merged} merged`);
      if (f.deleted) parts.push(`${f.deleted} deleted`);
      if (f.needsWork) parts.push(`${f.needsWork} not merged`);
    }
    return parts.join(" / ");
  }
  function famHeader(f) {
    const h = btn("fl-fam-h", null, () => go(`fam:${f.id}`), "Open this feature: its sessions and every branch");
    const top = el("div", "fl-fam-top");
    top.append(el("span", "fl-fam-title", f.title), famStateChip(f));
    const c = famCounts(f);
    if (c) top.append(el("span", "fl-badge", c));
    if (f.branchCount) {
      const dots = el("span", "fl-bdots");
      dots.title = f.branches.map((b) => `${b.name}: ${BRANCH_LABEL[b.state] || b.state}${b.pr ? ` (PR #${b.pr.n})` : ""}`).join("\n");
      f.branches.forEach((b) => dots.append(el("i", `fl-bd ${b.state}`)));
      top.append(dots);
    }
    h.append(top);
    const ai = aiFeature(f.id);
    if (ai && (ai.label || ai.next)) {
      const line = el("div", "fl-fam-ai");
      line.append(aiTag(), document.createTextNode(` ${ai.label || ""}${ai.next ? ` Next: ${ai.next}` : ""}`));
      h.append(line);
    }
    return h;
  }
  function buildTree(members) {
    const ids = new Set(members.map((m) => m.id));
    const kids = new Map();
    members.forEach((m) => {
      if (m.parentId && ids.has(m.parentId)) {
        if (!kids.has(m.parentId)) kids.set(m.parentId, []);
        kids.get(m.parentId).push(m);
      }
    });
    const byAge = (a, b) => (a.createdAt || 0) - (b.createdAt || 0);
    const tn = (m) => {
      const wrap = el("div", "fl-tn");
      const node = nodeEl(m);
      node.dataset.parent = m.parentId && ids.has(m.parentId) ? m.parentId : "";
      node.dataset.link = m.link || "";
      wrap.append(node);
      const ch = (kids.get(m.id) || []).sort(byAge);
      if (ch.length) { const k = el("div", "fl-kids"); ch.forEach((c) => k.append(tn(c))); wrap.append(k); }
      return wrap;
    };
    const tree = el("div", "fl-tree");
    members.filter((m) => !(m.parentId && ids.has(m.parentId))).sort(byAge).forEach((r) => tree.append(tn(r)));
    return tree;
  }
  function laneMeta(p, fams) {
    const meta = el("span", "fl-lane-meta");
    const open = fams.reduce((a, x) => a + x.members.filter((m) => m.live).length, 0);
    const working = fams.reduce((a, x) => a + x.members.filter((m) => m.status === "Working").length, 0);
    const features = fams.map((x) => x.f).filter((f) => f.kind === "feature");
    const needsWork = features.reduce((a, f) => a + (f.needsWork || 0), 0);
    const parts = [open ? `${plural(open, "session")} open` : "no sessions open"];
    if (working) parts.push(`${working} working`);
    if (features.length) parts.push(plural(features.length, "feature"));
    meta.append(document.createTextNode(parts.join(" / ")));
    meta.append(document.createTextNode(" / "));
    meta.append(btn(`fl-lk${needsWork ? " warn" : ""}`,
      needsWork ? `${plural(needsWork, "branch", "branches")} not merged` : "worktrees and branches",
      () => go(`proj:${p}`), "Open this project: worktrees, branches and its timeline"));
    return meta;
  }
  function renderLanes(box, d, idx, shown) {
    const order = laneGroups(shown, idx);
    const asks = askStrip(shown);
    const doing = doingStrip(shown);
    const tops = [doing, asks].filter(Boolean);
    if (!order.length) {
      box.replaceChildren(...tops, el("p", "fl-empty",
        "No sessions to show. Open one in Claude Code or Codex, or show closed sessions from the last 24 hours."));
      return;
    }
    const keep = box.scrollTop;
    box.replaceChildren(...tops, ...order.map(({ p, fams }) => {
      const lane = el("section", "fl-lane");
      lane.style.setProperty("--pc", pc(p));
      const head = el("div", "fl-lane-h");
      head.append(btn("fl-lane-name", p, () => go(`proj:${p}`), "Open this project"), laneMeta(p, fams));
      const aiLine = aiProject(p) ? aiNote(aiProject(p).status) : null;
      const body = el("div", "fl-lane-body");
      fams.forEach(({ f, members }) => {
        const fam = el("div", "fl-fam");
        fam.dataset.fam = f.id;
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "fl-wires");
        svg.setAttribute("aria-hidden", "true");
        fam.append(svg);
        if (f.kind === "feature") { fam.classList.add("group"); fam.append(famHeader(f)); }
        fam.append(buildTree(members));
        body.append(fam);
      });
      lane.append(head, ...(aiLine ? [aiLine] : []), body);
      return lane;
    }));
    box.scrollTop = keep;
    for (const id of [...tiles.keys()]) if (!idx.byId.has(id) || !idx.byId.get(id).live) tiles.delete(id);
    for (const id of [...chips.keys()]) if (!idx.byId.has(id) || idx.byId.get(id).live) chips.delete(id);
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => drawWires(box));
  }

  // Offsets, not client rects: floating windows are CSS-zoomed, and
  // layout offsets are in the same unzoomed space the SVG draws in.
  function offsetIn(node, ancestor) {
    let x = 0, y = 0, n = node;
    while (n && n !== ancestor) { x += n.offsetLeft; y += n.offsetTop; n = n.offsetParent; }
    return { x, y, w: node.offsetWidth, h: node.offsetHeight };
  }
  function drawWires(box) {
    box.querySelectorAll(".fl-fam").forEach((fam) => {
      const svg = fam.querySelector("svg.fl-wires");
      if (!svg) return;
      svg.setAttribute("width", fam.scrollWidth);
      svg.setAttribute("height", fam.scrollHeight);
      svg.replaceChildren();
      fam.querySelectorAll('[data-node][data-parent]:not([data-parent=""])').forEach((child) => {
        const parent = fam.querySelector(`[data-node="${CSS.escape(child.dataset.parent)}"]`);
        if (!parent) return;
        const a = offsetIn(parent, fam), b = offsetIn(child, fam);
        let dpath;
        if (b.x >= a.x + a.w - 2) {
          const x1 = a.x + a.w, y1 = a.y + Math.min(28, a.h / 2), x2 = b.x, y2 = b.y + Math.min(28, b.h / 2), dx = (x2 - x1) / 2;
          dpath = `M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
        } else {
          const x1 = a.x + 12, y1 = a.y + a.h, x2 = b.x, y2 = b.y + Math.min(20, b.h / 2);
          dpath = `M${x1} ${y1} V${y2 - 6} Q${x1} ${y2} ${x1 + 6} ${y2} H${x2}`;
        }
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", dpath);
        if (child.dataset.link === "fork") path.setAttribute("class", "fork");
        svg.append(path);
      });
    });
  }

  // ---------- detail views ----------
  function block(title, ...children) {
    const b = el("div", "fl-blk");
    b.append(el("h3", null, title), ...children.filter(Boolean));
    return b;
  }
  function dial(k, v, s, extra) {
    const d = el("div", "fl-dial");
    d.append(el("span", "k", k), v, el("span", "s", s));
    if (extra) d.append(extra);
    return d;
  }
  function head(eyebrow, title, extra) {
    const h = el("div", "fl-sheet-head");
    if (eyebrow) h.append(eyebrow);
    h.append(el("h2", null, title));
    if (extra) h.append(extra);
    return h;
  }
  function projLink(p) { const b = btn("fl-lk", p, () => go(`proj:${p}`)); b.style.color = pc(p); return b; }
  function sessionRow(n, extra) {
    const it = el("div", `fl-item${n.status === "Working" ? " run" : ""}`);
    const a = el("div", "a");
    a.append(btn("fl-lk", n.title, () => go(n.id)));
    const when = el("div", "b");
    if (n.live && n.turnOpen) since(when, n.turnStartedAt); else agoAt(when, n.lastAt);
    const c = el("div", "c");
    c.append(toolTag(n), el("span", null, n.live ? n.status : "closed"), projLink(n.project));
    if (extra) c.append(...extra);
    it.append(a, when, c);
    return it;
  }
  function questionBlock(n) {
    const q = n.question;
    if (!q || !realWaiting(n)) return null;
    const b = el("div", "fl-qblock");
    b.append(el("h3", null, q.kind === "plan" ? "Plan waiting for approval" : "Waiting on you"));
    if (q.kind === "ask") {
      q.questions.forEach((qq) => {
        b.append(el("p", "fl-qq", qq.question));
        const l = el("div", "fl-optl");
        qq.options.forEach((o) => {
          const d = el("div");
          d.append(el("b", null, o.label));
          if (o.description) d.append(el("span", null, ` - ${o.description}`));
          l.append(d);
        });
        if (qq.multi) l.append(el("div", null, "You can pick more than one."));
        b.append(l);
      });
    } else if (q.kind === "plan") {
      b.append(el("pre", "fl-plan-text", q.plan || "No plan text recorded."));
    } else {
      if (q.recap) b.append(el("p", "fl-quote soft", q.recap));
      b.append(el("p", "fl-qq", q.ask));
      const l = el("div", "fl-optl");
      (q.actions || []).forEach((a) => {
        const d = el("div");
        d.append(el("b", null, a.label));
        if (a.message && a.message !== a.label) d.append(el("span", null, ` - sends "${a.message}"`));
        l.append(d);
      });
      if (l.children.length) b.append(l);
    }
    b.append(el("p", "fl-empty", "Answer it in the session:"), openLinks(n));
    return b;
  }
  function gitBlock(n, idx) {
    const g = n.git || {};
    const list = el("div", "fl-list");
    const fam = idx.famById.get(n.family);
    const branch = g.branch || n.branch;
    if (branch) {
      const it = el("div", "fl-item");
      const a = el("div", "a fl-mono");
      a.append(fam && fam.kind === "feature" ? btn("fl-lk", branch, () => go(`fam:${fam.id}`)) : document.createTextNode(branch));
      const fb = fam && fam.branches.find((b) => b.name === branch);
      it.append(a, el("div", `b fl-bs ${fb ? fb.state : ""}`, fb ? branchLabel(fb) : `from ${g.base || "main"}`));
      if (g.worktree || n.folder) it.append(el("div", "c fl-mono", g.worktree ? `Worktree ${g.worktree}` : n.folder));
      list.append(it);
    }
    (g.prs || n.prs || []).forEach((p) => {
      const it = el("div", "fl-item");
      const a = el("div", "a");
      a.append(safeUrl(p.url) ? ext("fl-ext", `Pull request #${p.n}`, p.url) : document.createTextNode(`Pull request #${p.n}`));
      it.append(a, el("div", `b fl-pr ${p.state}`, String(p.state || "").toLowerCase()));
      list.append(it);
    });
    if ((g.written || []).length) {
      const it = el("div", "fl-item");
      it.append(el("div", "c fl-mono", `Wrote to: ${g.written.join(", ")}`));
      list.append(it);
    }
    if (!list.children.length) list.append(el("p", "fl-empty", "No branch or pull request recorded."));
    return block("Git", list);
  }
  function linkBlock(n, idx) {
    const list = el("div", "fl-list");
    const parent = n.parentId && idx.byId.get(n.parentId);
    if (parent) list.append(sessionRow(parent, [el("span", null, n.link === "fork" ? "this session was forked from it" : "this session was started from it")]));
    idx.all.filter((x) => x.parentId === n.id).sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0))
      .forEach((c) => list.append(sessionRow(c, [el("span", null, c.link === "fork" ? "forked from this one" : "started from this one")])));
    if (!list.children.length) list.append(el("p", "fl-empty", "Not linked to other sessions."));
    return block("Connected sessions", list);
  }
  function crumbsFor(n, idx) {
    const fam = idx.famById.get(n.family);
    const c = [[n.project, `proj:${n.project}`]];
    if (fam && fam.kind === "feature") c.push([fam.title, `fam:${fam.id}`]);
    return c;
  }
  function chipFor(status) {
    const chip = el("span", "fl-chip");
    chip.dataset.status = status;
    chip.append(el("span", "fl-dot"), el("span", null, status));
    return chip;
  }
  function renderSession(sheet, s, idx) {
    const eyebrow = el("div", "fl-eyebrow");
    const doing = el("span", "fl-doing-tag");
    doing.append(doingIcon(doingKind(s)), document.createTextNode(doingLabel(doingKind(s))));
    eyebrow.append(projLink(s.project), toolTag(s), chipFor(s.status), doing);
    const facts = el("div", "fl-facts");
    const fact = (k, v) => { const f = el("span"); f.append(document.createTextNode(`${k} `), typeof v === "string" ? el("b", null, v) : v); facts.append(f); };
    fact("Model", s.model || "-");
    fact("Effort", s.effort || "-");
    if (s.mode) fact("Mode", s.mode);
    if (s.memMB != null) fact("Memory", `${s.memMB} MB`);
    if (s.cpu != null) fact("CPU", `${s.cpu}%`);
    if (s.pid) fact("PID", String(s.pid));
    const ageB = el("b", "fl-num");
    since(ageB, s.createdAt || s.startedAt);
    fact("Session age", ageB);
    const turnV = el("span", "v fl-num");
    if (s.turnOpen || s.status === "Needs you") since(turnV, s.turnStartedAt); else turnV.textContent = "-";
    const p = pct(s.context);
    const tank = el("div", "fl-tank");
    const fill = el("i", fuelClass(p));
    fill.style.width = `${(p * 100).toFixed(1)}%`;
    tank.append(fill);
    const stepSegs = el("div", "fl-segs");
    segs(stepSegs, s.plan);
    const stepWrap = el("div", "fl-steps");
    stepWrap.append(stepSegs);
    const ctx = s.context || {};
    const dials = el("div", "fl-dials");
    dials.append(
      dial("Current request", turnV, s.turnOpen ? `started by ${s.turnOrigin || "you"}` : "no request running"),
      dial("Steps", el("span", "v fl-num", s.plan ? `${s.plan.done} / ${s.plan.total}` : "-"),
        s.plan ? "from its task list" : "no task list this request", s.plan ? stepWrap : null),
      dial("Context", el("span", "v fl-num", ctx.used ? `${Math.round(p * 100)}%` : "-"),
        ctx.used ? `${tok(ctx.used)} of ${tok(ctx.limit)} tokens${ctx.compactions ? ` / compacted ${ctx.compactions}x` : ""}` : "not reported", tank),
      dial("Agents running", el("span", "v fl-num", String(s.agents.live.length)), `${s.agents.total} spawned in this session`),
      // Shells are read from the session's process; without one, 0 would
      // claim nothing runs when nothing was looked at.
      dial("Shells running", el("span", "v fl-num", s.pid ? String(s.shells.length) : "-"),
        `${s.pid ? "" : "process not found / "}${s.turnTools || 0} tool calls this request`));
    const feed = el("div", "fl-list");
    if (s.now && s.turnOpen) {
      const it = el("div", "fl-item run");
      const a = el("div", "a");
      a.append(el("span", "fl-tooln", s.now.name), document.createTextNode(s.now.desc || ""));
      const bt = el("div", "b");
      since(bt, s.now.startedAt);
      it.append(a, bt);
      feed.append(it);
    }
    (s.feed || []).filter((f) => f.say || f.ms != null).forEach((f) => {
      const key = `${s.id}|${f.at}|${f.say ? "say" : f.name}`;
      const fresh = S.seenFeed.has(key) ? "" : " fresh";
      S.seenFeed.add(key);
      const bt = el("div", "b");
      const ag = el("span");
      agoAt(ag, f.at);
      if (f.say) {
        // The agent's own words between calls: what it says it is doing.
        const it = el("div", `fl-item say${fresh}`);
        bt.append(ag);
        it.append(el("div", "a fl-quote soft", f.say), bt);
        feed.append(it);
        return;
      }
      const it = el("div", `fl-item${f.error ? " err" : ""}${fresh}`);
      const a = el("div", "a");
      a.append(el("span", "fl-tooln", f.name), document.createTextNode(f.desc || ""));
      bt.textContent = `${f.error ? "failed / " : ""}${dur(f.ms)} / `;
      bt.append(ag);
      it.append(a, bt);
      if (f.out) it.append(el("div", "c fl-mono", f.out));
      feed.append(it);
    });
    if (!feed.children.length) feed.append(el("p", "fl-empty", "No tool calls yet."));
    const plan = el("ol", "fl-plan");
    (s.plan ? s.plan.items : []).forEach((i) => plan.append(el("li", i.status, i.content)));
    const agents = el("div", "fl-list");
    s.agents.live.forEach((a) => {
      const it = el("div", "fl-item run");
      const bt = el("div", "b");
      since(bt, a.startedAt);
      it.append(el("div", "a", a.what), bt, el("div", "c", `${a.now ? `Now: ${a.now} / ` : ""}${a.tools} tool calls`));
      agents.append(it);
    });
    if (!agents.children.length) agents.append(el("p", "fl-empty", s.agents.total ? `None running. ${s.agents.total} finished earlier.` : "None spawned."));
    const shells = el("div", "fl-list");
    s.shells.forEach((x) => {
      const it = el("div", "fl-item run");
      const bt = el("div", "b");
      since(bt, x.startedAt);
      it.append(el("div", "a fl-mono", x.cmd), bt, el("div", "c", `${x.cpu}% CPU / ${x.memMB} MB`));
      shells.append(it);
    });
    if (!shells.children.length) shells.append(el("p", "fl-empty", "No shell commands running."));
    const left = el("div", "fl-col");
    // append() writes a null argument as the text "null": filter first.
    left.append(...[block("Asked", el("p", "fl-quote", s.asked || "No request found.")),
      block("Task list", s.plan ? plan : el("p", "fl-empty", "This request has no task list.")),
      s.summary ? block("Where it stands", el("p", "fl-quote soft", s.summary.detail || s.summary.category || "-")) : null,
      gitBlock(s, idx), linkBlock(s, idx)].filter(Boolean));
    const right = el("div", "fl-col");
    right.append(block("Activity", feed), block("Subagents", agents), block("Shell commands", shells),
      block("Last message", el("p", "fl-quote soft", s.lastText || "-")));
    const cols = el("div", "fl-cols");
    cols.append(left, right);
    const keep = sheet.scrollTop;
    const ai = aiSession(s.id);
    sheet.replaceChildren(...[navRow(crumbsFor(s, idx)), head(eyebrow, s.title, openLinks(s)), questionBlock(s),
      ai && ai.why ? aiNote(`${ai.needsUser ? "Needs you: " : ""}${ai.why}`) : null, facts, dials, cols].filter(Boolean));
    sheet.scrollTop = keep;
  }
  function renderClosed(sheet, n, idx) {
    const eyebrow = el("div", "fl-eyebrow");
    eyebrow.append(projLink(n.project), toolTag(n), chipFor("Closed"));
    const facts = el("div", "fl-facts");
    const fact = (k, v) => { const f = el("span"); f.append(document.createTextNode(`${k} `), v); facts.append(f); };
    const last = el("b");
    agoAt(last, n.lastAt);
    fact("Last active", last);
    fact("Model", el("b", null, n.model || "-"));
    fact("Started", el("b", null, n.createdAt ? new Date(n.createdAt).toLocaleString([], {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "-"));
    const sm = n.summary;
    const left = el("div", "fl-col");
    left.append(block("Where it ended", sm ? el("p", "fl-quote", sm.detail || sm.category || "-")
      : el("p", n.lastText ? "fl-quote soft" : "fl-empty", n.lastText || "No summary recorded.")),
    block("Last report", n.report ? el("p", "fl-quote soft", n.report) : el("p", "fl-empty", "No report recorded.")));
    const right = el("div", "fl-col");
    right.append(gitBlock(n, idx), linkBlock(n, idx));
    const cols = el("div", "fl-cols");
    cols.append(left, right);
    const ai = aiSession(n.id);
    sheet.replaceChildren(...[navRow(crumbsFor(n, idx)), head(eyebrow, n.title, openLinks(n)),
      ai && ai.why ? aiNote(`${ai.needsUser ? "Needs you: " : ""}${ai.why}`) : null, facts,
      el("p", "fl-empty", "Nothing is running for this session. Open it to pick it back up."), cols].filter(Boolean));
  }

  const openFolds = new Set();
  function branchLabel(b) {
    let label = BRANCH_LABEL[b.state] || b.state;
    if (b.state === "unmerged" && b.ahead) label = `not merged / ${plural(b.ahead, "commit")} ahead`;
    if (b.state === "open_pr" && b.pr) label = `PR #${b.pr.n} open`;
    if (b.state === "merged" && b.pr) label = `merged / #${b.pr.n}`;
    if (b.state === "deleted") label = b.pr && b.pr.state === "CLOSED" ? `deleted / PR #${b.pr.n} closed` : "deleted / closed out";
    if (b.dirty && b.state !== "merged") label += ` / ${plural(b.dirty, "uncommitted file")}`;
    return label;
  }
  function branchRow(b, idx) {
    const r = el("div", "fl-brow");
    r.append(el("div", "fl-bn", b.name), el("div", `fl-bs ${b.state}`, branchLabel(b)));
    const meta = el("div", "fl-bm");
    meta.append(el("span", null, `into ${b.base || "main"}`));
    if (b.pr && safeUrl(b.pr.url)) meta.append(ext("fl-ext", `Pull request #${b.pr.n}`, b.pr.url));
    (b.sessions || []).forEach((sid) => { const n = idx.byId.get(sid); if (n) meta.append(btn("fl-lk", n.title, () => go(sid))); });
    r.append(meta);
    return r;
  }
  function fold(key, title, rows) {
    const det = el("details", "fl-fold");
    det.append(el("summary", null, title));
    det.open = openFolds.has(key);
    det.addEventListener("toggle", () => { if (det.open) openFolds.add(key); else openFolds.delete(key); });
    rows.forEach((r) => det.append(r));
    return det;
  }
  function branchList(branches, idx, key) {
    const box = el("div", "fl-brs");
    const todo = branches.filter((b) => ["open_pr", "unmerged", "fresh", "unknown"].includes(b.state));
    const merged = branches.filter((b) => b.state === "merged"), gone = branches.filter((b) => b.state === "deleted");
    if (todo.length) todo.forEach((b) => box.append(branchRow(b, idx)));
    else box.append(el("p", "fl-empty", "Nothing left to merge."));
    if (merged.length) box.append(fold(`${key}:m`, `Merged (${merged.length})`, merged.map((b) => branchRow(b, idx))));
    if (gone.length) box.append(fold(`${key}:d`, `Deleted, closed out (${gone.length})`, gone.map((b) => branchRow(b, idx))));
    return box;
  }
  function renderFeature(sheet, f, idx) {
    const eyebrow = el("div", "fl-eyebrow");
    eyebrow.append(projLink(f.project), famStateChip(f));
    const facts = el("div", "fl-facts");
    const fact = (k, v) => { const x = el("span"); x.append(el("b", null, v), document.createTextNode(` ${k}`)); facts.append(x); };
    fact(f.members.length === 1 ? "session" : "sessions", String(f.members.length));
    if (f.branchCount) { fact("not merged", String(f.needsWork)); fact("merged", String(f.merged)); fact("deleted", String(f.deleted)); }
    const ses = el("div", "fl-list");
    f.members.map((id) => idx.byId.get(id)).filter(Boolean).sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0))
      .forEach((n) => ses.append(sessionRow(n, n.parentId ? [el("span", null, n.link === "fork" ? "forked" : "started from another")] : [])));
    const fai = aiFeature(f.id);
    sheet.replaceChildren(...[navRow([[f.project, `proj:${f.project}`]]), head(eyebrow, f.title),
      fai ? aiNote(`${fai.label || ""}${fai.next ? ` Next: ${fai.next}` : ""}`) : null, facts,
      block("Sessions in this feature", ses),
      block("Branches", f.branches.length ? branchList(f.branches, idx, f.id) : el("p", "fl-empty", "No branches recorded for this feature."))].filter(Boolean));
  }

  // ----- project: worktrees, branches, instances, its timeline
  const LABEL_CLS = { "Live": "live", "In use": "use", "Uncommitted changes": "dirty",
                      "Unlanded commits": "unlanded", "Landed, can remove": "landed",
                      "No commits, can remove": "fresh" };
  function aheadText(x) {
    if (x.ahead == null) return "-";
    return `${x.ahead} ahead${x.behind ? ` / ${x.behind} behind` : ""}`;
  }
  function worktreeTable(repo, idx) {
    const t = el("div", "fl-table");
    const hdr = el("div", "fl-tr fl-th");
    ["Worktree", "Branch", "Status", "vs base", "Changes", "Last commit", "Sessions"].forEach((h) => hdr.append(el("span", null, h)));
    t.append(hdr);
    (repo.worktrees || []).forEach((w) => {
      const r = el("div", "fl-tr");
      const name = el("span", "fl-mono", w.kind === "main" ? `${w.name} (main checkout)` : w.name);
      name.title = w.path;
      const lab = el("span", `fl-label l-${LABEL_CLS[w.label] || "unknown"}`, w.label);
      const ses = el("span");
      (w.sessions || []).forEach((sid) => { const n = idx.byId.get(sid); if (n) ses.append(btn("fl-lk", n.title, () => go(sid))); });
      if (w.instance) ses.append(ext("fl-ext", `Vira :${w.instance.port}`, w.instance.url));
      const last = el("span");
      if (w.lastAt) { agoAt(last, w.lastAt); last.title = w.subject || ""; } else last.textContent = "-";
      r.append(name, el("span", "fl-mono", w.branch || (w.detached ? "detached" : "-")), lab,
        el("span", "fl-num", aheadText(w)), el("span", "fl-num", w.dirty == null ? "-" : w.dirty ? `${w.dirty} files` : "clean"),
        last, ses);
      t.append(r);
    });
    return t;
  }
  function branchTable(repo) {
    const t = el("div", "fl-table");
    const hdr = el("div", "fl-tr fl-th b5");
    ["Branch", "Status", "vs base", "Last commit", "Subject"].forEach((h) => hdr.append(el("span", null, h)));
    t.append(hdr);
    (repo.branches || []).forEach((b) => {
      const r = el("div", "fl-tr b5");
      const last = el("span");
      agoAt(last, b.lastAt);
      r.append(el("span", "fl-mono", b.name), el("span", `fl-label l-${LABEL_CLS[b.label] || "unknown"}`, b.label),
        el("span", "fl-num", aheadText(b)), last, el("span", "fl-dim", b.subject || ""));
      t.append(r);
    });
    if (!(repo.branches || []).length) t.append(el("p", "fl-empty", "Every local branch has a worktree."));
    return t;
  }
  function renderProject(sheet, name, d, idx) {
    const repo = (S.repos && S.repos.repos || []).find((r) => r.name === name);
    const fams = (d.families || []).filter((f) => f.project === name);
    const nodes = idx.all.filter((n) => n.project === name);
    const eyebrow = el("div", "fl-eyebrow");
    eyebrow.append(el("span", null, "Project"));
    const title = head(eyebrow, name);
    title.querySelector("h2").style.color = pc(name);
    const open = nodes.filter((n) => n.live);
    const openList = el("div", "fl-list");
    open.forEach((n) => openList.append(sessionRow(n)));
    if (!open.length) openList.append(el("p", "fl-empty", "No sessions open in this project."));
    const parts = [navRow(), title];
    if (aiProject(name)) parts.push(aiNote(aiProject(name).status));
    if (repo) {
      const facts = el("div", "fl-facts");
      const f = (k, v) => { const x = el("span"); x.append(document.createTextNode(`${k} `), el("b", null, v)); facts.append(x); };
      f("Folder", repo.root);
      f("Base", repo.base || "-");
      f("Worktrees", String(repo.worktrees.length));
      f("Other branches", String(repo.branchCount || 0));
      parts.push(facts);
      if (repo.note) parts.push(el("p", "fl-empty", repo.note));
    }
    parts.push(block("Open sessions", openList));
    if (repo) {
      parts.push(block("Worktrees", worktreeTable(repo, idx)));
      const insts = (S.data.instances || []).filter((i) => i.worktree && i.worktree.startsWith(repo.root));
      if (insts.length) {
        const l = el("div", "fl-list");
        insts.forEach((i) => l.append(instanceRow(i)));
        parts.push(block("Vira instances", l));
      }
      parts.push(block(`Branches without a worktree${repo.branchCount > repo.branches.length ? ` (newest ${repo.branches.length} of ${repo.branchCount})` : ""}`, branchTable(repo)));
      const prs = (repo.prs || []).filter((p) => p.state === "OPEN");
      const prl = el("div", "fl-list");
      prs.forEach((p) => {
        const it = el("div", "fl-item");
        const a = el("div", "a");
        a.append(safeUrl(p.url) ? ext("fl-ext", `#${p.n} ${p.title}`, p.url) : document.createTextNode(`#${p.n} ${p.title}`));
        it.append(a, el("div", "b fl-mono", p.branch || ""));
        prl.append(it);
      });
      if (!prs.length) prl.append(el("p", "fl-empty", repo.prNote || "No open pull requests."));
      parts.push(block("Open pull requests", prl));
    } else {
      parts.push(el("p", "fl-empty", S.repos ? "Not a git repository Fleet can read." : "Reading the repository..."));
    }
    const features = fams.filter((f) => f.kind === "feature" && f.branchCount);
    if (features.length) {
      const box = el("div", "fl-col");
      features.forEach((f) => {
        const g = el("div", "fl-fgroup");
        const h4 = el("h4");
        h4.append(btn("fl-lk", f.title, () => go(`fam:${f.id}`)), famStateChip(f));
        g.append(h4, branchList(f.branches, idx, f.id));
        box.append(g);
      });
      parts.push(block("Branches, by feature", box));
    }
    if (repo) {
      const tl = el("div", "fl-col");
      renderTimeline(tl, { ...S, project: name }, true);
      parts.push(block("Timeline", tl));
    }
    sheet.replaceChildren(...parts);
  }
  function renderList(sheet, key, idx) {
    const L = LISTS[key];
    const rows = idx.all.filter((n) => passes(n, S) && L.test(n)).sort((a, b) => (b.lastAt || 0) - (a.lastAt || 0));
    const list = el("div", "fl-list");
    rows.forEach((n) => {
      const extra = [];
      if (key === "needs") extra.push(el("span", null, questionText(n.question)));
      if (key === "working" && n.now) extra.push(el("span", null, `${n.now.name}: ${n.now.desc}`));
      if (key === "agents") n.agents.live.forEach((a) => extra.push(el("span", null, `Agent: ${a.what}`)));
      if (key === "shells") n.shells.forEach((x) => extra.push(el("span", "fl-mono", x.cmd)));
      list.append(sessionRow(n, extra));
    });
    if (!rows.length) list.append(el("p", "fl-empty", "None right now."));
    sheet.replaceChildren(navRow(), head(null, L.title), list);
  }
  function instanceRow(i) {
    const it = el("div", "fl-item");
    const a = el("div", "a");
    a.append(ext("fl-ext", `localhost:${i.port}`, i.url), document.createTextNode(` ${i.kind === "primary" ? "Live" : i.id || "branch"}${i.self ? " (this one)" : ""}`));
    const b = el("div", "b");
    b.textContent = i.startedAt ? `up ${up(now() - i.startedAt)}` : "";
    it.append(a, b, el("div", "c fl-mono", i.worktree || ""));
    return it;
  }
  function renderServer(sheet, pid, d) {
    const s = (d.servers || []).find((x) => String(x.pid) === pid);
    if (!s) { sheet.replaceChildren(navRow(), head(null, "Server stopped"), el("p", "fl-empty", "This process is no longer running.")); return; }
    const facts = el("div", "fl-facts");
    const fact = (k, v) => { const x = el("span"); x.append(document.createTextNode(`${k} `), el("b", null, v)); facts.append(x); };
    fact("Folder", s.folder || "-");
    fact("PID", String(s.pid));
    fact("Up", up(now() - s.startedAt));
    fact("CPU", `${s.cpu}%`);
    fact("Memory", `${s.memMB} MB`);
    const links = el("div", "fl-links");
    if (s.port) links.append(ext("fl-open", `Open localhost:${s.port}`, `http://localhost:${s.port}`));
    sheet.replaceChildren(navRow(), head(null, s.port ? `:${s.port} / ${s.folder || "server"}` : `PID ${s.pid}`, links), facts,
      block("Command", el("p", "fl-quote fl-mono", s.cmd)),
      el("p", "fl-empty", "Started by a session that has since moved on. Stop it from the session that owns the folder."));
  }
  function renderInstance(sheet, port, d) {
    const i = (d.instances || []).find((x) => String(x.port) === port);
    if (!i) { sheet.replaceChildren(navRow(), head(null, "Instance stopped"), el("p", "fl-empty", "Nothing answers on that port now.")); return; }
    const l = el("div", "fl-list");
    l.append(instanceRow(i));
    const work = el("div", "fl-list");
    (i.work || []).forEach((w) => {
      const it = el("div", "fl-item");
      const a = el("div", "a");
      a.append(el("b", null, w.title));
      const b = el("div", "b", workText(w));
      it.append(a, b, el("div", "c", w.kind || ""));
      work.append(it);
    });
    sheet.replaceChildren(navRow(), head(null, `Vira on :${i.port}`, (() => { const w = el("div", "fl-links"); w.append(ext("fl-open", "Open it", i.url)); return w; })()),
      l, block("Running inside it", (i.work || []).length ? work : el("p", "fl-empty", "Nothing beyond the request handlers right now.")),
      el("p", "fl-empty", i.kind === "primary" ? "The live Vira." :
        "A branch instance. Stop it with scripts/branch.sh stop <slug> once the branch is decided."));
  }

  // ---------- the timeline ----------
  function renderTimeline(box, f, compact) {
    const days = landingDays(S.repos, f, compact ? 30 : 60, now());
    const max = Math.max(1, ...days.map((d) => d.claude + d.codex + d.other + d.direct));
    const chart = el("div", "fl-bars");
    chart.setAttribute("role", "img");
    chart.setAttribute("aria-label", "Landings per day");
    const tip = el("div", "fl-tip");
    tip.hidden = true;
    days.forEach((d) => {
      const col = btn(`fl-bar${S.tl.day === d.day ? " on" : ""}`, null, () => {
        S.tl.day = S.tl.day === d.day ? null : d.day;
        render();
      });
      ["direct", "other", "codex", "claude"].forEach((k) => {
        if (!d[k]) return;
        const s = el("i", `k-${k}`);
        s.style.height = `${(d[k] / max) * 100}%`;
        col.append(s);
      });
      const total = d.claude + d.codex + d.other + d.direct;
      col.setAttribute("aria-label", `${dayLabel(d.day)}: ${plural(total, "landing")}`);
      col.addEventListener("mouseenter", () => {
        tip.hidden = !total;
        tip.replaceChildren(el("b", null, `${dayLabel(d.day)} / ${plural(total, "landing")}`),
          ...d.titles.slice(0, 12).map((t) => el("div", null, t)),
          ...(d.titles.length > 12 ? [el("div", "fl-dim", `and ${d.titles.length - 12} more`)] : []));
      });
      col.addEventListener("mouseleave", () => { tip.hidden = true; });
      chart.append(col);
    });
    const legend = el("div", "fl-legend");
    [["claude", "Claude"], ["codex", "Codex"], ["other", "Other branch"], ["direct", "Direct commit"]]
      .forEach(([k, label]) => { const s = el("span"); s.append(el("i", `k-${k}`), document.createTextNode(label)); legend.append(s); });
    const tools = el("div", "fl-tl-tools");
    const q = el("input", "fl-search");
    q.type = "search";
    q.placeholder = "Search landings and the change log";
    q.value = S.tl.q;
    q.addEventListener("input", () => { S.tl.q = q.value; renderTimelineList(list, f); });
    tools.append(seg("Timeline shows", [["all", "Everything"], ["landed", "Landed"], ["log", "Change log"]], S.tl.mode,
      (v) => { S.tl.mode = v; render(); }), q);
    if (S.tl.day) tools.append(btn("fl-navbtn quiet", `${dayLabel(S.tl.day)} only - show all days`, () => { S.tl.day = null; render(); }));
    const list = el("div", "fl-tl-list");
    renderTimelineList(list, f);
    if (!S.repos) { box.replaceChildren(el("p", "fl-empty", "Reading git history...")); return; }
    box.replaceChildren(el("div", "fl-chartwrap"), legend, tools, list);
    box.children[0].append(chart, tip);
  }
  function renderTimelineList(list, f) {
    const rows = timelineRows(S.repos, f, S.tl).slice(0, 400);
    const groups = new Map();
    rows.forEach((r) => { const k = dayKey(r.at); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); });
    const out = [];
    for (const [k, rs] of groups) {
      const g = el("div", "fl-tl-day");
      g.append(el("h4", null, `${dayLabel(k)} / ${plural(rs.filter((r) => r.kind === "landing").length, "landing")}`));
      rs.forEach((r) => {
        const it = el("div", `fl-tl-row k-${r.kind}`);
        if (r.kind === "landing") {
          const l = r.l;
          const a = el("div", "a");
          a.append(el("i", `fl-sq k-${l.tool}`));
          if (l.pr && l.pr.n) a.append(l.pr.url && safeUrl(l.pr.url) ? ext("fl-ext fl-mono", `#${l.pr.n}`, l.pr.url) : el("span", "fl-mono", `#${l.pr.n}`), document.createTextNode(" "));
          a.append(document.createTextNode((l.pr && l.pr.title) || l.subject));
          const b = el("div", "b fl-num");
          b.append(el("span", "fl-plus", `+${l.insertions}`), document.createTextNode(" "), el("span", "fl-minus", `-${l.deletions}`));
          it.append(a, b, el("div", "c fl-mono", `${r.repo}${l.branch ? ` / ${l.branch}` : ""} / ${plural(l.files, "file")}`));
        } else if (r.kind === "change") {
          const a = el("div", "a");
          if (r.c.section) a.append(el("span", "fl-tag", r.c.section));
          a.append(el("b", null, r.c.headline));
          it.append(a, el("div", "b fl-dim", "CHANGELOG"));
          if (r.c.detail) it.append(el("div", "c", r.c.detail));
        } else {
          const a = el("div", "a");
          a.append(el("span", "fl-tag", r.e.kind || "log"), document.createTextNode(r.e.text));
          it.append(a, el("div", "b fl-dim", r.e.source || ""));
        }
        g.append(it);
      });
      out.push(g);
    }
    if (!out.length) out.push(el("p", "fl-empty", "Nothing in this range."));
    list.replaceChildren(...out);
  }

  // ---------- the last 7 days ----------
  function renderWeek(box, d) {
    if (!S.repos) { box.replaceChildren(el("p", "fl-empty", "Reading the week...")); return; }
    const t = now();
    const bars = weekBars(S.repos.week, S, t);
    const axis = el("div", "fl-week-axis");
    const start = weekStart(t);
    for (let i = 0; i < 7; i++) {
      const k = dayKey(start + i * DAY + 3600e3);
      const s = el("span", null, dayLabel(k));
      axis.append(s);
    }
    const byProject = new Map();
    bars.forEach((b) => { if (!byProject.has(b.project)) byProject.set(b.project, []); byProject.get(b.project).push(b); });
    const idx = index(d);
    const groups = [...byProject.entries()].sort((a, b) => b[1].length - a[1].length).map(([p, rows]) => {
      const g = el("div", "fl-week-group");
      const h = el("h4", null, p);
      h.style.color = pc(p);
      g.append(h);
      rows.forEach((w) => {
        const r = el("div", "fl-week-row");
        const label = idx.byId.has(w.id) ? btn("fl-lk fl-week-name", w.title, () => go(w.id)) : el("span", "fl-week-name", w.title);
        const track = el("div", "fl-week-track");
        const bar = el("i", `t-${w.tool}${w.live ? " live" : ""}`);
        bar.style.left = `${(w.x * 100).toFixed(2)}%`;
        bar.style.width = `${(w.w * 100).toFixed(2)}%`;
        bar.title = `${TOOL[w.tool]}: ${w.title}\n${new Date(w.start).toLocaleString()} to ${w.live ? "now" : new Date(w.end).toLocaleString()}`;
        track.append(bar);
        r.append(label, track);
        g.append(r);
      });
      return g;
    });
    if (!groups.length) groups.push(el("p", "fl-empty", "No sessions in the last 7 days."));
    const legend = el("div", "fl-legend");
    Object.entries(TOOL).forEach(([k, label]) => { const s = el("span"); s.append(el("i", `t-${k}`), document.createTextNode(label)); legend.append(s); });
    const head2 = el("div", "fl-week-head");
    head2.append(el("span"), axis);
    box.replaceChildren(legend, head2, ...groups);
  }

  // ---------- background calls ----------
  function renderCalls(box) {
    if (!S.repos) { box.replaceChildren(el("p", "fl-empty", "Counting background calls...")); return; }
    const rows = callRows(S.repos.calls, S);
    const t = el("div", "fl-table calls");
    const hdr = el("div", "fl-tr fl-th c5");
    ["Folder", "Kind", "Today", "7 days", "Last call"].forEach((h) => hdr.append(el("span", null, h)));
    t.append(hdr);
    rows.forEach((c) => {
      const r = el("div", "fl-tr c5");
      const cell = (n, w) => { const s = el("span", "fl-cellbar"); const i = el("i"); i.style.width = `${(w * 100).toFixed(1)}%`; s.append(i, el("b", "fl-num", String(n))); return s; };
      const last = el("span");
      agoAt(last, c.last);
      const folder = el("span", "fl-mono", c.folder);
      folder.title = c.cwd || "";
      r.append(folder, el("span", null, c.kind), cell(c.today, c.todayW), cell(c.week, c.weekW), last);
      t.append(r);
    });
    if (!rows.length) t.append(el("p", "fl-empty", "No headless model calls in the last 7 days."));
    box.replaceChildren(el("p", "fl-note", "One-prompt calls made through the Claude Agent SDK (sdk-cli, sdk-py), counted per folder and what they were for. They are not sessions; a Vira job's own transcript is never counted here."), t);
  }

  // ---------- bottom strip ----------
  // A row of in-process work (runtimework): "857 of 916 / Hilary..." or just
  // the elapsed time when the pass has no counter.
  function workText(w) {
    const bits = [];
    if (w.total) bits.push(`${w.done || 0} of ${w.total}`);
    else if (w.done) bits.push(String(w.done));
    if (w.step) bits.push(w.step);
    if (w.startedAt) bits.push(up(now() - w.startedAt));
    return bits.join(" / ");
  }
  function workPill(i, w) {
    const c = btn("fl-srv fl-work", null, () => go(`inst:${i.port}`), `${w.title} inside Vira on :${i.port}`);
    c.append(el("b", null, w.title), el("span", null, workText(w)));
    return c;
  }
  function renderBottom(box, d) {
    const parts = [];
    // Work running inside a Vira process is otherwise invisible: no session,
    // no shell. Every instance's runtimework rows show here, the live one
    // included, and a pill opens that instance.
    const working = (d.instances || []).filter((i) => (i.work || []).length);
    if (working.length) {
      parts.push(el("span", "fl-lbl", "Inside Vira"));
      working.forEach((i) => (i.work || []).forEach((w) => parts.push(workPill(i, w))));
    }
    const inst = (d.instances || []).filter((i) => !i.self || i.kind !== "primary");
    if (inst.length) {
      parts.push(el("span", "fl-lbl", "Vira instances"));
      inst.forEach((i) => {
        const runFor = i.startedAt ? now() - i.startedAt : 0;
        const c = btn(`fl-srv${i.kind !== "primary" && runFor > 6 * 3600e3 ? " old" : ""}`, null, () => go(`inst:${i.port}`), i.worktree || "");
        c.append(el("b", null, `:${i.port}`), el("span", null, `${i.kind === "primary" ? "live" : (i.branch || i.id || "branch")}${runFor ? ` / up ${up(runFor)}` : ""}`));
        parts.push(c);
      });
    }
    if ((d.servers || []).length) {
      parts.push(el("span", "fl-lbl", "Left running"));
      d.servers.forEach((s) => {
        const runFor = now() - s.startedAt;
        const c = btn(`fl-srv${runFor > 6 * 3600e3 ? " old" : ""}`, null, () => go(`srv:${s.pid}`), s.cmd);
        c.append(el("b", null, s.port ? `:${s.port}` : `PID ${s.pid}`), el("span", null, `${s.folder || "server"} / up ${up(runFor)}`));
        parts.push(c);
      });
    }
    const missing = Object.entries(d.sources || {}).filter(([, v]) => v && !v.found && v.note);
    const gap = el("span", "fl-gap");
    gap.append(btn("fl-lk", `${d.closedTodayCount || 0} closed in the last 24 h`, () => go("list:closed24")),
      document.createTextNode(` / ${d.totals.all} sessions this week`));
    if (missing.length) {
      const m = el("span", "fl-missing", ` / ${missing.map(([, v]) => v.note).join("; ")}`);
      gap.append(m);
    }
    parts.push(gap);
    box.replaceChildren(...parts);
  }

  // ---------- render ----------
  function mount() {
    const root = document.getElementById("fleet-root");
    if (!root || S.mounted) return root;
    S.root = root;
    root.replaceChildren();
    const top = el("div", "fl-top");
    const brand = el("div", "fl-brand");
    const live = el("span", "fl-live");
    live.append(el("i"), el("span", "fl-live-t", "Connecting"));
    brand.append(btn("fl-home", "Fleet", home, "All sessions"), live);
    top.append(brand, el("div", "fl-counts"), el("div", "fl-ai"));
    const controls = el("div", "fl-controls");
    const stage = el("div", "fl-stage");
    const lanes = el("div", "fl-lanes");
    const sheet = el("section", "fl-sheet");
    sheet.hidden = true;
    stage.append(lanes, sheet);
    root.append(top, controls, stage, el("div", "fl-bottom"));
    lanes.append(el("div", "fl-skel"), el("div", "fl-skel"));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && S.view && visible()) back();
    });
    if (typeof ResizeObserver === "function") new ResizeObserver(() => {
      if (!S.view && S.tab === "sessions" && typeof requestAnimationFrame === "function") requestAnimationFrame(() => drawWires(lanes));
    }).observe(stage);
    S.mounted = true;
    return root;
  }

  // A frame re-renders the view under the cursor; whoever was typing in
  // the timeline's search keeps the box, the text and the caret.
  function render() {
    const root = S.root;
    if (!root) return;
    const active = typeof document !== "undefined" && document.activeElement;
    const typing = active && active.classList && active.classList.contains("fl-search")
      ? { start: active.selectionStart, end: active.selectionEnd } : null;
    renderInner(root);
    syncIcons();
    if (typing) {
      const box = root.querySelector(".fl-search");
      if (box) { box.focus(); try { box.setSelectionRange(typing.start, typing.end); } catch (e) { /* not a text box */ } }
    }
  }

  function renderInner(root) {
    const d = S.data;
    const lanes = q1(root, "fl-lanes"), sheet = q1(root, "fl-sheet");
    setLive();
    if (!d) return;
    const idx = index(d);
    assignColors(new Set([...idx.all.map((n) => n.project), ...((S.repos && S.repos.repos) || []).map((r) => r.name)]));
    renderTop(q1(root, "fl-counts"), d);
    renderControls(q1(root, "fl-controls"), d);
    renderBottom(q1(root, "fl-bottom"), d);
    const v = S.view, kind = viewKind(v);
    if (v) {
      lanes.hidden = true;
      sheet.hidden = false;
      const rest = v.slice(v.indexOf(":") + 1);
      if (kind === "session") {
        const cur = idx.byId.get(v);
        if (!cur) { back(); return; }
        if (cur.live) renderSession(sheet, cur, idx); else renderClosed(sheet, cur, idx);
      } else if (kind === "fam") {
        const f = idx.famById.get(rest);
        if (!f) { back(); return; }
        renderFeature(sheet, f, idx);
      } else if (kind === "proj") renderProject(sheet, rest, d, idx);
      else if (kind === "list") renderList(sheet, rest, idx);
      else if (kind === "srv") renderServer(sheet, rest, d);
      else if (kind === "inst") renderInstance(sheet, rest, d);
      return;
    }
    sheet.hidden = true;
    lanes.hidden = false;
    lanes.dataset.tab = S.tab;
    if (S.tab === "timeline") renderTimeline(lanes, S, false);
    else if (S.tab === "week") renderWeek(lanes, d);
    else if (S.tab === "calls") renderCalls(lanes);
    else renderLanes(lanes, d, idx, visibleNodes(d, idx, S, now()));
  }

  // ---------- live connection ----------
  function visible() {
    if (!S.root) return false;
    if (typeof document !== "undefined" && document.hidden) return false;
    return S.root.offsetParent !== null;
  }
  function onFrame(kind, data) {
    if (kind === "fleet") {
      const sent = Date.now();
      S.data = data;
      S.receivedAt = sent;
      S.skew = Math.max(0, sent - data.snapshotAt - 250);
      S.error = null;
      if (S.tab === "sessions" || S.view) render(); else { setLive(); renderTop(q1(S.root, "fl-counts"), S.data); renderBottom(q1(S.root, "fl-bottom"), S.data); }
    } else if (kind === "tick") {
      S.receivedAt = Date.now();
      S.error = null;
      setLive();
    } else if (kind === "fleetrepos") {
      S.repos = data;
      if (S.data) render();
    }
  }
  function connect() {
    if (S.es || typeof EventSource !== "function") return;
    const es = new EventSource("/api/fleet/stream");
    S.es = es;
    es.addEventListener("fleet", (e) => { try { onFrame("fleet", JSON.parse(e.data)); } catch (err) { /* a bad frame waits for the next */ } });
    es.addEventListener("tick", () => onFrame("tick"));
    es.addEventListener("fleetrepos", (e) => { try { onFrame("fleetrepos", JSON.parse(e.data)); } catch (err) { /* next one */ } });
    es.onerror = () => { S.error = "stream"; setLive(); };
  }
  function disconnect() {
    if (S.es) { S.es.close(); S.es = null; }
  }
  // Open while on screen, closed otherwise: closing the stream is what
  // stops the server's collector, so a hidden window costs nothing.
  function watch() {
    if (visible()) connect(); else disconnect();
  }

  async function load() {
    mount();
    if (!S.root) return;
    const saved = store();
    if (!S.loaded) {
      S.loaded = true;
      S.tool = saved.tool || "all";
      S.project = saved.project || "";
      S.mode = saved.mode || "24h";
      S.tab = saved.tab || "sessions";
    }
    connect();
    if (!S.aiLoaded) { S.aiLoaded = true; loadAi(); }
    renderAi();
    if (!S.watch) S.watch = setInterval(watch, 2000);
    if (!S.ticker) S.ticker = setInterval(() => {
      if (!visible()) return;
      S.root.querySelectorAll("[data-since]").forEach((n) => { n.textContent = clock(now() - Number(n.dataset.since)); });
      S.root.querySelectorAll("[data-ago]").forEach((n) => { n.textContent = ago(now() - Number(n.dataset.ago)); });
      setLive();
    }, 1000);
    document.addEventListener("visibilitychange", watch);
    render();
  }

  window.loadFleet = load;
  window.openFleet = (view) => {
    if (typeof openApp === "function") openApp("fleet");
    load().then(() => { if (view) go(view); });
  };
  if (window.__VIRA_TEST__) {
    window.__VIRA_FLEET_TESTS__ = {
      S, realWaiting, index, visibleNodes, laneGroups, counts, timelineRows, landingDays,
      weekBars, weekStart, callRows, liveLabel, viewKind, questionText, onFrame, render, mount,
      renderTop, renderLanes, askStrip, renderCalls, renderWeek, renderTimeline, renderBottom,
      tiles, chips, go, back, home, renderAi, famStateChip, branchLabel, doingStrip,
    };
  }
})();

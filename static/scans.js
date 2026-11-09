/* Scans - paper documents from the scanner, read, named and filed.
   server/scans.py does the work; this file owns the #view-scans surface:
   a strip saying where scans arrive and whether filing is automatic, the
   list of scans newest first, and the open scan on the right - its pages,
   what the reader made of it, where it was filed, and the buttons to file,
   change, undo or read it again. The shell registers the window and calls
   loadScans / openScan; everything else lives here. */
"use strict";

(() => {
  const STATE_LABEL = {
    new: "Waiting to be read", reading: "Reading", ready: "Waiting for you",
    filing: "Filing", filed: "Filed", failed: "Not filed", ignored: "Ignored",
    undone: "Filing undone", missing: "File gone",
  };
  const BUSY = new Set(["new", "reading", "filing"]);
  const S = { data: null, open: null, detail: null, timer: null, editing: false, root: null };

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const btn = (label, cls, fn) => {
    const b = el("button", "fchip sm " + (cls || ""), label);
    b.type = "button";
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(b); });
    return b;
  };
  const day = (iso) => {
    if (!iso) return "";
    // A bare YYYY-MM-DD is a calendar date, not midnight UTC: read it as
    // local, or west of Greenwich it shows the day before.
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(iso);
    return isNaN(d) ? iso.slice(0, 10) : d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  };
  const time = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const titleOf = (it) => (it.decision && it.decision.title) || it.name || it.original_name;
  const note = (host, text, cls) => host.appendChild(el("div", "sc-note " + (cls || ""), text));
  // The reader writes its summary in markdown. Paragraphs and **bold** are
  // all it uses; built as DOM nodes, never innerHTML, since it is model text.
  const prose = (md) => {
    const box = el("div", "sc-prose");
    for (const para of String(md || "").split(/\n\s*\n/)) {
      if (!para.trim()) continue;
      const p = el("p");
      String(para).split(/(\*\*[^*]+\*\*)/).forEach((part) => {
        if (/^\*\*[^*]+\*\*$/.test(part)) p.appendChild(el("strong", "", part.slice(2, -2)));
        else if (part) p.appendChild(document.createTextNode(part));
      });
      box.appendChild(p);
    }
    return box;
  };

  async function act(b, fn, done) {
    if (b) b.disabled = true;
    try {
      const r = await fn();
      if (done) done(r);
      await refresh();
    } catch (e) {
      toast(errText(e));
    } finally {
      if (b) b.disabled = false;
    }
  }

  // ---------------------------------------------------------------- top
  function renderTop(host, d) {
    const top = el("div", "sc-top");
    const where = el("div", "sc-where");
    if (!d.folder) {
      where.appendChild(el("b", "", "Scan filing is off."));
      where.appendChild(el("span", "", " Set a scans folder in Config > Intake to turn it on."));
      const go = btn("Open Config", "", () => { location.hash = "#setup"; });
      top.append(where, go);
      host.appendChild(top);
      return;
    }
    where.appendChild(el("span", "sc-dim", "Scans arrive in "));
    where.appendChild(el("span", "sc-mono", d.folder));
    if (d.vault) where.appendChild(el("span", "sc-dim", ` (vault: ${d.vault.name})`));
    else where.appendChild(el("span", "sc-warn", " - not inside a connected vault, so scans can be read but not filed"));
    if (!d.folder_exists) where.appendChild(el("span", "sc-warn", " - this folder does not exist"));
    const mode = el("div", "sc-mode");
    mode.textContent = d.auto ? "Files scans on its own when the reader is sure"
      : "Waits for you before filing";
    if (!d.automatic_here) mode.textContent = "Another running Vira does automatic work right now, so this one lists scans and reads or files one only when you press its button.";
    const tools = el("div", "sc-tools");
    tools.append(
      btn("Open scanner", "", (b) => act(b, () => post("/api/scans/open-scanner", {}), (r) => {
        if (r.installing) toast("Installing the scanner in your Applications folder. It opens by itself in about a minute.");
      })),
      btn("Look now", "", (b) => act(b, () => post("/api/scans/look", {}),
        () => setTimeout(refresh, 1500))),
    );
    top.append(where, tools);
    host.appendChild(top);
    host.appendChild(mode);
    if (d.last_error) note(host, "The last look failed: " + d.last_error, "warn");
    const inst = d.scanner_install || {};
    if (inst.state === "installing") note(host, "Installing the scanner app. It opens by itself when it is ready.");
    if (inst.state === "failed") note(host, "The scanner app could not be installed: " + inst.error, "warn");
  }

  // ---------------------------------------------------------------- list
  function renderList(host, d) {
    const list = el("div", "sc-list");
    if (!d.items.length) {
      note(list, d.folder ? "No scans yet. Scan a document and it shows up here within a few seconds."
        : "Nothing to show until a scans folder is set.");
    }
    for (const it of d.items) {
      const row = el("div", "sc-row" + (S.open === it.id ? " on" : ""));
      row.dataset.id = it.id;
      const th = el("div", "sc-thumb");
      if (it.has_thumb) {
        const img = el("img");
        img.loading = "lazy";
        img.alt = "";
        img.src = `/api/scans/${it.id}/thumb?v=${encodeURIComponent(it.sha256.slice(0, 8))}`;
        th.appendChild(img);
      } else th.appendChild(el("span", "", it.kind === "pdf" ? "PDF" : "IMG"));
      const body = el("div", "sc-rbody");
      body.appendChild(el("div", "sc-rtitle", titleOf(it)));
      const sub = el("div", "sc-rsub");
      const chip = el("span", "sc-state s-" + it.state, STATE_LABEL[it.state] || it.state);
      sub.appendChild(chip);
      const dd = it.decision && it.decision.document_date;
      sub.appendChild(el("span", "sc-dim", dd ? day(dd) : "scanned " + time(it.arrived)));
      body.appendChild(sub);
      row.append(th, body);
      row.addEventListener("click", () => openScan(it.id));
      list.appendChild(row);
    }
    host.appendChild(list);
  }

  // ---------------------------------------------------------------- detail
  function field(label, value, opts = {}) {
    const wrap = el("label", "sc-field");
    wrap.appendChild(el("span", "sc-flabel", label));
    const input = el(opts.area ? "textarea" : "input");
    input.value = value || "";
    if (opts.area) input.rows = opts.rows || 4;
    input.disabled = !S.editing;
    wrap.appendChild(input);
    return { wrap, input };
  }

  function renderDetail(host, it) {
    const pane = el("div", "sc-detail");
    if (!it) {
      note(pane, "Pick a scan to see its pages, what Vira made of it and where it went.");
      host.appendChild(pane);
      return;
    }
    const d = it.decision || {};
    const head = el("div", "sc-dhead");
    head.appendChild(el("h3", "", titleOf(it)));
    head.appendChild(el("span", "sc-state s-" + it.state, STATE_LABEL[it.state] || it.state));
    pane.appendChild(head);
    const meta = [d.sender, d.document_date && day(d.document_date),
      `${it.pages || "?"} page${it.pages === 1 ? "" : "s"}`, d.document_type].filter(Boolean);
    pane.appendChild(el("div", "sc-dim", meta.join(" - ")));
    if (it.error) note(pane, it.error, "warn");
    if (d.question && it.state === "ready") note(pane, "The reader asks: " + d.question, "ask");
    if (d.confidence === "low" && it.state === "ready") note(pane, "The reader was not sure about this one. Check the name and folders, then file it.", "ask");

    // the buttons for what can happen next
    const bar = el("div", "sc-bar");
    if (it.state === "ready" || it.state === "undone" || (it.state === "failed" && it.decision)) {
      bar.appendChild(btn(it.state === "undone" ? "File again" : "File it", "primary",
        (b) => act(b, () => post(`/api/scans/${it.id}/file`, {}))));
    }
    if (it.decision && it.state !== "filed" && !BUSY.has(it.state)) {
      bar.appendChild(btn(S.editing ? "Stop changing" : "Change", "", () => { S.editing = !S.editing; draw(); }));
    }
    if (it.state === "filed") {
      bar.appendChild(btn("Undo filing", "", (b) => act(b, () => post(`/api/scans/${it.id}/undo`, {}), (r) => {
        if (r.kept && r.kept.length) toast("Undone, but kept: " + r.kept.map((k) => `${k.path} (${k.why})`).join("; "));
        else toast("Filing undone. The scan has its old name back.");
      })));
    }
    if (!BUSY.has(it.state) && it.state !== "filed") {
      bar.appendChild(btn(it.decision ? "Read again" : "Read it", "",
        (b) => act(b, () => post(`/api/scans/${it.id}/read`, {}))));
    }
    if (it.job_id) {
      bar.appendChild(btn("Open the session", "", () => { if (typeof openJob === "function") openJob(it.job_id); }));
    }
    if (!["filed", "ignored", "filing"].includes(it.state)) {
      bar.appendChild(btn("Ignore", "quiet", (b) => act(b, () => post(`/api/scans/${it.id}/ignore`, {}))));
    }
    pane.appendChild(bar);

    const grid = el("div", "sc-dgrid");
    // pages
    const pages = el("div", "sc-pages");
    // Each page as a picture Vira draws: not every browser shows a PDF inline.
    const count = Math.max(1, Math.min(it.pages || 1, 40));
    for (let n = 1; n <= count; n++) {
      const img = el("img", "sc-img");
      img.alt = `Page ${n} of the scan`;
      img.loading = n > 2 ? "lazy" : "eager";
      img.src = `/api/scans/${it.id}/page/${n}?v=${encodeURIComponent(it.sha256.slice(0, 8))}`;
      img.addEventListener("error", () => img.remove());
      pages.appendChild(img);
    }
    const open = el("a", "sc-lk", "Open the file in a new tab");
    open.href = `/api/scans/${it.id}/file`;
    open.target = "_blank";
    open.rel = "noopener";
    pages.appendChild(open);
    grid.appendChild(pages);

    // what the reader made of it
    const info = el("div", "sc-info");
    if (BUSY.has(it.state) && !it.decision) {
      note(info, it.state === "new" ? "Waiting its turn to be read." : "An AI session is reading this scan now. Its answer appears here.");
    }
    if (it.decision) {
      const f = {
        title: field("Title", d.title),
        filename: field("File name", d.filename),
        summary: field("Summary", d.summary, { area: true, rows: 6 }),
      };
      info.append(f.title.wrap, f.filename.wrap);
      if (!S.editing) f.summary.wrap.replaceChildren(el("span", "sc-flabel", "Summary"), prose(d.summary));
      // folders
      info.appendChild(el("div", "sc-flabel", it.state === "filed" ? "Copies saved in" : "Copies go to"));
      const dests = el("div", "sc-dests");
      const chosen = (d.destinations || []).map((x) => ({ ...x }));
      const drawDests = () => {
        dests.replaceChildren();
        if (it.state === "filed" && it.filing) {
          for (const c of it.filing.copies || []) {
            const r = el("div", "sc-dest");
            r.appendChild(el("span", "sc-mono", c.rel));
            dests.appendChild(r);
          }
          for (const s of it.filing.skipped || []) {
            dests.appendChild(el("div", "sc-dest sc-warn", `Not saved in ${s.folder}: ${s.why}`));
          }
          if (!(it.filing.copies || []).length) dests.appendChild(el("div", "sc-dim", "No copies; the scan stays in the scans folder."));
          return;
        }
        if (!chosen.length) dests.appendChild(el("div", "sc-dim", "No folders; the scan stays in the scans folder."));
        chosen.forEach((x, i) => {
          const r = el("div", "sc-dest");
          r.appendChild(el("span", "sc-mono", x.folder));
          if (x.reason) r.appendChild(el("span", "sc-dim", " - " + x.reason));
          if (S.editing) r.appendChild(btn("Remove", "quiet", () => { chosen.splice(i, 1); drawDests(); }));
          dests.appendChild(r);
        });
        if (S.editing) {
          const pick = el("select", "sc-pick");
          pick.appendChild(new Option("Add a folder...", ""));
          for (const fo of it.folders_offered || []) pick.appendChild(new Option(fo, fo));
          pick.addEventListener("change", () => {
            if (pick.value && !chosen.some((x) => x.folder === pick.value)) chosen.push({ folder: pick.value, reason: "chosen by you" });
            drawDests();
          });
          dests.appendChild(pick);
        }
      };
      drawDests();
      info.appendChild(dests);
      info.appendChild(f.summary.wrap);
      if (S.editing) {
        info.appendChild(btn("Save changes", "primary", (b) => act(b, () => put(`/api/scans/${it.id}/decision`, {
          changes: { title: f.title.input.value, filename: f.filename.input.value,
            summary: f.summary.input.value, destinations: chosen },
        }), () => { S.editing = false; })));
      }
      if ((d.key_facts || []).length) {
        info.appendChild(el("div", "sc-flabel", "Key facts"));
        const ul = el("ul", "sc-facts");
        d.key_facts.forEach((k) => ul.appendChild(el("li", "", k)));
        info.appendChild(ul);
      }
      if ((d.actions || []).length) {
        info.appendChild(el("div", "sc-flabel", "To do"));
        const done = new Set(it.done_actions || []);
        d.actions.forEach((a, i) => {
          const r = el("label", "sc-todo" + (done.has(i) ? " done" : ""));
          const cb = el("input");
          cb.type = "checkbox";
          cb.checked = done.has(i);
          cb.disabled = it.state !== "filed";
          cb.addEventListener("change", () => act(null, () => post(`/api/scans/${it.id}/action`, { index: i, done: cb.checked })));
          r.appendChild(cb);
          const t = el("span", "", a.text + (a.due ? ` - due ${day(a.due)}` : ""));
          r.appendChild(t);
          if (a.quote) r.appendChild(el("span", "sc-quote" + (a.grounded ? "" : " sc-warn"),
            a.grounded ? `"${a.quote}"` : `"${a.quote}" (not found in the scan's text${a.due_unverified ? "; its date was left off" : ""})`));
          info.appendChild(r);
        });
        if (it.state !== "filed") info.appendChild(el("div", "sc-dim", "These go on Attention's review list when the scan is filed."));
      }
      if ((d.people || []).length) {
        info.appendChild(el("div", "sc-flabel", "People"));
        info.appendChild(el("div", "", d.people.join(", ")));
      }
      if (it.state === "filed" && it.filing) {
        info.appendChild(el("div", "sc-flabel", "Filed"));
        const fl = el("div", "sc-dim");
        fl.textContent = `${time(it.filing.filed_at)}${it.filing.by_owner ? " by you" : " automatically"}.`
          + (it.filing.renamed_from ? ` Renamed from ${it.filing.renamed_from.split("/").pop()}.` : "");
        info.appendChild(fl);
        if (it.filing.note) {
          const a = el("a", "sc-lk", "Open the summary note");
          a.href = `#library/${encodeURIComponent(it.vault_id)}/${it.filing.note.rel.split("/").map(encodeURIComponent).join("/")}`;
          info.appendChild(a);
        }
      }
    }
    if (it.text) {
      const det = el("details", "sc-text");
      det.appendChild(el("summary", "", `Text read from the scan (${it.text_method})`));
      det.appendChild(el("pre", "", it.text));
      info.appendChild(det);
    }
    grid.appendChild(info);
    pane.appendChild(grid);
    host.appendChild(pane);
  }

  // ---------------------------------------------------------------- draw
  function draw() {
    const root = S.root || document.getElementById("scans-root");
    if (!root || !S.data) return;
    S.root = root;
    root.replaceChildren();
    renderTop(root, S.data);
    const body = el("div", "sc-body");
    renderList(body, S.data);
    renderDetail(body, S.detail && S.detail.id === S.open ? S.detail : null);
    root.appendChild(body);
  }

  async function refresh() {
    S.data = await api("/api/scans");
    if (!S.open && S.data.items.length) S.open = S.data.items[0].id;
    if (S.open) {
      try { S.detail = await api(`/api/scans/${S.open}`); } catch { S.detail = null; }
    }
    if (!S.editing) draw();
    schedule();
  }

  function visible() {
    const v = document.getElementById("view-scans");
    return v && v.offsetParent !== null && document.visibilityState === "visible";
  }

  function schedule() {
    clearTimeout(S.timer);
    const busy = S.data && S.data.items.some((i) => BUSY.has(i.state));
    S.timer = setTimeout(() => {
      if (visible()) refresh().catch(() => schedule());
      else schedule();
    }, busy ? 4000 : 20000);
  }

  async function openScan(id) {
    S.open = id;
    S.editing = false;
    try { S.detail = await api(`/api/scans/${id}`); } catch (e) { toast(errText(e)); }
    draw();
  }

  window.loadScans = () => refresh();
  window.openScan = (id) => { openApp("scans"); return refresh().then(() => id && openScan(id)); };
})();

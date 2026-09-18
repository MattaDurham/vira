/* Work results: one inventory, two views and one stable detail inspector.
 * Source modules own action permissions. Polls update cards by identity while
 * leaving the search field, scroll position and open inspector untouched. */
(function () {
  "use strict";
  let current = null;
  let pendingRef = null;
  const PAGE = 60;
  const e = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = String(text);
    return node;
  };
  const button = (text, action, cls = "") => {
    const node = e("button", "wr-button " + cls, text);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  const safeUrl = (value) => {
    try {
      const url = new URL(value, location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch (_) { return ""; }
  };
  const link = (text, url) => {
    const node = e("a", "wr-button", text);
    node.href = safeUrl(url) || "#";
    node.target = "_blank";
    node.rel = "noopener noreferrer";
    return node;
  };
  const request = async (url, body) => {
    const res = await fetch(url, body == null ? { cache: "no-store" } : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed (" + res.status + ")");
    return data;
  };
  const label = (status) => ({ saved: "Saved", processed: "Saved and indexed", processing: "Indexing", session: "Session live", unlanded: "Ready for review",
    landed: "Merged; cleanup available", running: "Working", waiting: "Needs your input",
    done: "Complete", error: "Failed", orphaned: "Interrupted", ship: "Shipped",
    dropped: "Dropped", unknown: "Status not recorded" }[status] || status || "Recorded");
  const dateLabel = (value) => value ? new Date(value).toLocaleString([], {
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }) : "Date not recorded";
  const refId = (ref) => typeof ref === "string" ? ref :
    ref?.branch ? "branch:" + ref.branch : ref?.job_id ? "job:" + ref.job_id :
    ref?.flow_id ? "flow:" + ref.flow_id : ref?.session_id ? "session:" + ref.session_id : ref?.id;

  function mount(container, options = {}) {
    if (!container) throw new Error("Work results needs a container");
    if (current && current.container === container) {
      Object.assign(current.options, options);
      if (options.view) current.setView(options.view);
      current.refresh();
      return current;
    }
    current?.destroy();
    const state = { container, options, items: [], view: options.view || "timeline", query: "",
      module: "all", shown: PAGE, data: {}, nodes: new Map(), signatures: new Map(),
      loading: false, disposed: false, panel: null, timer: null, selected: "" };
    const shell = e("section", "work-results");
    const toolbar = e("div", "wr-toolbar");
    const search = e("input", "wr-search");
    search.type = "search";
    search.placeholder = "Find work, a branch, or a result";
    search.setAttribute("aria-label", "Search work results");
    search.addEventListener("input", () => { state.query = search.value; state.shown = PAGE; render(); });
    const views = e("div", "wr-views");
    views.setAttribute("role", "group");
    views.setAttribute("aria-label", "Results view");
    const timeline = button("Timeline", () => setView("timeline"));
    const gallery = button("Gallery", () => setView("gallery"));
    views.append(timeline, gallery);
    const modules = e("select", "wr-module");
    modules.setAttribute("aria-label", "Filter by subject");
    modules.append(new Option("All subjects", "all"));
    modules.addEventListener("change", () => { state.module = modules.value; state.shown = PAGE; render(); });
    const refreshButton = button("Refresh", () => refresh(true));
    toolbar.append(search, views, modules, refreshButton);
    const status = e("div", "wr-status", "Loading work results...");
    status.setAttribute("role", "status");
    const errors = e("div", "wr-errors");
    const list = e("div", "wr-list");
    const more = button("Show more", () => { state.shown += PAGE; render(); }, "wr-more");
    more.hidden = true;
    shell.append(toolbar, status, errors, list, more);
    container.replaceChildren(shell);

    function setView(view) {
      state.view = view === "gallery" ? "gallery" : "timeline";
      render();
      options.onViewChange?.(state.view);
    }

    function previewAction(item) {
      const info = item.branch_info || {};
      const instance = info.instance || {};
      if (instance.alive && instance.port) {
        return link("Open preview", location.protocol + "//" + location.hostname + ":" + Number(instance.port) + "/");
      }
      const launch = button(info.serving?.status === "starting" ? "Preview starting..." : "Launch preview", () => {
        const origin = state.data.passive ? location.protocol + "//" + location.hostname + ":8377" : location.origin;
        window.open(origin + "/showroom-launch.html?branch=" + encodeURIComponent(item.branch), "_blank", "noopener");
      });
      launch.disabled = info.serving?.status === "starting";
      return launch;
    }

    function card(item) {
      const node = e("article", "wr-card");
      node.dataset.resultId = item.id;
      const openButton = button(item.title, () => open(item.id), "wr-card-title");
      if (item.preview_url) {
        const imageButton = button("", () => open(item.id), "wr-image-button");
        imageButton.setAttribute("aria-label", "Inspect " + item.title);
        const image = e("img", "wr-preview");
        image.src = item.preview_url;
        image.alt = "Preview from this branch";
        image.loading = "lazy";
        image.addEventListener("error", () => imageButton.remove(), { once: true });
        imageButton.append(image);
        node.append(imageButton);
      }
      const content = e("div", "wr-card-content");
      const meta = e("div", "wr-meta");
      meta.append(e("span", "wr-state", label(item.status)), e("time", "", dateLabel(item.updated_at)));
      content.append(meta, openButton);
      if (item.summary) content.append(e("p", "wr-summary", item.summary));
      const provenance = [item.module !== "other" ? item.module : "", item.branch,
        item.job_ids?.length ? item.job_ids.length + " session" + (item.job_ids.length === 1 ? "" : "s") : "",
        item.flow_ids?.length ? item.flow_ids.length + " flow" + (item.flow_ids.length === 1 ? "" : "s") : ""].filter(Boolean);
      if (provenance.length) content.append(e("div", "wr-provenance", provenance.join(" · ")));
      const actions = e("div", "wr-card-actions");
      if (item.can_preview) actions.append(previewAction(item));
      actions.append(button("Inspect", () => open(item.id)));
      content.append(actions);
      node.append(content);
      return node;
    }

    function render() {
      if (state.disposed) return;
      list.className = "wr-list wr-" + state.view;
      timeline.setAttribute("aria-pressed", String(state.view === "timeline"));
      gallery.setAttribute("aria-pressed", String(state.view === "gallery"));
      const query = state.query.trim().toLocaleLowerCase();
      const filtered = state.items.filter((item) => (state.module === "all" || item.module === state.module)
        && (!query || [item.title, item.summary, item.branch, item.module, item.status]
          .join(" ").toLocaleLowerCase().includes(query)));
      const visible = filtered.slice(0, state.shown);
      const ids = new Set(visible.map((item) => item.id));
      // Search is outside this reconciled list. Unchanged cards keep their DOM
      // identity and focus; an active control is never replaced under a hand.
      for (const [id, node] of state.nodes) if (!ids.has(id)) { node.remove(); state.nodes.delete(id); }
      list.querySelectorAll(".wr-empty").forEach((node) => node.remove());
      let position = list.firstElementChild;
      for (const item of visible) {
        const signature = JSON.stringify(item);
        let node = state.nodes.get(item.id);
        if (!node || (state.signatures.get(item.id) !== signature && !node.contains(document.activeElement))) {
          const next = card(item);
          if (node) node.replaceWith(next);
          if (position === node) position = next;
          node = next;
          state.nodes.set(item.id, node);
          state.signatures.set(item.id, signature);
        }
        if (node !== position) list.insertBefore(node, position);
        position = node.nextElementSibling;
      }
      if (!visible.length) list.append(e("p", "wr-empty", state.items.length
        ? "No work matches these filters." : "No work has been recorded yet."));
      status.textContent = (state.data.fixture ? "Example data · " : "")
        + filtered.length + " result" + (filtered.length === 1 ? "" : "s")
        + (filtered.length !== state.items.length ? " of " + state.items.length : "")
        + (state.data.last_sweep ? " · Branch inventory checked " + dateLabel(state.data.last_sweep) : "");
      more.hidden = visible.length >= filtered.length;
      more.textContent = "Show more (" + (filtered.length - visible.length) + " remaining)";
      errors.replaceChildren();
      for (const [source, message] of Object.entries(state.data.errors || {}))
        errors.append(e("div", "wr-error", source + " unavailable: " + message));
      if (state.data.flow_limit) errors.append(e("div", "wr-error", "Showing the latest " + state.data.flow_limit + " flows."));
    }

    async function refresh(sweep = false) {
      if (state.loading || state.disposed) return;
      state.loading = true;
      refreshButton.disabled = true;
      const scroll = container.scrollTop;
      try {
        if (sweep && !state.data.fixture) await request("/api/showroom/refresh", {});
        const data = await request("/api/work/results");
        if (state.disposed) return;
        state.data = data;
        state.items = data.items || [];
        const tags = [...new Set(state.items.map((item) => item.module || "other"))].sort();
        const tagSignature = tags.join("\n");
        if (modules.dataset.signature !== tagSignature) {
          modules.replaceChildren(new Option("All subjects", "all"), ...tags.map((tag) => new Option(tag, tag)));
          modules.value = tags.includes(state.module) ? state.module : "all";
          state.module = modules.value;
          modules.dataset.signature = tagSignature;
        }
        render();
        container.scrollTop = scroll;
        if (state.panel) {
          const selected = state.items.find((item) => item.id === state.selected);
          const changed = !selected || JSON.stringify(selected) !== state.panel.dataset.signature;
          const notice = state.panel.querySelector(".wr-detail-update");
          if (notice) notice.hidden = !changed;
        }
        if (pendingRef) { const ref = pendingRef; pendingRef = null; open(ref); }
      } catch (error) {
        errors.replaceChildren(e("div", "wr-error", "Results could not refresh: " + error.message));
      } finally {
        state.loading = false;
        refreshButton.disabled = false;
      }
    }

    function closePanel() {
      state.panel?.remove();
      state.panel = null;
      state.selected = "";
      document.removeEventListener("keydown", panelKey, true);
      state.returnFocus?.focus?.({ preventScroll: true });
    }

    function panelKey(event) {
      if (event.key === "Escape") { event.stopPropagation(); closePanel(); }
      if (event.key !== "Tab" || !state.panel) return;
      const nodes = [...state.panel.querySelectorAll("button:not([disabled]), a[href], input, select, textarea, [tabindex='0']")]
        .filter((node) => !node.hidden && node.getClientRects().length);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }

    function section(body, name, text) {
      if (!text) return;
      const node = e("section", "wr-detail-section");
      node.append(e("h3", "", name), typeof text === "string" ? e("div", "wr-pre", text) : text);
      body.append(node);
    }

    async function open(ref) {
      const identity = refId(ref);
      if (!identity) return;
      const item = state.items.find((row) => (row.aliases || [row.id]).includes(identity));
      closePanel();
      state.returnFocus = document.activeElement;
      state.selected = item?.id || identity;
      const backdrop = e("div", "wr-detail-backdrop");
      const panel = e("section", "wr-detail");
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-label", "Work result details");
      const header = e("header", "wr-detail-header");
      const title = e("h2", "", item?.title || "Work details");
      const close = button("Close", closePanel);
      header.append(title, close);
      const update = button("This work changed. Refresh details", () => open(state.selected), "wr-detail-update");
      update.hidden = true;
      const body = e("div", "wr-detail-body", "Reading the source records...");
      panel.append(header, update, body);
      backdrop.append(panel);
      backdrop.dataset.signature = JSON.stringify(item);
      backdrop.addEventListener("click", (event) => { if (event.target === backdrop) closePanel(); });
      document.body.append(backdrop);
      state.panel = backdrop;
      document.addEventListener("keydown", panelKey, true);
      close.focus({ preventScroll: true });
      try {
        const detail = await request("/api/work/results/detail?id=" + encodeURIComponent(identity));
        if (state.panel !== backdrop) return;
        const row = detail.item;
        state.selected = row.id;
        backdrop.dataset.signature = JSON.stringify(row);
        title.textContent = row.title;
        body.replaceChildren();
        body.append(e("div", "wr-meta", label(row.status) + " · " + dateLabel(row.updated_at)));
        if (row.summary) body.append(e("p", "wr-detail-summary", row.summary));
        const actions = e("div", "wr-detail-actions");
        if (row.can_preview) actions.append(previewAction(row));
        if (row.branch_info?.pr?.url) actions.append(link("Pull request #" + row.branch_info.pr.number, row.branch_info.pr.url));
        if (row.branch_info?.instance?.alive && !state.data.passive)
          actions.append(button("Stop preview", () => action("/api/showroom/stop", { branch: row.branch }, actions)));
        if (row.branch_info?.band === "landed" && !state.data.passive && !row.action_limit)
          actions.append(button("Clean up branch", () => confirmCleanup(actions, row)));
        body.append(actions);
        if (row.action_limit) body.append(e("p", "wr-error", row.action_limit));
        if (row.orphan) {
          if (state.options.renderBranchActions) state.options.renderBranchActions(actions, row.orphan);
          else if (state.options.onReviewBranch) actions.append(button("Review branch", () => {
            closePanel(); state.options.onReviewBranch(row.orphan);
          }));
        }
        if (row.preview_url) {
          const image = e("img", "wr-detail-preview"); image.src = row.preview_url; image.alt = "Branch preview";
          image.addEventListener("error", () => image.replaceWith(e("p", "wr-error", "This branch preview is no longer available.")), { once: true });
          section(body, "Preview", image);
        }
        const context = detail.branch_context || {};
        const orphan = context.orphan || {};
        const failure = row.orphan?.failure || row.branch_info?.failure;
        section(body, "Failure", failure ? [failure.headline, failure.why, failure.fix].filter(Boolean).join("\n\n") : "");
        section(body, "What was asked", orphan.objective || orphan.prompt || row.branch_info?.asked);
        section(body, "Branch", [row.branch, row.branch_info?.worktree,
          row.branch_info?.tip ? "Commit " + row.branch_info.tip : "",
          context.disk_mb != null ? context.disk_mb + " MB on disk" : ""].filter(Boolean).join("\n"));
        section(body, "Commits", (orphan.commits || []).map((commit) => [commit.sha, commit.subject, commit.body].filter(Boolean).join("\n")).join("\n\n") || context.merge);
        section(body, "Changed files", (orphan.changed_files || context.merge_paths || []).join("\n"));
        section(body, "Working tree", orphan.status);
        section(body, "Branch report", orphan.report);
        section(body, "Pull request", context.pr?.body);
        for (const job of detail.jobs || []) {
          const jobNode = e("div", "wr-job");
          jobNode.append(e("div", "wr-meta", label(job.status) + " · " + dateLabel(job.finished || job.started)));
          if (state.options.onOpenSession) jobNode.append(button("Open session", () => {
            closePanel(); state.options.onOpenSession(job.id);
          }));
          if (job.transcript) {
            const source = e("div", "wr-transcript");
            source.append(e("code", "", job.transcript), button("Copy transcript path", async (event) => {
              try { await navigator.clipboard.writeText(job.transcript); event.currentTarget.textContent = "Copied"; }
              catch (_) { source.append(e("span", "wr-error", "Select the path to copy it.")); }
            }));
            jobNode.append(source);
          }
          if (job.prompt) section(jobNode, "Prompt", job.prompt);
          if (job.result) section(jobNode, "Result", job.result);
          if (job.judge) section(jobNode, "Review", [job.judge.grade, job.judge.summary, job.judge.recommendation].filter(Boolean).join("\n\n"));
          section(body, job.title || "Session " + job.id, jobNode);
        }
        for (const flow of detail.flows || []) {
          const flowNode = e("div", "wr-flow");
          if (state.options.onOpenFlow) flowNode.append(button("Open flow", () => {
            closePanel(); state.options.onOpenFlow(flow.id);
          }));
          for (const [id, stage] of Object.entries(flow.stages || {}))
            section(flowNode, id + " · " + label(stage.status), stage.result_text || stage.feedback || stage.error || "No result recorded yet.");
          section(body, flow.circuit_name || "Flow", flowNode);
        }
        if (row.receipt) {
          const receipt = row.receipt;
          section(body, "Filing", [receipt.summary, receipt.reason,
            receipt.destination, receipt.vault_id, receipt.path || receipt.saved_path,
            receipt.error].filter(Boolean).join("\n\n"));
          const filingActions = e("div", "wr-detail-actions");
          if (state.options.onOpenReceipt) filingActions.append(button("Original correspondence", () => {
            closePanel(); state.options.onOpenReceipt(receipt.id || receipt.key);
          }));
          const savedPath = receipt.path || receipt.saved_path;
          if (savedPath && !row.fixture && state.options.onOpenNote) filingActions.append(button("Read saved note", () => {
            closePanel(); state.options.onOpenNote(savedPath, row.title);
          }));
          body.append(filingActions);
          state.options.renderReceipt?.(body, receipt);
        }
        if (row.related_ids?.length) {
          const related = e("div", "wr-detail-actions");
          for (const id of row.related_ids) {
            const target = state.items.find((entry) => entry.id === id);
            related.append(button(target?.title || id, () => open(id)));
          }
          section(body, "Related work", related);
        }
        if (row.record) section(body, "Record source", [row.record.text, row.record.retro].filter(Boolean).join("\n\n"));
        for (const [source, error] of Object.entries(detail.errors || {})) section(body, source + " unavailable", error);
      } catch (error) {
        if (state.panel === backdrop) body.textContent = "Could not read this work: " + error.message;
      }
    }

    async function action(url, payload, target) {
      const notice = e("span", "wr-action-status", "Working...");
      target.append(notice);
      try {
        const result = await request(url, payload);
        notice.textContent = result.stopped === false ? (result.output || "The preview could not stop.") : "Done.";
        await refresh();
      } catch (error) { notice.textContent = error.message; notice.className = "wr-error"; }
    }

    function confirmCleanup(target, row) {
      if (target.querySelector(".wr-confirm")) return;
      const confirm = e("div", "wr-confirm");
      confirm.append(e("p", "", "Remove this merged branch, its worktree and its remote branch? The merge stays on main."),
        button("Cancel", () => confirm.remove()), button("Confirm cleanup", () => {
          confirm.querySelectorAll("button").forEach((node) => { node.disabled = true; });
          action("/api/showroom/cleanup", { branch: row.branch }, confirm);
        }));
      target.append(confirm);
    }

    state.refresh = refresh;
    state.setView = setView;
    state.open = open;
    state.destroy = () => { state.disposed = true; state.timer?.stop?.(); clearInterval(state.timer); closePanel(); };
    current = state;
    render();
    refresh();
    // The host uses its shared poll helper; the fallback keeps this module
    // independently previewable. Closed/hidden surfaces make no requests.
    const poll = () => { if (container.isConnected && container.getClientRects().length) refresh(); };
    state.timer = options.startPoll ? options.startPoll(poll, 8000) : setInterval(poll, 8000);
    return state;
  }

  window.ViraWorkResults = { mount,
    open(ref) { if (current) return current.open(ref); pendingRef = ref; },
    setView(view) { current?.setView(view); },
    refresh() { return current?.refresh(); },
  };
})();

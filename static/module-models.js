// Module defaults share the server's routing registry and the live model catalog.
// A choice is saved locally; opening this control never starts a model call.
window.ModuleModels = (() => {
  let modules = [];
  const key = (choice) => choice ? JSON.stringify({
    provider: choice.provider, backend: choice.backend, model: choice.model || "",
  }) : "";
  const label = (choice) => choice
    ? [choice.provider, choice.model || "provider default"].filter(Boolean).join(" · ")
    : "App default";
  const transport = (backend) => backend === "cli" ? "Subscription / CLI" : "API key";
  const moduleFor = (id) => modules.find((m) => m.id === id || (m.windows || []).includes(id));

  function choices(cat, module) {
    const rows = [];
    for (const p of cat.providers || []) {
      if (p.disabled || !p.connected || (module.kind === "session" && !p.sessions)) continue;
      for (const backend of ["cli", "api"]) {
        if (module.supported_backends && !module.supported_backends[p.id]?.includes(backend)) continue;
        if (!module.supported_backends && module.kind === "session" && backend !==
            (["anthropic", "openai"].includes(p.id) ? "cli" : "api")) continue;
        if (backend === "api" ? !p.has_key : p.auth !== "signed_in") continue;
        const group = `${p.label} · ${transport(backend)}`;
        for (const model of p[backend] || []) {
          if (cat.roster?.length && !cat.roster.includes(model.id)) continue;
          const selection = { provider: p.id, backend, model: model.id };
          rows.push({ selection, group, text: model.label && model.label !== model.id
            ? `${model.label} · ${model.id}` : model.id });
        }
        rows.push({ selection: { provider: p.id, backend, model: "" }, group,
          text: "Custom model...", custom: true });
      }
    }
    return rows;
  }

  function paint() {
    document.querySelectorAll("[data-module-model]").forEach((button) => {
      const module = moduleFor(button.dataset.moduleModel);
      if (!module) return;
      const current = label(module.selection || module.effective);
      button.textContent = "Model: " + current;
      button.title = `${module.title}: ${current}${module.selection ? "" : " (app default)"}. Choose model.`;
      button.setAttribute("aria-label", button.title);
    });
  }

  function button(id) {
    const node = el("button", "fchip sm module-model-button", "Model...");
    node.type = "button";
    node.dataset.moduleModel = id;
    node.setAttribute("aria-haspopup", "dialog");
    node.addEventListener("click", () => open(id, node));
    return node;
  }

  function mount() {
    for (const module of modules) {
      for (const id of module.windows || [module.id]) {
        const view = document.getElementById("view-" + id);
        const heads = id === "people" ? (view?.querySelectorAll(".section-head") || [])
          : [view?.querySelector(".section-head")];
        for (const head of heads) {
          if (head && !head.querySelector("[data-module-model]")) head.appendChild(button(id));
        }
      }
    }
    paint();
  }

  async function load() {
    const data = await api("/api/module-models");
    modules = data.modules || [];
    mount();
    return modules;
  }

  async function open(id, anchor, point) {
    closeCtxPops();
    const pop = el("div", "ctx-pop module-model-pop");
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "Choose module model");
    const heading = el("div", "ctx-head", "Choose model");
    const note = el("p", "ctx-note", "Loading models...");
    note.setAttribute("role", "status");
    pop.appendChild(heading);
    pop.appendChild(note);
    const rect = anchor?.getBoundingClientRect();
    placeCtxPop(pop, Math.max(8, point?.x ?? rect?.left ?? 16),
      Math.max(8, point?.y ?? rect?.bottom ?? 64));
    pop.tabIndex = -1;
    pop.focus();
    const dismiss = () => { pop.remove(); anchor?.focus(); };
    pop.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.stopPropagation(); dismiss(); }
    });
    try {
      const [cat] = await Promise.all([modelCatalog(), load()]);
      if (!pop.isConnected) return;
      if (cat.error) throw new Error(cat.error);
      const module = moduleFor(id);
      if (!module) throw new Error("No model setting is registered for this module.");
      heading.textContent = module.title + " model";
      note.textContent = module.description || "Used for this module's next model request.";
      const field = el("label", "field", "Model");
      const select = el("select");
      select.setAttribute("aria-label", module.title + " model");
      field.appendChild(select);
      pop.appendChild(field);
      let catalog = cat;
      let selection = module.selection;
      const fill = () => {
        select.innerHTML = "";
        const option = el("option", null, "Use app default · " + label(module.default || module.effective));
        option.value = "";
        select.appendChild(option);
        const groups = new Map();
        const available = choices(catalog, module);
        for (const row of available) {
          if (!groups.has(row.group)) {
            const group = el("optgroup");
            group.label = row.group;
            groups.set(row.group, group);
            select.appendChild(group);
          }
          const option = el("option", null, row.text);
          option.value = (row.custom ? "custom:" : "") + key(row.selection);
          groups.get(row.group).appendChild(option);
        }
        if (selection && !available.some((row) => !row.custom && key(row.selection) === key(selection))) {
          const saved = el("option", null, label(selection) + " (saved - unverified)");
          saved.value = key(selection);
          select.appendChild(saved);
        }
        select.value = key(selection);
      };
      fill();
      select.addEventListener("change", () => {
        if (select.value.startsWith("custom:")) {
          const custom = JSON.parse(select.value.slice(7));
          const model = (prompt("Model id, exactly as the provider names it:") || "").trim();
          if (model) selection = { ...custom, model };
          fill();
        } else selection = select.value ? JSON.parse(select.value) : null;
      });
      const detail = el("p", "ctx-note",
        "Saved for this module across reloads. Explicit choices for individual runs take priority.");
      pop.appendChild(detail);
      const error = el("div", "module-model-error");
      error.setAttribute("role", "alert");
      pop.appendChild(error);
      const actions = el("div", "row-end");
      const refresh = el("button", "btn small", "Refresh models");
      const cancel = el("button", "btn small", "Cancel");
      const save = el("button", "btn small primary", "Save");
      cancel.addEventListener("click", dismiss);
      refresh.addEventListener("click", async () => {
        refresh.disabled = save.disabled = true;
        error.textContent = "";
        try {
          const fresh = await modelCatalog(true);
          if (fresh.error) throw new Error(fresh.error);
          catalog = fresh;
          fill();
        } catch (e) { error.textContent = "Could not refresh models: " + e.message; }
        finally { refresh.disabled = save.disabled = false; }
      });
      save.addEventListener("click", async () => {
        if (save.disabled) return;
        save.disabled = refresh.disabled = select.disabled = true;
        error.textContent = "";
        try {
          const url = "/api/module-models/" + encodeURIComponent(module.id);
          if (selection) await put(url, selection);
          else await api(url, { method: "DELETE" });
          await load();
          toast(module.title + " model saved");
          dismiss();
        } catch (e) {
          error.textContent = "Could not save model: " + e.message;
          save.disabled = refresh.disabled = select.disabled = false;
        }
      });
      actions.appendChild(refresh);
      actions.appendChild(cancel);
      actions.appendChild(save);
      pop.appendChild(actions);
      // The loaded controls can be taller than the initial loading message.
      const bounds = pop.getBoundingClientRect();
      pop.style.top = Math.max(8, Math.min(bounds.top, innerHeight - bounds.height - 8)) + "px";
      select.focus();
    } catch (e) {
      note.textContent = "Could not load models: " + e.message;
      const retry = el("button", "btn small", "Retry");
      retry.addEventListener("click", () => open(id, anchor, point));
      pop.appendChild(retry);
    }
  }

  function contextItem(target, x, y) {
    const id = target.closest(".fwin")?.dataset.wid
      || target.closest(".view")?.id?.replace(/^view-/, "")
      || (target.closest("#person-panel, #email-panel, #group-panel") ? "people" : "");
    const module = moduleFor(id);
    return module ? { label: "Choose model...", hint: label(module.selection || module.effective),
      run: () => open(module.id, null, { x, y }) } : null;
  }

  return { load, open, button, contextItem, choices,
    selectionFor: (id) => moduleFor(id)?.selection || null };
})();

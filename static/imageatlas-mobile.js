(() => {
  "use strict";

  const panels = [
    {
      id: "hud",
      label: "Atlas search and help",
      icon: '<circle cx="10.5" cy="10.5" r="5.75"></circle><path d="m15 15 4 4"></path>',
    },
    {
      id: "legend",
      label: "Atlas topics",
      icon: '<rect x="4" y="4" width="5" height="5" rx="1"></rect><rect x="15" y="4" width="5" height="5" rx="1"></rect><rect x="4" y="15" width="5" height="5" rx="1"></rect><rect x="15" y="15" width="5" height="5" rx="1"></rect>',
    },
  ];

  const controls = document.createElement("div");
  controls.id = "vira-atlas-mobile-controls";
  controls.setAttribute("aria-label", "Atlas controls");

  // A launcher is only minted for a panel that is actually in the document.
  // A button opening nothing is worse than an absent one, and the viewer is
  // chaska's — an id can retire there without this file being touched.
  const entries = [];
  for (const spec of panels) {
    const panel = document.getElementById(spec.id);
    if (!panel) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "vira-atlas-mobile-toggle";
    button.setAttribute("aria-label", spec.label);
    button.setAttribute("aria-controls", spec.id);
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${spec.icon}</svg>`;
    controls.appendChild(button);
    entries.push({ panel, button });
  }
  if (!entries.length) return;

  function closeAll(except) {
    for (const entry of entries) {
      const open = entry === except;
      entry.panel.classList.toggle("vira-mobile-open", open);
      entry.button.setAttribute("aria-expanded", String(open));
    }
  }

  for (const entry of entries) {
    entry.button.addEventListener("click", () => {
      const open = !entry.panel.classList.contains("vira-mobile-open");
      closeAll(open ? entry : null);
      if (open) {
        const focusable = entry.panel.querySelector("input:not([disabled]), button:not([disabled])");
        focusable?.focus({ preventScroll: true });
      }
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && entries.some((entry) => entry.panel.classList.contains("vira-mobile-open"))) {
      closeAll(null);
    }
  });

  const phone = window.matchMedia("(max-width: 700px)");
  phone.addEventListener?.("change", () => closeAll(null));
  closeAll(null);
  document.body.appendChild(controls);
})();

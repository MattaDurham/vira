// ==================== Reader > Documents — the Shelves view ================
//
// A third way to look at the library, after List (Finder's rows) and Grid
// (the gallery): the shopping-app home page. The page scrolls down through
// titled SECTIONS; each section is a sideways strip of SHELF cards that snaps
// card to card, one card about 80% of the width so the next one peeks in at
// the right edge. A shelf card is a heading with a chevron over a 2x2 of its
// newest documents. Tapping the heading opens that shelf's RESULTS page:
// tabs across the section's other shelves, the Reader's own kind and tag
// filters, and a 2-column grid of every document in the shelf. Back returns
// to the shelves at the same scroll position, down and sideways.
//
// Nothing here invents a grouping. Sections are the Reader's "Group by"
// groups (RDOC_AXES, already filtered by rdocMatches); the shelves inside a
// section split it once more on a second axis — kind, or the feature tag when
// the Reader is already grouped by kind.
//
// Registration is the whole contract with app.js: RDOC_VIEWS.shelves gives
// rdocBar a "Shelves" button and renderReaderDocs a renderer. The choice is
// kept in the Reader's own `vira-rdoc-view` setting.
//
// No IntersectionObserver: nothing is hidden until it scrolls into view
// (tests/test_reader_grid_reveal.py), and pictures lazy-load natively.

(function () {
  const VIEW = "shelves";
  const TILE_N = 4;   // a shelf card is a 2x2

  // The open results page, or null on the shelves page. Survives re-renders
  // (a filter change re-runs renderReaderDocs, which re-enters render()).
  let shvOpen = null;   // { section: groupKey, shelf: shelfKey | "" (all) }
  // Where the shelves page was when a results page opened.
  let shvSaved = null;  // { top, strips: { sectionKey: scrollLeft } }

  const reduceMotion = () =>
    matchMedia("(prefers-reduced-motion: reduce)").matches;

  // The second axis. Grouped by kind, the shelves split on the feature tag;
  // grouped by anything else (or flat), they split on kind.
  function subAxis() {
    return rdocGroup === "kind" ? "module" : "kind";
  }

  function subKey(it) {
    if (subAxis() === "kind") return it.kind || RDOC_UNTAGGED;
    const t = rdocTagsOf(it, "module");
    return t.length ? t[0] : RDOC_UNTAGGED;
  }

  function subLabel(key) {
    if (key === RDOC_UNTAGGED) return "Not yet tagged";
    if (subAxis() === "kind") {
      const k = READER_KIND[key] || key;
      return k.charAt(0).toUpperCase() + k.slice(1) + "s";
    }
    return key;
  }

  function sectionLabel(key) {
    return key ? rdocGroupLabel(key) : "The library";
  }

  // A section's shelves: biggest first, untagged last, each newest first.
  function shelvesOf(group) {
    const by = new Map();
    group.items.forEach((it) => {
      const k = subKey(it);
      if (!by.has(k)) by.set(k, []);
      by.get(k).push(it);
    });
    const out = [...by.entries()].map(([key, items]) =>
      ({ key, label: subLabel(key), items: rdocSortNew(items) }));
    out.sort((a, b) => {
      if (a.key === RDOC_UNTAGGED) return 1;
      if (b.key === RDOC_UNTAGGED) return -1;
      return b.items.length - a.items.length || a.label.localeCompare(b.label);
    });
    return out;
  }

  // The nearest ancestor that actually scrolls vertically — the Reader's
  // panel on desktop, the page on a phone. Back restores ITS scrollTop.
  function scroller(node) {
    for (let n = node && node.parentElement; n; n = n.parentElement) {
      const oy = getComputedStyle(n).overflowY;
      if ((oy === "auto" || oy === "scroll") && n.scrollHeight > n.clientHeight) {
        return n;
      }
    }
    return document.scrollingElement || document.documentElement;
  }

  function shortDate(it) {
    const iso = it.created || it.added || "";
    const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
    if (isNaN(d)) return "";
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString(undefined, sameYear
      ? { month: "short", day: "numeric" }
      : { month: "short", day: "numeric", year: "numeric" });
  }

  function kindName(it) {
    return READER_KIND[it.kind] || it.kind || "document";
  }

  function pictureOf(it) {
    return (it.film || {}).thumb || it.thumb || "";
  }

  // One of six tones, fixed per document, so a text tile keeps its colour
  // from render to render and neighbours rarely match.
  function toneOf(it) {
    const s = String(it.id || it.title || "");
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h) % 6;
  }

  // The face of a document: its picture when it has one, else a calendar
  // leaf — month and year as a kicker, the day in the display face, a tag at
  // the foot. Not the title: the title is printed under every tile already.
  // A picture that fails to load turns into the leaf.
  function face(it, cls, avoidTag) {
    const box = el("div", "shv-face " + cls);
    const textFace = () => {
      box.classList.add("text", "tone" + toneOf(it));
      box.classList.remove("loading");
      box.innerHTML = "";
      const iso = it.created || it.added || "";
      const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
      if (isNaN(d)) {
        box.appendChild(el("span", "shv-tkick", "undated"));
        box.appendChild(el("span", "shv-tday", "\u2014"));
      } else {
        box.appendChild(el("span", "shv-tkick", d.toLocaleDateString(undefined,
          { month: "short", year: "numeric" })));
        box.appendChild(el("span", "shv-tday", String(d.getDate())));
      }
      const tags = ["module", "subproject", "theme", "concept"]
        .flatMap((ax) => rdocTagsOf(it, ax)).filter((t) => t !== avoidTag);
      box.appendChild(el("span", "shv-ttag", tags[0] || kindName(it)));
    };
    const src = pictureOf(it);
    if (!src) { textFace(); return box; }
    box.classList.add("loading");
    const img = el("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "";
    img.addEventListener("load", () => box.classList.remove("loading"));
    img.addEventListener("error", textFace);
    img.src = src;
    box.appendChild(img);
    if (it.kind === "walkthrough") box.appendChild(el("span", "shv-play", "film"));
    return box;
  }

  function badge(it) {
    return el("span", "shv-badge k-" + (it.kind || "doc"), kindName(it));
  }

  // ---------------------------------------------------------------- shelves

  // The badge row carries the date only when the face is a picture; a
  // calendar-leaf face already says it.
  function metaRow(it) {
    const meta = el("div", "shv-tmeta");
    meta.appendChild(badge(it));
    const d = pictureOf(it) ? shortDate(it) : "";
    if (d) meta.appendChild(el("span", "shv-date", d));
    return meta;
  }

  function shelfTile(it, sectionKey) {
    const t = el("div", "shv-tile" + (it.missing ? " missing" : ""));
    t.appendChild(face(it, "shv-tface", sectionKey));
    const meta = metaRow(it);
    t.appendChild(meta);
    t.appendChild(el("div", "shv-title", it.title || "Untitled"));
    t.title = it.title || "";
    cardAction(t, () => openReaderItem(it));
    return t;
  }

  function shelfCard(group, shelf) {
    const card = el("article", "shv-card");
    const head = el("button", "shv-chead");
    head.type = "button";
    head.appendChild(el("span", "shv-cname", shelf.label));
    head.appendChild(el("span", "shv-cn", String(shelf.items.length)));
    head.appendChild(el("span", "shv-chev", "›"));
    head.title = "See all " + shelf.items.length + " in " + shelf.label;
    head.addEventListener("click", () => openResults(group.key, shelf.key));
    card.appendChild(head);

    const grid = el("div", "shv-quad");
    shelf.items.slice(0, TILE_N).forEach((it) =>
      grid.appendChild(shelfTile(it, group.key)));
    for (let i = shelf.items.length; i < TILE_N; i++) {
      grid.appendChild(el("div", "shv-tile empty"));
    }
    card.appendChild(grid);
    return card;
  }

  function section(group, idx) {
    const shelves = shelvesOf(group);
    const sec = el("section", "shv-sec" + (idx % 2 ? " tinted" : ""));
    sec.dataset.key = group.key;
    const head = el("header", "shv-sechead");
    head.appendChild(el("h3", "shv-secname", sectionLabel(group.key)));
    head.appendChild(el("span", "shv-secn",
      group.items.length + " document" + (group.items.length === 1 ? "" : "s")));
    sec.appendChild(head);

    const wrap = el("div", "shv-stripwrap");
    const strip = el("div", "shv-strip");
    shelves.forEach((s) => strip.appendChild(shelfCard(group, s)));
    wrap.appendChild(strip);

    // Mouse users have no sideways swipe: arrows step one card. Touch and
    // trackpads never see them (CSS shows them only for a fine pointer).
    if (shelves.length > 1) {
      [["prev", "‹", -1], ["next", "›", 1]].forEach(([c, g, dir]) => {
        const b = el("button", "shv-arrow " + c, g);
        b.type = "button";
        b.setAttribute("aria-label", dir < 0 ? "Previous shelf" : "Next shelf");
        b.addEventListener("click", () => {
          const card = strip.querySelector(".shv-card");
          const step = card ? card.getBoundingClientRect().width + 12 : 300;
          strip.scrollBy({ left: dir * step,
            behavior: reduceMotion() ? "auto" : "smooth" });
        });
        wrap.appendChild(b);
      });
      const edges = () => {
        wrap.classList.toggle("at-start", strip.scrollLeft < 4);
        wrap.classList.toggle("at-end",
          strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 4);
      };
      strip.addEventListener("scroll", edges, { passive: true });
      requestAnimationFrame(edges);
    }
    sec.appendChild(wrap);
    return sec;
  }

  function renderShelves(list, groups) {
    const page = el("div", "shv-page");
    groups.forEach((g, i) => page.appendChild(section(g, i)));
    list.appendChild(page);
    if (shvSaved) {
      const saved = shvSaved;
      shvSaved = null;
      page.querySelectorAll(".shv-sec").forEach((sec) => {
        const left = saved.strips[sec.dataset.key];
        if (left) sec.querySelector(".shv-strip").scrollLeft = left;
      });
      requestAnimationFrame(() => {
        scroller(list).scrollTop = saved.top;
      });
    }
  }

  // ---------------------------------------------------------------- results

  function openResults(sectionKey, shelfKey) {
    const list = $("#reader-list");
    const strips = {};
    if (list) {
      list.querySelectorAll(".shv-sec").forEach((sec) => {
        strips[sec.dataset.key] = sec.querySelector(".shv-strip").scrollLeft;
      });
    }
    shvSaved = { top: list ? scroller(list).scrollTop : 0, strips };
    shvOpen = { section: sectionKey, shelf: shelfKey };
    renderReaderDocs();
    // Start the results page at its own top (scroll-margin in the CSS keeps
    // it clear of a phone's fixed top bar).
    const page = list && list.querySelector(".shv-results");
    if (page) page.scrollIntoView({ block: "start", behavior: "auto" });
  }

  function closeResults() {
    shvOpen = null;
    renderReaderDocs();
  }

  // The "+" on a result card: put one of this document's tags into the
  // Reader's tag filter — the same thing a tag chip does in List view.
  function plusMenu(anchor, it) {
    closeCtxPops();
    const pop = el("div", "ctx-pop rdoc-pop shv-pluspop");
    pop.appendChild(el("div", "shv-pophead", "Add a tag to the filter"));
    const body = el("div", "rdoc-poplist");
    const seen = new Set();
    ["module", "subproject", "theme", "concept"].forEach((ax) => {
      rdocTagsOf(it, ax).forEach((t) => {
        if (seen.has(t)) return;
        seen.add(t);
        const line = el("button", "rdoc-popitem" + (rdocTags.has(t) ? " on" : ""));
        line.type = "button";
        line.appendChild(el("span", "rdoc-poptag", t));
        line.appendChild(el("span", "rdoc-popn", rdocTags.has(t) ? "on" : ax));
        line.addEventListener("click", () => {
          if (rdocTags.has(t)) rdocTags.delete(t); else rdocTags.add(t);
          lsSet("vira-rdoc-tags", [...rdocTags]);
          pop.remove();
          renderReaderDocs();
        });
        body.appendChild(line);
      });
    });
    if (!seen.size) {
      body.appendChild(el("div", "empty left", "This document has no tags yet."));
    }
    pop.appendChild(body);
    const r = anchor.getBoundingClientRect();
    placeCtxPop(pop, Math.max(8, r.right - 240), r.bottom + 4);
  }

  function resultCard(it, sectionKey) {
    const card = el("div", "shv-rcard" + (it.missing ? " missing" : ""));
    const pic = el("div", "shv-rpic");
    pic.appendChild(face(it, "shv-rface", sectionKey));
    const plus = el("button", "shv-plus", "+");
    plus.type = "button";
    plus.title = "Add one of its tags to the filter";
    plus.setAttribute("aria-label", "Add one of its tags to the filter");
    plus.addEventListener("click", (e) => {
      e.stopPropagation();
      plusMenu(plus, it);
    });
    pic.appendChild(plus);
    card.appendChild(pic);

    card.appendChild(metaRow(it));
    card.appendChild(el("div", "shv-rtitle", it.title || "Untitled"));

    const tags = [];
    ["module", "subproject", "theme"].forEach((ax) =>
      rdocTagsOf(it, ax).slice(0, 1).forEach((t) => {
        if (!tags.includes(t)) tags.push(t);
      }));
    const desc = (it.film || {}).description || "";
    const line = desc || tags.join(" · ");
    if (line) card.appendChild(el("div", "shv-rdetail", line));
    card.title = it.title || "";
    cardAction(card, () => openReaderItem(it));
    return card;
  }

  function kindMenu(anchor) {
    closeCtxPops();
    const pop = el("div", "ctx-pop rdoc-pop");
    const body = el("div", "rdoc-poplist");
    ["walkthrough", "plan", "dossier", "retro", "brief"].forEach((k) => {
      const line = el("button", "rdoc-popitem" + (rdocKinds.has(k) ? " on" : ""));
      line.type = "button";
      line.appendChild(el("span", "rdoc-poptag", READER_KIND[k] || k));
      line.addEventListener("click", () => {
        if (rdocKinds.has(k)) rdocKinds.delete(k); else rdocKinds.add(k);
        lsSet("vira-rdoc-kinds", [...rdocKinds]);
        pop.remove();
        renderReaderDocs();
      });
      body.appendChild(line);
    });
    pop.appendChild(body);
    if (rdocKinds.size) {
      const clear = el("button", "rdoc-mini", "Every kind");
      clear.addEventListener("click", () => {
        rdocKinds.clear();
        lsSet("vira-rdoc-kinds", []);
        pop.remove();
        renderReaderDocs();
      });
      pop.appendChild(clear);
    }
    const r = anchor.getBoundingClientRect();
    placeCtxPop(pop, r.left, r.bottom + 4);
  }

  function dropBtn(label, on, run) {
    const b = el("button", "shv-drop" + (on ? " on" : ""));
    b.type = "button";
    b.appendChild(el("span", "", label));
    b.appendChild(el("span", "shv-caret", "▾"));
    b.addEventListener("click", (e) => run(e.currentTarget));
    return b;
  }

  function renderResults(list, groups) {
    const group = groups.find((g) => g.key === shvOpen.section);
    if (!group) { shvOpen = null; return false; }
    const shelves = shelvesOf(group);
    const shelf = shelves.find((s) => s.key === shvOpen.shelf);
    const items = shelf ? shelf.items : rdocSortNew(group.items);

    const page = el("div", "shv-results");
    const top = el("div", "shv-rtop");
    const back = el("button", "shv-back", "‹ " + sectionLabel(group.key));
    back.type = "button";
    back.title = "Back to the shelves";
    back.addEventListener("click", closeResults);
    top.appendChild(back);
    top.appendChild(el("h3", "shv-rname",
      shelf ? shelf.label : "Everything in " + sectionLabel(group.key)));
    top.appendChild(el("span", "shv-secn",
      items.length + " document" + (items.length === 1 ? "" : "s")));
    page.appendChild(top);

    // Tabs: the whole section, then each of its shelves.
    const tabs = el("div", "shv-tabs");
    tabs.setAttribute("role", "tablist");
    const tab = (key, label, n) => {
      const on = (shelf ? shelf.key : "") === key;
      const b = el("button", "shv-tab" + (on ? " on" : ""));
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.appendChild(el("span", "", label));
      b.appendChild(el("span", "shv-tabn", String(n)));
      b.addEventListener("click", () => {
        if (on) return;
        shvOpen = { section: group.key, shelf: key };
        renderReaderDocs();
      });
      tabs.appendChild(b);
      return b;
    };
    tab("", "All", group.items.length);
    shelves.forEach((s) => tab(s.key, s.label, s.items.length));
    page.appendChild(tabs);

    // Filters: the Reader's own kind and tag filters, as dropdowns.
    const filters = el("div", "shv-filters");
    filters.appendChild(dropBtn(
      rdocKinds.size === 0 ? "Kind"
        : rdocKinds.size === 1 ? (READER_KIND[[...rdocKinds][0]] || "Kind")
          : rdocKinds.size + " kinds",
      rdocKinds.size > 0, kindMenu));
    filters.appendChild(dropBtn(
      rdocTags.size === 0 ? "Tags"
        : rdocTags.size === 1 ? [...rdocTags][0] : rdocTags.size + " tags",
      rdocTags.size > 0, (a) => rdocTagPicker(a)));
    if (rdocKinds.size || rdocTags.size) {
      const clear = el("button", "shv-clear", "Clear filters");
      clear.type = "button";
      clear.addEventListener("click", () => {
        rdocKinds.clear();
        rdocTags.clear();
        lsSet("vira-rdoc-kinds", []);
        lsSet("vira-rdoc-tags", []);
        renderReaderDocs();
      });
      filters.appendChild(clear);
    }
    page.appendChild(filters);

    if (!items.length) {
      page.appendChild(el("div", "empty left",
        "Nothing on this shelf matches. Loosen a filter."));
    } else {
      const grid = el("div", "shv-rgrid");
      items.forEach((it) => grid.appendChild(resultCard(it, group.key)));
      page.appendChild(grid);
    }
    list.appendChild(page);
    // Keep the chosen tab in view on a narrow strip.
    const on = tabs.querySelector(".shv-tab.on");
    if (on) tabs.scrollLeft = Math.max(0, on.offsetLeft - 16);
    return true;
  }

  // ---------------------------------------------------------------- entry

  function render(list, groups) {
    if (shvOpen && renderResults(list, groups)) return;
    renderShelves(list, groups);
  }

  // Leaving the view (List or Grid chosen) drops the open results page, so
  // coming back to Shelves starts on the shelves.
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".rdoc-vbtn");
    if (!b || b.textContent === "Shelves") return;
    shvOpen = null;
    shvSaved = null;
  }, true);

  window.RDOC_VIEWS = window.RDOC_VIEWS || {};
  window.RDOC_VIEWS[VIEW] = { label: "Shelves", render };
  if (window.__VIRA_TEST__) {
    window.__VIRA_SHELVES_TESTS__ = { shelvesOf, subAxis, subLabel, shortDate };
  }
})();

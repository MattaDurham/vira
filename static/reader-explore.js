// ==================== Reader > Documents: the Explore view =================
//
// A photo wall, after a social app's Explore tab (owner's screen recording,
// 2026-11-04): a dense three-column mosaic with hairline gaps and square
// corners, every tile the document's picture with no title bar, a small
// count in each tile's bottom-left corner, and one tall tile in every block
// of five. It scrolls endlessly. A tap zooms the document up into a
// full-screen overlay with the wall still dimmed behind it; swipe or the
// arrow keys move to the next document, and back (or Escape) returns to the
// wall at the same scroll position. A note opens as a sideways set of pages
// with a "1/11" counter and dots.
//
// Same data as the Grid: rdocMatches filters, rdocSortNew orders (strict
// newest first, every kind mixed), pictures come from the item's thumb
// (/api/reading/thumb/{id}) or its film poster. A document with no picture
// gets a text tile. Opening a document goes through openReaderItem, the
// Reader's own path.
//
// Kept out of app.js on purpose: the view registers itself in RDOC_VIEWS,
// which the List/Grid switch and renderReaderDocs both read (one line each
// in app.js, the same two lines the Shelves view uses). renderReaderDocs
// draws the bar, the count line and the empty states, then hands this file
// the list node and the groups.

(function () {
  const RX_PAGE = 45;          // tiles per load: nine blocks of five
  const RX_NEAR = 1400;        // px from the bottom that loads the next set
  const RX_PAGE_CHARS = 1300;  // text per note page before it turns over
  const RX_MAX_PAGES = 24;

  let rxItems = [];       // the filtered, sorted list the wall shows
  let rxShown = RX_PAGE;  // how many of them are on the wall
  let rxKey = "";         // the filter the shown count belongs to
  let rxWall = null;      // the grid node
  let rxScroller = null;  // whichever ancestor actually scrolls
  let rxOnScroll = null;
  let rxTop = 0;          // the wall's last scroll position
  let rxOv = null;        // the overlay, while open
  const reduce = () =>
    matchMedia("(prefers-reduced-motion: reduce)").matches;

  window.RDOC_VIEWS = window.RDOC_VIEWS || {};
  window.RDOC_VIEWS.explore = { label: "Explore", render: rxRender };

  // ---------- the wall ----------

  function rxFilterKey() {
    return JSON.stringify([rdocQ, [...rdocTags].sort(), [...rdocKinds].sort()]);
  }

  // One stream, strict newest first, every kind mixed: the groups only
  // decide what matched, never the order.
  function rxRender(list, groups) {
    const key = rxFilterKey();
    const sameView = key === rxKey;
    if (!sameView) { rxKey = key; rxShown = RX_PAGE; rxTop = 0; }
    rxItems = rdocSortNew(groups.flatMap((g) => g.items), true);
    rxDetach();

    rxWall = el("div", "rx-wall" + (reduce() ? "" : " anim"));
    list.appendChild(rxWall);
    const foot = el("div", "rx-foot");
    list.appendChild(foot);
    rxFill(0, Math.min(rxShown, rxItems.length));
    rxFoot();

    rxScroller = rxScrollParent(list);
    // A background refresh re-renders the wall under the reader; put the
    // scroll back where it was so nothing jumps.
    if (sameView && rxScroller && rxTop) rxScroller.scrollTop = rxTop;
    rxOnScroll = () => {
      if (!rxWall || !rxWall.isConnected) { rxDetach(); return; }  // another view now
      if (rxScroller) rxTop = rxScroller.scrollTop;
      rxMaybeMore();
    };
    (rxScroller || window).addEventListener("scroll", rxOnScroll, { passive: true });
    // A short first page may not fill the window: load until it does.
    requestAnimationFrame(rxMaybeMore);
  }

  function rxDetach() {
    if (rxOnScroll) (rxScroller || window).removeEventListener("scroll", rxOnScroll);
    rxOnScroll = null;
  }

  function rxScrollParent(node) {
    for (let n = node; n && n !== document.body; n = n.parentElement) {
      const oy = getComputedStyle(n).overflowY;
      if (oy === "auto" || oy === "scroll") return n;
    }
    return null;
  }

  function rxMaybeMore() {
    if (!rxWall || !rxWall.isConnected || rxShown >= rxItems.length) return;
    const sc = rxScroller;
    const left = sc ? sc.scrollHeight - sc.scrollTop - sc.clientHeight
      : document.documentElement.scrollHeight - scrollY - innerHeight;
    if (left < RX_NEAR) rxMore();
  }

  function rxMore(upTo) {
    const from = rxWall.children.length;
    rxShown = Math.min(rxItems.length,
      Math.max(upTo || 0, from + RX_PAGE));
    rxFill(from, rxShown);
    rxFoot();
  }

  function rxFill(from, to) {
    for (let i = from; i < to; i++) rxWall.appendChild(rxTile(rxItems[i], i));
  }

  // The end of the wall says what it is: more is coming, or that's all.
  function rxFoot() {
    const foot = rxWall && rxWall.nextElementSibling;
    if (!foot) return;
    foot.innerHTML = "";
    const left = rxItems.length - rxWall.children.length;
    if (left > 0) {
      const b = el("button", "rx-more", "Show " + Math.min(left, RX_PAGE)
        + " more of " + left);
      b.addEventListener("click", () => rxMore());
      foot.appendChild(b);
    } else {
      foot.appendChild(el("div", "rx-end", rxItems.length + " items, all shown"));
    }
  }

  // Blocks of five: one tall tile and four small ones, the tall one on the
  // right in even blocks and on the left in odd ones, so the wall reads as a
  // mosaic and a document keeps its place across loads.
  function rxShape(i) {
    const block = Math.floor(i / 5), at = i % 5;
    if (block % 2 === 0) return at === 2 ? "tall-r" : "";
    return at === 0 ? "tall-l" : "";
  }

  function rxPicture(it) {
    const film = it.film || {};
    return film.thumb || it.thumb || "";
  }

  // A date out of the row, short: "Nov 5" this year, "Sep 2025" before.
  function rxShortDate(it) {
    const s = String(it.created || it.added || "");
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return "";
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    const thisYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString(undefined, thisYear
      ? { month: "short", day: "numeric" } : { month: "short", year: "numeric" });
  }

  // A retro's title leads with its date ("2026-09-20 day thedurham-nyc");
  // the date already rides in the corner, so the tile shows the rest.
  function rxTitle(it) {
    const t = String(it.title || "").trim();
    const rest = t.replace(/^\d{4}-\d{2}-\d{2}[\sT_-]*/, "").trim();
    return rest || t || "Untitled";
  }

  // What a document is "about", for its colour and its byline: the first
  // module tag, else its subproject or theme, else its kind.
  function rxTopic(it) {
    for (const ax of ["module", "subproject", "theme", "concept"]) {
      const t = rdocTagsOf(it, ax);
      if (t.length) return t[0];
    }
    return it.kind || "document";
  }

  // Eight tones from Vira's own palette, picked by the topic, so documents
  // about one thing share a colour across the wall.
  const RX_TONES = 8;
  function rxTone(it) {
    const s = rxTopic(it);
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h % RX_TONES;
  }

  const RX_ICON = {
    walkthrough: '<path d="M8 5v14l11-7z"/>',
    plan: '<path d="M4 6h16M4 12h10M4 18h13"/>',
    retro: '<path d="M12 7v5l3 2"/><circle cx="12" cy="12" r="8"/>',
    dossier: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5"/>',
    brief: '<path d="M4 7h16v12H4z"/><path d="M9 7V5h6v2"/>',
    doc: '<path d="M6 3h9l4 4v14H6z"/>',
  };
  function rxSvg(paths, cls) {
    return '<svg class="' + (cls || "rx-ico") + '" viewBox="0 0 24 24" '
      + 'aria-hidden="true">' + paths + "</svg>";
  }

  function rxTile(it, i) {
    const shape = rxShape(i);
    const pic = rxPicture(it);
    const tile = el("button", "rx-tile" + (shape ? " " + shape : "")
      + (pic ? " has-pic" : " text rx-tone" + rxTone(it))
      + (it.missing ? " missing" : ""));
    tile.type = "button";
    tile.dataset.i = String(i);
    tile.setAttribute("aria-label", (READER_KIND[it.kind] || it.kind || "document")
      + ": " + (it.title || ""));
    if (pic) {
      const img = el("img");
      img.src = pic;
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = "";
      img.addEventListener("error", () => {
        // A thumb that went away mid-session falls back to the text face.
        tile.querySelectorAll("img, .rx-face").forEach((n) => n.remove());
        tile.classList.remove("has-pic", "poster");
        tile.classList.add("text", "rx-tone" + rxTone(it));
        rxTextFace(tile, it, shape);
      }, { once: true });
      tile.appendChild(img);
      // A tall tile is three columns' worth of height for one capture of a
      // wide page: cropping it to a sliver reads as nothing. It becomes a
      // poster instead, the whole capture across the top over a blurred
      // fill of itself, and the title underneath.
      if (shape) {
        tile.classList.add("poster");
        const blur = el("img", "rx-blur");
        blur.src = pic; blur.loading = "lazy"; blur.alt = "";
        tile.insertBefore(blur, img);
        const face = el("div", "rx-face");
        face.appendChild(el("div", "rx-kick", READER_KIND[it.kind] || it.kind || "document"));
        face.appendChild(el("div", "rx-ttl big", rxTitle(it)));
        tile.appendChild(face);
      }
      if ((it.film || {}).motion) {
        tile.insertAdjacentHTML("beforeend", rxSvg(RX_ICON.walkthrough, "rx-reel"));
      }
    } else {
      rxTextFace(tile, it, shape);
    }
    const count = el("span", "rx-count");
    count.innerHTML = rxSvg(RX_ICON[rdocKindGlyph(it.kind)] || RX_ICON.doc);
    count.appendChild(document.createTextNode(rxShortDate(it)));
    tile.appendChild(count);
    tile.addEventListener("click", () => rxOpen(i));
    return tile;
  }

  function rxTextFace(tile, it, shape) {
    const face = el("div", "rx-face");
    face.appendChild(el("div", "rx-kick", READER_KIND[it.kind] || it.kind || "document"));
    face.appendChild(el("div", "rx-ttl" + (shape ? " big" : ""), rxTitle(it)));
    const sub = rxTopic(it);
    if (sub && sub !== it.kind) face.appendChild(el("div", "rx-topic", sub));
    tile.insertBefore(face, tile.firstChild);
  }

  // ---------- the overlay ----------

  function rxTileAt(i) {
    return rxWall ? rxWall.querySelector('.rx-tile[data-i="' + i + '"]') : null;
  }

  function rxOpen(i) {
    if (rxOv) { rxGo(i); return; }
    const ov = el("div", "rx-ov");
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    const scrim = el("div", "rx-scrim");
    scrim.addEventListener("click", () => exitFocus(ov));
    ov.appendChild(scrim);
    const card = el("div", "rx-card");
    ov.appendChild(card);
    // Desktop gets arrows beside the card; a phone swipes.
    const prev = el("button", "rx-side prev");
    prev.innerHTML = rxSvg('<path d="M15 5l-7 7 7 7"/>');
    prev.setAttribute("aria-label", "Previous document");
    prev.addEventListener("click", () => rxGo(rxOv.i - 1));
    const next = el("button", "rx-side next");
    next.innerHTML = rxSvg('<path d="M9 5l7 7-7 7"/>');
    next.setAttribute("aria-label", "Next document");
    next.addEventListener("click", () => rxGo(rxOv.i + 1));
    ov.appendChild(prev);
    ov.appendChild(next);
    document.body.appendChild(ov);
    rxOv = { node: ov, card, i, prev, next, seq: 0 };
    document.addEventListener("keydown", rxKeys, true);
    document.body.classList.add("rx-open");
    // The focus stack owns dismissal, so Escape, the back chevron and a
    // click on the scrim all close it the same way, and a document opened
    // from here stacks on top and closes first.
    enterFocus(ov, () => rxClose(ov));
    rxShow(i);
    rxZoom(card, rxTileAt(i), true);
    requestAnimationFrame(() => ov.classList.add("open"));
  }

  function rxClose(ov) {
    document.removeEventListener("keydown", rxKeys, true);
    document.body.classList.remove("rx-open");
    const st = rxOv;
    rxOv = null;
    if (!st) { ov.remove(); return; }
    const tile = rxTileAt(st.i);
    ov.classList.remove("open");
    const done = () => ov.remove();
    if (reduce() || !tile || !rxOnScreen(tile)) {
      setTimeout(done, reduce() ? 0 : 180);
      return;
    }
    rxZoom(st.card, tile, false).then(done, done);
  }

  function rxOnScreen(node) {
    const r = node.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && r.width > 0;
  }

  // The zoom between a tile and the card: a transform from the tile's box
  // to the card's, run forwards to open and backwards to close.
  function rxZoom(card, tile, opening) {
    if (reduce() || !tile || !card.animate) return Promise.resolve();
    const a = tile.getBoundingClientRect(), b = card.getBoundingClientRect();
    if (!a.width || !b.width) return Promise.resolve();
    const sx = a.width / b.width, sy = a.height / b.height;
    const dx = a.left - b.left, dy = a.top - b.top;
    const from = { transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`,
      opacity: 0.4, borderRadius: "0px" };
    const to = { transform: "none", opacity: 1 };
    const anim = card.animate(opening ? [from, to] : [to, from], {
      duration: opening ? 240 : 200,
      easing: "cubic-bezier(.2, .7, .2, 1)",
      fill: opening ? "none" : "forwards",
    });
    return anim.finished;
  }

  function rxKeys(e) {
    // Only while the overlay is the top of the focus stack: a document
    // opened from it sits above, and the arrows are that document's then.
    if (!rxOv || !focusStack.length
        || focusStack[focusStack.length - 1].panel !== rxOv.node) return;
    // No text-field exemption: the overlay is modal and holds no fields, and
    // on desktop Vira keeps a hidden typing box focused at all times, which
    // an exemption would let swallow every arrow press.
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "ArrowRight") { e.preventDefault(); e.stopPropagation(); rxGo(rxOv.i + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); e.stopPropagation(); rxGo(rxOv.i - 1); }
  }

  function rxGo(i) {
    if (!rxOv || i < 0 || i >= rxItems.length || i === rxOv.i) return;
    // Walking past the loaded wall loads it, so closing lands on a tile.
    if (rxWall && i >= rxWall.children.length) rxMore(i + 1);
    const dir = i > rxOv.i ? 1 : -1;
    rxOv.i = i;
    rxShow(i, dir);
  }

  function rxShow(i, dir) {
    const st = rxOv;
    const it = rxItems[i];
    const seq = ++st.seq;
    st.prev.disabled = i <= 0;
    st.next.disabled = i >= rxItems.length - 1;
    const card = st.card;
    card.innerHTML = "";
    card.classList.remove("is-live", "info");
    if (dir && !reduce()) {
      card.classList.remove("slide-l", "slide-r");
      void card.offsetWidth;
      card.classList.add(dir > 0 ? "slide-l" : "slide-r");
    }

    // Top bar: back chevron, the position in the list, details.
    const top = el("div", "rx-top");
    const back = el("button", "rx-ibtn rx-back");
    back.innerHTML = rxSvg('<path d="M15 5l-7 7 7 7"/>');
    back.setAttribute("aria-label", "Back to the wall");
    back.addEventListener("click", () => exitFocus(st.node));
    top.appendChild(back);
    top.appendChild(el("div", "rx-pos", (i + 1) + " of " + rxItems.length));
    const gear = el("button", "rx-ibtn rx-gear");
    gear.innerHTML = rxSvg('<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/>'
      + '<circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>');
    gear.setAttribute("aria-label", "Details");
    gear.addEventListener("click", () => card.classList.toggle("info"));
    top.appendChild(gear);
    card.appendChild(top);

    const stage = el("div", "rx-stage");
    card.appendChild(stage);
    const pic = rxPicture(it);
    const film = it.film || {};
    const live = rxLiveUrl(it);
    if (live) {
      // The page itself, drawn at the card's width. It does not take
      // pointer input (a swipe or the arrow keys must still move between
      // documents); a tap opens it in the Reader's own viewer, scrollable.
      card.classList.add("is-live");
      const media = el("div", "rx-media rx-live");
      if (pic) {
        const img = el("img", "rx-ph");
        img.src = pic; img.alt = "";
        media.appendChild(img);
      }
      const fr = el("iframe");
      fr.src = live;
      fr.title = it.title || "Document";
      fr.tabIndex = -1;
      fr.addEventListener("load", () => media.classList.add("loaded"));
      media.appendChild(fr);
      const hit = el("button", "rx-hit");
      hit.setAttribute("aria-label", "Open the document");
      hit.addEventListener("click", () => openReaderItem(it));
      media.appendChild(hit);
      stage.appendChild(media);
      rxSwipe(stage, null);
    } else if (pic) {
      const media = el("div", "rx-media");
      const img = el("img");
      img.src = pic;
      img.alt = it.title || "";
      media.appendChild(img);
      if (film.motion && it.locator && !reduce()) {
        const v = el("video");
        v.src = it.locator + "motion.mp4";
        v.muted = true; v.loop = true; v.playsInline = true; v.autoplay = true;
        v.addEventListener("playing", () => media.classList.add("playing"));
        v.play().catch(() => {});
        media.appendChild(v);
      }
      media.title = "Open the document";
      media.addEventListener("click", () => openReaderItem(it));
      stage.appendChild(media);
      rxSwipe(stage, null);
    } else if (it.locator_kind === "vault" && !it.missing) {
      stage.appendChild(el("div", "rx-loading", "Opening..."));
      rxSwipe(stage, null);
      rxPages(it).then((pages) => {
        if (!rxOv || rxOv.seq !== seq) return;
        rxCarousel(stage, it, pages);
      });
    } else {
      stage.appendChild(rxPaper(it));
      rxSwipe(stage, null);
    }

    // Right edge: the actions, one column, label under each.
    const rail = el("div", "rx-rail");
    rail.appendChild(rxAct("Open", '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/>'
      + '<path d="M18 14v6H4V6h6"/>', () => openReaderItem(it)));
    const topic = rxTopic(it);
    const tagged = topic && topic !== it.kind;
    if (tagged) {
      rail.appendChild(rxAct("Similar", '<circle cx="11" cy="11" r="6"/>'
        + '<path d="M20 20l-4.5-4.5"/>', () => {
        rdocTags = new Set([topic]);
        lsSet("vira-rdoc-tags", [...rdocTags]);
        exitFocus(st.node);
        renderReaderDocs();
      }));
    }
    rail.appendChild(rxAct("Copy", '<rect x="8" y="8" width="12" height="12" rx="2"/>'
      + '<path d="M16 8V4H4v12h4"/>', async () => {
      const link = it.locator_kind === "url"
        ? location.origin + it.locator : (it.locator || it.title || "");
      try { await copyText(link); toast("Copied " + (it.locator_kind === "url"
        ? "the link" : "the location")); }
      catch (e) { toast("Copy failed: " + errText(e)); }
    }));
    card.appendChild(rail);

    // Bottom: who/what, then the caption.
    const foot = el("div", "rx-cap");
    const by = el("div", "rx-by");
    const av = el("span", "rx-av rx-tone" + rxTone(it));
    av.innerHTML = rxSvg(RX_ICON[rdocKindGlyph(it.kind)] || RX_ICON.doc);
    by.appendChild(av);
    const who = el("div", "rx-who");
    who.appendChild(el("div", "rx-name", rxTitle(it)));
    who.appendChild(el("div", "rx-meta", [READER_KIND[it.kind] || it.kind,
      tagged ? topic : "", rxShortDate(it),
      it.missing ? "no longer where it was saved" : ""].filter(Boolean).join(" · ")));
    by.appendChild(who);
    foot.appendChild(by);
    const capText = film.description || film.subject || "";
    const cap = el("div", "rx-text", capText);
    if (!capText) cap.style.display = "none";
    cap.addEventListener("click", () => cap.classList.toggle("full"));
    foot.appendChild(cap);
    card.appendChild(foot);
    st.cap = cap;

    card.appendChild(rxInfo(it));

    // Warm the neighbours so a swipe lands on a drawn picture.
    [i - 1, i + 1].forEach((j) => {
      const p = rxItems[j] && rxPicture(rxItems[j]);
      if (p) { const im = new Image(); im.src = p; }
    });
  }

  // Which documents can be drawn live: served HTML (a dossier, a connected
  // folder's page). Films keep their poster and loop; notes page through.
  function rxLiveUrl(it) {
    if (it.missing || it.kind === "walkthrough") return "";
    if (it.locator_kind === "url" && it.locator) return it.locator;
    if (it.locator_kind === "file" && /\.html?$/i.test(it.locator || "")) {
      return "/api/reading/file/" + encodeURIComponent(it.id);
    }
    return "";
  }

  function rxAct(label, paths, run) {
    const b = el("button", "rx-act");
    b.innerHTML = rxSvg(paths);
    b.appendChild(el("span", "", label));
    b.addEventListener("click", (e) => { e.stopPropagation(); run(); });
    return b;
  }

  // The details sheet behind the sliders icon.
  function rxInfo(it) {
    const box = el("div", "rx-info");
    const rows = [
      ["Kind", READER_KIND[it.kind] || it.kind],
      ["Made", it.created ? fmtTime(it.created) : ""],
      ["Added", it.added ? fmtTime(it.added) : ""],
      ["Where", it.locator || ""],
    ];
    rows.forEach(([k, v]) => {
      if (!v) return;
      const r = el("div", "rx-irow");
      r.appendChild(el("span", "rx-ik", k));
      r.appendChild(el("span", "rx-iv", v));
      box.appendChild(r);
    });
    const tags = Object.values(it.tags || {}).flat();
    if (tags.length) {
      const tr = el("div", "rx-itags");
      [...new Set(tags)].forEach((t) => tr.appendChild(el("span", "rdoc-chip", t)));
      box.appendChild(tr);
    }
    return box;
  }

  // A document with no picture and no text to page through: a paper card
  // with its title and a way in.
  function rxPaper(it) {
    const p = el("div", "rx-paper solo rx-tone" + rxTone(it));
    p.appendChild(el("div", "rx-kick", READER_KIND[it.kind] || it.kind || "document"));
    p.appendChild(el("div", "rx-ttl big", rxTitle(it)));
    const b = el("button", "rx-openbig", it.missing ? "No longer where it was saved"
      : "Open the document");
    if (!it.missing) b.addEventListener("click", () => openReaderItem(it));
    p.appendChild(b);
    return p;
  }

  // A note, read through the vault route and cut into pages at its headings
  // (and wherever a section runs long), so it reads as a set of slides.
  async function rxPages(it) {
    let html = "";
    let firstText = "";
    try {
      const n = await api("/api/vault/note?path=" + encodeURIComponent(it.locator));
      // The frontmatter is the note's filing card, not its first page.
      const body = String(n.text || "").replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
      html = mdToHtml(body, it.locator);
    } catch (e) {
      return { pages: [], err: errText(e) };
    }
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    const pages = [];
    let cur = null, chars = 0;
    const turn = () => { cur = el("div", "rx-pagebody"); pages.push(cur); chars = 0; };
    [...tmp.childNodes].forEach((node) => {
      const tag = node.nodeName;
      const len = (node.textContent || "").length;
      // The last allowed page keeps everything that is left; it scrolls.
      const room = pages.length < RX_MAX_PAGES;
      if (!cur || (room && chars > 0
          && (/^H[12]$/.test(tag) || chars + len > RX_PAGE_CHARS))) turn();
      cur.appendChild(node);
      chars += len;
      if (!firstText && tag === "P") firstText = (node.textContent || "").trim();
    });
    return { pages: pages.filter((p) => p.textContent.trim()), firstText };
  }

  function rxCarousel(stage, it, res) {
    stage.innerHTML = "";
    if (!res.pages.length) {
      stage.appendChild(rxPaper(it));
      if (res.err) toast("Note unavailable: " + res.err);
      rxSwipe(stage, null);
      return;
    }
    if (res.firstText && rxOv && rxOv.cap && rxOv.cap.style.display === "none") {
      rxOv.cap.textContent = res.firstText;
      rxOv.cap.style.display = "";
    }
    const n = res.pages.length;
    const track = el("div", "rx-track");
    res.pages.forEach((body, k) => {
      const page = el("div", "rx-page");
      if (k === 0) {
        page.appendChild(el("div", "rx-kick dark",
          (READER_KIND[it.kind] || it.kind || "document")
          + (rxShortDate(it) ? " · " + rxShortDate(it) : "")));
        // The note's own H1 usually repeats its title; one is enough.
        const h1 = body.querySelector("h1");
        if (!h1 || h1 !== body.firstElementChild) {
          page.appendChild(el("h1", "rx-pagetitle", rxTitle(it)));
        } else h1.classList.add("rx-pagetitle");
      }
      // Links inside a page would leave the overlay; the Open action is the
      // way into the full note.
      body.querySelectorAll("a").forEach((a) => a.removeAttribute("href"));
      page.appendChild(body);
      track.appendChild(page);
    });
    stage.appendChild(track);
    if (n > 1) {
      const pill = el("div", "rx-pill", "1/" + n);
      stage.appendChild(pill);
      const dots = el("div", "rx-dots");
      const shown = Math.min(n, 7);
      for (let k = 0; k < shown; k++) dots.appendChild(el("span", "rx-dot"));
      stage.appendChild(dots);
      const mark = () => {
        const k = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
        pill.textContent = (k + 1) + "/" + n;
        // Seven dots at most; the lit one slides along like the app's.
        const lit = n <= 7 ? k : Math.min(6, Math.max(0,
          k < 3 ? k : (k > n - 4 ? 6 - (n - 1 - k) : 3)));
        [...dots.children].forEach((d, j) => d.classList.toggle("on", j === lit));
      };
      track.addEventListener("scroll", mark, { passive: true });
      mark();
    }
    rxSwipe(stage, track);
  }

  // Sideways swipes. With no page track a swipe moves between documents;
  // with one, the track pages natively and a swipe that starts at its first
  // or last page carries on to the neighbouring document.
  //
  // Touch events, not pointer events: once the track starts panning the
  // browser cancels the pointer, but touchend still arrives. A mouse has the
  // arrow buttons and the keyboard instead.
  function rxSwipe(stage, track) {
    if (stage._rxSwipe) {
      stage.removeEventListener("touchstart", stage._rxSwipe[0]);
      stage.removeEventListener("touchend", stage._rxSwipe[1]);
    }
    let x0 = 0, y0 = 0, s0 = 0, live = false;
    const start = (e) => {
      if (e.touches.length !== 1) { live = false; return; }
      live = true;
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
      s0 = track ? track.scrollLeft : 0;
    };
    const end = (e) => {
      if (!live || !rxOv) return;
      live = false;
      const t = e.changedTouches[0];
      const dx = t.clientX - x0, dy = t.clientY - y0;
      if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy) * 1.3) return;
      if (track) {
        const max = track.scrollWidth - track.clientWidth - 2;
        if (dx < 0 && s0 < max) return;   // the track paged; not past its end
        if (dx > 0 && s0 > 2) return;
      }
      rxGo(rxOv.i + (dx < 0 ? 1 : -1));
    };
    stage._rxSwipe = [start, end];
    stage.addEventListener("touchstart", start, { passive: true });
    stage.addEventListener("touchend", end, { passive: true });
  }

  // The pure rules, for tests/test_reader_explore.py (Node only).
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { rxShape, rxTitle, rxShortDate, rxTone, rxRender };
  }
})();

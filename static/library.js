/* Library - browse a vault by subject, and read it whole.
   server/library.py builds each vault's subject tree (areas, folders, then
   regions, subjects and sub-subjects found by clustering); this file owns
   the #view-library surface: the map (a squarified treemap you break apart
   down to single pages), the reader beside it, the action bar along the
   reader's bottom, the constellation of a page's connections, and saved
   subsets. The shell registers the window and calls loadLibrary /
   openLibrary; everything else lives here. */
"use strict";

(() => {
  // Box colours: muted, and still tellable apart on the dark ground.
  const PAL = ["#7fa6c9", "#c9a66b", "#8fbf8a", "#c98f8f", "#a991c9",
               "#6fbfb6", "#c9b86b", "#c98fb4", "#9aa4b0"];
  const LEVEL = { vault: "Vault", area: "Area", folder: "Folder",
                  region: "Region", subject: "Subject", detail: "Sub-subject" };
  const KIDS = { area: "areas", folder: "folders", region: "regions",
                 subject: "subjects", detail: "sub-subjects" };
  // A single tap waits this long on touch and narrow screens, so a
  // double-tap can break the box apart before the detail opens.
  const TAP_DELAY_MS = 300;
  // Title cards at the last level: a card is this big, and a box shows its
  // pages as cards only when they all fit at that size.
  const CARD_W = 150, CARD_H = 42, CARD_MIN_W = 96, CARD_MIN_H = 36;
  const STORE_KEY = "vira-library";

  const S = {
    vaults: [], vault: "primary", data: null, machine: false,
    chains: [], pages: [], groupIx: new Map(),
    base: null, stack: [], query: "", sel: null,
    side: null, page: null, history: [], hpos: -1,
    drawer: "", subsets: [], poll: null, namesPoll: null,
    loadGen: 0, pageGen: 0, mounted: false,
  };
  const DR = { S: [], sp: null, kids: [], rects: [], leaf: false, cards: false,
               cw: 8, ch: 8, cell: 8, W: 600, H: 400, dpr: 1 };
  const dom = {};
  let animating = false, clickT = null;

  // ---------- small helpers ----------
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  const unesc = (s) => String(s ?? "").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"").replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : (many || one + "s")}`;
  const IMG_EXT = /\.(jpe?g|png|webp|gif|avif|heic|svg)$/i;
  const slug = (s) => String(s || "").toLowerCase().replace(/<[^>]+>/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80);
  const cssVar = (name, fallback) => {
    try {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
    } catch (e) { return fallback; }
  };
  const ago = (secs) => {
    if (!secs) return "";
    const d = new Date(secs * 1000);
    return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
  };
  const builtAgo = (iso) => {
    const t = Date.parse(iso || "");
    if (!t) return "";
    const m = Math.round((Date.now() - t) / 60000);
    if (m < 2) return "just now";
    if (m < 90) return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 36) return `${h} h ago`;
    return `${Math.round(h / 24)} days ago`;
  };

  // ---------- pure layout (exposed to the test harness) ----------

  /* The squarified treemap (Bruls, Huizing, van Wijk): rows of boxes whose
     aspect ratios stay as close to square as the sizes allow. items carry
     {i, v}; returns [{i, x, y, w, h}]. */
  function squarify(items, x, y, w, h) {
    const out = [];
    let rest = items.slice();
    while (rest.length) {
      const short = Math.min(w, h), total = rest.reduce((a, b) => a + b.v, 0);
      const area = w * h;
      let row = [], best = Infinity;
      for (const it of rest) {
        const r = row.concat(it);
        const s = r.reduce((a, b) => a + b.v, 0) / total * area, side = s / short;
        const worst = Math.max(...r.map((q) => {
          const len = (q.v / total * area) / side;
          return Math.max(side / len, len / side);
        }));
        if (worst <= best) { row = r; best = worst; } else break;
      }
      const s = row.reduce((a, b) => a + b.v, 0) / total * area, side = s / short;
      let off = 0;
      for (const q of row) {
        const len = (q.v / total * area) / side;
        if (w >= h) out.push({ i: q.i, x, y: y + off, w: side, h: len });
        else out.push({ i: q.i, x: x + off, y, w: len, h: side });
        off += len;
      }
      if (w >= h) { x += side; w -= side; } else { y += side; h -= side; }
      rest = rest.slice(row.length);
    }
    return out;
  }

  /* Where n pages sit inside one box: a grid under the box's label when
     the box is big enough to carry one. */
  function placeIn(n, r, fixedCols) {
    const pad = 6, top = r.h > 44 && r.w > 80 ? 26 : pad;
    const ix = r.x + pad, iy = r.y + top;
    const iw = Math.max(2, r.w - 2 * pad), ih = Math.max(2, r.h - top - pad);
    let cols = fixedCols || Math.max(1, Math.round(Math.sqrt(n * iw / ih)));
    let rows = Math.ceil(n / cols);
    while (rows * cols < n) { cols++; rows = Math.ceil(n / cols); }
    const cw = iw / cols, ch = ih / Math.max(1, rows);
    const pts = [];
    for (let j = 0; j < n; j++) {
      pts.push([ix + cw * ((j % cols) + 0.5), iy + ch * (Math.floor(j / cols) + 0.5)]);
    }
    return { pts, cw, ch, cols };
  }

  /* The first level at which a set of pages differs, and its groups there
     ({L, kids: [{g, n}]}, biggest first); null when nothing splits further.
     chains[i] is page i's group path from its area down to its leaf. */
  function splitOf(members, chains) {
    for (let L = 0; L < 8; L++) {
      const m = new Map();
      let seen = 0;
      for (const i of members) {
        const g = chains[i][L];
        if (g === undefined) continue;
        seen++;
        m.set(g, (m.get(g) || 0) + 1);
      }
      if (m.size >= 2) {
        return { L, kids: [...m].map(([g, n]) => ({ g, n })).sort((a, b) => b.n - a.n || a.g - b.g) };
      }
      if (!seen) break;
    }
    return null;
  }

  /* A filter typed into the search box: words that must all appear in a
     page's title or path, -word to exclude, and tag:, kind: (the page's
     type) and in: (a folder) terms. */
  function parseQuery(q) {
    const out = { words: [], not: [], tags: [], kinds: [], folders: [] };
    const re = /(-?)(?:(tag|kind|type|in):)?(?:"([^"]+)"|(\S+))/gi;
    let m;
    while ((m = re.exec(String(q || "")))) {
      const neg = m[1] === "-", field = (m[2] || "").toLowerCase();
      const val = (m[3] ?? m[4] ?? "").toLowerCase().replace(/^#/, "");
      if (!val) continue;
      if (field === "tag") out.tags.push(val);
      else if (field === "kind" || field === "type") out.kinds.push(val);
      else if (field === "in") out.folders.push(val.replace(/^\/+|\/+$/g, ""));
      else (neg ? out.not : out.words).push(val);
    }
    return out;
  }

  function matchPage(pq, title, rel, tags, kind) {
    const hay = (title + " " + rel).toLowerCase();
    if (pq.words.some((w) => !hay.includes(w))) return false;
    if (pq.not.some((w) => hay.includes(w))) return false;
    if (pq.kinds.length && !pq.kinds.includes(String(kind || "").toLowerCase())) return false;
    if (pq.folders.length) {
      const low = rel.toLowerCase();
      if (!pq.folders.some((f) => low === f || low.startsWith(f + "/"))) return false;
    }
    if (pq.tags.length) {
      const have = (tags || []).map((t) => String(t).toLowerCase());
      if (!pq.tags.every((t) => have.some((h) => h === t || h.endsWith("/" + t)))) return false;
    }
    return true;
  }

  // ---------- the reader's renderer (pure; exposed to the harness) ----------

  const unquote = (v) => {
    v = String(v ?? "").trim();
    return v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === "\"" || v[0] === "'")
      ? v.slice(1, -1) : v;
  };

  function splitInlineList(raw) {
    const out = [];
    let cur = "", depth = 0, quote = "";
    for (const c of raw) {
      if (quote) { cur += c; if (c === quote) quote = ""; continue; }
      if (c === "\"" || c === "'") { quote = c; cur += c; continue; }
      if (c === "[") depth++;
      if (c === "]") depth--;
      if (c === "," && depth <= 0) { if (cur.trim()) out.push(unquote(cur)); cur = ""; continue; }
      cur += c;
    }
    if (cur.trim()) out.push(unquote(cur));
    return out;
  }

  /* The page's properties (Obsidian frontmatter) and its body. Top-level
     keys become rows; list items and nested lines become the row's values,
     so nothing in the frontmatter is hidden even when it is not flat. */
  function splitFrontmatter(text) {
    const t = String(text || "").replace(/\r\n?/g, "\n");
    const m = t.match(/^---\n([\s\S]*?)\n---[ \t]*(?:\n|$)/);
    if (!m) return { props: [], body: t };
    const props = [];
    let cur = null;
    for (const line of m[1].split("\n")) {
      if (!line.trim() || /^\s*#/.test(line)) continue;
      const top = !/^\s/.test(line) && line.match(/^([^:\s][^:]*?):(?:\s+(.*)|\s*)$/);
      if (top) {
        cur = { key: top[1].trim(), values: [] };
        const v = (top[2] || "").trim();
        if (v.startsWith("[") && v.endsWith("]") && !v.startsWith("[[")) {
          cur.values.push(...splitInlineList(v.slice(1, -1)));
        } else if (v && v !== "|" && v !== ">" && v !== "|-" && v !== ">-") {
          cur.values.push(unquote(v));
        }
        props.push(cur);
        continue;
      }
      if (!cur) continue;
      const item = line.match(/^\s*-\s+(.*)$/);
      cur.values.push(item ? unquote(item[1]) : line.trim());
    }
    return { props, body: t.slice(m[0].length) };
  }

  const isUrl = (u) => /^(https?:|mailto:)/i.test(u);

  function inline(src, ctx) {
    // Code spans, links and images are rendered first and parked in slots,
    // so emphasis and tag rules never reach inside a URL or attribute.
    const slots = [];
    const park = (html) => `\u0000${slots.push(html) - 1}\u0000`;
    let s = String(src).replace(/`([^`\n]+)`/g, (m, c) => park(`<code>${esc(c)}</code>`));
    s = esc(s);
    s = s.replace(/!\[\[([^\]|\n]+?)(?:\|([^\]\n]*))?\]\]/g,
      (m, t, opt) => park(embedHtml(unesc(t), unesc(opt || ""), ctx)));
    s = s.replace(/!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g,
      (m, alt, url) => park(mdImage(unesc(alt), unesc(url), ctx)));
    s = s.replace(/\[\[([^\]|\n]+?)(?:\|([^\]\n]*))?\]\]/g,
      (m, t, label) => park(wikiLink(unesc(t), label ? unesc(label) : "", ctx)));
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g,
      (m, text, url) => park(mdLink(text, unesc(url), ctx)));
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)\u0000]+)/g,
      (m, pre, url) => pre + park(`<a href="${esc(unesc(url))}" target="_blank" rel="noopener">${url}</a>`));
    s = s.replace(/\*\*(?=\S)([^*\n]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/__(?=\S)([^_\n]+?)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*(?=\S)([^*\n]+?)\*(?=[^*\w]|$)/g, "$1<em>$2</em>")
      .replace(/(^|[^_\w])_(?=\S)([^_\n]+?)_(?=[^_\w]|$)/g, "$1<em>$2</em>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/==([^=\n]+)==/g, "<mark>$1</mark>")
      .replace(/(^|\s)#([A-Za-z][\w/-]*)/g, "$1<span class=\"lib-tag\">#$2</span>");
    return s.replace(/\u0000(\d+)\u0000/g, (m, n) => slots[+n]);
  }

  const lookup = (ctx, target) => {
    const map = ctx?.linkmap || {};
    const key = String(target || "").trim().toLowerCase();
    return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
  };

  function assetUrl(ctx, target) {
    const hit = lookup(ctx, target);
    let path = hit && hit.asset;
    if (!path) {
      const clean = String(target).trim().replace(/^\/+/, "");
      path = ctx?.vault && ctx.vault !== "primary" && !clean.startsWith("@")
        ? `@${ctx.vault}/${clean}` : clean;
    }
    return "/api/vault/asset?path=" + encodeURIComponent(path);
  }

  function embedHtml(target, opt, ctx) {
    const base = target.split("#")[0].trim();
    if (IMG_EXT.test(base)) {
      const w = /^\d+(x\d+)?$/.test(opt.trim()) ? ` style="max-width:${parseInt(opt, 10)}px"` : "";
      return `<img class="lib-img" loading="lazy" src="${esc(assetUrl(ctx, base))}" alt="${esc(opt && !w ? opt : base)}"${w}>`;
    }
    return `<span class="lib-embed">${wikiLink(target, opt, ctx)}</span>`;
  }

  function mdImage(alt, url, ctx) {
    if (isUrl(url)) {
      // A remote non-image (Obsidian's YouTube embed) is not a picture.
      return IMG_EXT.test(url.split("?")[0])
        ? `<img class="lib-img" loading="lazy" src="${esc(url)}" alt="${esc(alt)}">`
        : `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(alt || url)}</a>`;
    }
    return IMG_EXT.test(url.split("#")[0])
      ? `<img class="lib-img" loading="lazy" src="${esc(assetUrl(ctx, url.split("#")[0]))}" alt="${esc(alt)}">`
      : "";
  }

  function wikiLink(target, label, ctx) {
    const [base, ...rest] = target.split("#");
    const anchor = rest.join("#").replace(/^\^/, "");
    const text = label || (anchor ? `${base.trim() || ""}${base.trim() ? " > " : ""}${anchor}` : base.trim());
    const hit = lookup(ctx, base.trim());
    if (!base.trim() && anchor) {
      return `<a class="lib-link" data-anchor="${esc(slug(anchor))}">${esc(text)}</a>`;
    }
    if (hit && hit.rel) {
      return `<a class="lib-link" data-rel="${esc(hit.rel)}"${anchor ? ` data-anchor="${esc(slug(anchor))}"` : ""} title="${esc(hit.title || hit.rel)}">${esc(text)}</a>`;
    }
    if (hit && hit.asset) {
      return `<a class="lib-link asset" data-asset="${esc(hit.asset)}" title="Open the file">${esc(text)}</a>`;
    }
    return `<a class="lib-link dead" data-dead="${esc(base.trim())}" title="No page by this name">${esc(text)}</a>`;
  }

  function mdLink(textHtml, url, ctx) {
    if (isUrl(url)) return `<a href="${esc(url)}" target="_blank" rel="noopener">${textHtml}</a>`;
    if (url.startsWith("#")) return `<a class="lib-link" data-anchor="${esc(slug(url.slice(1)))}">${textHtml}</a>`;
    const base = url.split("#")[0];
    let target = base;
    try { target = decodeURIComponent(base); } catch (e) { /* keep it as written */ }
    const hit = lookup(ctx, target) || lookup(ctx, base);
    if (hit && hit.rel) return `<a class="lib-link" data-rel="${esc(hit.rel)}">${textHtml}</a>`;
    if (hit && hit.asset) return `<a class="lib-link asset" data-asset="${esc(hit.asset)}">${textHtml}</a>`;
    return textHtml;
  }

  const cells = (line) => {
    let t = line.trim();
    if (t.startsWith("|")) t = t.slice(1);
    if (t.endsWith("|") && !t.endsWith("\\|")) t = t.slice(0, -1);
    // A pipe inside [[link|label]] is the link's, not a column break.
    const out = [];
    let cur = "", depth = 0;
    for (let k = 0; k < t.length; k++) {
      const c = t[k];
      if (c === "[" && t[k + 1] === "[") { depth++; cur += "[["; k++; continue; }
      if (c === "]" && t[k + 1] === "]") { depth = Math.max(0, depth - 1); cur += "]]"; k++; continue; }
      if (c === "\\" && t[k + 1] === "|") { cur += "|"; k++; continue; }
      if (c === "|" && !depth) { out.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    out.push(cur.trim());
    return out;
  };

  function listHtml(lines, start, ctx) {
    const items = [];
    let i = start;
    while (i < lines.length) {
      const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (!m) {
        if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
          items[items.length - 1].text += "\n" + lines[i].trim();
          i++;
          continue;
        }
        break;
      }
      let text = m[3], task = null;
      const t = text.match(/^\[([ xX])\]\s+(.*)$/);
      if (t) { task = t[1] !== " "; text = t[2]; }
      items.push({ indent: m[1].replace(/\t/g, "    ").length,
                   ordered: /\d/.test(m[2]), start: parseInt(m[2], 10), text, task });
      i++;
    }
    let html = "";
    const stack = [];
    for (const it of items) {
      while (stack.length && it.indent < stack[stack.length - 1].indent) {
        html += `</li></${stack.pop().tag}>`;
      }
      let top = stack[stack.length - 1];
      // A numbered list right after a bulleted one (or the reverse) at the
      // same depth is a new list, not more of the old one.
      if (top && it.indent === top.indent && top.tag !== (it.ordered ? "ol" : "ul")) {
        html += `</li></${stack.pop().tag}>`;
        top = null;
      }
      if (!top || it.indent > top.indent) {
        const tag = it.ordered ? "ol" : "ul";
        stack.push({ indent: it.indent, tag });
        html += it.ordered && it.start > 1 ? `<ol start="${it.start}">` : `<${tag}>`;
      } else {
        html += "</li>";
      }
      const body = it.text.split("\n").map((l) => inline(l, ctx)).join("<br>");
      html += it.task === null ? `<li>${body}`
        : `<li class="lib-task${it.task ? " done" : ""}"><span class="lib-check" aria-hidden="true"></span>${body}`;
    }
    while (stack.length) html += `</li></${stack.pop().tag}>`;
    return { html, next: i };
  }

  /* Markdown as Obsidian writes it: headings, paragraphs (single newlines
     kept), fenced code, tables, lists (nested, numbered, tasks), quotes and
     callouts, rules, images and [[links]]. Everything is escaped first;
     only the shapes above become HTML. */
  function renderMarkdown(body, ctx) {
    const lines = String(body || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [], para = [];
    const flush = () => {
      if (para.length) out.push(`<p>${para.map((l) => inline(l, ctx)).join("<br>")}</p>`);
      para.length = 0;
    };
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m = line.match(/^\s*(```+|~~~+)\s*([\w+#.-]*)/);
      if (m) {
        flush();
        const fence = m[1], buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
        i++;
        out.push(`<pre class="lib-code"${m[2] ? ` data-lang="${esc(m[2])}"` : ""}><code>${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }
      if (!line.trim()) { flush(); i++; continue; }
      if (/^\s*%%/.test(line)) {               // an Obsidian comment block
        flush();
        if (!/%%.*%%/.test(line.trim().slice(2))) {
          i++;
          while (i < lines.length && !lines[i].includes("%%")) i++;
        }
        i++;
        continue;
      }
      m = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (m) {
        flush();
        const lvl = Math.min(6, m[1].length + 1);
        out.push(`<h${lvl} id="${esc(slug(m[2]))}">${inline(m[2], ctx)}</h${lvl}>`);
        i++;
        continue;
      }
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push("<hr>"); i++; continue; }
      if (line.includes("|") && i + 1 < lines.length
          && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(lines[i + 1])
          && lines[i + 1].includes("-")) {
        flush();
        const head = cells(line);
        const aligns = cells(lines[i + 1]).map((c) =>
          (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        const td = (tag, c, k) => `<${tag}${aligns[k] ? ` style="text-align:${aligns[k]}"` : ""}>${inline(c, ctx)}</${tag}>`;
        out.push(`<div class="lib-table"><table><thead><tr>${head.map((c, k) => td("th", c, k)).join("")}</tr></thead><tbody>`
          + rows.map((r) => `<tr>${head.map((_h, k) => td("td", r[k] ?? "", k)).join("")}</tr>`).join("")
          + "</tbody></table></div>");
        continue;
      }
      if (/^\s*>/.test(line)) {
        flush();
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        const cm = buf[0] && buf[0].match(/^\[!([\w-]+)\]([+-]?)\s*(.*)$/);
        if (cm) {
          const kind = cm[1].toLowerCase();
          const title = cm[3] || kind.charAt(0).toUpperCase() + kind.slice(1);
          out.push(`<div class="lib-callout" data-kind="${esc(kind)}"><div class="lib-callout-title">${inline(title, ctx)}</div>${renderMarkdown(buf.slice(1).join("\n"), ctx)}</div>`);
        } else {
          out.push(`<blockquote>${renderMarkdown(buf.join("\n"), ctx)}</blockquote>`);
        }
        continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        flush();
        const res = listHtml(lines, i, ctx);
        out.push(res.html);
        i = res.next;
        continue;
      }
      para.push(line);
      i++;
    }
    flush();
    return out.join("\n");
  }

  function propsHtml(props, ctx) {
    if (!props.length) return "";
    const rows = props.map((p) => {
      const vals = p.values.length ? p.values : [""];
      const tagLike = /^(tags?|aliases|cssclasses)$/i.test(p.key);
      const cell = vals.map((v) => tagLike
        ? `<span class="lib-prop-chip">${esc(v)}</span>`
        : `<span class="lib-prop-val">${inline(v, ctx)}</span>`).join(tagLike ? "" : "<br>");
      return `<tr><th>${esc(p.key)}</th><td>${cell}</td></tr>`;
    }).join("");
    return `<details class="lib-props" open><summary>Properties <span class="lib-faint">${props.length}</span></summary><table>${rows}</table></details>`;
  }

  /* Where each star of the constellation sits: the page at the centre,
     pages it links to or from on the inner ring (grouped by subject), its
     similar pages on the outer ring. */
  function constellationLayout(nodes, w, h) {
    const cx = w / 2, cy = h / 2;
    const inner = [], outer = [];
    nodes.forEach((n, k) => (n.ring === "similar" ? outer : inner).push(k));
    const order = (ks) => ks.sort((a, b) => (nodes[a].leaf - nodes[b].leaf)
      || String(nodes[a].title).localeCompare(String(nodes[b].title)));
    order(inner);
    order(outer);
    const pos = new Array(nodes.length);
    const rx1 = w * 0.27, ry1 = h * 0.30, rx2 = w * 0.44, ry2 = h * 0.43;
    inner.forEach((k, j) => {
      const a = -Math.PI / 2 + (2 * Math.PI * j) / Math.max(1, inner.length);
      pos[k] = [cx + rx1 * Math.cos(a), cy + ry1 * Math.sin(a)];
    });
    outer.forEach((k, j) => {
      const a = -Math.PI / 2 + (2 * Math.PI * (j + 0.5)) / Math.max(1, outer.length);
      pos[k] = [cx + rx2 * Math.cos(a), cy + ry2 * Math.sin(a)];
    });
    return { center: [cx, cy], pos };
  }

  // ---------- data ----------

  function groupLabel(g) {
    const row = S.data.groups[g];
    if (!row) return "";
    return row[3] || row[4] || LEVEL[row[2]] || "Subject";
  }
  const groupLevel = (g) => (S.data.groups[g] || [])[2] || "";
  const groupId = (g) => (S.data.groups[g] || [])[0] || "";
  const groupNamed = (g) => {
    const row = S.data.groups[g];
    return !!row && !!row[3];
  };

  function prepare(data) {
    S.data = data;
    S.groupIx = new Map(data.groups.map((row, g) => [row[0], g]));
    const memo = new Map();
    const chainOf = (g) => {
      if (memo.has(g)) return memo.get(g);
      const chain = [];
      let h = g;
      while (h > 0) { chain.push(h); h = data.groups[h][1]; }
      chain.reverse();
      memo.set(g, chain);
      return chain;
    };
    const P = data.pages;
    S.chains = P.leaf.map((g) => chainOf(g));
    S.pages = P.rel.map((rel, i) => ({ i, x: 0, y: 0, tx: 0, ty: 0, k: 0 }));
    S.relIx = new Map(P.rel.map((rel, i) => [rel, i]));
  }

  function baseMembers() {
    if (S.base && S.base.members) return S.base.members;
    return S.pages.map((p) => p.i);
  }

  function currentSet() {
    const base = baseMembers();
    if (!S.stack.length) return base;
    const g = S.stack[S.stack.length - 1];
    return base.filter((i) => S.chains[i].includes(g));
  }

  // ---------- mounting ----------

  function mount() {
    const root = document.getElementById("lib-root");
    if (!root) return false;
    if (S.mounted) return true;
    root.innerHTML = `
      <div class="lib-main">
        <div class="lib-bar">
          <select class="lib-vault" aria-label="Vault" hidden></select>
          <input class="search lib-q" type="search" autocomplete="off" spellcheck="false"
                 placeholder="Filter: words, tag:, kind:, in:folder, -word" aria-label="Filter the map">
          <button class="fchip sm lib-machine" type="button" aria-pressed="false"
                  title="Machine output: generated reports, session logs and bulk captures">Machine output hidden</button>
          <button class="fchip sm lib-subsets-btn" type="button" data-pop-opener title="Saved subsets">Subsets</button>
          <button class="fchip sm lib-more" type="button" aria-label="More Library actions" title="Rebuild, name subjects">More</button>
        </div>
        <div class="lib-trail"></div>
        <div class="lib-stage">
          <canvas class="lib-canvas" aria-label="Subject map: double-click a box to break it apart"></canvas>
          <div class="lib-tip" hidden></div>
          <div class="lib-empty" hidden></div>
        </div>
        <div class="lib-legend"></div>
      </div>
      <aside class="lib-side" aria-label="Detail and reader">
        <div class="lib-side-body"></div>
      </aside>
      <div class="lib-pop" hidden></div>`;
    dom.root = root;
    dom.vault = root.querySelector(".lib-vault");
    dom.q = root.querySelector(".lib-q");
    dom.machine = root.querySelector(".lib-machine");
    dom.subsetsBtn = root.querySelector(".lib-subsets-btn");
    dom.more = root.querySelector(".lib-more");
    dom.trail = root.querySelector(".lib-trail");
    dom.stage = root.querySelector(".lib-stage");
    dom.cv = root.querySelector(".lib-canvas");
    dom.ctx = dom.cv.getContext("2d");
    dom.tip = root.querySelector(".lib-tip");
    dom.empty = root.querySelector(".lib-empty");
    dom.legend = root.querySelector(".lib-legend");
    dom.side = root.querySelector(".lib-side");
    dom.sideBody = root.querySelector(".lib-side-body");
    dom.pop = root.querySelector(".lib-pop");
    bindMap();
    bindBar();
    bindSide();
    let rz = null;
    new ResizeObserver(() => {
      clearTimeout(rz);
      rz = setTimeout(() => { if (!animating && S.data) { layout(); snap(); draw(1); } }, 120);
    }).observe(dom.stage);
    const redraw = () => { if (!animating && S.data) draw(1); };
    new MutationObserver(redraw).observe(document.documentElement,
      { attributes: true, attributeFilter: ["data-theme", "class", "style"] });
    S.mounted = true;
    return true;
  }

  const narrow = () => window.innerWidth < 1100 || matchMedia("(pointer: coarse)").matches;
  const sheetMode = () => window.innerWidth < 760;

  function saveState() {
    lsSet(STORE_KEY, { vault: S.vault, machine: S.machine, rel: S.page?.rel || "" });
  }

  // ---------- loading ----------

  async function loadLibrary(opts = {}) {
    if (!mount()) return;
    const saved = lsGet(STORE_KEY, {}) || {};
    if (!S.data && !opts.vault) {
      S.vault = saved.vault || S.vault;
      S.machine = !!saved.machine;
    }
    if (opts.vault) S.vault = opts.vault;
    const gen = ++S.loadGen;
    const over = await api("/api/library").catch((e) => ({ error: e }));
    if (gen !== S.loadGen) return;
    if (over.error) { showEmpty(`The Library could not load: ${errText(over.error)}`); return; }
    S.vaults = over.vaults || [];
    S.subsets = over.subsets || [];
    if (!S.vaults.length) {
      showEmpty("No vault is connected yet. Connect one in Config > Brain, then build its Library here.");
      return;
    }
    if (!S.vaults.some((v) => v.id === S.vault)) S.vault = S.vaults[0].id;
    renderVaults();
    const v = S.vaults.find((x) => x.id === S.vault);
    if (v.status && v.status.state === "building") watchBuild();
    if (!v.built) {
      showEmpty(null, v);
      return;
    }
    if (!S.data || S.data.vault !== S.vault || opts.reload || S.data.built !== v.built) {
      await loadMap();
      if (gen !== S.loadGen) return;
    } else {
      layout(); snap(); draw(1);
    }
    const rel = opts.rel || (!S.page && saved.vault === S.vault && saved.rel) || "";
    if (rel) openPage(rel, { push: true, quiet: !opts.rel });
    else if (!S.side) showVaultDetail();
  }

  async function loadMap() {
    dom.empty.hidden = true;
    setStatus("Loading the map...");
    const data = await api(`/api/library/map?vault=${encodeURIComponent(S.vault)}&machine=${S.machine ? 1 : 0}`)
      .catch((e) => ({ error: e }));
    if (data.error) { showEmpty(`The map could not load: ${errText(data.error)}`); return; }
    const keepStack = S.data && S.data.vault === data.vault && S.data.built === data.built
      ? S.stack.map(groupId) : [];
    prepare(data);
    S.stack = keepStack.map((id) => S.groupIx.get(id)).filter((g) => g !== undefined);
    if (S.query) applyQuery(S.query, true);
    else if (S.base && S.base.rels) setBase(S.base.kind, S.base.label, S.base.rels, S.base.extra, true);
    renderMachine();
    layout();
    snap();
    draw(1);
    renderTrail();
    renderLegend();
    renderStatus();
  }

  function renderVaults() {
    dom.vault.hidden = S.vaults.length < 2;
    dom.vault.innerHTML = S.vaults.map((v) =>
      `<option value="${esc(v.id)}"${v.id === S.vault ? " selected" : ""}>${esc(v.name)}</option>`).join("");
  }

  function setStatus(text) {
    const n = document.getElementById("library-status");
    if (n) n.textContent = text || "";
  }

  function renderStatus() {
    if (!S.data) return;
    const d = S.data;
    const subjects = d.groups.filter((g) => ["region", "subject", "detail"].includes(g[2])).length;
    const unnamed = d.groups.filter((g) => ["region", "subject", "detail"].includes(g[2]) && !g[3]).length;
    setStatus(`${plural(d.pages.rel.length, "page")} - ${fmt(subjects)} subjects`
      + (unnamed ? ` (${fmt(unnamed)} unnamed)` : "") + ` - built ${builtAgo(d.built)}`);
  }

  function showEmpty(text, v) {
    dom.empty.hidden = false;
    S.data = null;
    const ctx = dom.ctx;
    ctx.clearRect(0, 0, dom.cv.width, dom.cv.height);
    dom.trail.innerHTML = "";
    dom.legend.innerHTML = "";
    if (text) {
      dom.empty.innerHTML = `<p>${esc(text)}</p>`;
      return;
    }
    const st = v?.status || {};
    const building = st.state === "building";
    dom.empty.innerHTML = `
      <h3>Map ${esc(v.name)} by subject</h3>
      <p>The Library reads every page of this vault, groups them by meaning
         (from the vault's own local embeddings, and words where a page has
         none yet), and lays the subjects out as boxes you can break apart
         down to single pages. It runs on this Mac, in the background.</p>
      ${st.state === "error" ? `<p class="lib-err">The last build failed: ${esc(st.error || "unknown error")}</p>` : ""}
      <button class="btn primary lib-build" type="button"${building ? " disabled" : ""}>
        ${building ? `Building: ${esc(st.step || "starting")}...` : "Build the subject map"}</button>`;
    dom.empty.querySelector(".lib-build")?.addEventListener("click", startBuild);
    setStatus(building ? `Building: ${st.step || "starting"}` : "");
  }

  async function startBuild() {
    try {
      const st = await post("/api/library/build", { vault: S.vault });
      toast(st.state === "building" ? "Building the Library in the background" : "Build started");
      watchBuild();
      if (!S.data) showEmpty(null, { name: currentVaultName(), status: st });
    } catch (e) {
      toast(`Could not start the build: ${errText(e)}`);
    }
  }

  const currentVaultName = () => (S.vaults.find((v) => v.id === S.vault) || {}).name || S.vault;

  function watchBuild() {
    S.poll?.stop();
    S.poll = startPoll(async (h) => {
      const st = await api(`/api/library/status?vault=${encodeURIComponent(S.vault)}`);
      if (st.state === "building") {
        setStatus(`Building: ${st.step || "starting"}${st.pages ? ` (${fmt(st.pages)} pages)` : ""}`);
        if (!S.data) showEmpty(null, { name: currentVaultName(), status: st });
        return;
      }
      h.stop();
      if (st.state === "error") {
        toast(`The Library build failed: ${st.error || "unknown error"}`);
        if (!S.data) showEmpty(null, { name: currentVaultName(), status: st });
        renderStatus();
        return;
      }
      toast(`Library built: ${plural(st.pages || 0, "page")}, ${fmt(st.subjects || 0)} subjects`);
      await loadLibrary({ reload: true });
    }, 3000, 30 * 60 * 1000);
  }

  // ---------- the trail and the legend ----------

  function renderTrail() {
    if (!S.data) { dom.trail.innerHTML = ""; return; }
    const set = DR.S;
    const baseLabel = S.base ? S.base.label : S.data.name;
    const crumbs = [`<button type="button" class="lib-crumb" data-lvl="0">${esc(baseLabel)}</button>`]
      .concat(S.stack.map((g, k) => (k === S.stack.length - 1
        ? `<b>${esc(groupLabel(g))}</b>`
        : `<button type="button" class="lib-crumb" data-lvl="${k + 1}">${esc(groupLabel(g))}</button>`)));
    const hint = DR.leaf
      ? `${plural(set.length, "page")}, no finer subjects. ${DR.cards ? "Click a page to read it." : "Hover a dot for its title; click it to read."}`
      : `${plural(set.length, "page")} in ${fmt(DR.kids.length)} ${KIDS[groupLevel(DR.kids[0]?.g)] || "groups"}. Double-click one to break it apart.`;
    dom.trail.innerHTML = (S.stack.length || S.base
      ? `<button type="button" class="btn tiny lib-up" title="Up a level (Esc)">Up a level</button>` : "")
      + `<span class="lib-crumbs">${crumbs.join('<span class="lib-sep">&rsaquo;</span>')}</span>`
      + `<span class="lib-hint">${esc(hint)}</span>`
      + (S.stack.length || S.base ? `<button type="button" class="btn tiny lib-save-set" data-pop-opener title="Save this set as a subset">Save subset</button>` : "");
  }

  function renderLegend() {
    if (!S.data) { dom.legend.innerHTML = ""; return; }
    const m = S.data.machine;
    const parts = [];
    if (DR.kids.length > 1 && DR.kids.length <= 12) {
      parts.push(DR.kids.map((k, j) => `<span><i style="background:${PAL[j % PAL.length]}"></i>${esc(groupLabel(k.g))}</span>`).join(""));
    }
    if (m && m.pages) {
      parts.push(`<span class="lib-faint">${m.shown ? "Showing" : "Hiding"} ${plural(m.pages, "machine page")}${m.source === "suggested" ? " (suggested folders)" : ""}</span>`);
    }
    dom.legend.innerHTML = parts.join("");
  }

  function renderMachine() {
    const m = S.data?.machine || {};
    dom.machine.setAttribute("aria-pressed", S.machine ? "true" : "false");
    dom.machine.classList.toggle("on", S.machine);
    dom.machine.textContent = S.machine ? "Machine output shown" : "Machine output hidden";
    dom.machine.title = m.dirs && m.dirs.length
      ? `Machine output (${m.source === "owner" ? "your choice" : "suggested"}): ${m.dirs.join(", ")}`
      : "No folder counts as machine output in this vault";
  }

  // ---------- layout and drawing ----------

  function layout() {
    if (!S.data) return;
    const W = Math.max(240, dom.stage.clientWidth), baseH = Math.max(220, dom.stage.clientHeight);
    const set = currentSet();
    const sp = splitOf(set, S.chains);
    DR.S = set;
    DR.sp = sp;
    DR.leaf = !sp;
    DR.kids = sp ? sp.kids : [{ g: S.stack[S.stack.length - 1] ?? 0, n: set.length }];
    let H = baseH, cols = 0;
    DR.cards = false;
    if (DR.leaf && set.length) {
      cols = Math.max(1, Math.floor((W - 12) / CARD_W));
      const need = Math.ceil(set.length / cols) * (CARD_H + 4) + 40;
      // Cards when they fit in a few screens; past that the dots are the
      // honest picture and the detail lists the pages.
      if (need <= baseH * 4) { DR.cards = true; H = Math.max(baseH, need); } else cols = 0;
    }
    DR.W = W;
    DR.H = H;
    const dpr = window.devicePixelRatio || 1;
    DR.dpr = dpr;
    dom.cv.width = Math.round(W * dpr);
    dom.cv.height = Math.round(H * dpr);
    dom.cv.style.width = W + "px";
    dom.cv.style.height = H + "px";
    dom.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const items = DR.kids.map((k, j) => ({ i: j, v: Math.pow(Math.max(k.n, 1), 0.75) }))
      .sort((a, b) => b.v - a.v);
    const byI = {};
    for (const r of squarify(items, 0, 0, W, H)) byI[r.i] = r;
    DR.rects = DR.kids.map((_k, j) => byI[j]);
    const at = new Map(DR.kids.map((k, j) => [k.g, j]));
    const groups = DR.kids.map(() => []);
    const titles = S.data.pages.title;
    for (const i of set) {
      const j = sp ? (at.get(S.chains[i][sp.L]) ?? 0) : 0;
      S.pages[i].k = j;
      groups[j].push(i);
    }
    DR.cw = DR.ch = Infinity;
    groups.forEach((g, j) => {
      g.sort((a, b) => S.data.pages.leaf[a] - S.data.pages.leaf[b]
        || titles[a].toLowerCase().localeCompare(titles[b].toLowerCase()));
      const r = DR.rects[j];
      const c = placeIn(g.length, DR.cards ? { x: 0, y: 0, w: W, h: H } : r, DR.cards ? cols : 0);
      if (DR.cards) {
        // A card grid: fixed size, top-left, the box's own label above it.
        g.forEach((i, n) => {
          S.pages[i].tx = 6 + (n % cols) * CARD_W + CARD_W / 2;
          S.pages[i].ty = 34 + Math.floor(n / cols) * (CARD_H + 4) + CARD_H / 2;
        });
        DR.cw = CARD_W;
        DR.ch = CARD_H;
      } else {
        g.forEach((i, n) => { S.pages[i].tx = c.pts[n][0]; S.pages[i].ty = c.pts[n][1]; });
        DR.cw = Math.min(DR.cw, c.cw);
        DR.ch = Math.min(DR.ch, c.ch);
      }
    });
    DR.cell = Math.min(DR.cw, DR.ch);
  }

  function snap() { for (const i of DR.S) { S.pages[i].x = S.pages[i].tx; S.pages[i].y = S.pages[i].ty; } }

  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  function colors() {
    return {
      ink: cssVar("--text", "#cfcbc2"), dim: cssVar("--text-dim", "#8f8d85"),
      faint: cssVar("--text-faint", "#6a6864"), line: cssVar("--line", "#2f3034"),
      wash: cssVar("--bg-raised", "#222325"), card: cssVar("--bg-card", "#2a2b2d"),
      hi: cssVar("--accent-bright", "#a39c8d"), machine: cssVar("--text-faint", "#6a6864"),
      font: getComputedStyle(document.body).fontFamily || "sans-serif",
    };
  }

  function wrap(ctx, text, maxW, maxL) {
    const words = String(text).split(/\s+/), out = [];
    let line = "", i = 0;
    for (; i < words.length; i++) {
      const tryL = line ? line + " " + words[i] : words[i];
      if (!line || ctx.measureText(tryL).width <= maxW) { line = tryL; continue; }
      out.push(line);
      line = words[i];
      if (out.length === maxL) break;
    }
    if (out.length < maxL) { out.push(line); i = words.length; }
    let last = out[out.length - 1] || "";
    if (i < words.length || ctx.measureText(last).width > maxW) {
      while (last.length > 1 && ctx.measureText(last + "...").width > maxW) last = last.slice(0, -1);
      out[out.length - 1] = last + "...";
    }
    return out;
  }

  function draw(prog) {
    if (!S.data) return;
    const ctx = dom.ctx, C = colors(), W = DR.W, H = DR.H;
    ctx.clearRect(0, 0, W, H);
    ctx.lineWidth = 1;
    const machine = S.data.pages.machine || [];
    const top = DR.kids.length > 1 ? null : 0;
    const col = (i) => (machine[i] ? C.machine : PAL[(top ?? S.pages[i].k) % PAL.length]);
    if (!DR.cards || animating) {
      for (const r of DR.rects) {
        if (!r) continue;
        ctx.fillStyle = C.wash;
        ctx.globalAlpha = 0.55;
        ctx.fillRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = C.line;
        ctx.strokeRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3);
      }
    }
    const selRel = S.page?.rel, selI = selRel != null ? S.relIx.get(selRel) : undefined;
    if (DR.cards && !animating) {
      const fs = W < 560 ? 11 : 12, lh = Math.round(fs * 1.3);
      const w = CARD_W - 6, h = CARD_H, maxL = Math.max(1, Math.floor((h - 8) / lh));
      ctx.font = `500 ${fs}px ${C.font}`;
      ctx.textBaseline = "top";
      ctx.fillStyle = C.dim;
      ctx.fillText(DR.leaf ? groupLabel(DR.kids[0].g) + "  " + fmt(DR.S.length) : "", 8, 10);
      for (const i of DR.S) {
        const p = S.pages[i], x = p.x - w / 2, y = p.y - h / 2;
        ctx.fillStyle = C.card;
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = C.line;
        ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
        ctx.fillStyle = col(i);
        ctx.fillRect(x, y, 3, h);
        ctx.fillStyle = machine[i] ? C.dim : C.ink;
        wrap(ctx, S.data.pages.title[i], w - 14, maxL).forEach((ln, j) => ctx.fillText(ln, x + 9, y + 5 + j * lh));
        if (i === selI) {
          ctx.strokeStyle = C.hi;
          ctx.lineWidth = 2;
          ctx.strokeRect(x - 2, y - 2, w + 4, h + 4);
          ctx.lineWidth = 1;
        }
      }
    } else {
      const rad = Math.max(1.1, Math.min(6, DR.cell * 0.36));
      for (const i of DR.S) {
        const p = S.pages[i];
        ctx.fillStyle = col(i);
        if (rad >= 3) { ctx.beginPath(); ctx.arc(p.x, p.y, rad, 0, 7); ctx.fill(); }
        else ctx.fillRect(p.x - rad, p.y - rad, rad * 2, rad * 2);
      }
      if (selI !== undefined && DR.S.includes(selI)) {
        const p = S.pages[selI];
        ctx.strokeStyle = C.hi;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, rad + 5, 0, 7);
        ctx.stroke();
        ctx.lineWidth = 1;
      }
    }
    if (!DR.cards || animating) {
      ctx.globalAlpha = Math.min(1, prog * 1.4);
      DR.kids.forEach((k, j) => boxLabel(ctx, C, DR.rects[j], groupLabel(k.g), k.n, !groupNamed(k.g)));
      ctx.globalAlpha = 1;
    }
    if (S.sel && S.sel.kind === "group" && !DR.leaf) {
      const j = DR.kids.findIndex((k) => k.g === S.sel.g);
      const r = DR.rects[j];
      if (r) {
        ctx.strokeStyle = C.hi;
        ctx.lineWidth = 2.5;
        ctx.strokeRect(r.x + 2, r.y + 2, r.w - 4, r.h - 4);
        ctx.lineWidth = 1;
      }
    }
  }

  function boxLabel(ctx, C, r, label, n, isWords) {
    if (!r || r.w < 70 || r.h < 34) return;
    const small = DR.W < 560;
    ctx.font = `${isWords ? "italic 500" : "600"} ${small ? 11 : 12.5}px ${C.font}`;
    ctx.fillStyle = isWords ? C.dim : C.ink;
    ctx.textBaseline = "top";
    let t = label;
    while (ctx.measureText(t + "  " + fmt(n)).width > r.w - 16 && t.length > 4) t = t.slice(0, -2).trimEnd() + "...";
    ctx.fillText(t, r.x + 8, r.y + 8);
    const tw = ctx.measureText(t + "  ").width;
    ctx.font = `500 ${small ? 10.5 : 11.5}px ${cssVar("--mono", "monospace")}`;
    ctx.fillStyle = C.faint;
    ctx.fillText(fmt(n), r.x + 8 + tw, r.y + 9);
  }

  function animate() {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) { snap(); draw(1); return; }
    for (const i of DR.S) {
      const p = S.pages[i];
      if (!p.x && !p.y) { p.x = DR.W / 2; p.y = DR.H / 2; }
      p.x0 = p.x;
      p.y0 = p.y;
      p.d = ((p.k % 8) / 8) * 0.35 + Math.random() * 0.25;
    }
    animating = true;
    const t0 = performance.now(), dur = 900;
    const step = (now) => {
      const tt = (now - t0) / dur;
      let done = true;
      for (const i of DR.S) {
        const p = S.pages[i];
        let u = (tt - p.d) / 0.4;
        u = Math.max(0, Math.min(1, u));
        if (u < 1) done = false;
        const e = ease(u);
        p.x = p.x0 + (p.tx - p.x0) * e;
        p.y = p.y0 + (p.ty - p.y0) * e;
      }
      draw(Math.max(0, (tt - 0.55) / 0.45));
      if (!done) requestAnimationFrame(step);
      else { animating = false; snap(); draw(1); }
    };
    requestAnimationFrame(step);
  }

  /* Break a box apart (or go back up): the stack is one group per level
     opened, below the current base set. */
  function drillTo(stack) {
    if (animating) return;
    S.stack = stack.slice();
    dom.stage.scrollTop = 0;
    layout();
    renderTrail();
    renderLegend();
    animate();
  }

  // ---------- pointer ----------

  function pointer(e) {
    const r = dom.cv.getBoundingClientRect();
    // Floating windows are CSS-zoomed: the canvas's own size over its
    // on-screen size undoes the zoom.
    const sx = dom.cv.clientWidth / (r.width || 1), sy = dom.cv.clientHeight / (r.height || 1);
    return [(e.clientX - r.left) * sx, (e.clientY - r.top) * sy];
  }

  function hitBox(e) {
    if (DR.cards) return null;
    const [x, y] = pointer(e);
    for (let j = 0; j < DR.rects.length; j++) {
      const q = DR.rects[j];
      if (q && x >= q.x && x < q.x + q.w && y >= q.y && y < q.y + q.h) return j;
    }
    return null;
  }

  function hitPage(e) {
    if (!DR.cards && DR.cell < 9) return null;
    const [x, y] = pointer(e);
    const hx = DR.cards ? (CARD_W - 6) / 2 : Math.min(DR.cell * 0.5, 12);
    const hy = DR.cards ? CARD_H / 2 : hx;
    for (const i of DR.S) {
      const p = S.pages[i];
      if (Math.abs(p.x - x) <= hx && Math.abs(p.y - y) <= hy) return i;
    }
    return null;
  }

  function showTip(e, html) {
    const r = dom.stage.getBoundingClientRect();
    const sx = dom.stage.clientWidth / (r.width || 1);
    dom.tip.innerHTML = html;
    dom.tip.hidden = false;
    let x = (e.clientX - r.left) * sx + 14, y = (e.clientY - r.top) * sx + dom.stage.scrollTop + 14;
    const tw = dom.tip.offsetWidth;
    if (x + tw > dom.stage.clientWidth - 4) x = Math.max(4, x - tw - 28);
    dom.tip.style.left = x + "px";
    dom.tip.style.top = y + "px";
  }

  function bindMap() {
    const cv = dom.cv;
    cv.addEventListener("mousemove", (e) => {
      if (!S.data || animating) return;
      const i = hitPage(e);
      if (i !== null) {
        const kind = S.data.kinds[S.data.pages.kind[i]];
        showTip(e, `<b>${esc(S.data.pages.title[i])}</b><div class="lib-tip-sub">${esc(S.data.pages.rel[i])}${kind ? " - " + esc(kind) : ""}</div><div class="lib-tip-hint">Click to read</div>`);
        cv.style.cursor = "pointer";
        return;
      }
      const j = hitBox(e);
      if (j === null) { dom.tip.hidden = true; cv.style.cursor = ""; return; }
      const k = DR.kids[j], row = S.data.groups[k.g];
      cv.style.cursor = "pointer";
      showTip(e, `<b>${esc(groupLabel(k.g))}</b>${row[3] && row[4] ? `<div class="lib-tip-sub">${esc(row[4])}</div>` : ""}`
        + `<div class="lib-tip-sub">${LEVEL[row[2]] || ""} - ${plural(k.n, "page")}</div>`
        + `<div class="lib-tip-hint">${DR.leaf ? "No finer subjects here" : "Double-click to break it apart - click for its detail"}</div>`);
    });
    cv.addEventListener("mouseleave", () => { dom.tip.hidden = true; });
    cv.addEventListener("click", (e) => {
      dom.tip.hidden = true;
      if (!S.data || animating || e.detail > 1) return;
      const i = hitPage(e);
      const j = i === null ? hitBox(e) : null;
      if (i === null && j === null) return;
      const go = () => (i !== null ? openPage(S.data.pages.rel[i], { push: true })
        : openGroup(DR.kids[j].g));
      clearTimeout(clickT);
      if (narrow() && i === null) clickT = setTimeout(go, TAP_DELAY_MS);
      else go();
    });
    cv.addEventListener("dblclick", (e) => {
      e.preventDefault();
      clearTimeout(clickT);
      if (!S.data || animating) return;
      if (hitPage(e) !== null && DR.leaf) return;
      const j = hitBox(e);
      if (j === null) return;
      breakApart(DR.kids[j].g);
    });
  }

  function breakApart(g) {
    if (!S.data) return;
    const at = S.stack.indexOf(g);
    if (at >= 0) { drillTo(S.stack.slice(0, at + 1)); return; }
    if (DR.leaf && DR.kids.length === 1 && DR.kids[0].g === g) {
      const h = dom.trail.querySelector(".lib-hint");
      if (h) { h.classList.remove("flash"); void h.offsetWidth; h.classList.add("flash"); }
      return;
    }
    // A box below the current level (from a detail's list, or a page's
    // trail) opens with its whole path, so Up a level walks back through it.
    const members = currentSet();
    const sample = members.find((i) => S.chains[i].includes(g));
    if (sample === undefined) {
      S.base = null;
      S.query = "";
      dom.q.value = "";
      const any = S.pages.findIndex((p) => S.chains[p.i].includes(g));
      if (any < 0) return;
      const chain = S.chains[any];
      drillTo(chain.slice(0, chain.indexOf(g) + 1));
      return;
    }
    const chain = S.chains[sample];
    const from = S.stack.length ? chain.indexOf(S.stack[S.stack.length - 1]) + 1 : 0;
    const path = chain.slice(from, chain.indexOf(g) + 1);
    // Levels every page of the set shares add nothing to the trail.
    const useful = path.filter((h) => members.some((i) => !S.chains[i].includes(h)) || h === g);
    drillTo(S.stack.concat(useful));
  }

  // ---------- the bar: filter, machine output, subsets, more ----------

  function setBase(kind, label, rels, extra, quiet) {
    if (!rels) { S.base = null; }
    else {
      const members = rels.map((r) => S.relIx.get(r)).filter((i) => i !== undefined);
      S.base = { kind, label, rels, members, extra: extra || {} };
    }
    S.stack = [];
    if (!quiet) { layout(); renderTrail(); renderLegend(); animate(); }
  }

  function applyQuery(q, quiet) {
    S.query = q;
    if (!q.trim()) { setBase(null, null, null, null, quiet); return; }
    const pq = parseQuery(q), P = S.data.pages;
    const rels = [];
    for (let i = 0; i < P.rel.length; i++) {
      if (matchPage(pq, P.title[i], P.rel[i], P.tags[i], S.data.kinds[P.kind[i]])) rels.push(P.rel[i]);
    }
    setBase("filter", `Filter "${q.trim()}" (${fmt(rels.length)})`, rels, { query: q.trim() }, quiet);
  }

  function bindBar() {
    let qt = null;
    dom.q.addEventListener("input", () => {
      clearTimeout(qt);
      qt = setTimeout(() => { if (S.data) applyQuery(dom.q.value); }, 220);
    });
    dom.q.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { dom.q.value = ""; if (S.data) applyQuery(""); }
    });
    dom.vault.addEventListener("change", () => {
      S.vault = dom.vault.value;
      S.data = null; S.base = null; S.stack = []; S.query = ""; dom.q.value = "";
      S.page = null; S.side = null; S.history = []; S.hpos = -1;
      saveState();
      loadLibrary({ vault: S.vault });
    });
    dom.machine.addEventListener("click", async () => {
      S.machine = !S.machine;
      saveState();
      renderMachine();
      if (S.data) await loadMap();
    });
    dom.subsetsBtn.addEventListener("click", () => toggleSubsets());
    dom.more.addEventListener("click", (e) => {
      const r = dom.more.getBoundingClientRect();
      const unnamed = S.data ? S.data.groups.filter((g) => ["region", "subject", "detail"].includes(g[2]) && !g[3]).length : 0;
      showContextMenu(r.left, r.bottom + 4, [
        { label: "Rebuild the subject map", action: startBuild },
        unnamed ? { label: `Name ${fmt(unnamed)} subjects (a Vira job)`, action: nameSubjects } : null,
        { label: S.machine ? "Hide machine output" : "Show machine output", action: () => dom.machine.click() },
        { label: "Copy a link to this view", action: () => copyLink(S.page ? S.page.rel : "") },
      ]);
      e.stopPropagation();
    });
    dom.trail.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.classList.contains("lib-up")) upLevel();
      else if (b.classList.contains("lib-save-set")) saveSubsetPrompt();
      else if (b.dataset.lvl !== undefined) drillTo(S.stack.slice(0, +b.dataset.lvl));
    });
    dom.root.addEventListener("keydown", onKey);
    dom.root.tabIndex = -1;
    document.addEventListener("click", (e) => {
      if (dom.pop.hidden || dom.pop.contains(e.target) || e.target.closest?.("[data-pop-opener]")) return;
      dom.pop.hidden = true;
    });
  }

  function upLevel() {
    if (S.stack.length) drillTo(S.stack.slice(0, -1));
    else if (S.base) { S.query = ""; dom.q.value = ""; setBase(null); }
  }

  function onKey(e) {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable) return;
    if (e.key === "Escape" || e.key === "Backspace") {
      if (!dom.pop.hidden) { dom.pop.hidden = true; return; }
      if (sheetMode() && dom.root.classList.contains("lib-sheet-open")) { closeSheet(); return; }
      upLevel();
      e.preventDefault();
    } else if (e.altKey && e.key === "ArrowLeft") { historyGo(-1); e.preventDefault(); }
    else if (e.altKey && e.key === "ArrowRight") { historyGo(1); e.preventDefault(); }
    else if (e.key === "[" && S.page?.prev) openPage(S.page.prev.rel, { push: true });
    else if (e.key === "]" && S.page?.next) openPage(S.page.next.rel, { push: true });
  }

  async function nameSubjects() {
    try {
      const r = await post("/api/library/names", { vault: S.vault, machine: S.machine });
      toast("Naming the subjects: a Vira job is writing the names", [["Watch", () => openJob(r.job_id)]]);
      watchNames(r.job_id);
    } catch (e) {
      toast(`Could not start naming: ${errText(e)}`);
    }
  }

  function watchNames(jid) {
    S.namesPoll?.stop();
    S.namesPoll = startPoll(async (h) => {
      const j = await api(`/api/jobs/${encodeURIComponent(jid)}`);
      if (!["done", "error", "cancelled", "stopped"].includes(j.status)) return;
      h.stop();
      await loadMap();
      toast(j.status === "done" ? "Subject names are on the map" : `The naming job ${j.status}`);
    }, 8000, 45 * 60 * 1000);
  }

  function copyLink(rel) {
    const url = location.origin + "/" + libraryHash(S.vault, rel);
    copyText(url).then(() => toast("Link copied"), () => toast(url));
  }

  function libraryHash(vault, rel) {
    return "#library/" + encodeURIComponent(vault)
      + (rel ? "/" + rel.split("/").map(encodeURIComponent).join("/") : "");
  }

  // ---------- subsets ----------

  function currentSetRels() {
    return DR.S.map((i) => S.data.pages.rel[i]);
  }

  function currentSetName() {
    if (S.stack.length) return groupLabel(S.stack[S.stack.length - 1]);
    if (S.base && S.base.kind === "filter") return S.base.extra.query;
    return S.base ? S.base.label : S.data.name;
  }

  async function saveSubsetPrompt(rels, name, origin) {
    if (!S.data) return;
    rels = rels || currentSetRels();
    if (!rels.length) { toast("Nothing to save: this set is empty"); return; }
    const def = name || currentSetName();
    dom.pop.hidden = false;
    dom.pop.className = "lib-pop lib-pop-save";
    dom.pop.innerHTML = `<form class="lib-save-form">
      <label>Save ${plural(rels.length, "page")} as a subset</label>
      <input class="search" name="name" maxlength="80" value="${esc(def)}" aria-label="Subset name">
      <div class="lib-row"><button class="btn small primary" type="submit">Save</button>
      <button class="btn small lib-cancel" type="button">Cancel</button></div></form>`;
    placePop(dom.trail);
    const form = dom.pop.querySelector("form");
    form.name.focus();
    form.name.select();
    dom.pop.querySelector(".lib-cancel").onclick = () => { dom.pop.hidden = true; };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const nm = form.name.value.trim();
      if (!nm) return;
      try {
        const body = { vault: S.vault, name: nm, rels,
                       origin: origin || { kind: S.stack.length ? "box" : S.base?.kind || "pick",
                                           group: S.stack.length ? groupId(S.stack[S.stack.length - 1]) : "",
                                           query: S.base?.extra?.query || "",
                                           trail: S.stack.map(groupLabel) } };
        const saved = await post("/api/library/subsets", body);
        S.subsets = (await api(`/api/library/subsets`)).subsets || [];
        dom.pop.hidden = true;
        toast(`Saved "${saved.name}" (${plural(saved.rels.length, "page")})`,
              [["Open", () => openSubset(saved.id)]]);
      } catch (err) {
        toast(`Could not save: ${errText(err)}`);
      }
    };
  }

  function placePop(anchor) {
    const ra = anchor.getBoundingClientRect(), rr = dom.root.getBoundingClientRect();
    const s = dom.root.clientWidth / (rr.width || 1);
    dom.pop.style.left = Math.max(8, (ra.left - rr.left) * s) + "px";
    dom.pop.style.top = ((ra.bottom - rr.top) * s + 4) + "px";
  }

  async function toggleSubsets() {
    if (!dom.pop.hidden && dom.pop.classList.contains("lib-pop-subsets")) { dom.pop.hidden = true; return; }
    S.subsets = (await api("/api/library/subsets").catch(() => ({ subsets: S.subsets }))).subsets || [];
    const mine = S.subsets.filter((s) => s.vault === S.vault);
    dom.pop.className = "lib-pop lib-pop-subsets";
    dom.pop.innerHTML = `<div class="lib-pop-head">Saved subsets</div>`
      + (mine.length ? mine.map((s) => `<div class="lib-subset" data-id="${esc(s.id)}">
          <button class="lib-subset-open" type="button"><b>${esc(s.name)}</b>
            <span class="lib-faint">${plural(s.n, "page")}${s.origin?.trail?.length ? " - " + esc(s.origin.trail.join(" > ")) : s.origin?.query ? " - filter " + esc(s.origin.query) : ""}</span></button>
          <button class="btn tiny lib-subset-galaxy" type="button" title="Lay it out on its own in the galaxy">Galaxy</button>
          <button class="btn tiny lib-subset-del" type="button" title="Delete this subset">Delete</button></div>`).join("")
        : `<p class="lib-faint">None yet. Drill into a box or filter the map, then Save subset.</p>`)
      + (S.data && (S.stack.length || S.base) ? `<button class="btn small lib-subset-save" type="button">Save the current set</button>` : "");
    dom.pop.hidden = false;
    placePop(dom.subsetsBtn);
    dom.pop.onclick = async (e) => {
      const row = e.target.closest(".lib-subset");
      if (e.target.closest(".lib-subset-save")) { saveSubsetPrompt(); return; }
      if (!row) return;
      const id = row.dataset.id;
      if (e.target.closest(".lib-subset-open")) { dom.pop.hidden = true; openSubset(id); }
      else if (e.target.closest(".lib-subset-galaxy")) {
        const s = await api(`/api/library/subsets/${encodeURIComponent(id)}`);
        dom.pop.hidden = true;
        toGalaxy(s.name, s.rels, s.id);
      } else if (e.target.closest(".lib-subset-del")) {
        const b = e.target.closest(".lib-subset-del");
        if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = "Delete?"; return; }
        S.subsets = (await del(`/api/library/subsets/${encodeURIComponent(id)}`)).subsets || [];
        row.remove();
      }
    };
  }

  async function openSubset(id) {
    try {
      const s = await api(`/api/library/subsets/${encodeURIComponent(id)}`);
      if (s.vault !== S.vault) {
        S.vault = s.vault;
        S.data = null;
        await loadLibrary({ vault: s.vault });
      }
      if (!S.data) return;
      S.query = "";
      dom.q.value = "";
      setBase("subset", `${s.name} (${fmt(s.rels.length)})`, s.rels, { id: s.id });
      showSubsetDetail(s);
    } catch (e) {
      toast(`Could not open the subset: ${errText(e)}`);
    }
  }

  /* Hand a set of pages to the galaxy, laid out on its own. The galaxy
     owns that layout (World subsets); the Library sends World node ids. */
  async function toGalaxy(name, rels, subsetId) {
    if (!rels || !rels.length) { toast("Nothing to show: the set is empty"); return; }
    try {
      const { ids } = await post("/api/library/world-ids", { vault: S.vault, rels });
      if (typeof window.worldOpenSubset === "function") {
        openApp("atlas");
        const res = await window.worldOpenSubset({ name, ids, librarySubset: subsetId || "" });
        if (subsetId && res && res.id) {
          put(`/api/library/subsets/${encodeURIComponent(subsetId)}/world`, { world_subset: res.id }).catch(() => {});
        }
        return;
      }
      toast("The galaxy cannot lay a set out on its own yet: that arrives with World subsets. "
        + "Opening the World instead.");
      openApp("atlas");
    } catch (e) {
      toast(`Could not send it to the galaxy: ${errText(e)}`);
    }
  }

  // ---------- the side: details and the reader ----------

  function openSheet() { if (sheetMode()) dom.root.classList.add("lib-sheet-open"); }
  function closeSheet() { dom.root.classList.remove("lib-sheet-open"); }

  function showVaultDetail() {
    if (!S.data) return;
    S.side = { kind: "vault" };
    const d = S.data, m = d.machine;
    const areas = d.groups.map((row, g) => [row, g]).filter(([row]) => row[2] === "area");
    const counts = new Map();
    for (const chain of S.chains) counts.set(chain[0], (counts.get(chain[0]) || 0) + 1);
    dom.sideBody.innerHTML = `<div class="lib-detail">
      <div class="lib-eyebrow">Library</div>
      <h3 class="lib-title">${esc(d.name)}</h3>
      <p class="lib-sub">${plural(d.pages.rel.length, "page")} shown of ${fmt(d.total)}; ${fmt(d.embedded)} placed by their embeddings, the rest by their words.</p>
      <p class="lib-what">Double-click a box to break it into its subjects, down to single pages. Click once for its detail; click a page to read it here.</p>
      <div class="lib-sec"><div class="lib-sec-head">Areas</div><div class="lib-rows">${
        areas.filter(([, g]) => counts.get(g)).sort((a, b) => counts.get(b[1]) - counts.get(a[1])).map(([row, g]) =>
          `<button class="lib-rowbtn" type="button" data-group="${g}"><span class="t">${esc(groupLabel(g))}</span><span class="m">${fmt(counts.get(g))}</span></button>`).join("")
      }</div></div>
      ${m && m.dirs.length ? `<div class="lib-sec"><div class="lib-sec-head">Machine output (${m.source === "owner" ? "your choice" : "suggested"})</div>
        <p class="lib-faint">${esc(m.dirs.map((x) => x.startsWith("!") ? `${x.slice(1)} (kept)` : x).join(", "))} - ${plural(m.pages, "page")}. Open an area or folder's detail to change it.</p></div>` : ""}
    </div>`;
  }

  async function openGroup(g, opts = {}) {
    if (!S.data) return;
    S.sel = { kind: "group", g };
    draw(1);
    S.side = { kind: "group", g };
    const gen = ++S.pageGen;
    dom.sideBody.innerHTML = `<div class="lib-detail"><div class="lib-eyebrow">${esc(LEVEL[groupLevel(g)] || "")}</div><h3 class="lib-title">${esc(groupLabel(g))}</h3><p class="lib-faint">Loading...</p></div>`;
    openSheet();
    let d;
    try {
      d = await api(`/api/library/group?vault=${encodeURIComponent(S.vault)}&id=${encodeURIComponent(groupId(g))}&machine=${S.machine ? 1 : 0}`);
    } catch (e) {
      if (gen === S.pageGen) dom.sideBody.innerHTML = `<div class="lib-detail"><p class="lib-err">${esc(errText(e))}</p></div>`;
      return;
    }
    if (gen !== S.pageGen) return;
    const canSplit = d.children.length > 1;
    const rows = (list) => `<div class="lib-rows">${list.map((p) =>
      `<button class="lib-rowbtn" type="button" data-rel="${esc(p.rel)}"><span class="t">${esc(p.title)}</span>${p.kind ? `<span class="d">${esc(p.kind)}</span>` : ""}</button>`).join("")}</div>`;
    dom.sideBody.innerHTML = `<div class="lib-detail">
      ${sheetMode() ? `<button class="btn tiny lib-sheet-close" type="button">Back to the map</button>` : ""}
      <div class="lib-eyebrow">${esc(LEVEL[d.level] || "")}${d.named || !["region", "subject", "detail"].includes(d.level) ? "" : " - shown by its words until it is named"}</div>
      <h3 class="lib-title">${esc(d.label)}</h3>
      <p class="lib-sub">${plural(d.n, "page")}${d.n_all !== d.n ? ` here (${fmt(d.n_all)} with machine output)` : ""}${d.terms.length ? " - " + esc(d.terms.slice(0, 6).join(", ")) : ""}</p>
      <div class="lib-trailchips">${d.trail.slice(0, -1).map((t) =>
        `<button class="lib-chip" type="button" data-gid="${esc(t.id)}">${esc(t.label)}</button>`).join('<span class="lib-sep">&rsaquo;</span>')}</div>
      <div class="lib-row lib-actions">
        <button class="btn small primary lib-break" type="button"${canSplit || d.pages.length ? "" : " disabled"}>${canSplit ? "Break it apart" : "Show its pages"}</button>
        <button class="btn small lib-save-group" type="button" data-pop-opener>Save as subset</button>
        <button class="btn small lib-galaxy-group" type="button">Show in galaxy</button>
        ${d.folder ? `<button class="btn small lib-machine-toggle" type="button" data-machine="${d.machine ? 0 : 1}">${d.machine ? "Count as knowledge" : "Count as machine output"}</button>` : ""}
      </div>
      ${canSplit ? `<div class="lib-sec"><div class="lib-sec-head">Breaks into ${fmt(d.children.length)} ${KIDS[d.children[0].level] || "groups"}</div><div class="lib-rows">${d.children.map((c) =>
        `<button class="lib-rowbtn" type="button" data-gid="${esc(c.id)}"><span class="t">${esc(c.label)}</span><span class="m">${fmt(c.n)}</span>${c.terms.length && c.label !== c.terms.slice(0, 3).join(", ") ? `<span class="d">${esc(c.terms.slice(0, 4).join(", "))}</span>` : ""}</button>`).join("")}</div></div>` : ""}
      ${d.hubs.length ? `<div class="lib-sec"><div class="lib-sec-head">Most linked</div>${rows(d.hubs)}</div>` : ""}
      ${d.n > d.pages.length && d.reps.length ? `<div class="lib-sec"><div class="lib-sec-head">Nearest its centre</div>${rows(d.reps)}</div>` : ""}
      ${d.n <= d.pages.length ? `<div class="lib-sec"><div class="lib-sec-head">All ${plural(d.n, "page")}</div>${rows(d.pages)}</div>`
        : `<p class="lib-faint">${plural(d.n, "page")}: break it apart to reach every one.</p>`}
    </div>`;
    const q = (s) => dom.sideBody.querySelector(s);
    q(".lib-break")?.addEventListener("click", () => { breakApart(g); closeSheet(); });
    q(".lib-save-group")?.addEventListener("click", () => saveSubsetPrompt(
      groupRels(g), d.label, { kind: "box", group: d.id, trail: d.trail.map((t) => t.label) }));
    q(".lib-galaxy-group")?.addEventListener("click", () => toGalaxy(d.label, groupRels(g)));
    q(".lib-machine-toggle")?.addEventListener("click", async (e) => {
      try {
        await post("/api/library/machine", { vault: S.vault, folder: d.folder, machine: e.target.dataset.machine === "1" });
        toast(e.target.dataset.machine === "1" ? `${d.folder} now counts as machine output` : `${d.folder} now counts as knowledge`);
        await loadMap();
      } catch (err) { toast(`Could not change it: ${errText(err)}`); }
    });
  }

  function groupRels(g) {
    const base = baseMembers();
    return base.filter((i) => S.chains[i].includes(g)).map((i) => S.data.pages.rel[i]);
  }

  function showSubsetDetail(s) {
    S.side = { kind: "subset", id: s.id };
    dom.sideBody.innerHTML = `<div class="lib-detail">
      <div class="lib-eyebrow">Saved subset</div>
      <h3 class="lib-title">${esc(s.name)}</h3>
      <p class="lib-sub">${plural(s.rels.length, "page")}${s.origin?.trail?.length ? " - from " + esc(s.origin.trail.join(" > ")) : s.origin?.query ? " - filter " + esc(s.origin.query) : ""}</p>
      <div class="lib-row lib-actions">
        <button class="btn small primary lib-galaxy-subset" type="button">Show in galaxy</button>
        <button class="btn small lib-close-subset" type="button">Back to the whole vault</button>
      </div>
      <p class="lib-faint">The map now shows only this subset; double-click its boxes to break them apart.</p>
    </div>`;
    dom.sideBody.querySelector(".lib-galaxy-subset").onclick = () => toGalaxy(s.name, s.rels, s.id);
    dom.sideBody.querySelector(".lib-close-subset").onclick = () => { setBase(null); showVaultDetail(); };
  }

  // ---------- the reader ----------

  async function openPage(rel, opts = {}) {
    if (!rel) return;
    if (opts.push) {
      S.history = S.history.slice(0, S.hpos + 1);
      if (S.history[S.history.length - 1] !== rel) S.history.push(rel);
      S.hpos = S.history.length - 1;
    }
    S.sel = { kind: "page", rel };
    const gen = ++S.pageGen;
    S.side = { kind: "page", rel };
    if (!opts.quiet) openSheet();
    if (!S.page || S.page.rel !== rel) {
      dom.sideBody.innerHTML = `<div class="lib-reader"><div class="lib-reader-scroll"><p class="lib-faint">Opening ${esc(rel)}...</p></div></div>`;
    }
    let d;
    try {
      d = await api(`/api/library/page?vault=${encodeURIComponent(S.vault)}&path=${encodeURIComponent(rel)}&machine=${S.machine ? 1 : 0}`);
    } catch (e) {
      if (gen !== S.pageGen) return;
      dom.sideBody.innerHTML = `<div class="lib-reader"><div class="lib-reader-scroll"><p class="lib-err">Could not open ${esc(rel)}: ${esc(errText(e))}</p></div></div>`;
      return;
    }
    if (gen !== S.pageGen) return;
    const prevConst = S.page && S.page.constellation ? S.page : null;
    S.page = d;
    saveState();
    if (S.data) draw(1);
    renderReader(d, opts.anchor, prevConst);
  }

  function historyGo(step) {
    const to = S.hpos + step;
    if (to < 0 || to >= S.history.length) return;
    S.hpos = to;
    openPage(S.history[to]);
  }

  function renderReader(d, anchor, prev) {
    const ctx = { linkmap: d.linkmap || {}, vault: d.vault };
    const { props, body } = splitFrontmatter(d.text || "");
    const sub = d.subject;
    const counts = { links: d.links.length, backlinks: d.backlinks.length,
                     similar: d.similar.length, same: d.same.length };
    dom.sideBody.innerHTML = `<div class="lib-reader">
      <div class="lib-reader-head">
        ${sheetMode() ? `<button class="btn tiny lib-sheet-close" type="button">Map</button>` : ""}
        <button class="btn tiny lib-hist" data-step="-1" type="button" title="Back (Alt+Left)"${S.hpos > 0 ? "" : " disabled"}>&lsaquo;</button>
        <button class="btn tiny lib-hist" data-step="1" type="button" title="Forward (Alt+Right)"${S.hpos < S.history.length - 1 ? "" : " disabled"}>&rsaquo;</button>
        <div class="lib-trailchips">${d.trail.map((t) =>
          `<button class="lib-chip" type="button" data-gid="${esc(t.id)}" title="Show ${esc(t.label)} on the map">${esc(t.label)}</button>`).join('<span class="lib-sep">&rsaquo;</span>')}</div>
      </div>
      <div class="lib-reader-scroll">
        <h2 class="lib-page-title">${esc(d.title)}</h2>
        <div class="lib-page-meta">${[d.kind ? esc(d.kind) : "", d.words ? plural(d.words, "word") : "",
          d.mtime ? "updated " + ago(d.mtime) : "", d.machine ? "machine output" : "",
          `<span class="lib-path" title="${esc(d.path)}">${esc(d.path)}</span>`].filter(Boolean).join(" - ")}</div>
        ${propsHtml(props, ctx)}
        <article class="lib-doc">${renderMarkdown(body, ctx)}</article>
      </div>
      <div class="lib-drawer" hidden></div>
      <div class="lib-dock">
        <button class="lib-mini" type="button" title="The page's constellation: its links and similar pages">
          <svg class="lib-mini-svg" viewBox="0 0 96 44" aria-hidden="true"></svg></button>
        <div class="lib-dock-btns">
          <button class="lib-dbtn" data-drawer="links" type="button" title="Pages this one links to">Links <b>${fmt(counts.links)}</b></button>
          <button class="lib-dbtn" data-drawer="backlinks" type="button" title="Pages that link here">Backlinks <b>${fmt(counts.backlinks)}</b></button>
          <button class="lib-dbtn" data-drawer="similar" type="button" title="Pages most like this one">Similar <b>${fmt(counts.similar)}</b></button>
          <button class="lib-dbtn" data-drawer="same" type="button" title="${sub ? esc(sub.label) : "Same subject"}">Subject <b>${sub ? fmt(sub.n) : 0}</b></button>
          <span class="lib-dsep"></span>
          <button class="lib-dbtn lib-prev" type="button" title="Previous in this subject ([)"${d.prev ? "" : " disabled"}>&lsaquo; Prev</button>
          <span class="lib-pos">${sub ? `${fmt(sub.at)} / ${fmt(sub.n)}` : ""}</span>
          <button class="lib-dbtn lib-next" type="button" title="Next in this subject (])"${d.next ? "" : " disabled"}>Next &rsaquo;</button>
          <span class="lib-dsep"></span>
          <button class="lib-dbtn" data-drawer="ask" type="button" title="Ask about this page">Ask</button>
          <button class="lib-dbtn lib-galaxy" type="button" title="This page and its connections, laid out on their own in the galaxy">Galaxy</button>
          <button class="lib-dbtn lib-newtab" type="button" title="Open it in its own tab">New tab</button>
          <button class="lib-dbtn lib-copy" type="button" title="Copy its vault path">Copy path</button>
          <button class="lib-dbtn lib-onmap" type="button" title="Show it on the map">On map</button>
        </div>
      </div>
    </div>`;
    drawMini(d);
    const scroll = dom.sideBody.querySelector(".lib-reader-scroll");
    if (anchor) {
      const h = scroll.querySelector(`#${CSS.escape(anchor)}`);
      if (h) h.scrollIntoView({ block: "start" });
    }
    if (S.drawer) openDrawer(S.drawer, prev);
  }

  function bindSide() {
    dom.sideBody.addEventListener("click", (e) => {
      const t = e.target;
      const link = t.closest(".lib-link");
      if (link && dom.sideBody.contains(link)) { e.preventDefault(); followLink(link); return; }
      const chip = t.closest("[data-gid]");
      if (chip) {
        const g = S.groupIx.get(chip.dataset.gid);
        if (g === undefined) return;
        // In the reader a trail chip moves the map and keeps the page open;
        // in a detail it opens that level's detail.
        if (chip.classList.contains("lib-chip") && S.side?.kind === "page") {
          closeSheet();
          breakApart(g);
          return;
        }
        openGroup(g);
        return;
      }
      const row = t.closest("[data-group]");
      if (row) { openGroup(+row.dataset.group); return; }
      const rel = t.closest("[data-rel]");
      if (rel) { openPage(rel.dataset.rel, { push: true }); return; }
      if (t.closest(".lib-sheet-close")) { closeSheet(); return; }
      const hist = t.closest(".lib-hist");
      if (hist) { historyGo(+hist.dataset.step); return; }
      const d = S.page;
      if (!d) return;
      if (t.closest(".lib-prev") && d.prev) openPage(d.prev.rel, { push: true });
      else if (t.closest(".lib-next") && d.next) openPage(d.next.rel, { push: true });
      else if (t.closest(".lib-mini")) openDrawer(S.drawer === "constellation" ? "" : "constellation");
      else if (t.closest("[data-drawer]")) {
        const which = t.closest("[data-drawer]").dataset.drawer;
        openDrawer(S.drawer === which ? "" : which);
      } else if (t.closest(".lib-galaxy")) {
        const near = (d.constellation?.nodes || []).map((n) => n.rel);
        toGalaxy(d.title, [d.rel].concat(near));
      } else if (t.closest(".lib-newtab")) {
        window.open("/" + libraryHash(S.vault, d.rel), "_blank", "noopener");
      } else if (t.closest(".lib-copy")) {
        copyText(d.path).then(() => toast(`Copied ${d.path}`), () => toast(d.path));
      } else if (t.closest(".lib-onmap")) {
        showOnMap(d.rel);
      }
    });
    dom.sideBody.addEventListener("submit", (e) => {
      if (!e.target.classList.contains("lib-ask")) return;
      e.preventDefault();
      askPage(e.target);
    });
  }

  function followLink(link) {
    if (link.dataset.rel) {
      openPage(link.dataset.rel, { push: true, anchor: link.dataset.anchor });
    } else if (link.dataset.asset) {
      window.open("/api/vault/asset?path=" + encodeURIComponent(link.dataset.asset), "_blank", "noopener");
    } else if (link.dataset.anchor) {
      const h = dom.sideBody.querySelector(`#${CSS.escape(link.dataset.anchor)}`);
      if (h) h.scrollIntoView({ block: "start", behavior: "smooth" });
    } else if (link.dataset.dead) {
      toast(`No page called "${link.dataset.dead}" in this vault`);
    }
  }

  function showOnMap(rel) {
    if (!S.data) return;
    const i = S.relIx.get(rel);
    if (i === undefined) {
      toast(S.machine ? "This page is not on the map" : "This page is machine output: show machine output to see it on the map");
      return;
    }
    if (S.base && !S.base.members.includes(i)) { S.base = null; S.query = ""; dom.q.value = ""; }
    closeSheet();
    drillTo(S.chains[i].slice());
  }

  function drawerList(items, empty) {
    if (!items.length) return `<p class="lib-faint">${esc(empty)}</p>`;
    return `<div class="lib-rows">${items.map((p) =>
      `<button class="lib-rowbtn" type="button" data-rel="${esc(p.rel)}"><span class="t">${esc(p.title)}</span>`
      + (p.score ? `<span class="m">${p.score}%</span>` : "")
      + `<span class="d">${esc(p.rel)}</span></button>`).join("")}</div>`;
  }

  function openDrawer(which, prev) {
    S.drawer = which;
    const drawer = dom.sideBody.querySelector(".lib-drawer");
    const d = S.page;
    if (!drawer || !d) return;
    dom.sideBody.querySelectorAll("[data-drawer]").forEach((b) =>
      b.classList.toggle("on", b.dataset.drawer === which));
    dom.sideBody.querySelector(".lib-mini")?.classList.toggle("on", which === "constellation");
    if (!which) { drawer.hidden = true; drawer.innerHTML = ""; return; }
    drawer.hidden = false;
    const head = (title, extra) => `<div class="lib-drawer-head"><b>${esc(title)}</b>${extra || ""}<button class="btn tiny lib-drawer-x" type="button" aria-label="Close">Close</button></div>`;
    if (which === "links") drawer.innerHTML = head(`Links out (${fmt(d.links.length)})`) + drawerList(d.links, "This page links to no other page.");
    else if (which === "backlinks") drawer.innerHTML = head(`Backlinks (${fmt(d.backlinks.length)})`) + drawerList(d.backlinks, "No page links here.");
    else if (which === "similar") drawer.innerHTML = head("Similar pages") + drawerList(d.similar, "No page is close enough in meaning.");
    else if (which === "same") {
      drawer.innerHTML = head(d.subject ? `Same subject: ${d.subject.label}` : "Same subject",
        d.subject ? `<button class="btn tiny lib-same-map" type="button">Break it apart</button>` : "")
        + drawerList(d.same, "This page is alone in its subject.");
      drawer.querySelector(".lib-same-map")?.addEventListener("click", () => {
        const g = S.groupIx.get(d.subject.id);
        if (g !== undefined) { breakApart(g); closeSheet(); }
      });
    } else if (which === "ask") {
      drawer.innerHTML = head("Ask about this page")
        + `<form class="lib-ask"><input class="search" name="q" autocomplete="off" placeholder="What does it say about...?" aria-label="Question about this page">
           <button class="btn small primary" type="submit">Ask</button></form><div class="lib-answer"></div>`;
      drawer.querySelector("input").focus();
    } else if (which === "constellation") {
      drawer.innerHTML = head("Constellation", `<span class="lib-faint lib-const-key">inner ring: links (solid out, dashed in) - outer ring: similar - click a star to walk there</span>`)
        + `<svg class="lib-const" role="img" aria-label="This page's connections"></svg>`;
      drawConstellation(drawer.querySelector(".lib-const"), d, prev);
    }
    drawer.querySelector(".lib-drawer-x")?.addEventListener("click", () => openDrawer(""));
  }

  async function askPage(form) {
    const q = form.q.value.trim();
    if (!q || !S.page) return;
    const out = form.parentElement.querySelector(".lib-answer");
    const btn = form.querySelector("button");
    btn.disabled = true;
    out.innerHTML = `<p class="lib-faint">Reading the page and asking...</p>`;
    try {
      const r = await post("/api/library/ask", { vault: S.vault, path: S.page.rel, question: q });
      const linkmap = {};
      for (const c of r.citations || []) {
        const rel = String(c.path || "").replace(/^@[^/]+\//, "");
        linkmap[String(c.ref || "").toLowerCase()] = { rel, title: c.title };
      }
      out.innerHTML = `<div class="lib-doc lib-answer-text">${renderMarkdown(r.answer || "", { linkmap, vault: S.vault })}</div>`
        + (r.citations?.length ? `<div class="lib-faint">Sources: ${r.citations.map((c) =>
          `<a class="lib-link" data-rel="${esc(String(c.path || "").replace(/^@[^/]+\//, ""))}">${esc(c.title || c.ref)}</a>`).join(", ")}</div>` : "");
    } catch (e) {
      out.innerHTML = `<p class="lib-err">${esc(errText(e))}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- the constellation ----------

  const NS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };
  const leafColor = (leaf, center) => (leaf === center ? cssVar("--accent-bright", "#a39c8d")
    : PAL[Math.abs(leaf) % PAL.length]);

  function drawMini(d) {
    const svg = dom.sideBody.querySelector(".lib-mini-svg");
    if (!svg) return;
    const nodes = d.constellation?.nodes || [];
    const { center, pos } = constellationLayout(nodes, 96, 44);
    svg.innerHTML = "";
    nodes.forEach((n, k) => svg.appendChild(svgEl("line", {
      x1: center[0], y1: center[1], x2: pos[k][0], y2: pos[k][1], class: `lib-c-edge ${n.ring}` })));
    nodes.forEach((n, k) => svg.appendChild(svgEl("circle", {
      cx: pos[k][0], cy: pos[k][1], r: n.ring === "similar" ? 1.6 : 2.2,
      fill: leafColor(n.leaf, d.constellation.center_leaf) })));
    svg.appendChild(svgEl("circle", { cx: center[0], cy: center[1], r: 3.6, class: "lib-c-center" }));
  }

  /* The full constellation, drawn as SVG. When it re-centres on a star the
     owner clicked, stars that stay glide to their new places (the clicked
     one to the centre) instead of the picture being redrawn cold. */
  function drawConstellation(svg, d, prev) {
    const box = svg.getBoundingClientRect();
    const W = Math.max(280, svg.clientWidth || box.width || 520), H = Math.max(220, svg.clientHeight || 280);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const nodes = d.constellation?.nodes || [];
    const { center, pos } = constellationLayout(nodes, W, H);
    const old = new Map();
    if (prev && prev.constellation && prev._pos) {
      old.set(prev.rel, prev._pos.center);
      (prev.constellation.nodes || []).forEach((n, k) => old.set(n.rel, prev._pos.pos[k]));
    }
    d._pos = { center, pos };
    svg.innerHTML = "";
    const gEdges = svgEl("g", {}), gNodes = svgEl("g", {});
    svg.appendChild(gEdges);
    svg.appendChild(gNodes);
    const cl = d.constellation.center_leaf;
    const items = [{ rel: d.rel, title: d.title, ring: "center", leaf: cl, at: center }]
      .concat(nodes.map((n, k) => ({ ...n, at: pos[k] })));
    const edges = [];
    nodes.forEach((n, k) => edges.push({ a: 0, b: k + 1, cls: `lib-c-edge ${n.ring}` }));
    (d.constellation.edges || []).forEach(([a, b]) => edges.push({ a: a + 1, b: b + 1, cls: "lib-c-edge among" }));
    const lineEls = edges.map((e) => {
      const l = svgEl("line", { class: e.cls });
      gEdges.appendChild(l);
      return l;
    });
    const nodeEls = items.map((it, k) => {
      const g = svgEl("g", { class: `lib-c-node ${it.ring}`, tabindex: k ? "0" : "-1" });
      const r = it.ring === "center" ? 9 : it.ring === "similar" ? 4.5 : 6;
      g.appendChild(svgEl("circle", { r, fill: it.ring === "center" ? "var(--accent-bright)" : leafColor(it.leaf, cl) }));
      const label = svgEl("text", { y: it.ring === "center" ? -14 : -9, "text-anchor": "middle" });
      const short = it.title.length > 26 ? it.title.slice(0, 24) + "..." : it.title;
      label.textContent = it.ring === "center" || nodes.length <= 14 || it.ring !== "similar" && nodes.length <= 24 ? short : "";
      g.appendChild(label);
      const tip = svgEl("title", {});
      tip.textContent = `${it.title}${it.score ? ` (${it.score}% similar)` : ""}${it.ring !== "center" ? `\n${{ out: "linked from this page", back: "links to this page", both: "linked both ways", similar: "similar in meaning" }[it.ring] || ""}` : ""}`;
      g.appendChild(tip);
      if (k) {
        g.addEventListener("click", () => openPage(it.rel, { push: true }));
        g.addEventListener("keydown", (e) => { if (e.key === "Enter") openPage(it.rel, { push: true }); });
        g.addEventListener("mouseenter", () => highlight(k, true));
        g.addEventListener("mouseleave", () => highlight(k, false));
      }
      gNodes.appendChild(g);
      return g;
    });
    function highlight(k, on) {
      edges.forEach((e, n) => lineEls[n].classList.toggle("lit", on && (e.a === k || e.b === k)));
      nodeEls[k].classList.toggle("lit", on);
    }
    const from = items.map((it) => old.get(it.rel) || [center[0] + (it.at[0] - center[0]) * 0.2, center[1] + (it.at[1] - center[1]) * 0.2]);
    const place = (u) => {
      const cur = items.map((it, k) => [from[k][0] + (it.at[0] - from[k][0]) * u, from[k][1] + (it.at[1] - from[k][1]) * u]);
      nodeEls.forEach((g, k) => g.setAttribute("transform", `translate(${cur[k][0].toFixed(1)} ${cur[k][1].toFixed(1)})`));
      edges.forEach((e, n) => {
        lineEls[n].setAttribute("x1", cur[e.a][0]); lineEls[n].setAttribute("y1", cur[e.a][1]);
        lineEls[n].setAttribute("x2", cur[e.b][0]); lineEls[n].setAttribute("y2", cur[e.b][1]);
      });
      svg.style.setProperty("--lib-fade", String(Math.min(1, u * 1.6)));
    };
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { place(1); return; }
    const t0 = performance.now();
    const step = (now) => {
      const u = Math.min(1, (now - t0) / 520);
      place(ease(u));
      if (u < 1) requestAnimationFrame(step);
    };
    place(0);
    requestAnimationFrame(step);
  }

  // ---------- entry points ----------

  /* Open the Library on a vault, page or subset; from the hash route
     (#library/<vault>/<path>), another window, or a link. */
  async function openLibrary(opts = {}) {
    // openApp runs viewLoad -> window.loadLibrary synchronously, which takes
    // the pending target; if the window was already up and did not reload,
    // load it here instead.
    S.pendingOpen = opts;
    if (typeof openApp === "function") openApp("library");
    if (S.pendingOpen) {
      const o = S.pendingOpen;
      S.pendingOpen = null;
      await loadLibrary(o);
    }
  }

  window.loadLibrary = (opts) => {
    const o = opts || S.pendingOpen || {};
    S.pendingOpen = null;
    return loadLibrary(o);
  };
  window.openLibrary = openLibrary;
  if (window.__VIRA_TEST__) window.__VIRA_LIBRARY_TESTS__ = {
    squarify, placeIn, splitOf, parseQuery, matchPage, splitFrontmatter,
    renderMarkdown, inline, constellationLayout,
  };
})();

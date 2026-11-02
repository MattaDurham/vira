/* ============================================================
   DESIGN STUDIO — a canvas-first design tool.

   The surface being designed fills the screen. Hover shows what
   you would select; click selects it and its properties appear
   in the right rail as real design controls — a full color
   picker (saturation square, hue/alpha strips, eyedropper,
   palette, gradient stops), sliders, segmented controls, a font
   browser that previews every face in itself. Nothing selected =
   the Styles board: the whole design system as editable tiles.
   Cmd-Z undoes anything. The code is a drawer, one click away,
   still live in both directions. Save commits and pushes to the
   target's repo via the host (/api/design/save).
   ============================================================ */

(function () {
  "use strict";

  var THEME = "taurid";
  var CONTRACT_RE = /^:root\s*{/;
  var THEME_RE = /^:root\[data-theme="?[a-z0-9-]+"?\]/;

  var TARGETS = [
    {
      id: "vira", label: "Vira", url: "/", themed: false, injectSelf: false,
      deskWidth: 1280,
      files: [{ id: "app-style", label: "style.css", path: "/style.css", savePath: "static/style.css" }],
      tokenFileId: "app-style", tokenOpen: CONTRACT_RE,
      fallbackFileId: null, fallbackOpen: null, rulesFileId: "app-style"
    },
    {
      // Every Vira component on one page, wearing the active skin. Same
      // style.css binding as the Vira target, so a token edit here updates
      // every component at once and saves to the app's real stylesheet —
      // the "adjust globally, see it everywhere" surface.
      id: "gallery", label: "Gallery", url: "/gallery.html", themed: false, injectSelf: false,
      deskWidth: 1280,
      files: [{ id: "app-style", label: "style.css", path: "/style.css", savePath: "static/style.css" }],
      tokenFileId: "app-style", tokenOpen: CONTRACT_RE,
      fallbackFileId: null, fallbackOpen: null, rulesFileId: "app-style"
    },
  ];
  var target = TARGETS[0];

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ---------- state ---------- */
  var pristine = {}, working = {};
  var themeTok = {}, contractTok = {}, pThemeTok = {}, pContractTok = {};
  var rules = [];
  var activeFile = null;
  var currentHl = { file: null, lines: [], start: 0 };
  var canvasMode = "design";
  var previewDoc = null, previewWin = null;
  var phoneDoc = null, phoneWin = null, phoneView = null;
  var canvasScale = 1;
  var sel = null;                 // {name, rule?, tokens[], el?, inherited?}
  var undoStack = [], redoStack = [];
  var LH = 17.8, PAD_TOP = 12;
  var localFontFamilies = null;
  var drawerOpen = false;

  /* ============================================================
     Parsing (the proven engine)
     ============================================================ */
  function parseVarLines(lines, from, to) {
    var out = {};
    for (var i = from; i <= to; i++) {
      var m = lines[i] && lines[i].match(/^\s*--([a-z0-9-]+)\s*:\s*([^;]+);/i);
      if (m) out[m[1]] = { value: m[2].trim(), line: i };
    }
    return out;
  }
  function blockRange(lines, openRe) {
    for (var i = 0; i < lines.length; i++) {
      if (openRe.test(lines[i])) {
        var depth = 0;
        for (var j = i; j < lines.length; j++) {
          depth += (lines[j].match(/{/g) || []).length;
          depth -= (lines[j].match(/}/g) || []).length;
          if (depth === 0 && j > i) return { open: i, close: j };
        }
      }
    }
    return { open: -1, close: -1 };
  }
  function tokensFrom(text, openRe) {
    if (!text || !openRe) return {};
    var lines = text.split("\n");
    var b = blockRange(lines, openRe);
    if (b.open < 0) return {};
    return parseVarLines(lines, b.open, b.close);
  }
  function reparse(id) {
    if (id === target.tokenFileId) themeTok = tokensFrom(working[id], target.tokenOpen);
    if (id === target.fallbackFileId) contractTok = tokensFrom(working[id], target.fallbackOpen);
    if (id === target.rulesFileId) reparseRules(working[id]);
  }
  function pristineReparse() {
    pThemeTok = tokensFrom(pristine[target.tokenFileId], target.tokenOpen);
    pContractTok = target.fallbackFileId
      ? tokensFrom(pristine[target.fallbackFileId], target.fallbackOpen) : {};
  }
  function reparseRules(text) {
    rules = [];
    var lines = text.split("\n");
    parseRuleRange(lines, 0, lines.length - 1, null);
  }
  function parseRuleRange(lines, from, to, media) {
    var i = from;
    while (i <= to) {
      var line = lines[i];
      if (/^\s*\/\*/.test(line) || !/{/.test(line)) { i++; continue; }
      var start = i;
      while (start > from && /^[.\w\s,:\[\]"'=>()-]+$/.test(lines[start - 1]) &&
             lines[start - 1].trim() !== "" && !/[;{}]/.test(lines[start - 1])) start--;
      var depth = 0, end = i;
      for (var j = i; j <= to; j++) {
        depth += (lines[j].match(/{/g) || []).length;
        depth -= (lines[j].match(/}/g) || []).length;
        if (depth === 0) { end = j; break; }
      }
      var selText = lines.slice(start, i + 1).join(" ").replace(/\s*{.*$/, "").trim();
      var am = selText.match(/^@media\s+(.+)$/);
      if (am) {
        /* recurse so desktop-layout rules are individually matchable */
        parseRuleRange(lines, i + 1, end - 1, am[1].trim());
        i = end + 1;
        continue;
      }
      if (/^@supports/.test(selText)) {
        parseRuleRange(lines, i + 1, end - 1, media);
        i = end + 1;
        continue;
      }
      var body = lines.slice(i, end + 1).join("\n");
      var vars = [], vm, vre = /var\(--([a-z0-9-]+)/gi;
      while ((vm = vre.exec(body))) if (vars.indexOf(vm[1]) < 0) vars.push(vm[1]);
      rules.push({ sel: selText, start: start, end: end, vars: vars,
                   media: media, decls: parseDecls(body) });
      i = end + 1;
    }
  }
  function parseDecls(body) {
    var out = [], m;
    var re = /(?:^|[{;\n])\s*([a-zA-Z-]+)\s*:\s*([^;{}]+)/g;
    while ((m = re.exec(body))) {
      var prop = m[1].toLowerCase(), val = m[2].trim();
      var vars = [], vm, vre = /var\(--([a-z0-9-]+)/gi;
      while ((vm = vre.exec(val))) vars.push(vm[1]);
      out.push({ prop: prop, value: val, vars: vars });
    }
    return out;
  }

  function effective(name) {
    if (themeTok[name]) return themeTok[name].value;
    if (contractTok[name]) return contractTok[name].value;
    return null;
  }
  function pristineValue(name) {
    if (pThemeTok[name]) return pThemeTok[name].value;
    if (pContractTok[name]) return pContractTok[name].value;
    return null;
  }
  function usesOf(name) {
    return rules.filter(function (r) { return r.vars.indexOf(name) >= 0; });
  }
  function varRe(name) {
    return new RegExp("var\\(\\s*--" + name + "\\s*(?:,[^)]*)?\\)", "g");
  }
  function ruleApplies(r, el, win) {
    if (!r.sel || r.sel.charAt(0) === "@") return false;
    if (r.media && win) {
      try { if (!win.matchMedia(r.media).matches) return false; } catch (e) {}
    }
    return r.sel.split(",").some(function (s) {
      s = s.trim();
      if (!s) return false;
      try { return el.matches(s); } catch (e) { return false; }
    });
  }
  function rulesApplying(el) {
    if (!el || !el.matches) return [];
    return rules.filter(function (r) { return ruleApplies(r, el, previewWin); });
  }
  function aspectOfProp(prop) {
    if (prop === "color" || prop === "-webkit-text-fill-color") return "text";
    if (/^background/.test(prop)) return "background";
    if (/^(border|outline)(-(top|right|bottom|left))?(-color)?$/.test(prop)) return "bezel";
    return null;
  }
  /* which token drives each visual aspect of this element — walking up
     the tree for inherited text color and the background it sits on */
  function resolveAspects(el) {
    var found = { text: null, background: null, bezel: null };
    var node = el, hops = 0;
    while (node && node.nodeType === 1 && node.tagName !== "HTML" && hops < 30) {
      var applying = rulesApplying(node);
      ["text", "background", "bezel"].forEach(function (a) {
        if (found[a]) return;
        var hit = null;
        applying.forEach(function (r) {
          r.decls.forEach(function (d) {
            if (aspectOfProp(d.prop) === a && d.vars.length)
              hit = { token: d.vars[d.vars.length - 1], rule: r, el: node };
          });
        });
        if (hit) found[a] = hit;
      });
      if (found.text && found.background && found.bezel) break;
      node = node.parentElement;
      hops++;
    }
    return found;
  }

  var ADD_MARK = "  /* -- studio additions -- */";
  function replaceTokenInText(text, name, value) {
    var lines = text.split("\n");
    var b = blockRange(lines, target.tokenOpen);
    if (b.open < 0) return { text: text, line: -1 };
    var toks = parseVarLines(lines, b.open, b.close);
    if (toks[name]) {
      var i = toks[name].line;
      lines[i] = lines[i].replace(/(--[a-z0-9-]+\s*:\s*)[^;]+;/i, "$1" + value + ";");
      return { text: lines.join("\n"), line: i };
    }
    var add = ["  --" + name + ": " + value + ";"];
    if (lines.slice(b.open, b.close).join("\n").indexOf(ADD_MARK.trim()) < 0)
      add = ["", ADD_MARK].concat(add);
    lines.splice(b.close, 0, add.join("\n"));
    return { text: lines.join("\n"), line: b.close + add.length - 1 };
  }

  /* ============================================================
     Live channel + undo
     ============================================================ */
  function ensureLiveStyles(doc) {
    if (!doc) return;
    target.files.forEach(function (f) {
      if (doc.getElementById("df-live-" + f.id)) return;
      var st = doc.createElement("style");
      st.id = "df-live-" + f.id;
      doc.head.appendChild(st);
    });
  }
  function inject(id) {
    var docs = [previewDoc, phoneDoc];
    if (target.injectSelf) docs.push(document);
    docs.forEach(function (doc) {
      if (!doc) return;
      ensureLiveStyles(doc);
      var st = doc.getElementById("df-live-" + id);
      if (st && st.textContent !== working[id]) st.textContent = working[id];
    });
  }
  function injectAll() { target.files.forEach(function (f) { inject(f.id); }); }

  function syncFile(id, opts) {
    opts = opts || {};
    reparse(id);
    inject(id);
    if (previewWin && previewWin.DF) previewWin.DF.paint();
    renderTabs();
    if (drawerOpen && activeFile === id) {
      renderCode(!opts.typing);
      if (opts.flash >= 0) flashLine(id, opts.flash);
    } else if (activeFile === id && !opts.typing) {
      renderCode(true);
    }
    renderDirty();
    if (opts.refreshRail) renderRail(true);
  }

  function pushUndo(fileId, before, after, key) {
    var top = undoStack[undoStack.length - 1];
    var now = Date.now();
    if (top && key && top.key === key && top.file === fileId && now - top.t < 700) {
      top.after = after; top.t = now;
    } else {
      undoStack.push({ file: fileId, before: before, after: after, key: key, t: now });
      if (undoStack.length > 200) undoStack.shift();
    }
    redoStack = [];
    updateUndoButtons();
  }
  function mutate(fileId, newText, opts) {
    opts = opts || {};
    if (working[fileId] === newText) return;
    pushUndo(fileId, working[fileId], newText, opts.key);
    working[fileId] = newText;
    syncFile(fileId, opts);
  }
  function doUndo() {
    var e = undoStack.pop();
    if (!e) return;
    redoStack.push(e);
    working[e.file] = e.before;
    syncFile(e.file, {});
    renderCode(true); renderRail(true);
    updateUndoButtons();
  }
  function doRedo() {
    var e = redoStack.pop();
    if (!e) return;
    undoStack.push(e);
    working[e.file] = e.after;
    syncFile(e.file, {});
    renderCode(true); renderRail(true);
    updateUndoButtons();
  }
  function updateUndoButtons() {
    $("#undo-btn").disabled = undoStack.length === 0;
    $("#redo-btn").disabled = redoStack.length === 0;
  }

  function applyEdit(name, value, flashless) {
    var r = replaceTokenInText(working[target.tokenFileId], name, value);
    mutate(target.tokenFileId, r.text, { key: "tok:" + name, flash: flashless ? -1 : r.line });
  }

  function fileDirty(id) { return working[id] !== pristine[id]; }
  function anyDirty() { return target.files.some(function (f) { return fileDirty(f.id); }); }
  function tokenDiff() {
    var out = [], names = {};
    Object.keys(pThemeTok).forEach(function (n) { names[n] = 1; });
    Object.keys(themeTok).forEach(function (n) { names[n] = 1; });
    Object.keys(names).forEach(function (n) {
      var was = pThemeTok[n] ? pThemeTok[n].value : null;
      var now = themeTok[n] ? themeTok[n].value : null;
      if (was !== now) out.push({ name: n, was: was, now: now });
    });
    return out;
  }
  function dirtyCount() {
    var diff = tokenDiff().length;
    var extra = target.files.filter(function (f) {
      return f.id !== target.tokenFileId && fileDirty(f.id);
    }).length;
    var structural = fileDirty(target.tokenFileId) && diff === 0 ? 1 : 0;
    return diff + extra + structural;
  }
  function renderDirty() {
    var n = dirtyCount();
    var save = $("#save-btn");
    save.disabled = !anyDirty();
    save.textContent = n > 0 ? "Save · " + n : "Save";
    var ch = $("#changes-btn");
    if (ch) ch.hidden = n === 0;
  }

  /* ============================================================
     Color math
     ============================================================ */
  function parseColor(v) {
    v = (v || "").trim();
    var m;
    if ((m = v.match(/^#([0-9a-f]{6})$/i))) return { hex: "#" + m[1].toLowerCase(), a: 1 };
    if ((m = v.match(/^#([0-9a-f]{3})$/i))) {
      var s = m[1];
      return { hex: ("#" + s[0] + s[0] + s[1] + s[1] + s[2] + s[2]).toLowerCase(), a: 1 };
    }
    if ((m = v.match(/^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?\s*\)$/i))) {
      var hx = "#" + [m[1], m[2], m[3]].map(function (n) {
        return ("0" + (+n).toString(16)).slice(-2);
      }).join("");
      return { hex: hx, a: m[4] === undefined ? 1 : +m[4] };
    }
    if ((m = v.match(/^hsla?\(\s*([\d.]+)[,\s]+([\d.]+)%[,\s]+([\d.]+)%(?:[,\s/]+([\d.]+))?\s*\)$/i))) {
      var rgb = hslToRgb(+m[1], +m[2] / 100, +m[3] / 100);
      return {
        hex: "#" + rgb.map(function (n) { return ("0" + n.toString(16)).slice(-2); }).join(""),
        a: m[4] === undefined ? 1 : +m[4]
      };
    }
    return null;
  }
  function hslToRgb(h, s, l) {
    h = ((h % 360) + 360) % 360 / 360;
    function f(p, q, t) {
      if (t < 0) t += 1; if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    }
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
    return [f(p, q, h + 1 / 3), f(p, q, h), f(p, q, h - 1 / 3)]
      .map(function (x) { return Math.round(x * 255); });
  }
  function hexToHsv(hex) {
    var r = parseInt(hex.slice(1, 3), 16) / 255,
        g = parseInt(hex.slice(3, 5), 16) / 255,
        b = parseInt(hex.slice(5, 7), 16) / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    var h = 0;
    if (d) {
      if (mx === r) h = ((g - b) / d) % 6;
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60; if (h < 0) h += 360;
    }
    return { h: h, s: mx ? d / mx : 0, v: mx };
  }
  function hsvToHex(h, s, v) {
    var c = v * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), mm = v - c;
    var r = 0, g = 0, b = 0;
    if (h < 60) { r = c; g = x; } else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; } else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; } else { r = c; b = x; }
    return "#" + [r + mm, g + mm, b + mm].map(function (n) {
      return ("0" + Math.round(n * 255).toString(16)).slice(-2);
    }).join("");
  }
  function colorString(hex, a) {
    if (a >= 1) return hex;
    var r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16),
        b = parseInt(hex.slice(5, 7), 16);
    return "rgba(" + r + ", " + g + ", " + b + ", " + (+a.toFixed(2)) + ")";
  }
  function shade(hex, f) {
    var r = Math.round(parseInt(hex.slice(1, 3), 16) * f),
        g = Math.round(parseInt(hex.slice(3, 5), 16) * f),
        b = Math.round(parseInt(hex.slice(5, 7), 16) * f);
    return "#" + [r, g, b].map(function (n) {
      return ("0" + Math.min(255, n).toString(16)).slice(-2);
    }).join("");
  }
  function parseGradient(v) {
    var m = (v || "").trim().match(/^linear-gradient\(\s*([\d.]+)deg\s*,\s*(.+)\)$/i);
    if (!m) return null;
    var stops = [];
    var parts = m[2].match(/(#[0-9a-f]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))\s*([\d.]+)%/gi);
    if (!parts) return null;
    parts.forEach(function (p) {
      var pm = p.match(/^(.*?)\s*([\d.]+)%$/);
      if (pm) stops.push({ color: pm[1].trim(), pos: +pm[2] });
    });
    return stops.length >= 2 ? { angle: +m[1], stops: stops } : null;
  }
  function composeGradient(g) {
    return "linear-gradient(" + Math.round(g.angle) + "deg, " +
      g.stops.map(function (s) { return s.color + " " + Math.round(s.pos) + "%"; }).join(", ") + ")";
  }

  /* ============================================================
     Token classification
     ============================================================ */
  function kindOf(name, value) {
    var v = (value || "").trim();
    if (parseGradient(v) || parseColor(v)) return "color";
    if (name.indexOf("font") === 0 || name === "sans" || name === "mono" ||
        (/,/.test(v) && /(sans-serif|serif|monospace)/.test(v)) || /^var\(--(font-\w+|sans|mono)\)$/.test(v))
      return "font";
    if (/^[1-9]00$/.test(v)) return "weight";
    if (/^(uppercase|lowercase|none|capitalize)$/.test(v)) return "transform";
    if (/px$/.test(v)) return "px";
    if (/em$/.test(v)) return "em";
    if (/ms$/.test(v)) return "ms";
    if (/%$/.test(v)) return "pct";
    if (/hue/.test(name) && /^[\d.]+$/.test(v)) return "hue";
    return "text";
  }
  var KIND_ORDER = ["color", "font", "weight", "transform", "px", "em", "ms", "pct", "hue", "text"];
  function rangeFor(name, kind) {
    if (/^radius/.test(name)) return [0, 24, 1];
    if (/^space|^pad/.test(name)) return [0, 96, 2];
    if (/^text-/.test(name)) return [8, 44, 0.5];
    if (/^border-w/.test(name)) return [0, 4, 0.5];
    if (kind === "em") return [-0.05, 0.6, 0.005];
    if (kind === "ms") return [0, 1000, 10];
    if (kind === "hue") return [0, 360, 1];
    if (kind === "pct") return [0, 100, 1];
    return [0, 100, 1];
  }

  function debounced(fn, ms) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }
  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* ============================================================
     Popover infrastructure
     ============================================================ */
  var openPopEl = null, popCleanup = null;
  function closePop() {
    if (!openPopEl) return;
    openPopEl.remove(); openPopEl = null;
    if (popCleanup) { popCleanup(); popCleanup = null; }
  }
  function openPop(anchor, build, opts) {
    opts = opts || {};
    closePop();
    var pop = document.createElement("div");
    pop.className = "pop";
    build(pop);
    pop.style.visibility = "hidden";
    $("#pop-layer").appendChild(pop);
    var a = anchor ? anchor.getBoundingClientRect() : null;
    var w = pop.offsetWidth, h = pop.offsetHeight, x, y;
    if (opts.at) {
      x = Math.max(8, Math.min(innerWidth - w - 8, opts.at.x));
      y = Math.max(8, Math.min(innerHeight - h - 8, opts.at.y));
    } else if (opts.below) {
      x = Math.max(8, Math.min(innerWidth - w - 8, a.right - w));
      y = a.bottom + 8;
    } else {
      x = a.left - w - 14;
      if (x < 8) x = Math.min(innerWidth - w - 8, a.right + 14);
      y = Math.max(8, Math.min(innerHeight - h - 8, a.top - 30));
    }
    pop.style.left = x + "px";
    pop.style.top = y + "px";
    pop.style.visibility = "";
    openPopEl = pop;
    setTimeout(function () {
      var onDown = function (e) { if (!pop.contains(e.target)) closePop(); };
      document.addEventListener("pointerdown", onDown, true);
      popCleanup = function () { document.removeEventListener("pointerdown", onDown, true); };
    }, 0);
    return pop;
  }

  function dragOn(el, fn) {
    el.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      var move = function (ev) {
        var r = el.getBoundingClientRect();
        fn(Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)),
           Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height)));
      };
      move(e);
      var up = function () {
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
    });
  }

  /* ============================================================
     The color picker
     ============================================================ */
  function openColorPicker(anchor, name, onApply) {
    var raw = effective(name) || "#888888";
    var st = { mode: "solid", h: 0, s: 0, v: 0.5, a: 1, angle: 135, stops: null, selStop: 0 };
    var g = parseGradient(raw);
    if (g) {
      st.mode = "gradient"; st.angle = g.angle; st.stops = g.stops;
      adoptColor(g.stops[0].color);
    } else {
      adoptColor(raw);
    }
    function adoptColor(cv) {
      var p = parseColor(cv) || { hex: "#888888", a: 1 };
      var hsv = hexToHsv(p.hex);
      st.h = hsv.h; st.s = hsv.s; st.v = hsv.v; st.a = p.a;
    }
    function currentColor() { return colorString(hsvToHex(st.h, st.s, st.v), st.a); }
    function emit() {
      var v;
      if (st.mode === "gradient") {
        st.stops[st.selStop].color = currentColor();
        v = composeGradient({ angle: st.angle, stops: st.stops });
      } else {
        v = currentColor();
      }
      applyEdit(name, v, true);
      if (onApply) onApply(v);
      paintPicker();
    }

    var els = {};
    openPop(anchor, function (pop) {
      pop.classList.add("cp");
      var tabs = document.createElement("div");
      tabs.className = "cp-tabs";
      ["solid", "gradient"].forEach(function (mo) {
        var b = document.createElement("button");
        b.textContent = mo;
        b.addEventListener("click", function () {
          if (mo === st.mode) return;
          if (mo === "gradient") {
            var base = hsvToHex(st.h, st.s, st.v);
            st.stops = st.stops || [
              { color: colorString(base, st.a), pos: 0 },
              { color: shade(base, 0.55), pos: 100 }
            ];
            st.selStop = 0;
            adoptColor(st.stops[0].color);
          } else {
            adoptColor(st.stops[st.selStop].color);
          }
          st.mode = mo;
          emit();
          renderTabsBar(); renderGradBits();
        });
        tabs.appendChild(b);
      });
      pop.appendChild(tabs);
      els.tabs = tabs;

      var sv = document.createElement("div");
      sv.className = "cp-sv";
      var dot = document.createElement("div");
      dot.className = "cp-dot";
      sv.appendChild(dot);
      dragOn(sv, function (x, y) { st.s = x; st.v = 1 - y; emit(); });
      pop.appendChild(sv);
      els.sv = sv; els.dot = dot;

      var hue = strip(false, function (x) { st.h = x * 360; emit(); });
      pop.appendChild(hue.el);
      els.hue = hue;
      var alpha = strip(true, function (x) { st.a = +x.toFixed(2); emit(); });
      pop.appendChild(alpha.el);
      els.alpha = alpha;

      var gradWrap = document.createElement("div");
      pop.appendChild(gradWrap);
      els.gradWrap = gradWrap;

      var row = document.createElement("div");
      row.className = "cp-row";
      if (window.EyeDropper) {
        var eye = document.createElement("button");
        eye.className = "cp-eye";
        eye.title = "sample any pixel on your screen";
        eye.innerHTML = '<svg viewBox="0 0 16 16"><path d="M10.3 2.6a1.9 1.9 0 0 1 2.7 2.7l-1.2 1.2-2.7-2.7zM8.4 4.5 3.5 9.4c-.3.3-.5.7-.5 1.1v1.6l-.8 1.2.6.6 1.2-.8h1.6c.4 0 .8-.2 1.1-.5l4.9-4.9z" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg>';
        eye.addEventListener("click", function () {
          new window.EyeDropper().open().then(function (r) {
            adoptColor(r.sRGBHex); emit();
          }).catch(function () {});
        });
        row.appendChild(eye);
      }
      var hex = document.createElement("input");
      hex.className = "cp-hex"; hex.spellcheck = false;
      hex.addEventListener("change", function () {
        var p = parseColor(hex.value);
        if (p) { adoptColor(hex.value.trim()); emit(); }
      });
      row.appendChild(hex);
      pop.appendChild(row);
      els.hex = hex;

      var sws = document.createElement("div");
      sws.className = "cp-swatches";
      allTokenNames().forEach(function (n) {
        if (n === name) return;
        var v = effective(n);
        if (!v || kindOf(n, v) !== "color" || parseGradient(v)) return;
        var s = document.createElement("div");
        s.className = "s"; s.style.background = v; s.title = "--" + n;
        s.addEventListener("click", function () { adoptColor(v); emit(); });
        sws.appendChild(s);
      });
      pop.appendChild(sws);

      renderTabsBar(); renderGradBits(); paintPicker();
    });

    function strip(isAlpha, onDrag) {
      var el = document.createElement("div");
      el.className = "cp-strip" + (isAlpha ? " alpha" : "");
      var lay = document.createElement("div");
      lay.className = "lay";
      el.appendChild(lay);
      var pin = document.createElement("div");
      pin.className = "cp-pin";
      el.appendChild(pin);
      dragOn(el, function (x) { onDrag(x); });
      return { el: el, lay: lay, pin: pin };
    }

    function renderTabsBar() {
      $$("button", els.tabs).forEach(function (b) {
        b.classList.toggle("on", b.textContent === st.mode);
      });
    }
    function renderGradBits() {
      var w = els.gradWrap;
      w.innerHTML = "";
      if (st.mode !== "gradient") return;
      var head = document.createElement("div");
      head.className = "cp-grad";
      var knob = makeKnob(st.angle, 360, function (v) { st.angle = v; emit(); });
      head.appendChild(knob);
      var track = document.createElement("div");
      track.className = "cp-track";
      head.appendChild(track);
      w.appendChild(head);
      els.track = track;

      var foot = document.createElement("div");
      foot.className = "cp-grad-foot";
      var add = document.createElement("button");
      add.className = "cp-mini"; add.textContent = "add stop";
      add.addEventListener("click", function () {
        var last = st.stops[st.stops.length - 1];
        st.stops.push({ color: last.color, pos: Math.min(100, last.pos + 12) });
        st.selStop = st.stops.length - 1;
        adoptColor(st.stops[st.selStop].color);
        emit(); renderGradBits();
      });
      foot.appendChild(add);
      var rm = document.createElement("button");
      rm.className = "cp-mini"; rm.textContent = "remove stop";
      rm.addEventListener("click", function () {
        if (st.stops.length <= 2) return;
        st.stops.splice(st.selStop, 1);
        st.selStop = 0;
        adoptColor(st.stops[0].color);
        emit(); renderGradBits();
      });
      foot.appendChild(rm);
      w.appendChild(foot);
      paintTrack();
    }
    function paintTrack() {
      var track = els.track;
      if (!track) return;
      track.style.background = composeGradient({ angle: 90, stops: st.stops });
      $$(".cp-stop", track).forEach(function (n) { n.remove(); });
      st.stops.forEach(function (stop, i) {
        var chip = document.createElement("div");
        chip.className = "cp-stop" + (i === st.selStop ? " sel" : "");
        chip.style.left = stop.pos + "%";
        chip.style.background = stop.color;
        chip.addEventListener("pointerdown", function (e) {
          e.stopPropagation();
          st.selStop = i;
          adoptColor(stop.color);
          paintPicker(); paintTrack();
          chip.setPointerCapture(e.pointerId);
          var move = function (ev) {
            var r = track.getBoundingClientRect();
            stop.pos = Math.max(0, Math.min(100, ((ev.clientX - r.left) / r.width) * 100));
            emit();
          };
          var up = function () {
            chip.removeEventListener("pointermove", move);
            chip.removeEventListener("pointerup", up);
          };
          chip.addEventListener("pointermove", move);
          chip.addEventListener("pointerup", up);
        });
        track.appendChild(chip);
      });
    }
    function paintPicker() {
      var hueHex = hsvToHex(st.h, 1, 1);
      els.sv.style.background =
        "linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, " + hueHex + ")";
      els.dot.style.left = (st.s * 100) + "%";
      els.dot.style.top = ((1 - st.v) * 100) + "%";
      els.hue.lay.style.background =
        "linear-gradient(to right, #f00, #ff0, #0f0, #0ff, #00f, #f0f, #f00)";
      els.hue.pin.style.left = (st.h / 360 * 100) + "%";
      els.alpha.lay.style.background =
        "linear-gradient(to right, transparent, " + hsvToHex(st.h, st.s, st.v) + ")";
      els.alpha.pin.style.left = (st.a * 100) + "%";
      if (document.activeElement !== els.hex)
        els.hex.value = st.mode === "gradient"
          ? composeGradient({ angle: st.angle, stops: st.stops })
          : currentColor();
      if (els.track) paintTrack();
    }
  }

  /* ============================================================
     Rotary knob
     ============================================================ */
  function makeKnob(value, max, onTurn) {
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "knob");
    svg.setAttribute("width", "44"); svg.setAttribute("height", "44");
    svg.setAttribute("viewBox", "0 0 44 44");
    var face = document.createElementNS(NS, "circle");
    face.setAttribute("class", "face");
    face.setAttribute("cx", "22"); face.setAttribute("cy", "20"); face.setAttribute("r", "15");
    var needle = document.createElementNS(NS, "line");
    needle.setAttribute("class", "tick");
    var label = document.createElementNS(NS, "text");
    label.setAttribute("x", "22"); label.setAttribute("y", "43");
    svg.appendChild(face); svg.appendChild(needle); svg.appendChild(label);
    var val = +value || 0;
    function draw() {
      var rad = (val / max) * 2 * Math.PI - Math.PI / 2;
      needle.setAttribute("x1", 22); needle.setAttribute("y1", 20);
      needle.setAttribute("x2", 22 + Math.cos(rad) * 11);
      needle.setAttribute("y2", 20 + Math.sin(rad) * 11);
      label.textContent = Math.round(val) + "°";
    }
    draw();
    svg.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      svg.classList.add("grabbing");
      svg.setPointerCapture(e.pointerId);
      var move = function (ev) {
        var r = svg.getBoundingClientRect();
        var dx = ev.clientX - (r.left + 22), dy = ev.clientY - (r.top + 20);
        var ang = Math.atan2(dy, dx) + Math.PI / 2;
        if (ang < 0) ang += 2 * Math.PI;
        val = (ang / (2 * Math.PI)) * max;
        draw(); onTurn(val);
      };
      var up = function () {
        svg.classList.remove("grabbing");
        svg.removeEventListener("pointermove", move);
        svg.removeEventListener("pointerup", up);
      };
      svg.addEventListener("pointermove", move);
      svg.addEventListener("pointerup", up);
    });
    return svg;
  }

  /* ============================================================
     Font popover
     ============================================================ */
  var FONT_PRESETS = [
    { label: "Helvetica Neue", stack: '"Helvetica Neue", Helvetica, Arial, sans-serif' },
    { label: "System", stack: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif' },
    { label: "IBM Plex Mono", stack: '"IBM Plex Mono", "SF Mono", ui-monospace, Menlo, monospace' },
    { label: "Georgia", stack: 'Georgia, "Times New Roman", serif' },
    { label: "Avenir Next", stack: '"Avenir Next", Avenir, "Helvetica Neue", sans-serif' }
  ];
  function openFontPop(anchor, name, onApply) {
    var current = (effective(name) || "").trim();
    openPop(anchor, function (pop) {
      pop.classList.add("fp");
      var q = document.createElement("input");
      q.placeholder = "search fonts";
      pop.appendChild(q);
      var list = document.createElement("div");
      list.className = "fp-list";
      pop.appendChild(list);
      function items() {
        var out = FONT_PRESETS.map(function (p) { return { label: p.label, stack: p.stack, tag: "" }; });
        target.files.length > 1 && ["body", "display", "mono"].forEach(function (slot) {
          out.push({ label: slot + " slot", stack: "var(--font-" + slot + ")", tag: "slot" });
        });
        (localFontFamilies || []).forEach(function (fam) {
          out.push({ label: fam, stack: '"' + fam + '", sans-serif', tag: "installed" });
        });
        return out;
      }
      function renderList() {
        var needle = q.value.toLowerCase();
        list.innerHTML = "";
        items().filter(function (it) {
          return !needle || it.label.toLowerCase().indexOf(needle) >= 0;
        }).slice(0, 120).forEach(function (it) {
          var row = document.createElement("div");
          row.className = "fp-item" + (it.stack === current ? " on" : "");
          row.style.fontFamily = it.stack;
          row.textContent = it.label;
          if (it.tag) {
            var tg = document.createElement("span");
            tg.className = "tag"; tg.textContent = it.tag;
            row.appendChild(tg);
          }
          row.addEventListener("click", function () {
            applyEdit(name, it.stack);
            current = it.stack;
            if (onApply) onApply(it.stack);
            renderList();
          });
          list.appendChild(row);
        });
      }
      q.addEventListener("input", renderList);
      renderList();
      if (window.queryLocalFonts && localFontFamilies === null) {
        var more = document.createElement("button");
        more.className = "fp-more";
        more.textContent = "browse fonts installed on this Mac";
        more.addEventListener("click", function () {
          window.queryLocalFonts().then(function (fonts) {
            var fams = [];
            fonts.forEach(function (fd) {
              if (fams.indexOf(fd.family) < 0) fams.push(fd.family);
            });
            localFontFamilies = fams.sort();
            more.remove();
            renderList();
          }).catch(function () { more.textContent = "not permitted"; });
        });
        pop.appendChild(more);
      }
      setTimeout(function () { q.focus(); }, 30);
    });
  }

  /* ============================================================
     Rail controls
     ============================================================ */
  function allTokenNames() {
    var names = {};
    Object.keys(contractTok).forEach(function (n) { names[n] = 1; });
    Object.keys(themeTok).forEach(function (n) { names[n] = 1; });
    return Object.keys(names);
  }

  function usesLink(name) {
    var u = document.createElement("button");
    u.className = "ctl-uses";
    var n = usesOf(name).length;
    u.textContent = n + "×";
    u.title = "flash everything --" + name + " applies to";
    u.addEventListener("click", function (e) { e.stopPropagation(); showUses(name); });
    return u;
  }

  function colorRow(name) {
    var row = document.createElement("div");
    row.className = "color-row";
    var chip = document.createElement("div");
    chip.className = "chip";
    chip.style.setProperty("--sw", effective(name) || "transparent");
    var meta = document.createElement("div");
    meta.className = "meta";
    var nm = document.createElement("div");
    nm.className = "nm"; nm.textContent = "--" + name;
    var vl = document.createElement("div");
    vl.className = "vl"; vl.textContent = effective(name) || "";
    meta.appendChild(nm); meta.appendChild(vl);
    row.appendChild(chip); row.appendChild(meta); row.appendChild(usesLink(name));
    row.addEventListener("click", function () {
      openColorPicker(row, name, function (v) {
        chip.style.setProperty("--sw", v);
        vl.textContent = v;
      });
    });
    return row;
  }

  function ctlShell(name) {
    var ctl = document.createElement("div");
    ctl.className = "ctl";
    var head = document.createElement("div");
    head.className = "ctl-head";
    var nm = document.createElement("span");
    nm.className = "ctl-name"; nm.textContent = "--" + name;
    nm.title = "show in code";
    nm.addEventListener("click", function () { jumpToToken(name); });
    head.appendChild(nm);
    head.appendChild(usesLink(name));
    ctl.appendChild(head);
    var body = document.createElement("div");
    body.className = "ctl-body";
    ctl.appendChild(body);
    return { el: ctl, body: body };
  }

  function sliderCtl(name, kind) {
    var sh = ctlShell(name);
    var v = effective(name) || "0";
    var unit = kind === "px" ? "px" : kind === "em" ? "em" : kind === "ms" ? "ms" : kind === "pct" ? "%" : "";
    var num = parseFloat(v) || 0;
    var r = rangeFor(name, kind);
    var wrap = document.createElement("div");
    wrap.className = "sl-wrap";
    var s = document.createElement("input");
    s.type = "range"; s.className = "sl";
    s.min = r[0]; s.max = r[1]; s.step = r[2]; s.value = num;
    var out = document.createElement("input");
    out.className = "sl-val"; out.value = v; out.spellcheck = false;
    function fill() {
      s.style.setProperty("--fill", (((+s.value - r[0]) / (r[1] - r[0])) * 100) + "%");
    }
    fill();
    s.addEventListener("input", function () {
      var val = (+s.value) + unit;
      out.value = val; fill();
      applyEdit(name, val, true);
    });
    var live = debounced(function () {
      if (out.value.trim()) applyEdit(name, out.value.trim(), true);
      var p = parseFloat(out.value);
      if (!isNaN(p)) { s.value = p; fill(); }
    }, 200);
    out.addEventListener("input", live);
    wrap.appendChild(s); wrap.appendChild(out);
    sh.body.appendChild(wrap);
    return sh.el;
  }

  function segCtl(name, options) {
    var sh = ctlShell(name);
    var cur = (effective(name) || "").trim();
    var seg = document.createElement("div");
    seg.className = "seg";
    options.forEach(function (o) {
      var b = document.createElement("button");
      b.textContent = o.label;
      b.classList.toggle("on", String(o.v) === cur);
      b.addEventListener("click", function () {
        applyEdit(name, String(o.v));
        $$("button", seg).forEach(function (x) { x.classList.toggle("on", x === b); });
      });
      seg.appendChild(b);
    });
    sh.body.appendChild(seg);
    return sh.el;
  }

  function fontCtl(name) {
    var sh = ctlShell(name);
    var btn = document.createElement("button");
    btn.className = "font-btn";
    function paint() {
      var v = effective(name) || "";
      btn.innerHTML = "";
      var ag = document.createElement("span");
      ag.className = "ag"; ag.textContent = "Ag";
      ag.style.fontFamily = v;
      var fam = document.createElement("span");
      fam.className = "fam";
      fam.textContent = v.split(",")[0].replace(/["']/g, "").replace(/var\(--font-(\w+)\)/, "$1 slot");
      btn.appendChild(ag); btn.appendChild(fam);
    }
    paint();
    btn.addEventListener("click", function () { openFontPop(btn, name, paint); });
    sh.body.appendChild(btn);
    return sh.el;
  }

  function textCtl(name) {
    var sh = ctlShell(name);
    var inp = document.createElement("input");
    inp.className = "sl-val"; inp.style.width = "100%"; inp.style.textAlign = "left";
    inp.value = effective(name) || ""; inp.spellcheck = false;
    inp.addEventListener("input", debounced(function () {
      if (inp.value.trim()) applyEdit(name, inp.value.trim(), true);
    }, 250));
    sh.body.appendChild(inp);
    return sh.el;
  }

  function controlFor(name) {
    var v = effective(name) || "";
    var kind = kindOf(name, v);
    if (kind === "color") return colorRow(name);
    if (kind === "font") return fontCtl(name);
    if (kind === "weight") return segCtl(name, [100, 200, 300, 400, 500, 600, 700].map(function (n) {
      return { v: n, label: String(n) };
    }));
    if (kind === "transform") return segCtl(name, [
      { v: "none", label: "Aa" }, { v: "uppercase", label: "AA" },
      { v: "lowercase", label: "aa" }, { v: "capitalize", label: "Aa." }
    ]);
    if (kind === "px" || kind === "em" || kind === "ms" || kind === "pct" || kind === "hue")
      return sliderCtl(name, kind);
    return textCtl(name);
  }

  /* ============================================================
     Rail views
     ============================================================ */
  function section(title, open) {
    var sec = document.createElement(open === false ? "details" : "div");
    sec.className = "rail-sec";
    if (open === false) {
      var sum = document.createElement("summary");
      sum.textContent = title;
      sec.appendChild(sum);
    } else {
      var k = document.createElement("div");
      k.className = "sec-kick";
      k.textContent = title;
      sec.appendChild(k);
    }
    return sec;
  }

  function renderRail(preserveScroll) {
    var body = $("#rail-body");
    var top = preserveScroll ? body.scrollTop : 0;
    if (blast) renderBlastPanel(); else if (sel) renderSelection(); else renderHome();
    body.scrollTop = top;
  }

  function renderHome() {
    $("#rail-back").hidden = true;
    $("#rail-code-link").hidden = true;
    $("#rail-title").textContent = "Styles — " + target.label;
    var body = $("#rail-body");
    body.innerHTML = "";
    var names = allTokenNames();
    var byKind = {};
    names.forEach(function (n) {
      var k = kindOf(n, effective(n) || "");
      (byKind[k] = byKind[k] || []).push(n);
    });

    var colors = (byKind.color || []).filter(function (n) { return !/^(cat-|hash-)/.test(n); });
    if (colors.length) {
      var cs = section("Colors");
      var grid = document.createElement("div");
      grid.className = "swatch-grid";
      colors.forEach(function (n) {
        var tile = document.createElement("div");
        tile.className = "swatch-tile";
        var chip = document.createElement("div");
        chip.className = "chip";
        chip.style.setProperty("--sw", effective(n));
        var nm = document.createElement("div");
        nm.className = "nm"; nm.textContent = n;
        tile.appendChild(chip); tile.appendChild(nm);
        tile.addEventListener("click", function () {
          openColorPicker(tile, n, function (v) { chip.style.setProperty("--sw", v); });
        });
        grid.appendChild(tile);
      });
      cs.appendChild(grid);
      body.appendChild(cs);
    }

    var fonts = byKind.font || [];
    if (fonts.length) {
      var ts = section("Typography");
      fonts.forEach(function (n) {
        var row = document.createElement("div");
        row.className = "type-row";
        var v = effective(n) || "";
        var ag = document.createElement("span");
        ag.className = "ag"; ag.textContent = "Ag"; ag.style.fontFamily = v;
        var meta = document.createElement("div");
        meta.className = "meta";
        var nm = document.createElement("div");
        nm.className = "nm"; nm.textContent = "--" + n;
        var fam = document.createElement("div");
        fam.className = "fam";
        fam.textContent = v.split(",")[0].replace(/["']/g, "");
        meta.appendChild(nm); meta.appendChild(fam);
        row.appendChild(ag); row.appendChild(meta);
        row.addEventListener("click", function () {
          openFontPop(row, n, function (nv) {
            ag.style.fontFamily = nv;
            fam.textContent = nv.split(",")[0].replace(/["']/g, "");
          });
        });
        ts.appendChild(row);
      });
      var voice = names.filter(function (n) { return /^(weight-|track-|label-transform)/.test(n); });
      voice.forEach(function (n) { ts.appendChild(controlFor(n)); });
      body.appendChild(ts);
    }

    var shape = names.filter(function (n) { return /^radius/.test(n); });
    if (shape.length) {
      var ss = section("Shape");
      shape.forEach(function (n) { ss.appendChild(controlFor(n)); });
      body.appendChild(ss);
    }

    var groups = [
      ["Type scale", function (n) { return /^text-/.test(n); }],
      ["Spacing", function (n) { return /^(space-|pad$|border-w)/.test(n); }],
      ["Categorical colors", function (n) { return /^cat-/.test(n); }],
      ["Hash band", function (n) { return /^hash-/.test(n); }],
      ["Motion", function (n) { return /^(dur-|ease$)/.test(n); }]
    ];
    var used = {};
    names.forEach(function (n) {
      if ((byKind.color || []).indexOf(n) >= 0 && !/^(cat-|hash-)/.test(n)) used[n] = 1;
      if (fonts.indexOf(n) >= 0) used[n] = 1;
      if (/^(weight-|track-|label-transform|radius)/.test(n)) used[n] = 1;
    });
    groups.forEach(function (gr) {
      var members = names.filter(function (n) { return !used[n] && gr[1](n); });
      if (!members.length) return;
      members.forEach(function (n) { used[n] = 1; });
      var sec = section(gr[0], false);
      members.forEach(function (n) { sec.appendChild(controlFor(n)); });
      body.appendChild(sec);
    });
    var rest = names.filter(function (n) { return !used[n]; });
    if (rest.length) {
      var os = section("More", false);
      rest.forEach(function (n) { os.appendChild(controlFor(n)); });
      body.appendChild(os);
    }

    var hint = document.createElement("p");
    hint.className = "rail-hint";
    hint.textContent = "Click anything in the canvas to edit just what styles it. Every change applies live; Save commits it.";
    body.appendChild(hint);
  }

  function renderSelection() {
    $("#rail-back").hidden = false;
    $("#rail-code-link").hidden = false;
    $("#rail-title").textContent = sel.name;
    var body = $("#rail-body");
    body.innerHTML = "";
    var toks = sel.tokens.slice().sort(function (a, b) {
      return KIND_ORDER.indexOf(kindOf(a, effective(a) || "")) -
             KIND_ORDER.indexOf(kindOf(b, effective(b) || ""));
    });
    if (!toks.length) {
      var hint = document.createElement("p");
      hint.className = "rail-hint";
      hint.textContent = "Nothing here carries a design token — right-click the element to target its text, background, or bezel.";
      body.appendChild(hint);
      return;
    }
    var sec = section(sel.inherited ? "Painted by its container" : "Styles it uses");
    toks.forEach(function (n) { sec.appendChild(controlFor(n)); });
    body.appendChild(sec);
    var tip = document.createElement("p");
    tip.className = "rail-hint";
    tip.textContent = "Tip: right-click anything in the canvas to target one aspect — text, background, or bezel — and see its full blast radius.";
    body.appendChild(tip);
  }

  function showUses(name) {
    var used = usesOf(name);
    if (!used.length) return;
    var extras = [];
    used.slice(1).forEach(function (b) {
      for (var i = b.start; i <= b.end; i++) extras.push(i);
    });
    setHl(target.rulesFileId, used[0].start, used[0].end, extras);
    if (!previewDoc) return;
    var count = 0;
    used.forEach(function (r) {
      var els;
      try { els = previewDoc.querySelectorAll(r.sel); } catch (e) { return; }
      Array.prototype.forEach.call(els, function (el) {
        if (count++ > 250) return;
        var prev = el.style.outline;
        el.style.outline = "1px solid #a39c8d";
        el.style.outlineOffset = "1px";
        setTimeout(function () { el.style.outline = prev; el.style.outlineOffset = ""; }, 1300);
      });
    });
  }

  /* ============================================================
     Blast radius — pick an aspect, flood its token neon pink,
     see everything it touches, uncheck what should not follow,
     fork the unchecked off the label (one-off or permanent).
     ============================================================ */
  var BLAST_PINK = "#ff2fd4";
  var blast = null; // {token, aspect, from, orig, unchecked:{}, mode, pinkOn}

  function blastDocs() {
    var docs = [previewDoc, phoneDoc];
    if (target.injectSelf) docs.push(document);
    return docs.filter(Boolean);
  }
  function blastCss() {
    if (!blast) return "";
    var out = [];
    if (blast.pinkOn)
      out.push(":root { --" + blast.token + ": " + BLAST_PINK + " !important; }");
    var re = varRe(blast.token);
    Object.keys(blast.unchecked).forEach(function (idx) {
      var r = rules[+idx];
      if (!r) return;
      var decls = [];
      r.decls.forEach(function (d) {
        if (d.vars.indexOf(blast.token) >= 0)
          decls.push(d.prop + ": " + d.value.replace(re, blast.orig) + " !important;");
      });
      if (!decls.length) return;
      var css = r.sel + " { " + decls.join(" ") + " }";
      if (r.media) css = "@media " + r.media + " { " + css + " }";
      out.push(css);
    });
    return out.join("\n");
  }
  function paintBlast() {
    var css = blastCss();
    blastDocs().forEach(function (doc) {
      var st = doc.getElementById("df-blast");
      if (!blast) { if (st) st.remove(); return; }
      if (!st) { st = doc.createElement("style"); st.id = "df-blast"; }
      doc.head.appendChild(st); /* re-append keeps it after the live channel */
      if (st.textContent !== css) st.textContent = css;
    });
  }
  function enterBlast(token, aspectLabel, fromName) {
    blast = {
      token: token, aspect: aspectLabel, from: fromName,
      orig: effective(token) || "transparent",
      unchecked: {}, mode: "oneoff", pinkOn: true
    };
    paintBlast();
    renderRail();
  }
  function exitBlast() {
    blast = null;
    paintBlast();
    closePop();
    renderRail();
  }
  function cancelBlast() {
    if (!blast) return;
    if ((effective(blast.token) || "") !== blast.orig) applyEdit(blast.token, blast.orig, true);
    exitBlast();
  }
  function applyBlast() {
    if (!blast) return;
    var idxs = Object.keys(blast.unchecked).map(Number).filter(function (i) { return rules[i]; });
    if (idxs.length) {
      var fileId = target.rulesFileId;
      var lines = working[fileId].split("\n");
      var replacement, forkName = null;
      if (blast.mode === "permanent") {
        var n = 2;
        forkName = blast.token + "-2";
        while (effective(forkName)) { n++; forkName = blast.token + "-" + n; }
        replacement = "var(--" + forkName + ")";
      } else {
        replacement = blast.orig;
      }
      var re = varRe(blast.token);
      idxs.forEach(function (idx) {
        var r = rules[idx];
        for (var i = r.start; i <= r.end; i++)
          lines[i] = lines[i].replace(re, replacement);
      });
      var text = lines.join("\n");
      if (forkName) {
        if (target.tokenFileId === fileId) {
          text = replaceTokenInText(text, forkName, blast.orig).text;
        } else {
          applyEdit(forkName, blast.orig, true);
        }
      }
      mutate(fileId, text, { key: "fork:" + blast.token });
      toast(forkName
        ? "Split " + idxs.length + " rule" + (idxs.length > 1 ? "s" : "") + " onto --" + forkName
        : idxs.length + " rule" + (idxs.length > 1 ? "s" : "") + " pinned to " + blast.orig);
    }
    exitBlast();
  }
  function flashRule(r) {
    if (!previewDoc || r.sel.charAt(0) === "@") return;
    var els;
    try { els = previewDoc.querySelectorAll(r.sel); } catch (e) { return; }
    Array.prototype.slice.call(els, 0, 120).forEach(function (el) {
      var prev = el.style.outline;
      el.style.outline = "2px solid " + BLAST_PINK;
      el.style.outlineOffset = "1px";
      setTimeout(function () { el.style.outline = prev; el.style.outlineOffset = ""; }, 900);
    });
  }
  function renderBlastPanel() {
    $("#rail-back").hidden = false;
    $("#rail-code-link").hidden = true;
    $("#rail-title").textContent = blast.aspect + " of " + blast.from;
    var body = $("#rail-body");
    body.innerHTML = "";

    var meta = document.createElement("div");
    meta.className = "blast-meta";
    var chip = document.createElement("div");
    chip.className = "blast-chip";
    chip.id = "blast-cur";
    chip.style.background = blast.pinkOn ? BLAST_PINK : (effective(blast.token) || "transparent");
    var note = document.createElement("div");
    note.className = "blast-note";
    note.innerHTML = "Everything below is drawn by <b>--" + escapeHtml(blast.token) + "</b>" +
      (blast.pinkOn ? " — lit pink until you pick the color." : ".") +
      " Uncheck anything that should keep its current look.";
    meta.appendChild(chip);
    meta.appendChild(note);
    body.appendChild(meta);

    var pick = document.createElement("button");
    pick.className = "blast-pick";
    pick.id = "blast-pick";
    pick.textContent = "Pick the new color";
    pick.addEventListener("click", function () {
      openColorPicker(pick, blast.token, function (v) {
        if (!blast) return;
        if (blast.pinkOn) { blast.pinkOn = false; paintBlast(); }
        var c = $("#blast-cur");
        if (c) c.style.background = v;
      });
    });
    body.appendChild(pick);

    var sec = section("In the blast radius");
    usesOf(blast.token).forEach(function (r) {
      var idx = rules.indexOf(r);
      var row = document.createElement("label");
      row.className = "blast-row";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !blast.unchecked[idx];
      cb.addEventListener("change", function () {
        if (cb.checked) delete blast.unchecked[idx];
        else blast.unchecked[idx] = 1;
        paintBlast();
        renderForkBits();
      });
      var nm = document.createElement("span");
      nm.className = "blast-sel";
      nm.textContent = r.sel;
      nm.title = r.sel + (r.media ? "   [@media " + r.media + "]" : "");
      var count = document.createElement("span");
      count.className = "blast-n";
      var n = 0;
      try {
        if (previewDoc && r.sel.charAt(0) !== "@") n = previewDoc.querySelectorAll(r.sel).length;
      } catch (e) {}
      count.textContent = n ? n + "×" : "";
      row.appendChild(cb); row.appendChild(nm); row.appendChild(count);
      row.addEventListener("mouseenter", function () { flashRule(r); });
      sec.appendChild(row);
    });
    body.appendChild(sec);

    var fork = document.createElement("div");
    fork.className = "blast-fork";
    fork.id = "blast-fork";
    var fkick = document.createElement("div");
    fkick.className = "sec-kick";
    fkick.textContent = "Unchecked rules";
    fork.appendChild(fkick);
    var seg = document.createElement("div");
    seg.className = "seg";
    [["oneoff", "One-off"], ["permanent", "Permanent split"]].forEach(function (pair) {
      var b = document.createElement("button");
      b.textContent = pair[1];
      b.classList.toggle("on", blast.mode === pair[0]);
      b.addEventListener("click", function () {
        blast.mode = pair[0];
        $$("button", seg).forEach(function (x) { x.classList.toggle("on", x === b); });
        renderForkBits();
      });
      seg.appendChild(b);
    });
    fork.appendChild(seg);
    var fnote = document.createElement("p");
    fnote.className = "blast-forknote";
    fnote.id = "blast-forknote";
    fork.appendChild(fnote);
    body.appendChild(fork);

    var act = document.createElement("div");
    act.className = "blast-actions";
    var apply = document.createElement("button");
    apply.className = "blast-apply";
    apply.textContent = "Done";
    apply.addEventListener("click", applyBlast);
    var cancel = document.createElement("button");
    cancel.className = "blast-cancel";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", cancelBlast);
    act.appendChild(apply); act.appendChild(cancel);
    body.appendChild(act);

    renderForkBits();
  }
  function renderForkBits() {
    var n = Object.keys(blast.unchecked).length;
    var fork = $("#blast-fork");
    if (fork) fork.hidden = n === 0;
    var note = $("#blast-forknote");
    var plural = n !== 1;
    if (note) note.textContent = blast.mode === "permanent"
      ? "The " + n + " unchecked rule" + (plural ? "s move" : " moves") + " onto a new token holding today's color — from now on the studio treats " + (plural ? "them" : "it") + " as " + (plural ? "their" : "its") + " own thing."
      : "The " + n + " unchecked rule" + (plural ? "s get" : " gets") + " today's color written in directly — this edit passes " + (plural ? "them" : "it") + " by, the vocabulary stays as is.";
  }

  /* ============================================================
     Desktop canvas scale + iPhone parallel
     ============================================================ */
  var PHONE_W = 402, PHONE_H = 874;
  function layoutCanvas() {
    var frame = $("#preview");
    var box = $("#canvas-frame");
    if (!target.deskWidth) {
      frame.style.width = "100%"; frame.style.height = "100%";
      frame.style.transform = "";
      canvasScale = 1;
      return;
    }
    /* the app reloads itself whenever its desktop media query (1100px)
       flips, so the iframe is ALWAYS given an explicit width >= that
       breakpoint — natural edge-to-edge when the column affords it,
       forced design width scaled down when it doesn't. Never a
       percentage: a transient dip under 1100 mid-drag would reload the
       app mid-animation. */
    var BREAK = 1100;
    var w = box.clientWidth, h = box.clientHeight;
    if (!w || !h) return;
    if (w >= BREAK) {
      canvasScale = 1;
      frame.style.width = w + "px";
      frame.style.height = h + "px";
      frame.style.transform = "";
      return;
    }
    var k = w / target.deskWidth;
    canvasScale = k;
    frame.style.width = target.deskWidth + "px";
    frame.style.height = Math.round(h / k) + "px";
    frame.style.transform = "scale(" + k + ")";
    frame.style.transformOrigin = "0 0";
  }
  function phoneTargetWidth() {
    var stored = +localStorage.getItem("df-phone-w") || 292;
    var stageH = $("#stage").clientHeight - 70;
    var maxW = Math.floor(stageH / PHONE_H * PHONE_W);
    return Math.max(200, Math.min(460, Math.min(stored, maxW)));
  }
  function layoutPhone() {
    var dock = $("#phone-dock");
    if (!dock) return;
    var show = !!target.deskWidth && innerWidth > 1180;
    dock.hidden = !show;
    $("#stage").style.setProperty("--dock-w", show ? phoneTargetWidth() + "px" : "0px");
    if (!show) return;
    var w = phoneTargetWidth();
    var k = w / PHONE_W;
    dock.style.width = (w + 4) + "px";
    var shell = $("#phone-shell");
    var frame = $("#phone");
    frame.style.width = PHONE_W + "px";
    frame.style.height = PHONE_H + "px";
    frame.style.transform = "scale(" + k + ")";
    frame.style.transformOrigin = "0 0";
    shell.style.width = Math.round(PHONE_W * k) + "px";
    shell.style.height = Math.round(PHONE_H * k) + "px";
  }
  function moduleOf(el) {
    if (!el || !el.closest) return null;
    var w = el.closest('[id^="win-"]') || el.closest('[id^="view-"]');
    if (!w) return null;
    return w.id.replace(/^win-/, "").replace(/^view-/, "");
  }
  function setPhoneModule(view, force) {
    if (!view) return;
    if (!force && view === phoneView) return;
    phoneView = view;
    var label = $("#phone-label");
    if (label) label.textContent = view;
    if (!phoneDoc) return;
    var tab = phoneDoc.querySelector('.tab[data-view="' + view + '"]');
    if (tab) tab.click();
  }

  /* ============================================================
     Canvas: hover, select, mode
     ============================================================ */
  var ov = { hoverRing: null, hoverTag: null, selRing: null, selTag: null };
  function ensureOverlays() {
    if (!previewDoc || ov.hoverRing && ov.hoverRing.ownerDocument === previewDoc) return;
    function mk(solid) {
      var ring = previewDoc.createElement("div");
      ring.style.cssText =
        "position:absolute; pointer-events:none; z-index:2147483000; display:none;" +
        "border:1px " + (solid ? "solid" : "dashed") + " #a39c8d; border-radius:2px;" +
        (solid ? "background:rgba(138,132,120,0.08);" : "");
      var tag = previewDoc.createElement("div");
      tag.style.cssText =
        "position:absolute; pointer-events:none; z-index:2147483001; display:none;" +
        "font:600 9px/1 ui-monospace, Menlo, monospace; letter-spacing:0.08em;" +
        "color:#191713; background:#a39c8d; border-radius:2px; padding:3px 6px;" +
        "white-space:nowrap; text-transform:uppercase;";
      previewDoc.body.appendChild(ring);
      previewDoc.body.appendChild(tag);
      return { ring: ring, tag: tag };
    }
    var h = mk(false), s = mk(true);
    ov = { hoverRing: h.ring, hoverTag: h.tag, selRing: s.ring, selTag: s.tag };
  }
  function place(ring, tag, el, name) {
    var r = el.getBoundingClientRect();
    var x = r.left + previewWin.scrollX, y = r.top + previewWin.scrollY;
    ring.style.display = "block";
    ring.style.left = x + "px"; ring.style.top = y + "px";
    ring.style.width = r.width + "px"; ring.style.height = r.height + "px";
    tag.style.display = "block";
    tag.textContent = name;
    tag.style.left = x + "px";
    tag.style.top = Math.max(previewWin.scrollY + 2, y - 20) + "px";
  }
  function clearHover() {
    if (ov.hoverRing) { ov.hoverRing.style.display = "none"; ov.hoverTag.style.display = "none"; }
  }
  function clearSelRing() {
    if (ov.selRing) { ov.selRing.style.display = "none"; ov.selTag.style.display = "none"; }
  }

  var hoverPending = false;
  function onPreviewMove(e) {
    if (canvasMode !== "design" || hoverPending) return;
    hoverPending = true;
    var t = e.target;
    requestAnimationFrame(function () {
      hoverPending = false;
      if (!previewDoc) return;
      ensureOverlays();
      var res = resolveClick(t);
      if (res) {
        var el = res.el || (t.closest && t.closest("div,section,button,span")) || t;
        place(ov.hoverRing, ov.hoverTag, el, res.name);
      } else {
        clearHover();
      }
    });
  }
  function onPreviewClick(e) {
    if (canvasMode !== "design") return;
    e.preventDefault();
    e.stopPropagation();
    var res = resolveClick(e.target);
    clearHover();
    if (!res) { clearSelection(); return; }
    sel = res;
    ensureOverlays();
    var el = res.el || (e.target.closest && e.target.closest("div,section,button,span")) || e.target;
    sel.el = el;
    place(ov.selRing, ov.selTag, el, res.name);
    var tokens = (res.tokens || []).slice();
    if (res.rule) {
      var blocks = rulesFor(res.rule);
      blocks.forEach(function (b) {
        b.vars.forEach(function (v) { if (tokens.indexOf(v) < 0) tokens.push(v); });
      });
      /* every rule that applies right now — state compounds (.fchip.on),
         descendant selectors, desktop @media rules */
      rulesApplying(el).forEach(function (r) {
        r.vars.forEach(function (v) { if (tokens.indexOf(v) < 0) tokens.push(v); });
      });
      /* the element's own rules carry no tokens — surface what paints it
         from its containers so the rail never dead-ends */
      sel.inherited = false;
      if (!tokens.length) {
        var node = el.parentElement;
        while (node && node.nodeType === 1 && !tokens.length && node.tagName !== "HTML") {
          rulesApplying(node).forEach(function (r) {
            r.vars.forEach(function (v) { if (tokens.indexOf(v) < 0) tokens.push(v); });
          });
          node = node.parentElement;
        }
        sel.inherited = tokens.length > 0;
      }
      if (blocks.length) {
        var extras = [];
        blocks.slice(1).forEach(function (b) {
          for (var i = b.start; i <= b.end; i++) extras.push(i);
        });
        setHl(target.rulesFileId, blocks[0].start, blocks[0].end, extras);
      }
    } else if (res.token && themeTok[res.token]) {
      setHl(target.tokenFileId, themeTok[res.token].line, themeTok[res.token].line, []);
    }
    sel.tokens = tokens;
    var mod = moduleOf(el);
    if (mod) setPhoneModule(mod);
    renderRail();
  }
  function onPreviewContext(e) {
    if (canvasMode !== "design") return;
    e.preventDefault();
    e.stopPropagation();
    var res = resolveClick(e.target);
    if (!res) return;
    var probe = e.target.nodeType === 1 ? e.target : (res.el || e.target);
    var aspects = resolveAspects(probe);
    var fr = $("#preview").getBoundingClientRect();
    openAspectMenu(fr.left + e.clientX * canvasScale,
                   fr.top + e.clientY * canvasScale,
                   res.name, aspects);
  }
  function openAspectMenu(x, y, name, aspects) {
    openPop(null, function (pop) {
      pop.classList.add("aspect-menu");
      var head = document.createElement("div");
      head.className = "am-head";
      head.textContent = name;
      pop.appendChild(head);
      [["text", "Text"], ["background", "Background"], ["bezel", "Bezel"]].forEach(function (pair) {
        var hit = aspects[pair[0]];
        var row = document.createElement("button");
        row.className = "am-row";
        row.disabled = !hit;
        var chip = document.createElement("span");
        chip.className = "am-chip";
        if (hit) chip.style.background = effective(hit.token) || "transparent";
        var lab = document.createElement("span");
        lab.className = "am-label";
        lab.textContent = pair[1];
        var tok = document.createElement("span");
        tok.className = "am-token";
        tok.textContent = hit ? "--" + hit.token : "not tokenized";
        row.appendChild(chip); row.appendChild(lab); row.appendChild(tok);
        if (hit) row.addEventListener("click", function () {
          closePop();
          enterBlast(hit.token, pair[1], name);
        });
        pop.appendChild(row);
      });
    }, { at: { x: x, y: y } });
  }
  function stopIfDesign(e) {
    if (canvasMode !== "design") return;
    e.preventDefault();
    e.stopPropagation();
    if (openPopEl) closePop(); /* canvas clicks land inside the iframe, so the
                                  studio-document outside-click closer never fires */
  }
  function clearSelection() {
    sel = null;
    clearSelRing();
    renderRail();
  }
  function repositionSel() {
    if (sel && sel.el && ov.selRing && ov.selRing.style.display !== "none") {
      try { place(ov.selRing, ov.selTag, sel.el, sel.name); } catch (e) {}
    }
  }

  /* ============================================================
     Click resolution
     ============================================================ */
  var CLICK_MAP = [
    { sel: ".btn.primary", rule: ".btn.primary", name: "button / primary" },
    { sel: ".btn", rule: ".btn", name: "button" },
    { sel: ".chip", rule: ".chip", name: "chip" },
    { sel: ".badge", rule: ".badge", name: "badge" },
    { sel: ".row", rule: ".row", name: "list row" },
    { sel: ".avatar", rule: ".avatar", name: "avatar" },
    { sel: ".kpi", rule: ".kpi", name: "kpi tile" },
    { sel: ".input", rule: ".input", name: "input" },
    { sel: ".table", rule: ".table", name: "table" },
    { sel: ".tab", rule: ".tab", name: "tab" },
    { sel: ".window-bar", rule: ".window-bar", name: "window bar" },
    { sel: ".window", rule: ".window", name: "window" },
    { sel: ".toast", rule: ".toast", name: "toast" },
    { sel: ".dot", rule: ".dot", name: "status dot" },
    { sel: ".section-head", rule: ".section-head", name: "section head" },
    { sel: ".kicker", rule: ".kicker", name: "micro-label" },
    { sel: ".brand", rule: ".brand", name: "wordmark" },
    { sel: ".toolbar", rule: ".toolbar", name: "toolbar" }
  ];
  function rulesFor(selText) {
    var esc = selText.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    var re = new RegExp("(^|,\\s*|\\s)" + esc + "($|[\\s,:])");
    return rules.filter(function (r) { return re.test(r.sel); });
  }
  function resolveGeneric(el) {
    var node = el;
    while (node && node.nodeType === 1 && node.tagName !== "BODY") {
      var classes = Array.prototype.slice.call(node.classList || []);
      for (var i = 0; i < classes.length; i++) {
        var blocks = rulesFor("." + classes[i]);
        if (blocks.length) return { el: node, rule: "." + classes[i], name: "." + classes[i] };
      }
      node = node.parentElement;
    }
    return null;
  }
  function resolveClick(el) {
    if (!el || !el.closest) return null;
    if (target.id === "foundation") {
      var sw = el.closest(".swatch");
      if (sw) {
        var label = sw.querySelector(".swatch-name");
        if (label) {
          var tn = label.textContent.trim().replace(/\s+/g, "-");
          return { el: sw, name: "--" + tn, tokens: [tn], token: tn };
        }
      }
      for (var i = 0; i < CLICK_MAP.length; i++) {
        var hit = el.closest(CLICK_MAP[i].sel);
        if (hit) return Object.assign({ el: hit }, CLICK_MAP[i]);
      }
    }
    return resolveGeneric(el);
  }

  /* ============================================================
     Code drawer (the proven editor)
     ============================================================ */
  function measureMetrics() {
    var cs = getComputedStyle($("#code-tint"));
    var lh = parseFloat(cs.lineHeight);
    if (lh > 4) LH = lh;
    PAD_TOP = parseFloat(cs.paddingTop) || 12;
  }
  function tintText(text) {
    var lines = text.split("\n");
    var st = { inComment: false };
    return lines.map(function (l) { return tintLine(l, st) || ""; }).join("\n");
  }
  function tintLine(raw, st) {
    var out = "", s = raw;
    if (st.inComment) {
      var endIdx = s.indexOf("*/");
      if (endIdx < 0) return '<span class="c-com">' + escapeHtml(s) + "</span>";
      out += '<span class="c-com">' + escapeHtml(s.slice(0, endIdx + 2)) + "</span>";
      s = s.slice(endIdx + 2);
      st.inComment = false;
    }
    var openIdx = s.indexOf("/*");
    if (openIdx >= 0) {
      var closeIdx = s.indexOf("*/", openIdx);
      var head = s.slice(0, openIdx);
      if (closeIdx < 0) {
        st.inComment = true;
        return out + tintCode(head) + '<span class="c-com">' + escapeHtml(s.slice(openIdx)) + "</span>";
      }
      return out + tintCode(head) +
        '<span class="c-com">' + escapeHtml(s.slice(openIdx, closeIdx + 2)) + "</span>" +
        tintLine(s.slice(closeIdx + 2), st);
    }
    return out + tintCode(s);
  }
  function tintCode(s) {
    if (!s) return "";
    if (/{\s*$/.test(s) && !/[:;]/.test(s))
      return '<span class="c-sel">' + escapeHtml(s) + "</span>";
    var m = s.match(/^(\s*)(--?[a-z-][a-z0-9-]*)(\s*:\s*)(.*)$/i);
    if (m) {
      return escapeHtml(m[1]) + '<span class="c-prop">' + escapeHtml(m[2]) + "</span>" +
        escapeHtml(m[3]) + '<span class="c-val">' + escapeHtml(m[4]) + "</span>";
    }
    return escapeHtml(s);
  }
  function renderTabs() {
    var nav = $("#file-tabs");
    nav.innerHTML = "";
    target.files.forEach(function (f) {
      var b = document.createElement("button");
      b.className = "tab" + (f.id === activeFile ? " active" : "");
      b.textContent = f.label + (fileDirty(f.id) ? " ·" : "");
      b.addEventListener("click", function () {
        activeFile = f.id;
        if (currentHl.file !== f.id) currentHl = { file: f.id, lines: [], start: 0 };
        renderTabs(); renderCode(true);
      });
      nav.appendChild(b);
    });
  }
  function renderCode(setValue) {
    var text = working[activeFile] || "";
    var n = text.split("\n").length;
    var nums = [];
    for (var i = 1; i <= n; i++) nums.push(i);
    $("#gutter").textContent = nums.join("\n");
    $("#code-tint").innerHTML = tintText(text);
    var ta = $("#code-edit");
    if (setValue && ta.value !== text) ta.value = text;
    drawBars();
  }
  function drawBars() {
    var layer = $("#hl-layer");
    layer.innerHTML = "";
    if (currentHl.file !== activeFile) return;
    currentHl.lines.forEach(function (l) {
      var bar = document.createElement("div");
      bar.className = "hl-bar";
      bar.style.top = (PAD_TOP + l * LH) + "px";
      bar.style.height = LH + "px";
      layer.appendChild(bar);
    });
  }
  function scrollCodeTo(line, center) {
    var box = $("#code-scroll");
    var top = PAD_TOP + line * LH - (center ? box.clientHeight / 2 - LH : 0);
    if (center || top < box.scrollTop || top > box.scrollTop + box.clientHeight - 40)
      box.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  }
  function setHl(fileId, start, end, extras) {
    activeFile = fileId;
    var lines = [];
    for (var i = start; i <= end; i++) lines.push(i);
    currentHl = { file: fileId, lines: lines.concat(extras || []), start: start };
    renderTabs();
    renderCode(true);
    if (drawerOpen) scrollCodeTo(start, true);
  }
  function flashLine(fileId, line) {
    if (!drawerOpen || activeFile !== fileId || line < 0) return;
    var layer = $("#hl-layer");
    var bar = document.createElement("div");
    bar.className = "flash-bar";
    bar.style.top = (PAD_TOP + line * LH) + "px";
    bar.style.height = LH + "px";
    layer.appendChild(bar);
    setTimeout(function () { bar.remove(); }, 1500);
    scrollCodeTo(line, false);
  }
  function jumpToToken(name) {
    if (themeTok[name]) setHl(target.tokenFileId, themeTok[name].line, themeTok[name].line, []);
    else if (contractTok[name] && target.fallbackFileId)
      setHl(target.fallbackFileId, contractTok[name].line, contractTok[name].line, []);
    if (!drawerOpen) openDrawer();
    else scrollCodeTo(currentHl.start, true);
  }

  var codeBurstBefore = null;
  var syncTyped = debounced(function () {
    syncFile(activeFile, { typing: true, refreshRail: true });
  }, 240);
  function onCodeInput() {
    var ta = $("#code-edit");
    pushUndo(activeFile, codeBurstBefore !== null ? codeBurstBefore : working[activeFile],
             ta.value, "code:" + activeFile);
    codeBurstBefore = null;
    working[activeFile] = ta.value;
    renderCode(false);
    syncTyped();
  }

  function openDrawer() {
    drawerOpen = true;
    $("#code-drawer").hidden = false;
    $("#code-btn").classList.add("lit");
    measureMetrics();
    renderTabs(); renderCode(true);
    if (currentHl.lines.length) scrollCodeTo(currentHl.start, true);
  }
  function closeDrawer() {
    drawerOpen = false;
    $("#code-drawer").hidden = true;
    $("#code-btn").classList.remove("lit");
  }

  /* ============================================================
     Save / changes / history
     ============================================================ */
  function toast(msg) {
    var t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    $("#toast-dock").appendChild(t);
    setTimeout(function () { t.remove(); }, 3800);
  }
  function doSave() {
    if (!anyDirty()) return;
    var files = {};
    target.files.forEach(function (f) {
      if (fileDirty(f.id)) files[f.savePath] = working[f.id];
    });
    var btn = $("#save-btn");
    btn.disabled = true; btn.textContent = "Saving…";
    fetch("/api/design/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: (target.id === "vira" || target.id === "gallery") ? "vira" : "foundation", theme: THEME, files: files })
    }).then(function (r) {
      if (!r.ok) throw new Error("host " + r.status);
      return r.json();
    }).then(function (out) {
      target.files.forEach(function (f) { pristine[f.id] = working[f.id]; });
      pristineReparse();
      renderTabs(); renderDirty();
      toast("Saved " + (out.sha ? "· " + out.sha.slice(0, 7) : "") +
            (out.pushed === false ? " — push failed, will need a manual push" : " · pushed"));
    }).catch(function () {
      $("#modal-text").value = Object.keys(files).map(function (p) {
        return "/* ==== " + p + " ==== */\n\n" + files[p];
      }).join("\n\n");
      $("#modal-back").hidden = false;
    }).finally(function () {
      renderDirty();
      if ($("#save-btn").disabled) $("#save-btn").textContent = "Save";
    });
  }

  function openChangesPop(anchor) {
    openPop(anchor, function (pop) {
      pop.classList.add("list-pop");
      var head = document.createElement("div");
      head.className = "lp-head"; head.textContent = "unsaved changes";
      pop.appendChild(head);
      tokenDiff().forEach(function (d) {
        var row = document.createElement("div");
        row.className = "lp-row";
        var g = document.createElement("span");
        g.className = "grow";
        g.innerHTML = "--" + d.name + " <b>" + escapeHtml(d.now === null ? "(removed)" : d.now) + "</b>";
        g.title = (d.was || "(new)") + " → " + (d.now || "(removed)");
        var x = document.createElement("button");
        x.textContent = "×"; x.title = "revert";
        x.addEventListener("click", function () {
          var pv = pThemeTok[d.name] ? pThemeTok[d.name].value : null;
          if (pv !== null) applyEdit(d.name, pv, true);
          renderRail(true);
          row.remove();
          renderDirty();
        });
        row.appendChild(g); row.appendChild(x);
        pop.appendChild(row);
      });
      target.files.forEach(function (f) {
        if (f.id === target.tokenFileId || !fileDirty(f.id)) return;
        var row = document.createElement("div");
        row.className = "lp-row";
        var g = document.createElement("span");
        g.className = "grow";
        g.innerHTML = "<b>" + f.label + "</b> edited";
        var x = document.createElement("button");
        x.textContent = "×";
        x.addEventListener("click", function () {
          mutate(f.id, pristine[f.id], {});
          renderRail(true); row.remove(); renderDirty();
        });
        row.appendChild(g); row.appendChild(x);
        pop.appendChild(row);
      });
      var act = document.createElement("div");
      act.className = "lp-actions";
      var all = document.createElement("button");
      all.textContent = "discard all";
      all.addEventListener("click", function () {
        target.files.forEach(function (f) {
          if (fileDirty(f.id)) mutate(f.id, pristine[f.id], {});
        });
        renderCode(true); renderRail(true); closePop();
      });
      act.appendChild(all);
      pop.appendChild(act);
    }, { below: true });
  }

  function openHistoryPop(anchor) {
    fetch("/api/design/history?theme=" + THEME + "&target=" +
          ((target.id === "vira" || target.id === "gallery") ? "vira" : "foundation"))
      .then(function (r) { return r.ok ? r.json() : { history: [] }; })
      .catch(function () { return { history: [] }; })
      .then(function (out) {
        openPop(anchor, function (pop) {
          pop.classList.add("list-pop");
          var head = document.createElement("div");
          head.className = "lp-head"; head.textContent = "saved versions — " + target.label;
          pop.appendChild(head);
          if (!out.history || !out.history.length) {
            var none = document.createElement("div");
            none.className = "lp-row";
            none.textContent = "no saves yet (or no host)";
            pop.appendChild(none);
            return;
          }
          out.history.forEach(function (h) {
            var row = document.createElement("div");
            row.className = "lp-row";
            row.innerHTML = '<span class="lp-sha">' + escapeHtml(h.sha) + "</span>" +
              '<span class="grow">' + escapeHtml(h.message) + "</span>";
            pop.appendChild(row);
          });
        }, { below: true });
      });
  }

  /* ============================================================
     Targets
     ============================================================ */
  function renderTargetPills() {
    var nav = $("#bar-targets");
    nav.innerHTML = "";
    TARGETS.forEach(function (t) {
      var b = document.createElement("button");
      b.textContent = t.label;
      b.classList.toggle("on", t.id === target.id);
      b.addEventListener("click", function () {
        if (t.id !== target.id) loadTarget(t);
      });
      nav.appendChild(b);
    });
  }
  function loadTarget(t, isFallback) {
    Promise.all(t.files.map(function (f) {
      return fetch(f.path).then(function (r) {
        if (!r.ok) throw new Error(f.path);
        return r.text();
      });
    })).then(function (texts) {
      target = t;
      pristine = {}; working = {};
      t.files.forEach(function (f, i) { pristine[f.id] = texts[i]; working[f.id] = texts[i]; });
      themeTok = {}; contractTok = {}; rules = [];
      t.files.forEach(function (f) { reparse(f.id); });
      pristineReparse();
      undoStack = []; redoStack = [];
      updateUndoButtons();
      activeFile = t.tokenFileId;
      currentHl = { file: null, lines: [], start: 0 };
      sel = null;
      blast = null;
      previewDoc = null; previewWin = null;
      phoneDoc = null; phoneWin = null; phoneView = null;
      ov = { hoverRing: null, hoverTag: null, selRing: null, selTag: null };
      layoutCanvas();
      $("#preview").src = t.url;
      var ph = $("#phone");
      if (ph) {
        layoutPhone();
        ph.src = t.deskWidth ? t.url : "about:blank";
      }
      localStorage.setItem("df-target", t.id);
      renderTargetPills();
      renderTabs(); renderCode(true); renderDirty(); renderRail();
    }).catch(function () {
      if (!isFallback) {
        toast(t.label + " is not reachable here — loading Vira");
        loadTarget(TARGETS.find(function (x) { return x.id === "vira"; }), true);
      }
    });
  }

  /* ============================================================
     Boot
     ============================================================ */
  function boot() {
    var frame = $("#preview");
    frame.addEventListener("load", function () {
      previewDoc = frame.contentDocument;
      previewWin = frame.contentWindow;
      if (!previewDoc) return;
      ov = { hoverRing: null, hoverTag: null, selRing: null, selTag: null };
      previewDoc.addEventListener("click", onPreviewClick, true);
      previewDoc.addEventListener("contextmenu", onPreviewContext, true);
      previewDoc.addEventListener("pointerdown", stopIfDesign, true);
      previewDoc.addEventListener("mousedown", stopIfDesign, true);
      previewDoc.addEventListener("pointermove", onPreviewMove, true);
      previewDoc.addEventListener("pointerleave", clearHover, true);
      previewDoc.addEventListener("scroll", function () { clearHover(); repositionSel(); }, true);
      layoutCanvas();
      if (anyDirty()) injectAll();
      paintBlast();
    });

    var phone = $("#phone");
    if (phone) {
      phone.addEventListener("load", function () {
        phoneDoc = phone.contentDocument;
        phoneWin = phone.contentWindow;
        if (!phoneDoc) return;
        if (anyDirty()) injectAll();
        paintBlast();
        if (phoneView) setPhoneModule(phoneView, true);
      });
    }
    var grip = $("#phone-grip");
    if (grip) grip.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      grip.setPointerCapture(e.pointerId);
      var startX = e.clientX, startW = phoneTargetWidth();
      var move = function (ev) {
        /* the grip lives on the phone's free (left) corner: drag out to grow */
        var w = Math.max(200, Math.min(460, startW + (startX - ev.clientX)));
        localStorage.setItem("df-phone-w", w);
        layoutPhone();
      };
      var up = function () {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
    });
    var relayout = function () {
      layoutCanvas();
      layoutPhone();
      repositionSel();
    };
    window.addEventListener("resize", relayout);
    if (window.ResizeObserver) {
      /* the load-time flex race (canvas measured before the layout settled)
         and every dock/rail size change land here, not just window resizes */
      var ro = new ResizeObserver(relayout);
      ro.observe($("#canvas-frame"));
      ro.observe($("#stage"));
    }

    $$("#mode-switch button").forEach(function (b) {
      b.addEventListener("click", function () {
        canvasMode = b.dataset.canvasMode;
        $$("#mode-switch button").forEach(function (x) { x.classList.toggle("on", x === b); });
        if (canvasMode === "use") { clearHover(); }
      });
    });

    var changes = document.createElement("button");
    changes.id = "changes-btn";
    changes.hidden = true;
    changes.textContent = "changes";
    changes.addEventListener("click", function () { openChangesPop(changes); });
    $("#bar").insertBefore(changes, $("#save-btn"));

    $("#save-btn").addEventListener("click", doSave);
    $("#undo-btn").addEventListener("click", doUndo);
    $("#redo-btn").addEventListener("click", doRedo);
    $("#history-btn").addEventListener("click", function () { openHistoryPop($("#history-btn")); });
    $("#code-btn").addEventListener("click", function () {
      drawerOpen ? closeDrawer() : openDrawer();
    });
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#rail-back").addEventListener("click", function () {
      if (blast) cancelBlast(); else clearSelection();
    });
    $("#rail-code-link").addEventListener("click", function () {
      if (!drawerOpen) openDrawer();
      else if (currentHl.lines.length) scrollCodeTo(currentHl.start, true);
    });

    var grip = $("#drawer-grip");
    grip.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      grip.setPointerCapture(e.pointerId);
      var startY = e.clientY;
      var startH = $("#code-drawer").offsetHeight;
      var move = function (ev) {
        var h = Math.max(160, Math.min(innerHeight * 0.65, startH + (startY - ev.clientY)));
        $("#code-drawer").style.height = h + "px";
      };
      var up = function () {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
    });

    var ta = $("#code-edit");
    ta.addEventListener("beforeinput", function () {
      if (codeBurstBefore === null) codeBurstBefore = working[activeFile];
    });
    ta.addEventListener("input", onCodeInput);
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Escape") e.stopPropagation();
    });

    $("#modal-close").addEventListener("click", function () { $("#modal-back").hidden = true; });
    $("#modal-copy").addEventListener("click", function () {
      navigator.clipboard.writeText($("#modal-text").value).then(function () { toast("Copied"); });
    });

    document.addEventListener("keydown", function (e) {
      var meta = e.metaKey || e.ctrlKey;
      if (meta && e.key === "s") { e.preventDefault(); doSave(); }
      else if (meta && e.key === "z" && !e.shiftKey) { e.preventDefault(); doUndo(); }
      else if (meta && (e.key === "Z" || (e.key === "z" && e.shiftKey))) { e.preventDefault(); doRedo(); }
      else if (e.key === "Escape") {
        if (openPopEl) closePop();
        else if (blast) cancelBlast();
        else if (sel) clearSelection();
      }
    });

    renderTargetPills();
    var startId = localStorage.getItem("df-target") || "vira";
    var start = TARGETS.find(function (t) { return t.id === startId; }) || TARGETS[0];
    loadTarget(start);
  }

  boot();
})();

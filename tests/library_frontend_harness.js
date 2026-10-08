"use strict";

// The Library's pure pieces (static/library.js), run for real: the
// treemap, the split that decides what a double-click breaks a box into,
// the filter, the frontmatter reader, the Markdown renderer and the
// constellation's layout. Run by tests/test_library.py.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(root, "static", "library.js"), "utf8");
const context = { window: { __VIRA_TEST__: true }, URL };
vm.createContext(context);
vm.runInContext(script, context, { filename: "library.js" });
const L = context.window.__VIRA_LIBRARY_TESTS__;
assert.ok(L, "library test hooks were not installed");

// ---- the treemap covers its rectangle exactly, without overlap ----
const items = [40, 25, 12, 9, 7, 4, 2, 1].map((v, i) => ({ i, v }));
const rects = L.squarify(items, 0, 0, 600, 400);
assert.equal(rects.length, items.length);
const area = rects.reduce((a, r) => a + r.w * r.h, 0);
assert.ok(Math.abs(area - 600 * 400) < 1e-6, "the boxes fill the map");
for (const r of rects) {
  assert.ok(r.x >= -1e-9 && r.y >= -1e-9 && r.x + r.w <= 600 + 1e-6 && r.y + r.h <= 400 + 1e-6);
  const share = items[r.i].v / 100;
  assert.ok(Math.abs(r.w * r.h - share * 600 * 400) < 1e-6, "a box's area is its share");
}
for (let a = 0; a < rects.length; a++) {
  for (let b = a + 1; b < rects.length; b++) {
    const A = rects[a], B = rects[b];
    const ox = Math.min(A.x + A.w, B.x + B.w) - Math.max(A.x, B.x);
    const oy = Math.min(A.y + A.h, B.y + B.h) - Math.max(A.y, B.y);
    assert.ok(ox <= 1e-6 || oy <= 1e-6, "boxes never overlap");
  }
}

// ---- a label cut to fit always ends, and always fits ----
const measure = (s) => s.length * 7;
const cut = L.fitText(measure, "A very long subject name that cannot fit", 70);
assert.ok(cut.endsWith("\u2026") && measure(cut) <= 70, cut);
assert.equal(L.fitText(measure, "Short", 70), "Short");
assert.equal(L.fitText(measure, "Anything", 1), "A\u2026", "never loops on a box too narrow for any label");

// ---- pages are gridded inside their box ----
const grid = L.placeIn(10, { x: 0, y: 0, w: 200, h: 100 });
assert.equal(grid.pts.length, 10);
assert.ok(grid.pts.every(([x, y]) => x > 0 && x < 200 && y > 0 && y < 100));

// ---- splitOf: the first level at which a set of pages differs ----
const chains = [[1, 2, 5], [1, 2, 6], [1, 3, 7], [1, 3, 7], [4, 8]];
assert.deepEqual(JSON.parse(JSON.stringify(L.splitOf([0, 1, 2, 3, 4], chains))),
  { L: 0, kids: [{ g: 1, n: 4 }, { g: 4, n: 1 }] });
assert.deepEqual(JSON.parse(JSON.stringify(L.splitOf([0, 1, 2, 3], chains))),
  { L: 1, kids: [{ g: 2, n: 2 }, { g: 3, n: 2 }] });
assert.equal(L.splitOf([2, 3], chains), null, "one leaf: nothing splits further");
assert.equal(L.splitOf([4], chains), null);

// ---- the filter ----
const pq = L.parseQuery('rocket -launch tag:space kind:concept in:wiki "deep orbit"');
assert.deepEqual(JSON.parse(JSON.stringify(pq)), {
  words: ["rocket", "deep orbit"], not: ["launch"], tags: ["space"],
  kinds: ["concept"], folders: ["wiki"] });
assert.equal(L.matchPage(pq, "Rocket in deep orbit", "wiki/r.md", ["topic/space"], "concept"), true);
assert.equal(L.matchPage(pq, "Rocket launch in deep orbit", "wiki/r.md", ["space"], "concept"), false);
assert.equal(L.matchPage(pq, "Rocket in deep orbit", "raw/r.md", ["space"], "concept"), false);
assert.equal(L.matchPage(pq, "Rocket in deep orbit", "wiki/r.md", ["garden"], "concept"), false);

// ---- frontmatter: flat, inline lists, block lists, nested lines ----
const fm = L.splitFrontmatter([
  "---", "title: \"A page\"", "tags: [one, \"two, three\"]", "sources:",
  "  - \"[[raw/a]]\"", "  - \"[[raw/b]]\"", "meta:", "  nested: yes", "---", "", "Body line",
].join("\n"));
assert.equal(fm.body.trim(), "Body line");
const props = JSON.parse(JSON.stringify(Object.fromEntries(fm.props.map((p) => [p.key, p.values]))));
assert.deepEqual(props.title, ["A page"]);
assert.deepEqual(props.tags, ["one", "two, three"]);
assert.deepEqual(props.sources, ["[[raw/a]]", "[[raw/b]]"]);
assert.deepEqual(props.meta, ["nested: yes"]);
assert.equal(L.splitFrontmatter("No frontmatter").props.length, 0);

// ---- the renderer ----
const ctx = { vault: "primary", linkmap: {
  "garden-01": { rel: "wiki/garden-01.md", title: "Garden one" },
  "no-such": null,
  "wiki/assets/p.png": { asset: "wiki/assets/p.png" },
  "raw/doc.pdf": { asset: "raw/doc.pdf" },
} };
const html = L.renderMarkdown([
  "# Title", "", "Intro with [[garden-01|the first]] and [[no-such]] and `code [[x]]`.",
  "Second line, **bold** and *em* and ==mark== and a #tag.", "",
  "| A | B |", "|---|:-:|", "| [[garden-01|x]] | 2 |", "",
  "> [!warning] Careful", "> inside the callout", "",
  "- one", "  - nested", "- [x] done", "1. first", "",
  "![[wiki/assets/p.png|300]]", "![[raw/doc.pdf]]",
  "<script>alert(1)</script> [bad](javascript:alert(1)) https://example.com/a_b_c",
  "```py", "x = '<b>'", "```",
].join("\n"), ctx);
assert.match(html, /<h2 id="title">Title<\/h2>/);
assert.match(html, /<a class="lib-link" data-rel="wiki\/garden-01\.md"[^>]*>the first<\/a>/);
assert.match(html, /<a class="lib-link dead" data-dead="no-such"/);
assert.match(html, /<code>code \[\[x\]\]<\/code>/, "a link inside code stays code");
assert.match(html, /<br>Second line, <strong>bold<\/strong> and <em>em<\/em> and <mark>mark<\/mark> and a <span class="lib-tag">#tag<\/span>/);
assert.match(html, /<table><thead><tr><th>A<\/th><th style="text-align:center">B<\/th><\/tr>/);
assert.match(html, /<td><a class="lib-link" data-rel="wiki\/garden-01\.md"[^>]*>x<\/a><\/td>/, "a pipe inside a link is not a column");
assert.match(html, /<div class="lib-callout" data-kind="warning"><div class="lib-callout-title">Careful<\/div><p>inside the callout<\/p><\/div>/);
assert.match(html, /<ul><li>one<ul><li>nested<\/li><\/ul><\/li><li class="lib-task done"><span class="lib-check" aria-hidden="true"><\/span>done<\/li><\/ul><ol><li>first<\/li><\/ol>/);
assert.match(html, /<img class="lib-img" loading="lazy" src="\/api\/vault\/asset\?path=wiki%2Fassets%2Fp\.png"[^>]*style="max-width:300px"/);
assert.match(html, /<a class="lib-link asset" data-asset="raw\/doc\.pdf"/);
assert.doesNotMatch(html, /<script>/, "raw HTML is escaped");
assert.match(html, /&lt;script&gt;/);
assert.doesNotMatch(html, /href="javascript/i, "a javascript: link is never a link");
assert.match(html, /<a href="https:\/\/example\.com\/a_b_c" target="_blank" rel="noopener">https:\/\/example\.com\/a_b_c<\/a>/,
  "emphasis never reaches inside a URL");
assert.match(html, /<pre class="lib-code" data-lang="py"><code>x = &#39;&lt;b&gt;&#39;<\/code><\/pre>/);

// A numbered list inside a bulleted item, then bullets again: three lists.
assert.equal(L.renderMarkdown("- a\n  1. b\n  - c\n- d", ctx),
  "<ul><li>a<ol><li>b</li></ol><ul><li>c</li></ul></li><li>d</li></ul>");

// A vault other than the primary prefixes embeds that the server did not resolve.
const other = L.renderMarkdown("![[pics/a.png]]", { vault: "personal", linkmap: {} });
assert.match(other, /path=%40personal%2Fpics%2Fa\.png/);

// ---- the constellation: inner ring for links, outer for similar ----
const nodes = [
  { rel: "a", title: "A", ring: "out", leaf: 2 },
  { rel: "b", title: "B", ring: "back", leaf: 1 },
  { rel: "c", title: "C", ring: "similar", leaf: 1 },
  { rel: "d", title: "D", ring: "both", leaf: 1 },
];
const lay = L.constellationLayout(nodes, 400, 300);
const dist = ([x, y]) => Math.hypot((x - 200) / 400, (y - 150) / 300);
assert.deepEqual(Array.from(lay.center), [200, 150]);
for (const k of [0, 1, 3]) assert.ok(dist(lay.pos[k]) < dist(lay.pos[2]), "links sit inside similar pages");
for (const p of lay.pos) assert.ok(p[0] > 0 && p[0] < 400 && p[1] > 0 && p[1] < 300);

console.log("library harness ok");

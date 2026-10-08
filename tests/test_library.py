"""Library - browse a vault by subject, and read it whole.

The contracts pinned here, over a synthetic vault (three subjects in wiki/,
generated reports, session logs, bulk captures and long articles in raw/):

* the build places pages by their embeddings, and pages with none by their
  words, into subjects that are each about one thing; template words never
  describe a subject; links resolve (dead ones stay dead); similar pages
  are found; and the machine-output suggestion names exactly the generated
  folders, from general signals, not folder names;
* machine output is hidden from the map until shown, the owner's choice
  overrides the suggestion, and "!folder" keeps a folder inside one;
* subject names are validated, written only through save_library_names
  (a write tool read-only sessions cannot reach), and survive a rebuild;
* the reader payload resolves every [[link]] and embed, orders prev/next
  within the subject, and draws a bounded constellation;
* saved subsets refuse what they cannot hold, and no test touches the real
  data/library or data/library-subsets.json;
* the routes dispatch real job and build shapes and serve the map gzipped;
* the window's UI rules, as source contracts over the shipped files, and
  the map's pure layout and renderer through a node harness.

Run: .venv/bin/python -m unittest tests.test_library
"""
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from server import library, onboard, settings, vault, viratools

ROOT = Path(__file__).resolve().parents[1]
REAL = [ROOT / "data" / "library", ROOT / "data" / "library-subsets.json"]

TOPICS = {
    "garden": "tomato soil compost seedling mulch harvest pruning trellis",
    "rocket": "thrust booster orbit propellant nozzle payload launch stage",
    "kitchen": "braise saucepan simmer dough knead roast marinade skillet",
}
PER_TOPIC = 60          # wiki pages per subject: 180 in all, so wiki splits
UNEMBEDDED = 10         # of them, per subject, have no vector yet


def _stamp(path):
    if not path.exists():
        return None
    if path.is_dir():
        return sorted((p.name, p.stat().st_mtime_ns) for p in path.iterdir())
    return path.stat().st_mtime_ns


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_vault(root, db):
    """The synthetic vault, and a qocha-shaped index with vectors for the
    embedded wiki pages (a basis direction per subject, plus noise)."""
    rng = np.random.default_rng(7)
    basis = {t: rng.normal(size=768) for t in TOPICS}
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE notes(path TEXT PRIMARY KEY, title TEXT, mtime REAL, size INTEGER);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, seq INTEGER,
                            heading TEXT, text TEXT);
        CREATE TABLE vecs(chunk_id INTEGER PRIMARY KEY, vec BLOB);""")
    truth = {}
    for topic, words in TOPICS.items():
        w = words.split()
        for n in range(PER_TOPIC):
            name = f"{topic}-{n:02d}"
            rel = f"wiki/{name}.md"
            truth[rel] = topic
            links = f"[[{topic}-{(n + 1) % PER_TOPIC:02d}]] and [[{topic}-{(n + 2) % PER_TOPIC:02d}]]"
            body = " ".join(w[(n + k) % len(w)] for k in range(40))
            extra = ""
            if topic == "garden" and n == 0:
                extra = ("\n\nSee [[no-such-page]] and the plot:\n\n![[wiki/assets/plot.png]]\n\n"
                         "| Bed | Crop |\n|---|---|\n| A | [[garden-03|three]] |\n")
            source = "source: raw/articles/article-1.md\n" if (topic, n) == ("garden", 0) else ""
            _write(root, rel, f"---\ntitle: {topic.title()} note {n}\ntype: concept\n{source}"
                              f"tags: [{topic}, cat/{topic}]\n---\n\n## Summary\n\n{body}\n\n"
                              f"## Takeaways\n\nRelated: {links}{extra}\n")
            if n < PER_TOPIC - UNEMBEDDED:
                vec = basis[topic] + rng.normal(size=768) * 0.35
                vec = (vec / np.linalg.norm(vec)).astype(np.float16)
                cur = con.execute("INSERT INTO chunks(path, seq, heading, text) VALUES (?,?,?,?)",
                                  (rel, 0, "Summary", body))
                con.execute("INSERT INTO vecs(chunk_id, vec) VALUES (?,?)",
                            (cur.lastrowid, vec.tobytes()))
                con.execute("INSERT INTO notes VALUES (?,?,?,?)", (rel, name, 0, 0))
    (root / "wiki" / "assets").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "assets" / "plot.png").write_bytes(b"\x89PNG fake")
    for n in range(120):                    # generated reports: a run id in the name
        hexid = hashlib.sha1(str(n).encode()).hexdigest()[:16]
        _write(root, f"Reports/report-{hexid}.md", f"# Report {n}\n\nRun output {n}.\n")
    for n in range(110):                    # session logs: a generator key
        _write(root, f"Sessions/2026-09-{n % 28 + 1:02d}-s{n}.md",
               f"---\ngenerated_by: daily-provenance\ntags: [session, auto]\n---\n\nLog {n}.\n")
    for n in range(105):                    # bulk captures: a URL over a thin body
        _write(root, f"raw/captures/save-{n}.md",
               f"---\nsource: https://example.com/p/{n}\n---\n\nA saved post {n}.\n")
    for n in range(40):                     # long articles with a URL: reading, not output
        _write(root, f"raw/articles/article-{n}.md",
               f"---\nsource: https://example.com/a/{n}\n---\n\n" + ("word " * 500) + "\n")
    _write(root, "index.md", "# Index\n\n[[garden-00]] [[rocket-00]] [[kitchen-00]]\n")
    con.commit()
    con.close()
    return truth


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_before = [_stamp(p) for p in REAL]

    @classmethod
    def tearDownClass(cls):
        after = [_stamp(p) for p in REAL]
        assert after == cls.real_before, "a test touched the real Library stores"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "vault"
        self.root.mkdir()
        self.db = base / "index.sqlite"
        self.truth = make_vault(self.root, self.db)
        self.cfg = {}
        spec = {"id": "primary", "name": "Fixture", "root": self.root, "dirs": None,
                "primary": True, "db": self.db, "read_enabled": True}
        patches = [
            mock.patch.object(library, "DATA", base / "library"),
            mock.patch.object(library, "SUBSETS", base / "library-subsets.json"),
            mock.patch.object(vault, "source_specs", lambda: [dict(spec)]),
            mock.patch.object(vault, "_path_allowed", lambda spec, rel, policy: True),
            mock.patch.object(vault, "_read_policy", lambda *a, **k: None),
            mock.patch.object(vault, "note_text",
                              lambda path, cap=None, for_model=False:
                              (self.root / path).read_text(encoding="utf-8")),
            mock.patch.object(settings, "raw", lambda: self.cfg),
            mock.patch.object(onboard, "config_set", self._config_set),
            mock.patch.object(library, "_start_reaper", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for cache in (library._cache, library._payload_cache, library._asset_cache):
            cache.clear()

    def _config_set(self, *, _validate=None, **updates):
        if _validate:
            _validate(self.cfg)
        self.cfg.update(updates)
        return self.cfg

    def build(self):
        return library.build("primary")

    def idx(self):
        return library.load("primary")

    def rel_of(self, idx):
        return {rel: i for i, rel in enumerate(idx["pages"]["rel"])}


class TheBuild(Base):
    def test_subjects_are_each_about_one_thing(self):
        summary = self.build()
        idx = self.idx()
        P, G = idx["pages"], idx["groups"]
        self.assertEqual(summary["pages"], len(P["rel"]))
        self.assertEqual(summary["embedded"], 3 * (PER_TOPIC - UNEMBEDDED))
        wiki = next(g for g, grp in enumerate(G) if grp["id"] == "a/wiki")
        regions = [g for g, grp in enumerate(G) if grp["parent"] == wiki]
        self.assertGreaterEqual(len(regions), 3, "180 pages break apart")
        under = {}
        for i, rel in enumerate(P["rel"]):
            g = P["leaf"][i]
            while g > 0 and G[g]["parent"] != wiki:
                g = G[g]["parent"]
            if g in regions:
                under.setdefault(g, []).append(self.truth[rel])
        for g, topics in under.items():
            top = max(set(topics), key=topics.count)
            with self.subTest(region=G[g]["id"]):
                self.assertGreaterEqual(topics.count(top) / len(topics), 0.9)
        # A page with no embedding yet lands with its own subject, by words.
        for topic in TOPICS:
            rel = f"wiki/{topic}-{PER_TOPIC - 1:02d}.md"
            self.assertFalse(P["emb"][self.rel_of(idx)[rel]])
            g = P["leaf"][self.rel_of(idx)[rel]]
            while G[g]["parent"] != wiki:
                g = G[g]["parent"]
            topics = under[g]
            self.assertEqual(max(set(topics), key=topics.count), topic)

    def test_template_words_never_describe_a_subject(self):
        self.build()
        for grp in self.idx()["groups"]:
            if grp["level"] in ("region", "subject", "detail"):
                self.assertTrue(grp["terms"], grp["id"])
                for word in ("summary", "takeaways", "cat", "related"):
                    self.assertNotIn(word, grp["terms"], grp["id"])

    def test_links_resolve_and_similar_pages_share_a_subject(self):
        self.build()
        idx = self.idx()
        at = self.rel_of(idx)
        g0 = at["wiki/garden-00.md"]
        targets = {idx["pages"]["rel"][j] for j in idx["links"][g0]}
        self.assertEqual(targets, {"wiki/garden-01.md", "wiki/garden-02.md",
                                   "wiki/garden-03.md"})
        near = [idx["pages"]["rel"][j] for j in idx["near"][at["wiki/rocket-05.md"]][::2]]
        self.assertTrue(near)
        self.assertTrue(all(r.startswith("wiki/rocket-") for r in near))

    def test_machine_output_is_found_by_its_marks_not_its_name(self):
        self.build()
        self.assertEqual(self.idx()["machine_suggested"],
                         ["Reports", "Sessions", "raw/captures"])

    def test_a_rebuild_keeps_names_and_status_says_done(self):
        self.build()
        idx = self.idx()
        sid = next(g["id"] for g in idx["groups"] if g["level"] == "region")
        library.save_names("primary", {sid: "Growing food"}, idx["built"])
        self.build()
        self.assertEqual(library.names("primary").get(sid), "Growing food")
        st = library.status("primary")
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["names_kept"], 1)

    def test_a_huge_page_is_read_from_its_head(self):
        _write(self.root, "wiki/dump.md", "# Dump\n\n[[garden-00]]\n\n" + "tok " * 2000)
        with mock.patch.object(library, "READ_CAP", 200):
            self.build()
        idx = self.idx()
        i = self.rel_of(idx)["wiki/dump.md"]
        self.assertGreater(idx["pages"]["words"][i], 1900, "scaled from the part read")
        self.assertEqual([idx["pages"]["rel"][j] for j in idx["links"][i]], ["wiki/garden-00.md"])


class MachineOutput(Base):
    def test_hidden_until_shown_and_the_owner_choice_wins(self):
        self.build()
        raw, packed = library.map_payload("primary")
        data = json.loads(raw)
        self.assertEqual(json.loads(gzip.decompress(packed)), data)
        self.assertFalse(any(r.startswith(("Reports/", "Sessions/", "raw/captures/"))
                             for r in data["pages"]["rel"]))
        self.assertEqual(data["machine"]["pages"], 120 + 110 + 105)
        self.assertEqual(data["machine"]["source"], "suggested")
        shown = json.loads(library.map_payload("primary", True)[0])
        self.assertEqual(len(shown["pages"]["rel"]), data["total"])
        self.assertEqual(sum(shown["pages"]["machine"]), 335)
        library.set_machine("primary", "raw/captures", False)
        library.set_machine("primary", "raw/articles", True)
        self.assertEqual(self.cfg["library_machine_dirs"]["primary"],
                         ["Reports", "Sessions", "raw/articles"])
        library.set_machine("primary", "Reports/keep", False)
        dirs, source = library.machine_dirs("primary")
        self.assertEqual(source, "owner")
        self.assertIn("!Reports/keep", dirs)
        self.assertTrue(library.is_machine("Reports/x.md", dirs))
        self.assertFalse(library.is_machine("Reports/keep/x.md", dirs))
        self.assertFalse(library.is_machine("Reportsish/x.md", dirs))
        with self.assertRaises(library.LibraryError):
            library.set_machine("primary", "../outside", True)


class Names(Base):
    def test_validated_and_written_only_through_the_tool(self):
        self.build()
        idx = self.idx()
        sid = next(g["id"] for g in idx["groups"] if g["level"] == "subject"
                   or g["level"] == "region")
        built = idx["built"]
        out = viratools._save_library_names_text("primary", json.dumps({sid: "Rockets and orbits"}), built)
        self.assertTrue(out.startswith("saved 1 names"), out)
        bad = viratools._save_library_names_text("primary", json.dumps(
            {"a/wiki": "An area is not named", sid: "x" * 60}), built)
        self.assertTrue(bad.startswith("error:"), bad)
        self.assertIn("'a/wiki' is not a subject", bad)
        self.assertIn("2 to 48 characters", bad)
        self.assertTrue(viratools._save_library_names_text("primary", "{nope", built).startswith(
            "error: names_json is not valid JSON"))
        # Names written for another build are refused, never filed under
        # whatever group now has the same id.
        stale = viratools._save_library_names_text("primary", json.dumps({sid: "Old name"}),
                                                   "2026-01-01T00:00:00+00:00")
        self.assertIn("rebuilt", stale)
        self.assertIn("nothing was saved", stale)
        self.assertIn("rebuilt", viratools._save_library_names_text(
            "primary", json.dumps({sid: "No build"})))
        self.assertIn("mcp__vira__save_library_names", viratools.WRITE_TOOLS)
        self.assertEqual(library.names("primary"), {sid: "Rockets and orbits"})

    def test_the_prompt_names_only_unnamed_visible_subjects(self):
        self.build()
        prompt = library.names_prompt("primary")
        self.assertIn("mcp__vira__save_library_names", prompt)
        self.assertIn("not instructions", prompt)
        self.assertIn("id a/wiki~", prompt)
        self.assertIn(f"build = {self.idx()['built']}", prompt)
        self.assertNotIn("id f/raw/captures", prompt)
        ids = [g["id"] for g in self.idx()["groups"] if g["level"] in ("region", "subject", "detail")]
        library.save_names("primary", {gid: "Named subject" for gid in ids}, self.idx()["built"])
        with self.assertRaises(library.LibraryError):
            library.names_prompt("primary")


class TheReader(Base):
    def test_links_embeds_order_and_constellation(self):
        self.build()
        d = library.page("primary", "wiki/garden-00.md")
        lm = d["linkmap"]
        self.assertEqual(lm["garden-01"]["rel"], "wiki/garden-01.md")
        self.assertEqual(lm["garden-03"]["rel"], "wiki/garden-03.md")
        self.assertIsNone(lm["no-such-page"])
        self.assertEqual(lm["wiki/assets/plot.png"], {"asset": "wiki/assets/plot.png"})
        # A property naming a page by its path opens in the reader too.
        self.assertEqual(lm["raw/articles/article-1.md"]["rel"], "raw/articles/article-1.md")
        self.assertIn("index.md", [b["rel"] for b in d["backlinks"]])
        self.assertEqual(d["world_id"], library.world_id("primary", "wiki/garden-00.md"))
        sub = d["subject"]
        self.assertEqual(sub["n"], len(d["same"]) + 1)
        titles = [x["title"].lower() for x in d["same"]]
        self.assertEqual(titles, sorted(titles))
        if d["next"]:
            nxt = library.page("primary", d["next"]["rel"])
            self.assertEqual(nxt["prev"]["rel"], "wiki/garden-00.md")
        c = d["constellation"]
        self.assertLessEqual(len(c["nodes"]), library.CONSTELLATION_MAX)
        rings = {n["rel"]: n["ring"] for n in c["nodes"]}
        self.assertEqual(rings["wiki/garden-01.md"], "out")
        self.assertIn(rings.get("index.md"), ("back", None))
        self.assertTrue(any(r == "similar" for r in rings.values()))
        for a, b in c["edges"]:
            self.assertLess(max(a, b), len(c["nodes"]))

    def test_an_idle_index_is_dropped_and_comes_back(self):
        self.build()
        first = library.load("primary")
        library.map_payload("primary")
        library._evict_idle(now=library._cache["primary"]["used"] + library.IDLE_EVICT_S - 1)
        self.assertIs(library.load("primary"), first, "kept while in use")
        library._evict_idle(now=library._cache["primary"]["used"] + library.IDLE_EVICT_S + 1)
        self.assertNotIn("primary", library._cache)
        self.assertFalse([k for k in library._payload_cache if k[0] == "primary"])
        again = library.load("primary")
        self.assertIsNot(again, first)
        self.assertEqual(again["pages"]["rel"], first["pages"]["rel"])

    def test_a_group_detail_lists_its_children(self):
        self.build()
        d = library.group("primary", "a/wiki")
        self.assertEqual(d["n"], 3 * PER_TOPIC)
        self.assertGreaterEqual(len(d["children"]), 3)
        self.assertEqual(sum(c["n"] for c in d["children"]), d["n"])
        f = library.group("primary", "a/Reports")
        self.assertEqual(f["n"], 0, "machine output stays hidden in details too")
        self.assertEqual(library.group("primary", "a/Reports", True)["n"], 120)
        self.assertTrue(f["machine"])


class Subsets(Base):
    def test_save_list_link_and_delete(self):
        self.build()
        s = library.save_subset("primary", {"name": "  Rocket  pages ", "rels": [
            "wiki/rocket-01.md", "wiki/rocket-01.md", "nope.md"],
            "origin": {"kind": "box", "trail": ["wiki", "Rockets"]}})
        self.assertEqual(s["name"], "Rocket pages")
        self.assertEqual(s["rels"], ["wiki/rocket-01.md"])
        self.assertEqual(library.subsets("primary")[0]["n"], 1)
        library.link_world_subset(s["id"], "w123")
        self.assertEqual(library.subset(s["id"])["world_subset"], "w123")
        for raw, needle in (({"name": "", "rels": ["wiki/rocket-01.md"]}, "name the subset"),
                            ({"name": "x", "rels": []}, "at least one page"),
                            ({"name": "x", "rels": ["nope.md"]}, "none of those pages")):
            with self.subTest(needle=needle), self.assertRaises(library.LibraryError) as cm:
                library.save_subset("primary", raw)
            self.assertIn(needle, str(cm.exception))
        with mock.patch.object(library, "MAX_MEMBERS", 2), self.assertRaises(library.LibraryError):
            library.save_subset("primary", {"name": "x", "rels": [
                "wiki/rocket-01.md", "wiki/rocket-02.md", "wiki/rocket-03.md"]})
        self.assertEqual(library.delete_subset(s["id"])["subsets"], [])
        with self.assertRaises(library.LibraryError):
            library.delete_subset(s["id"])


class TheRoutes(Base):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server import main
        self.main = main
        self.client = TestClient(main.app)

    def test_map_is_gzipped_and_errors_are_named(self):
        r = self.client.get("/api/library/map?vault=primary")
        self.assertEqual(r.status_code, 400)
        self.assertIn("build it first", r.text)
        self.build()
        r = self.client.get("/api/library/map?vault=primary",
                            headers={"accept-encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")
        self.assertEqual(r.json()["vault"], "primary")
        self.assertEqual(self.client.get("/api/library/map?vault=../x").status_code, 400)

    def test_build_starts_one_child_process(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        with mock.patch.object(library, "_spawn", return_value=proc) as spawn, \
                mock.patch.object(library, "_start_watch") as watch:
            r = self.client.post("/api/library/build", json={"vault": "primary"})
            again = self.client.post("/api/library/build", json={"vault": "primary"})
        self.assertEqual(r.json()["state"], "building")
        self.assertEqual(again.json()["state"], "building")
        self.assertEqual(spawn.call_count, 1, "a second press joins the running build")
        cmd = spawn.call_args.args[0]
        self.assertEqual(cmd[1:], ["-m", "server.library", "build", "primary"])
        watch.assert_called_once_with("primary", proc)
        library._running.clear()

    def test_names_dispatches_a_job_and_pages_open(self):
        self.build()
        with mock.patch.object(self.main.jobs, "launch", return_value="job7") as launch:
            r = self.client.post("/api/library/names", json={"vault": "primary"})
        self.assertEqual(r.json(), {"job_id": "job7"})
        self.assertEqual(launch.call_args.kwargs["meta"],
                         {"kind": "library-names", "vault": "primary"})
        page = self.client.get("/api/library/page?vault=primary&path=wiki/kitchen-04.md").json()
        self.assertEqual(page["title"], "Kitchen note 4")
        ids = self.client.post("/api/library/world-ids",
                               json={"vault": "primary", "rels": ["wiki/kitchen-04.md"]}).json()
        self.assertEqual(ids["ids"], [page["world_id"]])
        s = self.client.post("/api/library/subsets", json={
            "vault": "primary", "name": "Kitchen", "rels": ["wiki/kitchen-04.md"]}).json()
        self.assertEqual(self.client.get("/api/library/subsets?vault=primary").json()
                         ["subsets"][0]["id"], s["id"])
        self.assertEqual(self.client.delete(f"/api/library/subsets/{s['id']}").status_code, 200)


def _strip(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _body(src, name):
    """The text of one function in a JS source (brace-matched)."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"no function {name}"
    k, parens = m.end(), 1                 # past the parameters, defaults and all
    while parens:
        parens += {"(": 1, ")": -1}.get(src[k], 0)
        k += 1
    i = src.index("{", k)
    depth, j = 0, i
    while True:
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        j += 1
        if depth == 0:
            return src[i:j]


class TheWindow(unittest.TestCase):
    """Source contracts over the shipped files, comments stripped."""

    @classmethod
    def setUpClass(cls):
        read = lambda *p: (ROOT.joinpath(*p)).read_text(encoding="utf-8")
        cls.js = _strip(read("static", "library.js"))
        cls.app = _strip(read("static", "app.js"))
        cls.html = re.sub(r"<!--.*?-->", "", read("static", "index.html"), flags=re.S)
        cls.css = read("static", "library.css")

    def test_it_is_a_window_with_a_deep_link(self):
        self.assertIn('<section id="view-library" class="view">', self.html)
        self.assertIn('<script src="/library.js"></script>', self.html)
        self.assertIn('<link rel="stylesheet" href="/library.css">', self.html)
        self.assertRegex(self.app, r'\{ id: "library", title: "Library"')
        self.assertIn('if (id === "library") window.loadLibrary?.()', self.app)
        self.assertRegex(self.app, r'"library": \(rest\) => \{[^}]*window\.openLibrary')

    def test_double_click_breaks_apart_and_a_tap_waits_for_it(self):
        dbl = re.search(r'cv\.addEventListener\("dblclick".*?\n    \}\);', self.js, re.S).group(0)
        self.assertIn("breakApart(DR.kids[j].g)", dbl)
        self.assertIn("clearTimeout(clickT)", dbl)
        click = re.search(r'cv\.addEventListener\("click".*?\n    \}\);', self.js, re.S).group(0)
        self.assertIn("setTimeout(go, TAP_DELAY_MS)", click)
        self.assertIn("e.detail > 1", click)
        self.assertIn("const TAP_DELAY_MS = 300;", self.js)

    def test_every_detail_has_a_break_it_apart_button(self):
        group = _body(self.js, "openGroup")
        self.assertIn("Break it apart", group)
        self.assertIn('q(".lib-break")?.addEventListener("click", () => { breakApart(g)', group)
        drawer = _body(self.js, "openDrawer")
        self.assertIn("Break it apart", drawer)

    def test_links_open_in_the_reader_with_history(self):
        follow = _body(self.js, "followLink")
        self.assertIn("openPage(link.dataset.rel, { push: true", follow)
        self.assertNotIn("openNoteWindow", follow)
        self.assertIn("historyGo(-1)", _body(self.js, "onKey"))

    def test_the_action_bar_carries_every_action(self):
        reader = _body(self.js, "renderReader")
        for label in ("Links <b>", "Backlinks <b>", "Similar <b>", "Subject <b>",
                      "&lsaquo; Prev", "Next &rsaquo;", ">Ask<", ">Galaxy<",
                      ">New tab<", ">Copy path<", "lib-mini"):
            self.assertIn(label, reader)
        side = _body(self.js, "bindSide")
        self.assertIn("toGalaxy(d.title, [d.rel].concat(near))", side)
        self.assertIn('window.open("/" + libraryHash(S.vault, d.rel)', side)
        self.assertIn("copyText(d.path)", side)

    def test_the_galaxy_gets_world_ids_and_its_own_layout(self):
        to = _body(self.js, "toGalaxy")
        self.assertIn('post("/api/library/world-ids"', to)
        self.assertIn("window.worldOpenSubset({ name, ids, worldSubset })", to)
        # A saved Library subset is saved in the galaxy too, and linked.
        self.assertIn('post("/api/world/subsets", { name, recipe })', to)
        self.assertIn("/world`", to)
        atlas = _strip((ROOT / "static" / "atlas.js").read_text(encoding="utf-8"))
        hook = re.search(r"window\.worldOpenSubset = async .*?\n  \};", atlas, re.S).group(0)
        self.assertIn("recipe: { steps: [{ seeds:", hook)
        self.assertIn("await openSubset(sub)", hook)
        self.assertIn("S.loading || !S.world", hook, "waits for the World to load")

    def test_nothing_is_wider_than_a_phone(self):
        self.assertIn("@media (max-width: 759px)", self.css)
        self.assertIn(".lib-root.lib-sheet-open .lib-side", self.css)
        self.assertRegex(self.css, r"\.lib-bar \.search \{[^}]*font-size: 16px")
        self.assertRegex(self.css, r"\.lib-ask \.search \{[^}]*font-size: 16px")


class TheHarness(unittest.TestCase):
    def test_layout_and_renderer(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        result = subprocess.run(
            [node, str(ROOT / "tests" / "library_frontend_harness.js")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=30, check=False)
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)


class Registered(unittest.TestCase):
    def test_config_backup_and_registry_know_it(self):
        from server import backup, joblog, modulemap, modulemodels
        self.assertEqual(settings.DEFAULTS["library_machine_dirs"], {})
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["library_machine_dirs"], {})
        self.assertIn("library-subsets.json", backup.FILES)
        self.assertEqual(joblog._KIND_BY_META["library-names"], "Library names")
        ids = {m["id"] for m in modulemap.DEFAULT_MODULES}
        self.assertTrue({"library-engine", "library-win"} <= ids)
        self.assertEqual(modulemodels.module_for_path("/api/library/names"), "library")


if __name__ == "__main__":
    unittest.main()

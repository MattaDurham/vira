"""Vira's Documents: soft pointers, and no read state at all.

The behaviours worth pinning, because each is a decision rather than an
implementation detail:
  1. register() is idempotent on the locator - both the producers and the
     backfill sweep call it repeatedly.
  2. Nothing is ever filed away as read (owner's call, 2026-11-04): an old
     document, a per-session retro and an entry carrying a pre-2026-11-04
     `completed` stamp are all listed exactly like a fresh one, and the
     routes that marked documents read are gone.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import plans, reading, readinglist, settings


class Base(unittest.TestCase):
    """Every source the sweep READS is rooted at one tmp fixture directory.

    Patching the store is not isolation. backfill() reads four producers, and
    any one of them left pointing at the checkout sweeps the owner's real
    documents into the assertions — which is what happened: `1 != 25` in a
    checkout with 24 real documents, green in a fresh worktree and in CI,
    because those have nothing on disk to leak. Rooting the sources beats
    mocking them out: the real code path still runs, it just runs over a
    directory this test made."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = root / "reading-list.json"
        self.static = root / "static"
        self.docs = self.static / "docs"
        self.vault = root / "vault"
        # The walkthrough films. Rooted here like every other source: without
        # it walkthroughs.root() falls back to settings' lab_root and the sweep
        # reads the owner's REAL directory — 32 films leaking into a fixture
        # that asserts zero. That is the trap _sources() documents, and this is
        # the seam that closes it.
        self.films = root / "walkthroughs"
        (self.static / "explainer").mkdir(parents=True)
        self.docs.mkdir(parents=True)
        self.vault.mkdir()
        self.films.mkdir()
        for p in (mock.patch.object(readinglist, "STORE", self.store),
                  mock.patch.object(readinglist, "ROOT", root),
                  mock.patch.object(readinglist, "EXPLAINER_DIR",
                                    self.static / "explainer"),
                  mock.patch.object(readinglist, "DOCS_DIR", self.docs),
                  mock.patch.object(readinglist, "WALKTHROUGH_DIR", self.films),
                  mock.patch.object(plans, "REG_PATH", root / "plans.json"),
                  # the only setting these paths read is vault_root; anything
                  # else answers empty, which every reader treats as dormant
                  mock.patch.object(settings, "get", side_effect=self._setting)):
            p.start()
            self.addCleanup(p.stop)

    def _setting(self, key):
        return str(self.vault) if key == "vault_root" else ""

    def session_note(self, *names):
        """Vault retros, where _vault_documents() looks for them."""
        d = self.vault / "Sessions"
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / f"{n}.md").write_text("## a\n## b\n", encoding="utf-8")
        return d

    def dossier(self, name, title, sections=()):
        d = self.static / "explainer" / name
        d.mkdir(parents=True, exist_ok=True)
        body = "".join(f'<h2 id="{sid}">{lbl}</h2><p>x</p>'
                       for sid, lbl in sections)
        (d / "index.html").write_text(
            f"<html><head><title>{title}</title></head><body>{body}</body></html>",
            encoding="utf-8")
        return f"/explainer/{name}/"


class RegisterTests(Base):
    def test_register_returns_an_entry_with_a_legal_slug(self):
        it = readinglist.register("Where we stand", "dossier", "/explainer/audit/")
        self.assertTrue(it["id"].startswith("rl_"))
        self.assertEqual(it["kind"], "dossier")
        self.assertNotIn("completed", it)       # no read state is ever written
        # the slug keys the dedupe and must stay a reading.py-legal name
        self.assertRegex(it["slug"], reading.NAME_RE)

    def test_register_is_idempotent_on_the_locator(self):
        a = readinglist.register("Audit", "dossier", "/explainer/audit/")
        b = readinglist.register("Audit", "dossier", "/explainer/audit/")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(readinglist.library()), 1)

    def test_reregistering_refreshes_the_title(self):
        readinglist.register("Old name", "dossier", "/explainer/audit/")
        readinglist.register("New name", "dossier", "/explainer/audit/")
        self.assertEqual(readinglist.library()[0]["title"], "New name")

    def test_same_locator_different_kind_is_a_different_entry(self):
        # The REGISTRY fact this test has always named: register() is
        # idempotent per (locator_kind, locator), so two kinds are two rows.
        # Asserted on the store rather than the queue, because the queue is a
        # VIEW and now de-duplicates — see the two tests below.
        readinglist.register("A", "dossier", "x", "url")
        readinglist.register("A", "plan", "x", "plan")
        self.assertEqual(len(readinglist._load()["items"]), 2)

    def test_the_queue_shows_one_row_for_a_document_registered_twice(self):
        """A plan is registered by BOTH producers — plans.save_plan as
        `plan:pl_...` and the sitedocs sweep as `url:/docs/plans/....html`.
        Eleven of them on the live store. Same slug, so the list shows one."""
        readinglist.register("A", "dossier", "x", "url")
        readinglist.register("A", "plan", "x", "plan")
        rows = readinglist.library()
        self.assertEqual(len(rows), 1)
        # The plan locator wins: plans.py resolves it and knows whether the
        # file is still there, where a url row for a deleted plan reads live.
        self.assertEqual(rows[0]["locator_kind"], "plan")

    def test_two_documents_with_different_titles_are_never_collapsed(self):
        """The dedupe keys on SLUG, which is derived from the title — so it
        can only ever merge things that are the same document under two
        locators, never two genuinely different ones."""
        readinglist.register("A", "dossier", "x", "url")
        readinglist.register("B", "plan", "x", "plan")
        self.assertEqual(len(readinglist.library()), 2)

    def test_bad_kind_and_bad_locator_kind_are_refused(self):
        with self.assertRaises(ValueError):
            readinglist.register("A", "nonsense", "/x/")
        with self.assertRaises(ValueError):
            readinglist.register("A", "dossier", "/x/", "carrier-pigeon")

    def test_empty_locator_is_refused(self):
        with self.assertRaises(ValueError):
            readinglist.register("A", "dossier", "   ")

    def test_new_documents_preserve_more_than_2000_pointers(self):
        first = readinglist.register("Doc 0", "dossier", "/d0/")
        # Seed an existing full registry without 2000 unrelated rewrites. Half
        # carry a legacy read stamp from before read state was removed.
        rows = [dict(first, id=first["id"] if i == 0 else f"rl_fixture{i}",
                     title=f"Doc {i}", slug=f"doc-{i}", locator=f"/d{i}/",
                     **({"completed": "2026-08-01T00:00:00"} if i % 2 else {}))
                for i in range(2000)]
        self.store.write_text(json.dumps({"items": rows}), encoding="utf-8")

        readinglist.register("Doc 2000", "dossier", "/d2000/")

        self.assertEqual(readinglist.get(first["id"]), first)
        self.assertEqual(readinglist.counts(), {"total": 2001})
        self.assertEqual(len(readinglist.library()), 2001)
        # A sweep must find the original row, never recreate it.
        again = readinglist.register("Doc 0", "dossier", "/d0/")
        self.assertEqual(again["id"], first["id"])

    def test_title_falls_back_to_the_locator(self):
        it = readinglist.register("", "dossier", "/explainer/audit/")
        self.assertEqual(it["title"], "/explainer/audit/")


class NoReadStateTests(Base):
    def test_a_legacy_read_stamp_never_hides_a_document(self):
        """Entries written before 2026-11-04 may still carry `completed`. The
        list must show them, and must not hand the stamp to the client."""
        it = readinglist.register("Audit", "dossier", "/explainer/audit/")
        data = json.loads(self.store.read_text(encoding="utf-8"))
        data["items"][0]["completed"] = "2026-08-05T10:00:00-04:00"
        self.store.write_text(json.dumps(data), encoding="utf-8")
        rows = readinglist.library()
        self.assertEqual([r["id"] for r in rows], [it["id"]])
        self.assertNotIn("completed", rows[0])
        self.assertNotIn("read", rows[0])

    def test_the_read_api_is_gone(self):
        for name in ("complete", "completed", "progress", "sections",
                     "queue", "_is_stale", "FRESH_DAYS"):
            self.assertFalse(hasattr(readinglist, name), name)

    def test_forget_drops_the_pointer_and_leaves_the_file(self):
        loc = self.dossier("audit", "Audit")
        it = readinglist.register("Audit", "dossier", loc)
        readinglist.forget(it["id"])
        self.assertEqual(readinglist.library(), [])
        self.assertTrue((self.static / "explainer" / "audit" / "index.html").is_file())

    def test_counts_is_the_whole_library(self):
        readinglist.register("A", "dossier", "/a/")
        readinglist.register("B", "dossier", "/b/")
        self.assertEqual(readinglist.counts(), {"total": 2})


class VaultContainmentTests(Base):
    def test_a_locator_escaping_the_vault_resolves_to_nothing(self):
        (self.vault / "Sessions").mkdir(parents=True)
        secret = Path(self.tmp.name) / "secret.md"     # a sibling of the vault
        secret.write_text("## a\n## b\n", encoding="utf-8")
        it = readinglist.register("esc", "retro", "../secret.md", "vault")
        self.assertIsNone(readinglist.source_path(it))
        self.assertTrue(readinglist.library()[0]["missing"])

    def test_no_vault_configured_is_dormant_not_broken(self):
        with mock.patch.object(settings, "get", return_value=""):
            it = readinglist.register("r", "retro", "Sessions/r.md", "vault")
            self.assertIsNone(readinglist.source_path(it))
            self.assertEqual(readinglist._vault_documents(), [])


class BackfillTests(Base):
    def test_an_empty_fixture_root_sweeps_nothing(self):
        """The leak detector, and the reason the other sources are rooted
        rather than mocked out. A sweep over an empty fixture must find nothing
        — in a fresh worktree, in CI, and in the live checkout whose real
        static/docs/ holds dozens of documents. A source that reads the
        checkout instead of the fixture fails here first, and fails loudly."""
        self.assertEqual(readinglist.backfill(), {"added": 0, "total": 0})

    def test_backfill_finds_dossiers_and_reads_their_titles(self):
        self.dossier("audit", "Where we stand - end-of-week audit")
        self.dossier("other", "Another dossier")
        out = readinglist.backfill()
        self.assertEqual(out["added"], 2)
        titles = sorted(i["title"] for i in readinglist.library())
        self.assertEqual(titles,
                         ["Another dossier", "Where we stand - end-of-week audit"])

    def test_backfill_is_idempotent(self):
        self.dossier("audit", "Audit")
        readinglist.backfill()
        second = readinglist.backfill()
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(readinglist.library()), 1)

    def test_a_directory_without_an_index_is_skipped(self):
        (self.static / "explainer" / "assets").mkdir()
        self.assertEqual(readinglist.backfill()["added"], 0)

    def test_top_level_pages_are_not_taken(self):
        # the four hand-authored atlas pages are the system explainer, not reading
        (self.static / "explainer" / "modules.html").write_text(
            "<title>Modules</title>", encoding="utf-8")
        self.assertEqual(readinglist.backfill()["added"], 0)


class AgeTests(Base):
    """The sweep used to file anything older than 14 days, and every
    per-session retro, as already read. Both now list like anything else."""

    def _aged(self, name, days):
        loc = self.dossier(name, name.title())
        old = datetime.now(timezone.utc) - timedelta(days=days)
        f = self.static / "explainer" / name / "index.html"
        os.utime(f, (old.timestamp(), old.timestamp()))
        return loc

    def test_an_old_document_is_listed_like_a_fresh_one(self):
        self._aged("ancient", 90)
        self._aged("today", 0)
        out = readinglist.backfill()
        self.assertEqual(out, {"added": 2, "total": 2})
        self.assertEqual(sorted(i["title"] for i in readinglist.library()),
                         ["Ancient", "Today"])

    def test_every_per_session_retro_is_listed(self):
        names = [f"2026-07-25 020{n} vira" for n in range(5)]
        names += ["2026-07-25 day vira", "2026-07-25 cross-project"]
        self.session_note(*names)
        self.assertEqual(readinglist.backfill()["added"], 7)
        self.assertEqual(len(readinglist.library()), 7)

    def test_a_filename_date_prefix_beats_the_mtime(self):
        # retros are rsynced, so mtime is when the copy landed, not when it
        # was written — the name carries the truth
        f = Path(self.tmp.name) / "2026-05-17 1009 some-project.md"
        f.write_text("## a\n## b\n", encoding="utf-8")
        self.assertTrue(readinglist._file_created(f, f.stem)
                        .startswith("2026-05-17"))


class RoomShelfTests(Base):
    """Rooms are tabs in the Reader, not document rows (owner's call,
    2026-07-27). Legacy room rows stay registered but leave every view."""

    def test_a_room_row_is_excluded_from_every_view(self):
        readinglist.register("Anthropic reading room", "room",
                             "/reading/anthropic-universe.html", "url")
        readinglist.register("A dossier", "dossier", "/explainer/x/", "url")
        self.assertEqual([i["kind"] for i in readinglist.library()], ["dossier"])
        self.assertEqual(readinglist.counts(), {"total": 1})

    def test_backfill_no_longer_sweeps_rooms_in(self):
        with mock.patch.object(readinglist, "_rooms",
                               side_effect=AssertionError("retired from backfill")):
            out = readinglist.backfill()
        self.assertEqual(out["added"], 0)


class StoreTests(Base):
    def test_a_corrupt_store_does_not_take_the_queue_down(self):
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text("{{{ not json", encoding="utf-8")
        self.assertEqual(readinglist.library(), [])
        it = readinglist.register("A", "dossier", "/a/")
        self.assertEqual(len(readinglist.library()), 1)
        written = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(written["items"][0]["id"], it["id"])

    def test_library_is_newest_first(self):
        readinglist.register("old", "dossier", "/a/", created="2026-01-01T00:00:00")
        readinglist.register("new", "dossier", "/b/", created="2026-07-27T00:00:00")
        self.assertEqual([i["title"] for i in readinglist.library()], ["new", "old"])

    def test_same_day_documents_are_newest_landed_first(self):
        with mock.patch.object(
                readinglist, "_now",
                side_effect=["2026-09-02T09:00:00-04:00",
                             "2026-09-02T17:00:00-04:00"]):
            readinglist.register("morning", "dossier", "/morning/",
                                 created="2026-09-02T08:00:00-04:00")
            readinglist.register("afternoon", "dossier", "/afternoon/",
                                 created="2026-09-02")
        self.assertEqual([i["title"] for i in readinglist.library()],
                         ["afternoon", "morning"])


class SlugTests(Base):
    def test_slugs_are_always_reading_name_legal(self):
        for text in ("Where we stand — audit!", "2026-07-27 retro", "///",
                     "a" * 200, "Ünicode Ttitle", ""):
            self.assertRegex(readinglist.slugify(text), reading.NAME_RE)


class WalkthroughLocatorTests(Base):
    """Films resolve against the lab mount, never static/. The day after the
    library shipped, every one of the 32 films read `missing: true` — the
    row's own thumbnail rendered while the click refused with "no longer
    where it was saved" — because source_path resolved /walkthroughs/ under
    ROOT/static like any other url locator (2026-08-05)."""

    def film(self, slug):
        d = self.films / slug
        d.mkdir()
        (d / "index.html").write_text("<title>x</title>", encoding="utf-8")

    def test_a_film_on_disk_is_not_missing(self):
        self.film("vira-2026-07-30-onboarding")
        it = readinglist.register("Film", "walkthrough",
                                  "/walkthroughs/vira-2026-07-30-onboarding/")
        self.assertFalse(readinglist._missing(it))

    def test_a_dormant_install_reads_a_film_as_missing(self):
        it = readinglist.register("Film", "walkthrough",
                                  "/walkthroughs/somewhere/")
        from server import walkthroughs
        with mock.patch.object(readinglist, "WALKTHROUGH_DIR", None), \
             mock.patch.object(walkthroughs.settings, "raw", return_value={}):
            self.assertTrue(readinglist._missing(it))

    def test_a_registered_but_deleted_film_is_missing(self):
        it = readinglist.register("Film", "walkthrough",
                                  "/walkthroughs/never-captured/")
        self.assertTrue(readinglist._missing(it))


class LibraryTests(Base):
    def test_the_library_dedupes_twins(self):
        # A plan registered both ways (plans.save_plan + the sitedocs sweep);
        # the library must not hand the story panel the same build twice.
        readinglist.register("Same Title", "plan", "pl_1", "plan")
        readinglist.register("Same Title", "plan", "/docs/same-title.html")
        self.assertEqual(len(readinglist.library()), 1)


class RouteTests(Base):
    """The join: what the Reader's client actually gets over HTTP."""

    def setUp(self):
        # Import the app BEFORE Base pins settings.get: main's import reads
        # real defaults (CPU slots and the like) that the pin would blank.
        from fastapi.testclient import TestClient
        from server import docthumbs, doctags, main, walkthroughs
        super().setUp()
        self.client = TestClient(main.app)
        for p in (mock.patch.object(doctags, "annotate", side_effect=lambda r: r),
                  mock.patch.object(docthumbs, "annotate", side_effect=lambda r: r),
                  mock.patch.object(doctags, "vocabulary", return_value={}),
                  mock.patch.object(doctags, "status", return_value={}),
                  mock.patch.object(walkthroughs, "films", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def test_the_list_serves_every_document_and_no_read_state(self):
        a = readinglist.register("A", "dossier", "/a/")
        readinglist.register("B", "dossier", "/b/")
        data = json.loads(self.store.read_text(encoding="utf-8"))
        data["items"][0]["completed"] = "2026-08-05T10:00:00-04:00"
        self.store.write_text(json.dumps(data), encoding="utf-8")
        body = self.client.get("/api/reading/list").json()
        self.assertEqual(sorted(r["title"] for r in body["items"]), ["A", "B"])
        self.assertNotIn("completed", body)
        self.assertNotIn("queue", body)
        self.assertEqual(body["counts"], {"total": 2})
        row = next(r for r in body["items"] if r["id"] == a["id"])
        self.assertNotIn("completed", row)

    def test_the_mark_read_routes_are_gone(self):
        it = readinglist.register("A", "dossier", "/a/")
        r = self.client.post(f"/api/reading/list/{it['id']}/complete",
                             json={"done": True})
        self.assertIn(r.status_code, (404, 405))
        for room in ("inflow", "some-room"):
            self.assertIn(self.client.get(f"/api/reading/{room}/done").status_code,
                          (404, 405))
            self.assertIn(self.client.post(f"/api/reading/{room}/done",
                                           json={"id": "x", "done": True}).status_code,
                          (404, 405))
        self.assertNotIn("completed", readinglist.get(it["id"]))


class WalkthroughResolutionTests(Base):
    """/walkthroughs/ is a MOUNT off lab_root, not a static/ subtree.

    Resolving films under static/ read all 32 as missing, so the client
    refused to open documents the server was serving fine (2026-08-05,
    "the files aren't findable")."""

    def film(self, slug):
        d = self.films / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text("<title>F</title>", encoding="utf-8")
        return f"/walkthroughs/{slug}/"

    def test_a_film_on_disk_is_not_missing(self):
        loc = self.film("vira-2026-07-30-onboarding")
        readinglist.register("Film", "walkthrough", loc)
        self.assertFalse(readinglist.library()[0]["missing"])

    def test_source_path_resolves_into_the_walkthrough_root(self):
        loc = self.film("vira-2026-07-30-onboarding")
        it = readinglist.register("Film", "walkthrough", loc)
        p = readinglist.source_path(it)
        self.assertEqual(
            p, (self.films / "vira-2026-07-30-onboarding").resolve()
            / "index.html")

    def test_a_film_that_left_the_disk_reads_missing(self):
        readinglist.register("Film", "walkthrough", "/walkthroughs/gone/")
        self.assertTrue(readinglist.library()[0]["missing"])

    def test_escape_from_the_walkthrough_root_is_refused(self):
        (Path(self.tmp.name) / "secret.html").write_text("x", encoding="utf-8")
        it = readinglist.register("Sneak", "walkthrough",
                                  "/walkthroughs/../secret.html")
        self.assertIsNone(readinglist.source_path(it))
        self.assertTrue(readinglist.library()[0]["missing"])

    def test_a_dormant_install_reads_missing_rather_than_raising(self):
        with mock.patch.object(readinglist, "WALKTHROUGH_DIR",
                               Path(self.tmp.name) / "absent"):
            readinglist.register("Film", "walkthrough", "/walkthroughs/x/")
            self.assertTrue(readinglist.library()[0]["missing"])


if __name__ == "__main__":
    unittest.main()


class ConnectedFolderTests(Base):
    """Folders outside the checkout, connected through `reader_sources`.

    The seam matters more than the feature. A connected folder is READ: its
    documents are soft pointers like every other source, nothing is copied, and
    containment is re-checked on every resolution so that removing a folder
    from the config is by itself enough to stop the server reaching into it."""

    def setUp(self):
        super().setUp()
        self.connected = Path(self.tmp.name) / "connected"
        self.connected.mkdir()
        self.sources = [{"path": str(self.connected), "kind": "dossier"}]

    def _setting(self, key):
        if key == "reader_sources":
            return self.sources
        return str(self.vault) if key == "vault_root" else ""

    def html(self, name, title):
        p = self.connected / name
        p.write_text(
            f"<html><head><title>{title}</title></head><body>x</body></html>",
            encoding="utf-8")
        return p

    def test_a_connected_file_is_swept_in_under_its_own_title(self):
        p = self.html("why-anthropic.html", "Why Anthropic")
        readinglist.backfill()
        rows = [r for r in readinglist.library() if r["locator_kind"] == "file"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Why Anthropic")
        self.assertEqual(rows[0]["kind"], "dossier")
        self.assertEqual(readinglist.source_path(rows[0]), p.resolve())

    def test_a_bundle_directory_is_taken_by_its_index(self):
        d = self.connected / "dossier-with-assets"
        d.mkdir()
        (d / "index.html").write_text("<html><body>no title</body></html>",
                                      encoding="utf-8")
        readinglist.backfill()
        rows = [r for r in readinglist.library() if r["locator_kind"] == "file"]
        self.assertEqual([r["title"] for r in rows], ["dossier-with-assets"])

    def test_nothing_configured_sweeps_nothing(self):
        self.html("stray.html", "Stray")
        self.sources = []
        readinglist.backfill()
        self.assertEqual(
            [r for r in readinglist.library() if r["locator_kind"] == "file"], [])

    def test_disconnecting_the_folder_makes_its_documents_missing(self):
        self.html("doc.html", "Doc")
        readinglist.backfill()
        row = [r for r in readinglist.library() if r["locator_kind"] == "file"][0]
        self.sources = []
        self.assertIsNone(readinglist.source_path(row))
        self.assertTrue(readinglist._missing(row))

    def test_a_glob_may_not_reach_outside_the_folder(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.html").write_text("<html></html>", encoding="utf-8")
        self.html("kept.html", "Kept")
        self.sources = [{"path": str(self.connected), "glob": "../outside/*.html"}]
        readinglist.backfill()
        rows = [r for r in readinglist.library() if r["locator_kind"] == "file"]
        self.assertEqual([r["title"] for r in rows], ["Kept"])

    def test_source_path_refuses_a_locator_outside_every_root(self):
        outside = Path(self.tmp.name) / "outside.html"
        outside.write_text("<html></html>", encoding="utf-8")
        it = readinglist.register("Outside", "dossier", outside.as_posix(), "file")
        self.assertIsNone(readinglist.source_path(it))

    def test_a_malformed_row_is_skipped_rather_than_fatal(self):
        self.html("good.html", "Good")
        self.sources = ["not a dict", {"kind": "dossier"}, {"path": ""},
                        {"path": str(self.connected)}]
        readinglist.backfill()
        rows = [r for r in readinglist.library() if r["locator_kind"] == "file"]
        self.assertEqual([r["title"] for r in rows], ["Good"])

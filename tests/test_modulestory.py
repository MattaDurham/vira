"""The module story: right-click a window, "What is this?".

What is worth pinning, because each is a decision:
  1. The corpus is the LIBRARY — read and unread alike. A module's build
     story is mostly documents already marked read, which queue() excludes.
  2. The window id itself always counts as a tag, so a vocabulary that
     converges on the id needs no table edit.
  3. Companions alias to their host; ids with no row answer None (the route
     404s) rather than inventing an empty story for chrome like the palette.
  4. Untagged documents are invisible to a story but COUNTED (`pending`), so
     a thin story reads as still-being-tagged, never as "this is all".
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import (changelog, doctags, ideas, ideatags, joblog, modulemap,
                    modulestory, readinglist, settings)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.films = root / "walkthroughs"
        self.films.mkdir()
        (root / "static").mkdir()
        self.sessions = root / "Sessions"
        self.sessions.mkdir()
        patches = (
            # The timeline reads the change log and the idea tags too, so
            # every store either one touches is rooted here — a story that
            # read the live ledger would pass or fail with the machine.
            mock.patch.object(changelog, "SESSIONS", self.sessions),
            mock.patch.object(ideas, "STORE", root / "ideas.json"),
            mock.patch.object(ideatags, "STORE", root / "idea-index.json"),
            mock.patch.object(joblog, "STORE", root / "jobs-log.json"),
            mock.patch.object(readinglist, "STORE", root / "reading-list.json"),
            mock.patch.object(readinglist, "ROOT", root),
            mock.patch.object(readinglist, "WALKTHROUGH_DIR", self.films),
            mock.patch.object(doctags, "STORE", root / "doc-index.json"),
            mock.patch.object(modulemap, "STORE", root / "modules.json"),
            mock.patch.object(settings, "get", side_effect=lambda k: ""),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def retro(self, stem, goal, ships, session_id="sess-1", time="21:00"):
        """A session retro on disk, dated from its stem."""
        day = stem[:10]
        body = "\n".join("- " + s for s in ships) or "_none_"
        (self.sessions / (stem + ".md")).write_text(
            f"---\ndate: {day}\ntime: \"{time}\"\nsession_id: {session_id}\n"
            f"---\n\n## Goal\n\n{goal}\n\n## Shipped\n\n{body}\n",
            encoding="utf-8")

    def reg(self, title, kind="plan", locator=None, tags=None, read=False):
        locator = locator or "/docs/" + title.replace(" ", "-") + ".html"
        rid = readinglist.register(title, kind, locator)["id"]
        if read:
            readinglist.complete(rid, True)
        if tags is not None:
            s = {"entries": {}, "last_pass": ""}
            if doctags.STORE.exists():
                s = json.loads(doctags.STORE.read_text(encoding="utf-8"))
            s["entries"][rid] = {"tags": {"module": tags, "subproject": [],
                                          "theme": [], "concept": []},
                                 "tagged": True}
            doctags.STORE.write_text(json.dumps(s), encoding="utf-8")
        return rid


class TableTests(Base):
    def test_every_row_is_an_alias_or_carries_tags_and_a_map_id(self):
        for wid, row in modulestory.WINDOWS.items():
            if row.get("alias"):
                self.assertIn(row["alias"], modulestory.WINDOWS, wid)
                self.assertIsNone(modulestory.WINDOWS[row["alias"]].get("alias"),
                                  "an alias must resolve in one hop")
            else:
                self.assertTrue(row.get("tags"), wid)
                self.assertTrue(row.get("map"), wid)

    def test_companions_resolve_to_find(self):
        wid, row = modulestory.resolve("find-cloud")
        self.assertEqual(wid, "find")
        self.assertTrue(row)

    def test_chrome_has_no_story(self):
        self.assertIsNone(modulestory.story("launchpad"))
        self.assertIsNone(modulestory.story("no-such-window"))


class StoryTests(Base):
    def test_filters_on_the_module_axis(self):
        self.reg("Brief plan", tags=["brief"])
        self.reg("Atlas plan", tags=["atlas"])
        s = modulestory.story("brief")
        self.assertEqual([d["title"] for d in s["docs"]], ["Brief plan"])
        self.assertEqual(s["counts"], {"plan": 1})

    def test_read_documents_are_the_story_too(self):
        self.reg("Old brief plan", tags=["brief"], read=True)
        s = modulestory.story("brief")
        self.assertEqual(len(s["docs"]), 1)
        self.assertTrue(s["docs"][0]["read"])

    def test_the_window_id_itself_counts_as_a_tag(self):
        # "feed" is not in its own tags list; the implicit {win_id} is what
        # keeps a vocabulary that converges on the id working with no edit.
        self.assertNotIn("feed", modulestory.WINDOWS["feed"]["tags"])
        self.reg("2026-07-11 0900 vira", kind="retro", tags=["feed"])
        s = modulestory.story("feed")
        self.assertEqual(len(s["docs"]), 1)

    def test_untagged_documents_are_pending_never_invisible_silently(self):
        self.reg("Tagged", tags=["brief"])
        self.reg("Untagged", tags=None)
        s = modulestory.story("brief")
        self.assertEqual(len(s["docs"]), 1)
        self.assertEqual(s["pending"], 1)

    def test_registry_blurb_joins_when_present(self):
        modulemap.STORE.write_text(json.dumps({"modules": [
            {"id": "attention-win", "name": "Attention",
             "what": "The visual focus cockpit."}]}), encoding="utf-8")
        s = modulestory.story("attention")
        self.assertEqual(s["title"], "Attention")
        self.assertEqual(s["what"], "The visual focus cockpit.")

    def test_a_fresh_install_answers_from_the_seeded_registry(self):
        # No modules.json on disk: modulemap seeds DEFAULT_MODULES, so the
        # story still opens with a real description rather than a blank.
        self.reg("Brief plan", tags=["brief"])
        s = modulestory.story("brief")
        self.assertTrue(s["what"])
        self.assertEqual(len(s["docs"]), 1)

    def test_film_metadata_joins_on_the_locator(self):
        slug = "vira-2026-08-01-brief"
        d = self.films / slug
        d.mkdir()
        (d / "index.html").write_text("<title>Brief film</title>",
                                      encoding="utf-8")
        (d / "thumb.jpg").write_bytes(b"x")
        self.reg("Brief film", kind="walkthrough",
                 locator=f"/walkthroughs/{slug}/", tags=["brief"])
        s = modulestory.story("brief")
        self.assertEqual(s["docs"][0]["film"]["thumb"],
                         f"/walkthroughs/{slug}/thumb.jpg")


class TimelineTests(Base):
    """The 2026-09-02 rebuild: the story is a chronology, and every
    change-log entry rides it flagged rather than filtered."""

    def test_an_empty_fixture_tells_no_story(self):
        # The isolation guard: this module reads the library, the change
        # log, the idea tags and the job ledger. Nothing here may leak in.
        s = modulestory.story("brief")
        self.assertEqual(s["docs"], [])
        self.assertEqual(s["events"], [])
        self.assertEqual(s["stats"]["changes"], 0)
        self.assertEqual(s["stats"]["changelog_total"], 0)

    def test_a_shipped_line_inherits_its_retros_module_tags(self):
        stem = "2026-07-11 2100 vira"
        self.retro(stem, "Ship the brief.", ["The brief window shipped."])
        self.reg(stem, kind="retro", locator=f"Sessions/{stem}.md",
                 tags=["brief"])
        s = modulestory.story("brief")
        ships = [e for e in s["events"] if e["kind"] == "ship"]
        self.assertEqual(len(ships), 1)
        self.assertTrue(ships[0]["hit"])
        self.assertTrue(ships[0]["strong"])
        self.assertEqual(ships[0]["why"], ["tag:brief"])
        self.assertEqual(ships[0]["retro"], stem)
        # The retro narrates its day, and the library row is what opens it.
        day = s["days"]["2026-07-11"]
        self.assertTrue(day["hit"])
        self.assertEqual(day["retros"][0]["goal"], "Ship the brief.")
        self.assertEqual(day["retros"][0]["ships"], 1)
        self.assertEqual(day["retros"][0]["doc"]["kind"], "retro")
        self.assertEqual(s["stats"]["sessions"], 1)

    def test_wording_joins_weakly_and_only_on_a_phrase(self):
        stem = "2026-07-12 0900 vira"
        self.retro(stem, "Odds and ends.",
                   ["Reworked the decision card cascade.",
                    "Fixed a review of the atlas layout."])
        # The retro is untagged, so only wording can join its lines.
        s = modulestory.story("attention")
        ships = {e["text"]: e for e in s["events"] if e["kind"] == "ship"}
        card = ships["Reworked the decision card cascade."]
        self.assertTrue(card["hit"])
        self.assertFalse(card["strong"])
        self.assertEqual(card["why"], ["word:decision card"])
        # "review" is one of the window's TAGS but a single word is never a
        # key phrase — the second line must not join on it.
        other = ships["Fixed a review of the atlas layout."]
        self.assertFalse(other["hit"])
        self.assertEqual(s["stats"]["changes"], 1)
        self.assertEqual(s["stats"]["strong"], 0)

    def test_every_changelog_entry_rides_the_timeline_flagged(self):
        self.retro("2026-07-13 0900 vira", "Elsewhere.",
                   ["Something about the galaxy renderer."])
        s = modulestory.story("brief")
        self.assertEqual(s["stats"]["changelog_total"], 1)
        self.assertEqual(s["stats"]["changes"], 0)
        e = s["events"][0]
        self.assertFalse(e["hit"])
        self.assertEqual(e["why"], [])
        # The day still narrates, marked as not this module's.
        self.assertFalse(s["days"]["2026-07-13"]["hit"])

    def test_a_done_idea_joins_through_its_own_tags(self):
        ideas.STORE.write_text(json.dumps({"items": [
            {"id": "idea_1", "text": "Ship the thing", "status": "done",
             "project": "Vira", "created": "2026-07-14T10:00:00+00:00",
             "updated": "2026-07-14T20:00:00+00:00"}]}), encoding="utf-8")
        ideatags.STORE.write_text(json.dumps({"entries": {
            "idea_1": {"tags": {"module": ["subscriptions"], "subproject": [],
                                "theme": [], "concept": []},
                       "hash": "x", "tagged": "2026-07-14"}}}),
            encoding="utf-8")
        s = modulestory.story("subs")
        done = [e for e in s["events"] if e["kind"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertTrue(done[0]["strong"])
        self.assertEqual(done[0]["why"], ["tag:subscriptions"])
        self.assertEqual(done[0]["idea_id"], "idea_1")

    def test_documents_are_events_and_retros_are_narrative(self):
        self.reg("Brief plan", tags=["brief"])
        stem = "2026-07-15 0800 vira"
        self.reg(stem, kind="retro", locator=f"Sessions/{stem}.md",
                 tags=["brief"])
        s = modulestory.story("brief")
        kinds = [e["kind"] for e in s["events"]]
        self.assertEqual(kinds, ["plan"])
        self.assertEqual(s["events"][0]["doc"]["title"], "Brief plan")
        self.assertEqual(s["events"][0]["weight"], 0.8)
        # A retro with no change-log day of its own still narrates.
        self.assertIn("2026-07-15", s["days"])
        self.assertTrue(s["days"]["2026-07-15"]["hit"])
        self.assertEqual(s["counts"], {"plan": 1, "retro": 1})

    def test_eras_bucket_by_month_and_count_hits_apart_from_totals(self):
        self.retro("2026-07-11 2100 vira", "g", ["decision card work"])
        self.retro("2026-08-02 2100 vira", "g", ["unrelated"],
                   session_id="sess-2")
        s = modulestory.story("attention")
        self.assertEqual([(e["key"], e["hits"], e["total"]) for e in s["eras"]],
                         [("2026-07", 1, 1), ("2026-08", 0, 1)])
        self.assertEqual(s["eras"][0]["label"], "July 2026")
        self.assertEqual(s["stats"]["first"], "2026-07-11")
        self.assertEqual(s["stats"]["last"], "2026-07-11")
        self.assertEqual(s["stats"]["changelog_last"], "2026-08-02")

    def test_another_projects_retro_never_narrates(self):
        # The library holds TC-IL and CRM retros too; a module tag is Vira's
        # vocabulary, so only Vira's own retros may join a story.
        self.reg("2026-07-11 0900 TC-IL", kind="retro",
                 locator="Sessions/2026-07-11 0900 TC-IL.md", tags=["brief"])
        self.reg("2026-07-11 1000 vira", kind="retro",
                 locator="Sessions/2026-07-11 1000 vira.md", tags=["brief"])
        s = modulestory.story("brief")
        self.assertEqual([d["title"] for d in s["docs"]],
                         ["2026-07-11 1000 vira"])
        self.assertEqual(s["stats"]["sessions"], 1)

    def test_events_read_oldest_first(self):
        self.retro("2026-08-02 2100 vira", "g", ["b"], session_id="s2")
        self.retro("2026-07-11 2100 vira", "g", ["a"])
        s = modulestory.story("brief")
        self.assertEqual([e["text"] for e in s["events"]], ["a", "b"])


class KeyPhraseTests(unittest.TestCase):
    def test_single_words_never_become_phrases(self):
        row = {"tags": ["find", "search-and-recall"], "keywords": ["chat"]}
        reg = {"keywords": ["find window", "brain"]}
        self.assertEqual(modulestory.key_phrases(row, reg),
                         ["search and recall", "find window"])

    def test_every_window_row_has_at_least_one_phrase(self):
        for wid, row in modulestory.WINDOWS.items():
            if row.get("alias"):
                continue
            self.assertTrue(modulestory.key_phrases(row, {}), wid)


class CoverageTests(Base):
    def test_every_story_window_is_audited_with_reasons_named(self):
        cov = modulestory.coverage()
        ids = {r["id"] for r in cov["windows"]}
        self.assertEqual(ids, {w for w, r in modulestory.WINDOWS.items()
                               if not r.get("alias")})
        for r in cov["windows"]:
            self.assertTrue(r["registry"], r["id"])     # seeded registry
            self.assertIn("only 0 change-log entries join", r["thin"])
        att = next(r for r in cov["windows"] if r["id"] == "attention")
        self.assertEqual(att["aliases"], ["brief", "review", "subsviz"])

    def test_the_new_windows_have_stories(self):
        for wid in ("imageatlas", "research"):
            self.assertIsNotNone(modulestory.story(wid), wid)

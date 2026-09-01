"""Change-log contracts, two families:

Scoping — the Vira changelog folds in ONLY Vira-project ideas and jobs
that ran in the Vira checkout / its worktrees (or were dispatched from a
Vira-project idea). Entries belonging to other projects never leak in.

Day ledger (2026-09-01 redesign) — entries own the timeline: every entry
dates itself from its own store field, groups are local calendar days,
retros are narrative overlays, and no group is ever dateless. The
regression this pins: 44 entries spanning seven weeks once rendered as
"Today · shipped 0s ago" because anything a retro didn't claim landed in
a dateless bucket stamped Date.now() at render.

Run: .venv/bin/python -m unittest tests.test_changelog
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from server import changelog


RETRO = """---
date: 2026-07-11
time: "21:00"
---

## Goal

Ship the widget.

## Shipped

- The widget shipped.
"""


def _idea(iid, text, project, status="done", updated="2026-07-11T20:00:00"):
    return {"id": iid, "text": text, "project": project,
            "status": status, "updated": updated}


def _job(jid, cwd, idea_id=None, started="2026-07-11T20:30:00"):
    return {"id": jid, "prompt": "do " + jid, "cwd": cwd,
            "idea_id": idea_id, "status": "done", "started": started,
            "model": None, "session_id": None}


class ChangelogScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sessions = Path(self.tmp.name)
        (sessions / "2026-07-11 vira.md").write_text(RETRO, encoding="utf-8")
        self.sessions_patch = mock.patch.object(
            changelog, "SESSIONS", sessions)
        self.sessions_patch.start()
        self.addCleanup(self.sessions_patch.stop)

    def _groups(self, ideas, jobs):
        with mock.patch.object(changelog.ideasstore, "list_items",
                               return_value=ideas), \
             mock.patch.object(changelog.joblog, "list_records",
                               return_value=jobs):
            return changelog.groups()

    def _texts(self, groups):
        return [e["text"] for g in groups for e in g["entries"]]

    def test_foreign_project_ideas_stay_out(self):
        groups = self._groups(
            [_idea("i1", "vira thing", "Vira"),
             _idea("i2", "site thing", "other-project"),
             _idea("i3", "legacy thing", None)],   # project-less = Vira
            [])
        texts = self._texts(groups)
        self.assertIn("vira thing", texts)
        self.assertIn("legacy thing", texts)
        self.assertNotIn("site thing", texts)

    def test_foreign_cwd_jobs_stay_out(self):
        vira_cwd = str(changelog.REPO)
        groups = self._groups([], [
            _job("j1", vira_cwd),
            _job("j2", str(Path.home())),
            _job("j3", str(Path.home() / "TC-IL")),
        ])
        texts = " ".join(self._texts(groups))
        self.assertIn("do j1", texts)
        self.assertNotIn("do j2", texts)
        self.assertNotIn("do j3", texts)

    def test_a_worktree_job_counts_as_vira(self):
        # Feature-branch jobs run in .worktrees/<slug> inside the checkout
        # (or, pre-2026-07-29, in a sibling named vira-<slug>); both are
        # Vira work and must not be dropped by the cwd scope.
        inside = str(changelog.REPO / ".worktrees" / "some-branch")
        sibling = str(changelog.REPO.parent
                      / (changelog.REPO.name + "-old-style"))
        groups = self._groups([], [_job("j1", inside), _job("j2", sibling)])
        texts = " ".join(self._texts(groups))
        self.assertIn("do j1", texts)
        self.assertIn("do j2", texts)

    def test_vira_idea_job_counts_even_from_foreign_cwd(self):
        groups = self._groups(
            [_idea("i1", "vira idea", "Vira", status="open")],
            [_job("j1", str(Path.home()), idea_id="i1"),
             _job("j2", str(Path.home()), idea_id="missing")])
        texts = " ".join(self._texts(groups))
        # idea-linked jobs are named for the idea, not the prompt head
        self.assertIn("Implement — vira idea", texts)
        self.assertNotIn("do j2", texts)

    def test_job_labels_prefer_meaning_over_prompt_head(self):
        routine = _job("j1", str(changelog.REPO))
        routine["meta"] = {"routine_id": "system-map", "kind": "digest"}
        ask = _job("j2", str(changelog.REPO))
        ask["prompt"] = ('You are Vira, spawned from a right-click.\n"""\n'
                         "What is the demo contact waiting on?\n" + '"""\n')
        groups = self._groups([], [routine, ask])
        texts = " ".join(self._texts(groups))
        self.assertIn("System map — refresh the registry from the change log",
                      texts)
        self.assertIn("Ask Vira — What is the demo contact waiting on?", texts)

    def test_edited_title_wins_in_the_log(self):
        job = _job("j1", str(changelog.REPO))
        job["title"] = "Morning subs pipeline"      # an owner rename
        groups = self._groups([], [job])
        self.assertIn("Morning subs pipeline — done",
                      " ".join(self._texts(groups)))

    def test_retro_ships_survive_scoping(self):
        groups = self._groups([], [])
        self.assertEqual(groups[0]["date"], "2026-07-11")
        self.assertIn("The widget shipped.", self._texts(groups))

    def test_job_entries_carry_their_full_job_id(self):
        # The merged Record stream renders the ledger BESIDE the changelog
        # and dedupes one job's two appearances by this key — the id inside
        # the entry text is truncated to 8 chars and unusable for the join.
        long_id = "abcdef0123456789"
        groups = self._groups([], [_job(long_id, str(changelog.REPO))])
        jobs = [e for g in groups for e in g["entries"]
                if e["kind"] == "job"]
        self.assertEqual([e.get("job_id") for e in jobs], [long_id])

    def test_non_job_entries_carry_no_job_id(self):
        groups = self._groups([_idea("i1", "vira thing", "Vira")], [])
        for g in groups:
            for e in g["entries"]:
                if e["kind"] != "job":
                    self.assertNotIn("job_id", e)


class DayLedgerTests(unittest.TestCase):
    """The 2026-09-01 inversion: entries date themselves, groups are days,
    retros are overlays, nothing is ever unfiled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = Path(self.tmp.name)
        self.sessions_patch = mock.patch.object(
            changelog, "SESSIONS", self.sessions)
        self.sessions_patch.start()
        self.addCleanup(self.sessions_patch.stop)

    def _build(self, ideas=(), jobs=()):
        with mock.patch.object(changelog.ideasstore, "list_items",
                               return_value=list(ideas)), \
             mock.patch.object(changelog.joblog, "list_records",
                               return_value=list(jobs)):
            return changelog.build()

    def _retro(self, name, text):
        (self.sessions / name).write_text(text, encoding="utf-8")

    # ---- the regression guard for the whole redesign ----

    def test_no_group_is_ever_dateless(self):
        self._retro("2026-07-11 vira.md", RETRO)
        groups, _ = self._build(
            [_idea("i1", "an idea", "Vira", updated="2026-08-19T09:00:00")],
            [_job("j1", str(changelog.REPO), started="2026-08-06T14:00:00")])
        self.assertTrue(groups)
        for g in groups:
            self.assertTrue(g["date"], "a group escaped without a date")

    def test_an_unmatched_job_lands_on_its_own_day(self):
        self._retro("2026-07-11 vira.md", RETRO)
        groups, _ = self._build(
            [], [_job("j1", str(changelog.REPO),
                      started="2026-08-06T14:00:00")])
        g = next(g for g in groups if g["date"] == "2026-08-06")
        self.assertTrue(g["no_retro"])
        self.assertIn("do j1", " ".join(e["text"] for e in g["entries"]))

    def test_an_unmatched_idea_lands_on_its_own_day(self):
        groups, _ = self._build(
            [_idea("i1", "late idea", "Vira",
                   updated="2026-08-19T09:00:00")], [])
        g = next(g for g in groups if g["date"] == "2026-08-19")
        self.assertTrue(g["no_retro"])
        self.assertEqual(g["entries"][0]["text"], "late idea")

    def test_idea_updated_is_read_in_local_time(self):
        # The ideas store writes UTC; slicing [:10] filed evening ideas a
        # day forward. The expected day is computed, not hardcoded, so the
        # test holds in any zone.
        stamp = "2026-08-07T02:00:00+00:00"
        want = datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d")
        groups, _ = self._build(
            [_idea("i1", "night idea", "Vira", updated=stamp)], [])
        self.assertEqual([g["date"] for g in groups], [want])

    def test_a_job_dates_by_finished_over_started(self):
        job = _job("j1", str(changelog.REPO), started="2026-08-06T23:50:00")
        job["finished"] = "2026-08-07T00:10:00"
        groups, _ = self._build([], [job])
        self.assertEqual([g["date"] for g in groups], ["2026-08-07"])

    # ---- retros as overlays ----

    def test_a_zero_entry_retro_still_supplies_the_goal(self):
        self._retro("2026-07-20 1000 vira.md", """---
date: 2026-07-20
time: "10:00"
---

## Goal

Quiet housekeeping.

## Shipped

_none_
""")
        groups, _ = self._build(
            [_idea("i1", "tidy thing", "Vira",
                   updated="2026-07-20T12:00:00")], [])
        g = next(g for g in groups if g["date"] == "2026-07-20")
        self.assertFalse(g["no_retro"])
        self.assertEqual(g["goal"], "Quiet housekeeping.")
        self.assertEqual([e["source"] for e in g["entries"]], ["idea"])

    def test_a_day_retro_heading_parses(self):
        # Pins the reader side of the nightly generator's contract: `##
        # Shipped` (not `Shipped today`), title line as narrative, no time.
        self._retro("2026-08-30 day vira.md", """---
tags: [day-retro, retrospective, auto, vira]
project: vira
date: 2026-08-30
generated_by: daily-provenance
---

# vira - day retro 2026-08-30

Label bug fixed; showroom shipped

## The arc

Prose about the day.

## Shipped

- Fixed the label bug.
- Shipped the showroom.
""")
        groups, _ = self._build([], [])
        g = next(g for g in groups if g["date"] == "2026-08-30")
        self.assertEqual(len(g["entries"]), 2)
        self.assertEqual(g["goal"], "Label bug fixed; showroom shipped")

    def test_multiple_retros_on_one_day_make_one_group(self):
        self._retro("2026-07-11 0900 vira.md", RETRO.replace('"21:00"', '"09:00"'))
        self._retro("2026-07-11 2100 vira.md", RETRO)
        groups, _ = self._build([], [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["retros"]), 2)
        self.assertEqual(len(groups[0]["entries"]), 2)

    def test_a_slug_named_retro_is_read(self):
        # /close-session and hand-written retros are named
        # `YYYY-MM-DD HHMM vira — <slug>.md`; the old glob never saw them.
        self._retro("2026-08-14 1123 vira — react-rebuild.md", """---
date: 2026-08-14
time: "11:23"
---

## Goal

Rebuild the thing.

## Shipped

- Rebuilt the thing.
""")
        groups, _ = self._build([], [])
        self.assertEqual([g["date"] for g in groups], ["2026-08-14"])
        self.assertEqual(groups[0]["entries"][0]["text"], "Rebuilt the thing.")

    # ---- never invent, never silently drop ----

    def test_a_retro_with_no_date_frontmatter_uses_its_filename(self):
        self._retro("2026-07-21 1144 vira.md", """---
created: 2026-07-21
---

## Shipped

- Something real.
""")
        groups, _ = self._build([], [])
        self.assertEqual([g["date"] for g in groups], ["2026-07-21"])

    def test_a_retro_with_no_recoverable_date_is_warned_not_invented(self):
        self._retro("notes vira.md", "## Shipped\n\n- A bullet.\n")
        groups, warnings = self._build([], [])
        self.assertEqual(groups, [])
        self.assertTrue(any("no recoverable date" in w for w in warnings))

    def test_a_duplicate_generation_is_skipped_and_warned(self):
        self._retro("2026-07-11 vira.md", RETRO)
        self._retro("2026-07-11 vira (2).md",
                    RETRO.replace("The widget shipped.", "A near-copy."))
        groups, warnings = self._build([], [])
        texts = [e["text"] for g in groups for e in g["entries"]]
        self.assertNotIn("A near-copy.", texts)
        self.assertTrue(any("duplicate generations" in w for w in warnings))

    # ---- dedupe and links ----

    def test_an_idea_and_its_job_render_once(self):
        # joblog.name() titles an idea-dispatched job with the idea's own
        # text, so the resolved idea and its job would say the same thing.
        groups, _ = self._build(
            [_idea("i1", "build the widget", "Vira",
                   updated="2026-07-20T15:00:00")],
            [_job("j1", str(changelog.REPO), idea_id="i1",
                  started="2026-07-20T14:00:00")])
        entries = [e for g in groups for e in g["entries"]]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "job")
        self.assertIn("build the widget", entries[0]["text"])

    def test_a_job_links_to_its_sessions_retro(self):
        self._retro("2026-07-11 2100 vira.md", RETRO.replace(
            "---\n\n## Goal", 'session_id: sess-abc-123\n---\n\n## Goal'))
        job = _job("j1", str(changelog.REPO))
        job["session_id"] = "sess-abc-123"
        groups, _ = self._build([], [job])
        e = next(e for g in groups for e in g["entries"]
                 if e["kind"] == "job")
        self.assertEqual(e["retro"], "2026-07-11 2100 vira")
        self.assertEqual(e["session_id"], "sess-abc-123")

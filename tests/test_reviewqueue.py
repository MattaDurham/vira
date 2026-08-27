"""The needs-review queue: the aggregator behind the brief's picker.

Covers the registry contract (a source that raises costs only its own
rows), each source reader including its missing-file and empty cases, the
duplicated-proposal-id split that decides HOW a lesson is approved, and the
subprocess boundary — every ledger write goes through lessons.py, argv is
list-form and validated, and NO TEST EVER RUNS THE REAL SCRIPT: subprocess
is mocked in every case, so the owner's ledger is never touched.

Isolation follows the lessonwatch/readinglist lesson: every root this
module reads is repointed into ONE tmp fixture through the real settings
override keys, and test_every_root_points_into_the_fixture is the guard —
a root added later that reads the real machine fails it on sight.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import ideas, journal, reviewqueue, subs_visuals, triage


PROPOSAL_A = "Never merge a branch without running the suite in the live tree."
PROPOSAL_B = "Say merged or not merged in one plain sentence, first line."
PROPOSAL_C = "Reproduce a retrieval miss with a literal grep before ranking."
PROPOSAL_D = "Never delete owner data without an explicit instruction."

# One canonical document since 2026-08-11: FACTS.md was folded into Master
# History and deleted, so every open flag now lives under Part V.
HISTORY_DOC = """# Part IV. Something

- Not a flag.

# Part V. Open questions and contradictions

These issues remain visible.

- **Comp band ambiguity.** The posting says one thing and the dossier
  another. Resolve at first contact.
- **~~Old question~~ - RESOLVED (2026-08-10).** Already ruled on.
- **Second live flag.** Still open.
- **Disposition reconciliation.** Two aggregates disagree.

# Part VI. Provenance

- Not a flag either.

## Confidentiality and privacy

- Nothing here should surface.
"""


def proposal(pid, text, day="2026-07-25", tier=2, status="proposed"):
    return {"id": pid, "text": text, "tier": tier, "why": "because",
            "day": day, "project": "vira", "status": status}


class _Case(unittest.TestCase):
    """One tmp fixture; every reviewqueue root repointed into it through the
    real settings override keys, so the override path itself is exercised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "sessions"
        self.state.mkdir()
        self.record = self.root / "self"
        (self.record / "inbox" / "notes").mkdir(parents=True)
        (self.record / "canon").mkdir(parents=True)
        self.script = self.root / "lessons.py"
        self.script.write_text("# stub\n", encoding="utf-8")
        self.cfg = {
            "lessons_script_path": str(self.script),
            "lessons_state_dir": str(self.state),
            "self_record": str(self.record),
        }
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(reviewqueue.settings, "raw", lambda: self.cfg)
        p.start()
        self.addCleanup(p.stop)
        # applications.self_record() reads the same config object
        from server import applications
        p2 = mock.patch.object(applications.settings, "raw", lambda: self.cfg)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(reviewqueue, "DECISIONS",
                               self.root / "review-decided.json")
        p3.start()
        self.addCleanup(p3.stop)
        p4 = mock.patch.object(ideas, "STORE", self.root / "ideas.json")
        p4.start()
        self.addCleanup(p4.stop)
        # The journal and picker sources read module-attribute roots
        # (journal.STORE / subs_visuals.STATE_FILE); repointed here so
        # roots() reports them inside the fixture too.
        p5 = mock.patch.object(journal, "STORE",
                               self.root / "brief-journal.json")
        p5.start()
        self.addCleanup(p5.stop)
        p6 = mock.patch.object(subs_visuals, "STATE_FILE",
                               self.root / "subs-visuals-state.json")
        p6.start()
        self.addCleanup(p6.stop)
        # The senders source reads through the triage FUNCTION seam (CRM
        # registry + chat.db + the dismissal store) - no path roots() can
        # declare - so the seam itself is pinned in the base fixture, the
        # test_attention.py source-pinning pattern. Tests set self.cands;
        # the stub reads it live.
        self.cands = []
        p7 = mock.patch.object(triage, "candidates",
                               lambda: list(self.cands))
        p7.start()
        self.addCleanup(p7.stop)
        # the in-process senders cache must not leak between tests
        reviewqueue._SENDERS_CACHE.update({"at": 0.0, "rows": None})
        # A journal approve can DISPATCH a session inside the blast
        # radius; pinned passive so no test ever spawns one (the
        # JournalBase isolation lesson). The dispatch test overrides this
        # and stubs the launch.
        p8 = mock.patch.object(journal, "_passive", lambda: True)
        p8.start()
        self.addCleanup(p8.stop)

    # ---------------------------------------------------------- helpers

    def write_proposals(self, rows):
        (self.state / "lessons-proposed.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def write_decided(self, rows):
        (self.state / "lessons-decided.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def by_source(self, key):
        return [i for i in reviewqueue.items()["items"] if i["source"] == key]

    def run_ok(self, stdout="done"):
        return mock.Mock(returncode=0, stdout=stdout, stderr="")

    def write_journal(self, entries):
        (self.root / "brief-journal.json").write_text(
            json.dumps({"entries": entries}), encoding="utf-8")

    def read_journal(self):
        return json.loads((self.root / "brief-journal.json")
                          .read_text(encoding="utf-8"))

    def write_picker_state(self, videos=2, built="2026-08-27 06:00",
                           ready=True, batch_dir=None):
        batch = Path(batch_dir) if batch_dir else self.root / "batch-0627"
        batch.mkdir(parents=True, exist_ok=True)
        if ready:
            (batch / "picker.html").write_text("<html></html>",
                                               encoding="utf-8")
        (self.root / "subs-visuals-state.json").write_text(json.dumps({
            "pending": {
                "batch_dir": str(batch), "built": built,
                "videos": [{"slug": f"v{i}", "title": f"Video {i}",
                            "channel": "ch", "url": "", "video_id": str(i)}
                           for i in range(videos)],
            }}), encoding="utf-8")
        return batch


def jentry(eid, instr, staged=False, resolved=False,
           created="2026-08-10T09:00:00", area="app",
           text="the owner's own note"):
    u = {"instruction": instr, "area": area, "pid_check": "none"}
    if staged:
        u["staged"] = "2026-08-10T09:05:00"
        u["idea_id"] = "idea_deadbeef00"
    if resolved:
        u["resolved"] = "2026-08-11T08:00:00"
    return {"id": eid, "text": text, "person_id": None, "person_name": None,
            "context": None, "created": created, "status": "integrated",
            "result": {"summary": "read", "actions": [], "unapplied": [u]}}


def cand(handle, name="", worthy="yes", business=False, evidence="",
         relationship="", pid=None, msgs=5):
    return {"handle": handle, "person_id": pid, "name": name,
            "relationship": relationship, "evidence": evidence,
            "contact_worthy": worthy, "confidence": "high",
            "action": "needs_name", "tier": None, "msgs": msgs,
            "business": business, "business_signals": [], "company_guess": "",
            "referral_hint": ""}


class RegistryTests(_Case):

    def test_the_seven_sources_are_registered(self):
        self.assertEqual(set(reviewqueue.SOURCES),
                         {"lessons", "inbox", "flags", "ideas",
                          "journal", "senders", "picker"})

    def test_every_root_points_into_the_fixture(self):
        for name, path in reviewqueue.roots().items():
            self.assertTrue(str(path).startswith(str(self.root)),
                            f"{name} reads outside the fixture: {path}")

    def test_a_cold_machine_has_an_empty_quiet_queue(self):
        q = reviewqueue.items()
        self.assertEqual(q["total"], 0)
        self.assertEqual(q["errors"], {})
        self.assertIsNone(reviewqueue.summary())

    def test_a_broken_source_costs_only_its_own_rows(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        boom = mock.Mock(side_effect=OSError("store on fire"))
        with mock.patch.object(reviewqueue.SOURCES["inbox"], "reader", boom):
            q = reviewqueue.items()
        self.assertEqual(q["total"], 1)
        self.assertIn("inbox", q["errors"])
        self.assertIn("store on fire", q["errors"]["inbox"])

    def test_summary_leads_with_the_oldest_and_caps_the_top(self):
        self.write_proposals([
            proposal(f"L{i}", f"Proposal number {i} about something.",
                     day=f"2026-07-{10 + i:02d}") for i in range(8)])
        s = reviewqueue.summary()
        self.assertEqual(s["total"], 8)
        self.assertEqual(len(s["top"]), reviewqueue.BRIEF_TOP)
        self.assertEqual(s["top"][0]["date"], "2026-07-10")

    def test_act_rejects_an_unknown_source_and_a_read_only_one(self):
        with self.assertRaises(KeyError):
            reviewqueue.act("nosuch:1", "approve")
        with self.assertRaises(KeyError):
            reviewqueue.act("lessons", "approve")
        (self.record / "inbox" / "notes" / "2026-08-01-a-note.md").write_text(
            "a thought\n", encoding="utf-8")
        item = self.by_source("inbox")[0]
        with self.assertRaises(ValueError):
            reviewqueue.act(item["id"], "approve")


class LessonSourceTests(_Case):

    def test_only_undecided_proposals_are_listed(self):
        self.write_proposals([
            proposal("L1", PROPOSAL_A),
            proposal("L2", PROPOSAL_B),
            proposal("L3", PROPOSAL_C, status="approved"),
        ])
        self.write_decided([{"id": "L2", "status": "approved",
                             "at": "2026-08-01"}])
        rows = self.by_source("lessons")
        self.assertEqual([r["title"] for r in rows], [PROPOSAL_A])

    def test_a_missing_proposals_file_is_an_empty_store(self):
        self.assertEqual(self.by_source("lessons"), [])

    def test_a_malformed_line_never_costs_the_rest(self):
        (self.state / "lessons-proposed.jsonl").write_text(
            json.dumps(proposal("L1", PROPOSAL_A)) + "\n{ not json\n"
            + json.dumps(proposal("L2", PROPOSAL_B)) + "\n", encoding="utf-8")
        self.assertEqual(len(self.by_source("lessons")), 2)

    def test_rows_sharing_an_id_are_all_shown_and_flagged(self):
        """The defect this queue had to be built around: the generator
        repeats ids, so keying on the id would hide most of the backlog."""
        self.write_proposals([
            proposal("L1", PROPOSAL_A),
            proposal("L1", PROPOSAL_B),
            proposal("L2", PROPOSAL_C),
        ])
        rows = self.by_source("lessons")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["id"] for r in rows}), 3)
        flagged = [r for r in rows if r["note"]]
        self.assertEqual({r["title"] for r in flagged},
                         {PROPOSAL_A, PROPOSAL_B})
        self.assertIn("dup id", [r["ref"] for r in flagged][0])
        self.assertEqual([r["note"] for r in rows if r["title"] == PROPOSAL_C],
                         [""])

    def test_a_unique_id_is_approved_by_id(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=self.run_ok()) as run:
            out = reviewqueue.act(item["id"], "approve")
        self.assertEqual(out["mode"], "id")
        self.assertEqual(run.call_args[0][0],
                         [sys.executable, str(self.script), "approve", "L1"])
        self.assertFalse(run.call_args[1].get("shell", False))

    def test_a_unique_id_is_dropped_by_id(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=self.run_ok()) as run:
            reviewqueue.act(item["id"], "drop")
        self.assertEqual(run.call_args[0][0][2:], ["drop", "L1"])

    def test_a_duplicated_id_is_approved_by_its_exact_text(self):
        self.write_proposals([proposal("L1", PROPOSAL_A, tier=3),
                              proposal("L1", PROPOSAL_B)])
        item = [r for r in self.by_source("lessons")
                if r["title"] == PROPOSAL_A][0]
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=self.run_ok()) as run:
            out = reviewqueue.act(item["id"], "approve")
        self.assertEqual(out["mode"], "text")
        self.assertEqual(run.call_args[0][0][2:],
                         ["add", PROPOSAL_A, "--tier", "3"])
        # and it leaves the queue without the jsonl being rewritten
        left = [r["title"] for r in self.by_source("lessons")]
        self.assertEqual(left, [PROPOSAL_B])
        self.assertIn(PROPOSAL_A,
                      (self.state / "lessons-proposed.jsonl")
                      .read_text(encoding="utf-8"))

    def test_a_duplicated_id_is_never_dropped_upstream(self):
        """drop <id> would bury every sibling row, so the decision is banked
        locally instead and the siblings stay pending."""
        self.write_proposals([proposal("L1", PROPOSAL_A),
                              proposal("L1", PROPOSAL_B)])
        item = [r for r in self.by_source("lessons")
                if r["title"] == PROPOSAL_A][0]
        with mock.patch.object(reviewqueue.subprocess, "run") as run:
            out = reviewqueue.act(item["id"], "drop")
        run.assert_not_called()
        self.assertEqual(out["mode"], "local")
        self.assertEqual([r["title"] for r in self.by_source("lessons")],
                         [PROPOSAL_B])

    def test_an_unsafe_id_never_reaches_a_subprocess(self):
        self.write_proposals([proposal("L1; rm -rf ~", PROPOSAL_D)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                reviewqueue.act(item["id"], "approve")
        run.assert_not_called()

    def test_an_unknown_action_never_reaches_a_subprocess(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with mock.patch.object(reviewqueue.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                reviewqueue.act(item["id"], "delete")
        run.assert_not_called()

    def test_a_stale_item_id_raises_rather_than_guessing(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        with self.assertRaises(KeyError):
            reviewqueue.act("lessons:deadbeefdead", "approve")

    def test_a_failing_cli_surfaces_its_error(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        failed = mock.Mock(returncode=2, stdout="", stderr="not pending")
        with mock.patch.object(reviewqueue.subprocess, "run",
                               return_value=failed):
            with self.assertRaises(RuntimeError) as cm:
                reviewqueue.act(item["id"], "approve")
        self.assertIn("not pending", str(cm.exception))

    def test_a_missing_cli_is_a_named_failure(self):
        self.script.unlink()
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        item = self.by_source("lessons")[0]
        with self.assertRaises(FileNotFoundError):
            reviewqueue.act(item["id"], "approve")


class InboxSourceTests(_Case):

    def test_the_documented_steady_state_shows_nothing(self):
        self.assertEqual(self.by_source("inbox"), [])

    def test_a_missing_inbox_is_not_an_error(self):
        import shutil
        shutil.rmtree(self.record / "inbox")
        self.assertEqual(self.by_source("inbox"), [])
        self.assertEqual(reviewqueue.items()["errors"], {})

    def test_notes_surface_oldest_first_and_read_only(self):
        d = self.record / "inbox" / "notes"
        (d / "2026-08-09-a-thing.md").write_text(
            "# Heading\n\nThe first real line.\n", encoding="utf-8")
        (d / "2026-08-02-older.md").write_text("Older note.\n",
                                               encoding="utf-8")
        (d / "README.md").write_text("how this folder works\n",
                                     encoding="utf-8")
        (d / ".DS_Store").write_text("x", encoding="utf-8")
        rows = self.by_source("inbox")
        self.assertEqual([r["date"] for r in rows],
                         ["2026-08-02", "2026-08-09"])
        self.assertEqual(rows[1]["why"], "The first real line.")
        self.assertEqual(rows[0]["actions"], [])


class FlagSourceTests(_Case):

    def write_canon(self):
        (self.record / "canon" / "MASTER_HISTORY.md").write_text(
            HISTORY_DOC, encoding="utf-8")

    def test_a_missing_canon_file_is_an_empty_store(self):
        self.assertEqual(self.by_source("flags"), [])

    def test_open_flags_are_surfaced_from_the_canonical_record(self):
        self.write_canon()
        rows = self.by_source("flags")
        self.assertEqual([r["title"] for r in rows],
                         ["Comp band ambiguity", "Second live flag",
                          "Disposition reconciliation"])
        self.assertTrue(all(r["actions"] == [] for r in rows))

    def test_a_resolved_flag_is_not_waiting_on_anyone(self):
        self.write_canon()
        self.assertNotIn("Old question",
                         [r["title"] for r in self.by_source("flags")])

    def test_a_wrapped_bullet_is_one_item(self):
        self.write_canon()
        row = self.by_source("flags")[0]
        self.assertIn("Resolve at first contact", row["why"])

    def test_the_section_ends_at_the_next_heading(self):
        self.write_canon()
        titles = " ".join(r["title"] for r in self.by_source("flags"))
        self.assertNotIn("Nothing here", titles)
        self.assertNotIn("Not a flag", titles)

    def test_ids_are_stable_across_reads(self):
        self.write_canon()
        self.assertEqual([r["id"] for r in self.by_source("flags")],
                         [r["id"] for r in self.by_source("flags")])


class IdeaSourceTests(_Case):

    def test_only_proposed_ideas_are_queued(self):
        ideas.add("An approved thing", status="open")
        prop = ideas.add("A staged proposal", status="proposed")
        rows = self.by_source("ideas")
        self.assertEqual([r["title"] for r in rows], ["A staged proposal"])
        self.assertEqual(rows[0]["id"], "ideas:" + prop["id"])

    def test_approving_opens_it_without_dispatching_a_build(self):
        prop = ideas.add("A staged proposal", status="proposed")
        reviewqueue.act("ideas:" + prop["id"], "approve")
        got = [i for i in ideas.list_items() if i["id"] == prop["id"]][0]
        self.assertEqual(got["status"], "open")
        self.assertEqual(self.by_source("ideas"), [])

    def test_dropping_records_who_decided_it(self):
        prop = ideas.add("A staged proposal", status="proposed")
        reviewqueue.act("ideas:" + prop["id"], "drop")
        got = [i for i in ideas.list_items() if i["id"] == prop["id"]][0]
        self.assertEqual(got["status"], "dropped")
        self.assertIn("declined by the owner", got["note"])


class JournalSourceTests(_Case):

    def test_only_unstaged_unresolved_instructions_surface(self):
        self.write_journal([
            jentry("note_aaaaaaaaaa", "Fix the calendar overlap judgment."),
            jentry("note_bbbbbbbbbb", "Already staged elsewhere.",
                   staged=True),
            jentry("note_cccccccccc", "Already resolved.", resolved=True),
        ])
        rows = self.by_source("journal")
        self.assertEqual([r["title"] for r in rows],
                         ["Fix the calendar overlap judgment."])
        self.assertEqual(rows[0]["why"], "the owner's own note")
        self.assertEqual(rows[0]["date"], "2026-08-10")
        self.assertIn("note_aaaaaaaaaa", rows[0]["ref"])
        self.assertEqual(rows[0]["actions"], ["approve", "drop"])

    def test_a_missing_journal_store_is_an_empty_store(self):
        self.assertEqual(self.by_source("journal"), [])
        self.assertEqual(reviewqueue.items()["errors"], {})

    def test_dropping_resolves_it_and_the_journal_keeps_the_record(self):
        self.write_journal([jentry("note_aaaaaaaaaa", "Do the thing.")])
        row = self.by_source("journal")[0]
        out = reviewqueue.act(row["id"], "drop")
        self.assertTrue(out["ok"])
        self.assertEqual(self.by_source("journal"), [])
        u = self.read_journal()["entries"][0]["result"]["unapplied"][0]
        self.assertEqual(u["instruction"], "Do the thing.")
        self.assertTrue(u.get("resolved"))

    def test_approving_stages_through_journals_own_machinery(self):
        # _passive is pinned True in the base fixture, so staging takes
        # the proposed path and no session can be dispatched.
        self.write_journal([jentry("note_aaaaaaaaaa", "Do the thing.")])
        row = self.by_source("journal")[0]
        out = reviewqueue.act(row["id"], "approve")
        self.assertTrue(out["ok"])
        self.assertIn("approval bar", out["output"])
        staged = [i for i in ideas.list_items()
                  if i["text"] == "Do the thing."]
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["status"], "proposed")
        # the stamp is persisted, so the row leaves the queue for good
        self.assertEqual(self.by_source("journal"), [])
        u = self.read_journal()["entries"][0]["result"]["unapplied"][0]
        self.assertTrue(u.get("staged"))
        self.assertEqual(u.get("idea_id"), staged[0]["id"])

    def test_approving_dispatches_inside_the_blast_radius(self):
        self.write_journal([jentry("note_aaaaaaaaaa", "Do the thing.",
                                   area="app")])
        row = self.by_source("journal")[0]
        from server import session
        with mock.patch.object(journal, "_passive", lambda: False), \
             mock.patch.object(session.sessions, "launch",
                               return_value="jobabc123def") as launch:
            out = reviewqueue.act(row["id"], "approve")
        self.assertTrue(out["ok"])
        self.assertEqual(out["job_id"], "jobabc123def")
        launch.assert_called_once()
        got = [i for i in ideas.list_items()
               if i["text"] == "Do the thing."][0]
        self.assertEqual(got["status"], "open")
        u = self.read_journal()["entries"][0]["result"]["unapplied"][0]
        self.assertEqual(u.get("job_id"), "jobabc123def")

    def test_staging_that_cannot_place_reports_honestly(self):
        self.write_journal([jentry("note_aaaaaaaaaa", "Do the thing.")])
        row = self.by_source("journal")[0]
        with mock.patch.object(journal, "_stage_one", lambda e, u: None):
            out = reviewqueue.act(row["id"], "approve")
        self.assertFalse(out["ok"])
        self.assertIn("could not place", out["output"])
        # nothing was stamped, so the row is still in the queue
        self.assertEqual(len(self.by_source("journal")), 1)

    def test_a_stale_journal_id_raises_rather_than_guessing(self):
        self.write_journal([jentry("note_aaaaaaaaaa", "Do the thing.")])
        with self.assertRaises(KeyError):
            reviewqueue.act("journal:note_aaaaaaaaaa:000000000000", "drop")


class SenderSourceTests(_Case):

    def test_contact_worthy_people_surface_read_only(self):
        self.cands = [
            cand("+12125550142", name="Casey", evidence="intro'd by Eric"),
            cand("+18005550177", business=True),
            cand("+13475550163", worthy="unsure"),
            cand("dana@example.com", evidence="emailed about the boat"),
        ]
        rows = self.by_source("senders")
        self.assertEqual([r["title"] for r in rows],
                         ["Casey — +12125550142", "dana@example.com"])
        self.assertEqual(rows[0]["why"], "intro'd by Eric")
        self.assertEqual(rows[0]["actions"], [])
        self.assertEqual(rows[0]["ref"], "People > Triage")

    def test_the_cap_holds(self):
        self.cands = [cand(f"+121255501{i:02d}") for i in range(9)]
        self.assertEqual(len(self.by_source("senders")),
                         reviewqueue.SENDERS_TOP)

    def test_the_read_is_cached_briefly(self):
        counted = mock.Mock(return_value=[cand("+12125550142")])
        with mock.patch.object(triage, "candidates", counted):
            reviewqueue.items()
            reviewqueue.items()
        self.assertEqual(counted.call_count, 1)

    def test_a_broken_triage_costs_only_its_own_rows(self):
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        boom = mock.Mock(side_effect=OSError("chat.db unreadable"))
        with mock.patch.object(triage, "candidates", boom):
            q = reviewqueue.items()
        self.assertEqual(q["counts"]["lessons"], 1)
        self.assertIn("senders", q["errors"])


class PickerSourceTests(_Case):

    def test_a_missing_state_file_is_dormant(self):
        self.assertEqual(self.by_source("picker"), [])
        self.assertEqual(reviewqueue.items()["errors"], {})

    def test_a_pending_batch_is_one_pointer_row(self):
        self.write_picker_state(videos=3)
        rows = self.by_source("picker")
        self.assertEqual(len(rows), 1)
        self.assertIn("batch of 3 videos", rows[0]["title"])
        self.assertEqual(rows[0]["actions"], [])
        self.assertEqual(rows[0]["open"], "#subs-visuals")
        self.assertEqual(rows[0]["date"], "2026-08-27")
        self.assertIn("batch-0627", rows[0]["why"])
        self.assertNotIn("not built", rows[0]["why"])

    def test_an_unbuilt_picker_is_named_on_the_row(self):
        self.write_picker_state(ready=False)
        self.assertIn("picker not built yet",
                      self.by_source("picker")[0]["why"])

    def test_a_running_apply_job_is_not_waiting(self):
        self.write_picker_state()
        with mock.patch.object(subs_visuals, "_job_for",
                               lambda b: {"id": "j", "status": "running",
                                          "started": "", "finished": ""}):
            self.assertEqual(self.by_source("picker"), [])

    def test_a_finished_apply_job_leaves_the_row_pending(self):
        # state only clears when the apply marks the batch reviewed; a
        # failed/finished job means the decision is back with the owner
        self.write_picker_state()
        with mock.patch.object(subs_visuals, "_job_for",
                               lambda b: {"id": "j", "status": "error",
                                          "started": "", "finished": ""}):
            self.assertEqual(len(self.by_source("picker")), 1)

    def test_a_stale_record_without_a_batch_dir_is_nothing(self):
        (self.root / "subs-visuals-state.json").write_text(
            json.dumps({"pending": {"batch_dir": ""}}), encoding="utf-8")
        self.assertEqual(self.by_source("picker"), [])


class BriefSectionTests(_Case):

    def test_the_brief_section_is_quiet_when_nothing_waits(self):
        from server import brief
        self.assertIsNone(brief._review_section())

    def test_the_brief_never_breaks_on_this_section(self):
        from server import brief
        with mock.patch.object(reviewqueue, "summary",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(brief._review_section())

    def test_the_brief_section_carries_the_head_of_the_queue(self):
        from server import brief
        self.write_proposals([proposal("L1", PROPOSAL_A)])
        section = brief._review_section()
        self.assertEqual(section["total"], 1)
        self.assertEqual(section["top"][0]["title"], PROPOSAL_A)
        self.assertEqual([s["key"] for s in section["sources"]], ["lessons"])


if __name__ == "__main__":
    unittest.main()

"""server/attention.py — the tier-1 aggregator behind the attention window.

The module is an AGGREGATOR, so these tests pin the classification, the
edge-trigger tokens, the sort, and the never-break-on-a-source contract —
with every source pinned at its seam. Attention reads SIX things outside
its own code (the session registry's job dirs, the joblog ledger, circuit
runs, the orphan-work store, the health stores, the review queue), so the
base case pins all of them to empty and every test overrides only what it
is about; `test_an_empty_world_composes_nothing` is the isolation guard —
a source added later that reads the machine instead of a seam fails it on
sight (the readinglist lesson)."""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from server import attention


class FakeHandle:
    kind = "detached"

    def __init__(self, jid, jdir, spec=None):
        self.id = jid
        self.dir = Path(jdir)
        self.spec = spec or {}


class FakeRegistry:
    def __init__(self, handles=(), pending=()):
        self.lock = threading.Lock()
        self.sessions = {h.id: h for h in handles}
        self._pending = list(pending)

    def pending_all(self):
        return self._pending


def iso(days_ago=0.0):
    return (datetime.now()
            - timedelta(days=days_ago)).isoformat(timespec="seconds")


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        from server import (aihealth, circuits, jobboards, joblog,
                            orphanwork, reviewqueue)
        patches = [
            mock.patch.object(joblog, "list_records", return_value=[]),
            mock.patch.object(circuits, "list_runs", return_value=[]),
            mock.patch.object(orphanwork, "compose",
                              return_value={"items": []}),
            mock.patch.object(aihealth, "summary",
                              return_value={"state": "green"}),
            mock.patch.object(jobboards, "health",
                              return_value={"registered": 0, "fetched": "",
                                            "errors": {}}),
            mock.patch.object(reviewqueue, "items",
                              return_value={"items": [], "total": 0,
                                            "counts": {}}),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("VIRA_PASSIVE", None)
        # the review note is time-cached in-process; a test must never read
        # the previous test's answer
        attention._review_cache.update({"at": 0.0, "data": None})

    def handle(self, jid, state, spec=None):
        jdir = self.dir / jid
        jdir.mkdir(parents=True, exist_ok=True)
        (jdir / "state.json").write_text(json.dumps(state),
                                         encoding="utf-8")
        return FakeHandle(jid, jdir, spec)

    def compose(self, handles=(), pending=()):
        return attention.compose(FakeRegistry(handles, pending))


class Membership(Base):

    def test_an_empty_world_composes_nothing(self):
        p = self.compose()
        self.assertEqual(p["rows"], [])
        self.assertEqual(p["cards"], [])
        self.assertIsNone(p["review"])
        self.assertEqual(p["errors"], {})
        self.assertEqual(p["tokens"], [])

    def test_a_working_session_is_informational(self):
        h = self.handle("j1", {"status": "running", "awaiting": None,
                               "pending": []})
        p = self.compose([h])
        [r] = p["rows"]
        self.assertEqual(r["kind"], "working")
        self.assertFalse(r["needs_you"])
        # a non-needs-you token carries no state, so mere progress can
        # never re-trigger the window — only the id appearing does
        self.assertTrue(r["trigger"].endswith("@"))
        self.assertEqual(p["counts"], {"needs_you": 0, "working": 1})

    def test_a_parked_reply_session_needs_you(self):
        h = self.handle("j1", {"status": "running", "awaiting": "reply"})
        [r] = self.compose([h])["rows"]
        self.assertEqual(r["kind"], "reply")
        self.assertTrue(r["needs_you"])
        self.assertEqual(r["trigger"], "session:j1@reply")

    def test_a_session_with_a_pending_card_renders_as_the_card_only(self):
        card = {"req_id": "rq1", "kind": "ask", "created": 5}
        h = self.handle("j1", {"status": "running", "awaiting": "ask",
                               "pending": [card]})
        p = self.compose([h], pending=[{"job_id": "j1", "card": card}])
        kinds = [r["kind"] for r in p["rows"]]
        self.assertEqual(kinds, ["card"])       # never twice
        self.assertEqual(len(p["cards"]), 1)
        self.assertEqual(p["cards"][0]["card"]["req_id"], "rq1")

    def test_a_resumable_dead_session_needs_you(self):
        h = self.handle("j1", {"status": "error", "session_id": "s1",
                               "finished": iso(0.1),
                               "error": "spend limit reached"})
        [r] = self.compose([h])["rows"]
        self.assertEqual(r["kind"], "died")
        self.assertTrue(r["needs_you"])
        self.assertIn("resumable", r["sub"])

    def test_a_session_the_owner_finished_makes_no_row(self):
        h = self.handle("j1", {"status": "error", "session_id": "s1",
                               "finished": iso(0.1),
                               "finished_by_owner": True})
        self.assertEqual(self.compose([h])["rows"], [])

    def test_an_old_dead_session_ages_out(self):
        h = self.handle("j1", {"status": "error", "session_id": "s1",
                               "finished": iso(3.0)})
        self.assertEqual(self.compose([h])["rows"], [])

    def test_a_machine_dead_session_makes_no_row(self):
        # a routine's failed dispatch is the health watcher's business, not
        # a conversation the owner was having
        h = self.handle("j1", {"status": "error", "session_id": "s1",
                               "finished": iso(0.1)},
                        spec={"meta": {"routine_id": "muse"}})
        self.assertEqual(self.compose([h])["rows"], [])

    def test_a_circuit_stage_session_defers_to_the_flow_row(self):
        from server import circuits
        h = self.handle("j1", {"status": "running", "awaiting": None},
                        spec={"meta": {"circuit_run": "run_x"}})
        circuits.list_runs.return_value = [{
            "id": "run_x", "status": "running", "circuit_name": "Plan it",
            "stages": {"plan": {"status": "running"},
                       "build": {"status": "pending"}}}]
        p = self.compose([h])
        kinds = [r["kind"] for r in p["rows"]]
        self.assertEqual(kinds, ["flow"])       # one dot, one row


class Flows(Base):

    def test_a_running_flow_reports_stage_progress(self):
        from server import circuits
        circuits.list_runs.return_value = [{
            "id": "run_1", "status": "running", "circuit_name": "PBJ",
            "stages": {"plan": {"status": "done"},
                       "build": {"status": "running"},
                       "judge": {"status": "pending"}}}]
        [r] = self.compose()["rows"]
        self.assertEqual(r["kind"], "flow")
        self.assertFalse(r["needs_you"])
        self.assertIn("stage 2 of 3", r["sub"])
        self.assertIn("build", r["sub"])
        self.assertEqual(r["stages_done"], 1)

    def test_a_finished_flow_makes_no_row(self):
        from server import circuits
        circuits.list_runs.return_value = [{"id": "r", "status": "done",
                                            "stages": {}}]
        self.assertEqual(self.compose()["rows"], [])

    def test_flow_rows_carry_the_stage_strip_in_topo_order(self):
        # stages_def is stored in AUTHORING order; the strip must read
        # left-to-right in execution order, judge stages marked, grades
        # carried — the exact fields the client's mini strip renders.
        from server import circuits
        circuits.list_runs.return_value = [{
            "id": "run_1", "status": "running", "circuit_name": "PBJ",
            "stages_def": [
                {"id": "judge", "name": "Judge", "mode": "judge",
                 "needs": ["build"], "judge": {"of": ["build"]}},
                {"id": "build", "name": "Build", "mode": "bypassPermissions",
                 "needs": ["plan"]},
                {"id": "plan", "name": "Plan", "mode": "manual", "needs": []},
            ],
            "stages": {"plan": {"status": "done"},
                       "build": {"status": "running"},
                       "judge": {"status": "pending", "grade": "B"}}}]
        [r] = self.compose()["rows"]
        strip = r["stages"]
        self.assertEqual([s["id"] for s in strip],
                         ["plan", "build", "judge"])
        self.assertEqual([s["status"] for s in strip],
                         ["done", "running", "pending"])
        self.assertEqual([s["judge"] for s in strip],
                         [False, False, True])
        self.assertEqual(strip[2]["grade"], "B")
        self.assertNotIn("grade", strip[0])
        self.assertEqual(strip[0]["name"], "Plan")

    def test_the_strip_never_joins_the_trigger_token(self):
        # A stage transition is progress; progress must not re-pop the
        # window. The token stays membership-only however the strip moves.
        from server import circuits
        run = {"id": "run_1", "status": "running", "circuit_name": "PBJ",
               "stages_def": [{"id": "plan", "mode": "manual", "needs": []}],
               "stages": {"plan": {"status": "pending"}}}
        circuits.list_runs.return_value = [run]
        before = self.compose()["rows"][0]["trigger"]
        run["stages"]["plan"]["status"] = "done"
        after = self.compose()["rows"][0]["trigger"]
        self.assertEqual(before, after)

    def test_a_legacy_run_without_stages_def_still_strips(self):
        # Runs stored before stages_def existed fall back to the stages
        # dict's own order — an honest strip, never a crash or an absence.
        from server import circuits
        circuits.list_runs.return_value = [{
            "id": "run_1", "status": "running", "circuit_name": "Old",
            "stages": {"a": {"status": "done"},
                       "b": {"status": "running"}}}]
        [r] = self.compose()["rows"]
        self.assertEqual([s["id"] for s in r["stages"]], ["a", "b"])
        self.assertEqual([s["judge"] for s in r["stages"]], [False, False])

    def test_a_legacy_stage_mode_spelling_still_reads_as_not_judge(self):
        # Stored defs outlive the 2026-07-29 rung rename; the judge flag
        # goes through norm_stage_mode, so "autopilot" is an agent stage.
        from server import circuits
        circuits.list_runs.return_value = [{
            "id": "run_1", "status": "running", "circuit_name": "Old",
            "stages_def": [{"id": "s", "mode": "autopilot", "needs": []}],
            "stages": {"s": {"status": "running"}}}]
        [r] = self.compose()["rows"]
        self.assertEqual(r["stages"][0]["judge"], False)


class Orphans(Base):

    def test_orphan_rows_need_attention(self):
        from server import orphanwork
        orphanwork.compose.return_value = {"items": [{
            "key": "k1", "branch": "claude/x", "kind": "dirty",
            "dirty": 3, "ahead": 1, "age_days": 2.0,
            "read": {"verdict": "land", "why": "clean"}}]}
        [r] = self.compose()["rows"]
        self.assertEqual(r["kind"], "orphan")
        self.assertTrue(r["needs_you"])
        self.assertIn("3 dirty files", r["sub"])
        self.assertIn("Vira: land", r["sub"])


class Health(Base):

    def test_ai_red_makes_a_row(self):
        from server import aihealth
        aihealth.summary.return_value = {"state": "red",
                                         "action": "run claude auth login"}
        [r] = self.compose()["rows"]
        self.assertEqual(r["id"], "health:ai")
        self.assertTrue(r["needs_you"])

    def test_boards_staleness_and_errors_make_rows(self):
        from server import jobboards
        jobboards.health.return_value = {
            "registered": 5, "fetched": iso(1.0),
            "errors": {"gh-acme": "HTTP 500", "wd-nv": "timeout"}}
        rows = {r["id"]: r for r in self.compose()["rows"]}
        self.assertIn("health:boards-stale", rows)
        self.assertIn("health:boards-errors", rows)
        self.assertIn("2 job boards failing", rows["health:boards-errors"]
                      ["title"])

    def test_a_fresh_sweep_is_quiet(self):
        from server import jobboards
        jobboards.health.return_value = {"registered": 5,
                                         "fetched": iso(0.01), "errors": {}}
        self.assertEqual(self.compose()["rows"], [])

    def test_health_rows_skip_on_passive(self):
        # a clone runs no watchers, so its health rows could only be false
        # alarms about the live machine's stores
        from server import aihealth
        aihealth.summary.return_value = {"state": "red", "action": "x"}
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            self.assertEqual(self.compose()["rows"], [])


class Review(Base):

    def _queue(self, age):
        return {"items": [{"age_days": age}], "total": 12,
                "counts": {"lessons": 12}}

    def test_a_fresh_backlog_is_a_note_not_a_row(self):
        from server import reviewqueue
        reviewqueue.items.return_value = self._queue(2.0)
        p = self.compose()
        self.assertEqual(p["rows"], [])
        self.assertEqual(p["review"]["total"], 12)

    def test_an_aging_backlog_escalates_into_a_row(self):
        from server import reviewqueue
        reviewqueue.items.return_value = self._queue(33.0)
        p = self.compose()
        [r] = p["rows"]
        self.assertEqual(r["kind"], "review")
        self.assertTrue(r["needs_you"])
        self.assertIn("12 decisions", r["title"])
        # weekly-bucketed trigger: re-announces once a week, not once a day
        self.assertEqual(r["trigger"], "review:aging@w4")


class Contract(Base):

    def test_a_broken_source_never_breaks_the_list(self):
        from server import orphanwork
        orphanwork.compose.side_effect = RuntimeError("store corrupt")
        h = self.handle("j1", {"status": "running", "awaiting": None})
        p = self.compose([h])
        self.assertEqual(len(p["rows"]), 1)     # the session survives
        self.assertIn("orphans", p["errors"])
        self.assertIn("store corrupt", p["errors"]["orphans"])

    def test_needs_you_rows_lead_the_sort(self):
        from server import orphanwork
        orphanwork.compose.return_value = {"items": [{
            "key": "k1", "branch": "claude/x", "kind": "unmerged",
            "ahead": 2, "age_days": 5.0}]}
        h = self.handle("j1", {"status": "running", "awaiting": None})
        p = self.compose([h])
        self.assertEqual([r["needs_you"] for r in p["rows"]],
                         [True, False])

    def test_tokens_mirror_the_rows(self):
        h = self.handle("j1", {"status": "running", "awaiting": "reply"})
        p = self.compose([h])
        self.assertEqual(p["tokens"], [r["trigger"] for r in p["rows"]])


if __name__ == "__main__":
    unittest.main()

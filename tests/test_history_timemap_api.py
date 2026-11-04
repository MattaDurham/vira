"""What the Time map front end added to the history API, and the two older
screens that now read the history store: list rows that carry their trail
(steps), a bare branch name as an id, "Sessions that broke a rule", the
window-to-module map, the "What is this?" change lines, and the Evidence
Ledger's commits and sessions. On the synthetic machine of
test_history_ingest.Sandbox; nothing reads the real store."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import evidence, lessonwatch, modulestory
from server import history as H
from server import historyroutes as R
from server import historyview as V
from tests.test_history_ingest import Sandbox


def _rule(rid, breaks, tier=2, dismissed=False):
    return {"id": rid, "text": f"Rule {rid}", "tier": tier, "count": len(breaks),
            "breaks": breaks, "dismissed": dismissed, "retired": False,
            "guard_did_not_hold": False, "proposal": None}


class TimemapApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(R.router)
        cls.app = app

    def setUp(self):
        self.sb = Sandbox(self)
        self.sb.build_all()
        self.sb.ingest()
        self.c = TestClient(self.app)

    def get(self, path, code=200):
        r = self.c.get(path)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def widget(self, out):
        rows = [e for g in out["groups"] for e in g["entries"]]
        return next(e for e in rows if e["title"].startswith("Widget"))

    # ------------------------------------------------------------ steps
    def test_list_rows_carry_their_trail_only_when_asked(self):
        plain = self.widget(self.get("/api/history?group=request"))
        self.assertNotIn("steps", plain)
        row = self.widget(self.get("/api/history?group=request&steps=1"))
        kinds = [(s["kind"], s["sub"]) for s in row["steps"]]
        self.assertIn(("ask", "journal"), kinds)
        self.assertIn(("session", "vira"), kinds)
        self.assertIn(("code", "merged"), kinds)
        # oldest first, the same order as the detail's trail
        detail = self.get(f"/api/history/entry?id={row['id']}")
        self.assertEqual([s["title"] for s in row["steps"]], [s["title"] for s in detail["trail"]])

    def test_a_long_trail_keeps_its_ends_and_says_what_was_left_out(self):
        idx = V.index()
        tid = self.widget(self.get("/api/history?group=request"))["id"]
        full = V.steps_of(idx, tid, cap=10 ** 6)
        short = V.steps_of(idx, tid, cap=4)
        self.assertEqual(len(short), 5)
        self.assertEqual(short[0], full[0])
        self.assertEqual(short[-1], full[-1])
        self.assertEqual(short[2]["type"], "more")
        self.assertEqual(short[2]["title"], f"{len(full) - 4} more steps")

    def test_a_module_history_lists_with_steps_and_merges_only(self):
        out = self.get("/api/history/for?module=work&steps=1")
        rows = [e for g in out["list"]["groups"] for e in g["entries"]]
        self.assertTrue(rows)
        self.assertTrue(all("steps" in e for e in rows if e["type"] == "trail"))
        merged = self.get("/api/history/for?module=work&kind=merged")
        mrows = [e for g in merged["list"]["groups"] for e in g["entries"]]
        self.assertTrue(mrows)
        self.assertTrue(all(e["kinds"].get("code") for e in mrows))
        self.assertLessEqual(len(mrows), len(rows))

    # ------------------------------------------------------------ ids
    def test_a_bare_branch_name_opens_its_entry(self):
        full = self.get("/api/history/entry?id=branch:vira:claude/widget")
        bare = self.get("/api/history/entry?id=branch:claude/widget")
        self.assertEqual(bare["entry"]["id"], full["entry"]["id"])
        self.get("/api/history/entry?id=branch:claude/never-was", 404)

    # ------------------------------------------------------------ rules
    def test_sessions_that_broke_a_rule_join_the_store_with_their_actions(self):
        report = {"rules": [
            _rule("lr_a", [{"id": "ev1", "day": "2026-11-03", "project": "vira",
                            "session_id": "j1", "quote": "skipped the test", "text": "x"}]),
            _rule("lr_none", []),
            _rule("lr_t1", [{"id": "ev2", "day": "2026-11-02", "session_id": "j1"}], tier=1, dismissed=True),
            _rule("lr_gone", [{"id": "ev3", "day": "2026-11-01", "session_id": "nobody"}]),
        ], "status": {"note": "summaries"}}
        with mock.patch.object(lessonwatch, "report", return_value=report):
            out = self.get("/api/history/rules")
        by = {r["id"]: r for r in out["items"]}
        self.assertNotIn("lr_none", by)            # nothing broken, nothing listed
        a = by["lr_a"]
        self.assertEqual(a["history_ids"], ["session:vira:j1"])
        self.assertEqual(a["sessions"][0]["id"], "session:vira:j1")
        self.assertEqual(a["trail"], V.index().trail_of["session:vira:j1"])
        acts = {x["action"]: x for x in a["actions"]}
        self.assertEqual(acts["guard"]["url"], "/api/lessons/lr_a/propose")
        self.assertEqual(acts["dismiss"]["body"], {"dismissed": True})
        nb = a["sessions"][0]["actions"][0]
        self.assertEqual(nb["body"], {"rule_id": "lr_a", "evidence_id": "ev1", "breaks": False})
        t1 = {x["action"]: x for x in by["lr_t1"]["actions"]}
        self.assertNotIn("guard", t1)              # a tier-1 rule already has its mechanism
        self.assertEqual(t1["dismiss"]["label"], "Resume proposing")
        self.assertEqual(t1["dismiss"]["body"], {"dismissed": False})
        # a break whose session the store never saw is still listed, with no entry to open
        self.assertEqual(by["lr_gone"]["sessions"], [])
        self.assertEqual(out["note"], "summaries")

    def test_a_break_read_from_a_retro_opens_that_retro(self):
        H.append([H.event("made:retro:2026-11-03 1400 vira", "made", "2026-11-03T19:00:00+00:00",
                          sub="retro", status=H.RAN, title="vira - 2026-11-03 14:00", project="vira")])
        report = {"rules": [_rule("lr_r", [{"id": "e", "day": "2026-11-03",
                                            "retro": "/x/Sessions/2026-11-03 1400 vira.md"}])]}
        with mock.patch.object(lessonwatch, "report", return_value=report):
            out = self.get("/api/history/rules")
        self.assertEqual(out["items"][0]["history_ids"], ["made:retro:2026-11-03 1400 vira"])

    # ------------------------------------------------------------ windows
    def test_each_window_names_its_module(self):
        w = self.get("/api/history/windows")["windows"]
        self.assertEqual(w["fleet"]["module"], "fleet")
        self.assertEqual(w["work"]["module"], "work")
        self.assertEqual(w["journal"]["module"], "attention")
        self.assertNotIn("nothing", w)


class StoreReaders(unittest.TestCase):
    """"What is this?" and the Evidence Ledger read the history store."""

    def setUp(self):
        self.sb = Sandbox(self)
        self.sb.build_all()
        self.sb.ingest()

    def test_the_story_change_lines_come_from_the_store_joined_by_module(self):
        days = modulestory.store_entries()
        rows = [r for rs in days.values() for r in rs]
        ships = [r for r in rows if r["kind"] == "ship"]
        self.assertTrue(ships)
        self.assertTrue(all(r["history_id"].startswith("code:merged:") for r in ships))
        widget = next(r for r in ships if "Widget" in r["text"])
        self.assertIn("work/ideas", widget["modules"])
        self.assertIn("(PR #7)", widget["text"])
        jobs = [r for r in rows if r["kind"] == "job"]
        self.assertEqual(jobs[0]["job_id"], "j1")
        inputs = {"docs": [], "films": {}, "groups": modulestory._with_store_entries([]),
                  "idea_tags": {}, "retro_tags": {}, "retro_docs": {},
                  "win_modules": {"work": "work", "people": "people"}}
        work = modulestory.story("work", inputs)
        hit = next(e for e in work["events"] if e.get("history_id") == widget["history_id"])
        self.assertTrue(hit["hit"] and hit["strong"])
        self.assertIn("module:work/ideas", hit["why"])
        people = modulestory.story("people", inputs)
        miss = next(e for e in people["events"] if e.get("history_id") == widget["history_id"])
        self.assertFalse(miss["strong"])           # another module's files never join strongly

    def test_an_empty_store_keeps_the_change_log_lines(self):
        groups = [{"date": "2026-08-01", "entries": [{"kind": "ship", "text": "old line"}], "retros": []}]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(H, "DIR", Path(d)):
            self.assertEqual(modulestory._with_store_entries(groups), groups)
        kept = modulestory._with_store_entries(groups)
        self.assertEqual(kept[-1]["date"], "2026-08-01")
        self.assertEqual(kept[-1]["entries"], [])  # the store's lines replace the log's

    def test_the_evidence_ledger_reads_commits_and_sessions_from_the_store(self):
        with mock.patch.object(evidence, "ROOT", self.sb.repo), \
                mock.patch("server.joblog.list_records", return_value=[
                    {"id": "j1", "prompt": "the whole prompt " * 100, "cwd": str(self.sb.repo)}]):
            commits = evidence._git_log()
            jobs = evidence._jobs(10_000)
        subjects = [c["subject"] for c in commits]
        self.assertIn("Merge branch 'claude/widget'", subjects)
        self.assertTrue(all(len(c["date"]) == 10 for c in commits))
        j = next(x for x in jobs if x["id"] == "j1")
        self.assertTrue(j["prompt"].startswith("the whole prompt"))
        self.assertGreater(len(j["prompt"]), 600)  # the ledger's prompt, not the store's head
        self.assertEqual(j["title"], "Widget: draw in half the time")


if __name__ == "__main__":
    unittest.main()

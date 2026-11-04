"""The history store: appends, newest line wins, compression, times,
titles, cost, the status words, and the transcript reader.

Every case writes into its own temporary folder (history.DIR pinned).
"""
import gzip
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from server import history as H
from server import historyingest as I
from server import historyview as V


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(H, "DIR", self.tmp / "history")
        p.start()
        self.addCleanup(p.stop)


class Store(TmpCase):
    def ev(self, eid, title="T", status=H.RAN, at=None):
        return H.event(eid, "run", at or time.time(), sub="flow", status=status, title=title)

    def test_put_writes_only_what_changed_and_newest_line_wins(self):
        self.assertEqual(H.put([self.ev("a"), self.ev("b")]), 2)
        self.assertEqual(H.put([self.ev("a"), self.ev("b")]), 0)
        self.assertEqual(H.put([self.ev("a", title="T2")]), 1)
        events = H.load()
        self.assertEqual(events["a"]["title"], "T2")
        lines = list(H.iter_lines())
        self.assertEqual(len(lines), 3, "the old line is kept, never rewritten")
        self.assertEqual([x["title"] for x in lines if x["id"] == "a"], ["T", "T2"])

    def test_signature_and_revision_move_on_append(self):
        self.assertEqual(H.revision(), "empty")
        H.put([self.ev("a")])
        r1 = H.revision()
        H.put([self.ev("b")])
        self.assertNotEqual(H.revision(), r1)

    def test_a_torn_last_line_is_skipped(self):
        H.put([self.ev("a")])
        seg = H._segments()[0]
        with open(seg, "a", encoding="utf-8") as fh:
            fh.write('{"id": "half')
        self.assertEqual(list(H.load()), ["a"])

    def test_compress_old_keeps_every_line(self):
        d = H.store_dir()
        d.mkdir(parents=True)
        for month in ("2026-07", "2026-08", "2026-09", "2026-10"):
            (d / f"events-{month}.jsonl").write_text(
                json.dumps(self.ev("id-" + month)) + "\n", encoding="utf-8")
        done = H.compress_old(keep_months=2)
        self.assertEqual(sorted(done), ["events-2026-07.jsonl.gz", "events-2026-08.jsonl.gz"])
        self.assertFalse((d / "events-2026-07.jsonl").exists())
        with gzip.open(d / "events-2026-07.jsonl.gz", "rt", encoding="utf-8") as fh:
            self.assertIn("id-2026-07", fh.read())
        self.assertEqual(sorted(H.load()), ["id-2026-07", "id-2026-08", "id-2026-09",
                                            "id-2026-10"])

    def test_event_refuses_unknown_kind_and_word(self):
        with self.assertRaises(ValueError):
            H.event("x", "thing", time.time())
        with self.assertRaises(ValueError):
            H.event("x", "run", time.time(), status="Done")

    def test_links_are_deduplicated_lists_and_empty_ones_dropped(self):
        e = H.event("x", "session", time.time(), links={"job": ["a", "a", None, ""],
                                                         "idea": None, "pr": "vira#3"})
        self.assertEqual(e["links"], {"job": ["a"], "pr": ["vira#3"]})

    def test_archive_lines_appends_and_never_raises(self):
        p = self.tmp / "x-archive.jsonl"
        self.assertEqual(H.archive_lines(p, [{"a": 1}, None, {"b": 2}]), 2)
        self.assertEqual(H.archive_lines(p, [{"c": 3}]), 1)
        self.assertEqual(len(p.read_text(encoding="utf-8").splitlines()), 3)
        self.assertEqual(H.archive_lines(self.tmp / "file" / "under" / "x.jsonl", []), 0)


class Times(unittest.TestCase):
    def test_to_ts_reads_every_store_shape(self):
        utc = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(H.to_ts("2026-11-04T16:00:00+00:00"), utc)
        self.assertEqual(H.to_ts("2026-11-04T16:00:00Z"), utc)
        self.assertEqual(H.to_ts(utc), utc)
        self.assertEqual(H.to_ts(utc * 1000), utc)
        self.assertEqual(H.to_ts(str(utc)), utc)
        naive = datetime(2026, 11, 4, 11, 0).timestamp()      # local wall time
        self.assertEqual(H.to_ts("2026-11-04T11:00:00"), naive)
        for missing in (None, "", 0, "garbage", -5):
            self.assertEqual(H.to_ts(missing), 0.0)

    def test_iso_is_utc_and_empty_for_nothing(self):
        self.assertTrue(H.iso(1793808558).endswith("+00:00"))
        self.assertEqual(H.iso(None), "")

    def test_local_day_is_the_local_calendar_day(self):
        ts = datetime(2026, 11, 4, 23, 30).timestamp()
        self.assertEqual(H.local_day(ts), "2026-11-04")


class Titles(unittest.TestCase):
    def test_clean_title_removes_ids_branches_and_hashes(self):
        t = H.clean_title("Merge claude/implement-x-8e0392 (5c688613-e224-4060-b055-d8e4a873ff13) "
                          "at 432bbd6a004a run_4f6131a9ff job_ab12cd34ef")
        for gone in ("claude/", "5c688613", "432bbd6", "run_4f6", "job_ab"):
            self.assertNotIn(gone, t)
        self.assertEqual(t, "Merge at")

    def test_clean_title_keeps_ordinary_words_and_numbers(self):
        self.assertEqual(H.clean_title("Fix 2026 calendar for PR #178"),
                         "Fix 2026 calendar for PR #178")
        self.assertEqual(H.clean_title("deadbeef cafe"), "deadbeef cafe",
                         "a word of hex letters with no digit is not a hash")

    def test_clean_title_caps_on_a_word(self):
        t = H.clean_title("word " * 60)
        self.assertLessEqual(len(t), H.TITLE_CAP + 3)
        self.assertTrue(t.endswith("..."))

    def test_first_sentence_reads_the_quoted_ask(self):
        self.assertEqual(H.first_sentence('You are Vira.\n"""Make it faster. Then test."""'),
                         "Make it faster.")
        self.assertEqual(H.first_sentence("Fix the grid\nand more"), "Fix the grid")

    def test_pick_title_order(self):
        self.assertEqual(H.pick_title("PR title", "Idea", "Flow", "App", "Ask."), "PR title")
        self.assertEqual(H.pick_title("", "Idea", "Flow", "App", "Ask."), "Idea")
        self.assertEqual(H.pick_title("", "", "Flow", "App", "Ask."), "Flow")
        self.assertEqual(H.pick_title("", "", "", "App", "Ask."), "App")
        self.assertEqual(H.pick_title("", "", "", "", "Ask. More."), "Ask.")
        self.assertEqual(H.pick_title("claude/only-a-branch", "Idea"), "Idea",
                         "a PR title that was only a branch name falls through")

    def test_automation_title_has_counts(self):
        self.assertEqual(H.automation_title("Capture each link", {"captured": 3, "failed": 0,
                                                                  "reused": 2}),
                         "Capture each link: 3 captured, 2 reused")
        self.assertEqual(H.automation_title("Export", {}, errors=2), "Export: 2 errors")
        self.assertEqual(H.automation_title("Export"), "Export")


class Cost(unittest.TestCase):
    def test_cost_of_a_known_model(self):
        totals = H.add_usage({}, "claude-opus-5-5", {
            "input_tokens": 1_000_000, "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 2_000_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000,
                               "ephemeral_5m_input_tokens": 1_000_000}})
        usd, tokens = H.cost_of(totals)
        # 4 in + 20 out + 0.20 read + 4*1.25 (5m) + 4*2 (1h)
        self.assertAlmostEqual(usd, 4 + 20 + 0.20 + 5 + 8)
        self.assertEqual(tokens, 5_000_000)

    def test_an_unpriced_model_gives_no_cost_not_a_partial_one(self):
        totals = H.add_usage({}, "claude-opus-5-5", {"input_tokens": 10})
        H.add_usage(totals, "gpt-5-codex", {"input_tokens": 10})
        self.assertEqual(H.cost_of(totals), (None, 20))

    def test_short_model_names_are_priced(self):
        self.assertEqual(H.price_of("opus-5-5"), H.price_of("claude-opus-5-5"))
        self.assertIsNone(H.price_of("gpt-5"))


class Modules(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(H._tree, {"at": time.time() + 1e9, "pats": [
            ("server/ideas.py", "work/ideas"), ("static/*.js", "core/shell"),
            ("tests/test_ideas*.py", "work/ideas")]})
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(H, "vira_repo", lambda: "/r/vira")
        q.start()
        self.addCleanup(q.stop)

    def test_modules_of_counts_and_picks_the_top(self):
        counts, top = H.modules_of(["server/ideas.py", "tests/test_ideas_x.py",
                                    "static/app.js", "README.md"], "/r/vira")
        self.assertEqual(counts, {"work/ideas": 2, "core/shell": 1})
        self.assertEqual(top, "work/ideas")

    def test_worktree_paths_are_made_relative(self):
        self.assertEqual(H.rel_path("/r/vira/.claude/worktrees/x-1/server/ideas.py"),
                         "server/ideas.py")
        self.assertEqual(H.rel_path("/r/vira/server/ideas.py", "/r/vira"), "server/ideas.py")
        counts, _ = H.modules_of(["/r/vira/.worktrees/y/static/app.js"], "/r/vira")
        self.assertEqual(counts, {"core/shell": 1})

    def test_other_repositories_have_no_module(self):
        self.assertEqual(H.modules_of(["src/main.ts"], "/r/tiksi"), ({}, ""))


def _ev(kind, sub, status, ts, **kw):
    e = H.event(kw.pop("id", f"{kind}:{sub}:{ts}"), kind, ts, sub=sub, status=status,
                title=kw.pop("title", ""), links=kw.pop("links", None),
                data=kw.pop("data", None))
    e["ts"], e["end_ts"] = ts, H.to_ts(e.get("end")) or ts
    return e


class StatusWords(unittest.TestCase):
    """settle(): one word for each place work can end up."""
    now = time.time()

    def test_running_wins(self):
        self.assertEqual(V.settle([_ev("session", "vira", H.RUNNING, self.now),
                                   _ev("code", "merged", H.MERGED, self.now - 60)]), H.RUNNING)

    def test_waiting_unless_a_merge_came_after(self):
        wait = _ev("session", "claude", H.WAITING, self.now - 100)
        self.assertEqual(V.settle([wait]), H.WAITING)
        self.assertEqual(V.settle([wait, _ev("code", "merged", H.MERGED, self.now)]), H.MERGED)

    def test_open_branch_is_not_merged(self):
        self.assertEqual(V.settle([_ev("session", "vira", H.NOT_MERGED, self.now - 10),
                                   _ev("code", "not_merged", H.NOT_MERGED, self.now)]),
                         H.NOT_MERGED)

    def test_discarded_branch_is_dropped(self):
        self.assertEqual(V.settle([_ev("session", "vira", H.NOT_MERGED, self.now - 10),
                                   _ev("code", "discarded", H.DROPPED, self.now)]), H.DROPPED)

    def test_dropped_idea(self):
        self.assertEqual(V.settle([_ev("ask", "idea", H.DROPPED, self.now)]), H.DROPPED)

    def test_answered_question(self):
        self.assertEqual(V.settle([_ev("ask", "chat", H.ANSWERED, self.now),
                                   _ev("answer", "chat", H.ANSWERED, self.now)]), H.ANSWERED)

    def test_automation_ran_or_failed(self):
        ok = _ev("run", "program", H.RAN, self.now)
        bad = _ev("run", "program", H.FAILED, self.now - 5, id="run:program:bad")
        self.assertEqual(V.settle([ok]), H.RAN)
        self.assertEqual(V.settle([ok, bad]), H.FAILED)

    def test_a_flow_step_session_keeps_the_automation_word(self):
        flow = _ev("run", "flow", H.RAN, self.now)
        step = _ev("session", "vira", H.RAN, self.now, links={"flow_run": "run_1"})
        self.assertEqual(V.trail_kind([flow, step]), "automation")
        self.assertEqual(V.settle([flow, step]), H.RAN)

    def test_failed_session(self):
        self.assertEqual(V.settle([_ev("session", "vira", H.FAILED, self.now)]), H.FAILED)

    def test_every_word_is_reachable_and_documented(self):
        self.assertEqual(set(H.STATUS_MEANS), set(H.STATUSES))
        self.assertEqual(len(H.STATUSES), 8)


def _line(ts, **kw):
    r = {"timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(), **kw}
    return json.dumps(r) + "\n"


class Transcript(TmpCase):
    def write(self, path, lines, mode="w"):
        with open(path, mode, encoding="utf-8") as fh:
            fh.write("".join(lines))

    def lines(self, t0):
        usage = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000}
        return [
            _line(t0, type="user", cwd="/w", sessionId="s1", entrypoint="cli", gitBranch="claude/x",
                  message={"role": "user", "content": "Build the thing. Then test it."}),
            # one API message written as two lines: its usage counts once
            _line(t0 + 10, type="assistant", message={"id": "m1", "model": "claude-opus-5-5",
                  "usage": usage, "content": [{"type": "text", "text": "ok"}]}),
            _line(t0 + 20, type="assistant", message={"id": "m1", "model": "claude-opus-5-5",
                  "usage": usage, "content": [{"type": "tool_use", "name": "Edit",
                                               "input": {"file_path": "/w/server/a.py"}}]}),
            # twenty idle minutes, then one more message
            _line(t0 + 20 + 1200, type="assistant", message={
                "id": "m2", "model": "claude-opus-5-5", "usage": usage, "content": []}),
        ]

    def test_totals_count_each_message_once_and_skip_idle_gaps(self):
        p = self.tmp / "t.jsonl"
        t0 = 1_790_000_000.0
        self.write(p, self.lines(t0))
        s = I.TStats()
        self.assertTrue(s.scan(str(p)))
        tot = s.totals()["claude-opus-5-5"]
        self.assertEqual(tot["output"], 200, "two messages, not three lines")
        self.assertEqual(s.active, 20.0, "the 20-minute gap is idle")
        self.assertEqual(s.prompt, "Build the thing. Then test it.")
        self.assertEqual(s.files, ["/w/server/a.py"])
        self.assertEqual((s.branch, s.cwd, s.entry, s.session), ("claude/x", "/w", "cli", "s1"))
        self.assertFalse(s.scan(str(p)), "nothing new, nothing read")

    def test_resumed_scan_matches_a_full_scan(self):
        t0 = 1_790_000_000.0
        lines = self.lines(t0)
        a, b = self.tmp / "a.jsonl", self.tmp / "b.jsonl"
        self.write(a, lines)
        full = I.TStats()
        full.scan(str(a))
        self.write(b, lines[:2])
        part = I.TStats()
        part.scan(str(b))
        resumed = I.TStats(part.to_dict())     # what the state file keeps
        # a line still being written is not read until it ends
        self.write(b, [lines[2].rstrip("\n")], mode="a")
        resumed.scan(str(b))
        self.write(b, ["\n"] + lines[3:], mode="a")
        resumed.scan(str(b))
        self.assertEqual(resumed.totals(), full.totals())
        self.assertEqual(resumed.active, full.active)
        self.assertEqual(resumed.files, full.files)


if __name__ == "__main__":
    unittest.main()

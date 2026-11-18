"""Session retros and their rollups, on a synthetic machine.

Sandbox builds everything in a temporary folder: transcripts in each tool's
own format, a Vira job folder, a retro folder, a private project. Every
source the module reads is repointed there (the history events, the retro
folder, the job folder, the store, the private roots) and the model is a
stub, so no case reads the real machine, writes the real retro folder or
spends a model call. test_nothing_outside_the_sandbox_is_named is the guard.

Times are built from local wall-clock values, so the day boundary cases
hold in any time zone.

Run: .venv/bin/python -m unittest tests.test_sessionretro
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from server import history as H
from server import sessionretro as SR


def ts(day, hhmm="12:00"):
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").astimezone().timestamp()


def claude_rows(*turns):
    """turns: ("user", text) | ("assistant", text) | ("edit", path) |
    ("bash", command, output)."""
    rows = []
    for t in turns:
        if t[0] == "user":
            rows.append({"type": "user", "message": {"role": "user", "content": t[1]}})
        elif t[0] == "assistant":
            rows.append({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "text", "text": t[1]}]}})
        elif t[0] == "edit":
            rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": t[1]}}]}})
        elif t[0] == "bash":
            rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": t[1]}}]}})
            rows.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": [{"type": "text", "text": t[2]}]}]}})
    return rows


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


GOOD = {"headline": "Fixed the export", "goal": "Fix the export.",
        "shipped": ["The export writes one row per order."],
        "decisions": ["Kept the old column order - other tools read it."],
        "reversals": ["First fix broke the header and was redone."],
        "open": ["The import side is untested."], "next": "Run the import test."}


class Sandbox(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.retros = self.root / "Sessions"
        self.retros.mkdir()
        self.private = self.root / "private-project"
        self.private.mkdir()
        self.events = {}
        self.prompts = []
        self.answer = dict(GOOD)
        self.now = ts("2026-11-10", "15:00")
        for name, value in (
                ("_retro_dir", lambda: self.retros),
                ("_events", lambda: self.events),
                ("_private_roots", lambda: [self.private]),
                ("_ask", self._ask),
                ("_budget", lambda: 100_000),
                ("_now", lambda: self.now),
                ("STORE", self.root / "store.json"),
                ("JOBS", self.root / "jobs")):
            p = mock.patch.object(SR, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _ask(self, prompt, model=None):
        self.prompts.append((prompt, model))
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.loads(json.dumps(self.answer))

    def session(self, key, sub="claude", start=None, end=None, folder=None,
                rows=None, status=H.ANSWERED, links=None, project="alpha"):
        start = start or ts("2026-11-10", "09:00")
        end = end or start + 1800
        folder = str(folder or self.root / project)
        tpath = ""
        if rows is not None:
            tpath = str(write_jsonl(self.root / "transcripts" / f"{key}.jsonl", rows))
        all_links = {"session": [key]}
        all_links.update(links or {})
        ev = H.event(f"session:{sub}:{key}", "session", start, end=end, sub=sub,
                     status=status, title=f"Work on {key}", project=project,
                     folder=folder, links=all_links,
                     data={"app": sub, "model": "model-x", "transcript": tpath})
        self.events[ev["id"]] = ev
        return ev

    def plain(self, key, **kw):
        return self.session(key, rows=claude_rows(
            ("user", "Please fix the export."), ("assistant", "Fixed.")), **kw)

    def files(self):
        return sorted(p.name for p in self.retros.glob("*.md"))


class Readers(Sandbox):
    def test_reads_a_claude_transcript(self):
        rows = claude_rows(
            ("user", "<system-reminder>ignore me</system-reminder>Fix the export."),
            ("edit", "/work/alpha/export.py"),
            ("bash", "git commit -m fix", "[main 1a2b3c4] fix\n 1 file changed"),
            ("assistant", "Done."))
        rows.append({"type": "assistant", "isSidechain": True, "message": {
            "role": "assistant", "content": [{"type": "text", "text": "subagent chatter"}]}})
        d = SR.read_claude(write_jsonl(self.root / "t.jsonl", rows))
        self.assertEqual([(t["role"], t["text"]) for t in d["turns"]],
                         [("user", "Fix the export."), ("assistant", "Done.")])
        self.assertEqual(d["files"], ["/work/alpha/export.py"])
        self.assertEqual(d["shas"], {"1a2b3c4"})
        self.assertEqual(d["tools"]["Bash"], 1)
        self.assertEqual(len(d["commands"]), 1)

    def test_reads_a_codex_transcript(self):
        rows = [
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "You are Codex."}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "Render the rooms."}]}}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
             "input": "*** Begin Patch\n*** Update File: scene/room.json\n*** End Patch"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output",
             "output": [{"type": "input_text", "text": "[render 9f8e7d6] rooms"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Rendered both rooms."}]}},
        ]
        path = write_jsonl(self.root / "c.jsonl", rows)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "x" * 5000}]}}) + "\n")
        with mock.patch.object(SR, "BIG_LINE", 2000):
            d = SR.read_codex(path)
        self.assertEqual([(t["role"], t["text"]) for t in d["turns"]],
                         [("user", "Render the rooms."), ("assistant", "Rendered both rooms.")])
        self.assertEqual(d["files"], ["scene/room.json"])
        self.assertEqual(d["shas"], {"9f8e7d6"})
        self.assertEqual(d["skipped_lines"], 1)

    def test_a_vira_session_with_no_known_transcript_is_read_from_its_job_folder(self):
        jdir = self.root / "jobs" / "job1"
        jdir.mkdir(parents=True)
        (jdir / "job.json").write_text(json.dumps({"prompt": "Count the rows."}), encoding="utf-8")
        (jdir / "state.json").write_text(json.dumps({"result_text": "There are 12 rows."}),
                                         encoding="utf-8")
        write_jsonl(jdir / "events.jsonl", [
            {"event": "message_completed", "phase": "thinking", "text": "hmm"},
            {"event": "tool_started", "receipt": {"name": "Read"}},
            {"event": "message_completed", "phase": "answer", "text": "Counting now."}])
        ev = self.session("s9", sub="vira", links={"job": ["job1"]})
        d = SR.digest(SR.describe(ev))
        self.assertEqual([t["text"] for t in d["turns"]],
                         ["Count the rows.", "Counting now.", "There are 12 rows."])
        self.assertEqual(d["tools"]["Read"], 1)

    def test_a_long_conversation_keeps_its_start_and_end_and_says_what_it_left_out(self):
        d = SR._blank_digest()
        d["turns"] = [{"role": "user", "text": "FIRST ask"}]
        for i in range(40):
            d["turns"].append({"role": "user", "text": f"middle {i} " + "m" * 400})
        d["turns"].append({"role": "assistant", "text": "LAST answer"})
        text, left_out = SR.conversation(d, 3_000)
        self.assertLessEqual(len(text), 3_100)
        self.assertIn("FIRST ask", text)
        self.assertIn("LAST answer", text)
        self.assertGreater(left_out, 0)
        self.assertIn(f"[{left_out} turns in the middle were left out to fit]", text)
        whole, none = SR.conversation(d, 1_000_000)
        self.assertEqual(none, 0)
        self.assertNotIn("left out", whole)


class WhichSessions(Sandbox):
    def test_every_tool_gets_a_retro(self):
        self.plain("c1", sub="claude", start=ts("2026-11-10", "09:00"))
        self.session("x1", sub="codex", start=ts("2026-11-10", "09:10"), rows=[
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Draw it."}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Drawn."}}])
        self.plain("v1", sub="vira", start=ts("2026-11-10", "09:20"))
        counts = SR.run_pass()
        self.assertEqual(counts["written"], 3)
        tools = set()
        for p in self.retros.glob("*.md"):
            fm = SR._front(p.read_text(encoding="utf-8"))
            self.assertEqual(fm["generated_by"], SR.GENERATED_BY)
            self.assertIn(fm["session_id"], ("c1", "x1", "v1"))
            tools.add(fm["tool"])
        self.assertEqual(tools, {"claude", "codex", "vira"})

    def test_a_session_still_in_use_waits(self):
        self.plain("recent", start=self.now - 3600, end=self.now - 1800)
        self.plain("running", status=H.RUNNING)
        self.assertEqual(SR.run_pass().get("written", 0), 0)
        self.assertEqual(self.files(), [])
        self.now += 2 * 3600                  # the recent one has now been quiet
        self.assertEqual(SR.run_pass()["written"], 1)

    def test_a_second_pass_writes_nothing_and_asks_nothing(self):
        self.plain("c1")
        SR.run_pass()
        asked = len(self.prompts)
        before = self.files()
        self.assertEqual(SR.run_pass().get("written", 0), 0)
        self.assertEqual(len(self.prompts), asked)
        self.assertEqual(self.files(), before)

    def test_a_session_that_resumes_is_rewritten_in_the_same_file(self):
        ev = self.plain("c1")
        SR.run_pass()
        first = self.files()
        later = ts("2026-11-10", "11:30")
        self.events[ev["id"]] = dict(ev, end=H.iso(later))
        self.answer = dict(GOOD, headline="Fixed the export and the import")
        counts = SR.run_pass()
        self.assertEqual(counts["rewritten"], 1)
        self.assertEqual(self.files(), first)
        text = (self.retros / first[0]).read_text(encoding="utf-8")
        self.assertIn("Fixed the export and the import", text)
        self.assertEqual(SR._front(text)["session_end"], H.iso(later))

    def test_a_retro_written_by_hand_is_never_replaced(self):
        self.plain("withid")
        hand1 = self.retros / "2026-11-10 0930 alpha.md"
        hand1.write_text("---\nsession_id: withid\n---\n\n# mine\n", encoding="utf-8")
        ev = self.plain("noid", project="beta", start=ts("2026-11-10", "10:00"))
        hand2 = self.retros / "2026-11-10 1020 beta.md"
        hand2.write_text(f"---\ndate: 2026-11-10\ntime: \"10:20\"\ncwd: {ev['folder']}\n---\n\n# mine\n",
                         encoding="utf-8")
        self.assertEqual(SR.run_pass().get("written", 0), 0)
        self.assertEqual(self.prompts, [])
        self.assertEqual(hand1.read_text(encoding="utf-8"), "---\nsession_id: withid\n---\n\n# mine\n")
        self.assertEqual(len(self.files()), 2)

    def test_a_hand_retro_for_another_time_does_not_cover_a_session(self):
        ev = self.plain("noid", project="beta", start=ts("2026-11-10", "10:00"))
        (self.retros / "2026-11-02 1020 beta.md").write_text(
            f"---\ndate: 2026-11-02\ntime: \"10:20\"\ncwd: {ev['folder']}\n---\n", encoding="utf-8")
        self.assertEqual(SR.run_pass()["written"], 1)

    def test_a_session_with_nothing_said_is_remembered_and_not_read_again(self):
        self.session("quiet", rows=claude_rows(("user", "hello?")))
        self.assertEqual(SR.run_pass()["empty"], 1)
        self.assertEqual(self.prompts, [])
        with mock.patch.object(SR, "digest", side_effect=AssertionError("read again")):
            self.assertEqual(SR.run_pass().get("empty", 0), 0)

    def test_a_failing_session_is_tried_three_times_then_left(self):
        self.plain("bad")
        self.answer = RuntimeError("model down")
        for _ in range(SR.MAX_ATTEMPTS):
            self.assertEqual(SR.run_pass()["failed"], 1)
        tried = len(self.prompts)
        self.assertEqual(SR.run_pass().get("failed", 0), 0)
        self.assertEqual(len(self.prompts), tried)
        self.assertEqual(SR.status()["failed_three_times"], 1)
        self.answer = dict(GOOD)              # and it is tried again once it changes
        ev = self.events["session:claude:bad"]
        self.events[ev["id"]] = dict(ev, end=H.iso(ts("2026-11-10", "12:00")))
        self.assertEqual(SR.run_pass()["written"], 1)

    def test_a_reply_that_is_not_json_is_asked_for_once_more(self):
        self.plain("c1")
        replies = [ValueError("not json"), dict(GOOD)]

        def ask(prompt, model=None):
            self.prompts.append((prompt, model))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(SR, "_ask", ask):
            self.assertEqual(SR.run_pass()["written"], 1)
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("could not be read as JSON", self.prompts[1][0])

    def test_a_missing_retro_folder_is_a_named_error_not_a_new_folder(self):
        gone = self.root / "nowhere"
        self.plain("c1")
        with mock.patch.object(SR, "_retro_dir", lambda: gone):
            self.assertIn("does not exist", SR.run_pass()["error"])
            self.assertIn("does not exist", SR.run_daily()["error"])
        self.assertFalse(gone.exists())

    def test_a_private_session_goes_to_its_own_folder_and_into_no_rollup(self):
        self.plain("secret", folder=self.private / "sub", project="private-project")
        self.plain("open1")
        SR.run_pass()
        own = list((self.private / "retros").glob("*.md"))
        self.assertEqual(len(own), 1)
        self.assertEqual(SR._front(own[0].read_text(encoding="utf-8"))["session_id"], "secret")
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(list(SR.session_files_by_day()["2026-11-10"]), ["alpha"])
        self.assertEqual(SR.run_pass().get("written", 0), 0)   # found again there


class WhatARetroSays(Sandbox):
    def test_a_line_naming_something_not_in_the_record_is_dropped_and_counted(self):
        self.session("c1", rows=claude_rows(
            ("user", "Fix the export."),
            ("edit", "/work/alpha/export.py"),
            ("bash", "git commit -m fix", "[main 1a2b3c4] fix"),
            ("assistant", "Fixed export.py.")))
        self.answer = dict(GOOD, shipped=[
            "Fixed export.py in commit 1a2b3c4.",
            "Rewrote billing/invoice.py.",
            "Landed commit 9d8c7b6.",
            "Opened pull request #412.",
            "The export writes one row per order."])
        SR.run_pass()
        text = (self.retros / self.files()[0]).read_text(encoding="utf-8")
        self.assertIn("Fixed export.py in commit 1a2b3c4.", text)
        self.assertIn("The export writes one row per order.", text)
        for invented in ("invoice.py", "9d8c7b6", "#412"):
            self.assertNotIn(invented, text)
        self.assertIn("not in the record: 3", text)

    def test_code_events_come_from_the_history_store(self):
        start = ts("2026-11-10", "09:00")
        self.plain("c1", start=start, links={"branch": ["alpha:claude/fix"]})
        sha = "abcdef1234567890abcdef1234567890abcdef12"
        for key, at, branch in (("in", start + 600, "alpha:claude/fix"),
                                ("late", start + 9 * 3600, "alpha:claude/fix"),
                                ("other", start + 600, "alpha:claude/else")):
            ev = H.event(f"code:commit:alpha:{key}", "code", at, sub="commit",
                         status=H.MERGED, title=f"Commit titled {key}", project="alpha",
                         links={"commit": [sha if key == "in" else key * 8], "branch": [branch]})
            self.events[ev["id"]] = ev
        self.answer = dict(GOOD, shipped=["Landed abcdef1."])
        SR.run_pass()
        text = (self.retros / self.files()[0]).read_text(encoding="utf-8")
        self.assertIn("Commit `abcdef1`: Commit titled in", text)
        self.assertNotIn("Commit titled late", text)
        self.assertNotIn("Commit titled other", text)
        self.assertIn("Landed abcdef1.", text)          # a hash the record holds stays
        self.assertIn("Commit titled in", self.prompts[0][0])

    def test_the_branch_a_transcript_was_written_on_ties_in_its_commits(self):
        start = ts("2026-11-10", "09:00")
        rows = claude_rows(("user", "Fix it."),
                           ("bash", "gh pr create", "https://github.com/someone/alpha/pull/77"),
                           ("assistant", "Fixed."))
        for r in rows:
            r["gitBranch"] = "claude/fix"
        self.session("c1", start=start, rows=rows)
        for key, branch, pr, project in (("mine", "alpha:claude/fix", None, "alpha"),
                                         ("bypr", "alpha:other", "alpha#77", "alpha"),
                                         ("elsewhere", "gamma:claude/fix", None, "gamma")):
            links = {"commit": [key * 8], "branch": [branch]}
            if pr:
                links["pr"] = [pr]
            ev = H.event(f"code:commit:{project}:{key}", "code", start + 600, sub="commit",
                         status=H.MERGED, title=f"Commit titled {key}", project=project,
                         links=links)
            self.events[ev["id"]] = ev
        self.answer = dict(GOOD, shipped=["Opened pull request #77."])
        SR.run_pass()
        text = (self.retros / self.files()[0]).read_text(encoding="utf-8")
        self.assertIn("Commit titled mine", text)
        self.assertIn("Commit titled bypr", text)
        self.assertNotIn("Commit titled elsewhere", text)
        self.assertIn("Opened pull request #77.", text)

    def test_a_codex_patch_names_only_the_file(self):
        row = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
               "input": 'tools.apply_patch("*** Begin Patch\\n*** Add File: build/a b.py\\n+x\\n*** End Patch")'}}
        d = SR.read_codex(write_jsonl(self.root / "p.jsonl", [row]))
        self.assertEqual(d["files"], ["build/a b.py"])

    def test_a_scratch_file_is_not_listed_as_a_change(self):
        d = SR.read_claude(write_jsonl(self.root / "s.jsonl", claude_rows(
            ("edit", "/tmp/x/scratchpad/notes.md"), ("edit", "/work/alpha/a.py"))))
        self.assertEqual(d["files"], ["/work/alpha/a.py"])

    def test_the_model_named_on_the_command_line_is_the_one_asked(self):
        self.plain("c1")
        SR.run_pass(model="small-one")
        self.assertEqual(self.prompts[0][1], "small-one")


class Rollups(Sandbox):
    def test_the_working_day_ends_at_four_in_the_morning(self):
        self.assertEqual(SR.work_day(ts("2026-11-11", "03:59")), "2026-11-10")
        self.assertEqual(SR.work_day(ts("2026-11-11", "04:00")), "2026-11-11")

    def seed_two_days(self):
        self.plain("a1", project="alpha", start=ts("2026-11-08", "10:00"))
        self.plain("a2", project="alpha", start=ts("2026-11-09", "01:00"))   # still the 8th
        self.plain("b1", project="beta", start=ts("2026-11-08", "11:00"))
        self.plain("a3", project="alpha", start=ts("2026-11-09", "10:00"))
        SR.run_pass()
        self.prompts.clear()
        self.answer = {"headline": "A day", "arc": "It went.", "shipped": ["One thing."],
                       "threads": ["export - open"], "reversals": [], "open": ["Import."],
                       "how": "Fine.", "patterns": ["Tests first."], "carried": ["alpha: import"]}

    def test_a_day_is_rolled_up_per_project_and_across_projects(self):
        self.seed_two_days()
        counts = SR.run_daily(day="2026-11-08")
        self.assertEqual(counts["day rollups"], 2)
        self.assertEqual(counts["cross-project"], 1)
        alpha = (self.retros / "2026-11-08 day alpha.md").read_text(encoding="utf-8")
        self.assertEqual(alpha.count("[["), 2)          # a1 and the after-midnight a2
        self.assertNotIn("2026-11-09 1000", alpha)
        cross = (self.retros / "2026-11-08 cross-project.md").read_text(encoding="utf-8")
        self.assertIn("[[2026-11-08 day alpha]]", cross)
        self.assertIn("[[2026-11-08 day beta]]", cross)
        # rollups are not sessions: a second look at the folder still finds 4
        self.assertEqual(sum(len(v) for d in SR.session_files_by_day().values()
                             for v in d.values()), 4)

    def test_rolling_a_day_up_twice_asks_the_model_once(self):
        self.seed_two_days()
        SR.run_daily(day="2026-11-08")
        asked = len(self.prompts)
        counts = SR.run_daily(day="2026-11-08")
        self.assertEqual(len(self.prompts), asked)
        self.assertEqual(counts["day rollups same"], 2)

    def test_a_day_file_another_writer_made_is_left_alone(self):
        self.seed_two_days()
        theirs = self.retros / "2026-11-08 day alpha.md"
        theirs.write_text("---\ngenerated_by: someone-else\n---\n\n# theirs\n", encoding="utf-8")
        counts = SR.run_daily(day="2026-11-08")
        self.assertEqual(counts["day rollups kept"], 1)
        self.assertIn("# theirs", theirs.read_text(encoding="utf-8"))

    def test_the_default_day_is_the_one_that_just_ended(self):
        self.seed_two_days()
        self.now = ts("2026-11-10", "04:15")
        SR.run_daily()
        self.assertTrue((self.retros / "2026-11-09 day alpha.md").exists())
        self.assertFalse((self.retros / "2026-11-08 day alpha.md").exists())
        SR.run_daily(since="2026-11-07")
        self.assertTrue((self.retros / "2026-11-08 day alpha.md").exists())

    def test_a_week_is_rolled_up_from_its_seven_days(self):
        self.seed_two_days()                   # the 8th is a Sunday, the 9th a Monday
        SR.run_daily(since="2026-11-01", now=ts("2026-11-15", "05:00"))
        self.prompts.clear()
        self.answer = {"headline": "A week", "summary": "It went.",
                       "by_project": [{"project": "alpha", "shipped": ["Export."], "open": []}],
                       "open_all_week": ["Import."], "patterns": [], "next_week": ["Import."]}
        self.now = ts("2026-11-15", "05:00")   # a Sunday morning
        self.assertEqual(SR.run_weekly(), {"week rollups": 1})
        week = (self.retros / "2026-11-14 week.md").read_text(encoding="utf-8")
        self.assertIn("2026-11-08 to 2026-11-14", week)
        self.assertIn("[[2026-11-09 cross-project]]", week)
        self.assertIn("### alpha", week)
        self.assertEqual(SR.run_weekly(), {"week rollups same": 1})
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(SR.run_weekly(week_ending="2026-10-31"),
                         {"weeks with no daily rollups": 1})


class Guard(Sandbox):
    def test_nothing_outside_the_sandbox_is_named(self):
        self.plain("c1")
        self.plain("p1", folder=self.private, project="private-project")
        SR.run_pass()
        SR.run_daily(day="2026-11-10")
        for d in SR._all_dirs() + [SR._store()]:
            self.assertTrue(str(d).startswith(str(self.root)), d)
        made = [p for p in self.root.rglob("*.md")]
        self.assertTrue(made)
        self.assertTrue(all(str(p).startswith(str(self.root)) for p in made))


if __name__ == "__main__":
    unittest.main()

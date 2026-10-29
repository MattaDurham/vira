"""Command routines: the detached runner, its history, and the launchd list.

Every case runs against throwaway stores and a throwaway runtime root, so
no case reads or writes the checkout's data/ or the machine's real locks.
The runner is driven in process (``routinerun.main``) through the spawn
seam, but the program it starts is a real child process.
"""
import os
import plistlib
import sys
import tempfile
import textwrap
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import (flows, instance, launchdjobs, routinerun, routines,
                    routinesrc, settings)

PROGRAM = textwrap.dedent('''\
    import os, sys
    watched = sys.argv[1]
    print("[demo 10:00:00] starting")
    print("[demo 10:00:01] FAILED chan/abc123 (try 1): timed out")
    with open(watched, "a", encoding="utf-8") as f:
        f.write("[demo 10:00:02] captured one\\n")
        f.write("[demo 10:00:03] done: 1 captured, 1 failed, 4 reused\\n")
    print("PATH=" + os.environ.get("PATH", ""))
    print("LEAK=" + os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
          + os.environ.get("VIRA_INSTANCE_ID", ""))
    sys.exit(int(sys.argv[2]))
''')


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.work = self.root / "work"
        self.work.mkdir()
        for p in (mock.patch.object(routinerun, "STORE",
                                    self.data / "routine-runs.json"),
                  mock.patch.object(routinerun, "RUNS",
                                    self.data / "routine-runs"),
                  mock.patch.object(routines, "STORE",
                                    self.data / "routines.json"),
                  mock.patch.object(routines, "SEEDS", []),
                  mock.patch.dict(os.environ, {
                      "VIRA_RUNTIME_ROOT": str(self.root / "runtime")})):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("VIRA_INSTANCE_ID", None)
        self.program = self.work / "demo.py"
        self.program.write_text(PROGRAM, encoding="utf-8")
        self.watched = self.work / "capture.log"
        self.watched.write_text("an older run's line\n", encoding="utf-8")
        self.legacy = self.work / "routine.log"

    def routine(self, code=0, **extra):
        row = {"id": "rt_demo", "name": "Demo capture", "kind": "command",
               "command": [sys.executable, str(self.program),
                           str(self.watched), str(code)],
               "cwd": str(self.work), "env": {"PATH": "/demo/bin"},
               "output_log": str(self.legacy),
               "watch_logs": [str(self.watched)], "every_hours": 1,
               "enabled": False, "instance_id": "primary"}
        row.update(extra)
        return row

    def inline(self):
        """Run the runner in process instead of detaching it."""
        def spawn(argv, log):
            routinerun.main(argv[-1])
            return types.SimpleNamespace(pid=os.getpid())
        return mock.patch.object(routinerun, "_spawn", side_effect=spawn)


class ResultTextTests(Base):
    """A finished run in words: what the wire carries into the card after a
    Loop card in a Flow."""

    def test_a_finished_run_in_words(self):
        with self.inline():
            rec = routinerun.start(self.routine(), trigger="schedule")
        text = routinerun.result_text(routinerun.get(rec["id"]))
        self.assertTrue(text.startswith(f"LOOP Demo capture: run {rec['id']} ok"), text)
        self.assertIn("Counts: 1 captured, 1 failed, 4 reused", text)
        self.assertIn("Errors (1):", text)
        self.assertIn("FAILED chan/abc123", text)
        self.assertIn("[demo 10:00:00] starting", text)
        self.assertIn("lines of the output:", text)

    def test_a_failed_run_says_so(self):
        with self.inline():
            rec = routinerun.start(self.routine(code=2), trigger="schedule")
        text = routinerun.result_text(routinerun.get(rec["id"]))
        self.assertIn(" failed (exit 2)", text)

    def test_a_run_with_no_log_still_reads(self):
        self.assertEqual(routinerun.result_text({"id": "nope", "status": "ok"}),
                         "LOOP loop: run nope ok")


class ParseTests(unittest.TestCase):
    def test_counts_come_from_the_last_done_line(self):
        counts, errors, total = routinerun.parse(
            "[youtube-subs 16:31:47] capturing 2\n"
            "[youtube-subs 16:35:36] done: 2 captured, 0 failed, "
            "0 left for later runs\n"
            "[youtube-routine 2026-10-26T16:35:36] done (exit 0)\n")
        self.assertEqual(counts, {"captured": 2, "failed": 0,
                                  "left for later runs": 0})
        self.assertEqual((errors, total), ([], 0))

    def test_error_lines(self):
        text = "\n".join([
            "[notes-capture 16:38:54]   FAILED https://x.example (day 1): bad",
            "Traceback (most recent call last):",
            "json.decoder.JSONDecodeError: Unterminated string",
            "notes-app-export: osascript failed: AppleEvent timed out",
            "[notes-routine 2026-10-26T16:38:54] done (capture exit 1)",
            "[notes-routine 2026-10-26T15:38:54] done (capture exit 0)",
            "[notes-capture 16:38:54] done: 0 captured, 0 failed, 23 reused",
        ])
        counts, errors, total = routinerun.parse(text)
        self.assertEqual(total, 5)
        self.assertEqual(counts, {"captured": 0, "failed": 0, "reused": 23})
        self.assertNotIn("capture exit 0", " ".join(errors))

    def test_errors_are_capped_but_counted(self):
        text = "\n".join(f"FAILED item {i}" for i in range(50))
        counts, errors, total = routinerun.parse(text)
        self.assertEqual(total, 50)
        self.assertEqual(len(errors), routinerun.MAX_ERRORS)


class RunTests(Base):
    def test_a_run_records_its_result_and_keeps_its_lines(self):
        with self.inline():
            rec = routinerun.start(self.routine(), trigger="manual")
        done = routinerun.get(rec["id"])
        self.assertEqual(done["status"], "ok")
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(done["trigger"], "manual")
        self.assertEqual(done["counts"],
                         {"captured": 1, "failed": 1, "reused": 4})
        self.assertEqual(done["error_count"], 1)
        self.assertIn("FAILED chan/abc123", done["errors"][0])
        self.assertTrue(done["started"] and done["finished"])
        files = {f["name"]: f for f in routinerun.log_files(rec["id"])}
        self.assertIn("starting", files["output.log"]["text"])
        watched = [f for f in files.values() if f["source"]]
        self.assertEqual(len(watched), 1)
        # only this run's lines, not the older run's
        self.assertIn("captured one", watched[0]["text"])
        self.assertNotIn("older run", watched[0]["text"])
        # the routine's own log keeps growing where it always did
        self.assertIn("starting", self.legacy.read_text(encoding="utf-8"))

    def test_environment_is_stripped_then_overridden(self):
        with self.inline(), mock.patch.dict(os.environ, {
                "CLAUDE_CODE_ENTRYPOINT": "sdk", "VIRA_INSTANCE_ID": "primary"}):
            rec = routinerun.start(self.routine())
        text = routinerun.log_files(rec["id"])[0]["text"]
        self.assertIn("PATH=/demo/bin", text)
        self.assertIn("LEAK=\n", text.replace("\r\n", "\n"))

    def test_a_nonzero_exit_is_a_failed_run(self):
        with self.inline():
            rec = routinerun.start(self.routine(code=3))
        done = routinerun.get(rec["id"])
        self.assertEqual((done["status"], done["exit_code"]), ("failed", 3))

    def test_a_missing_program_is_recorded_not_raised(self):
        row = self.routine(command=[str(self.work / "absent-program")])
        with self.inline():
            rec = routinerun.start(row)
        done = routinerun.get(rec["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("could not start", done["errors"][0])

    def test_a_held_lock_refuses_a_second_run(self):
        lease = instance.Lease(routinerun.lock_path("rt_demo"))
        self.assertTrue(lease.acquire())
        self.addCleanup(lease.close)
        self.assertTrue(routinerun.live("rt_demo"))
        with self.inline(), self.assertRaises(ValueError):
            routinerun.start(self.routine())
        self.assertEqual(routinerun.runs("rt_demo")["total"], 0)

    def test_a_runner_that_finds_the_lock_taken_skips(self):
        with mock.patch.object(routinerun, "_spawn",
                               return_value=types.SimpleNamespace(pid=1)):
            rec = routinerun.start(self.routine())
        lease = instance.Lease(routinerun.lock_path("rt_demo"))
        self.assertTrue(lease.acquire())
        self.addCleanup(lease.close)
        with mock.patch.object(routinerun, "LOCK_WAIT_S", 0):
            routinerun.main(routinerun.run_dir(rec["id"]))
        self.assertEqual(routinerun.get(rec["id"])["status"], "skipped")

    def test_settle_closes_a_dead_runner_and_spares_a_fresh_one(self):
        with mock.patch.object(routinerun, "_spawn",
                               return_value=types.SimpleNamespace(pid=1)):
            fresh = routinerun.start(self.routine())
        self.assertEqual(routinerun.settle("rt_demo"), 0)
        self.assertEqual(routinerun.get(fresh["id"])["status"], "starting")
        routinerun._update(fresh["id"], status="running")
        self.assertEqual(routinerun.settle("rt_demo"), 1)
        self.assertEqual(routinerun.get(fresh["id"])["status"], "interrupted")

    def test_settle_leaves_another_instances_runs(self):
        with mock.patch.object(routinerun, "_spawn",
                               return_value=types.SimpleNamespace(pid=1)):
            rec = routinerun.start(self.routine())
        routinerun._update(rec["id"], status="running", instance_id="b-x")
        self.assertEqual(routinerun.settle(), 0)
        self.assertEqual(routinerun.get(rec["id"])["status"], "running")

    def test_history_is_capped_and_says_so(self):
        with self.inline(), mock.patch.object(routinerun,
                                              "KEEP_PER_ROUTINE", 2):
            first = routinerun.start(self.routine())
            routinerun.start(self.routine())
            routinerun.start(self.routine())
        page = routinerun.runs("rt_demo")
        self.assertEqual((page["total"], page["pruned"]), (2, 1))
        self.assertNotIn(first["id"], [r["id"] for r in page["runs"]])
        self.assertFalse(routinerun.run_dir(first["id"]).exists())

    def test_run_ids_are_the_only_paths_served(self):
        with self.assertRaises(ValueError):
            routinerun.run_dir("../../etc")


class RoutineTests(Base):
    def test_save_validates_a_command_routine(self):
        with self.assertRaises(ValueError):
            routines.save_routine({"name": "x", "kind": "command",
                                   "every_hours": 1})
        with self.assertRaises(ValueError):
            routines.save_routine({"name": "x", "kind": "command",
                                   "every_hours": 1, "command": ["a"],
                                   "cwd": "relative/folder"})
        r = routines.save_routine({
            "name": "Demo", "kind": "command", "daily_at": "20:00",
            "command": "bash 'my script.sh' --limit 3",
            "cwd": str(self.work), "env": {"PATH": "/x"},
            "watch_logs": [str(self.watched)], "enabled": False})
        self.assertEqual(r["command"], ["bash", "my script.sh", "--limit", "3"])
        for is_win in (False, True):
            with mock.patch.object(settings, "IS_WIN", is_win):
                self.assertEqual(routines._split_command(
                    'bash "my script.sh" --limit 3'),
                    ["bash", "my script.sh", "--limit", "3"], is_win)
        with mock.patch.object(settings, "IS_WIN", True):
            self.assertEqual(routines._split_command(
                r'C:\tools\run.cmd --dir "C:\My Files"'),
                [r"C:\tools\run.cmd", "--dir", r"C:\My Files"])
        self.assertEqual(r["env"], {"PATH": "/x"})
        self.assertFalse(r["enabled"])

    def test_dispatch_starts_a_run_and_settle_stamps_it(self):
        row = routines.save_routine({k: v for k, v in self.routine().items()
                                     if k not in ("id", "instance_id")})
        with self.inline():
            out = routines.dispatch(row, trigger="schedule")
        self.assertIn("command_run", out)
        stamped = routines.get_routine(row["id"])
        self.assertEqual(stamped["last_status"], "running")
        self.assertEqual(routinerun.get(out["command_run"])["trigger"],
                         "schedule")
        routines.scheduler._settle(stamped)
        settled = routines.get_routine(row["id"])
        self.assertEqual(settled["last_status"], "ok")
        self.assertEqual(settled["last_summary"],
                         "1 captured, 1 failed, 4 reused")

    def test_only_the_owning_instance_may_run_it(self):
        row = self.routine(instance_id="primary")
        with mock.patch.dict(os.environ, {"VIRA_INSTANCE_ID": "branch-x"}), \
                self.inline(), self.assertRaises(ValueError):
            routines.dispatch(row)
        self.assertEqual(routinerun.runs("rt_demo")["total"], 0)

    def test_previous_live_reads_the_lock(self):
        row = self.routine()
        self.assertFalse(routines._previous_live(row))
        lease = instance.Lease(routinerun.lock_path("rt_demo"))
        lease.acquire()
        self.addCleanup(lease.close)
        self.assertTrue(routines._previous_live(row))

    def test_a_run_before_the_daily_gate_leaves_the_evening_run_due(self):
        now = datetime.now().astimezone().replace(hour=20, minute=5,
                                                  second=0, microsecond=0)
        r = {"enabled": True, "daily_at": "20:00",
             "last_run": now.replace(hour=17, minute=45).isoformat()}
        self.assertTrue(routines.is_due(r, now))
        r["last_run"] = now.replace(hour=20, minute=1).isoformat()
        self.assertFalse(routines.is_due(r, now))
        self.assertFalse(routines.is_due(r, now.replace(hour=23)))


class HoodTests(Base):
    def test_the_inspector_shows_the_program_and_its_runs(self):
        row = self.routine()
        with self.inline():
            routinerun.start(row)
        h = routinesrc.hood(row)
        self.assertEqual(h["engine"], "command")
        labels = {f["label"]: f["value"] for f in h["facts"]}
        self.assertEqual(labels["PATH"], "/demo/bin")
        self.assertEqual(labels["Folder"], str(self.work))
        self.assertIn("demo.py", labels["Command"])
        program = next(p for p in h["parts"] if p["id"] == "file:program")
        self.assertIn("done: 1 captured", program["text"])
        self.assertEqual(h["total"], 1)
        self.assertEqual(h["runs"][0]["status"], "ok")
        self.assertFalse(h["live"])

    def test_an_interpreter_or_binary_is_never_shown_as_the_program(self):
        # Windows CI: sys.executable is python.exe, which once came back
        # as "the program" read as text.
        for name in ("python.exe", "mytool"):
            binary = self.work / name
            binary.write_bytes(b"MZ\x90\x00\x03\x00binary")
            h = routinesrc.hood(self.routine(command=[str(binary),
                                                      str(self.program)]))
            program = next(p for p in h["parts"] if p["id"] == "file:program")
            self.assertEqual(program["path"], str(self.program), name)

    def test_an_inline_script_is_not_mistaken_for_a_file(self):
        row = self.routine(command=["/bin/bash", "-c", "echo hi\n" * 200])
        h = routinesrc.hood(row)
        self.assertNotIn("file:program", [p["id"] for p in h["parts"]])

    def test_a_loop_is_listed_under_the_hood_and_never_as_a_flow(self):
        # Standing loops have their own list in the Forge (over
        # /api/routines and /hood); the Flow list is circuits only.
        row = routines.save_routine({k: v for k, v in self.routine().items()
                                     if k not in ("id", "instance_id")})
        h = routinesrc.hood(row)
        self.assertEqual(h["engine"], "command")
        self.assertIn("demo.py", {f["label"]: f["value"] for f in h["facts"]}["Command"])
        with mock.patch.object(flows.circuits, "list_circuits", return_value=[]):
            self.assertEqual(flows.list_flows(), [])


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.user, self.daemons = root / "agents", root / "daemons"
        self.user.mkdir()
        self.daemons.mkdir()
        (self.user / "com.example.hourly.plist").write_bytes(plistlib.dumps({
            "Label": "com.example.hourly",
            "ProgramArguments": ["/opt/example/run.sh", "--all"],
            "StartInterval": 3600, "RunAtLoad": True}))
        (self.user / "com.example.nightly.plist").write_bytes(plistlib.dumps({
            "Label": "com.example.nightly", "Program": "/opt/example/night",
            "StartCalendarInterval": {"Hour": 20, "Minute": 0}},
            fmt=plistlib.FMT_BINARY))
        (self.user / "com.example.empty.plist").write_bytes(
            plistlib.dumps({}))
        (self.user / "notes.txt").write_text("not a job", encoding="utf-8")
        (self.daemons / "com.example.daemon.plist").write_bytes(
            plistlib.dumps({"Label": "com.example.daemon",
                            "ProgramArguments": ["/opt/example/d"],
                            "KeepAlive": True, "RunAtLoad": True}))
        outputs = {
            ("list",): "PID\tStatus\tLabel\n-\t0\tcom.example.hourly\n"
                       "77\t0\tcom.example.nightly\n",
            ("print", "system"): "\tservices = {\n\t\t     55      - \t"
                                 "com.example.daemon\n\t}\n",
        }
        for p in (mock.patch.object(launchdjobs, "_dirs", return_value=[
                    ("user", self.user, "Yours", "gui"),
                    ("missing", root / "nope", "Missing", "gui"),
                    ("daemons", self.daemons, "Daemons", "system")]),
                  mock.patch.object(launchdjobs, "_launchctl",
                                    side_effect=lambda *a: outputs.get(a, "")),
                  mock.patch.object(launchdjobs.settings, "IS_MAC", True),
                  mock.patch.object(launchdjobs.os, "getuid",
                                    return_value=501, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_every_file_is_listed_with_its_program_and_when(self):
        inv = launchdjobs.inventory()
        self.assertEqual(inv["status"], "ok")
        user = {j["file"]: j for j in inv["groups"][0]["jobs"]}
        self.assertEqual(len(user), 4)
        hourly = user["com.example.hourly.plist"]
        self.assertEqual(hourly["program"], "/opt/example/run.sh --all")
        self.assertEqual(hourly["when"], ["at login", "every hour"])
        self.assertEqual(hourly["tone"], "loaded")
        nightly = user["com.example.nightly.plist"]
        self.assertEqual(nightly["when"], ["daily at 20:00"])
        self.assertEqual(nightly["state"], "running (pid 77)")
        self.assertEqual(user["com.example.empty.plist"]["when"],
                         ["never: the file names no program"])
        self.assertIn("not a readable job file",
                      user["notes.txt"]["error"])
        self.assertEqual(inv["groups"][1]["jobs"], [])
        daemon = inv["groups"][2]["jobs"][0]
        self.assertEqual(daemon["when"][0], "at startup")
        self.assertEqual(daemon["state"], "running (pid 55)")

    def test_off_a_mac_it_says_so(self):
        with mock.patch.object(launchdjobs.settings, "IS_MAC", False):
            self.assertEqual(launchdjobs.inventory()["status"], "not_mac")


if __name__ == "__main__":
    unittest.main()

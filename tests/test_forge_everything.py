"""The Forge's one list and the controls behind each row (2026-10-29):
server/runlist.py, server/launchdjobs.py's writes, server/claudetasks.py
and server/adopt.py. Every source is patched to a temporary folder; no
test reads or changes this machine's launchd jobs, task files or stores."""
import os
import plistlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from server import (adopt, circuits, claudetasks, flows, launchdjobs, routinerun,
                    routines, runlist)


class Stores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = Path(self.tmp.name)
        self.agents = root / "LaunchAgents"
        self.global_agents = root / "GlobalAgents"
        self.daemons = root / "LaunchDaemons"
        for folder in (self.agents, self.global_agents, self.daemons):
            folder.mkdir()
        self.calls = []

        def fake_run(argv, admin=False):
            self.calls.append((list(argv), admin))
            return True, ""
        patches = [
            mock.patch.object(flows, "STORE", root / "flow-graphs.json"),
            mock.patch.object(circuits, "DEFS", root / "circuits.json"),
            mock.patch.object(circuits, "RUNS", root / "circuit-runs.json"),
            mock.patch.object(routines, "STORE", root / "routines.json"),
            mock.patch.object(routines, "SEEDS", []),
            mock.patch.object(routinerun, "STORE", root / "routine-runs.json"),
            mock.patch.object(routinerun, "RUNS", root / "routine-runs"),
            mock.patch.object(routinerun, "_spawn",
                              return_value=types.SimpleNamespace(pid=4242)),
            mock.patch.dict(os.environ, {"VIRA_RUNTIME_ROOT": str(root / "runtime")}),
            mock.patch.object(launchdjobs, "_dirs", return_value=[
                ("user", self.agents, "Your login agents", "gui"),
                ("agents", self.global_agents, "Login agents for every user", "gui"),
                ("daemons", self.daemons, "System daemons", "system")]),
            mock.patch.object(launchdjobs, "archive_dir", return_value=root / "LaunchAgents-archive"),
            mock.patch.object(launchdjobs, "_run", side_effect=fake_run),
            mock.patch.object(launchdjobs, "_gui_state", return_value={}),
            mock.patch.object(launchdjobs, "_system_state", return_value={}),
            mock.patch.object(launchdjobs, "_disabled", return_value=set()),
            mock.patch.object(claudetasks, "tasks_dir", return_value=root / "scheduled-tasks"),
            mock.patch.object(claudetasks, "archive_dir", return_value=root / "scheduled-tasks-archive"),
            # launchd is macOS only; the inventory says so elsewhere, and CI
            # runs these on Windows too, against the temporary folders above
            mock.patch.object(launchdjobs.settings, "IS_MAC", True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("VIRA_INSTANCE_ID", None)

    def job(self, name="com.example.capture", folder=None, **keys):
        data = {"Label": name, "ProgramArguments": ["/bin/bash", "/opt/capture.sh"],
                "WorkingDirectory": str(self.root), "Comment": "kept as it is", **keys}
        path = (folder or self.agents) / f"{name}.plist"
        path.write_bytes(plistlib.dumps(data))
        return path

    def task(self, name="nightly-ingest", body="Run the ingest.\n"):
        folder = self.root / "scheduled-tasks" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Nightly ingest (21:30)\n---\n\n{body}",
            encoding="utf-8")
        return folder


class LaunchdTests(Stores):
    def test_fields_change_only_the_keys_they_name_and_keep_a_backup(self):
        path = self.job(StartCalendarInterval={"Hour": 20, "Minute": 0})
        out = launchdjobs.save(str(path), fields={
            "command_line": "/bin/bash /opt/capture.sh --limit 30",
            "daily_at": ["20:00", "21:15"], "env": "PATH=/usr/bin"})
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["ProgramArguments"], ["/bin/bash", "/opt/capture.sh", "--limit", "30"])
        self.assertEqual(data["StartCalendarInterval"],
                         [{"Hour": 20, "Minute": 0}, {"Hour": 21, "Minute": 15}])
        self.assertEqual(data["EnvironmentVariables"], {"PATH": "/usr/bin"})
        self.assertEqual(data["Comment"], "kept as it is")
        self.assertEqual(out["fields"]["daily_at"], ["20:00", "21:15"])
        backups = list((self.root / "LaunchAgents-archive" / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(plistlib.loads(backups[0].read_bytes())["ProgramArguments"],
                         ["/bin/bash", "/opt/capture.sh"])

    def test_a_weekday_calendar_is_not_flattened_by_the_time_field(self):
        path = self.job(StartCalendarInterval={"Weekday": 1, "Hour": 9})
        with self.assertRaisesRegex(ValueError, "weekdays or dates"):
            launchdjobs.save(str(path), fields={"daily_at": ["09:00"]})
        self.assertEqual(plistlib.loads(path.read_bytes())["StartCalendarInterval"],
                         {"Weekday": 1, "Hour": 9})

    def test_the_whole_file_saves_and_a_broken_one_is_refused(self):
        path = self.job()
        with self.assertRaisesRegex(ValueError, "not a valid job file"):
            launchdjobs.save(str(path), xml="<plist><dict><key>Label</key>")
        data = plistlib.loads(path.read_bytes())
        data["RunAtLoad"] = True
        launchdjobs.save(str(path), xml=plistlib.dumps(data).decode("utf-8"))
        self.assertTrue(plistlib.loads(path.read_bytes())["RunAtLoad"])

    def test_a_loaded_job_is_reloaded_after_a_save(self):
        path = self.job()
        with mock.patch.object(launchdjobs, "_gui_state",
                               return_value={"com.example.capture": (None, "0")}):
            launchdjobs.save(str(path), fields={"cwd": "/tmp"})
        verbs = [argv[1] for argv, _ in self.calls if argv[0] == "launchctl"]
        self.assertEqual(verbs, ["bootout", "bootstrap"])

    def test_pause_disables_and_unloads_resume_enables_and_loads(self):
        path = self.job()
        launchdjobs.set_paused(str(path), True)
        launchdjobs.set_paused(str(path), False)
        verbs = [argv[1] for argv, _ in self.calls]
        self.assertEqual(verbs, ["disable", "bootout", "enable", "bootstrap"])
        self.assertTrue(self.calls[0][0][2].endswith("/com.example.capture"))

    def test_archive_moves_the_file_away_and_restore_puts_it_back(self):
        path = self.job()
        out = launchdjobs.archive(str(path))
        self.assertFalse(path.exists())
        self.assertTrue(out["archived"])
        self.assertEqual(out["origin"], str(path))
        rows = launchdjobs.archived_jobs()
        self.assertEqual([r["label"] for r in rows], ["com.example.capture"])
        self.assertIn(["launchctl", "bootout"], [argv[:2] for argv, _ in self.calls])
        back = launchdjobs.restore(out["path"])
        self.assertTrue(path.exists())
        self.assertEqual(back["path"], str(path))
        self.assertEqual(launchdjobs.archived_jobs(), [])

    def test_a_root_owned_job_goes_through_the_admin_prompt(self):
        path = self.job(folder=self.daemons)
        launchdjobs.set_paused(str(path), True)
        self.assertTrue(all(admin for _, admin in self.calls))
        self.assertEqual(self.calls[0][0][2], "system/com.example.capture")

    def test_a_file_outside_launchds_folders_is_refused(self):
        stray = self.root / "elsewhere.plist"
        stray.write_bytes(plistlib.dumps({"Label": "x"}))
        for call in (lambda: launchdjobs.detail(str(stray)),
                     lambda: launchdjobs.archive(str(stray)),
                     lambda: launchdjobs.save(str(stray), fields={})):
            with self.assertRaisesRegex(ValueError, "not a launchd job file"):
                call()


class TaskTests(Stores):
    def test_list_read_save_archive_restore(self):
        self.task()
        rows = claudetasks.list_tasks()
        self.assertEqual([(r["name"], r["description"], r["archived"]) for r in rows],
                         [("nightly-ingest", "Nightly ingest (21:30)", None)])
        task = claudetasks.read("nightly-ingest")
        self.assertEqual(task["body"], "Run the ingest.\n")
        claudetasks.save("nightly-ingest", task["text"] + "Then lint.\n")
        self.assertIn("Then lint.", claudetasks.read("nightly-ingest")["text"])
        self.assertEqual(len(list((self.root / "scheduled-tasks-archive" / "backups").iterdir())), 1)
        claudetasks.archive("nightly-ingest")
        self.assertFalse((self.root / "scheduled-tasks" / "nightly-ingest").exists())
        self.assertTrue(claudetasks.list_tasks()[0]["archived"])
        claudetasks.restore("nightly-ingest")
        self.assertTrue((self.root / "scheduled-tasks" / "nightly-ingest" / "SKILL.md").exists())

    def test_a_name_that_walks_out_of_the_folder_is_refused(self):
        with self.assertRaises(ValueError):
            claudetasks.read("../secrets")


class AdoptTests(Stores):
    def test_a_loop_card_becomes_a_program_card_that_keeps_the_loops_history(self):
        loop = routines.save_routine({"name": "Notes capture", "kind": "command",
                                      "every_hours": 1, "enabled": True,
                                      "command": ["/bin/bash", "/opt/notes.sh"],
                                      "cwd": str(self.root)})
        earlier = routinerun.start(loop, trigger="schedule")
        circuits.save_circuit({"id": "notes", "name": "Notes capture", "stages": [
            {"id": "loop", "name": "Notes capture", "mode": "native", "needs": [],
             "native": {"routine_id": loop["id"]}},
            {"id": "out", "name": "Out", "mode": "output", "needs": ["loop"],
             "output": {"destination": "record"}}]})
        routines.save_routine({"name": "Nightly", "kind": "circuit", "circuit_id": "notes",
                               "daily_at": "20:45", "enabled": True, "prompt": "report"})
        plan = adopt.adopt_command_routine(loop["id"], dry_run=True)
        self.assertEqual(plan["flows"][0]["cards"], ["loop"])
        self.assertIsNotNone(routines.get_routine(loop["id"]))
        adopt.adopt_command_routine(loop["id"])
        self.assertIsNone(routines.get_routine(loop["id"]))
        stage = next(s for s in circuits.get_circuit("notes")["stages"] if s["id"] == "loop")
        self.assertEqual(stage["mode"], "program")
        self.assertEqual(stage["program"]["run_key"], loop["id"])
        self.assertEqual(stage["program"]["command"], ["/bin/bash", "/opt/notes.sh"])
        self.assertEqual(routinerun.runs(loop["id"])["runs"][0]["id"], earlier["id"])
        # the loop's own hourly cadence is now a Schedule card wired to it,
        # created paused: on, it would also run the Output card after it
        self.assertEqual(plan["flows"][0]["cards_after"], ["out"])
        self.assertTrue(plan["flows"][0]["new_schedule_paused"])
        rows = [r for r in routines.list_routines() if r.get("circuit_id") == "notes"]
        hourly = next(r for r in rows if r.get("every_hours"))
        self.assertEqual(hourly["targets"], ["loop"])
        self.assertEqual(hourly["cwd"], str(self.root))
        self.assertFalse(hourly["enabled"])
        nightly = next(r for r in rows if r.get("daily_at"))
        self.assertEqual(nightly["targets"], ["loop"])

    def test_a_loop_no_flow_runs_becomes_its_own_flow(self):
        loop = routines.save_routine({"name": "Export", "kind": "command", "daily_at": "06:00",
                                      "enabled": False, "command": ["/usr/bin/true"]})
        report = adopt.adopt_command_routine(loop["id"])
        flow = flows.get_flow(report["flows"][0]["id"])
        kinds = sorted(n["type"] for n in flow["nodes"])
        self.assertEqual(kinds, ["input", "program", "trigger"])
        trigger = next(n for n in flow["nodes"] if n["type"] == "trigger")
        self.assertFalse(trigger["enabled"])
        self.assertEqual(trigger["daily_at"], "06:00")

    def test_a_launchd_job_on_a_calendar_moves_into_vira(self):
        path = self.job(StartCalendarInterval=[{"Hour": 20, "Minute": 0}, {"Hour": 8, "Minute": 30}],
                        StandardOutPath="/tmp/capture.log")
        out = adopt.adopt_launchd(str(path))
        self.assertFalse(path.exists())
        flow = out["flow"]
        program = next(n for n in flow["nodes"] if n["type"] == "program")
        self.assertEqual(program["program"]["command"], ["/bin/bash", "/opt/capture.sh"])
        self.assertEqual(program["program"]["output_log"], "/tmp/capture.log")
        times = sorted(n["daily_at"] for n in flow["nodes"] if n["type"] == "trigger")
        self.assertEqual(times, ["08:30", "20:00"])

    def test_a_service_launchd_keeps_running_stays_a_launchd_job(self):
        path = self.job(KeepAlive=True, RunAtLoad=True)
        with self.assertRaisesRegex(ValueError, "keeps this job running"):
            adopt.adopt_launchd(str(path))
        self.assertTrue(path.exists())

    def test_a_claude_task_becomes_a_scheduled_agent_card(self):
        self.task(body="Run the ingest end to end.\n")
        out = adopt.adopt_task("nightly-ingest", daily_at="21:30", cwd=str(self.root))
        agent = next(n for n in out["flow"]["nodes"] if n["type"] == "agent")
        self.assertEqual(agent["prompt"], "Run the ingest end to end.")
        self.assertEqual(agent["mode"], "acceptEdits")
        row = next(r for r in routines.list_routines() if r.get("circuit_id") == out["flow"]["id"])
        self.assertEqual((row["daily_at"], row["targets"], row["cwd"]),
                         ("21:30", ["task"], str(self.root)))
        self.assertTrue(claudetasks.list_tasks()[0]["archived"])


class EverythingTests(Stores):
    def test_one_list_with_the_same_fields_for_every_kind(self):
        circuits.save_circuit({"id": "cap", "name": "Capture", "stages": [
            {"id": "go", "name": "Go", "mode": "program", "needs": [],
             "program": {"command": ["/usr/bin/true"], "run_key": "prog_cap"}}]})
        routines.save_routine({"name": "Nightly", "kind": "circuit", "circuit_id": "cap",
                               "daily_at": "20:00", "enabled": True, "prompt": "x"})
        circuits.save_circuit({"id": "old", "name": "Old", "stages": [
            {"id": "go", "name": "Go", "mode": "output", "needs": []}]})
        flows.archive_flow("old")
        routines.save_routine({"name": "Sweep", "kind": "custom", "prompt": "x",
                               "every_hours": 1, "enabled": False})
        self.job(StartInterval=3600)
        self.task()
        live = (lambda: {"sessions": [
            {"id": "local_1", "tool": "claude", "status": "Working", "title": "Building",
             "folder": "/tmp", "openUrl": "claude://x", "viraJob": None, "startedAt": 1e12},
            {"id": "local_2", "tool": "claude", "status": "Idle", "title": "Resting"}]})
        with mock.patch.object(runlist, "_fleet_live", side_effect=live):
            out = runlist.everything()
        rows = {r["id"]: r for r in out["rows"]}
        self.assertTrue(all(s["status"] == "ok" for s in out["sources"].values()), out["sources"])
        self.assertEqual(rows["flow:cap"]["state"], "scheduled")
        self.assertEqual(rows["flow:cap"]["cadence"], "Every day at 20:00")
        self.assertEqual(rows["flow:old"]["state"], "archived")
        sweep = next(r for r in out["rows"] if r["name"] == "Sweep")
        self.assertEqual((sweep["state"], sweep["origin"], sweep["cadence_kind"]),
                         ("paused", "mine", "hourly"))
        job = next(r for r in out["rows"] if r["kind"] == "launchd")
        self.assertEqual((job["who"], job["cadence_kind"], job["origin"]), ("launchd", "hourly", "mac"))
        task = next(r for r in out["rows"] if r["kind"] == "task")
        self.assertEqual((task["who"], task["state"]), ("Claude", "manual"))
        sessions = [r for r in out["rows"] if r["kind"] == "session"]
        self.assertEqual([s["name"] for s in sessions], ["Building"])
        keys = set(rows["flow:cap"])
        for row in out["rows"]:
            self.assertTrue({"id", "kind", "who", "name", "line", "state", "cadence",
                             "cadence_kind", "folder", "origin", "note", "ref"} <= set(row), row)
        self.assertTrue(keys)

    def test_a_source_that_fails_is_named_and_the_rest_still_loads(self):
        with mock.patch.object(launchdjobs, "inventory", side_effect=OSError("denied")):
            out = runlist.everything()
        self.assertEqual(out["sources"]["launchd"]["status"], "error")
        self.assertEqual(out["sources"]["flows"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()

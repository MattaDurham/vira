"""Restart queue and real request/session/worker joins; never restart a process."""
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import backup, circuits, restart, runtimework, session, update


class RestartTests(unittest.TestCase):
    def setUp(self):
        runtimework.freeze(False)
        self.addCleanup(runtimework.freeze, False)
        self.callbacks = []
        self.relaunch = mock.Mock()
        self.work = []
        self.queue = restart.Coordinator(lambda: list(self.work), self.relaunch,
                                         self.callbacks.append)
        patch = mock.patch.object(update, "supervisor", return_value=("launchd", "test.service"))
        patch.start()
        self.addCleanup(patch.stop)

    def tick(self):
        self.callbacks.pop(0)()

    def test_wait_drains_new_work_and_restarts_once(self):
        self.work = [{"id": "coding"}]
        self.queue.request()
        self.tick()
        self.relaunch.assert_not_called()
        self.work = [{"id": "arrived-later"}]
        self.tick()
        self.relaunch.assert_not_called()
        self.work.clear()
        callback = self.callbacks[0]
        self.tick()
        callback()
        self.relaunch.assert_called_once()
        with self.assertRaises(runtimework.Restarting):
            with runtimework.activity("New work"):
                self.fail("new work was admitted after drain")

    def test_cancel_invalidates_timer_even_after_requeue(self):
        self.queue.request()
        old = self.callbacks.pop()
        self.queue.cancel()
        self.queue.request()
        old()
        self.relaunch.assert_not_called()
        self.tick()
        self.relaunch.assert_called_once()

    def test_immediate_is_delayed_but_does_not_wait(self):
        self.work = [{"id": "active"}]
        self.queue.request("now")
        self.relaunch.assert_not_called()
        self.tick()
        self.relaunch.assert_called_once()
        with self.assertRaises(ValueError):
            self.queue.cancel()

    def test_duplicate_requests_are_refused(self):
        self.queue.request()
        with self.assertRaises(ValueError):
            self.queue.request("now")
        self.assertEqual(len(self.callbacks), 1)

    def test_no_supervisor_never_schedules_or_exits(self):
        with mock.patch.object(update, "supervisor", return_value=("task", "")):
            self.assertFalse(self.queue.status()["available"])
            with self.assertRaises(ValueError):
                self.queue.request("now")
        self.assertEqual(self.callbacks, [])
        self.relaunch.assert_not_called()

    def test_disappearing_supervisor_cancels_without_exit(self):
        self.queue.request()
        with mock.patch.object(update, "supervisor", return_value=("task", "")):
            self.tick()
        self.relaunch.assert_not_called()
        self.assertEqual(self.queue.phase, "failed")
        with runtimework.activity("Still usable"):
            pass

    def test_failed_activity_check_never_means_idle(self):
        self.queue.read = mock.Mock(side_effect=OSError("unreadable"))
        # Request's status itself also reads, so use a successful initial view.
        self.queue.read.side_effect = [[], OSError("unreadable")]
        self.queue.request()
        self.tick()
        self.relaunch.assert_not_called()
        self.assertEqual(self.queue.phase, "failed")

    def test_update_waits_before_pull_and_failed_deps_never_restart(self):
        self.work = [{"id": "session"}]
        with mock.patch.object(update, "pull", side_effect=update.DepsError("dependency failure")) as pull:
            self.queue.request(operation="update")
            self.tick()
            pull.assert_not_called()
            self.work.clear()
            self.tick()
        self.relaunch.assert_not_called()
        self.assertEqual(self.queue.phase, "failed")
        self.assertIn("dependency failure", self.queue.error)
        with runtimework.activity("Retry allowed"):
            pass

    def test_update_success_restarts_and_no_update_stays_online(self):
        for updated in (False, True):
            with self.subTest(updated=updated), mock.patch.object(update, "pull", return_value={"updated": updated}):
                runtimework.freeze(False)
                queue = restart.Coordinator(lambda: [], self.relaunch, self.callbacks.append)
                queue.request(operation="update")
                self.tick()
                self.assertEqual(queue.phase, "updating" if updated else "idle")
        self.relaunch.assert_called_once()

    def test_http_routes_queue_cancel_and_update_share_coordinator(self):
        from server import main
        with mock.patch.object(restart, "coordinator", self.queue):
            client = TestClient(main.app)
            response = client.post("/api/restart", json={"mode": "when_idle"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["phase"], "waiting")
            self.assertEqual(client.post("/api/restart", json={}).status_code, 409)
            self.assertEqual(client.post("/api/restart/cancel").json()["phase"], "idle")
            response = client.post("/api/update/apply", json={})
            self.assertEqual(response.json()["operation"], "update")
            self.assertEqual(client.get("/api/restart").json()["boot_id"], self.queue.boot_id)
            self.assertEqual(client.post("/api/restart", json={"mode": "invalid"}).status_code, 409)
        self.relaunch.assert_not_called()

    def test_wait_reads_real_session_registry_until_fresh_completion(self):
        from server import atlas
        registry = session.Sessions()
        handle = session.DetachedJob("example", ".", {"id": "example", "subject": "Example build"})
        registry.sessions[handle.id] = handle
        state = {"status": "running"}
        queue = restart.Coordinator(restart.activities, self.relaunch, self.callbacks.append)
        with mock.patch.object(session, "sessions", registry), \
             mock.patch.object(session.jobfiles, "read_json", side_effect=lambda *args: dict(state)), \
             mock.patch.object(circuits, "restart_activity", return_value=[]), \
             mock.patch.object(atlas._building, "is_set", return_value=False):
            queue.request()
            self.tick()
            self.relaunch.assert_not_called()
            state["status"] = "done"
            self.tick()
        self.relaunch.assert_called_once()


class ActivityTests(unittest.TestCase):
    def tearDown(self):
        runtimework.freeze(False)

    def test_actual_http_handler_is_visible_and_cleanup_survives_errors(self):
        app = FastAPI()
        app.add_middleware(runtimework.Middleware)

        @app.post("/api/save")
        def save():
            return {"active": runtimework.snapshot()}

        client = TestClient(app)
        response = client.post("/api/save")
        self.assertEqual(response.json()["active"][0]["title"], "Saving app changes")
        self.assertEqual(runtimework.snapshot(), [])
        runtimework.freeze(True)
        self.assertEqual(client.post("/api/save").status_code, 503)

    def test_periodic_worker_resumes_if_update_thaws(self):
        completed = threading.Event()

        @runtimework.tracked("Example worker")
        def work():
            completed.set()

        runtimework.freeze(True)
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            self.assertFalse(completed.wait(0.03))
        finally:
            runtimework.freeze(False)
        self.assertTrue(completed.wait(1))
        worker.join(timeout=1)
        self.assertEqual(runtimework.snapshot(), [])

    def test_backup_pass_is_visible_during_real_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "example.json").write_text("{}", encoding="utf-8")
            dest = root / "backups"
            real_copy = shutil.copy2
            seen = []

            def copy(src, dst):
                seen.extend(runtimework.snapshot())
                return real_copy(src, dst)

            with mock.patch.multiple(backup, DATA=root, DEST=dest, FILES=("example.json",), DIRS=()), \
                 mock.patch.object(backup.shutil, "copy2", side_effect=copy):
                backup.snapshot()
            self.assertTrue(any(row["title"] == "Local backup" for row in seen))
            self.assertEqual(runtimework.snapshot(), [])

    def test_sessions_use_fresh_state_and_distinguish_detached_and_legacy(self):
        registry = session.Sessions()
        spec = {"id": "detached", "subject": "Example build", "prompt": "Synthetic task"}
        handle = session.DetachedJob("detached", ".", spec)
        handle.last_state = {"status": "done"}
        registry.sessions = {"detached": handle,
                             "legacy": session.Session({"id": "legacy", "subject": "Other build", "status": "running"})}
        with mock.patch.object(session.jobfiles, "read_json", return_value={"status": "running", "awaiting": "permission"}):
            rows = registry.restart_activity()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["survives"])
        self.assertIn("permission", rows[0]["detail"])
        self.assertFalse(rows[1]["survives"])
        with mock.patch.object(session.jobfiles, "read_json", return_value={"status": "done"}):
            self.assertEqual(len(registry.restart_activity()), 1)

    def test_all_owned_flows_are_read_without_a_history_limit(self):
        runs = [{"id": str(i), "status": "running", "circuit_name": "Example flow", "instance_id": "primary"}
                for i in range(220)]
        runs.append({"id": "foreign", "status": "running", "instance_id": "other"})
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(circuits, "RUNS", Path(temp) / "runs.json"), \
             mock.patch.object(circuits, "_load_runs", return_value={"runs": runs}), \
             mock.patch.object(circuits.instance, "id", return_value="primary"):
            self.assertEqual(len(circuits.restart_activity()), 220)

    def test_unreadable_source_is_a_named_blocker(self):
        from server import atlas
        with mock.patch.object(session.sessions, "restart_activity", side_effect=OSError("unreadable")), \
             mock.patch.object(circuits, "restart_activity", return_value=[]), \
             mock.patch.object(atlas._building, "is_set", return_value=False):
            rows = restart.activities()
        self.assertTrue(any(row["kind"] == "unknown" and "sessions" in row["title"] for row in rows))

    def test_frontend_confirmation_queue_cancel_and_reconnect(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        result = subprocess.run([node, "tests/restart_ui.js"],
                                cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

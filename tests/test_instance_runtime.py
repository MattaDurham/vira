"""Parallel processes retain full capabilities and own only their execution."""
import asyncio
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from server import circuits, instance, jobfiles, joblog, routines, session

BRANCH = {"VIRA_INSTANCE_ID": "branch:example",
          "VIRA_INSTANCE_URL": "http://localhost:8391",
          "VIRA_SERVICE_LABEL": "vira.test.example"}


class RuntimeTests(unittest.TestCase):
    def test_child_context_routes_to_its_branch(self):
        with mock.patch.dict(os.environ, BRANCH):
            env = instance.child_env()
            self.assertEqual(env["VIRA_INSTANCE_URL"], "http://localhost:8391")
            self.assertEqual(instance.metadata()["kind"], "branch")
            self.assertEqual(instance.service_label(), "vira.test.example")

    def test_branch_without_api_origin_fails_explicitly(self):
        with mock.patch.dict(os.environ, {"VIRA_INSTANCE_ID": "branch:example"}, clear=True):
            with self.assertRaisesRegex(ValueError, "own.*URL"):
                instance.api_url()

    def test_history_does_not_claim_another_instances_execution(self):
        original = {"id": "old", "status": "running", "pid": os.getpid()}
        with mock.patch.dict(os.environ, BRANCH):
            view = instance.record_view(original)
        self.assertEqual(view["status"], "snapshot")
        self.assertEqual(view["source_status"], "running")
        self.assertEqual(original["status"], "running")

    def test_lease_excludes_another_process_then_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owner.lock"
            lease = instance.Lease(path)
            self.assertTrue(lease.acquire())
            script = ("from server.instance import Lease; import sys; "
                      "lease=Lease(sys.argv[1]); print(lease.acquire())")
            def probe():
                return subprocess.check_output([sys.executable, "-c", script, str(path)],
                                               text=True, encoding="utf-8").strip()
            try:
                self.assertEqual(probe(), "False")
            finally:
                lease.close()
            self.assertEqual(probe(), "True")

    def test_busy_old_primary_is_not_mistaken_for_a_stopped_server(self):
        with mock.patch.dict(os.environ, BRANCH), \
             mock.patch.object(instance, "_legacy_until", 0), \
             mock.patch.object(instance, "urlopen", side_effect=URLError(TimeoutError())):
            self.assertTrue(instance._legacy_primary_running())
        with mock.patch.dict(os.environ, BRANCH), \
             mock.patch.object(instance, "_legacy_until", 0), \
             mock.patch.object(instance, "urlopen", side_effect=URLError(
                 ConnectionRefusedError(errno.ECONNREFUSED, "refused"))):
            self.assertFalse(instance._legacy_primary_running())

    def test_existing_primary_reclaims_shared_actions_from_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease = instance.Lease(Path(tmp) / "owner.lock")
            lease.acquire()
            with mock.patch.dict(os.environ, BRANCH), \
                 mock.patch.object(instance, "_coordinating", True), \
                 mock.patch.object(instance, "_lease", lease), \
                 mock.patch.object(instance, "_legacy_primary_running", return_value=True):
                self.assertFalse(instance.owns_automation())
            self.assertIsNone(lease.handle)

    def test_two_instances_cannot_overwrite_the_same_vault_revision(self):
        from server import vaultwrite
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault_root = root / "vault"
            vault_root.mkdir()
            (vault_root / "note.md").write_text("original", encoding="utf-8")
            script = '''import json, sys
from pathlib import Path
from unittest import mock
from server import vaultwrite
spec = {"id": "primary", "name": "Example", "primary": True,
        "root": Path(sys.argv[1]), "read_enabled": True, "write_enabled": True,
        "write_scope": "all", "protected_dirs": []}
with mock.patch.object(vaultwrite, "_index_source", side_effect=lambda s, r: r), mock.patch.object(vaultwrite, "_specs", return_value=[spec]):
    try:
        vaultwrite.write_note(spec, "note.md", sys.argv[2],
                              expected_hash=sys.argv[3], create_only=False)
        won = True
    except ValueError as exc:
        if "note changed" not in str(exc): raise
        won = False
print(json.dumps({"won": won, "lock": str(vaultwrite._lock_path(spec["root"] / "note.md"))}))
'''
            processes = []
            for number in (1, 2):
                env = dict(os.environ, VIRA_INSTANCE_ID=f"branch:{number}",
                           VIRA_PRIMARY_ROOT=str(root / "primary"),
                           VIRA_INSTANCE_URL=f"http://localhost:{8390 + number}")
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", script, str(vault_root), str(number),
                     vaultwrite.digest("original")], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8"))
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                results.append(json.loads(stdout))
            self.assertEqual(sum(row["won"] for row in results), 1)
            self.assertEqual(results[0]["lock"], results[1]["lock"])


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patch in (mock.patch.dict(os.environ, BRANCH),
                      mock.patch.object(jobfiles, "JOBS_DIR", self.root / "jobs"),
                      mock.patch.object(joblog, "STORE", self.root / "ledger.json"),
                      mock.patch.object(circuits, "RUNS", self.root / "flows.json"),
                      mock.patch.object(routines, "STORE", self.root / "routines.json"),
                      mock.patch.object(routines, "SEEDS", [])):
            patch.start()
            self.addCleanup(patch.stop)

    def test_supervisor_ignores_copied_live_pid_and_adopts_own_runner(self):
        for jid, owner in (("copied", "primary"), ("local", "branch:example")):
            directory = jobfiles.job_dir(jid)
            jobfiles.write_json_atomic(directory / "job.json", {"id": jid, "instance_id": owner})
            jobfiles.write_json_atomic(directory / "state.json", {
                "id": jid, "status": "running", "pid": os.getpid(), "heartbeat": 0})
        registry = session.Sessions()
        self.assertEqual(registry._boot_reattach(), ["local"])
        self.assertNotIn("copied", registry.sessions)
        self.assertEqual(jobfiles.read_json(jobfiles.job_dir("copied") / "state.json")["status"], "running")

    def test_ledger_sweep_preserves_foreign_history(self):
        jobfiles.write_json_atomic(joblog.STORE, {"jobs": [
            {"id": "copied", "status": "running", "finished": None},
            {"id": "local", "status": "running", "finished": None,
             "instance_id": "branch:example"}]})
        joblog.sweep_orphans()
        raw = jobfiles.read_json(joblog.STORE)["jobs"]
        self.assertEqual([r["status"] for r in raw], ["running", "orphaned"])
        self.assertEqual(joblog.list_records()[0]["status"], "snapshot")

    def test_copied_flow_cannot_dispatch_or_accept_a_stale_approval(self):
        run = {"id": "old", "status": "running", "stages_def": [
            {"id": "approval", "mode": "approval"}],
            "stages": {"approval": {"status": "waiting"}}}
        jobfiles.write_json_atomic(circuits.RUNS, {"runs": [run]})
        with mock.patch.object(session.sessions, "launch") as launch:
            circuits.Driver()._advance(run)
        launch.assert_not_called()
        with self.assertRaisesRegex(ValueError, "another instance"):
            circuits.decide_approval("old", "approval", True)
        self.assertEqual(jobfiles.read_json(circuits.RUNS)["runs"][0], run)

    def test_saved_branch_routine_executes_but_unedited_copy_does_not(self):
        original = {"id": "copied", "name": "Example", "kind": "custom",
                    "prompt": "Example", "enabled": True, "every_hours": 1,
                    "last_run": None, "last_job": "primary-job", "last_status": "running"}
        jobfiles.write_json_atomic(routines.STORE, {"routines": [original]})
        scheduler = routines.Scheduler()
        with mock.patch.object(routines, "dispatch") as dispatch:
            scheduler.tick()
            dispatch.assert_not_called()
            saved = routines.save_routine({"name": "Branch example"}, "copied")
            self.assertEqual(saved["instance_id"], "branch:example")
            self.assertIsNone(saved["last_job"])
            scheduler.tick()
            dispatch.assert_called_once()

    def test_every_branch_starts_supervision_flows_and_indexing(self):
        from server import main
        names = ("watcher", "mail_watcher", "whatsapp_watcher", "indexer", "text_indexer",
                 "idea_indexer", "doc_indexer", "doc_thumb_sweeper", "circle_watcher",
                 "media_archiver", "vault_indexer")
        patchers = [mock.patch.object(main, name) for name in names]
        others = [mock.patch.object(instance, "start_automation"),
                  mock.patch.object(main, "_start_account_workers"),
                  mock.patch.object(main.loopwatch.watcher, "start"),
                  mock.patch.object(session.sessions, "start_supervisor"),
                  mock.patch.object(main.photos, "start_background_build"),
                  mock.patch.object(main.backup, "start"),
                  mock.patch.object(circuits.driver, "start"),
                  mock.patch.object(routines.scheduler, "start"),
                  mock.patch.object(main.atlas, "GRAPH")]
        mocks = [p.start() for p in patchers + others]
        try:
            asyncio.run(main._startup())
            for worker in mocks[:len(names)]:
                worker.start.assert_called_once()
            session.sessions.start_supervisor.assert_called_once()
            circuits.driver.start.assert_called_once()
            routines.scheduler.start.assert_called_once()
        finally:
            for patch in reversed(patchers + others):
                patch.stop()

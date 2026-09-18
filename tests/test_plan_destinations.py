"""Plans keep their resolved source through saves, jobs, and continuation.

All content, indexes, registry entries, and process launches are synthetic.
"""
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from server import (circuits, jobfiles, joblog, plans, readinglist, session,
                    settings, vault, vaultwrite)


class DestinationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.research = self.base / "research"
        self.reflection = self.base / "reflection"
        self.home = self.base / "home"
        for path in (self.research, self.reflection, self.home):
            path.mkdir()
        self.cfg = {
            "vault_root": str(self.research), "vault_dirs": ["plans", "inbox"],
            "vault_sources": [
                {"id": "reflection", "name": "Reflection", "root": str(self.reflection),
                 "write_enabled": True, "capture_dir": "inbox/notes",
                 "write_dirs": ["inbox/notes"], "protected_dirs": ["canon"],
                 "contexts": ["self"]},
                {"id": "home", "name": "Home", "root": str(self.home),
                 "write_enabled": True, "capture_dir": "captures",
                 "write_dirs": ["captures"], "protected_dirs": ["evidence"],
                 "contexts": ["family"]},
            ],
            "vault_default_destination": "primary",
        }
        for patch in (
            mock.patch.object(settings, "get", side_effect=lambda key: self.cfg.get(key)),
            mock.patch.object(plans, "REG_PATH", self.base / "plans.json"),
            mock.patch.object(readinglist, "STORE", self.base / "reading.json"),
            mock.patch.object(vault, "DB_PATH", self.base / "index.sqlite"),
            mock.patch.object(vaultwrite, "LOCK_ROOT", self.base / "locks"),
            mock.patch.object(jobfiles, "JOBS_DIR", self.base / "jobs"),
            mock.patch.object(joblog, "STORE", self.base / "joblog.json"),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("VIRA_PASSIVE", None)


class PlanRoutingTests(DestinationCase):
    def test_context_destinations_do_not_copy_to_primary(self):
        a = plans.save_plan("# An idea\n\nReflection", context="self")
        b = plans.save_plan("# An idea\n\nFamily", context="family")
        self.assertEqual(a["source_id"], "reflection")
        self.assertEqual(Path(a["path"]).parent, self.reflection / "inbox/notes/plans")
        self.assertEqual(b["source_id"], "home")
        self.assertEqual(Path(b["path"]).parent, self.home / "captures/plans")
        self.assertEqual(list(self.research.rglob("*.md")), [])
        self.assertIn("Reflection", plans.get_plan(a["id"])["markdown"])
        self.assertIn("Family", plans.get_plan(b["id"])["markdown"])
        self.assertTrue(a["citation"].startswith("@reflection/"))

    def test_explicit_destination_wins_over_context_and_default(self):
        entry = plans.save_plan("# Selected", destination="home", context="self")
        self.assertEqual(entry["source_id"], "home")

    def test_unknown_readonly_disconnected_and_ambiguous_never_fall_back(self):
        self.cfg["vault_sources"][0]["write_enabled"] = False
        self.home.rmdir()
        for destination in ("missing", "reflection", "home"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                plans.save_plan("# Never route elsewhere", destination=destination)
        self.cfg["vault_default_destination"] = ""
        self.cfg["vault_sources"][0]["write_enabled"] = True
        with self.assertRaises(ValueError):
            plans.save_plan("# Ambiguous")
        self.assertEqual(list(self.research.rglob("*.md")), [])

    def test_concurrent_same_title_plans_keep_both_bodies(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            entries = list(pool.map(
                lambda body: plans.save_plan("# Same title\n\n" + body,
                                             destination="home"), ["One", "Two"]))
        self.assertNotEqual(entries[0]["path"], entries[1]["path"])
        self.assertIn("One", plans.get_plan(entries[0]["id"])["markdown"])
        self.assertIn("Two", plans.get_plan(entries[1]["id"])["markdown"])

    def test_reopening_is_bound_to_source_not_new_default(self):
        entry = plans.save_plan("# Saved here", destination="reflection")
        self.cfg["vault_default_destination"] = "home"
        self.assertIn("Saved here", plans.get_plan(entry["id"])["markdown"])
        self.cfg["vault_sources"] = self.cfg["vault_sources"][1:]
        self.assertTrue(plans.get_plan(entry["id"])["missing"])
        self.assertEqual(plans.get_plan(entry["id"])["markdown"], "")
        with self.assertRaises(ValueError):
            plans.delete_plan(entry["id"])
        self.assertTrue(Path(entry["path"]).is_file())

    def test_read_and_delete_follow_current_permissions(self):
        entry = plans.save_plan("# Permissions", destination="home")
        policy = self.cfg["vault_sources"][1]
        policy["write_enabled"] = False
        self.assertIn("Permissions", plans.get_plan(entry["id"])["markdown"])
        with self.assertRaises(ValueError):
            plans.delete_plan(entry["id"])
        policy["read_enabled"] = False
        self.assertEqual(plans.get_plan(entry["id"])["markdown"], "")

    def test_legacy_absolute_registry_path_cannot_escape_connected_sources(self):
        outside = self.base / "outside.md"
        outside.write_text("private outside", encoding="utf-8")
        plans._save({"plans": [{"id": "old", "title": "Untrusted", "path": str(outside)}]})
        self.assertEqual(plans.get_plan("old")["markdown"], "")
        with self.assertRaises(ValueError):
            plans.delete_plan("old")
        self.assertTrue(outside.is_file())

    def test_symlink_swap_blocks_reopen_and_deletion(self):
        entry = plans.save_plan("# Original", destination="home")
        target = Path(entry["path"])
        target.unlink()
        outside = self.base / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.assertEqual(plans.get_plan(entry["id"])["markdown"], "")
        with self.assertRaises(ValueError):
            plans.delete_plan(entry["id"])
        self.assertTrue(outside.exists())

    def test_passive_blocks_direct_save_and_delete(self):
        entry = plans.save_plan("# Keep", destination="home")
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(ValueError):
                plans.save_plan("# Block", destination="home")
            with self.assertRaises(ValueError):
                plans.delete_plan(entry["id"])
        self.assertTrue(Path(entry["path"]).exists())


class PlanPublicationTests(DestinationCase):
    def test_default_never_calls_hook_and_receipt_names_source(self):
        with mock.patch.object(session, "_publish_plan") as hook:
            receipt = session._finalize_plan("# Private plan", destination="home")
        hook.assert_not_called()
        self.assertTrue(receipt["plan_id"])
        self.assertEqual(receipt["source_id"], "home")
        self.assertTrue(receipt["path"].startswith("@home/"))

    def test_opt_in_hook_runs_only_after_successful_save(self):
        self.cfg["vault_sources"][1]["allow_publish"] = True
        def publish(md):
            self.assertEqual(len(plans.list_plans()), 1)
            return "https://example.invalid/plans/plan.html"
        with mock.patch.object(session, "_publish_plan", side_effect=publish) as hook:
            receipt = session._finalize_plan("# Approved copy", destination="home")
        hook.assert_called_once()
        self.assertEqual(plans.get_plan(receipt["plan_id"])["lab_url"], receipt["url"])

    def test_save_policy_failure_never_publishes(self):
        self.cfg["vault_sources"][1]["allow_publish"] = True
        self.cfg["vault_sources"][1]["protected_dirs"] = ["captures/plans"]
        with mock.patch.object(session, "_publish_plan") as hook:
            receipt = session._finalize_plan("# Restricted", destination="home")
        hook.assert_not_called()
        self.assertIsNone(receipt["plan_id"])
        self.assertIn("protected", receipt["error"])


class JobDestinationTests(DestinationCase):
    def launch(self, **kwargs):
        registry = session.Sessions()
        proc = mock.Mock(pid=999)
        with mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(session.subprocess, "Popen", return_value=proc), \
             mock.patch.object(session.agentbackend, "session_provider", return_value="anthropic"), \
             mock.patch.object(session.agentbackend, "sessions_quality", return_value=True), \
             mock.patch.object(session, "config", return_value={"cli_model": "test-model"}):
            jid = registry.launch("Make a plan", cwd=str(self.base), read_only=True,
                                  publish_plan=True, **kwargs)
        spec = jobfiles.read_json(jobfiles.job_dir(jid) / "job.json")
        return registry, jid, spec

    def test_real_launch_spec_ledger_snapshot_and_resume_keep_destination(self):
        registry, jid, spec = self.launch(vault_context="family")
        self.assertEqual(spec["vault_destination"], "home")
        self.assertEqual(joblog.get_record(jid)["vault_destination"], "home")
        self.assertEqual(registry.get(jid)["vault_destination"], "home")
        joblog.record_session(jid, "fake-conversation")
        joblog.record_finish(jid, "done")
        handle = registry.sessions[jid]
        handle.last_state.update({"status": "done", "session_id": "fake-conversation"})
        self.cfg["vault_default_destination"] = "primary"
        with mock.patch.object(registry, "launch", return_value="continued") as launch:
            registry.say(jid, "Continue")
        self.assertEqual(launch.call_args.kwargs["vault_destination"], "home")
        self.assertEqual(launch.call_args.kwargs["vault_context"], "family")

    def test_explicit_unavailable_fails_before_spawn(self):
        with mock.patch.object(session.Sessions, "_spawn_runner") as spawn:
            with self.assertRaises(ValueError):
                session.Sessions().launch("Save", vault_destination="unknown")
        spawn.assert_not_called()

    def test_default_is_pinned_even_if_changed_before_save(self):
        _, jid, spec = self.launch()
        self.cfg["vault_default_destination"] = "home"
        receipt = session._finalize_plan("# Started earlier", job_id=jid,
                                         destination=spec["vault_destination"])
        self.assertEqual(receipt["source_id"], "primary")
        joblog.record_plan(jid, receipt)
        self.assertEqual(joblog.get_record(jid)["plan"]["source_id"], "primary")

    def test_circuit_stores_context_route_before_stages_start(self):
        circ = {"id": "test", "name": "Test", "stages": [
            {"id": "write", "name": "Write", "mode": "manual", "needs": [],
             "prompt": "{{input}}", "publish_plan": True}]}
        with mock.patch.object(circuits, "get_circuit", return_value=circ), \
             mock.patch.object(circuits, "_mutate_runs"):
            run = circuits.start_run("test", "Plan", vault_context="self")
        self.assertEqual(run["vault_destination"], "reflection")
        self.assertEqual(run["vault_context"], "self")


if __name__ == "__main__":
    unittest.main()

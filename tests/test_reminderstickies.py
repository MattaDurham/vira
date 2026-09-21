"""Pins retain identity and geometry while canonical reminder state changes."""
import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import commitments, executive, reminderstickies as stickies


NOW = datetime(2030, 9, 10, 12, tzinfo=timezone.utc)
SUBJECT = "sender:email:synthetic"
LOOP = {"assistant_key": "agenda", "what": "Review the workshop agenda",
        "status": "open", "owed_by": "me", "source": "vira-assistant",
        "since": "2030-09-10", "due": "2030-09-12",
        "evidence": [{"id": "mail:synthetic", "channel": "email",
                      "when": NOW.isoformat(), "quote": "Review the workshop agenda."}]}


class ReminderStickiesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "data").mkdir()
        self.fixture = False
        self.sandbox = False
        self.crm_load = executive.crm._load
        self.cfg = dict(executive.DEFAULT_CONFIG, assistant_enabled=True, assistant_timezone="UTC")
        patches = [
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(stickies.settings, "ROOT", self.root),
            mock.patch.object(stickies, "STORE", self.root / "data" / "reminder-stickies.json"),
            mock.patch.object(executive, "STATE", self.root / "assistant.json"),
            mock.patch.object(commitments, "STORE", self.root / "commitments.json"),
            mock.patch.object(stickies.settings, "fixture_mode", side_effect=lambda: self.fixture),
            mock.patch.object(stickies.settings, "sandboxed", side_effect=lambda: self.sandbox),
            mock.patch.object(executive.settings, "raw", side_effect=lambda: self.cfg.copy()),
            mock.patch.object(executive.crm, "_load", return_value={"profiles": {}, "by_id": {}}),
            mock.patch.object(executive, "_now", return_value=NOW),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        os.environ.pop("VIRA_SANDBOX", None)
        self.write_commitment()
        self.rid = executive._key(SUBJECT, LOOP)
        app = FastAPI()
        app.include_router(stickies.router)
        # No application lifespan, workers, models, or real source stores.
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def write_commitment(self, **changes):
        loop = dict(copy.deepcopy(LOOP), **changes)
        commitments.STORE.write_text(json.dumps({"subjects": {SUBJECT: {
            "person_name": "Casey Example", "open_loops": [loop]}}}), encoding="utf-8")

    def pin(self, **geometry):
        response = self.client.post("/api/reminder-stickies", json={"reminder_id": self.rid, **geometry})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def snapshot(self):
        response = self.client.get("/api/reminder-stickies")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_pin_persists_identity_and_geometry_without_task_copy(self):
        self.pin(x=120, y=160, width=310, height=260)
        saved = json.loads(stickies.STORE.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"pins": {self.rid: {"x": 120, "y": 160, "width": 310, "height": 260}}})
        # A new HTTP client/read uses only the persisted layout, no memory cache.
        fresh = TestClient(self.client.app)
        self.addCleanup(fresh.close)
        item = fresh.get("/api/reminder-stickies").json()["items"][0]
        self.assertEqual(item["reminder"]["what"], LOOP["what"])
        self.assertEqual(item["x"], 120)

    def test_repeated_pin_is_idempotent_and_preserves_placement(self):
        self.pin(x=160, y=220)
        before = stickies.STORE.read_bytes()
        again = self.pin(x=500, y=600)
        self.assertEqual(again["x"], 160)
        self.assertEqual(stickies.STORE.read_bytes(), before)
        self.assertEqual(len(self.snapshot()["items"]), 1)

    def test_move_and_resize_survive_fresh_read(self):
        self.pin()
        response = self.client.put(f"/api/reminder-stickies/{self.rid}", json={"x": 230, "width": 420})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.snapshot()["items"][0]["x"], 230)
        self.assertEqual(self.snapshot()["items"][0]["width"], 420)
        self.assertEqual(self.snapshot()["items"][0]["height"], 250)

    def test_rejects_malformed_coordinates_without_changing_store(self):
        self.pin()
        before = stickies.STORE.read_bytes()
        cases = [{"x": -1}, {"x": True}, {"y": "10"}, {"width": 12}, {"height": 9999},
                 {"x": 20001}, {"html": "<script>bad()</script>"}, {"what": "Alter canonical task"}]
        for geometry in cases:
            with self.subTest(geometry=geometry):
                response = self.client.put(f"/api/reminder-stickies/{self.rid}", json=geometry)
                self.assertEqual(response.status_code, 400)
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ValueError):
                stickies.move(self.rid, {"x": number})
        self.assertEqual(stickies.STORE.read_bytes(), before)

    def test_rejects_noncanonical_and_unknown_identifiers(self):
        for rid, status in (("calendar:example", 400), ("../other", 400), ("A" * 24, 400), ("f" * 24, 404)):
            with self.subTest(rid=rid):
                self.assertEqual(self.client.post("/api/reminder-stickies", json={"reminder_id": rid}).status_code, status)
        self.assertFalse(stickies.STORE.exists())

    def test_canonical_edits_are_visible_without_changing_pin(self):
        self.pin()
        before = stickies.STORE.read_bytes()
        self.write_commitment(what="Review the revised workshop agenda", due="2030-09-15")
        item = self.snapshot()["items"][0]
        self.assertEqual(item["reminder"]["what"], "Review the revised workshop agenda")
        self.assertEqual(item["reminder"]["due"], "2030-09-15")
        self.assertEqual(stickies.STORE.read_bytes(), before)

    def test_existing_snooze_action_reflects_canonical_state(self):
        self.pin()
        result = executive.reminder_action(self.rid, "snooze", 24)
        self.assertEqual(result["status"], "snoozed")
        self.assertEqual(executive.reminders(), [])
        item = self.snapshot()["items"][0]
        self.assertEqual(item["state"], "snoozed")
        self.assertEqual(item["reminder"]["snoozed_until"], "2030-09-11T12:00:00+00:00")

    def test_existing_done_action_retains_visible_completed_pin(self):
        self.pin()
        executive.reminder_action(self.rid, "done")
        item = self.snapshot()["items"][0]
        self.assertEqual(item["state"], "closed")
        self.assertEqual(item["reminder"]["what"], LOOP["what"])
        self.assertNotIn(self.rid, self.snapshot()["eligible_ids"])
        self.assertEqual(self.client.post("/api/reminder-stickies", json={"reminder_id": self.rid}).status_code, 404)
        self.assertEqual(commitments.snapshot(SUBJECT)["open_loops"][0]["status"], "closed")

    def test_expired_snooze_returns_to_open(self):
        self.pin()
        executive.reminder_action(self.rid, "snooze", 1)
        with mock.patch.object(executive, "_now", return_value=datetime(2030, 9, 11, tzinfo=timezone.utc)):
            self.assertEqual(self.snapshot()["items"][0]["state"], "open")

    def test_missing_source_is_not_falsely_completed_or_deleted(self):
        self.pin()
        commitments.STORE.write_text('{"subjects": {}}', encoding="utf-8")
        item = self.snapshot()["items"][0]
        self.assertEqual(item["state"], "missing")
        self.assertIsNone(item["reminder"])
        self.assertIn(self.rid, json.loads(stickies.STORE.read_text(encoding="utf-8"))["pins"])

    def test_source_read_failure_preserves_pin_and_names_unavailable(self):
        self.pin()
        with mock.patch.object(stickies, "_sources", side_effect=ValueError("bad source")):
            data = self.snapshot()
        self.assertEqual(data["items"][0]["state"], "unavailable")
        self.assertTrue(data["error"])

    def test_unpin_is_idempotent_and_does_not_change_task(self):
        self.pin()
        before = commitments.STORE.read_bytes()
        for _ in range(2):
            self.assertEqual(self.client.delete(f"/api/reminder-stickies/{self.rid}").status_code, 200)
        self.assertEqual(self.snapshot()["items"], [])
        self.assertEqual(commitments.STORE.read_bytes(), before)


    def prepare_snapshot(self):
        (self.root / ".git").write_text("gitdir: ../primary/.git/worktrees/preview\n", encoding="utf-8")
        (self.root / "data" / ".test-snapshot").write_text("complete clone\n", encoding="utf-8")

    def test_real_snapshot_reads_canonical_reminders_and_only_changes_local_layout(self):
        self.prepare_snapshot()
        source = self.root / "external-crm"
        (source / "profiles").mkdir(parents=True)
        (source / "people.json").write_text(json.dumps({"people": [{
            "id": "p_example", "name": "Casey Example"}]}), encoding="utf-8")
        (source / "profiles" / "p_example.json").write_text(json.dumps({
            "open_loops": [dict(LOOP, assistant_key="contact-agenda")]}), encoding="utf-8")
        source_before = {p: p.read_bytes() for p in source.rglob("*.json")}
        commitments_before = commitments.STORE.read_bytes()
        with mock.patch.object(executive.crm, "_load", side_effect=self.crm_load), \
                mock.patch.object(executive.crm, "_cache", {"loaded_at": 0}), \
                mock.patch.object(executive.settings, "crm_root", return_value=source), \
                mock.patch("server.contactcard.added_handles", return_value={}):
            data = self.snapshot()
            self.assertEqual(len(data["eligible_ids"]), 2)
            self.assertFalse(data["read_only"])
            self.assertFalse(data["actions_read_only"])
            self.pin(x=240, y=180)
            self.assertEqual(self.snapshot()["items"][0]["reminder"]["what"], LOOP["what"])
            self.assertEqual(self.client.put(f"/api/reminder-stickies/{self.rid}", json={"x": 420}).status_code, 200)
            self.assertEqual(self.snapshot()["items"][0]["x"], 420)
            self.assertEqual(self.client.delete(f"/api/reminder-stickies/{self.rid}").status_code, 200)
        self.assertEqual({p: p.read_bytes() for p in source.rglob("*.json")}, source_before)
        self.assertEqual(commitments.STORE.read_bytes(), commitments_before)
        self.assertFalse(executive.STATE.exists())
        self.assertEqual(json.loads(stickies.STORE.read_text(encoding="utf-8")), {"pins": {}})

    def test_shared_snapshot_data_root_refuses_reads_and_layout_changes(self):
        self.prepare_snapshot()
        local = self.root / "data"
        shared = self.root / "shared-data"
        local.rename(shared)
        try:
            local.symlink_to(shared, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks are unavailable: {exc}")
        before = {p: p.read_bytes() for p in shared.iterdir()}
        with mock.patch.object(stickies, "isolated", return_value=True), \
                mock.patch.object(stickies, "_sources", side_effect=AssertionError("read shared sources")):
            self.assertTrue(self.snapshot()["read_only"])
            self.assertEqual(self.client.post("/api/reminder-stickies", json={"reminder_id": self.rid}).status_code, 403)
            self.assertEqual(self.client.put(f"/api/reminder-stickies/{self.rid}", json={"x": 100}).status_code, 403)
            self.assertEqual(self.client.delete(f"/api/reminder-stickies/{self.rid}").status_code, 403)
        self.assertEqual({p: p.read_bytes() for p in shared.iterdir()}, before)

    def test_linked_store_or_writer_sidecar_refuses_layout_changes(self):
        self.prepare_snapshot()
        shared = self.root / "shared.json"
        shared.write_text("owner content", encoding="utf-8")
        for suffix in ("", ".lock", ".tmp"):
            path = stickies.STORE.with_name(stickies.STORE.name + suffix)
            for kind in ("symlink", "hardlink"):
                with self.subTest(suffix=suffix, kind=kind):
                    try:
                        path.symlink_to(shared) if kind == "symlink" else os.link(shared, path)
                    except OSError as exc:
                        self.skipTest(f"Links are unavailable: {exc}")
                    try:
                        with mock.patch.object(stickies, "isolated", return_value=True), \
                                mock.patch.object(stickies, "_sources", side_effect=AssertionError("read sources")):
                            self.assertTrue(self.snapshot()["read_only"])
                            self.assertEqual(self.client.post("/api/reminder-stickies", json={"reminder_id": self.rid}).status_code, 403)
                        self.assertEqual(shared.read_text(encoding="utf-8"), "owner content")
                    finally:
                        path.unlink()

    def test_fixture_preview_can_test_layout_but_task_actions_are_read_only(self):
        self.prepare_snapshot()
        self.fixture = True
        before = commitments.STORE.read_bytes()
        with mock.patch.object(stickies, "isolated", return_value=True):
            self.pin(x=220)
            data = self.snapshot()
            self.assertFalse(data["read_only"])
            self.assertTrue(data["actions_read_only"])
            self.assertEqual(self.client.put(f"/api/reminder-stickies/{self.rid}", json={"y": 250}).status_code, 200)
            self.assertEqual(self.client.delete(f"/api/reminder-stickies/{self.rid}").status_code, 200)
        self.assertEqual(commitments.STORE.read_bytes(), before)

    def test_corrupt_layout_is_reported_and_never_overwritten(self):
        for text in ('{"pins": []}', '{"pins": {"' + self.rid + '": null}}', '{bad'):
            with self.subTest(text=text):
                stickies.STORE.write_text(text, encoding="utf-8")
                self.assertEqual(self.client.get("/api/reminder-stickies").status_code, 400)
                self.assertEqual(self.client.post("/api/reminder-stickies", json={"reminder_id": self.rid}).status_code, 400)
                self.assertEqual(stickies.STORE.read_text(encoding="utf-8"), text)


if __name__ == "__main__":
    unittest.main()

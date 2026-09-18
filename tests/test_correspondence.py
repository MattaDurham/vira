"""Private intake uses synthetic messages, isolated state, and temporary vaults."""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import correspondence as intake
from server import contactintel, executive, inbound, mediaindex, settings, textindex, vault, vaultwrite


class CorrespondenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "vault"
        self.other = self.root / "other"
        self.home.mkdir()
        self.other.mkdir()
        self.cfg = self.root / "config.json"
        self.values = {"vault_root": str(self.other), "vault_primary": {"write_enabled": False},
            "vault_sources": [{"id": "home", "name": "Household", "root": str(self.home),
                "write_enabled": True, "capture_dir": "inbox", "write_dirs": ["inbox", "finances"],
                "protected_dirs": ["finances/private"], "purpose": "Shared household records"}],
            "correspondence": {"enabled": True, "auto_file": True, "auto_confidence": .9,
                "catchup_days": 14, "routes": []}}
        self.write_config()
        for patch in (
            mock.patch.object(settings, "CONFIG_PATH", self.cfg),
            mock.patch.object(settings, "fixture_mode", return_value=False),
            mock.patch.object(settings, "sandboxed", return_value=False),
            mock.patch.object(intake, "STATE", self.root / "intake.json"),
            mock.patch.object(intake, "LOCKS", self.root / "locks"),
            mock.patch.object(vaultwrite, "LOCK_ROOT", self.root / "vault-locks"),
            mock.patch.object(vaultwrite, "_index_source", side_effect=lambda spec, receipt: receipt),
            mock.patch.object(mediaindex, "DB", self.root / "media.sqlite"),
            mock.patch.object(inbound, "is_ours", return_value=False),
            mock.patch.object(contactintel, "_state", return_value={"seen": {}, "pending": {}}),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("VIRA_PASSIVE", None)
        os.environ.pop("VIRA_SANDBOX", None)
        self.source = {"id": "mail:<draft@example.invalid>", "channel": "email",
            "account": "owner@example.invalid", "handle": "partner@example.invalid", "person_id": "p1",
            "when": datetime.now(timezone.utc).isoformat(), "subject": "Household budget draft",
            "text": "Here is the draft budget for our household.", "is_from_me": False,
            "body_complete": True, "has_attachments": False}
        self.lookup = mock.patch.object(textindex, "lookup_sources", side_effect=lambda ids: {
            self.source["id"]: dict(self.source)} if self.source["id"] in ids else {})
        self.lookup.start()
        self.addCleanup(self.lookup.stop)

    def write_config(self):
        self.cfg.write_text(json.dumps(self.values), encoding="utf-8")

    def insert(self, source=None):
        ids = []
        intake._change(lambda s: ids.append(intake._insert(s, source or self.source)))
        return ids[0]

    def route(self, **extra):
        return {"id": "budget", "destination": "home", "folder": "finances",
                "terms": ["budget"], "purpose": "Household finances", "automatic": True, **extra}

    def test_manual_capture_preserves_evidence_in_selected_category_and_deduplicates(self):
        first = intake.capture(self.source["id"], destination="home", folder="finances")
        second = intake.capture(self.source["id"], destination="home", folder="finances")
        self.assertEqual(first["state"], "processed")
        self.assertEqual(first["receipt"], second["receipt"])
        path = self.home / first["receipt"]["relative_path"]
        text = path.read_text(encoding="utf-8")
        self.assertIn(self.source["id"], text)
        self.assertIn(self.source["text"], text)
        self.assertEqual(len(list(self.home.rglob("*.md"))), 1)
        self.assertEqual(list(self.other.rglob("*.md")), [])

    def test_rule_routes_locally_without_a_model(self):
        intake.save_config({"routes": [self.route()]})
        ident = self.insert()
        with mock.patch("server.suggest.complete") as model:
            result = intake._process(ident)
        model.assert_not_called()
        self.assertEqual(result["state"], "processed")
        self.assertEqual(result["folder"], "finances")

    def test_ambiguous_rules_never_choose_default_or_write(self):
        intake.save_config({"routes": [self.route(), self.route(id="other-rule", folder="inbox")]})
        result = intake._process(self.insert())
        self.assertEqual(result["state"], "review")
        self.assertIsNone(result["destination"])
        self.assertEqual(list(self.home.rglob("*.md")), [])

    def test_disabled_and_low_confidence_file_only_after_review(self):
        ident = self.insert()
        with mock.patch.object(intake, "_classify", return_value={
                "disposition": "keep", "confidence": .5, "route_id": "vault:home"}):
            item = intake._process(ident)
        self.assertEqual(item["state"], "review")
        self.assertEqual(list(self.home.rglob("*.md")), [])
        intake.save_config({"enabled": False})
        accepted = intake.review(ident, "approve", destination="home", folder="finances")
        self.assertEqual(accepted["state"], "processed")

    def test_task_only_reuses_existing_engine_without_vault_note(self):
        with mock.patch.object(contactintel, "enabled", return_value=True), \
                mock.patch.object(contactintel, "enqueue") as enqueue, \
                mock.patch.object(executive, "commitment_records", return_value=[]):
            item = intake.capture(self.source["id"], disposition="task")
        self.assertEqual(item["task_status"], "pending")
        enqueue.assert_called_once()
        self.assertIsNone(item["receipt"])
        self.assertEqual(list(self.home.rglob("*.md")), [])

    def test_both_links_task_by_exact_evidence_without_duplicate_creation(self):
        record = {"subject_key": "p1", "loop": {"what": "Review budget", "owed_by": "me",
                  "assistant_key": "a", "evidence": [{"id": self.source["id"]}]}}
        with mock.patch.object(executive, "commitment_records", return_value=[record]), \
                mock.patch.object(contactintel, "enqueue") as enqueue:
            item = intake.capture(self.source["id"], disposition="both", destination="home")
        self.assertEqual(item["task_status"], "recorded")
        self.assertEqual(len(item["task_ids"]), 1)
        self.assertEqual(item["state"], "processed")
        enqueue.assert_not_called()

    def test_task_engine_failure_does_not_block_independent_preservation(self):
        with mock.patch.object(executive, "commitment_records", side_effect=ValueError("task store needs repair")):
            item = intake.capture(self.source["id"], disposition="both", destination="home")
        self.assertEqual(item["state"], "processed")
        self.assertEqual(item["task_status"], "error")
        self.assertTrue(item["receipt"])

    def test_already_processed_without_task_is_review_not_perpetually_pending(self):
        state = {"seen": {self.source["id"]: {
            "digest": hashlib.sha256(self.source["text"].encode("utf-8")).hexdigest()}}, "pending": {}}
        with mock.patch.object(executive, "commitment_records", return_value=[]), \
                mock.patch.object(contactintel, "enabled", return_value=True), \
                mock.patch.object(contactintel, "_state", return_value=state), \
                mock.patch.object(contactintel, "enqueue") as enqueue:
            item = intake.capture(self.source["id"], disposition="task")
        self.assertEqual(item["task_status"], "review")
        enqueue.assert_not_called()

    def test_folder_protection_unknown_destination_and_passive_fail_closed(self):
        for folder in ("../out", "finances/private", "canon", "finances/../../out"):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                intake.capture(self.source["id"], destination="home", folder=folder)
        with self.assertRaises(ValueError):
            intake.capture(self.source["id"], destination="missing")
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(ValueError):
                intake.capture(self.source["id"], destination="home")
            self.assertFalse(intake.enabled())
            intake.tick()
        self.assertEqual(list(self.home.rglob("*.md")), [])

    def test_route_readonly_after_classification_does_not_fallback(self):
        ident = self.insert()
        intake._decision(ident, {"disposition": "keep", "confidence": 1, "route_id": "vault:home"})
        self.values["vault_sources"][0]["write_enabled"] = False
        self.write_config()
        item = intake._process(ident)
        self.assertEqual(item["state"], "error")
        self.assertEqual(list(self.other.rglob("*.md")), [])

    def test_model_hidden_vault_excluded_but_manual_capture_allowed(self):
        self.values["vault_sources"][0]["model_exposure"] = False
        self.write_config()
        self.assertEqual(intake.routes(for_model=True), [])
        item = intake.capture(self.source["id"], destination="home")
        self.assertEqual(item["state"], "processed")

    def test_binary_retry_after_partial_failure_preserves_exact_bytes_once(self):
        a, b = self.root / "a.xlsx", self.root / "b.pdf"
        a.write_bytes(b"sheet-original")
        b.write_bytes(b"pdf-original")
        files = [{"id": "1", "path": str(a), "name": "../../budget.xlsx"},
                 {"id": "2", "path": str(b), "name": "budget.pdf"}]
        original = vaultwrite.write_bytes
        calls = []
        def interrupted(spec, rel, data):
            calls.append(rel)
            if len(calls) == 2:
                raise OSError("simulated interruption")
            return original(spec, rel, data)
        with mock.patch.object(intake, "_attachments", return_value=(files, [])), \
                mock.patch.object(vaultwrite, "write_bytes", side_effect=interrupted):
            item = intake.capture(self.source["id"], destination="home", folder="finances")
        self.assertEqual(item["state"], "error")
        self.assertEqual(len([p for p in self.home.rglob("*") if p.is_file()]), 1)
        with mock.patch.object(intake, "_attachments", return_value=(files, [])):
            item = intake.retry(item["id"])
        self.assertEqual(item["state"], "processed")
        preserved = item["receipt"]["attachments"]
        self.assertEqual(len(preserved), 2)
        self.assertEqual(len([p for p in self.home.rglob("*") if p.is_file()]), 3)
        for receipt, expected in zip(preserved, (b"sheet-original", b"pdf-original")):
            self.assertEqual((self.home / receipt["relative_path"]).read_bytes(), expected)
            self.assertEqual(receipt["sha256"], hashlib.sha256(expected).hexdigest())

    def test_restart_after_note_write_recovers_without_duplicate(self):
        original = vaultwrite.write_note
        def crash(spec, rel, text, **kwargs):
            original(spec, rel, text, **kwargs)
            raise OSError("interrupted after file commit")
        with mock.patch.object(vaultwrite, "write_note", side_effect=crash):
            item = intake.capture(self.source["id"], destination="home")
        self.assertEqual(item["state"], "error")
        self.assertEqual(len(list(self.home.rglob("*.md"))), 1)
        item = intake.retry(item["id"])
        self.assertEqual(item["state"], "processed")
        self.assertEqual(len(list(self.home.rglob("*.md"))), 1)

    def test_recapture_after_preservation_started_retains_recoverable_state(self):
        original = vaultwrite.write_note
        def interrupted(spec, rel, text, **kwargs):
            original(spec, rel, text, **kwargs)
            raise OSError("interrupted before receipt")
        with mock.patch.object(vaultwrite, "write_note", side_effect=interrupted):
            first = intake.capture(self.source["id"], destination="home", folder="inbox")
        self.assertEqual(first["state"], "error")
        repeated = intake.capture(self.source["id"], disposition="task", destination="home", folder="finances")
        self.assertEqual(repeated["state"], "error")
        self.assertEqual(repeated["disposition"], "keep")
        self.assertEqual(repeated["folder"], "inbox")
        self.assertEqual(repeated["error"], first["error"])
        recovered = intake.retry(first["id"])
        self.assertEqual(recovered["state"], "processed")
        self.assertEqual(recovered["folder"], "inbox")
        self.assertEqual(len(list(self.home.rglob("*.md"))), 1)

    def test_owner_edit_is_not_overwritten_on_resume(self):
        item = intake.capture(self.source["id"], destination="home")
        path = self.home / item["receipt"]["relative_path"]
        path.write_text("Owner edit", encoding="utf-8")
        intake._set(item["id"], state="processing")
        item = intake.retry(item["id"])
        self.assertEqual(item["state"], "error")
        self.assertEqual(path.read_text(encoding="utf-8"), "Owner edit")

    def test_unknown_attachments_and_truncation_are_saved_not_processed(self):
        self.source.update(has_attachments=True, body_complete=False)
        item = intake.capture(self.source["id"], destination="home")
        self.assertEqual(item["state"], "saved")
        self.assertGreaterEqual(len(item["limitations"]), 2)
        self.assertTrue(item["receipt"])

    def test_supplement_revision_does_not_erase_original_or_requeue_same_source(self):
        self.source.update(has_attachments=True)
        item = intake.capture(self.source["id"], destination="home")
        first = item["receipt"]["path"]
        with mock.patch.object(intake, "_attachments", return_value=([], [])):
            item = intake.retry(item["id"])
        self.assertNotEqual(first, item["receipt"]["path"])
        self.assertEqual(len(list(self.home.rglob("*.md"))), 2)
        self.insert()
        self.assertEqual(intake._state()["items"][item["id"]]["state"], "processed")

    def test_cursor_is_durable_and_source_changes_are_revisioned(self):
        batch = {"items": [self.source], "cursor": 12, "available": True}
        with mock.patch.object(textindex, "changes_since", return_value=batch):
            intake.catch_up()
            intake.catch_up()
        self.assertEqual(len(intake._state()["items"]), 1)
        self.assertEqual(intake._state()["cursor"], 12)
        item = intake.capture(self.source["id"], destination="home")
        self.source["text"] += " The full original is now available."
        self.insert()
        changed = intake._state()["items"][item["id"]]
        self.assertEqual(changed["state"], "queued")
        self.assertEqual(len(changed["history"]), 1)

    def test_source_attachment_manifest_is_exact_not_sender_or_subject_match(self):
        con = sqlite3.connect(mediaindex.DB)
        con.executescript("CREATE TABLE items(id INTEGER,name TEXT,path TEXT,source TEXT,account TEXT);"
                         "CREATE TABLE mail_attachment_sources(source_id TEXT,account TEXT,attachment_ids TEXT,complete INT);")
        con.execute("INSERT INTO items VALUES(1,'a.pdf','/unused','email',?)", (self.source["account"],))
        con.execute("INSERT INTO mail_attachment_sources VALUES(?,?,?,1)",
                    (self.source["id"], self.source["account"], "[1]"))
        con.commit()
        con.close()
        files, limits = intake._attachments(self.source)
        self.assertEqual(files[0]["id"], 1)
        self.assertEqual(limits, [])
        unknown = {**self.source, "id": "mail:<other@example.invalid>", "has_attachments": True}
        files, limits = intake._attachments(unknown)
        self.assertEqual(files, [])
        self.assertTrue(limits)

    def test_binary_confinement_stale_data_and_symlink(self):
        spec = vaultwrite.resolve_destination("home")
        receipt = vaultwrite.write_bytes(spec, "inbox/a.bin", b"original")
        self.assertEqual(vaultwrite.write_bytes(spec, "inbox/a.bin", b"original"), receipt)
        with self.assertRaises(FileExistsError):
            vaultwrite.write_bytes(spec, "inbox/a.bin", b"changed")
        with self.assertRaises(ValueError):
            vaultwrite.write_bytes(spec, "finances/private/a.bin", b"secret")
        try:
            (self.home / "inbox/escape").symlink_to(self.other, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(ValueError):
            vaultwrite.write_bytes(spec, "inbox/escape/a.bin", b"escape")

    @unittest.skipUnless(vaultwrite._DIR_FD, "requires descriptor-relative filesystem calls")
    def test_binary_commit_rolls_back_if_parent_is_moved_outside_vault(self):
        spec = vaultwrite.resolve_destination("home")
        original = os.link
        moved = self.root / "moved"
        def swap(source, target, **kwargs):
            (self.home / "inbox").rename(moved)
            (self.home / "inbox").symlink_to(moved, target_is_directory=True)
            return original(source, target, **kwargs)
        with mock.patch.object(vaultwrite.os, "link", side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                vaultwrite.write_bytes(spec, "inbox/new.pdf", b"original")
        self.assertEqual(list(moved.iterdir()), [])

    def test_review_rejects_pathlike_ids_and_pause_survives_disconnected_route(self):
        with self.assertRaises(ValueError):
            intake.review("../../escape", "approve")
        intake.save_config({"routes": [self.route()]})
        self.home.rmdir()
        self.assertFalse(intake.save_config({"enabled": False})["enabled"])

    def test_corrupt_state_is_not_silently_reset(self):
        intake.STATE.write_text('{"wrong":true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.insert()
        self.assertEqual(intake.STATE.read_text(encoding="utf-8"), '{"wrong":true}')

    def test_work_receipts_are_uncapped_without_source_bodies(self):
        def seed(state):
            for number in range(65):
                source = {**self.source, "id": f"mail:<{number}@example.invalid>"}
                ident = intake._insert(state, source)
                state["items"][ident].update(state="processed", receipt={"path": f"@home/inbox/{number}.md"})
        intake._change(seed)
        self.assertEqual(len(intake.receipts()), 65)
        self.assertEqual(len(intake.status()["items"]), 50)
        self.assertNotIn("source", intake.receipts()[0])

    def test_model_classification_requires_separate_consent_and_available_routes(self):
        ident = self.insert()
        item = intake._state()["items"][ident]
        with mock.patch("server.suggest.complete") as model, mock.patch("server.models.probe") as probe:
            result = intake._classify(item)
        self.assertEqual(result["confidence"], 0)
        model.assert_not_called()
        probe.assert_not_called()
        intake.save_config({"model_classification": True})
        self.values = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.values["vault_sources"][0]["model_exposure"] = False
        self.write_config()
        with mock.patch("server.suggest.complete") as model, mock.patch("server.models.probe") as probe:
            result = intake._classify(item)
        model.assert_not_called()
        probe.assert_not_called()
        self.assertIn("permits model access", result["reason"])

    def test_unavailable_model_and_manual_only_route_cannot_auto_file(self):
        intake.save_config({"model_classification": True,
                            "routes": [self.route(terms=["unmatched"], automatic=False)]})
        ident = self.insert()
        item = intake._state()["items"][ident]
        with mock.patch("server.models.probe", return_value={"connected": False}), \
                mock.patch("server.suggest.complete") as model:
            result = intake._classify(item)
        model.assert_not_called()
        self.assertIn("No connected", result["reason"])
        with mock.patch("server.models.probe", return_value={"connected": True}), \
                mock.patch("server.suggest.complete", return_value=json.dumps({
                    "disposition": "keep", "confidence": 1, "route_id": "budget"})):
            result = intake._process(item["id"])
        self.assertEqual(result["state"], "review")
        self.assertEqual(list(self.home.rglob("*.md")), [])

    def test_historical_backfill_after_cursor_needs_manual_capture(self):
        self.source["when"] = "2005-01-01T00:00:00+00:00"
        intake._change(lambda s: s.update(cursor=99))
        with mock.patch.object(textindex, "changes_since", return_value={
                "items": [self.source], "cursor": 100, "available": True}):
            intake.catch_up()
        self.assertEqual(intake._state()["items"], {})
        self.assertEqual(intake._state()["cursor"], 100)
        item = intake.capture(self.source["id"], destination="home")
        self.assertEqual(item["state"], "processed")

    def test_enrichment_waits_for_inflight_source_before_replacing_state(self):
        ident = self.insert()
        started, release = threading.Event(), threading.Event()
        def classify(item):
            started.set()
            self.assertTrue(release.wait(5))
            return {"disposition": "keep", "confidence": 1, "route_id": "vault:home"}
        changed = {**self.source, "text": "A fuller source body, newly indexed."}
        with mock.patch.object(intake, "_classify", side_effect=classify), \
                mock.patch.object(textindex, "changes_since", return_value={
                    "items": [changed], "cursor": 101, "available": True}), \
                ThreadPoolExecutor(max_workers=2) as pool:
            processing = pool.submit(intake._process, ident)
            self.assertTrue(started.wait(5))
            catchup = pool.submit(intake.catch_up)
            try:
                self.assertEqual(intake._state()["items"][ident]["source"]["text"], self.source["text"])
            finally:
                release.set()
            processing.result(timeout=5)
            catchup.result(timeout=5)
        item = intake._state()["items"][ident]
        self.assertEqual(item["source"]["text"], changed["text"])
        self.assertEqual(item["state"], "queued")
        self.assertIsNone(item["receipt"])
        self.assertEqual(len(item["history"]), 1)

    def test_frozen_recovery_uses_preserved_attachments_and_refuses_reroute(self):
        source_file = self.root / "original.pdf"
        source_file.write_bytes(b"original bytes")
        files = [{"id": 1, "name": "original.pdf", "path": str(source_file)}]
        original = vaultwrite.write_note
        def interrupted(spec, rel, text, **kwargs):
            original(spec, rel, text, **kwargs)
            raise OSError("after note committed")
        with mock.patch.object(intake, "_attachments", return_value=(files, [])), \
                mock.patch.object(vaultwrite, "write_note", side_effect=interrupted):
            item = intake.capture(self.source["id"], destination="home")
        source_file.write_bytes(b"changed cache contents")
        with self.assertRaisesRegex(ValueError, "frozen"):
            intake.review(item["id"], "approve", destination="home", folder="finances")
        with mock.patch.object(intake, "_attachments", side_effect=AssertionError("must use frozen plan")):
            item = intake.retry(item["id"])
        self.assertEqual(item["state"], "processed")
        self.assertEqual(len([p for p in self.home.rglob("*") if p.is_file()]), 2)
        self.assertEqual((self.home / item["receipt"]["attachments"][0]["relative_path"]).read_bytes(), b"original bytes")

    def test_routes_and_capture_api(self):
        app = FastAPI()
        app.include_router(intake.router)
        client = TestClient(app)
        response = client.patch("/api/correspondence/config", json={"routes": [self.route()]})
        self.assertEqual(response.status_code, 200)
        item = client.post("/api/correspondence/capture", json={"source_id": self.source["id"]}).json()
        self.assertEqual(item["state"], "review")
        detail = client.get("/api/correspondence/" + item["id"]).json()
        self.assertEqual(detail["text"], self.source["text"])
        approved = client.post("/api/correspondence/" + item["id"] + "/review",
                               json={"action": "approve", "route_id": "budget"})
        self.assertEqual(approved.json()["state"], "processed")
        summary = client.get("/api/correspondence").json()
        self.assertNotIn("text", summary["items"][0])
        self.assertFalse(summary["passive"])


if __name__ == "__main__":
    unittest.main()

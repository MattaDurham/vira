"""Native Microsoft setup joins; no real stores, tokens, or network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from server import jsonstore, main, msgraph, settings


CLIENT = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
TENANT = "00000000-0000-4000-8000-000000000003"
EMAIL = "casey@example.com"


class MicrosoftSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = self.root / "config.json"
        self.accounts = self.root / "mail-accounts.json"
        self.tokens = {}
        self.now = 1000
        self.poll_args = []
        read, write = jsonstore.read, jsonstore.write_atomic

        def guard(fn):
            def run(path, *args, **kwargs):
                self.assertTrue(Path(path).resolve().is_relative_to(self.root), "Real store accessed")
                return fn(path, *args, **kwargs)
            return run

        patches = [
            mock.patch.object(msgraph, "CONFIG", self.config),
            mock.patch.object(settings, "CONFIG_PATH", self.config),
            mock.patch.object(msgraph, "ACCOUNTS", self.accounts),
            mock.patch.object(msgraph, "_flows", {}),
            mock.patch.object(msgraph, "_tokens", {}),
            mock.patch.object(msgraph, "_spawn_device_poll", side_effect=lambda *args: self.poll_args.append(args)),
            mock.patch.object(msgraph, "_post_form"),
            mock.patch.object(msgraph.time, "time", side_effect=lambda: self.now),
            mock.patch.object(msgraph.time, "sleep"),
            mock.patch.object(settings, "keychain_service", side_effect=lambda name: "test-" + name),
            mock.patch.object(msgraph.secrets, "get", side_effect=lambda service, email: self.tokens.get((service, email))),
            mock.patch.object(msgraph.secrets, "set", side_effect=lambda service, email, value: self.tokens.__setitem__((service, email), value)),
            mock.patch.object(jsonstore, "read", side_effect=guard(read)),
            mock.patch.object(jsonstore, "write_atomic", side_effect=guard(write)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.api = TestClient(main.app)  # no lifespan/background workers

    def seed(self, **values):
        jsonstore.write_atomic(self.config, {"owner_name": "Example", **values})

    def save(self, client=CLIENT, tenant=TENANT):
        return self.api.post("/api/mail/graph/registration", json={"client_id": client, "tenant": tenant})

    def start(self):
        msgraph._post_form.return_value = {"device_code": "private-device-code", "user_code": "USER-CODE", "expires_in": 60, "interval": 5}
        return self.api.post("/api/mail/graph/start", json={"email": EMAIL})

    def test_missing_registration_is_setup_needed_and_does_not_call_microsoft(self):
        status = self.api.get("/api/mail/graph/registration").json()
        self.assertFalse(status["configured"])
        response = self.start()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Config", response.json()["detail"])
        msgraph._post_form.assert_not_called()
        self.assertFalse(self.poll_args)

    def test_save_then_button_starts_login_without_restart_or_config_file_edit(self):
        self.seed()
        response = self.save("  " + CLIENT + "  ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(jsonstore.read(self.config, {})["owner_name"], "Example")
        self.assertTrue(self.api.get("/api/mail/graph/registration").json()["configured"])
        response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        url, form = msgraph._post_form.call_args.args
        self.assertIn(TENANT, url)
        self.assertEqual(form["client_id"], CLIENT)
        self.assertEqual(form["scope"], msgraph.SCOPE_LOGIN)
        self.assertEqual(response.json()["user_code"], "USER-CODE")
        self.assertNotIn("device_code", response.json())
        self.assertTrue(msgraph.flow_status(EMAIL)["pending"])

    def test_archived_shape_and_empty_tenant_are_detected_without_overwrite(self):
        self.seed(msgraph_client_id=CLIENT, msgraph_tenant="")
        before = self.config.read_bytes()
        status = self.api.get("/api/mail/graph/registration").json()
        self.assertTrue(status["configured"])
        self.assertEqual(status["tenant"], "organizations")
        self.assertEqual(self.config.read_bytes(), before)

    def test_bad_identifiers_cannot_write_config_or_change_authority(self):
        self.seed()
        before = self.config.read_bytes()
        for client, tenant in [("not-an-id", TENANT), (CLIENT, "https://example.com"), (CLIENT, "../common"), (CLIENT, "tenant?query=1")]:
            self.assertEqual(self.save(client, tenant).status_code, 400)
            self.assertEqual(self.config.read_bytes(), before)

    def test_unreadable_config_is_not_replaced_by_setup(self):
        self.config.write_text("{broken", encoding="utf-8")
        self.assertFalse(self.api.get("/api/mail/graph/registration").json()["configured"])
        self.assertEqual(self.save().status_code, 409)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{broken")

    def test_tenant_domain_and_supported_account_aliases(self):
        for tenant in ["example.onmicrosoft.com", "common", "consumers", "organizations"]:
            self.assertEqual(self.save(tenant=tenant).status_code, 200)

    def test_invalid_email_does_not_request_a_device_code(self):
        self.save()
        response = self.api.post("/api/mail/graph/start", json={"email": "wrong"})
        self.assertEqual(response.status_code, 400)
        msgraph._post_form.assert_not_called()

    def test_pending_login_blocks_registration_change_but_not_same_registration(self):
        self.save()
        self.start()
        self.assertEqual(self.save(OTHER).status_code, 409)
        self.assertEqual(jsonstore.read(self.config, {})["msgraph_client_id"], CLIENT)
        self.assertEqual(self.save().status_code, 200)
        self.now += 61
        self.assertEqual(self.save(OTHER).status_code, 200)
        self.assertIn("expired", msgraph.flow_status(EMAIL)["error"])

    def test_existing_mailbox_blocks_registration_change(self):
        self.save()
        jsonstore.write_atomic(self.accounts, [{"email": EMAIL, "type": "graph"}])
        self.assertEqual(self.save(OTHER).status_code, 409)
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(jsonstore.read(self.config, {})["msgraph_client_id"], CLIENT)

    def test_completed_login_persists_token_and_mailbox_before_connected(self):
        self.save()
        self.start()
        msgraph._post_form.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
        msgraph._poll_for_token(*self.poll_args[0])
        self.assertEqual(self.tokens[("test-" + msgraph.KEYCHAIN_SERVICE, EMAIL)], "test-refresh")
        self.assertEqual(jsonstore.read(self.accounts, []), [{"email": EMAIL, "type": "graph"}])
        self.assertTrue(self.api.get("/api/mail/graph/status", params={"email": EMAIL}).json()["connected"])
        self.assertNotIn("test-refresh", self.config.read_text(encoding="utf-8"))

    def test_account_registration_preserves_wrapped_store_and_imap(self):
        jsonstore.write_atomic(self.accounts, {"accounts": [{"email": EMAIL, "type": "imap"}], "extra": "keep"})
        msgraph._ensure_account_entry(EMAIL)
        msgraph._ensure_account_entry(EMAIL)
        value = jsonstore.read(self.accounts, {})
        self.assertEqual(value["extra"], "keep")
        self.assertEqual(len(value["accounts"]), 2)

    def test_failed_secret_write_does_not_claim_mailbox_connected(self):
        self.save()
        self.start()
        msgraph._post_form.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
        with mock.patch.object(msgraph.secrets, "set", side_effect=RuntimeError("Cannot save refresh token")):
            msgraph._poll_for_token(*self.poll_args[0])
        self.assertFalse(self.accounts.exists())
        status = msgraph.flow_status(EMAIL)
        self.assertFalse(status["connected"])
        self.assertIn("Cannot save", status["error"])

    def test_network_failure_is_visible_and_retry_can_start(self):
        self.save()
        self.start()
        msgraph._post_form.side_effect = OSError("Microsoft unavailable")
        msgraph._poll_for_token(*self.poll_args[0])
        self.assertEqual(msgraph.flow_status(EMAIL)["error"], "Microsoft unavailable")
        self.assertEqual(self.save(OTHER).status_code, 200)

    def test_stale_login_cannot_persist_tokens_or_modify_new_flow(self):
        self.save()
        self.start()
        old = self.poll_args[0]
        def response(*args):
            msgraph._flows[EMAIL] = {"expires_at": self.now + 60, "connected": False}
            return {"access_token": "stale", "refresh_token": "stale"}
        msgraph._post_form.side_effect = response
        msgraph._poll_for_token(*old)
        self.assertFalse(self.tokens)
        self.assertFalse(self.accounts.exists())
        self.assertFalse(msgraph._flows[EMAIL]["connected"])

    def test_polling_uses_issuing_registration_and_expiry_has_visible_error(self):
        self.save()
        self.start()
        self.seed(msgraph_client_id=OTHER, msgraph_tenant="common")
        msgraph._post_form.return_value = {"error": "access_denied", "error_description": "Declined"}
        msgraph._poll_for_token(*self.poll_args[0])
        url, form = msgraph._post_form.call_args.args
        self.assertIn(TENANT, url)
        self.assertEqual(form["client_id"], CLIENT)
        self.assertEqual(msgraph.flow_status(EMAIL)["error"], "Declined")

    def test_public_client_error_explains_action_inside_setup(self):
        self.save()
        msgraph._post_form.return_value = {"error_description": "AADSTS7000218 client_secret required"}
        response = self.api.post("/api/mail/graph/start", json={"email": EMAIL})
        self.assertEqual(response.status_code, 502)
        self.assertIn("Allow public client flows", response.json()["detail"])

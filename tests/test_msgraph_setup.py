"""Native Microsoft setup joins; no real stores, tokens, or network."""
import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from server import jsonstore, main, msgraph, msgraphbrowser, msgraphsetup, settings, viratools


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
            mock.patch.object(msgraph, "PUBLISHER_CLIENT_ID", ""),
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
        self.api = TestClient(main.app, base_url="http://localhost:8378")  # no lifespan/background workers

    def browser_start(self):
        return self.api.post("/api/mail/graph/browser/start", headers={"origin": "http://localhost:8378"})

    def browser_complete(self, params, **values):
        return self.api.post("/api/mail/graph/browser/complete", json={"state": params["state"][0], "code": "example-code", **values}, headers={"origin": "http://localhost:8378"})

    def test_browser_login_joins_pkce_profile_secret_and_account(self):
        self.save()
        started = self.browser_start()
        self.assertEqual(started.status_code, 200, started.text)
        params = parse_qs(urlparse(started.json()["authorize_url"]).query)
        self.assertEqual(params["response_mode"], ["fragment"])
        self.assertEqual(params["redirect_uri"], ["http://localhost:8378" + msgraphbrowser.CALLBACK])
        self.assertIn("HttpOnly", started.headers["set-cookie"])
        self.assertIn("SameSite=lax", started.headers["set-cookie"])
        self.assertEqual(self.save(OTHER).status_code, 409)
        msgraph._post_form.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
        with mock.patch.object(msgraphbrowser, "_profile", return_value=EMAIL):
            done = self.browser_complete(params)
        self.assertTrue(done.json()["connected"], done.text)
        self.assertEqual(done.json()["email"], EMAIL)
        form = msgraph._post_form.call_args.args[1]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode("ascii")).digest()).decode("ascii").rstrip("=")
        self.assertEqual(params["code_challenge"], [challenge])
        self.assertEqual(form["client_id"], CLIENT)
        self.assertEqual(form["redirect_uri"], params["redirect_uri"][0])
        self.assertEqual(jsonstore.read(self.accounts, []), [{"email": EMAIL, "type": "graph"}])
        self.assertEqual(self.tokens[("test-" + msgraph.KEYCHAIN_SERVICE, EMAIL)], "test-refresh")
        self.assertTrue(self.api.get("/api/mail/graph/browser/status").json()["connected"])
        msgraph._post_form.reset_mock()
        self.assertEqual(self.browser_complete(params).status_code, 400)
        msgraph._post_form.assert_not_called()
        self.assertEqual(self.save(OTHER).status_code, 409)

    def test_browser_state_cookie_origin_expiry_and_retry_guards(self):
        self.save()
        started = self.browser_start()
        params = parse_qs(urlparse(started.json()["authorize_url"]).query)
        missing_cookie = TestClient(main.app, base_url="http://localhost:8378")
        response = missing_cookie.post("/api/mail/graph/browser/complete", json={"state": params["state"][0], "code": "code"}, headers={"origin": "http://localhost:8378"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.browser_complete(params, state="wrong").status_code, 400)
        self.assertEqual(self.api.post("/api/mail/graph/browser/complete", json={"state": params["state"][0], "code": "code"}, headers={"origin": "https://example.com"}).status_code, 400)
        self.assertEqual(self.api.post("/api/mail/graph/browser/start", headers={"origin": "http://localhost:8378", "host": "example.com"}).status_code, 400)
        self.assertEqual(self.api.post("/api/mail/graph/browser/start").status_code, 400)
        newer = parse_qs(urlparse(self.browser_start().json()["authorize_url"]).query)
        self.assertEqual(self.browser_complete(params).status_code, 400)
        self.now += msgraphbrowser.TTL + 1
        self.assertEqual(self.browser_complete(newer).status_code, 400)
        self.assertIn("Connect Microsoft again", self.api.get("/api/mail/graph/browser/status").json()["error"])
        self.assertFalse(self.accounts.exists())
        self.assertFalse(self.tokens)
        msgraph._post_form.assert_not_called()
        self.assertEqual(self.save(OTHER).status_code, 200)

    def test_browser_denial_and_failed_secret_never_show_connected(self):
        self.save()
        params = parse_qs(urlparse(self.browser_start().json()["authorize_url"]).query)
        denied = self.browser_complete(params, code="", error="access_denied")
        self.assertFalse(denied.json()["connected"])
        self.assertIn("access_denied", denied.json()["error"])
        msgraph._post_form.assert_not_called()
        params = parse_qs(urlparse(self.browser_start().json()["authorize_url"]).query)
        msgraph._post_form.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
        with mock.patch.object(msgraphbrowser, "_profile", return_value=EMAIL), mock.patch.object(msgraph.secrets, "set", side_effect=RuntimeError("Secret store unavailable")):
            failed = self.browser_complete(params)
        self.assertFalse(failed.json()["connected"])
        self.assertIn("Secret store unavailable", failed.json()["error"])
        self.assertFalse(self.accounts.exists())
        self.assertFalse(msgraph._tokens)

    def test_browser_callback_has_no_store_or_third_party_resources(self):
        callback = self.api.get(msgraphbrowser.CALLBACK)
        self.assertEqual(callback.status_code, 200)
        self.assertEqual(callback.headers["cache-control"], "no-store")
        self.assertEqual(callback.headers["referrer-policy"], "no-referrer")
        self.assertIn("connect-src 'self'", callback.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", callback.headers["content-security-policy"])

    def test_browser_identity_comes_from_authenticated_graph_and_profile_failure_is_not_connected(self):
        self.save()
        params = parse_qs(urlparse(self.browser_start().json()["authorize_url"]).query)
        msgraph._post_form.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"mail": EMAIL, "userPrincipalName": "different@example.com"}).encode("utf-8")
        with mock.patch.object(msgraphbrowser.urllib.request, "urlopen", return_value=response) as request:
            done = self.browser_complete(params)
        self.assertEqual(done.json()["email"], EMAIL)
        self.assertEqual(request.call_args.args[0].get_header("Authorization"), "Bearer test-access")
        params = parse_qs(urlparse(self.browser_start().json()["authorize_url"]).query)
        with mock.patch.object(msgraphbrowser.urllib.request, "urlopen", side_effect=OSError("network")):
            failed = self.browser_complete(params)
        self.assertFalse(failed.json()["connected"])
        self.assertIn("try again", failed.json()["error"])

    def test_browser_expired_cookie_cannot_redeem_and_failure_to_register_mailbox_is_visible(self):
        self.save()
        started = self.browser_start()
        params = parse_qs(urlparse(started.json()["authorize_url"]).query)
        cookie = started.headers["set-cookie"].split(";", 1)[0]
        self.now += msgraphbrowser.TTL + 1
        expired = self.api.post("/api/mail/graph/browser/complete", json={"state": params["state"][0], "code": "code"}, headers={"origin": "http://localhost:8378", "cookie": cookie})
        self.assertEqual(expired.status_code, 400)
        msgraph._post_form.assert_not_called()
        params = parse_qs(urlparse(self.browser_start().json()["authorize_url"]).query)
        msgraph._post_form.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
        with mock.patch.object(msgraphbrowser, "_profile", return_value=EMAIL), mock.patch.object(msgraph, "_ensure_account_entry", side_effect=RuntimeError("Account store unavailable")):
            failed = self.browser_complete(params)
        self.assertFalse(failed.json()["connected"])
        self.assertIn("Account store unavailable", failed.json()["error"])
        self.assertFalse(self.accounts.exists())

    def test_publisher_registration_is_used_only_when_provisioned_and_local_wins(self):
        self.seed()
        self.assertFalse(msgraph.registration_status()["configured"])
        with mock.patch.object(msgraph, "PUBLISHER_CLIENT_ID", OTHER):
            self.assertEqual(msgraph.registration_status()["source"], "publisher")
            self.assertEqual(msgraph._auth(), (OTHER, "https://login.microsoftonline.com/common"))
            self.save()
            self.assertEqual(msgraph.registration_status()["source"], "local")
            self.assertEqual(msgraph.registration_status()["client_id"], CLIENT)

    def test_native_setup_discovers_only_authorized_identifiers_and_configures_this_instance(self):
        source = self.root / "vault"
        source.mkdir()
        archived = source / "archive"
        archived.mkdir()
        jsonstore.write_atomic(archived / "config.json", {"msgraph_client_id": CLIENT, "msgraph_tenant": TENANT, "unrelated_secret": "must-not-be-returned"})
        hidden = source / "hidden"
        hidden.mkdir()
        jsonstore.write_atomic(hidden / "config.json", {"msgraph_client_id": OTHER})
        specs = [{"root": source, "id": "primary", "name": "Example vault", "primary": True, "read_enabled": True, "model_exposure": True, "model_exclude_dirs": ["hidden"]}]
        with mock.patch.object(msgraphsetup.backup, "DEST", self.root / "backups"), mock.patch.object(msgraphsetup.vault, "source_specs", return_value=specs):
            found = asyncio.run(viratools.invoke("microsoft_setup"))
        text = found["content"][0]["text"]
        self.assertNotIn("must-not-be-returned", text)
        payload = json.loads(text)
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertEqual(payload["candidates"][0]["client_id"], CLIENT)
        proposal = {"client_id": CLIENT, "tenant": TENANT}
        denied = asyncio.run(viratools.invoke("configure_microsoft", proposal, read_only=True))
        self.assertIn("read-only", denied["content"][0]["text"])
        self.assertFalse(self.config.exists())
        accepted = asyncio.run(viratools.invoke("configure_microsoft", proposal))
        self.assertTrue(json.loads(accepted["content"][0]["text"])["configured"])
        self.assertEqual(jsonstore.read(self.config, {})["msgraph_client_id"], CLIENT)
        bad = asyncio.run(viratools.invoke("configure_microsoft", {"client_id": "wrong"}))
        self.assertIn("error", bad["content"][0]["text"])
        self.assertEqual(jsonstore.read(self.config, {})["msgraph_client_id"], CLIENT)

    def test_setup_button_launches_a_native_task_without_config_or_microsoft_writes(self):
        with mock.patch.object(main.jobs, "launch", return_value="synthetic-setup") as launch:
            response = self.api.post("/api/mail/graph/setup", json={})
        self.assertEqual(response.json(), {"job_id": "synthetic-setup"})
        self.assertIn("configure_microsoft", launch.call_args.args[0])
        self.assertIn("ask_owner", launch.call_args.args[0])
        self.assertFalse(self.config.exists())
        self.assertFalse(self.accounts.exists())
        msgraph._post_form.assert_not_called()

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

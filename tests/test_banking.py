"""Bank setup validates read access before touching instance credentials."""
import json
import unittest
import urllib.error
from unittest import mock

from server import banking, mercury


class BankSetup(unittest.TestCase):
    def setUp(self):
        # Every network and secret seam is isolated; never inspect real credentials.
        for name, target in (("fetch", "server.mercury.fetch_accounts"),
                             ("get", "server.mercury._get"),
                             ("save", "server.banking.secrets.set"),
                             ("delete", "server.banking.secrets.delete"),
                             ("token", "server.mercury.keychain_token"),
                             ("service", "server.mercury.keychain_service")):
            patch = mock.patch(target)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        self.service.return_value = "test-vira-mercury"
        self.token.return_value = None
        self.fetch.return_value = [{"id": "checking"}, {"id": "credit"}]
        self.save.return_value = "keychain"

    def test_read_access_checked_before_saving_and_token_never_returned(self):
        self.get.side_effect = lambda *a: self.save.assert_not_called()
        result = banking.connect_mercury(" synthetic-token ", True)
        self.assertEqual(self.get.call_count, 2)
        self.save.assert_called_once_with("test-vira-mercury", None, "synthetic-token")
        self.assertEqual(result["accounts"], 2)
        self.assertNotIn("synthetic-token", json.dumps(result))

    def test_bad_token_or_missing_permission_confirmation_is_not_saved(self):
        for token, confirmed in (("", True), ("two words", True), ("token", False)):
            with self.subTest(token=token), self.assertRaises(ValueError):
                banking.connect_mercury(token, confirmed)
        self.fetch.assert_not_called()
        self.save.assert_not_called()

    def test_failed_account_or_transaction_reads_preserve_existing_token(self):
        for seam in (self.fetch, self.get):
            seam.side_effect = urllib.error.HTTPError("url", 403, "secret-token", {}, None)
            with self.assertRaises(ValueError) as error:
                banking.connect_mercury("synthetic-token", True)
            self.assertNotIn("secret-token", str(error.exception))
            self.save.assert_not_called()
            seam.side_effect = None

    def test_storage_errors_do_not_leak_token(self):
        self.save.side_effect = RuntimeError("synthetic-token")
        with self.assertRaises(ValueError) as error:
            banking.connect_mercury("synthetic-token", True)
        self.assertNotIn("synthetic-token", str(error.exception))

    def test_status_only_exposes_presence(self):
        self.token.return_value = "synthetic-token"
        self.assertEqual(banking.status(), {"mercury": {"configured": True}})

    def test_disconnect_only_removes_this_instance_token(self):
        self.assertEqual(banking.disconnect_mercury(), {"configured": False})
        self.delete.assert_called_once_with("test-vira-mercury")

    def test_route_connect_wakes_poller_after_validated_save(self):
        from server import main
        with mock.patch.object(main.mercury_poller, "poll_now") as wake:
            result = main.api_banking_mercury(main.BankingConnectReq(
                token="synthetic-token", read_only=True))
        self.assertTrue(result["configured"])
        self.save.assert_called_once()
        wake.assert_called_once()

    def test_setup_dispatch_carries_selected_bank_and_guidance(self):
        from server import main
        with mock.patch.object(main.jobs, "launch", return_value="test-job") as launch:
            result = main.api_banking_setup(main.BankingSetupReq(service="Chase"))
        self.assertEqual(result["job_id"], "test-job")
        prompt = launch.call_args.args[0]
        self.assertIn("'Chase'", prompt)
        self.assertIn("PRELOADED MERCURY PATH", prompt)
        self.assertIn("Vira has no Plaid/Chase connector yet", prompt)
        self.assertIn("Never ask for bank login credentials", prompt)


class BankCredentialIsolation(unittest.TestCase):
    def test_branch_service_cannot_read_or_replace_primary_token(self):
        with mock.patch.object(mercury.instance, "is_branch", return_value=True), \
             mock.patch.object(mercury.instance, "id", return_value="branch:test-bank"), \
             mock.patch.object(mercury.settings, "keychain_service", side_effect=lambda name: name), \
             mock.patch.object(mercury.secrets, "get", return_value="") as get:
            mercury.keychain_token()
        self.assertNotEqual(get.call_args.args[0], "vira-mercury")
        self.assertEqual(get.call_args.args[0], "vira-mercury-branch-test-bank")


if __name__ == "__main__":
    unittest.main()

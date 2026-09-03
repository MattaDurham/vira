"""Mail account management — the Setup mail card's IMAP add path.

Password never touches the JSON; the {email, host} row is deduped by
address and a graph account for the same address is left intact."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import mail


class ImapAddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accts = Path(self.tmp.name) / "mail-accounts.json"
        self.store = {}
        self._patches = [
            mock.patch.object(mail, "ACCOUNTS", self.accts),
            mock.patch.object(
                mail.secrets, "set",
                side_effect=lambda s, a, v: self.store.__setitem__((s, a), v)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_add_writes_row_and_stores_password(self):
        r = mail.add_imap_account("Me@Example.com", "imap.example.com", "pw1")
        self.assertTrue(r["added"])
        self.assertEqual(r["email"], "me@example.com")  # normalized
        rows = json.loads(self.accts.read_text(encoding="utf-8"))
        self.assertEqual(
            rows, [{"email": "me@example.com", "host": "imap.example.com"}])
        # password rode the secrets ladder, never the file
        self.assertEqual(
            self.store[(mail.keychain_service(), "me@example.com")], "pw1")
        self.assertNotIn("pw1", self.accts.read_text(encoding="utf-8"))

    def test_readd_updates_host_in_place(self):
        mail.add_imap_account("me@example.com", "old.example.com", "pw1")
        r = mail.add_imap_account("me@example.com", "new.example.com", "pw2")
        self.assertFalse(r["added"])
        rows = json.loads(self.accts.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "new.example.com")

    def test_graph_account_for_same_address_is_left_intact(self):
        self.accts.write_text(
            json.dumps([{"email": "me@example.com", "type": "graph"}]), encoding="utf-8")
        mail.add_imap_account("me@example.com", "imap.example.com", "pw")
        rows = json.loads(self.accts.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(a.get("type") == "graph" for a in rows))
        self.assertTrue(any(a.get("host") == "imap.example.com" for a in rows))

    def test_load_accounts_tolerates_wrapped_shape(self):
        self.accts.write_text(
            json.dumps({"accounts": [{"email": "a@example.com",
                                      "host": "h.example.com"}]}), encoding="utf-8")
        self.assertEqual(len(mail.load_accounts()), 1)

    def test_validation_rejects_bad_input(self):
        for bad in [("noat", "h.example.com", "p"),
                    ("a@example.com", "", "p"),
                    ("a@example.com", "h.example.com", "")]:
            with self.assertRaises(ValueError):
                mail.add_imap_account(*bad)


if __name__ == "__main__":
    unittest.main()


# ---------- reconnect, health, remove (the Config mail card, 2026-09-03) ----

class _Con:
    """A fake imaplib connection: login raises what the test says."""
    def __init__(self, fail=None):
        self.fail = fail
        self.logged_out = False

    def login(self, user, pw):
        if self.fail:
            raise self.fail
        return "OK", []

    def logout(self):
        self.logged_out = True


class ClassifyTests(unittest.TestCase):
    def test_gmail_auth_failure_reads_as_a_revoked_app_password(self):
        c = mail.classify("[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
                          "gmail")
        self.assertEqual(c["state"], "auth")
        self.assertIn("app password", c["fix"])
        # the raw text survives as evidence, never as the whole explanation
        self.assertIn("AUTHENTICATIONFAILED", c["detail"])

    def test_graph_and_generic_auth_carry_their_own_fix(self):
        self.assertIn("device login",
                      mail.classify("invalid_grant: token expired", "graph")["fix"])
        self.assertIn("current password",
                      mail.classify("LOGIN failed", "imap")["fix"])

    def test_network_no_password_ok_and_unknown(self):
        self.assertEqual(mail.classify("timed out", "imap")["state"], "network")
        self.assertEqual(mail.classify("no password in keychain (service x)",
                                       "imap")["state"], "no_password")
        self.assertEqual(mail.classify("ok", "imap")["state"], "ok")
        self.assertEqual(mail.classify("", "imap")["state"], "unknown")
        self.assertEqual(mail.classify("not connected — connect in settings",
                                       "graph")["state"], "not_connected")
        self.assertEqual(mail.classify("something odd", "imap")["state"], "error")

    def test_default_host_and_kind(self):
        self.assertEqual(mail.default_host("Me@Googlemail.com"), "imap.gmail.com")
        self.assertEqual(mail.default_host("me@example.com"), "imap.example.com")
        self.assertEqual(mail.default_host("nope"), "")
        self.assertEqual(mail.account_kind({"email": "a@googlemail.com",
                                            "host": "imap.gmail.com"}), "gmail")
        self.assertEqual(mail.account_kind({"email": "a@x.com",
                                            "type": "graph"}), "graph")
        self.assertEqual(mail.account_kind({"email": "a@x.com",
                                            "host": "imap.x.com"}), "imap")


class ProbeAndReconnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accts = root / "mail-accounts.json"
        self.state = root / "mail-state.json"
        self.health = root / "mail-health.json"
        self.store = {}
        self.deleted = []
        self._patches = [
            mock.patch.object(mail, "ACCOUNTS", self.accts),
            mock.patch.object(mail, "STATE", self.state),
            mock.patch.object(mail, "HEALTH", self.health),
            mock.patch.object(
                mail.secrets, "set",
                side_effect=lambda s, a, v: self.store.__setitem__((s, a), v)),
            mock.patch.object(
                mail.secrets, "delete",
                side_effect=lambda s, a=None: self.deleted.append((s, a))),
        ]
        for p in self._patches:
            p.start()
        self.accts.write_text(json.dumps([
            {"email": "me@example.com", "host": "imap.example.com"},
            {"email": "work@example.com", "type": "graph"}]), encoding="utf-8")
        self.state.write_text(json.dumps({
            "me@example.com": 326610,
            "graph:work@example.com": "2026-09-03T00:00:00Z",
            "graph_seen:work@example.com": ["a"]}), encoding="utf-8")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_probe_returns_the_classified_failure_and_writes_nothing(self):
        err = mail.imaplib.IMAP4.error(
            b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        with mock.patch.object(mail.imaplib, "IMAP4_SSL",
                               return_value=_Con(fail=err)):
            r = mail.probe_imap("me@googlemail.com", "", "bad")
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "auth")
        self.assertIn("app password", r["fix"])
        self.assertEqual(self.store, {})

    def test_probe_ok_fills_the_default_host(self):
        con = _Con()
        with mock.patch.object(mail.imaplib, "IMAP4_SSL", return_value=con) as m:
            r = mail.probe_imap("me@googlemail.com", "", "pw")
        self.assertTrue(r["ok"])
        self.assertEqual(r["host"], "imap.gmail.com")
        self.assertEqual(m.call_args[0][0], "imap.gmail.com")
        self.assertTrue(con.logged_out)

    def test_reconnect_refuses_a_rejected_password_and_stores_nothing(self):
        err = mail.imaplib.IMAP4.error(b"LOGIN failed")
        with mock.patch.object(mail.imaplib, "IMAP4_SSL",
                               return_value=_Con(fail=err)):
            with self.assertRaises(ValueError) as cm:
                mail.reconnect_imap("me@example.com", "bad")
        self.assertIn("Sign-in rejected", str(cm.exception))
        self.assertEqual(self.store, {})

    def test_reconnect_stores_a_verified_password_and_keeps_the_watermark(self):
        with mock.patch.object(mail.imaplib, "IMAP4_SSL", return_value=_Con()):
            r = mail.reconnect_imap("Me@Example.com", "good")
        self.assertTrue(r["verified"])
        self.assertEqual(self.store[(mail.keychain_service(), "me@example.com")],
                         "good")
        # the watermark is untouched: nothing already seen re-enters the feed
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["me@example.com"],
                         326610)
        # and the row is still exactly one row
        self.assertEqual(len(json.loads(self.accts.read_text(encoding="utf-8"))), 2)

    def test_reconnect_refuses_an_unknown_or_graph_address(self):
        with self.assertRaises(ValueError):
            mail.reconnect_imap("nobody@example.com", "pw", verify=False)
        with self.assertRaises(ValueError):
            mail.reconnect_imap("work@example.com", "pw", verify=False)

    def test_reconnect_unverified_updates_host_in_place(self):
        r = mail.reconnect_imap("me@example.com", "pw", host="new.example.com",
                                verify=False)
        self.assertFalse(r["verified"])
        rows = json.loads(self.accts.read_text(encoding="utf-8"))
        self.assertEqual([a["host"] for a in rows if "host" in a],
                         ["new.example.com"])

    def test_remove_takes_row_secret_watermark_and_health(self):
        self.health.write_text(json.dumps({
            "me@example.com": {"state": "auth"},
            "graph:work@example.com": {"state": "ok"}}), encoding="utf-8")
        r = mail.remove_account("me@example.com")
        self.assertEqual(r["removed"], 1)
        rows = json.loads(self.accts.read_text(encoding="utf-8"))
        self.assertEqual(rows, [{"email": "work@example.com", "type": "graph"}])
        self.assertIn((mail.keychain_service(), "me@example.com"), self.deleted)
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("me@example.com", st)
        self.assertIn("graph:work@example.com", st)   # the other account's kept
        self.assertEqual(list(json.loads(self.health.read_text(encoding="utf-8"))),
                         ["graph:work@example.com"])

    def test_remove_graph_deletes_the_refresh_token_and_graph_state(self):
        from server import msgraph
        r = mail.remove_account("work@example.com", kind="graph")
        self.assertEqual(r["removed"], 1)
        self.assertIn((mail.settings.keychain_service(msgraph.KEYCHAIN_SERVICE),
                       "work@example.com"), self.deleted)
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(set(st), {"me@example.com"})
        self.assertEqual(mail.remove_account("work@example.com")["removed"], 0)

    def test_summary_and_accounts_view_read_the_health_snapshot(self):
        self.health.write_text(json.dumps({
            "me@example.com": {"status": "[AUTHENTICATIONFAILED] x",
                               "state": "auth", "checked_at": 1e12,
                               "ok_at": None, "fail_since": 1e12 - 100},
            "graph:work@example.com": {"status": "ok", "state": "ok",
                                       "checked_at": 1e12, "ok_at": 1e12}}), encoding="utf-8")
        s = mail.summary()
        self.assertEqual((s["accounts"], s["ok"], s["failing"]), (2, 1, 1))
        self.assertEqual(s["attention"], "me@example.com needs attention")
        with mock.patch.object(mail.msgraph if hasattr(mail, "msgraph") else
                               __import__("server.msgraph", fromlist=["x"]),
                               "connected", return_value=True):
            v = mail.accounts_view(poll_seconds=60)
        by = {r["email"]: r for r in v["accounts"]}
        self.assertEqual(by["me@example.com"]["state"], "auth")
        self.assertEqual(by["me@example.com"]["label"], "IMAP")
        self.assertEqual(by["work@example.com"]["label"], "Microsoft 365")
        self.assertTrue(by["work@example.com"]["signed_in"])
        self.assertEqual(v["summary"]["failing"], 1)

    def test_accounts_view_marks_an_old_snapshot_stale_and_live_health_wins(self):
        import time
        self.health.write_text(json.dumps({
            "me@example.com": {"status": "ok", "state": "ok",
                               "checked_at": time.time() - 3600}}), encoding="utf-8")
        with mock.patch("server.msgraph.connected", return_value=False):
            v = mail.accounts_view(poll_seconds=60)
        me = next(r for r in v["accounts"] if r["email"] == "me@example.com")
        self.assertTrue(me["stale"])
        live = {"me@example.com": {"status": "timed out", "state": "network",
                                   "checked_at": time.time()}}
        with mock.patch("server.msgraph.connected", return_value=False):
            v = mail.accounts_view(live, poll_seconds=60)
        me = next(r for r in v["accounts"] if r["email"] == "me@example.com")
        self.assertEqual(me["state"], "network")
        self.assertFalse(me["stale"])
        # a Graph row whose refresh token is gone reads not-signed-in even
        # with no health at all
        work = next(r for r in v["accounts"] if r["email"] == "work@example.com")
        self.assertEqual(work["state"], "not_connected")

    def test_a_row_with_no_health_is_neither_ok_nor_failing(self):
        s = mail.summary({})
        self.assertEqual((s["accounts"], s["ok"], s["failing"]), (2, 0, 0))
        self.assertEqual(s["attention"], "")


class WatcherHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accts = root / "mail-accounts.json"
        self.health = root / "mail-health.json"
        self._patches = [
            mock.patch.object(mail, "ACCOUNTS", self.accts),
            mock.patch.object(mail, "STATE", root / "mail-state.json"),
            mock.patch.object(mail, "HEALTH", self.health),
        ]
        for p in self._patches:
            p.start()
        self.accts.write_text(json.dumps(
            [{"email": "me@example.com", "host": "imap.example.com"}]), encoding="utf-8")
        self.w = mail.MailWatcher(None, poll_seconds=60)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_a_failure_then_a_clean_poll_reads_ok_not_last_cycles_error(self):
        acct = {"email": "me@example.com", "host": "imap.example.com"}
        with mock.patch.object(self.w, "_poll_account",
                               side_effect=RuntimeError("LOGIN failed")):
            h = self.w._poll_one(acct)
        self.assertEqual(h["state"], "auth")
        self.assertIsNotNone(h["fail_since"])
        first_fail = h["fail_since"]
        with mock.patch.object(self.w, "_poll_account",
                               side_effect=RuntimeError("LOGIN failed")):
            h = self.w._poll_one(acct)
        self.assertEqual(h["fail_since"], first_fail)   # since the FIRST failure
        with mock.patch.object(self.w, "_poll_account", return_value=None):
            h = self.w._poll_one(acct)
        self.assertEqual(h["state"], "ok")
        self.assertEqual(self.w.status["me@example.com"], "ok")
        self.assertIsNone(h["fail_since"])
        self.assertIsNotNone(h["ok_at"])

    def test_poll_account_named_states_survive(self):
        acct = {"email": "me@example.com", "host": "imap.example.com"}

        def no_pw(a):
            self.w.status[a["email"]] = "no password in keychain (service s)"
        with mock.patch.object(self.w, "_poll_account", side_effect=no_pw):
            h = self.w._poll_one(acct)
        self.assertEqual(h["state"], "no_password")

    def test_check_account_polls_one_row_now_and_writes_the_snapshot(self):
        with mock.patch.object(self.w, "_poll_account", return_value=None):
            h = self.w.check_account("Me@Example.com")
        self.assertEqual(h["state"], "ok")
        snap = json.loads(self.health.read_text(encoding="utf-8"))
        self.assertEqual(snap["me@example.com"]["state"], "ok")
        with self.assertRaises(ValueError):
            self.w.check_account("nobody@example.com")

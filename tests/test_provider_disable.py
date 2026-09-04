"""The provider disable switch - config `providers_disabled`.

WHY THIS FILE EXISTS. The switch was built for the model-parity eval
(docs/model-parity-eval-2026-09-04.md): disable every provider but one,
drive every surface, and know that the one provider ran. That only works if
a disabled provider is REFUSED everywhere it could otherwise be reached, by
name, instead of a fallback ladder quietly rerouting to whichever provider
is still connected. Five ladders reroute silently without it:

  - agentbackend.default_session_provider  -> anthropic
  - agentbackend.session_provider (a pin)  -> ran the pinned provider
  - suggest.effective_backend              -> the go-to, whatever it is
  - virachat.chat_provider                 -> the next connected provider
  - models.active / options["active"]      -> the first connected provider

Each has a case here. The JOIN - the real session.Sessions.launch refusing
BEFORE a job dir or a ledger row exists - is the one that matters most,
because a refusal that lands after the dispatch is recorded is a dead job
with a note, not a refusal (the branch-guard-wiring lesson).

Every case pins settings.raw, so no test reads this machine's config.

Run: .venv/bin/python -m unittest tests.test_provider_disable
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import agentbackend, aihealth, models, session, settings
from server import suggest, virachat


def _all_signed_in():
    """Every provider reads connected at the auth layer, with no shell-out:
    the switch is the only thing that can make one unusable here."""
    return [
        mock.patch.object(models, "find_binary", return_value="/x/bin"),
        mock.patch.object(models, "api_key", return_value="k"),
        mock.patch.object(models, "_probe_auth",
                          return_value=(models.SIGNED_IN, "ok")),
        mock.patch.object(models, "cli_models", return_value=[]),
        mock.patch.object(models, "catalog",
                          return_value={"cli": [], "api": [],
                                        "cli_detail": "", "api_detail": ""}),
    ]


class _Pinned(unittest.TestCase):
    """settings.raw pinned to a dict the test owns; auth stubbed green."""

    def setUp(self):
        self.cfg = {"ai_provider": "anthropic", "providers_disabled": []}
        for p in _all_signed_in():
            p.start()
        mock.patch.object(settings, "raw", lambda: dict(self.cfg)).start()
        self.addCleanup(mock.patch.stopall)

    def disable(self, *pids):
        self.cfg["providers_disabled"] = list(pids)


class TheProbeFoldsTheSwitchIn(_Pinned):
    def test_a_disabled_provider_is_not_connected_but_its_auth_survives(self):
        self.disable("google")
        rec = models.probe("google")
        self.assertFalse(rec["connected"])
        self.assertTrue(rec["disabled"])
        # The auth verdict is untouched, so Config can say "disabled by
        # you" instead of "not signed in".
        self.assertEqual(rec["auth"], models.SIGNED_IN)
        self.assertIn("disabled in Config", rec["action"])

    def test_an_enabled_provider_is_untouched(self):
        rec = models.probe("google")
        self.assertTrue(rec["connected"])
        self.assertFalse(rec["disabled"])
        self.assertEqual(rec["action"], "")

    def test_connected_excludes_it_and_discover_keeps_the_row(self):
        self.disable("openai", "xai")
        self.assertEqual({p["id"] for p in models.connected()},
                         {"anthropic", "google"})
        self.assertEqual(len(models.discover()), len(models.PROVIDERS))

    def test_unknown_ids_and_non_lists_disable_nothing(self):
        self.cfg["providers_disabled"] = ["nosuch", "  GOOGLE "]
        self.assertEqual(models.disabled_providers(), {"google"})
        self.cfg["providers_disabled"] = "google"
        self.assertEqual(models.disabled_providers(), set())


class ADisabledGoToIsNobody(_Pinned):
    """active() and options()["active"] used to fall to the FIRST connected
    provider. With the go-to disabled the call itself refuses, so the record
    must not claim another provider will answer."""

    def test_active_is_none_even_with_others_connected(self):
        self.cfg["ai_provider"] = "google"
        self.disable("google")
        self.assertIsNone(models.active())

    def test_options_active_is_empty_and_rows_carry_disabled(self):
        self.cfg["ai_provider"] = "google"
        self.disable("google")
        out = models.options(refresh=True)
        self.assertEqual(out["active"], "")
        by = {p["id"]: p for p in out["providers"]}
        self.assertTrue(by["google"]["disabled"])
        self.assertFalse(by["google"]["connected"])
        self.assertFalse(by["anthropic"]["disabled"])

    def test_an_enabled_go_to_still_reads_active(self):
        self.cfg["ai_provider"] = "google"
        self.disable("xai")
        self.assertEqual(models.active()["id"], "google")
        self.assertEqual(models.options(refresh=True)["active"], "google")


class SessionsRefuseByName(_Pinned):
    def test_an_explicit_pin_on_a_disabled_provider_refuses(self):
        self.disable("google")
        with self.assertRaises(models.ProviderDisabled) as cm:
            agentbackend.session_provider(provider="google")
        self.assertIn("Gemini", str(cm.exception))
        self.assertIn("providers_disabled", str(cm.exception))

    def test_a_model_naming_a_disabled_provider_refuses(self):
        self.disable("openai")
        with self.assertRaises(models.ProviderDisabled):
            agentbackend.session_provider(model="gpt-5.6-sol")

    def test_a_disabled_go_to_refuses_instead_of_falling_to_anthropic(self):
        self.cfg["ai_provider"] = "google"
        self.disable("google")
        with self.assertRaises(models.ProviderDisabled) as cm:
            agentbackend.default_session_provider()
        self.assertIn("go-to", str(cm.exception))

    def test_the_anthropic_fallback_itself_refuses_when_disabled(self):
        # An unknown go-to used to fall through to anthropic silently.
        self.cfg["ai_provider"] = "nosuch"
        self.disable("anthropic")
        with self.assertRaises(models.ProviderDisabled):
            agentbackend.default_session_provider()

    def test_everything_else_resolves_as_before(self):
        self.cfg["ai_provider"] = "openai"
        self.disable("google")
        self.assertEqual(agentbackend.session_provider(), "openai")
        self.assertEqual(agentbackend.session_provider(provider="xai"), "xai")
        self.assertEqual(agentbackend.session_provider(model="claude-opus-5"),
                         "anthropic")


class DraftsRefuseBeforeAnyCall(_Pinned):
    def test_effective_backend_refuses_a_disabled_go_to(self):
        cfg = {"ai_backend": "cli", "ai_provider": "google"}
        self.disable("google")
        with self.assertRaises(models.ProviderDisabled):
            suggest.effective_backend(cfg)

    def test_the_refusal_never_reaches_the_health_ledger(self):
        """A disabled go-to is a choice, not a backend failure: _run must
        raise before its try, so note_failure never flips the banner."""
        cfg = dict(suggest.DEFAULTS, ai_provider="google",
                   providers_disabled=["google"])
        self.disable("google")
        with mock.patch.object(aihealth, "note_failure") as nf, \
             mock.patch.object(suggest, "_call_cli") as cli, \
             mock.patch.object(suggest, "_call_api") as api:
            with self.assertRaises(models.ProviderDisabled):
                suggest._run("hello", cfg)
        nf.assert_not_called()
        cli.assert_not_called()
        api.assert_not_called()

    def test_an_enabled_go_to_takes_the_normal_ladder(self):
        self.disable("xai")
        with mock.patch.object(aihealth, "preferred_backend",
                               return_value="cli"):
            self.assertEqual(
                suggest.effective_backend(
                    {"ai_backend": "cli", "ai_provider": "anthropic"}),
                ("anthropic", "cli"))


class ChatRefusesADisabledGoTo(_Pinned):
    def test_chat_provider_raises_rather_than_taking_the_next_connected(self):
        self.cfg["ai_provider"] = "openai"
        self.disable("openai")
        with self.assertRaises(models.ProviderDisabled):
            virachat.chat_provider()

    def test_chat_follows_an_enabled_go_to(self):
        self.cfg["ai_provider"] = "openai"
        self.disable("anthropic")
        self.assertEqual(virachat.chat_provider()[0], "openai")


class TheBannerStaysQuiet(_Pinned):
    def test_probe_reads_setup_and_probes_nothing(self):
        self.cfg["ai_provider"] = "google"
        self.disable("google")
        with mock.patch.object(aihealth, "_raw_cfg",
                               return_value={"ai_provider": "google"}), \
             mock.patch.object(aihealth, "_probe_cli") as cli, \
             mock.patch.object(aihealth, "_probe_api") as api:
            out = aihealth.probe(write=False)
        self.assertEqual(out["state"], "setup")
        self.assertIn("Gemini", out["detail"])
        self.assertIn("disabled", out["detail"])
        cli.assert_not_called()
        api.assert_not_called()


class TheJoin(_Pinned):
    """The real launch(), with the detached runner the only thing stubbed:
    a pin on a disabled provider must refuse BEFORE a job dir or a ledger
    row exists, and an enabled launch on the same machine must still land."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jobs.mkdir()
        self.rows = []
        real = subprocess.Popen

        def popen(args, *a, **kw):
            if (isinstance(args, (list, tuple)) and len(args) > 2
                    and list(args[1:3]) == ["-m", "server.runner"]):
                return mock.Mock(pid=424242)
            return real(args, *a, **kw)
        mock.patch.object(session.subprocess, "Popen", popen).start()
        mock.patch.object(session.jobfiles, "job_dir",
                          lambda jid: self.jobs / jid).start()
        mock.patch.object(session.joblog, "record_launch",
                          lambda job: self.rows.append(job)).start()
        mock.patch.object(session, "SDK_AVAILABLE", True).start()

    def test_a_pinned_disabled_provider_refuses_before_anything_is_written(self):
        self.disable("google")
        with self.assertRaises(models.ProviderDisabled):
            session.Sessions().launch("say hi", provider="google")
        self.assertEqual(list(self.jobs.iterdir()), [])
        self.assertEqual(self.rows, [])

    def test_a_disabled_go_to_refuses_a_dispatch_that_names_nothing(self):
        self.cfg["ai_provider"] = "openai"
        self.disable("openai")
        with self.assertRaises(models.ProviderDisabled):
            session.Sessions().launch("say hi")
        self.assertEqual(list(self.jobs.iterdir()), [])
        self.assertEqual(self.rows, [])

    def test_an_enabled_provider_still_launches_and_records_itself(self):
        self.cfg["ai_provider"] = "anthropic"
        self.disable("google", "xai")
        jid = session.Sessions().launch("say hi")
        spec = json.loads((self.jobs / jid / "job.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(spec["provider"], "anthropic")
        self.assertEqual(len(self.rows), 1)


class TheConfigRoute(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from server import main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfgp = Path(self.tmp.name) / "config.json"
        cfgp.write_text("{}", encoding="utf-8")
        mock.patch.object(suggest, "CONFIG_PATH", cfgp).start()
        mock.patch.object(settings, "CONFIG_PATH", cfgp).start()
        # The route refreshes the picker payload; keep that off the shell.
        mock.patch.object(models, "find_binary", return_value="").start()
        mock.patch.object(models, "api_key", return_value="").start()
        mock.patch.object(models, "cli_models", return_value=[]).start()
        mock.patch.object(models, "catalog",
                          return_value={"cli": [], "api": [],
                                        "cli_detail": "",
                                        "api_detail": ""}).start()
        self.addCleanup(mock.patch.stopall)
        self.cfgp = cfgp
        self.client = TestClient(main.app)

    def test_an_unknown_id_is_refused_not_stored(self):
        r = self.client.post("/api/config",
                             json={"providers_disabled": ["nosuch"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("nosuch", r.json()["detail"])
        self.assertNotIn("providers_disabled",
                         json.loads(self.cfgp.read_text(encoding="utf-8")))

    def test_a_valid_list_is_stored_deduped_and_sorted(self):
        r = self.client.post("/api/config",
                             json={"providers_disabled": ["xai", "google",
                                                          "xai"]})
        self.assertEqual(r.status_code, 200, r.text)
        saved = json.loads(self.cfgp.read_text(encoding="utf-8"))
        self.assertEqual(saved["providers_disabled"], ["google", "xai"])
        self.assertEqual(models.disabled_providers(), {"google", "xai"})

    def test_a_pinned_run_on_a_disabled_provider_is_a_400_not_a_429(self):
        self.client.post("/api/config", json={"providers_disabled": ["google"]})
        r = self.client.post("/api/actions/run",
                             json={"prompt": "hi", "provider": "google"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Gemini", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()

"""Module picks must reach actual sessions, including an already-open chat.

All configuration and chat stores are temporary. Session launches and
completion calls are stubbed, so these checks never invoke a model.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import jobfiles, models, modulemodels, session, settings, suggest, virachat


def choice(provider="openai", model="gpt-test", backend="cli"):
    return {"provider": provider, "backend": backend, "model": model}


class LocalConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.cfg = {"ai_provider": "anthropic", "cli_model": "sonnet",
                    "openai_cli_model": "gpt-default", "module_models": {},
                    "providers_disabled": [], "model_alias_overrides": {}}
        self.write_config()
        for patch in (mock.patch.object(suggest, "CONFIG_PATH", self.path),
                      mock.patch.object(settings, "CONFIG_PATH", self.path)):
            patch.start()
            self.addCleanup(patch.stop)

    def write_config(self):
        self.path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def pick(self, module_id, picked):
        if picked is None:
            self.cfg["module_models"].pop(module_id, None)
        else:
            self.cfg["module_models"][module_id] = picked
        self.write_config()


class SessionDefaults(LocalConfig):
    def launch(self, **kwargs):
        registry = session.Sessions()
        with mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(registry, "_spawn_runner") as spawn:
            registry.launch("A synthetic request", **kwargs)
        return spawn.call_args.args[0]

    def test_an_unpinned_launch_uses_its_module_choice(self):
        self.pick("work", choice())
        with modulemodels.scope("work"):
            launched = self.launch()
        self.assertEqual((launched["provider"], launched["model"]),
                         ("openai", "gpt-test"))

    def test_explicit_model_and_provider_each_outrank_module_defaults(self):
        self.pick("work", choice())
        with modulemodels.scope("work"):
            by_model = self.launch(model="sonnet")
            by_provider = self.launch(provider="anthropic")
        self.assertEqual((by_model["provider"], by_model["model"]),
                         ("anthropic", "sonnet"))
        self.assertEqual(by_provider["provider"], "anthropic")
        self.assertIsNone(by_provider["model"])

    def test_a_resumed_session_does_not_take_a_new_module_choice(self):
        self.pick("work", choice())
        with modulemodels.scope("work"):
            launched = self.launch(resume_session="old-conversation")
        self.assertEqual(launched["provider"], "anthropic")

    def test_selected_disabled_provider_refuses_before_spawning(self):
        self.pick("work", choice())
        self.cfg["providers_disabled"] = ["openai"]
        self.write_config()
        registry = session.Sessions()
        with modulemodels.scope("work"), \
             mock.patch.object(registry, "_spawn_runner") as spawn, \
             self.assertRaises(models.ProviderDisabled):
            registry.launch("A synthetic request")
        spawn.assert_not_called()

    def test_incompatible_completion_transport_refuses_honestly(self):
        with self.assertRaisesRegex(ValueError, "CLI connection"):
            session.module_session_model(choice(backend="api"))

    def test_an_explicit_provider_gets_its_own_unscoped_default(self):
        self.pick("work", choice("anthropic", "opus"))
        registry = session.Sessions()
        jobs = Path(self.tmp.name) / "jobs"
        with modulemodels.scope("work"), \
             mock.patch.object(session, "SDK_AVAILABLE", True), \
             mock.patch.object(jobfiles, "job_dir", side_effect=lambda jid: jobs / jid), \
             mock.patch.object(session.joblog, "record_launch"), \
             mock.patch.object(session.subprocess, "Popen", return_value=mock.Mock(pid=42)):
            jid = registry.launch("A synthetic request", provider="anthropic")
        spec = json.loads((jobs / jid / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["model_resolved"], "sonnet")


class FindChatDefaults(LocalConfig):
    def setUp(self):
        super().setUp()
        self.registry = mock.Mock()
        self.registry.launch.side_effect = ["first-job", "second-job", "third-job"]
        self.registry.say.side_effect = lambda jid, text: {"job": jid}
        self.registry.get.return_value = None
        for patch in (mock.patch.object(virachat, "STORE", Path(self.tmp.name) / "chat.json"),
                      mock.patch.object(session, "sessions", self.registry),
                      mock.patch.object(virachat.threading, "Thread"),
                      mock.patch.object(models, "connected", return_value=[{"id": "anthropic"}])):
            patch.start()
            self.addCleanup(patch.stop)

    def answer(self, answer="The first saved answer"):
        current = virachat.current()
        virachat._finish_turn(current["id"], len(current["turns"]) - 1,
                              answer, "", [], [], [], [])

    def test_first_turn_uses_find_choice_and_an_unchanged_pick_continues(self):
        self.pick("find", choice())
        virachat.send("First synthetic question")
        launch = self.registry.launch.call_args
        self.assertEqual(launch.kwargs["provider"], "openai")
        self.assertEqual(launch.kwargs["model"], "gpt-test")
        self.assertIn("vira.find first", launch.args[0])
        self.answer()
        sent = virachat.send("Follow-up")
        self.registry.launch.assert_called_once()
        self.registry.say.assert_called_once_with("first-job", "Follow-up")
        self.assertFalse(sent["turns"][-1]["model_changed"])

    def test_inherited_picker_choice_respects_legacy_chat_model(self):
        self.cfg["chat_model"] = "gpt-chat-override"
        self.pick("find", choice("anthropic", "sonnet"))
        with modulemodels.scope("find"):
            inherited = virachat.default_model_selection()
        self.assertEqual(inherited, choice(model="gpt-chat-override"))
        self.assertEqual(virachat._chat_model(), choice("anthropic", "sonnet"))

    def test_provider_change_preserves_visible_chat_and_seeds_new_session(self):
        self.pick("find", choice("anthropic", "sonnet"))
        first = virachat.send("Remember this synthetic detail")
        self.answer("A saved synthetic answer")
        self.pick("find", choice())
        sent = virachat.send("Use that detail now")
        launch = self.registry.launch.call_args
        self.assertEqual(launch.kwargs["provider"], "openai")
        self.assertIn("Remember this synthetic detail", launch.args[0])
        self.assertIn("A saved synthetic answer", launch.args[0])
        self.assertIn("CURRENT MESSAGE:\nUse that detail now", launch.args[0])
        self.assertEqual(sent["id"], first["id"])
        self.assertEqual(sent["previous_jobs"], ["first-job"])
        self.assertTrue(sent["turns"][-1]["model_changed"])
        self.assertEqual(sent["job_id"], "second-job")
        self.assertEqual(len(sent["turns"]), 2)
        self.registry.say.assert_not_called()

    def test_same_provider_model_change_starts_a_new_underlying_session(self):
        self.pick("find", choice("anthropic", "sonnet"))
        virachat.send("Question")
        self.answer()
        self.pick("find", choice("anthropic", "opus"))
        sent = virachat.send("Follow-up")
        self.assertEqual(self.registry.launch.call_args.kwargs["model"], "opus")
        self.assertTrue(sent["turns"][-1]["model_changed"])
        self.registry.say.assert_not_called()

    def test_returning_to_default_changes_the_existing_chat_on_next_turn(self):
        self.pick("find", choice())
        virachat.send("Question")
        self.answer()
        self.pick("find", None)
        sent = virachat.send("Follow-up")
        self.assertEqual(sent["model_selection"]["provider"], "anthropic")
        self.assertTrue(sent["turns"][-1]["model_changed"])

    def test_a_failed_switch_leaves_the_previous_job_and_choice_intact(self):
        self.pick("find", choice("anthropic", "sonnet"))
        virachat.send("Question")
        self.answer()
        self.pick("find", choice())
        self.registry.launch.side_effect = ValueError("Selected provider unavailable")
        sent = virachat.send("Follow-up")
        self.assertEqual(sent["job_id"], "first-job")
        self.assertEqual(sent["model_selection"]["provider"], "anthropic")
        self.assertEqual(sent["turns"][-1]["status"], "failed")
        self.assertIn("Selected provider unavailable", sent["turns"][-1]["answer"])
        self.registry.say.assert_not_called()

    def test_a_disabled_pick_cannot_continue_a_parked_chat(self):
        self.pick("find", choice())
        virachat.send("Question")
        self.answer()
        self.cfg["providers_disabled"] = ["openai"]
        self.write_config()
        sent = virachat.send("Follow-up")
        self.assertEqual(sent["turns"][-1]["status"], "failed")
        self.assertIn("disabled", sent["turns"][-1]["answer"])
        self.registry.say.assert_not_called()

    def test_legacy_chat_uses_recorded_model_to_decide_whether_to_switch(self):
        old = virachat.new()
        def seed(state):
            state["sessions"][old["id"]]["job_id"] = "legacy-job"
        virachat._mutate(seed)
        self.registry.get.return_value = {"provider": "anthropic", "model": "sonnet"}
        self.pick("find", choice())
        virachat.send("Use the new model")
        self.assertEqual(self.registry.launch.call_args.kwargs["provider"], "openai")
        self.registry.say.assert_not_called()

    def test_concept_completion_reenters_find_scope_in_the_worker(self):
        self.pick("find", choice())
        with mock.patch.object(suggest, "complete", side_effect=lambda prompt: self.check_scope()), \
             mock.patch.object(virachat.modelbudget, "split",
                               side_effect=lambda *a, **k: (self.check_scope() and (500, 100))):
            concepts, followups = virachat._concepts("synthetic-chat", "answer", [])
        self.assertEqual((concepts, followups), ([], []))
        self.assertIsNone(modulemodels.current())

    def check_scope(self):
        self.assertEqual(modulemodels.current(), "find")
        self.assertEqual(suggest.config()["_module_model_explicit"], choice())
        return "{}"

    def test_large_carried_history_is_bounded_and_marked(self):
        turns = [{"question": "Question", "answer": "x" * 100, "status": "done"}]
        with mock.patch.object(virachat.modelbudget, "context_chars", return_value=40) as budget:
            transcript, truncated = virachat._conversation_context(turns, choice())
        self.assertTrue(truncated)
        self.assertIn("Earlier text omitted", transcript)
        self.assertLess(len(transcript), 100)
        budget.assert_called_once_with("standard", "openai", "cli", model="gpt-test")

    def test_resuming_a_legacy_chat_does_not_claim_a_model_change(self):
        old = virachat.new()
        def seed(state):
            state["sessions"][old["id"]]["job_id"] = "legacy-job"
        virachat._mutate(seed)
        self.registry.say.side_effect = lambda jid, text: {"job": "resumed-job"}
        sent = virachat.send("Continue this conversation")
        self.assertEqual(sent["job_id"], "resumed-job")
        self.assertFalse(sent["turns"][-1]["model_changed"])
        self.registry.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Module picks must reach dispatch and budgeting without changing defaults."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import modelbudget, models, modulemodels, suggest


class ModuleModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps({
            "ai_provider": "anthropic", "ai_backend": "cli", "cli_model": "sonnet",
            "module_models": {"find": {"provider": "openai", "backend": "cli", "model": "test-codex"}},
        }), encoding="utf-8")
        patch = mock.patch.object(suggest, "CONFIG_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(models, "is_disabled", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_scope_routes_completion_and_budget_to_same_model(self):
        with modulemodels.scope("find"), \
                mock.patch.object(suggest, "_call_codex_cli", return_value="answer") as call, \
                mock.patch.object(models, "api_key", return_value=""), \
                mock.patch.object(modelbudget, "_store", return_value={}):
            self.assertEqual(suggest.complete("question"), "answer")
            self.assertEqual(call.call_args.args[1], "test-codex")
            cap = modelbudget.capability()
            self.assertEqual((cap["provider"], cap["backend"], cap["model"]),
                             ("openai", "cli", "test-codex"))
        self.assertEqual(suggest.config()["cli_model"], "sonnet")
        self.assertEqual(suggest.config()["ai_provider"], "anthropic")

    def test_missing_explicit_api_key_does_not_silently_use_cli(self):
        pick = {"provider": "anthropic", "backend": "api", "model": "selected-api"}
        with mock.patch.object(modulemodels, "snapshot", return_value={}):
            modulemodels.save("attention", pick)
        with modulemodels.scope("attention"), \
                mock.patch.object(models, "api_key", return_value=""), \
                mock.patch("server.aihealth.note_failure"), \
                mock.patch.object(suggest, "_call_cli") as cli:
            with self.assertRaisesRegex(RuntimeError, "needs an API key"):
                suggest.complete("question")
            cli.assert_not_called()

    def test_atomic_choice_preserves_global_and_other_module(self):
        pick = {"provider": "anthropic", "backend": "cli", "model": "haiku"}
        with mock.patch.object(modulemodels, "snapshot", return_value={}):
            modulemodels.save("feed", pick)
            modulemodels.save("find", None)
        cfg = suggest.base_config()
        self.assertEqual(cfg["cli_model"], "sonnet")
        self.assertEqual(cfg["module_models"], {"people": pick})

    def test_global_save_inside_scope_never_promotes_module_pick(self):
        with modulemodels.scope("find"):
            suggest.save_config({"timeout": 99})
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["ai_provider"], "anthropic")
        self.assertNotIn("_module_model_explicit", stored)
        self.assertEqual(stored["timeout"], 99)

    def test_scopes_are_isolated_for_concurrent_requests(self):
        async def get(module):
            with modulemodels.scope(module):
                await asyncio.sleep(0)
                return suggest.config()["ai_provider"]
        async def run():
            return await asyncio.gather(get("find"), get("people"))
        self.assertEqual(asyncio.run(run()), ["openai", "anthropic"])
        self.assertIsNone(modulemodels.current())

    def test_background_decorator_keeps_initiating_module(self):
        @modulemodels.scoped("people")
        def operation():
            return modulemodels.current()
        self.assertEqual(operation(), "people")
        with modulemodels.scope("find"):
            self.assertEqual(operation(), "find")
            with modulemodels.scope(None):
                self.assertEqual(operation(), "people")

    def test_work_choice_replaces_judge_default_but_not_action_pin(self):
        from server import judge, session
        pick = {"provider": "openai", "backend": "cli", "model": "test-codex"}
        with mock.patch.object(modulemodels, "snapshot", return_value={}):
            modulemodels.save("work", pick)
        with mock.patch.object(judge.joblog, "get_record", return_value={"status": "done"}), \
                mock.patch.object(judge, "prompt_for_job", return_value="review"), \
                mock.patch.object(session.sessions, "launch", return_value="judge-job") as launch, \
                mock.patch.object(judge.threading, "Thread"):
            judge.launch_judge("finished-job")
            self.assertIsNone(launch.call_args.kwargs["model"])
            judge.launch_judge("finished-job", model="fable")
            self.assertEqual(launch.call_args.kwargs["model"], "fable")

    def test_design_only_offers_local_image_capable_transports(self):
        self.assertEqual(modulemodels.supported_backends("design"),
                         {"anthropic": ["cli"], "openai": ["cli"]})

    def test_snapshot_keeps_inherited_default_separate_from_pick(self):
        with mock.patch.object(suggest, "effective_backend", side_effect=lambda cfg: (cfg["ai_provider"], cfg["ai_backend"])), \
                mock.patch("server.virachat.default_model_selection", return_value={"provider": "anthropic", "backend": "cli", "model": "sonnet"}):
            rows = modulemodels.snapshot()["modules"]
        find = next(row for row in rows if row["id"] == "find")
        self.assertEqual(find["default"]["model"], "sonnet")
        self.assertEqual(find["effective"]["model"], "test-codex")

    def test_invalid_module_or_transport_does_not_write(self):
        original = self.path.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown model module"):
            modulemodels.save("invented", None)
        with self.assertRaisesRegex(ValueError, "does not support"):
            modulemodels.save("find", {"provider": "openai", "backend": "api", "model": "test"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_request_route_scopes_include_current_find_chat(self):
        expected = {"/api/vira/chat": "find", "/api/find/ask": "find",
                    "/api/define/source": "find-define", "/api/brief/journal/resolve": "journal",
                    "/api/brief/narrative": "attention", "/api/reading/list/tag": "reader",
                    "/api/actions/run": "work", "/api/module-models": None,
                    "/api/findings": None}
        for path, module in expected.items():
            with self.subTest(path=path):
                self.assertEqual(modulemodels.module_for_path(path), module)

    def test_disabled_provider_refuses_explicit_module_call(self):
        with modulemodels.scope("find"), \
                mock.patch.object(models, "is_disabled", return_value=True):
            with self.assertRaises(models.ProviderDisabled):
                suggest.effective_backend(suggest.config())

    def test_saved_choice_reaches_real_find_route_and_reset_restores_default(self):
        from fastapi.testclient import TestClient
        from server import main, virachat
        client = TestClient(main.app)
        picked = {"provider": "openai", "backend": "cli", "model": "test-selected"}
        seen = []

        def answer(question):
            cfg = suggest.config()
            seen.append((question, cfg["ai_provider"], cfg["openai_cli_model"]))
            return {"answer": "Synthetic answer"}

        with mock.patch.object(modulemodels, "snapshot", return_value={"modules": []}), \
                mock.patch.object(virachat, "send", side_effect=answer), \
                mock.patch.object(virachat, "summary_rows", return_value=[]):
            self.assertEqual(client.put("/api/module-models/find", json=picked).status_code, 200)
            self.assertEqual(client.post("/api/find/ask", json={"question": "Test question"}).status_code, 200)
            self.assertEqual(seen, [("Test question", "openai", "test-selected")])
            self.assertEqual(client.delete("/api/module-models/find").status_code, 200)
            self.assertEqual(client.post("/api/find/ask", json={"question": "Default question"}).status_code, 200)
        self.assertEqual(seen[-1][1], "anthropic")
        self.assertIsNone(modulemodels.current())
        self.assertNotIn("find", suggest.base_config()["module_models"])


if __name__ == "__main__":
    unittest.main()

"""Launch review: preserve reviewed inputs and refresh provider identity."""
import asyncio
import unittest
from unittest import mock

from server import codexapp, models, orphanwork


class CodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        models._codex_discovery_cache.clear()
        models._cli_catalog_cache.clear()
        self.addCleanup(models._codex_discovery_cache.clear)
        self.addCleanup(models._cli_catalog_cache.clear)

    def test_account_pages_are_read_without_creating_a_session(self):
        rpc = mock.Mock()
        rpc.start = mock.AsyncMock()
        rpc.close = mock.AsyncMock()
        rpc.request = mock.AsyncMock(side_effect=[
            {"data": [{"model": "future-one"}], "nextCursor": "page-two"},
            {"data": [{"model": "future-two"}], "nextCursor": None}])
        with mock.patch.object(codexapp, "JsonRpcClient", return_value=rpc):
            rows = asyncio.run(codexapp.discover_models("codex", ".", {}))
        self.assertEqual(len(rows), 2)
        self.assertEqual([c.args[0] for c in rpc.request.call_args_list],
                         ["model/list", "model/list"])
        self.assertEqual(rpc.request.call_args_list[1].args[1]["cursor"], "page-two")
        rpc.close.assert_awaited_once()

    def test_rpc_is_closed_on_discovery_failure(self):
        rpc = mock.Mock(start=mock.AsyncMock(side_effect=RuntimeError("offline")),
                        close=mock.AsyncMock())
        with mock.patch.object(codexapp, "JsonRpcClient", return_value=rpc):
            with self.assertRaises(RuntimeError):
                asyncio.run(codexapp.discover_models("codex", ".", {}))
        rpc.close.assert_awaited_once()

    def test_refresh_discovers_a_new_release_and_excludes_hidden_models(self):
        discover = mock.AsyncMock(side_effect=[
            [{"model": "future-one", "displayName": "Future One"}],
            [{"model": "future-two"}, {"model": "secret", "hidden": True}]])
        with mock.patch.object(models, "find_binary", return_value="codex"), \
             mock.patch.object(codexapp, "discover_models", discover):
            first, _ = models._codex_catalog()
            self.assertEqual(models._codex_catalog()[0], first)
            second, source = models._codex_catalog(refresh=True)
        self.assertEqual(discover.await_count, 2)
        self.assertEqual([r["id"] for r in second], ["future-two"])
        self.assertIn("account catalog", source)

    def test_expired_catalog_is_discovered_again(self):
        models._codex_discovery_cache["codex"] = (-models.MODELS_TTL, [], "old")
        with mock.patch.object(models, "find_binary", return_value="codex"), \
             mock.patch.object(codexapp, "discover_models", mock.AsyncMock(return_value=[])) as discover:
            models._codex_catalog()
        discover.assert_awaited_once()

    def test_failure_names_the_fallback_and_forwards_refresh(self):
        with mock.patch.object(models, "find_binary", return_value="codex"), \
             mock.patch.object(codexapp, "discover_models", mock.AsyncMock(side_effect=OSError())), \
             mock.patch.object(models, "_codex_bundled_models", return_value=[{"id": "future"}]) as bundled:
            rows, source = models._codex_catalog(refresh=True)
        bundled.assert_called_once_with(refresh=True)
        self.assertEqual(rows[0]["id"], "future")
        self.assertIn("availability not confirmed", source)

    def test_subscription_catalog_does_not_union_api_only_models(self):
        with mock.patch.object(models, "_codex_catalog", return_value=([{"id": "account-model"}], "account")), \
             mock.patch.object(models, "_live_models", return_value=([{"id": "api-only"}], "api")):
            cat = models.catalog("openai", refresh=True)
        self.assertEqual(cat["cli"], [{"id": "account-model"}])
        self.assertEqual(cat["api"], [{"id": "api-only"}])


class ReviewedResumeTests(unittest.TestCase):
    def test_reviewed_instructions_and_settings_reach_the_runner(self):
        item = {"branch": "claude/example", "worktree": "/tmp/example"}
        with mock.patch.object(orphanwork, "_refuse_if_busy"), \
             mock.patch.object(orphanwork, "branch_subject", return_value="Example"), \
             mock.patch.object(orphanwork, "branch_about", return_value="Example work"), \
             mock.patch("server.session.sessions.launch", return_value="new-run") as launch:
            jid = orphanwork.resume(item, prompt="Inspect first.\n\nExtra instructions.",
                                    provider="openai", model="future-model",
                                    mode="manual", read_only=True)
        self.assertEqual(jid, "new-run")
        self.assertEqual(launch.call_args.args[0], "Inspect first.\n\nExtra instructions.")
        for key, value in {"cwd": "/tmp/example", "provider": "openai",
                           "model": "future-model", "mode": "manual", "read_only": True}.items():
            self.assertEqual(launch.call_args.kwargs[key], value)

    def test_empty_edited_prompt_cannot_launch(self):
        with mock.patch.object(orphanwork, "_refuse_if_busy"), \
             mock.patch("server.session.sessions.launch") as launch:
            with self.assertRaises(ValueError):
                orphanwork.resume({"worktree": "/tmp/example"}, prompt="  ")
        launch.assert_not_called()

    def test_resume_route_preserves_reviewed_inputs(self):
        from fastapi.testclient import TestClient
        from server import main
        payload = {"key": "example", "prompt": "Review then continue", "model": "future",
                   "provider": "openai", "mode": "manual", "read_only": True}
        with mock.patch.dict(main.os.environ, {}, clear=True), \
             mock.patch.object(main, "_orphan_item", return_value={"key": "example"}), \
             mock.patch.object(orphanwork, "resume", return_value="new") as resume:
            response = TestClient(main.app).post("/api/orphanwork/resume", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(resume.call_args.kwargs, {k: v for k, v in payload.items() if k != "key"})


class LaunchUiTests(unittest.TestCase):
    def test_review_lifecycle_in_javascript(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, "tests/session_launch_ui.js"], cwd=root,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

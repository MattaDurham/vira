"""Ambiguous native captures ask before any write, through the session channel."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import settings, vaultwrite, viratools


class VaultRoutingDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.specs = []
        for sid, name in (("primary", "Research"), ("work", "Work")):
            root = self.root / sid
            root.mkdir()
            self.specs.append({"id": sid, "name": name, "root": root,
                               **vaultwrite.policy({"write_enabled": True,
                                                    "write_dirs": ["inbox"],
                                                    "purpose": name + " notes"})})
        self.default = ""
        for patcher in (
                mock.patch.object(vaultwrite, "_specs", side_effect=lambda: self.specs),
                mock.patch.object(settings, "get", side_effect=lambda key, default=None:
                                  self.default if key == "vault_default_destination" else default),
                mock.patch.object(viratools, "_ASK", None),
                mock.patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        writer = mock.patch.object(vaultwrite, "write_note", return_value={
            "source_id": "work", "path": "@work/inbox/synthetic.md", "sha256": "synthetic"})
        self.write = writer.start()
        self.addCleanup(writer.stop)
        self.args = {"title": "A research question", "text": "Synthetic material."}

    async def capture(self, **kwargs):
        result = await viratools.invoke("vault_capture", self.args, **kwargs)
        return json.loads(result["content"][0]["text"])

    async def test_ambiguous_capture_asks_with_filing_modes_and_never_writes(self):
        async def answer(question, options, allow_text):
            self.write.assert_not_called()
            self.assertIn("Research (primary", question)
            self.assertIn("Work (work", question)
            self.assertTrue(allow_text)
            self.assertEqual(len(options), 6)
            self.assertEqual({o["label"] for o in options}, {
                "Save only in Research", "Save only in Work", "Split by topic",
                "Keep references in both", "Duplicate in both", "Leave unsaved"})
            return "Split by topic"

        callback = mock.AsyncMock(side_effect=answer)
        result = await self.capture(ask_owner=callback)
        callback.assert_awaited_once()
        self.assertFalse(result["saved"])
        self.assertEqual(result["owner_decision"], "Split by topic")
        self.assertEqual(result["status"], "vault_choice_received")
        self.write.assert_not_called()

    async def test_no_channel_preserves_a_structured_unsaved_choice(self):
        result = await self.capture()
        self.assertEqual(result["status"], "needs_vault_choice")
        self.assertNotIn("owner_decision", result)
        self.assertFalse(result["saved"])
        self.write.assert_not_called()

    async def test_owner_choice_requires_explicit_followthrough(self):
        callback = mock.AsyncMock(return_value="Save only in Work")
        await self.capture(ask_owner=callback)
        self.write.assert_not_called()
        result = await viratools.invoke("vault_capture", self.args | {"destination": "work"},
                                       ask_owner=callback)
        self.assertEqual(json.loads(result["content"][0]["text"])["source_id"], "work")
        self.assertEqual(self.write.call_args.args[0]["id"], "work")
        callback.assert_awaited_once()

    async def test_cancel_and_unanswered_questions_never_write(self):
        for answer in ("Leave unsaved", "No answer received. Stop and report."):
            with self.subTest(answer=answer):
                result = await self.capture(ask_owner=mock.AsyncMock(return_value=answer))
                self.assertFalse(result["saved"])
                self.assertEqual(result["owner_decision"], answer)
        self.write.assert_not_called()

    async def test_many_vaults_remain_named_without_truncating_filing_modes(self):
        for i in range(6):
            sid = f"extra-{i}"
            root = self.root / sid
            root.mkdir()
            self.specs.append({**self.specs[1], "id": sid, "name": f"Extra {i}", "root": root})
        result = await self.capture()
        self.assertLessEqual(len(result["options"]), 6)
        self.assertEqual(result["options"][0]["label"], "Choose one vault")
        for spec in self.specs:
            self.assertIn(f"{spec['name']} ({spec['id']}", result["question"])
        self.assertIn("Duplicate in both", {o["label"] for o in result["options"]})
        self.write.assert_not_called()

    async def test_exact_context_ambiguity_asks_even_with_a_default(self):
        self.default = "primary"
        for spec in self.specs:
            spec["contexts"] = ["shared"]
        callback = mock.AsyncMock(return_value="Leave unsaved")
        self.args["context"] = "shared"
        await self.capture(ask_owner=callback)
        callback.assert_awaited_once()
        self.write.assert_not_called()

    async def test_invalid_explicit_destinations_remain_errors_without_question(self):
        callback = mock.AsyncMock()
        for policy, expected in (({"write_enabled": False}, "read-only"),
                                 ({"model_exposure": False}, "model access"),
                                 ({"root": self.root / "missing"}, "disconnected")):
            with self.subTest(policy=policy), mock.patch.dict(self.specs[1], policy):
                result = await viratools.invoke("vault_capture", self.args | {"destination": "work"},
                                               ask_owner=callback)
                self.assertIn(expected, result["content"][0]["text"])
        result = await viratools.invoke("vault_capture", self.args | {"destination": "unknown"},
                                       ask_owner=callback)
        self.assertIn("unknown", result["content"][0]["text"])
        callback.assert_not_awaited()
        self.write.assert_not_called()

    async def test_hidden_vault_metadata_never_enters_the_question(self):
        self.specs[1]["model_exposure"] = False
        result = await self.capture()
        self.assertNotIn("Work", result["question"])
        self.assertNotIn("work", {s["id"] for s in result["destinations"]})
        self.assertNotIn("Duplicate in both", {o["label"] for o in result["options"]})
        self.write.assert_not_called()

    async def test_sdk_bound_channel_and_normal_ask_owner_still_work(self):
        callback = mock.AsyncMock(return_value="Leave unsaved")
        with mock.patch.object(viratools, "_ASK", callback):
            await self.capture()
            result = await viratools.invoke("ask_owner", {
                "question": "Choose a method", "options": "A :: First method|B :: Second method",
                "allow_text": "false"})
        self.assertEqual(result["content"][0]["text"], "Leave unsaved")
        self.assertEqual(callback.await_args.args,
                         ("Choose a method", [{"label": "A", "description": "First method"},
                                              {"label": "B", "description": "Second method"}], False))

    async def test_concurrent_callbacks_and_failures_do_not_leak_channels(self):
        async def first(*args):
            await asyncio.sleep(0)
            return "First decision"

        async def second(*args):
            await asyncio.sleep(0)
            return "Second decision"

        results = await asyncio.gather(self.capture(ask_owner=first), self.capture(ask_owner=second))
        self.assertEqual([r["owner_decision"] for r in results], ["First decision", "Second decision"])
        callback = mock.AsyncMock(side_effect=RuntimeError("channel disconnected"))
        with self.assertRaisesRegex(RuntimeError, "channel disconnected"):
            await self.capture(ask_owner=callback)
        self.assertIsNone(viratools._OWNER_CHANNEL.get())
        self.assertEqual((await self.capture())["status"], "needs_vault_choice")
        self.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()

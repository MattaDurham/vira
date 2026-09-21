"""Synthetic evaluation contracts; no model, personal stores, or live server."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from server import answer_eval_guard

SPEC = importlib.util.spec_from_file_location("answer_eval", Path(__file__).resolve().parents[1] / "scripts" / "answer_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


class FixtureScoring(unittest.TestCase):
    def setUp(self):
        self.fixtures = evaluation.load_fixtures()

    def test_all_six_gold_answers_pass_without_models(self):
        self.assertEqual(len(self.fixtures["cases"]), 6)
        for case in self.fixtures["cases"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(evaluation.score(case, case["reference"], self.fixtures)["passed"])

    def test_correct_words_with_wrong_citation_do_not_pass(self):
        case = self.fixtures["cases"][0]
        answer = copy.deepcopy(case["reference"])
        answer["claims"][0]["evidence"] = [{"source_id": "travel", "quote": "The team discussed a trip to Harbor City."}]
        self.assertEqual(evaluation.score(case, answer, self.fixtures)["coverage"], 0)

    def test_correct_source_with_unrelated_exact_excerpt_does_not_support_claim(self):
        case = self.fixtures["cases"][0]
        answer = copy.deepcopy(case["reference"])
        answer["claims"][0]["evidence"][0]["quote"] = "Record date: 2026-02-01"
        self.assertEqual(evaluation.score(case, answer, self.fixtures)["coverage"], 0)

    def test_invented_quote_and_excluded_source_fail(self):
        case = self.fixtures["cases"][0]
        for evidence in ({"source_id": "meeting", "quote": "starts at 10:30"},
                         {"source_id": "excluded", "quote": "Canary: NEVER-USE-THIS-EVIDENCE."}):
            answer = copy.deepcopy(case["reference"])
            answer["claims"][0]["evidence"] = [evidence]
            result = evaluation.score(case, answer, self.fixtures)
            self.assertFalse(result["passed"])
            self.assertTrue(result["citation_errors"])

    def test_derived_copies_do_not_satisfy_independent_original_sources(self):
        case = self.fixtures["cases"][2]
        answer = copy.deepcopy(case["reference"])
        answer["claims"][0]["evidence"] = answer["claims"][0]["evidence"][:1] * 3
        result = evaluation.score(case, answer, self.fixtures)
        self.assertFalse(result["passed"])
        self.assertEqual(result["supported_expected"], 2)

    def test_extra_unmapped_claim_requires_review(self):
        case = self.fixtures["cases"][0]
        answer = copy.deepcopy(case["reference"])
        answer["claims"].append({"text": "Everyone prefers morning meetings.", "evidence": []})
        self.assertEqual(evaluation.score(case, answer, self.fixtures)["unmapped_claims"], 1)

    def test_missing_evidence_requires_insufficient_flag(self):
        case = self.fixtures["cases"][4]
        answer = dict(case["reference"], insufficient=False)
        self.assertFalse(evaluation.score(case, answer, self.fixtures)["passed"])

    def test_unstructured_and_malformed_answers_do_not_pass(self):
        self.assertIsNone(evaluation.parse_answer("I will search the records."))
        self.assertFalse(evaluation.score(self.fixtures["cases"][0], None, self.fixtures)["passed"])
        self.assertEqual(evaluation.parse_answer('preface {"answer":"x","claims":[]}')["answer"], "x")


class ReceiptsAndIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = evaluation.materialize(self.root / "evaluation", "test-model", "high")
        self.manifest_path = self.root / "evaluation" / "manifest.json"

    def test_materialized_model_corpus_omits_gold_and_excluded_records(self):
        root = Path(self.manifest["corpus_root"])
        body = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())
        self.assertNotIn("NEVER-USE-THIS-EVIDENCE", body)
        self.assertNotIn('"expected"', body)
        self.assertNotIn('"reference"', body)
        self.assertEqual(evaluation.checked_manifest(self.manifest_path)["corpus_hash"], self.manifest["corpus_hash"])

    def test_changed_corpus_is_refused(self):
        (Path(self.manifest["corpus_root"]) / "extra.md").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corpus changed"):
            evaluation.checked_manifest(self.manifest_path)

    def test_rehashed_arbitrary_corpus_is_not_trusted_as_synthetic(self):
        root = Path(self.manifest["corpus_root"])
        (root / "extra.md").write_text("not part of the synthetic fixture", encoding="utf-8")
        digest, files = answer_eval_guard.tree_hash(root)
        forged = dict(self.manifest, corpus_hash=digest, files=files)
        self.manifest_path.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corpus changed"):
            evaluation.checked_manifest(self.manifest_path)

    def test_source_symlink_is_refused(self):
        try:
            (Path(self.manifest["corpus_root"]) / "alias").symlink_to(self.manifest_path)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            evaluation.checked_manifest(self.manifest_path)

    def test_replay_has_no_invented_performance_and_cannot_be_compared(self):
        path = self.root / "replay.jsonl"
        results = evaluation.replay(path)
        self.assertTrue(all(r["passed"] for r in results))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(r["time_to_first_useful_s"] is None for r in rows))
        with self.assertRaisesRegex(ValueError, "not live performance"):
            evaluation.compare([path])

    def test_effective_settings_must_be_recorded_and_exact(self):
        for runtime in ({}, {"effective": {"model": "different", "effort": "high"}},
                        {"effective": {"model": "test-model", "effort": None}}):
            with self.assertRaises(ValueError):
                evaluation.validate_effective(runtime, self.manifest)

    def test_real_live_port_and_external_urls_are_refused(self):
        for url in ("http://localhost:8377", "https://example.test:8400", "http://localhost:8400/path", "http://user@localhost:8400"):
            with self.assertRaises(ValueError):
                evaluation.validate_endpoint(url)
        self.assertEqual(evaluation.validate_endpoint("http://localhost:8400/"), "http://localhost:8400")

    def test_schedule_is_balanced_and_reverses_order(self):
        runs = evaluation.plan()["runs"]
        self.assertEqual(len(runs), 48)
        self.assertEqual(runs[0]["adapter"], "vira")
        self.assertEqual(runs[24]["adapter"], "codex")

    def test_native_metadata_reads_only_the_named_rollout(self):
        sessions = self.root / "codex" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "rollout-target.jsonl").write_text(json.dumps({"type": "turn_context", "payload": {"model": "test-model", "effort": "high"}}) + "\n", encoding="utf-8")
        (sessions / "rollout-unrelated.jsonl").write_text("not valid JSON", encoding="utf-8")
        runtime = evaluation.codex_runtime("target", self.root / "codex")
        self.assertEqual(evaluation.validate_effective(runtime, self.manifest)["effort"], "high")

    def test_codex_command_pins_settings_and_disables_integrations(self):
        cmd = evaluation.codex_command("codex", self.manifest)
        self.assertIn('--ignore-user-config', cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertIn('read-only', cmd)
        self.assertIn('plugins', cmd)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', cmd)

    def test_guard_requires_actual_fixture_configuration_and_bytes(self):
        from server import modulemodels, settings
        config = {"fixture_mode": True, "vault_root": self.manifest["corpus_root"],
                  "vault_sources": [], "reader_sources": [], "chat_model": "test-model",
                  "chat_effort": "high", "crm_root": str(Path(self.manifest["sandbox_home"]) / "crm")}
        env = {"VIRA_ANSWER_EVAL_MANIFEST": str(self.manifest_path),
               "VIRA_SANDBOX": "1", "VIRA_KEYCHAIN_PREFIX": "synthetic-eval-"}
        with mock.patch.dict(os.environ, env), mock.patch.object(settings, "raw", return_value=config), \
                mock.patch.object(settings, "get", side_effect=config.get), \
                mock.patch.object(modulemodels, "selection", return_value=None) as selection, \
                mock.patch.object(Path, "home", return_value=Path(self.manifest["sandbox_home"])):
            self.assertTrue(answer_eval_guard.status()["enabled"])
            selection.return_value = {"provider": "openai", "backend": "cli", "model": "other-model"}
            self.assertFalse(answer_eval_guard.status()["enabled"])
            selection.return_value["model"] = "test-model"
            self.assertTrue(answer_eval_guard.status()["enabled"])
            config["vault_sources"] = [{"id": "other"}]
            self.assertFalse(answer_eval_guard.status()["enabled"])

    def test_vira_adapter_requires_handshake_before_any_post(self):
        args = SimpleNamespace(vira_url="http://localhost:8400", timeout=2)
        journal = evaluation.Journal(self.root / "events.jsonl")
        with mock.patch.object(evaluation, "request_json", return_value={"enabled": False, "reason": "not synthetic"}) as request:
            with self.assertRaisesRegex(ValueError, "handshake refused"):
                evaluation.vira_run(args, evaluation.load_fixtures()["cases"][0], self.manifest, journal)
        request.assert_called_once_with("http://localhost:8400/api/answer/evaluation")

    def test_vira_adapter_preserves_actual_runtime_and_does_not_invent_browser_visibility(self):
        case = evaluation.load_fixtures()["cases"][0]
        args = SimpleNamespace(vira_url="http://localhost:8400", timeout=2)
        proof = {k: self.manifest[k] for k in ("model", "effort", "corpus_hash", "output_policy_hash")}
        proof.update(enabled=True, fixture_only=True)
        turn = {"status": "done", "answer": json.dumps(case["reference"]),
                "runtime": {"effective": {"model": "test-model", "effort": "high"}},
                "receipts": [{"name": "vault_note", "outcome": "ok"}],
                "metrics": {"answer_ready_t": 100}}
        journal = evaluation.Journal(self.root / "events.jsonl")
        with mock.patch.object(evaluation, "request_json", side_effect=[proof, {"session": {"id": "synthetic-chat"}}, {"session": {"id": "synthetic-chat", "turns": [turn]}}]) as request:
            result = evaluation.vira_run(args, case, self.manifest, journal)
        self.assertTrue(result["score"]["passed"])
        self.assertIsNone(result["visible_lag_s"])
        self.assertEqual(result["scope"]["status"], "requires_review")
        body = request.call_args_list[-1].args[1]
        self.assertEqual(body["sources"], ["vault:primary"])
        self.assertEqual(body["mode"], "auto")
        self.assertNotIn("expected", body["question"])

    def test_different_effective_model_fails_even_after_matching_handshake(self):
        case = evaluation.load_fixtures()["cases"][0]
        args = SimpleNamespace(vira_url="http://localhost:8400", timeout=2)
        proof = {k: self.manifest[k] for k in ("model", "effort", "corpus_hash", "output_policy_hash")}
        proof.update(enabled=True, fixture_only=True)
        turn = {"status": "done", "runtime": {"effective": {"model": "other", "effort": "high"}}}
        with mock.patch.object(evaluation, "request_json", side_effect=[proof, {"session": {"id": "synthetic-chat"}}, {"session": {"id": "synthetic-chat", "turns": [turn]}}]):
            with self.assertRaisesRegex(ValueError, "unmatched"):
                evaluation.vira_run(args, case, self.manifest, evaluation.Journal(self.root / "events.jsonl"))

    def test_comparison_pairs_only_matching_trials_and_preserves_failures(self):
        identity = {k: self.manifest[k] for k in ("model", "effort", "corpus_hash", "output_policy_hash", "fixture_definition_hash")}
        path = self.root / "pair.jsonl"
        for adapter, latency in (("vira", 4), ("codex", 8)):
            evaluation.Journal(path).emit("run_finished", adapter=adapter, replay=False, case="direct-fact",
                cache={"state": "uncontrolled"}, repetition=1, matching=identity,
                effective={"model": "test-model", "effort": "high"},
                time_to_first_useful_s=latency, score={"passed": True})
        evaluation.Journal(path).emit("run_failed", error="synthetic failure")
        result = evaluation.compare([path])
        self.assertEqual(result["matched_pairs"], 1)
        self.assertEqual(result["failed_runs"], 1)
        self.assertEqual(result["median_paired_latency_ratio"], 0.5)
        self.assertFalse(result["superiority_established"])


if __name__ == "__main__":
    unittest.main()

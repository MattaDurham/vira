"""The context-budget seam.

Every case here exists because a literal typed once and never revisited was
the defect: define fed a model ~9,000 characters against a backend reporting
a 1,000,000-token window in its own response JSON. So the tests pin the
LADDER (what is known, what is assumed, and that an unknown degrades DOWN
rather than guessing high) and the JOINS - that suggest really asks this
module, and that the security posture the CLI path now carries is really on
the argv, not merely described in a docstring.
"""
import json
import pathlib
import unittest
from unittest import mock

from server import modelbudget as mb
from server import suggest


class Base(unittest.TestCase):
    """Rooted at a tmp store; the module reads config AND a learned store."""

    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = pathlib.Path(self.tmp.name) / "model-limits.json"
        p = mock.patch.object(mb, "STORE", self.store)
        p.start(); self.addCleanup(p.stop)
        c = mock.patch.object(mb, "_cfg", lambda: {"cli_model": "", "api_model": ""})
        c.start(); self.addCleanup(c.stop)

    def test_an_empty_fixture_has_learned_nothing(self):
        """The isolation guard: this module reads a store outside its own
        tests, so a case that quietly answered from the real machine's
        learned limits would prove nothing."""
        self.assertEqual(mb._store(), {})
        self.assertEqual(mb.capability("anthropic", "cli")["source"], "floor")


class Ladder(Base):
    def test_learned_outranks_the_floor(self):
        mb.learn("anthropic", "cli", "", 1_000_000, 64_000)
        cap = mb.capability("anthropic", "cli")
        self.assertEqual(cap["source"], "learned")
        self.assertEqual(cap["context_tokens"], 1_000_000)
        self.assertEqual(cap["max_output_tokens"], 64_000)

    def test_an_unknown_provider_degrades_down_never_up(self):
        cap = mb.capability("nobody", "api")
        self.assertEqual(cap["source"], "unknown")
        self.assertEqual(cap["context_tokens"], mb.SAFE_MIN_CONTEXT_TOKENS)
        self.assertLess(cap["context_tokens"], min(mb.FLOORS.values()))

    def test_a_floor_is_below_every_real_model(self):
        # A floor may only ever under-spend. If one of these ever exceeded a
        # real model's window the seam would build prompts that get rejected.
        for pid, tokens in mb.FLOORS.items():
            self.assertLessEqual(tokens, 200_000, pid)

    def test_learning_is_keyed_per_model_and_falls_back(self):
        mb.learn("anthropic", "cli", "opus", 500_000, 32_000)
        self.assertEqual(
            mb.capability("anthropic", "cli", "opus")["context_tokens"], 500_000)
        # A model nothing was learned for still finds the provider-level row.
        self.assertEqual(mb.capability("anthropic", "cli", "zzz")["source"], "floor")

    def test_a_junk_learn_is_ignored(self):
        mb.learn("anthropic", "cli", "", 0, 0)
        self.assertEqual(mb.capability("anthropic", "cli")["source"], "floor")


class Classes(Base):
    def test_the_classes_are_ordered(self):
        i = mb.context_chars("interactive", "anthropic", "cli")
        s = mb.context_chars("standard", "anthropic", "cli")
        d = mb.context_chars("deep", "anthropic", "cli")
        self.assertLess(i, s)
        self.assertLess(s, d)

    def test_interactive_is_capped_by_latency_not_only_by_share(self):
        """A share of a 1M window is ~126k tokens - an enormous prompt to
        build while someone watches a card open. The ceiling must bind."""
        mb.learn("anthropic", "cli", "", 1_000_000, 64_000)
        chars = mb.context_chars("interactive", "anthropic", "cli")
        ceiling = mb.CLASS_CEILING_TOKENS["interactive"] * mb.CHARS_PER_TOKEN
        self.assertLessEqual(chars, ceiling)

    def test_deep_has_no_ceiling_because_nobody_is_waiting(self):
        mb.learn("anthropic", "cli", "", 1_000_000, 64_000)
        self.assertNotIn("deep", mb.CLASS_CEILING_TOKENS)
        self.assertGreater(mb.context_chars("deep", "anthropic", "cli"), 1_000_000)

    def test_every_class_still_beats_the_literal_it_replaced(self):
        # define's old budget was 5 x 1800. Even on the conservative floor
        # with no learned limits, the seam must do better than that.
        self.assertGreater(mb.context_chars("interactive", "anthropic", "cli"), 9_000)

    def test_split_divides_the_same_total_it_reports(self):
        total, each = mb.split("standard", 8, "anthropic", "cli")
        self.assertEqual(total, mb.context_chars("standard", "anthropic", "cli"))
        self.assertLessEqual(each * 8, total + 8)

    def test_split_never_returns_a_useless_slice(self):
        _, each = mb.split("interactive", 10_000, "anthropic", "cli")
        self.assertGreaterEqual(each, 400)


class Tools(Base):
    def test_only_the_anthropic_cli_path_has_tools(self):
        self.assertTrue(mb.has_tools("anthropic", "cli"))
        for pid, backend in [("anthropic", "api"), ("openai", "cli"),
                             ("google", "api"), ("xai", "api")]:
            self.assertFalse(mb.has_tools(pid, backend), f"{pid}/{backend}")


class Transport(Base):
    def test_a_tool_result_is_bounded_by_the_transport_too(self):
        """The SDK frames one NDJSON line; exceeding it kills the session
        rather than degrading, so the transport binds before the window."""
        mb.learn("anthropic", "cli", "", 1_000_000, 64_000)
        with mock.patch.object(mb, "transport_cap", lambda: 20_000):
            self.assertLessEqual(mb.tool_result_cap(), 20_000)

    def test_the_transport_cap_reads_the_runner_not_a_literal(self):
        from server import runner
        with mock.patch.object(runner, "_max_buffer_bytes", lambda: 8 * 1024 * 1024):
            self.assertEqual(mb.transport_cap(), 4 * 1024 * 1024)

    def test_a_tool_result_still_beats_the_literal_it_replaced(self):
        self.assertGreater(mb.tool_result_cap(), 12_000)


class OutputTokens(Base):
    def test_the_api_output_cap_prefers_what_the_backend_reported(self):
        mb.learn("anthropic", "api", "", 1_000_000, 128_000)
        self.assertEqual(mb.api_output_tokens("anthropic", "api"), 128_000)

    def test_it_never_returns_zero(self):
        # The Anthropic API requires max_tokens; 0 would be a broken request.
        self.assertGreater(mb.api_output_tokens("nobody", "api"), 0)

    def test_it_beats_the_hardcoded_1500_it_replaced(self):
        self.assertGreater(mb.api_output_tokens("anthropic", "api"), 1_500)


class TheJoin(unittest.TestCase):
    """Both halves of this feature were separately correct once before and
    still did nothing, because nothing passed the value across (the branch
    guard, four days disarmed). These pin the crossing."""

    def test_suggest_owns_the_one_backend_ladder(self):
        self.assertTrue(hasattr(suggest, "effective_backend"))
        src = pathlib.Path("server/suggest.py").read_text(encoding="utf-8")
        # _run must ASK rather than carry a second copy of the ladder.
        run = src.split("def _run(")[1]
        self.assertIn("effective_backend(cfg)", run)

    def test_the_cli_call_really_pins_a_permission_mode(self):
        """Measured 2026-08-28: invoked with no flags this path inherits the
        machine's settings, and on defaultMode auto that means Write, Edit
        and Bash are permitted against absolute paths with permission_denials
        empty - verified by writing a file. 37 call sites route here and
        several carry text the owner did not write."""
        seen = {}

        class R:
            returncode = 0
            stdout = json.dumps({"result": "ok"})
            stderr = ""

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return R()

        with mock.patch.object(suggest.subprocess, "run", fake_run):
            suggest._call_cli("hi", "m", 5)
        cmd = seen["cmd"]
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
        self.assertNotIn("--allowedTools", cmd)

    def test_read_tools_are_opt_in_and_read_only(self):
        seen = {}

        class R:
            returncode = 0
            stdout = json.dumps({"result": "ok"})
            stderr = ""

        with mock.patch.object(suggest.subprocess, "run",
                               lambda cmd, **kw: (seen.update(cmd=cmd), R())[1]):
            suggest._call_cli("hi", "m", 5, tools=suggest.READ_TOOLS)
        cmd = seen["cmd"]
        self.assertIn("--allowedTools", cmd)
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        for t in allowed:
            self.assertIn(t, ("Read", "Glob", "Grep"),
                          "the opt-in must stay read-only")

    def test_the_codex_draft_path_takes_its_own_read_only_sandbox(self):
        src = pathlib.Path("server/suggest.py").read_text(encoding="utf-8")
        codex = src.split("def _call_codex_cli(")[1].split("def ")[0]
        self.assertIn('"--sandbox", "read-only"', codex)

    def test_the_learning_hook_reads_a_real_response_shape(self):
        """The CLI reports its own window; this is the rung that cannot rot."""
        learned = []
        with mock.patch.object(mb, "learn",
                               lambda *a, **k: learned.append(a)):
            suggest._learn_from_cli(
                {"modelUsage": {"claude-opus-5": {"contextWindow": 1_000_000,
                                                  "maxOutputTokens": 64_000}}},
                "opus")
        # Learned under BOTH the alias asked for and the id resolved to.
        models = {a[2] for a in learned}
        self.assertIn("opus", models)
        self.assertIn("claude-opus-5", models)

    def test_the_learning_hook_never_raises(self):
        suggest._learn_from_cli({}, "m")
        suggest._learn_from_cli({"modelUsage": "nonsense"}, "m")


class DefineUsesTheSeam(unittest.TestCase):
    def test_define_asks_for_a_budget_rather_than_carrying_one(self):
        src = pathlib.Path("server/define.py").read_text(encoding="utf-8")
        self.assertIn("modelbudget.split", src)
        self.assertNotIn("MAX_CONTEXT_CHARS", src)

    def test_the_pinned_passage_always_survives_retrieval(self):
        from server import define
        with mock.patch.object(define.vault, "search",
                               lambda q, limit=0: [{"path": f"n{i}.md",
                                                    "text": "x" * 5000}
                                                   for i in range(20)]):
            ctx = define._context("term", {"text": "THE ARTICLE", "path": "a"})
        self.assertTrue(ctx[0].get("pinned"))
        self.assertIn("THE ARTICLE", ctx[0]["text"])

    def test_no_source_behaves_exactly_as_before(self):
        from server import define
        with mock.patch.object(define.vault, "search",
                               lambda q, limit=0: [{"path": "n.md", "text": "y"}]):
            ctx = define._context("term")
        self.assertFalse(any(c.get("pinned") for c in ctx))


if __name__ == "__main__":
    unittest.main()

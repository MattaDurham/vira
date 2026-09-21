# Synthetic answer comparison

This fixture is entirely invented. It contains six cases: a direct fact, a
changed fact, recurring concerns, conflicting accounts, missing evidence, and
duplicated derived summaries. Its excluded canary must never enter the model's
corpus or answer. Source IDs, originals, families, exact passages, expected
claims, and forbidden claims are explicit in `cases.json`.

The evaluator is `scripts/answer_eval.py`, using Python's standard library.
Gold expectations stay in the test fixture. Materialization writes only allowed
source documents and a source index into a separate model workspace.

## Free checks

```sh
.venv/bin/python -m unittest tests.test_answer_eval
.venv/bin/python scripts/answer_eval.py replay --receipts /tmp/answer-replay.jsonl
.venv/bin/python scripts/answer_eval.py plan
```

Replay feeds reference answers into the scorer. It makes no model calls, runs no
harness, records null performance fields, and is rejected by `compare`. Its pass
rate is evidence about the fixture/scorer only.

## Prepare a frozen corpus

```sh
.venv/bin/python scripts/answer_eval.py materialize \
  --output /tmp/answer-eval-run-01 --model MODEL_ID --effort high
```

The output must be new: this command preserves existing runs instead of
overwriting them. `manifest.json` pins the corpus file inventory and hash, fixture
definitions, exact requested model and effort, and output policy hash. The
corpus lives in `home/vault`; gold answers and the excluded record are omitted.

Use a **separately prepared synthetic Vira sandbox**, never a review instance
cloned from personal data. This tool does not configure or start it. Its process
must have `VIRA_SANDBOX`, a dedicated `VIRA_KEYCHAIN_PREFIX`, and
`VIRA_ANSWER_EVAL_MANIFEST` pointing to this manifest. Its synthetic home must
match `sandbox_home`; set explicit fixture mode, `vault_root` to `corpus_root`,
no secondary vaults/Reader sources, a CRM location inside the synthetic home, and
explicit `chat_model`/`chat_effort` matching the manifest. Model authentication
must already be configured for that sandbox.

The read-only `/api/answer/evaluation` handshake checks those conditions and all
corpus bytes. A marker alone is insufficient. The evaluator refuses live port
8377, non-local URLs, absent handshakes, changed corpus files, and missing or
different effective model/effort. It never changes live configuration or restarts
any server. The handshake validates configured sources; it is **not an OS read
sandbox**. Review complete native tool receipts for reads outside the corpus.

## Real adapters (model calls, opt-in)

```sh
.venv/bin/python scripts/answer_eval.py run --adapter vira \
  --manifest /tmp/answer-eval-run-01/manifest.json --case direct-fact \
  --vira-url http://localhost:8400 --receipts /tmp/vira-real.jsonl \
  --allow-model-calls

.venv/bin/python scripts/answer_eval.py run --adapter codex \
  --manifest /tmp/answer-eval-run-01/manifest.json --case direct-fact \
  --codex-bin codex --receipts /tmp/codex-real.jsonl --allow-model-calls

.venv/bin/python scripts/answer_eval.py compare /tmp/vira-real.jsonl /tmp/codex-real.jsonl
```

Vira receives the real chat request with `mode=auto`, explicit fixture vault
scope, and the identical question/output policy. Its configuration is checked
before dispatch and its provider-reported effective settings after completion.
The direct adapter uses `codex exec --json`, explicit model/effort, a read-only
sandbox, no inherited user/project instructions, and disabled apps/plugins/MCP,
web and other action integrations. Shell reading remains available to examine
the fixture corpus. Native `turn_context` metadata from **only this newly
created thread** verifies effective settings; if unavailable the run fails
comparison eligibility. `--codex-home` selects the existing rollout location for
that check; it does not change credentials or the subprocess environment.

This is a controlled direct Codex CLI baseline, not an assertion that all Codex
desktop settings match. Shell read-only does not restrict all filesystem reads;
run on an isolated synthetic environment and inspect access receipts. The tool
does not claim that prompt instructions enforce read isolation.

Each JSONL event is flushed and synced locally. Receipts include settings/corpus
identity, raw provider events or Vira tool outcomes, text observations, first
supported structured claim, final availability, usage when supplied, and
scoring. Errors are preserved. The evaluator may terminate **only its own direct
Codex child** on its deadline; an observation timeout never interrupts Vira.

## Matched conditions and limits

`plan` emits 48 trials: six cases, two adapters, cold/warm preparation, and two
repetitions with reversed adapter order. Run serially to avoid contention.
Prepare a new isolated process/caches for each cold trial; warm the intended
retrieval caches before each warm trial. The evaluator does not pretend that
the second repetition is automatically warm. Use `--cache-state cold` or `warm`
with `--cache-receipt PATH`, a JSON object naming `state`, `instance_id`, and
`preparation`. These are explicit operator receipts, not independently verified
cache resets. Provider prompt-cache usage is a separate reported quantity.

The default label is `uncontrolled`. Preserve all failures and retries; choose
one run explicitly for each case/condition/repetition rather than duplicating
its trial identity. Only identical settings, corpus and policy hashes are paired.
Differences in model, effort, corpus, instructions and cache preparation must
not be attributed solely to the harness.

The deterministic scorer requires expected claim patterns, their specified
source sets, exact quotes, and relevant quoted phrases. It rejects invented
quotes, excluded/unknown sources, forbidden assertions, unsupported counts and
extra unmapped claims. It does **not** generally prove semantic entailment or
that freeform answer prose contains no additional assertions. Review anonymized
answers against the gold evidence before making quality claims.

First-useful timing is conservative: the first observed structured answer with
at least one gold-supported claim. Plain progress narration does not qualify.
Earlier useful freeform prose may need human annotation of the preserved text
events. Vira's first-nonempty-message metric is retained separately. HTTP answer
availability is not browser rendering. The tool never manufactures visibility
acknowledgments; missing browser timestamps stay null. Direct CLI has no browser
visibility receipt. Consequently `compare` provides descriptive paired latency
ratios and quality results, and always leaves broad superiority unestablished.

Suggested pilot gates: zero wrong-source or excluded-source citations; every
expected fixture claim supported; no indefinite pending turn; answer rendering
within two polling intervals of readiness; and at least 30% lower median time
to a useful answer without quality loss. These are proposed acceptance criteria,
not measured performance. A 48-trial pilot cannot establish a reliable broad
p95 claim. No live model benchmark was run when this fixture was added.

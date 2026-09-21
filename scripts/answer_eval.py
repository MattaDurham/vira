#!/usr/bin/env python3
"""Synthetic answer evaluation. Replay never represents a live benchmark.

No third-party dependencies. Real model calls require the explicit run
subcommand and --allow-model-calls. This command never edits Vira config,
restarts a server, or supplies fixture gold answers to a model.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.answer_eval_guard import expected_inventory, fixture_documents, tree_hash  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "answer_eval" / "cases.json"
POLICY = """Use only the supplied synthetic corpus. Read the relevant original records;
distinguish historical and derived sources, conflicts, and missing information.
Return a direct answer, with no report or external research. End with one JSON
object: {"answer":"...", "claims":[{"text":"one factual claim or supported
uncertainty", "evidence":[{"source_id":"ID from wiki/source-index.md", "quote":
"exact supporting text from that source"}]}], "insufficient":false}.
Every material claim must appear in claims and cite its evidence. Set
insufficient=true when the requested fact is not established. Do not invent
facts, count derived copies as independent incidents, or read other stores.
"""


def digest(value):
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_fixtures():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def parse_answer(text):
    """Find the final structured answer without trusting arbitrary prose."""
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text or ""):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("claims"), list) and "answer" in obj:
            found = obj
    return found


def score(case, answer, fixtures):
    """Deterministic fixture rubric, NOT a general semantic entailment judge."""
    sources = {s["id"]: s for s in fixtures["sources"]}
    claims = answer.get("claims") or [] if isinstance(answer, dict) else []
    expected = case["expected"]
    supported, mapped, errors = set(), set(), []
    cited = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            errors.append("malformed claim")
            continue
        text = str(claim.get("text") or "")
        valid = set()
        quotes = {}
        for evidence in claim.get("evidence") or []:
            if not isinstance(evidence, dict):
                errors.append("malformed evidence")
                continue
            sid, quote = evidence.get("source_id"), evidence.get("quote")
            cited.add(str(sid))
            source = sources.get(sid)
            if source is None or source.get("excluded"):
                errors.append("unknown or excluded citation: " + str(sid))
            elif not isinstance(quote, str) or not quote.strip() or quote not in source["text"]:
                errors.append("quote does not match source: " + str(sid))
            else:
                valid.add(sid)
                quotes.setdefault(sid, []).append(quote)
        for n, gold in enumerate(expected):
            if re.search(gold["pattern"], text, re.I | re.S):
                mapped.add(index)
                supported_quotes = all(re.search(gold["quote_pattern"], "\n".join(quotes.get(sid, [])), re.I | re.S)
                                       for sid in gold["sources"])
                if set(gold["sources"]).issubset(valid) and supported_quotes:
                    supported.add(n)
    body = json.dumps(answer or {}, ensure_ascii=False)
    forbidden = [p for p in case.get("forbidden", []) + ["NEVER-USE-THIS-EVIDENCE", "987654"]
                 if re.search(p, body, re.I | re.S)]
    unmapped = len(claims) - len(mapped)
    insufficient_ok = isinstance(answer, dict) and answer.get("insufficient") is case.get("insufficient", False)
    coverage = len(supported) / len(expected)
    return {"supported_expected": len(supported), "expected": len(expected),
            "coverage": coverage, "citation_errors": errors, "unmapped_claims": unmapped,
            "forbidden_matches": forbidden, "insufficient_correct": insufficient_ok,
            "cited_sources": sorted(cited),
            "passed": coverage == 1 and not errors and not unmapped and not forbidden and insufficient_ok,
            "rubric": "synthetic pattern + exact quote + expected source set; human review still required"}


class Journal:
    def __init__(self, path, run_id=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex
        self.started = time.monotonic()

    def emit(self, event, **fields):
        row = {"version": 1, "run_id": self.run_id, "event": event,
               "at_unix": time.time(), "elapsed_s": time.monotonic() - self.started, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row


def materialize(output, model, effort):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("output already exists; choose a new directory to preserve prior runs")
    fixtures = load_fixtures()
    home = output / "home"
    corpus = home / "vault"
    corpus.mkdir(parents=True)
    for name, body in fixture_documents().items():
        path = corpus / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    corpus_hash, files = tree_hash(corpus)
    manifest = {"version": 1, "fixture_only": True, "sandbox_home": str(home),
                "corpus_root": str(corpus), "corpus_hash": corpus_hash, "files": files,
                "fixture_definition_hash": digest(fixtures), "model": model, "effort": effort,
                "output_policy_hash": digest(POLICY)}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def checked_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if not manifest.get("fixture_only") or manifest.get("output_policy_hash") != digest(POLICY):
        raise ValueError("manifest is not for this fixture output policy")
    if manifest.get("fixture_definition_hash") != digest(load_fixtures()):
        raise ValueError("fixture definitions changed after materialization")
    actual, files = tree_hash(manifest["corpus_root"])
    if actual != manifest["corpus_hash"] or files != manifest["files"] or files != expected_inventory():
        raise ValueError("corpus changed after materialization")
    return manifest


def validate_effective(runtime, manifest):
    effective = (runtime or {}).get("effective") or {}
    if effective.get("model") != manifest["model"] or effective.get("effort") != manifest["effort"]:
        raise ValueError("effective model/effort absent or unmatched; this is not a matched run")
    return effective


def prompt(case):
    return POLICY + "\nQUESTION:\n" + case["question"]


def request_json(url, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def validate_endpoint(url):
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != "localhost" or not parts.port
            or parts.port == 8377 or parts.path not in ("", "/") or parts.query or parts.fragment
            or parts.username or parts.password):
        raise ValueError("Vira evaluation requires http://localhost:<non-live-port>")
    return url.rstrip("/")


def vira_run(args, case, manifest, journal):
    base = validate_endpoint(args.vira_url)
    proof = request_json(base + "/api/answer/evaluation")
    if not proof.get("enabled") or not proof.get("fixture_only"):
        raise ValueError("Vira fixture handshake refused: " + str(proof.get("reason")))
    for key in ("model", "effort", "corpus_hash", "output_policy_hash"):
        if proof.get(key) != manifest[key]:
            raise ValueError("Vira fixture handshake mismatch: " + key)
    journal.emit("fixture_handshake", proof=proof)
    session = request_json(base + "/api/vira/chat/new", {})["session"]
    sid = session["id"]
    response = request_json(base + "/api/vira/chat", {
        "question": prompt(case), "session_id": sid, "mode": "auto", "sources": ["vault:primary"]})
    deadline = time.monotonic() + args.timeout
    seen, useful = set(), None
    while True:
        session = response["session"]
        turn = session["turns"][-1]
        for item in turn.get("messages") or []:
            identity = digest(item)
            if identity not in seen:
                seen.add(identity)
                journal.emit("text_observed", phase=item.get("phase"), text=item.get("text"))
                parsed = parse_answer(item.get("text"))
                if useful is None and parsed and score(case, parsed, load_fixtures())["supported_expected"]:
                    useful = time.monotonic() - journal.started
                    journal.emit("first_useful_text", basis="fixture-supported structured claim")
        for receipt in turn.get("receipts") or []:
            identity = digest(receipt)
            if identity not in seen:
                seen.add(identity)
                journal.emit("tool_outcome", receipt=receipt)
        if turn.get("status") in ("done", "failed"):
            effective = validate_effective(turn.get("runtime"), manifest)
            answer = parse_answer(turn.get("answer"))
            result = score(case, answer, load_fixtures())
            observed = time.monotonic() - journal.started
            useful = useful if useful is not None else observed if result["supported_expected"] else None
            metrics = turn.get("metrics") or {}
            journal.emit("final_ready", provider_metrics=metrics, observed_ready_s=observed)
            return {"answer": answer, "score": result, "effective": effective,
                    "time_to_first_useful_s": useful, "final_observed_s": observed,
                    "visible_lag_s": metrics.get("visible_answer_lag_s"),
                    "visibility_basis": "browser receipt" if metrics.get("answer_visible_t") else "not observed; HTTP polling is not browser rendering",
                    "scope": {"status": "requires_review", "basis": "validated configured corpus; inspect complete native tool receipts for other reads"},
                    "job_id": turn.get("job_id"), "chat_id": sid, "usage": turn.get("usage")}
        if time.monotonic() >= deadline:
            raise TimeoutError("evaluation observer timed out; Vira session was not canceled or restarted")
        time.sleep(0.25)
        response = request_json(base + "/api/vira/chat?" + urlencode({"session_id": sid}))


def codex_runtime(thread_id, codex_home):
    """Read only this generated run's metadata, never unrelated transcript bodies."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", thread_id or ""):
        raise ValueError("Codex did not return a valid thread identity")
    paths = list((Path(codex_home) / "sessions").rglob("*" + thread_id + ".jsonl"))
    if len(paths) != 1:
        raise ValueError("exact Codex rollout is unavailable; effective settings cannot be verified")
    runtime = None
    with paths[0].open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("type") == "turn_context":
                payload = row.get("payload") or {}
                runtime = {"effective": {"model": payload.get("model"),
                           "effort": payload.get("effort") or payload.get("reasoning_effort"),
                           "source": "native turn_context", "provider": "openai"}}
    return runtime


def codex_command(binary, manifest):
    command = [binary, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
               "--sandbox", "read-only", "--color", "never", "--model", manifest["model"],
               "-C", manifest["corpus_root"], "-c", 'approval_policy="never"',
               "-c", "model_reasoning_effort=" + json.dumps(manifest["effort"]),
               "-c", 'web_search="disabled"', "-c", "mcp_servers={}", "-c", "project_doc_max_bytes=0"]
    for feature in ("apps", "plugins", "hooks", "memories", "multi_agent", "browser_use", "computer_use"):
        command += ["--disable", feature]
    return command + ["-"]


def codex_run(args, case, manifest, journal):
    events = queue.Queue()
    command = codex_command(args.codex_bin, manifest)
    journal.emit("adapter_command", argv=command)
    # stderr stays local and cannot fill a PIPE while stdout is consumed.
    error_path = journal.path.with_name(journal.run_id + ".stderr.log")
    with error_path.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, text=True, encoding="utf-8")
        process.stdin.write(prompt(case))
        process.stdin.close()
        def collect():
            try:
                for line in process.stdout:
                    events.put(line)
            finally:
                events.put(None)
        worker = threading.Thread(target=collect, daemon=True)
        worker.start()
        thread_id, answer, useful, usage = None, None, None, None
        deadline = time.monotonic() + args.timeout
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("direct Codex evaluation exceeded its deadline")
                try:
                    line = events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if line is None:
                    break
                row = json.loads(line)
                journal.emit("provider_event", receipt=row)
                if row.get("type") == "thread.started":
                    thread_id = row.get("thread_id")
                if row.get("type") == "item.completed":
                    item = row.get("item") or {}
                    if item.get("type") == "agent_message":
                        parsed = parse_answer(item.get("text"))
                        if parsed:
                            answer = parsed
                            if useful is None and score(case, parsed, load_fixtures())["supported_expected"]:
                                useful = time.monotonic() - journal.started
                                journal.emit("first_useful_text", basis="fixture-supported structured claim")
                    else:
                        journal.emit("tool_outcome", receipt=item)
                if row.get("type") == "turn.completed":
                    usage = row.get("usage")
            returncode = process.wait(timeout=10)
            if returncode:
                raise RuntimeError("Codex exited " + str(returncode) + "; see local stderr receipt")
        finally:
            if process.poll() is None:
                process.terminate()  # only the child created by this evaluator
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    effective = validate_effective(codex_runtime(thread_id, args.codex_home), manifest)
    final = time.monotonic() - journal.started
    journal.emit("final_ready", observed_ready_s=final)
    return {"answer": answer, "score": score(case, answer, load_fixtures()), "effective": effective,
            "time_to_first_useful_s": useful, "final_observed_s": final, "visible_lag_s": None,
            "visibility_basis": "CLI output only; no desktop browser receipt",
            "scope": {"status": "requires_review", "basis": "fixture cwd and restricted integrations; shell read-only is not filesystem read confinement"},
            "thread_id": thread_id, "usage": usage}


def run_one(args):
    if not args.allow_model_calls:
        raise ValueError("real adapters require --allow-model-calls; use replay for a free deterministic check")
    manifest = checked_manifest(args.manifest)
    case = next((c for c in load_fixtures()["cases"] if c["id"] == args.case), None)
    if case is None:
        raise ValueError("unknown fixture case")
    cache = {"state": "uncontrolled"}
    if args.cache_state != "uncontrolled":
        if not args.cache_receipt:
            raise ValueError("cold/warm labels require a cache preparation receipt; evaluator never resets live caches")
        cache = json.loads(Path(args.cache_receipt).read_text(encoding="utf-8"))
        if cache.get("state") != args.cache_state or not cache.get("preparation") or not cache.get("instance_id"):
            raise ValueError("cache receipt must name state, preparation and isolated instance_id")
    journal = Journal(args.receipts)
    identity = {k: manifest[k] for k in ("model", "effort", "corpus_hash", "output_policy_hash", "fixture_definition_hash")}
    journal.emit("run_started", adapter=args.adapter, case=args.case, repetition=args.repetition,
                 cache=cache, matching=identity, replay=False)
    try:
        result = (vira_run if args.adapter == "vira" else codex_run)(args, case, manifest, journal)
        row = journal.emit("run_finished", adapter=args.adapter, case=args.case,
                           repetition=args.repetition, cache=cache, matching=identity, replay=False, **result)
        return row
    except Exception as exc:
        journal.emit("run_failed", error=str(exc), adapter=args.adapter, case=args.case)
        raise


def replay(receipts):
    fixtures = load_fixtures()
    results = []
    for case in fixtures["cases"]:
        journal = Journal(receipts)
        result = score(case, case["reference"], fixtures)
        journal.emit("run_finished", adapter="reference-replay", case=case["id"], replay=True,
                     score=result, time_to_first_useful_s=None, final_observed_s=None,
                     visible_lag_s=None, performance_measured=False,
                     explanation="gold fixture replay validates scoring only; no model or harness was run")
        results.append({"case": case["id"], **result})
    return results


def plan():
    rows = []
    for repetition in (1, 2):
        for state in ("cold", "warm"):
            for case in load_fixtures()["cases"]:
                order = ("vira", "codex") if repetition == 1 else ("codex", "vira")
                for adapter in order:
                    rows.append({"case": case["id"], "adapter": adapter,
                                 "cache_state": state, "repetition": repetition})
    return {"runs": rows, "count": len(rows),
            "instructions": "Prepare a separately isolated cache condition before each run; cold/warm labels are operator receipts, not inferred from repetition. Keep provider cache usage separate."}


def compare(paths):
    pairs, failures = defaultdict(dict), []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("event") == "run_failed":
                failures.append(row)
            if row.get("event") != "run_finished":
                continue
            if row.get("replay") or row.get("adapter") not in ("vira", "codex"):
                raise ValueError("reference replay is not live performance evidence")
            validate_effective({"effective": row.get("effective")}, row["matching"])
            key = (digest(row["matching"]), row["case"], row["cache"]["state"], row["repetition"])
            if row["adapter"] in pairs[key]:
                raise ValueError("duplicate trial identity; choose one preserved run explicitly")
            pairs[key][row["adapter"]] = row
    complete = [p for p in pairs.values() if set(p) == {"vira", "codex"}]
    ratios = [p["vira"]["time_to_first_useful_s"] / p["codex"]["time_to_first_useful_s"]
              for p in complete if p["vira"].get("time_to_first_useful_s") is not None
              and (p["codex"].get("time_to_first_useful_s") or 0) > 0]
    quality = all(p[a]["score"]["passed"] for p in complete for a in p)
    return {"matched_pairs": len(complete), "unpaired_trials": len(pairs) - len(complete),
            "failed_runs": len(failures), "median_paired_latency_ratio": statistics.median(ratios) if ratios else None,
            "all_fixture_quality_gates_pass": bool(complete) and quality,
            "scope_review_required": True, "browser_visibility_comparison_available": False,
            "superiority_established": False,
            "limitations": "Descriptive pilot only. Review claims and complete access receipts, failures, cache preparation, and browser visibility before making a performance claim."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("materialize")
    p.add_argument("--output", required=True); p.add_argument("--model", required=True); p.add_argument("--effort", required=True)
    p = sub.add_parser("replay"); p.add_argument("--receipts", required=True)
    sub.add_parser("plan")
    p = sub.add_parser("compare"); p.add_argument("receipts", nargs="+")
    p = sub.add_parser("run")
    p.add_argument("--adapter", choices=("vira", "codex"), required=True)
    p.add_argument("--manifest", required=True); p.add_argument("--case", required=True)
    p.add_argument("--receipts", required=True); p.add_argument("--allow-model-calls", action="store_true")
    p.add_argument("--cache-state", choices=("cold", "warm", "uncontrolled"), default="uncontrolled")
    p.add_argument("--cache-receipt"); p.add_argument("--repetition", type=int, default=1)
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--vira-url", default="http://localhost:8400")
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    args = parser.parse_args(argv)
    try:
        if args.command == "materialize": result = materialize(args.output, args.model, args.effort)
        elif args.command == "replay": result = replay(args.receipts)
        elif args.command == "plan": result = plan()
        elif args.command == "compare": result = compare(args.receipts)
        else: result = run_one(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "replay" and not all(row["passed"] for row in result):
            return 1
        if args.command == "run" and not result["score"]["passed"]:
            return 1
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, TimeoutError) as exc:
        print("answer-eval: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

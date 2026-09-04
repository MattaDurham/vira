"""Reply suggestions. Default backend is the local claude CLI (Max plan);
optional API backend via ANTHROPIC_API_KEY-style key in config.

CLI gotchas inherited from crm/scripts/synthesize_profiles.py: strip
ANTHROPIC_*/CLAUDE* env vars so the child CLI authenticates with its own
stored login instead of 401ing on session-scoped vars.
"""
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from . import data as crm
from . import imessage
from . import settings

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "config.json"

DEFAULTS = {
    "ai_provider": "anthropic",   # any server/models.py PROVIDERS id
    # Providers switched OFF (models.disabled_providers): a disabled id is
    # never drafted on or run as a session - every path REFUSES by name
    # rather than rerouting. The eval switch (docs/model-parity-eval).
    "providers_disabled": [],
    "ai_backend": "cli",          # "cli" (subscription login) | "api" (key)
    # cli_model is an ALIAS, so it names a tier and never a generation.
    "cli_model": "sonnet",
    # Corrections for aliases this machine's CLI resolves WRONG, e.g.
    # {"opus": "claude-opus-5"} when `--model opus` still gives 4.8.
    # Empty by default; see session.ALIAS_OVERRIDE_KEY for why this is
    # owner data rather than a shipped table, and how to re-measure.
    "model_alias_overrides": {},
    # Every api_model default is EMPTY on purpose (models.py MODEL SOURCES):
    # a shipped model id is the thing that goes stale, and it goes stale in
    # the one place only an admin edit and a push could fix. Empty resolves
    # at call time from the key's own live list — the newest model of the
    # tier cli_model names. An empty CLI model means the same for codex: run
    # whatever ~/.codex/config.toml is set to.
    "api_model": "",
    "api_key_env": "VIRA_ANTHROPIC_KEY",
    "openai_cli_model": "",
    "openai_api_model": "",
    "google_api_model": "",
    "xai_api_model": "",
    # The curated model roster (the Cursor pattern, owner's ask 2026-07-28):
    # the model ids enabled for pickers across every provider. EMPTY means
    # "everything the catalog offers" — so a fresh install shows all, and a
    # future model arrives enabled unless the owner has curated.
    "model_roster": [],
    "timeout": 120,
}

# Providers with no CLI draft path — their backend is always the API.
API_ONLY = ("google", "xai")


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(updates):
    cfg = config()
    cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg


PROMPT = """You are drafting reply suggestions for {owner}.

Channel: {channel}
Contact dossier (from {owner}'s CRM; may be partial):
{profile}

Recent conversation (chronological; "me" = {owner}):
{thread}

{facts}

{extra}

Write 3 candidate replies {owner} could send next on this channel. Prefer
replies that close the pending asks named in the facts; never re-answer an
ask marked released. Make the "forward" reply the single message that
closes the most open asks at once. Match
{owner}'s own voice as evidenced in the thread (their texts are the "me"
lines) — length, warmth, punctuation habits. Vary the three: one direct/minimal, one warmer,
one that moves the relationship or open loop forward. Never invent facts not
in the dossier or thread.

Return ONLY a JSON object:
{{"suggestions": [{{"text": "...", "tone": "direct|warm|forward", "why": "one short line"}}]}}
"""

HOOK_PROMPT = """You are drafting one conversation-opener iMessage for {owner}.

Contact dossier (from {owner}'s CRM; may be partial):
{profile}

Recent conversation (chronological; "me" = {owner}):
{thread}

{facts}

The opener should act on this conversation hook:
{extra}

If the facts show {owner} rarely starts conversations, this opener is the
point: it should read as {owner} reaching out unprompted, not as a reply.

Write ONE message {owner} could send to open this thread of conversation. Match
{owner}'s own voice as evidenced in the thread (their texts are the "me" lines) —
length, warmth, punctuation habits. Natural, not salesy. Never invent facts
not in the dossier or thread.

Return ONLY a JSON object:
{{"suggestions": [{{"text": "...", "tone": "opener", "why": "one short line"}}]}}
"""


# Tools a caller may opt into on the CLI path. Read-only by construction:
# this is a completion path, and nothing composed here has any business
# mutating the machine. Anything absent from an allow-list is refused by
# --permission-mode default, which also RECORDS the refusal.
READ_TOOLS = ("Read", "Glob", "Grep")


def _call_cli(prompt, model, timeout, tools=None):
    """One completion through the Claude CLI, with tools OFF unless asked.

    THIS PATH IS A FULL CLAUDE CODE TURN, NOT A TEXT COMPLETION -- measured
    2026-08-28, and the reason this function pins a permission mode at all.
    Invoked with no flags it inherits ~/.claude/settings.json, and on a
    machine whose defaultMode is "auto" that means Write, Edit and Bash are
    live and permitted against absolute paths outside cwd, with
    permission_denials empty. Verified by writing a file, twice.

    That matters because 37 call sites route through here and several put
    text the owner did not write into the prompt -- an inbound email body
    (receipts), an employer's job posting (jobrescore), a message thread
    (reply drafting). Model injection-resistance was the ONLY thing standing
    between that text and a shell, and model judgement is not a control: it
    has no audit trail and it changes with the model.

    So the default is locked. `--permission-mode default` was verified to
    deny the Write AND to record it in permission_denials, which is what
    makes a refusal visible rather than silent. A caller that genuinely
    needs to gather context passes `tools=READ_TOOLS`; that was verified to
    permit a read of a file outside cwd while writes stay denied.
    """
    cmd = ["claude", "--print", "--output-format", "json", "--model", model,
           "--permission-mode", "default"]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                         timeout=timeout, env=settings.strip_env())
    if res.returncode != 0:
        raise RuntimeError(f"claude exit {res.returncode}: {res.stderr.strip()[-400:]}")
    try:
        envelope = json.loads(res.stdout)
        text = envelope.get("result", "")
        if envelope.get("is_error"):
            raise RuntimeError(f"claude error: {text[:300]}")
        _learn_from_cli(envelope, model)
    except json.JSONDecodeError:
        text = res.stdout
    return text


def _learn_from_cli(envelope, model):
    """Read the response's own account of the window it ran in.

    The CLI reports modelUsage.<resolved-model>.contextWindow and
    maxOutputTokens on every --output-format json call, so the app can size
    its prompts from its own receipts instead of from a literal that goes
    stale the week a model ships. This is the one rung of modelbudget's
    ladder that cannot rot. Best-effort by construction: losing a completion
    over bookkeeping would be the worse trade.
    """
    try:
        from . import modelbudget
        usage = envelope.get("modelUsage") or {}
        for resolved, row in usage.items():
            ctx = int(row.get("contextWindow") or 0)
            out = int(row.get("maxOutputTokens") or 0)
            if ctx or out:
                # Learn under BOTH the requested spelling and the one the CLI
                # actually resolved: `--model opus` is an alias whose target
                # moves, and a budget asked for by the alias must still find
                # the answer (models.py's 2026-07-29 alias lesson).
                modelbudget.learn("anthropic", "cli", model, ctx, out)
                modelbudget.learn("anthropic", "cli", resolved, ctx, out)
    except Exception:      # noqa: BLE001
        pass


def _call_api(prompt, model, timeout, key):
    # Was a hardcoded 1500 against models reporting 128_000 -- an 85x
    # reduction nobody chose, sized for the 2026-07-07 reply-draft path this
    # function was written for and never revisited as 22 modules adopted it.
    from . import modelbudget
    body = json.dumps({
        "model": model,
        "max_tokens": modelbudget.api_output_tokens(),
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return "".join(b.get("text", "") for b in payload.get("content", []))


def _call_codex_cli(prompt, model, timeout):
    """OpenAI's subscription path, the mirror of _call_cli: `codex exec`
    runs non-interactively against the ChatGPT login. The binary is often
    NOT on PATH (it ships inside ChatGPT.app), so it is resolved through
    models.find_binary rather than named directly."""
    from . import models as provider
    binary = provider.find_binary("openai")
    if not binary:
        raise RuntimeError("codex CLI not found on this Mac")
    # Same posture as _call_cli: a drafting call gets codex's read-only
    # sandbox rather than its default workspace-write one. `exec` accepts
    # --sandbox (only `exec resume` does not -- see agentbackend).
    cmd = [binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
    if model:                       # empty = codex's own configured default
        cmd += ["--model", model]
    cmd += [prompt]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=timeout, env=settings.strip_env())
    if res.returncode != 0:
        raise RuntimeError(f"codex exit {res.returncode}: "
                           f"{res.stderr.strip()[-400:]}")
    return res.stdout


def _call_openai_api(prompt, model, timeout, key):
    body = json.dumps({
        "model": model,
        "input": prompt,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=body, method="POST",
        headers={"content-type": "application/json",
                 "authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    # Responses API: walk output[].content[].text, tolerating shape drift.
    out = []
    for item in payload.get("output") or []:
        for block in item.get("content") or []:
            if block.get("text"):
                out.append(block["text"])
    return "".join(out) or payload.get("output_text", "")


def _call_google_api(prompt, model, timeout, key):
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }).encode()
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent",
        data=body, method="POST",
        headers={"content-type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    for cand in payload.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)
    return ""


def _call_xai_api(prompt, model, timeout, key):
    """xAI is OpenAI-compatible chat completions, different host."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions", data=body, method="POST",
        headers={"content-type": "application/json",
                 "authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    choices = payload.get("choices") or []
    if choices:
        return (choices[0].get("message") or {}).get("content", "")
    return ""


def _provider_models(cfg, pid):
    """(cli_model, api_model) for the provider in play."""
    if pid == "openai":
        return cfg["openai_cli_model"], cfg["openai_api_model"]
    if pid == "google":
        return "", cfg["google_api_model"]
    if pid == "xai":
        return "", cfg["xai_api_model"]
    return cfg["cli_model"], cfg["api_model"]


def effective_backend(cfg):
    """(provider, backend) that will ACTUALLY answer, after the ladder.

    Extracted from _run so that anything needing to reason about the
    answering backend -- modelbudget, above all -- asks the same question
    _run answers, instead of carrying a second copy of a ladder that really
    does re-route (no key, dead login). Two implementations of "which model
    is about to run" is how a surface ends up describing one backend while
    another one answers.
    """
    from . import aihealth
    from . import models as provider
    backend = cfg["ai_backend"]
    pid = str(cfg.get("ai_provider") or "anthropic")
    if pid not in provider.PROVIDERS:
        pid = "anthropic"
    if provider.is_disabled(pid):
        # Raised BEFORE _run's try, on purpose: a disabled go-to is the
        # owner's choice, not a backend failure, so it must never reach
        # aihealth.note_failure and flip the banner red.
        raise provider.ProviderDisabled(pid, role="the configured go-to")
    # The key may come from the env (existing installs) or the Keychain
    # (pasted in Setup by someone with no shell profile to edit).
    key = provider.api_key(pid)
    if pid in API_ONLY:
        # No CLI to fall back to: the API is the only path, and a missing
        # key fails honestly rather than silently switching providers.
        return pid, "api"
    if backend == "api" and not key:
        backend = "cli"
    if backend == "cli":
        backend = aihealth.preferred_backend("cli", key)
    return pid, backend


def _run(prompt, cfg, tools=None):
    """Pick the EFFECTIVE backend, call it, and on failure record the auth
    state so the app degrades gracefully. Returns (text, backend_used).

    Backend selection is the fallback ladder (aihealth rung 3):
      - configured "api" but no key present  -> fall back to cli
      - configured "cli" but the login is dead + a key IS present -> use api
    A dead cli login with no key stands as cli: the call then fails honestly
    and note_failure flips the health state red + alerts the owner."""
    from . import aihealth
    from . import models as provider
    pid, backend = effective_backend(cfg)
    key = provider.api_key(pid)
    cli_model, api_model = _provider_models(cfg, pid)
    try:
        if backend == "api":
            if not key:
                raise RuntimeError(
                    f"{pid} needs an API key — connect it in Config")
            if not api_model:
                # Nothing picked: derive it from the key's own live list
                # rather than falling back to a spelling shipped months ago.
                api_model = provider.default_api_model(pid, tier=cli_model)
                if not api_model:
                    raise RuntimeError(
                        f"no API model set for {pid}, and its model list "
                        f"could not be read — pick one in Config > Models")
            if pid == "openai":
                return _call_openai_api(prompt, api_model, cfg["timeout"], key), backend
            if pid == "google":
                return _call_google_api(prompt, api_model, cfg["timeout"], key), backend
            if pid == "xai":
                return _call_xai_api(prompt, api_model, cfg["timeout"], key), backend
            return _call_api(prompt, api_model, cfg["timeout"], key), backend
        if pid == "openai":
            return _call_codex_cli(prompt, cli_model, cfg["timeout"]), backend
        return _call_cli(prompt, cli_model, cfg["timeout"], tools), backend
    except Exception as e:  # noqa: BLE001 — classify + record, then re-raise
        aihealth.note_failure(str(e), source="reply-draft")
        raise


def complete(prompt, tools=None):
    """One-shot completion on the configured backend.

    `tools` is an explicit, read-only allow-list for a caller that needs to
    gather its own context (see READ_TOOLS). It is honoured ONLY on the
    Anthropic CLI path -- every other backend is a plain completion -- so a
    caller must treat it as an enhancement and still work when it does
    nothing. Ask modelbudget.has_tools() before relying on it.
    """
    return _run(prompt, config(), tools=tools)[0]


def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]!r}")
    return json.loads(m.group(0))


def suggest(person_id, channel="imessage", extra="", mode="replies"):
    cfg = config()
    detail = crm.get_person(person_id)
    if not detail:
        raise KeyError(person_id)

    prof = detail.get("profile")
    if prof:
        profile_txt = json.dumps({k: prof.get(k) for k in (
            "name", "relationship_class", "relationship_summary", "comms_style",
            "open_loops", "hooks", "personal_facts", "cadence")}, indent=1)[:6000]
    else:
        m = detail.get("master") or {}
        profile_txt = json.dumps({"name": detail["person"]["name"],
                                  "relationship": m.get("relationship"),
                                  "company": m.get("company"),
                                  "evidence": m.get("evidence")}, indent=1)

    msgs = imessage.thread_for_person(person_id, limit=30)
    thread_txt = "\n".join(
        f"[{m['when'][:16] if m['when'] else '?'}] {'me' if m['from_me'] else 'them'}: {m['text']}"
        for m in msgs) or "(no recent iMessage thread on file)"

    owner = config().get("owner_name") or "the user"
    # the arithmetic first: computed cadence and the open-ask ledger, so the
    # drafts close loops instead of adding tone variations to guess between
    from . import threadread
    facts = threadread.facts_block(person_id)
    if mode == "hook":
        prompt = HOOK_PROMPT.format(owner=owner, profile=profile_txt,
                                    thread=thread_txt[:12000], extra=extra,
                                    facts=facts)
    else:
        prompt = PROMPT.format(owner=owner, channel=channel, profile=profile_txt,
                               thread=thread_txt[:12000],
                               extra=f"Guidance from {owner}: {extra}" if extra else "",
                               facts=facts)

    text, backend = _run(prompt, cfg)

    result = _extract_json(text)
    result["backend"] = backend
    result["thread_len"] = len(msgs)
    return result

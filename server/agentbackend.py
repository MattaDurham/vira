"""Provider-agnostic session backends — a live agent session is no longer
synonymous with the Claude Agent SDK.

Two provider adapters, one Vira control plane:

- **gated** (Anthropic, the SDK path in runner.py): per-tool Approve/Deny
  cards, the acceptedits rung, the branch-first write guard, the ask_owner
  decision channel. The full experience.
- **gated Codex** (App Server path in codexapp.py): the same Vira registry,
  approval cards, owner questions, branch guard, resume and in-turn steering
  translated over bidirectional JSON-RPC.
- **best_effort compatibility**: ``codex exec`` remains a fallback for an old
  Codex installation that cannot initialize App Server. It is deliberately
  not the primary OpenAI engine.

The harness around a session — the job dir protocol, heartbeat, control
tail, the reply window, the epilogue (plan finalize, idea close-out,
ledger) — is runner.py's and does NOT fork; only the engine inside
run_session does. CLI_EXEC is a table: adding a provider (the grok CLI has
the flags, pending a verified headless run) is a row, not new branching.
"""
import asyncio
import json
import shlex

from . import models, settings, viratools

# Item types codex emits that render as one summary line. Anything unknown
# renders generically — a CLI release must not blank the transcript.
_SKIP_ITEMS = {"reasoning"}          # keep the log readable, as the SDK path does

# A completed CLI tool call is one JSONL event and may carry its entire
# captured output in that single line.  asyncio's subprocess default is only
# 64 KiB; a normal repository read crossed it and killed the session before
# the event could be parsed.  Keep a deliberate memory bound, but size it for
# real agent tool output rather than the generic StreamReader default.
CLI_JSONL_LIMIT = 8 * 1024 * 1024


def _codex_argv(binary, spec, resume_id, prompt):
    """One codex turn. `resume_id` continues the same conversation, which is
    what makes steering and the reply window work across turns.

    The subcommand comes first (`exec resume <id> [flags]`), and --sandbox
    exists only on the FIRST turn — `exec resume` does not accept it; a
    resumed session keeps the sandbox it was started with. Autopilot's
    bypass flag IS accepted on both and must ride every turn."""
    sandbox = sandbox_for(spec)
    argv = [binary, "exec"]
    if resume_id:
        argv += ["resume", resume_id]
    argv += ["--json", "--skip-git-repo-check"]
    if spec.get("model_resolved") or spec.get("model"):
        argv += ["--model", spec.get("model_resolved") or spec["model"]]
    if sandbox == "danger-full-access":
        # autopilot: the owner opted out of gating entirely (same meaning
        # as bypassPermissions on the SDK path).
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    elif not resume_id:
        argv += ["--sandbox", sandbox]
    argv += [prompt]
    return argv


CLI_EXEC = {
    "openai": {
        "label": "Codex",
        "argv": _codex_argv,
    },
    # xAI's grok CLI advertises the whole headless surface (--single /
    # --output-format streaming-json / --permission-mode) and still does NOT
    # get a row: the blocker is measured below, not a guess.
    #
    # The 2026-07-28 "hung before its first byte" note was imprecise. Probed
    # again on 2026-07-30 (grok 0.2.14, signed OUT): it emits bytes fine and
    # then blocks on an INTERACTIVE OAUTH FLOW — it prints an auth.x.ai
    # sign-in URL and waits on a loopback browser callback that never comes.
    # So the failure is an auth flow, not a wedged transport, and that is the
    # dangerous shape: a detached runner has no terminal and nobody to click,
    # so it would park at that prompt with a live heartbeat, doing nothing —
    # the silent-stall class ask_owner exists to end. Whoever lands this row
    # needs an auth PRECHECK before spawn, not just an argv function; the
    # CLI offers no cheap `auth status` (hence status_cmd None in
    # models.PROVIDERS), and `grok models` answering "You are not
    # authenticated" is the cheapest tell found so far.
    #
    # The mechanism Vira would actually use IS verified: exporting
    # XAI_API_KEY diverts it off OAuth straight to api.x.ai/v1/responses
    # ("the API key takes precedence over browser credentials"), which is why
    # the xai row already carries api_env + key_url. A deliberately bogus key
    # failed fast and honestly — exit 1, one JSONL line on stdout. Wire shape
    # for the renderer: stdout is JSONL in a {"type": ...} envelope, the same
    # family _run_turn already parses (an error turn is
    # {"type":"error","message":...}); stderr is ANSI tracing noise and must
    # be drained concurrently exactly as codex's is. The permission ladder
    # also maps nearly 1:1 — grok's --permission-mode takes default,
    # acceptEdits, auto, dontAsk, bypassPermissions and plan, so three of
    # Vira's rungs are spelled identically, and it has its own --sandbox
    # profile for sandbox_for() to target.
    #
    # What is still UNMEASURED is precisely what a row needs: the event names
    # of a SUCCESSFUL turn (assistant message, tool call, thread/session id)
    # and whether -r/--resume continues a conversation — the two things
    # _render_item and the resume argument are built out of. No xAI credential
    # exists on this machine (no env var, no shell profile, no keychain entry,
    # nothing in the secrets ladder), so a successful turn could not be run.
    # Guessing those names would ship a renderer that blanks transcripts.
    # Honesty over reach: a key pasted in Config, or `grok login
    # --device-auth`, is all that stands between this comment and a row.
}


# The public provider contract. Routing and UI disclosure consume this
# instead of inferring features from a provider name. A new adapter earns a
# capability only when its path implements and tests that behavior.
CAPABILITIES = {
    "anthropic": {
        "draft": True, "sessions": True, "native_tools": True,
        "workspace_tools": True,
        "approvals": True, "owner_questions": True, "resume": True,
        "steering": True, "interrupt": True, "model_catalog": True,
    },
    "openai": {
        "draft": True, "sessions": True, "native_tools": True,
        "workspace_tools": True,
        "approvals": True, "owner_questions": True, "resume": True,
        "steering": True, "interrupt": True, "model_catalog": True,
    },
    "google": {
        "draft": True, "sessions": True, "native_tools": True,
        "workspace_tools": False,
        "approvals": True, "owner_questions": True, "resume": True,
        "steering": False, "interrupt": False, "model_catalog": True,
    },
    "xai": {
        "draft": True, "sessions": True, "native_tools": True,
        "workspace_tools": False,
        "approvals": True, "owner_questions": True, "resume": True,
        "steering": False, "interrupt": False, "model_catalog": True,
    },
}


def capabilities(pid):
    """A copy of the verified feature contract for one provider."""
    return dict(CAPABILITIES.get(pid, {}))


# The transport each adapter STAMPS on the ledger row (joblog.record_session
# `transport`). A parity check reads this to ask "did the session run on the
# lane its provider is supposed to run on", so the strings here must be the
# ones the adapters actually write - tests/test_parity_flows.py pins each
# against the adapter's source, because a table that drifts from the writers
# would grade every session as off-lane while nothing was wrong.
EXPECTED_TRANSPORT = {
    "anthropic": "claude-sdk",
    "openai": "codex-app-server",
    "google": "google-function-api",
    "xai": "xai-function-api",
}


def provider_of_model(model):
    """Which provider a model id/alias belongs to. '' means unknown —
    the caller falls back to the configured session default."""
    m = (model or "").strip().lower()
    if not m:
        return ""
    if m.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    if m.startswith("grok"):
        return "xai"
    if m.startswith(("claude", "sonnet", "opus", "haiku", "fable")):
        return "anthropic"
    # Future ids need no prefix rule when the provider's own catalog has
    # already vouched for them.
    return models.provider_for_model(model)


def default_session_provider():
    """The engine a dispatch that names NOTHING runs on — read from config
    (`ai_provider`, the owner's go-to, the same key suggest.py drafts
    against), never a shipped literal.

    This is what makes automatic work follow the owner. Most machine
    dispatches name no model at all — orphan resume and land, the journal's
    unapplied instructions, profile explore, define sourcing, a routine with
    no model of its own — so a hardcoded fallback pinned every one of them
    to Anthropic no matter what the owner had configured. On 2026-08-06 that
    surfaced the hard way: Vira's go-to was Codex, an Implement session ran
    on claude-opus-5 anyway and died on "You've hit your monthly spend
    limit", and the Resume button could only relaunch it onto the same dead
    account — there was no way, anywhere in the UI, to say otherwise.

    Guarded by session capability rather than trusted blindly. Unknown or
    drafting-only future providers fall back to Anthropic; Gemini and Grok
    qualify through Vira's function-calling session adapter.
    """
    want = str(settings.raw().get("ai_provider") or "").strip().lower()
    if want in models.PROVIDERS:
        # A disabled go-to REFUSES rather than falling through to
        # anthropic: the fallback was written for a go-to that cannot host
        # sessions, and re-using it here would run a dispatch on a provider
        # the owner switched off - the exact reroute the switch exists to
        # stop. See models.ProviderDisabled.
        if models.is_disabled(want):
            raise models.ProviderDisabled(want, role="the configured go-to")
        if sessions_quality(want):
            return want
    if models.is_disabled("anthropic"):
        raise models.ProviderDisabled("anthropic", role="the fallback")
    return "anthropic"


def session_provider(model=None, provider=None):
    """The provider a launch will run its session on. An explicit provider
    wins; else the model names it; else the owner's configured go-to (see
    default_session_provider) rather than a hardcoded anthropic."""
    p = (provider or "").strip().lower()
    if p not in models.PROVIDERS:
        p = provider_of_model(model)
    if p:
        # An explicit pin - or a model that names its provider - never
        # runs a disabled provider. Refuse by name; nothing else in the
        # chain is consulted.
        if models.is_disabled(p):
            raise models.ProviderDisabled(p)
        return p
    return default_session_provider()


def uses_cli_exec(spec):
    # Historical name kept for the structural launch contract. True now
    # means "Runner must use a provider adapter rather than Claude SDK."
    return (spec.get("provider") or "anthropic") != "anthropic"


def sessions_quality(pid):
    """"gated" | "best_effort" | "" (cannot host sessions)."""
    caps = CAPABILITIES.get(pid, {})
    if (caps.get("sessions") and caps.get("native_tools")
            and caps.get("approvals") and caps.get("owner_questions")):
        return "gated"
    if caps.get("sessions") or pid in CLI_EXEC:
        return "best_effort"
    return ""


def sandbox_for(spec):
    """Vira's permission ladder mapped onto codex's sandbox vocabulary.
    read-only stays read-only; bypassPermissions means on this path
    what it means on the SDK path; everything between runs workspace-write —
    confined to the cwd, which branch-first placement has already made a
    worktree for any session that can write."""
    from . import session          # lazy: session imports this module
    if spec.get("read_only"):
        return "read-only"
    if session.norm_mode(spec.get("mode")) == "bypassPermissions":
        return "danger-full-access"
    return "workspace-write"


def default_model(pid):
    """The provider's configured CLI model, for a launch that names none."""
    from .suggest import config
    cfg = config()
    key = (models.PROVIDERS.get(pid, {}).get("config_keys") or {}).get("cli") \
        or (models.PROVIDERS.get(pid, {}).get("config_keys") or {}).get("api")
    return cfg.get(key) or ""


# ---------------------------------------------------------------- the run ---

def _render_item(item):
    """One completed codex item -> a transcript line (or None to skip),
    matching the shapes renderTermLine already knows."""
    t = item.get("type") or ""
    if t in _SKIP_ITEMS:
        return None
    if t == "agent_message":
        txt = (item.get("text") or "").strip()
        return txt + "\n" if txt else None
    if t == "command_execution":
        cmd = (item.get("command") or "").strip()
        code = item.get("exit_code")
        tail = f" (exit {code})" if code not in (None, 0) else ""
        return f"  → Bash: {cmd[:160]}{tail}\n" if cmd else None
    if t == "file_change":
        changes = item.get("changes") or []
        if isinstance(changes, list) and changes:
            names = ", ".join(str(c.get("path", ""))[-60:] for c in changes[:4]
                              if isinstance(c, dict))
            return f"  → Edit: {names}\n" if names else None
        path = item.get("path") or ""
        return f"  → Edit: {str(path)[-60:]}\n" if path else None
    if t == "web_search":
        q = item.get("query") or ""
        return f"  → WebSearch: {str(q)[:120]}\n" if q else None
    if t == "mcp_tool_call":
        name = item.get("tool") or item.get("name") or "tool"
        return f"  → {name}\n"
    if t == "error":
        # The turn.failed handler carries the message; a bare "→ error"
        # line beside it is noise, not information.
        return None
    if t == "todo_list":
        return None
    return f"  → {t}\n"


async def _run_turn(runner, binary, prompt, resume_id):
    """One codex exec turn. Returns (thread_id, last_message, ok)."""
    spec = runner.spec
    argv = CLI_EXEC[spec["provider"]]["argv"](binary, spec, resume_id, prompt)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=spec["cwd"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=settings.strip_env(), limit=CLI_JSONL_LIMIT)
    runner.exec_proc = proc

    # Drain stderr CONCURRENTLY — codex logs progress there, and an unread
    # pipe fills and wedges the child mid-turn.
    err_buf = bytearray()

    async def _drain_err():
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                return
            del err_buf[:-8192]
            err_buf.extend(chunk)

    err_task = asyncio.ensure_future(_drain_err())
    thread_id = resume_id
    last_msg = ""
    last_err = None                  # codex mirrors error + turn.failed
    turn_ok = False
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                runner.append(line + "\n")
                continue
            et = ev.get("type") or ""
            if et == "thread.started":
                thread_id = ev.get("thread_id") or thread_id
                if thread_id and not runner.state.get("session_id"):
                    runner.state["session_id"] = thread_id
                    runner.flush_state()
                    from . import joblog
                    joblog.record_session(spec["id"], thread_id,
                                          transport="cli-exec")
                    runner.append(f"[vira] codex working… "
                                  f"(thread {thread_id[:8]})\n")
            elif et == "item.completed":
                piece = _render_item(ev.get("item") or {})
                if piece:
                    runner.append(piece)
                item = ev.get("item") or {}
                # the same per-turn record the SDK path keeps (what a chat
                # turn looked at); a command is the codex path's whole
                # tool surface, so it is recorded under the Bash name
                rec = getattr(runner, "record_tool", None)
                if rec and item.get("type") == "command_execution":
                    rec("Bash", {"query": (item.get("command") or "")[:200]})
                elif rec and item.get("type") == "mcp_tool_call":
                    rec(item.get("tool") or item.get("name") or "tool", {})
                if item.get("type") == "agent_message":
                    last_msg = item.get("text") or last_msg
            elif et == "turn.completed":
                turn_ok = True
            elif et in ("turn.failed", "error"):
                msg = (ev.get("error") or {}).get("message") \
                    if isinstance(ev.get("error"), dict) else ev.get("message")
                # codex emits the same failure as an error event AND a
                # turn.failed — one line in the transcript is enough.
                if msg != last_err:
                    runner.append(f"[vira] {spec['provider']} turn failed: "
                                  f"{msg or 'unknown error'}\n")
                    last_err = msg
                turn_ok = False
        rc = await proc.wait()
        await err_task
        if rc != 0:
            err = err_buf.decode("utf-8", "replace")
            runner.append(f"[vira] {CLI_EXEC[spec['provider']]['label']} "
                          f"exited {rc}: {err.strip()[-400:]}\n")
            turn_ok = False
    finally:
        err_task.cancel()
        runner.exec_proc = None
    return thread_id, last_msg, turn_ok


async def run_cliexec(runner):
    """Drive a best-effort session through the provider's own CLI, inside
    runner.py's harness: same inbox/steering, same reply window, same
    epilogue. Returns (result_text, ok) exactly as the SDK path does."""
    spec = runner.spec
    row = CLI_EXEC[spec["provider"]]
    label = models.PROVIDERS[spec["provider"]]["label"]
    binary = models.find_binary(spec["provider"])
    if not binary:
        runner.append(f"[vira] {row['label']} CLI not found on this machine "
                      f"— connect {label} in Config or pick another model\n")
        return "", False
    sandbox = sandbox_for(spec)
    runner.append(
        f"[vira] {label} session via {row['label']} — best-effort mode: "
        f"no per-tool approval cards on this provider; containment is "
        f"{row['label']}'s own sandbox ({sandbox}) in {spec['cwd']}\n")
    runner.append(f"[vira] $ {shlex.join([row['label'].lower(), 'exec'])} "
                  f"--sandbox {sandbox}\n")

    # The deep Vira connection, HTTP flavor: no in-process MCP tools here,
    # so the preamble names the API on :8377 instead. Held separately
    # because continuity on this path is `exec resume <thread_id>` — when
    # the thread is lost, a steering/reply turn starts a FRESH conversation
    # and must re-carry the preamble or that turn runs with no Vira context
    # at all (only whatever the CLI auto-loaded from cwd).
    pre = viratools.preamble(
        native=False,
        worktree_path=spec.get("worktree") or "",
        branch=spec.get("branch") or "",
        live_root=spec.get("live_root") or "")
    # Resuming an EARLIER run's thread: the preamble is already in that
    # conversation, so it is not re-carried — the same rule the reply turn
    # below follows (`reply if thread_id else pre + reply`). The prompt is
    # the owner's message, delivered straight into the existing thread.
    thread_id = spec.get("resume_session") or None
    prompt = spec["prompt"] if thread_id else pre + "\n\n" + spec["prompt"]


    result_text = ""
    ok = False
    done = False
    while not done:
        thread_id, last_msg, ok = await _run_turn(
            runner, binary, prompt, thread_id)
        result_text = last_msg or result_text
        if runner.closing or runner.interrupted:
            break
        # Turn boundary — mirror the SDK loop: queued steering first.
        steered = False
        while not runner.inbox.empty():
            try:
                item = runner.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is runner.END:
                continue
            runner.finished_cleanly = False
            runner.append("[vira] steering delivered\n")
            prompt = item
            steered = True
            break
        if steered:
            if not thread_id:
                runner.append("[vira] note: no session thread to resume — "
                              "starting a fresh one with your message\n")
                prompt = pre + "\n\n" + prompt
            continue
        # Publish the turn's answer BEFORE parking - the SDK path has done
        # this since the reply-followthrough fix (2026-08-28), and this path
        # never did: a parked codex session reported result_text "" for the
        # whole reply window, so anything reading the answer at the turn
        # boundary (the reply channel, a chat) saw nothing until Finish.
        from . import runner as _runner_mod   # lazy: runner imports this module
        runner.state["result_text"] = (result_text or "")[:_runner_mod.RESULT_KEEP]
        runner.flush_state()
        park = ok and runner.parks_at_turn_end()
        if park:
            park = await runner.offer_landing()
        reply = await runner.await_reply() if park else None
        if reply is None:
            done = True
        else:
            runner.finished_cleanly = False
            runner.append("[vira] reply delivered\n")
            # a new turn: tool calls from here belong to it (record_tool)
            runner.state["turn"] = int(runner.state.get("turn") or 0) + 1
            runner.flush_state()
            prompt = reply if thread_id else pre + "\n\n" + reply
    return result_text, ok


async def run_provider_session(runner):
    """Run the best available interactive adapter for this provider.

    App Server is Codex's first-class lane. The exec implementation remains a
    compatibility fallback only when initialization fails before a thread has
    started; falling back after work began could replay side effects.
    """
    spec = runner.spec
    if spec.get("provider") == "openai":
        binary = models.find_binary("openai")
        if not binary:
            runner.append("[vira] Codex CLI not found — connect OpenAI in "
                          "Config or pick another model\n")
            return "", False
        from . import codexapp
        runner.append(
            "[vira] OpenAI session via Codex App Server — Vira tools, "
            "approval cards, owner questions, resume, steering and "
            "interrupts share the native control plane\n")
        try:
            return await codexapp.run_session(
                runner, binary, settings.strip_env())
        except codexapp.AppServerUnavailable as e:
            runner.append(
                f"[vira] Codex App Server unavailable ({e}); falling back "
                "to compatibility exec mode without native Vira cards\n")
        return await run_cliexec(runner)
    if spec.get("provider") in ("google", "xai"):
        from . import functionagent
        return await functionagent.run_session(runner, spec["provider"])
    return await run_cliexec(runner)

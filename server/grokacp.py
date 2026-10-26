"""Grok Build adapter: Vira sessions on the owner's Grok subscription.

`grok agent stdio` speaks the Agent Client Protocol (agentclientprotocol.com):
JSON-RPC 2.0, one message per line, over the child's stdin and stdout. A
`grok login` account runs it on the subscription rather than the per-token
API (authenticate answers `backend_billed: false`). Like codexapp.py, this
module owns no Vira policy; it translates between ACP and the runner's
established contracts. Every shape below was measured against grok 1.0.70
on 2026-10-26, not read off a spec:

- Auth precheck. `initialize` lists the auth methods, and `cached_token`
  is there only once `grok login` has run. Without it the session stops by
  name before any prompt: a detached runner has nobody to click an OAuth
  page, which is the silent stall that kept Grok off the CLI lane in July.
- Grok's own tools ask runner.gate. In its default (ask) mode Grok sends
  session/request_permission for every file edit and every shell command
  outside its built-in read-only list. Reads, read-only shell commands and
  a bare `mkdir`/`touch` run without asking (Grok's documented rule), so
  the gate never sees those; everything that writes content does.
- A rejection ENDS Grok's turn (stopReason `cancelled`; Grok's own label is
  "No, and tell Grok what to do differently"). On the SDK lane the model
  reads a denial and carries on, so after a gate denial this adapter sends
  the denial back as the next prompt, inside the same Vira turn.
- Vira's tools arrive through mcpbridge, which Grok names `vira__<tool>`.
  Grok asks permission for an MCP call too; that ask is answered yes here,
  because the real gate runs inside the bridge's tools/call, where a denial
  comes back as a tool result and the turn continues.
- Model and effort go through session/set_config_option: `grok agent -m`
  was ignored in 1.0.70.
- session/load replays the old conversation as session/update notifications
  BEFORE it returns. Those are history, not this turn, and are discarded.
- session/cancel stops a turn in flight. There is no in-turn steering: a
  steer waits in the inbox for the turn boundary.
"""
import asyncio
import json
import os
import uuid

from . import joblog, viratools
from .mcpbridge import McpBridge

JSONL_LIMIT = 8 * 1024 * 1024
TRANSPORT = "grok-acp"
PROTOCOL_VERSION = 1
# The auth method a stored `grok login` offers. "grok.com" is the
# interactive browser flow, which a detached runner cannot complete.
AUTH_METHOD = "cached_token"
# How many denials in a row one Vira turn sends back before it ends. A model
# that keeps retrying a refused write after being told why is not going to
# stop on the ninth explanation.
MAX_DENIAL_RESUMES = 8
DENIAL_PROMPT = ("[Vira] Vira's permission gate refused that tool call: "
                 "{message}\nDo not retry it. Continue the task without it, "
                 "or explain in your answer what you could not do.")
MCP_PREFIX = "vira__"
STARTUP_TIMEOUT = 30
# A replayed message chunk or a vendor notification is never shown; these
# are the session/update kinds that carry this turn's visible work.
_SHOWN = {"agent_message_chunk", "tool_call", "tool_call_update"}


class AcpUnavailable(RuntimeError):
    """The Grok CLI could not establish or keep the ACP session."""


class NotSignedIn(AcpUnavailable):
    """The Grok CLI has no stored login to run on."""


class RpcError(RuntimeError):
    def __init__(self, method, error):
        self.method = method
        self.error = error
        message = (error.get("message") or json.dumps(error)
                   if isinstance(error, dict) else str(error))
        detail = error.get("data") if isinstance(error, dict) else None
        super().__init__(f"{method}: {message}" + (f" ({detail})" if detail else ""))


class AcpClient:
    """Newline-delimited JSON-RPC 2.0 client for `grok agent stdio`."""

    def __init__(self, argv, cwd, env, on_request):
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.on_request = on_request
        self.proc = None
        self.notifications = asyncio.Queue()
        self._pending = {}
        self._next_id = 1
        self._reader_task = None
        self._stderr_task = None
        self.stderr_tail = bytearray()

    async def start(self):
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, cwd=self.cwd, env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=JSONL_LIMIT)
        except (OSError, ValueError) as e:
            raise AcpUnavailable(str(e)) from e
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            # The client offers no filesystem or terminal of its own: Grok
            # uses its own tools, and those are what the gate is asked about.
            return await asyncio.wait_for(self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False},
                "clientInfo": {"name": "vira", "title": "Vira",
                               "version": "1"},
            }), STARTUP_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - normalize protocol startup
            await self.close()
            if isinstance(e, AcpUnavailable):
                raise
            raise AcpUnavailable(str(e)) from e

    async def _write(self, payload):
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise AcpUnavailable("grok agent is not running")
        line = json.dumps({"jsonrpc": "2.0", **payload}, ensure_ascii=False,
                          separators=(",", ":"))
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method, params):
        req_id = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = (method, fut)
        try:
            await self._write({"id": req_id, "method": method,
                               "params": params or {}})
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method, params):
        await self._write({"method": method, "params": params or {}})

    async def _serve_request(self, message):
        try:
            result = await self.on_request(
                message.get("method") or "", message.get("params") or {})
            await self._write({"id": message["id"],
                               "result": result if result is not None else {}})
        except Exception as e:  # noqa: BLE001 - answer the agent, never hang it
            code = -32601 if isinstance(e, NotImplementedError) else -32000
            await self._write({"id": message["id"],
                               "error": {"code": code, "message": str(e)}})

    async def _read_loop(self):
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    if "id" in message:
                        asyncio.create_task(self._serve_request(message))
                    else:
                        await self.notifications.put(message)
                    continue
                # A response. Grok also answers requests of its own making
                # (id "skills-reload"); an id this client never sent is not
                # ours to resolve.
                row = self._pending.get(message.get("id"))
                if not row:
                    continue
                method, fut = row
                if fut.done():
                    continue
                if "error" in message:
                    fut.set_exception(RpcError(method, message["error"]))
                else:
                    fut.set_result(message.get("result") or {})
        finally:
            rc = await self.proc.wait() if self.proc else -1
            detail = self.stderr_tail.decode("utf-8", "replace").strip()[-500:]
            error = AcpUnavailable(
                f"grok agent exited {rc}" + (f": {detail}" if detail else ""))
            for _method, fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(error)
            await self.notifications.put({"method": "vira/eof",
                                          "params": {"error": str(error)}})

    async def _drain_stderr(self):
        while self.proc and self.proc.stderr:
            chunk = await self.proc.stderr.read(65536)
            if not chunk:
                return
            self.stderr_tail.extend(chunk)
            if len(self.stderr_tail) > 8192:
                del self.stderr_tail[:-8192]

    async def close(self):
        proc = self.proc
        if not proc:
            return
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()


def argv(binary):
    """The agent process. --no-auto-update keeps a background update check
    from stalling a session; model and effort are session options."""
    return [binary, "--no-auto-update", "agent", "stdio"]


def _allowed(result):
    return type(result).__name__ == "PermissionResultAllow"


def _text(result):
    return "\n".join(
        str(item.get("text") or "")
        for item in (result or {}).get("content") or []
        if isinstance(item, dict) and item.get("type") == "text")


def _tool_meta(tool_call):
    return ((tool_call.get("_meta") or {}).get("x.ai/tool") or {})


def mcp_tool_name(tool_call):
    """The MCP tool a use_tool call names ("vira__vault_search"), or ""."""
    raw = tool_call.get("rawInput") or {}
    if raw.get("variant") == "UseTool" or _tool_meta(tool_call).get("kind") == "use_tool":
        return str(raw.get("tool_name") or tool_call.get("title") or "")
    return ""


def tool_paths(tool_call):
    """Every path a tool call names, from the ACP locations and the raw input."""
    out = []
    for loc in tool_call.get("locations") or []:
        if isinstance(loc, dict) and loc.get("path"):
            out.append(str(loc["path"]))
    raw = tool_call.get("rawInput") or {}
    for key in ("file_path", "path", "target_path", "destination"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            out.append(raw[key].strip())
    meta_input = _tool_meta(tool_call).get("input") or {}
    if isinstance(meta_input.get("path"), str):
        out.append(meta_input["path"])
    seen = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen


def permission_checks(tool_call, spec):
    """Translate one Grok permission ask into Vira gate operations.

    [] means "already gated elsewhere": a Vira tool, whose gate runs inside
    the bridge. An edit that names no path is checked against the live root
    when the session is placed, so a write Grok could not describe is
    denied by the branch-first rule rather than waved through on cwd (the
    Codex grantRoot lesson, 2026-09-04)."""
    mcp = mcp_tool_name(tool_call)
    if mcp:
        if mcp.startswith(MCP_PREFIX) and viratools.has_tool(mcp[len(MCP_PREFIX):]):
            return []
        raw = tool_call.get("rawInput") or {}
        args = raw.get("tool_input")
        return [(f"mcp__{mcp}", args if isinstance(args, dict) else {})]
    raw = tool_call.get("rawInput") or {}
    kind = str(tool_call.get("kind") or _tool_meta(tool_call).get("kind") or "")
    title = str(tool_call.get("title") or _tool_meta(tool_call).get("name") or "tool")
    if kind == "execute":
        return [("Bash", {"command": str(raw.get("command") or title),
                          "cwd": spec.get("cwd")})]
    if kind in ("edit", "delete", "move"):
        paths = tool_paths(tool_call)
        if not paths:
            from . import agentbackend
            paths = [(spec.get("live_root") if agentbackend.placed(spec)
                      else spec.get("cwd")) or ""]
        return [("Edit", {"file_path": p}) for p in paths]
    if kind in ("read", "search"):
        paths = tool_paths(tool_call) or [spec.get("cwd") or ""]
        return [("Read" if kind == "read" else "Grep", {"file_path": p})
                for p in paths]
    if kind == "fetch":
        return [("WebFetch", {"url": str(raw.get("url") or title)})]
    return [(f"grok.{_tool_meta(tool_call).get('name') or title}",
             raw if isinstance(raw, dict) else {})]


def offered_models(options):
    """The model ids a session's configOptions offer, or [] if unstated."""
    for option in options or []:
        if isinstance(option, dict) and option.get("id") == "model":
            return [str(o.get("value")) for o in option.get("options") or []
                    if isinstance(o, dict) and o.get("value")]
    return []


def pick_option(options, allowed):
    """The ACP outcome for a gate verdict.

    Always a ONCE option when there is one. "Allow always" would let Grok
    stop asking for the rest of the session, and every later edit - one
    aimed at the live checkout included - would then skip the gate. Vira's
    own session grant (runner.session_allow) already spares the owner a
    second card, so the once answer costs nothing."""
    order = (("allow_once", "allow_always") if allowed
             else ("reject_once", "reject_always"))
    for kind in order:
        for option in options or []:
            if isinstance(option, dict) and option.get("kind") == kind:
                return {"outcome": {"outcome": "selected",
                                    "optionId": option.get("optionId")}}
    return {"outcome": {"outcome": "cancelled"}}


def render_tool(call):
    """One finished Grok tool call as a transcript line, or None."""
    kind = str(call.get("kind") or _tool_meta(call).get("kind") or "")
    raw = call.get("rawInput") or {}
    failed = call.get("status") == "failed"
    rejected = (call.get("_meta") or {}).get("toolNotRun") == "rejected"
    tail = " (denied)" if rejected else " (failed)" if failed else ""
    mcp = mcp_tool_name(call)
    if mcp:
        name = (f"vira.{mcp[len(MCP_PREFIX):]}" if mcp.startswith(MCP_PREFIX)
                else mcp)
        return f"  → {name}{tail}\n"
    if kind == "search_tool" or _tool_meta(call).get("name") == "search_tool":
        return None                  # Grok looking up its deferred tools
    if kind == "execute":
        command = str(raw.get("command") or "").strip()
        code = (call.get("rawOutput") or {}).get("exit_code")
        if code not in (None, 0) and not tail:
            tail = f" (exit {code})"
        return f"  → Bash: {command[:160]}{tail}\n" if command else None
    if kind in ("edit", "delete", "move"):
        names = [p[-60:] for p in tool_paths(call)]
        return f"  → Edit: {', '.join(names[:4])}{tail}\n" if names else f"  → Edit{tail}\n"
    if kind == "read":
        names = [p[-60:] for p in tool_paths(call)]
        return f"  → Read: {', '.join(names[:4])}{tail}\n" if names else None
    if kind == "search":
        query = raw.get("query") or raw.get("pattern") or ""
        return f"  → Grep: {str(query)[:120]}{tail}\n" if query else None
    if kind == "fetch":
        url = raw.get("url") or raw.get("query") or ""
        return f"  → WebFetch: {str(url)[:120]}{tail}\n" if url else None
    if kind == "think":
        return None
    title = call.get("title") or _tool_meta(call).get("label") or kind
    return f"  → {str(title)[:120]}{tail}\n" if title else None


def _native_name(call):
    """The ledger's name for a Grok tool call, in the SDK lane's vocabulary."""
    kind = str(call.get("kind") or _tool_meta(call).get("kind") or "")
    return {"execute": "Bash", "edit": "Edit", "delete": "Edit",
            "move": "Edit", "read": "Read", "search": "Grep",
            "fetch": "WebFetch"}.get(kind, f"grok.{_tool_meta(call).get('name') or kind or 'tool'}")


class GrokSession:
    def __init__(self, runner, binary, env):
        self.runner = runner
        self.spec = runner.spec
        cwd = os.path.abspath(self.spec["cwd"])   # session/new refuses a relative cwd
        self.rpc = AcpClient(argv(binary), cwd, env, self.handle_request)
        self.cwd = cwd
        self.session_id = ""
        self.bridge = None
        self.last_message = ""
        self.segment = ""
        self.message_id = uuid.uuid4().hex
        self.calls = {}
        self.open_receipts = set()
        self.denied = None

    # ------------------------------------------------- agent -> client ---

    async def interrupt(self):
        if self.session_id:
            await self.rpc.notify("session/cancel", {"sessionId": self.session_id})

    async def _gate(self, tool, inp):
        result = await self.runner.gate(tool, inp, None)
        return _allowed(result), str(getattr(result, "message", "") or "")

    async def handle_request(self, method, params):
        if method == "session/request_permission":
            tool_call = params.get("toolCall") or {}
            for tool, inp in permission_checks(tool_call, self.spec):
                allowed, message = await self._gate(tool, inp)
                if not allowed:
                    self.denied = message or "denied by the owner"
                    return pick_option(params.get("options"), False)
            return pick_option(params.get("options"), True)
        # fs/* and terminal/* are refused by capability at initialize; any
        # other agent request is one this client does not implement.
        raise NotImplementedError(f"Vira does not implement {method}")

    async def call_vira_tool(self, name, args):
        """The bridge's tools/call: the gate, then the registry, as on every
        other lane (functionagent.FunctionSession.invoke)."""
        fqname = f"mcp__vira__{name}"
        permission = await self.runner.gate(fqname, args, None)
        if not _allowed(permission):
            if hasattr(self.runner, "native_tool"):
                self.runner.native_tool(uuid.uuid4().hex, fqname, args,
                                        error="Denied by Vira's permission policy.",
                                        completed=True)
            return "Denied by Vira's permission policy.", True
        if not hasattr(self.runner, "start_tool"):
            self.runner.record_tool(fqname, args)
        result = await viratools.invoke(
            name, args, read_only=bool(self.spec.get("read_only")),
            ask_owner=self.runner.ask_owner,
            vault_destination=self.spec.get("vault_destination"),
            vault_context=self.spec.get("vault_context"),
            runtime=self.runner.state.get("runtime"),
            receipt=self.runner if hasattr(self.runner, "start_tool") else None)
        text = _text(result)
        return text, text.startswith("error:")

    # ----------------------------------------------------------- start ---

    def _rules(self):
        spec = self.spec
        return viratools.preamble(
            native=True,
            worktree_path=spec.get("worktree") or "",
            branch=spec.get("branch") or "",
            live_root=spec.get("live_root") or "",
            tool_prefix=MCP_PREFIX,
            vault_destination=spec.get("vault_destination"),
            vault_context=spec.get("vault_context"))

    async def _set_option(self, config_id, value):
        result = await self.rpc.request("session/set_config_option", {
            "sessionId": self.session_id, "configId": config_id,
            "value": value})
        return result.get("configOptions") or []

    async def start(self):
        init = await self.rpc.start()
        methods = [m.get("id") for m in init.get("authMethods") or []
                   if isinstance(m, dict)]
        if AUTH_METHOD not in methods:
            raise NotSignedIn(
                "the Grok CLI is not signed in - sign in from Config > AI "
                "(or run `grok login`) and start the session again")
        await self.rpc.request("authenticate", {
            "methodId": AUTH_METHOD, "_meta": {"headless": True}})
        self.bridge = await McpBridge(
            viratools.function_tool_specs(read_only=bool(self.spec.get("read_only"))),
            self.call_vira_tool).start()
        params = {"cwd": self.cwd, "mcpServers": [self.bridge.acp_server()],
                  "_meta": {"rules": self._rules()}}
        resume = (self.spec.get("resume_session") or "").strip()
        if resume:
            prior_id = self.spec.get("resumed_from") or ""
            prior = joblog.get_record(prior_id) if prior_id else None
            if not prior or prior.get("session_transport") != TRANSPORT:
                raise AcpUnavailable(
                    "cannot resume this session on Grok Build: it did not "
                    "start there")
            result = await self.rpc.request("session/load",
                                            dict(params, sessionId=resume))
            self.session_id = resume
            # The replay arrived before load answered; it is history.
            while not self.rpc.notifications.empty():
                message = self.rpc.notifications.get_nowait()
                if message.get("method") == "vira/eof":
                    raise AcpUnavailable(message["params"]["error"])
        else:
            result = await self.rpc.request("session/new", params)
            self.session_id = str(result.get("sessionId") or "")
        if not self.session_id:
            raise AcpUnavailable("grok agent returned no session id")
        options = result.get("configOptions") or []
        model = self.spec.get("model_resolved") or self.spec.get("model")
        if model:
            offered = offered_models(options)
            if offered and model not in offered:
                # Refuse by name rather than run another model: an API-lane
                # id, or one this account's plan does not include.
                raise AcpUnavailable(
                    f"{model} is not a model your Grok subscription offers "
                    f"here ({', '.join(offered)}) - pick one of those, or "
                    "leave the model empty for the CLI's default")
            options = await self._set_option("model", model)
        if self.spec.get("effort"):
            options = await self._set_option("reasoning_effort", self.spec["effort"])
        current = {o.get("id"): o.get("currentValue") for o in options
                   if isinstance(o, dict)}
        model = current.get("model") or model or "grok"
        effort = current.get("reasoning_effort") or self.spec.get("effort")
        self.runner.state["session_id"] = self.session_id
        self.runner.flush_state()
        joblog.record_session(self.spec["id"], self.session_id, transport=TRANSPORT)
        if hasattr(self.runner, "set_runtime"):
            self.runner.set_runtime(model, effort, source="provider_session",
                                    transport=TRANSPORT, backend="cli")
        self.runner.state["model_used"] = model
        joblog.record_model_used(self.spec["id"], model)
        self.runner.flush_state()
        self.runner.append(f"[vira] {model} working… "
                           f"(Grok session {self.session_id[:8]})\n")

    # ----------------------------------------------------------- turns ---

    def _close_segment(self):
        """A tool call ends the text before it. The answer is the text after
        the last tool call, as the SDK lane's last assistant message is."""
        if self.segment:
            self.runner.append("\n")
            if hasattr(self.runner, "publish_message"):
                self.runner.publish_message(self.message_id, self.segment,
                                            completed=True)
            self.last_message = self.segment
        self.segment = ""
        self.message_id = uuid.uuid4().hex

    def _handle_notification(self, message):
        method = message.get("method") or ""
        params = message.get("params") or {}
        if method == "vira/eof":
            raise AcpUnavailable(params.get("error") or "grok agent ended")
        if method != "session/update" or params.get("sessionId") != self.session_id:
            return                   # x.ai/* status, another session's news
        update = params.get("update") or {}
        kind = update.get("sessionUpdate") or ""
        if kind not in _SHOWN:
            return
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = str(content.get("text") or "") if content.get("type") == "text" else ""
            if text:
                self.segment += text
                self.runner.append(text)
                if hasattr(self.runner, "publish_message"):
                    self.runner.publish_message(self.message_id, text, delta=True)
            return
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return
        if kind == "tool_call":
            self._close_segment()
        call = self.calls.setdefault(call_id, {})
        for key, value in update.items():
            if key == "_meta" and isinstance(value, dict):
                # A rejection's update carries only {"toolNotRun": ...};
                # replacing the whole dict would lose which tool it was.
                call["_meta"] = {**(call.get("_meta") or {}), **value}
            elif key == "rawInput" and call.get("rawInput") and not value:
                continue
            elif value is not None:
                call[key] = value
        if _tool_meta(call).get("name") == "search_tool":
            # Grok looking up its own deferred tools: not work to record.
            if call.get("status") in ("completed", "failed"):
                self.calls.pop(call_id, None)
            return
        if mcp_tool_name(call).startswith(MCP_PREFIX):
            # viratools.invoke records the Vira call itself; a second
            # receipt here would count it twice.
            if call.get("status") in ("completed", "failed"):
                piece = render_tool(call)
                if piece:
                    self.runner.append(piece)
                self.calls.pop(call_id, None)
            return
        name = _native_name(call)
        args = {"command": (call.get("rawInput") or {}).get("command")} \
            if name == "Bash" else {"path": ", ".join(tool_paths(call))}
        if call.get("status") not in ("completed", "failed"):
            if kind == "tool_call" and hasattr(self.runner, "native_tool"):
                self.runner.native_tool(call_id, name, args)
                self.open_receipts.add(call_id)
            return
        piece = render_tool(call)
        if piece:
            self.runner.append(piece)
        if hasattr(self.runner, "native_tool"):
            error = None
            if call.get("status") == "failed":
                said = [str((c.get("content") or {}).get("text") or "")
                        for c in call.get("content") or []
                        if isinstance(c, dict) and isinstance(c.get("content"), dict)]
                error = " ".join(s for s in said if s)[:500] or f"{name}: failed"
            code = (call.get("rawOutput") or {}).get("exit_code")
            self.runner.native_tool(call_id, name, args,
                                    result={"structuredContent": {"exitCode": code,
                                                                  "provider_status": call.get("status")}},
                                    error=error, completed=True)
        else:
            self.runner.record_tool(name, {"query": str(args)[:200]})
        self.calls.pop(call_id, None)
        self.open_receipts.discard(call_id)

    def _close_open_receipts(self):
        """A cancelled turn can leave a started call with no final update;
        its receipt must not read "running" forever."""
        for call_id in list(self.open_receipts):
            call = self.calls.pop(call_id, {})
            self.runner.native_tool(call_id, _native_name(call), {},
                                    error="the turn ended before this call finished",
                                    completed=True)
        self.open_receipts.clear()

    async def _prompt(self, text):
        """One session/prompt; streams its updates. Returns the stop reason."""
        request = asyncio.ensure_future(self.rpc.request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}]}))
        try:
            while True:
                note = asyncio.ensure_future(self.rpc.notifications.get())
                done, _pending = await asyncio.wait(
                    {request, note}, return_when=asyncio.FIRST_COMPLETED)
                if note in done:
                    self._handle_notification(note.result())
                else:
                    note.cancel()
                if request in done:
                    # Updates read before the response line are this turn's.
                    while not self.rpc.notifications.empty():
                        self._handle_notification(self.rpc.notifications.get_nowait())
                    result = request.result()
                    usage = (result.get("_meta") or {}).get("usage")
                    if usage and hasattr(self.runner, "record_usage"):
                        self.runner.record_usage(usage)
                    return str(result.get("stopReason") or "")
        finally:
            if not request.done():
                request.cancel()

    async def run_turn(self, prompt):
        self.last_message = ""
        self.segment = ""
        if hasattr(self.runner, "begin_turn"):
            await self.runner.begin_turn()
        text = prompt
        resumes = 0
        try:
            while True:
                self.denied = None
                stop = await self._prompt(text)
                if (stop == "cancelled" and self.denied is not None
                        and not self.runner.interrupted and not self.runner.closing
                        and resumes < MAX_DENIAL_RESUMES):
                    resumes += 1
                    self._close_segment()
                    self.runner.append("[vira] denial sent back to Grok\n")
                    text = DENIAL_PROMPT.format(message=self.denied)
                    continue
                break
        except Exception:
            # The turn record must close whatever ended it; the runner
            # reports the error itself.
            self._close_segment()
            self._close_open_receipts()
            if hasattr(self.runner, "end_turn"):
                self.runner.end_turn("interrupted" if self.runner.interrupted
                                     else "failed", "")
            raise
        self._close_segment()
        self._close_open_receipts()
        if self.runner.interrupted:
            status = "interrupted"
        elif stop == "end_turn":
            status = "completed"
        else:
            status = "failed"
            self.runner.append(f"[vira] Grok turn ended: {stop or 'no stop reason'}"
                               + (" (denials kept coming)" if self.denied else "")
                               + "\n")
        if hasattr(self.runner, "end_turn"):
            self.runner.end_turn(status, self.last_message if status == "completed" else "")
        return self.last_message, status == "completed"

    async def close(self):
        await self.rpc.close()
        if self.bridge:
            await self.bridge.close()


async def run_session(runner, binary, env):
    """Run all turns of one detached Vira job over one `grok agent stdio`."""
    session = GrokSession(runner, binary, env)
    result_text = ""
    ok = False
    try:
        await session.start()
        runner.client = session
        prompt = runner.spec["prompt"]
        while True:
            last, ok = await session.run_turn(prompt)
            result_text = last if ok else ""
            if runner.closing:
                break
            steered = False
            while not runner.interrupted and not runner.inbox.empty():
                try:
                    item = runner.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is runner.END:
                    continue
                prompt = str(item or "").strip()
                if prompt:
                    steered = True
                    runner.finished_cleanly = False
                    runner.append("[vira] steering delivered as a new turn\n")
                    break
            if steered:
                continue
            from .runner import RESULT_KEEP
            runner.state["result_text"] = result_text[:RESULT_KEEP]
            runner.flush_state()
            park = runner.should_park(ok)
            if park and hasattr(runner, "offer_landing"):
                park = await runner.offer_landing()
            reply = await runner.await_reply() if park else None
            if reply is None:
                break
            runner.finished_cleanly = False
            runner.interrupted = False
            runner.append("[vira] reply delivered\n")
            runner.state["turn"] = int(runner.state.get("turn") or 0) + 1
            runner.flush_state()
            prompt = reply
        return result_text, ok
    except NotSignedIn as e:
        runner.append(f"[vira] {e}\n")
        return "", False
    finally:
        runner.client = None
        await session.close()

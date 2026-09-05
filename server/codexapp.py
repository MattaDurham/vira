"""Codex App Server adapter for first-class Vira sessions.

The old OpenAI lane drove ``codex exec --json`` one process per turn. That
made detached jobs and resume work, but the transport had no callback channel
for Vira's tools, approval cards, owner questions, or in-turn steering. App
Server is a bidirectional JSON-RPC protocol over stdio, so those become normal
requests on the same durable Runner control plane Claude already uses.

This module intentionally owns no Vira policy. The provider-neutral registry
lives in viratools; permission and owner interaction live on Runner. This file
only translates between Codex protocol values and those established contracts.
"""
import asyncio
import json

from . import joblog, viratools

JSONL_LIMIT = 8 * 1024 * 1024


class AppServerUnavailable(RuntimeError):
    """The installed Codex cannot establish the App Server protocol."""


class RpcError(RuntimeError):
    def __init__(self, method, error):
        self.method = method
        self.error = error
        if isinstance(error, dict):
            message = error.get("message") or json.dumps(error)
        else:
            message = str(error)
        super().__init__(f"{method}: {message}")


class JsonRpcClient:
    """Small newline-delimited JSON-RPC client for ``codex app-server``."""

    def __init__(self, binary, cwd, env, on_request):
        self.binary = binary
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
                self.binary, "app-server", "--stdio",
                # Vira supplies a session-scoped tool registry. Do not inherit
                # unrelated global MCP servers into a background Vira job.
                "-c", "mcp_servers={}",
                # A placed session runs workspace-write (see
                # agentbackend.sandbox_for), whose default cuts the network -
                # and Vira's own HTTP API on :8377 is the session's whole-store
                # fallback. Loopback reach is part of the contract, not a
                # sandbox escalation.
                "-c", "sandbox_workspace_write.network_access=true",
                cwd=self.cwd, env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=JSONL_LIMIT)
        except (OSError, ValueError) as e:
            raise AppServerUnavailable(str(e)) from e
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await asyncio.wait_for(self.request("initialize", {
                "clientInfo": {"name": "vira", "title": "Vira",
                               "version": "1"},
                "capabilities": {"experimentalApi": True},
            }), 10)
            await self.notify("initialized", {})
            return result
        except Exception as e:  # noqa: BLE001 — normalize protocol startup
            await self.close()
            if isinstance(e, AppServerUnavailable):
                raise
            raise AppServerUnavailable(str(e)) from e

    async def _write(self, payload):
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise AppServerUnavailable("Codex App Server is not running")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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

    async def _reply(self, req_id, result=None, error=None):
        payload = {"id": req_id}
        if error is None:
            payload["result"] = result if result is not None else {}
        else:
            payload["error"] = {"code": -32000, "message": str(error)}
        await self._write(payload)

    async def _serve_request(self, message):
        try:
            result = await self.on_request(
                message.get("method") or "", message.get("params") or {})
            await self._reply(message["id"], result=result)
        except Exception as e:  # noqa: BLE001 — return tool failure to Codex
            await self._reply(message["id"], error=e)

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
                if "method" in message:
                    if "id" in message:
                        asyncio.create_task(self._serve_request(message))
                    else:
                        await self.notifications.put(message)
                    continue
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
            error = AppServerUnavailable(
                f"Codex App Server exited {rc}" + (f": {detail}" if detail else ""))
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


def approval_policy(mode, placed=False):
    """Codex's approval vocabulary for a Vira rung.

    A PLACED bypass session asks `on-request` rather than `never`: its
    sandbox is workspace-write (agentbackend.sandbox_for says why), and with
    `never` a write outside the worktree would simply FAIL inside Codex -
    the live tree stays safe, but silently, with no request reaching the
    gate and no branch-first denial on the record. `on-request` turns that
    refusal into the escalation the gate exists to answer; the bypass rung
    inside the gate still allows everything else on sight.
    """
    if mode == "bypassPermissions":
        return "on-request" if placed else "never"
    if mode == "manual":
        return "untrusted"
    return "on-request"


def _allowed(result):
    return type(result).__name__ == "PermissionResultAllow"


def _result_text(result):
    parts = []
    for item in (result or {}).get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def _permission_checks(profile, live_root=""):
    """Translate a Codex permission expansion into Vira gate operations.

    Broad/glob write grants are checked against the live root deliberately:
    unlike a concrete path they may include it, and a branch-first session
    must never turn a sandbox expansion into a route around that backstop.
    """
    checks = []
    fs = profile.get("fileSystem") or {}
    for path in fs.get("read") or []:
        checks.append(("Read", {"file_path": str(path)}))
    for path in fs.get("write") or []:
        checks.append(("Edit", {"file_path": str(path)}))
    for entry in fs.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        access = entry.get("access") or "read"
        path = entry.get("path") or {}
        if isinstance(path, dict) and path.get("type") == "path":
            target = str(path.get("path") or "")
        elif access == "write":
            target = live_root or str(path)
        else:
            target = str(path)
        checks.append(("Edit" if access == "write" else "Read",
                       {"file_path": target, "permission": entry}))
    if (profile.get("network") or {}).get("enabled"):
        checks.append(("WebSearch", {"permission": "network"}))
    if not checks:
        checks.append(("Permissions", {"requested": profile}))
    return checks


def change_paths(item):
    """Every path a fileChange item touched, as Codex reported them."""
    out = []
    for change in item.get("changes") or []:
        if isinstance(change, dict):
            path = change.get("path") or change.get("filePath") or ""
            if path:
                out.append(str(path))
    return out


def render_item(item):
    """One completed App Server ThreadItem in Vira's transcript vocabulary."""
    kind = item.get("type") or ""
    if kind == "agentMessage":
        return None                 # streamed through agentMessage/delta
    if kind == "commandExecution":
        command = str(item.get("command") or "").strip()
        code = item.get("exitCode")
        tail = f" (exit {code})" if code not in (None, 0) else ""
        return f"  → Bash: {command[:160]}{tail}\n" if command else None
    if kind == "fileChange":
        names = [p[-60:] for p in change_paths(item)]
        return f"  → Edit: {', '.join(names[:4])}\n" if names else "  → Edit\n"
    if kind in ("dynamicToolCall", "mcpToolCall"):
        namespace = item.get("namespace") or item.get("server") or ""
        tool = item.get("tool") or "tool"
        label = f"{namespace}.{tool}" if namespace else tool
        return f"  → {label}\n"
    if kind == "webSearch":
        query = item.get("query") or ""
        return f"  → WebSearch: {str(query)[:120]}\n" if query else None
    if kind in ("reasoning", "plan", "userMessage"):
        return None
    return f"  → {kind}\n" if kind else None


class CodexSession:
    def __init__(self, runner, binary, env):
        self.runner = runner
        self.spec = runner.spec
        self.rpc = JsonRpcClient(binary, self.spec["cwd"], env,
                                 self.handle_request)
        self.thread_id = ""
        self.turn_id = ""
        self.last_message = ""
        self.streamed_items = set()

    async def interrupt(self):
        if self.thread_id and self.turn_id:
            await self.rpc.request("turn/interrupt", {
                "threadId": self.thread_id, "turnId": self.turn_id})

    async def _gate(self, tool, inp):
        before = tool in self.runner.session_allow
        result = await self.runner.gate(tool, inp, None)
        after = tool in self.runner.session_allow
        return _allowed(result), (after and not before)

    async def handle_request(self, method, params):
        if method == "item/tool/call":
            namespace = params.get("namespace")
            tool = params.get("tool") or ""
            if namespace not in (None, "", "vira") or not viratools.has_tool(tool):
                raise ValueError(f"unknown dynamic tool {namespace or ''}.{tool}")
            fqname = f"mcp__vira__{tool}"
            args = params.get("arguments")
            args = args if isinstance(args, dict) else {}
            allowed, _session = await self._gate(fqname, args)
            if not allowed:
                return {"success": False, "contentItems": [{
                    "type": "inputText", "text": "Denied by Vira's permission policy."}]}
            self.runner.record_tool(fqname, args)
            result = await viratools.invoke(
                tool, args, read_only=bool(self.spec.get("read_only")),
                ask_owner=self.runner.ask_owner)
            text = _result_text(result)
            return {"success": not text.startswith("error:"),
                    "contentItems": [{"type": "inputText", "text": text}]}

        if method == "item/commandExecution/requestApproval":
            command = params.get("command") or ""
            allowed, session_scope = await self._gate(
                "Bash", {"command": command, "cwd": params.get("cwd")})
            return {"decision": ("acceptForSession" if session_scope
                                  else "accept") if allowed else "decline"}

        if method == "item/fileChange/requestApproval":
            # This request is emitted ONLY for a write the sandbox blocked,
            # and for a PLACED (branch-first) session the sandbox is the
            # worktree - so an escalated file write is by definition outside
            # it. The request carries no changed path in this protocol
            # (grantRoot is null in codex-cli 0.153.1), and cwd IS the
            # worktree, so gating on cwd whitewashed every out-of-tree write:
            # the parity guard probe wrote the LIVE README this way
            # (2026-09-04, run_fb115f495e). Gate the concrete grantRoot when
            # present, else the live root itself, so runner.gate's
            # branch-first denial fires and is recorded - never cwd.
            from . import agentbackend
            grant = params.get("grantRoot")
            if agentbackend.placed(self.spec):
                target = grant or self.spec.get("live_root") or ""
            else:
                target = grant or self.spec.get("cwd") or ""
            allowed, session_scope = await self._gate(
                "Edit", {"file_path": target})
            return {"decision": ("acceptForSession" if session_scope
                                  else "accept") if allowed else "decline"}

        if method == "item/permissions/requestApproval":
            requested = params.get("permissions") or {}
            session_scope = False
            for tool, inp in _permission_checks(
                    requested, self.spec.get("live_root") or ""):
                allowed, this_scope = await self._gate(tool, inp)
                if not allowed:
                    return {"permissions": {}, "scope": "turn"}
                session_scope = session_scope or this_scope
            return {"permissions": requested,
                    "scope": "session" if session_scope else "turn"}

        if method == "item/tool/requestUserInput":
            answers = {}
            for question in params.get("questions") or []:
                qid = str(question.get("id") or "")
                if not qid:
                    continue
                if question.get("isSecret"):
                    answers[qid] = {"answers": []}
                    continue
                answer = await self.runner.ask_owner(
                    question.get("question") or question.get("header") or "Question",
                    question.get("options") or [], bool(question.get("isOther", True)))
                answers[qid] = {"answers": [answer] if answer else []}
            return {"answers": answers}

        if method == "mcpServer/elicitation/request":
            if params.get("mode") == "url":
                return {"action": "decline"}
            schema = params.get("requestedSchema") or {}
            content = {}
            for name, field in (schema.get("properties") or {}).items():
                choices = [{"label": str(v), "description": ""}
                           for v in (field.get("enum") or [])]
                answer = await self.runner.ask_owner(
                    field.get("title") or params.get("message") or str(name),
                    choices, not bool(choices))
                content[name] = answer
            return {"action": "accept", "content": content}

        raise ValueError(f"unsupported Codex App Server request: {method}")

    def _thread_params(self):
        from . import agentbackend
        spec = self.spec
        params = {
            "cwd": spec["cwd"],
            "runtimeWorkspaceRoots": [spec["cwd"]],
            "sandbox": agentbackend.sandbox_for(spec),
            "approvalPolicy": approval_policy(spec.get("mode"),
                                              agentbackend.placed(spec)),
            "approvalsReviewer": "user",
            "developerInstructions": viratools.preamble(
                native=True,
                worktree_path=spec.get("worktree") or "",
                branch=spec.get("branch") or "",
                live_root=spec.get("live_root") or "",
                tool_prefix="vira."),
        }
        model = spec.get("model_resolved") or spec.get("model")
        if model:
            params["model"] = model
        return params

    async def start(self):
        await self.rpc.start()
        params = self._thread_params()
        resume = (self.spec.get("resume_session") or "").strip()
        if resume:
            # Threads created by compatibility `codex exec` predate dynamic
            # tools, and thread/resume has no parameter that can add them.
            # Resume those through the old adapter instead of claiming a
            # native session whose Vira tools are silently absent.
            prior_id = self.spec.get("resumed_from") or ""
            prior = joblog.get_record(prior_id) if prior_id else None
            if not prior or prior.get("session_transport") != "codex-app-server":
                raise AppServerUnavailable(
                    "this is a legacy Codex thread; native Vira tools can "
                    "only be restored by its compatibility transport")
            params["threadId"] = resume
            result = await self.rpc.request("thread/resume", params)
        else:
            params["dynamicTools"] = viratools.dynamic_tool_specs(
                read_only=bool(self.spec.get("read_only")))
            result = await self.rpc.request("thread/start", params)
        thread = result.get("thread") or {}
        self.thread_id = thread.get("id") or resume
        if not self.thread_id:
            raise RuntimeError("Codex App Server returned no thread id")
        self.runner.state["session_id"] = self.thread_id
        self.runner.flush_state()
        joblog.record_session(self.spec["id"], self.thread_id,
                              transport="codex-app-server")
        model = result.get("model") or self.spec.get("model_resolved") or "Codex"
        if model:
            self.runner.state["model_used"] = model
            joblog.record_model_used(self.spec["id"], model)
        self.runner.flush_state()
        self.runner.append(f"[vira] {model} working… "
                           f"(Codex thread {self.thread_id[:8]})\n")

    async def _handle_notification(self, message):
        method = message.get("method") or ""
        params = message.get("params") or {}
        if params.get("threadId") not in (None, self.thread_id):
            return None
        if method == "item/agentMessage/delta":
            item_id = params.get("itemId") or ""
            self.streamed_items.add(item_id)
            delta = params.get("delta") or ""
            self.runner.append(delta)
            return None
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                self.last_message = item.get("text") or self.last_message
                if item.get("id") not in self.streamed_items and self.last_message:
                    self.runner.append(self.last_message + "\n")
                elif item.get("id") in self.streamed_items:
                    self.runner.append("\n")
            else:
                piece = render_item(item)
                if piece:
                    self.runner.append(piece)
                if item.get("type") == "commandExecution":
                    self.runner.record_tool("Bash", {
                        "query": (item.get("command") or "")[:200]})
                elif item.get("type") == "fileChange":
                    # Codex's own file tool is a write the ledger must see:
                    # the guard probe found an edit the record did not carry.
                    self.runner.record_tool("Edit", {
                        "path": ", ".join(change_paths(item))[:200]})
            return None
        if method == "error":
            error = params.get("error") or {}
            self.runner.append(f"[vira] Codex error: "
                               f"{error.get('message') or error}\n")
            return None
        if method == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("id") != self.turn_id:
                return None
            status = turn.get("status") or "failed"
            error = turn.get("error") or {}
            if status == "failed":
                self.runner.append(f"[vira] Codex turn failed: "
                                   f"{error.get('message') or 'unknown error'}\n")
            return status
        if method == "vira/eof":
            raise AppServerUnavailable(params.get("error") or "App Server ended")
        return None

    async def run_turn(self, prompt):
        self.last_message = ""
        response = await self.rpc.request("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}],
        })
        turn = response.get("turn") or {}
        self.turn_id = turn.get("id") or ""
        if not self.turn_id:
            raise RuntimeError("Codex App Server returned no turn id")
        while True:
            notification = asyncio.create_task(self.rpc.notifications.get())
            steering = asyncio.create_task(self.runner.inbox.get())
            done, pending = await asyncio.wait(
                (notification, steering), return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if notification in done:
                status = await self._handle_notification(notification.result())
                if status:
                    # A steer and completion can cross on the same event-loop
                    # tick. Preserve the owner's text for the next turn rather
                    # than consuming it with a now-finished expectedTurnId.
                    if steering in done:
                        self.runner.inbox.put_nowait(steering.result())
                    self.turn_id = ""
                    return self.last_message, status == "completed"
            if steering in done:
                text = steering.result()
                if text is self.runner.END:
                    continue
                text = str(text or "").strip()
                if text:
                    await self.rpc.request("turn/steer", {
                        "threadId": self.thread_id,
                        "expectedTurnId": self.turn_id,
                        "input": [{"type": "text", "text": text}],
                    })
                    self.runner.finished_cleanly = False
                    self.runner.append("[vira] steering delivered\n")
                continue

    async def close(self):
        await self.rpc.close()


async def run_session(runner, binary, env):
    """Run all turns for one detached Vira job over a persistent App Server."""
    session = CodexSession(runner, binary, env)
    started = False
    result_text = ""
    ok = False
    try:
        await session.start()
        started = True
        runner.client = session
        prompt = runner.spec["prompt"]
        while True:
            last, ok = await session.run_turn(prompt)
            result_text = last or result_text
            if runner.closing or runner.interrupted:
                break
            steered = False
            while not runner.inbox.empty():
                try:
                    item = runner.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is runner.END:
                    continue
                prompt = item
                steered = True
                runner.finished_cleanly = False
                runner.append("[vira] steering delivered as a new turn\n")
                break
            if steered:
                continue
            from .runner import RESULT_KEEP
            runner.state["result_text"] = result_text[:RESULT_KEEP]
            runner.flush_state()
            reply = (await runner.await_reply()
                     if ok and runner.parks_at_turn_end() else None)
            if reply is None:
                break
            runner.finished_cleanly = False
            runner.interrupted = False
            runner.append("[vira] reply delivered\n")
            runner.state["turn"] = int(runner.state.get("turn") or 0) + 1
            runner.flush_state()
            prompt = reply
        return result_text, ok
    except AppServerUnavailable:
        if not started:
            raise
        raise RuntimeError("Codex App Server ended after the session started")
    finally:
        runner.client = None
        await session.close()

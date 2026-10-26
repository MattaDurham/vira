"""A session-scoped MCP server that hands Vira's tool registry to an agent
CLI which only takes tools as MCP servers.

Grok Build over ACP (grokacp.py) accepts `mcpServers` on session/new and
cannot be handed in-process tools the way the Claude SDK and Codex App
Server can. So the runner process that owns the session serves its own
registry here, over MCP's Streamable HTTP transport, on a loopback port
that lives exactly as long as the session. Every tools/call runs inside
that runner, so it goes through the same runner.gate, ask_owner card and
receipt as any other provider's Vira tool call: this module owns transport
only, never policy.

Deliberately small: one POSTed JSON-RPC message in, one JSON body out (the
spec lets a server answer a request with plain application/json), no SSE
stream (a GET is answered 405, which the spec allows), no resumability. A
random bearer token, compared in constant time, keeps any other local
process from calling the tools; the agent gets it in the server's headers.
"""
import asyncio
import hmac
import json
import secrets

# Versions this server speaks. The client's requested version is echoed when
# known; otherwise the newest here is offered and the client decides.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# One MCP message per request. A Vira tool call's arguments are small; this
# bounds memory against a runaway client, not a real payload.
MAX_BODY = 4 * 1024 * 1024
# A connection that sends nothing for this long is dropped. The agent opens
# a fresh one per call when it needs to.
IDLE_TIMEOUT = 300
SERVER_NAME = "vira"


class McpBridge:
    """Serve `tools` (name, description, parameters rows, the shape
    viratools.function_tool_specs returns) and run calls through
    `on_call(name, arguments) -> (text, is_error)`, an async callable."""

    def __init__(self, tools, on_call):
        self.tools = list(tools)
        self.on_call = on_call
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.port = 0

    @property
    def url(self):
        # Numeric loopback on purpose: this address is dialled by the agent
        # CLI's own HTTP client, not by an agent harness that blocks
        # 127.0.0.1, and "localhost" may resolve to ::1, which this socket
        # does not listen on.
        return f"http://127.0.0.1:{self.port}/mcp"

    def acp_server(self):
        """This bridge as one entry of ACP session/new `mcpServers`."""
        return {"type": "http", "name": SERVER_NAME, "url": self.url,
                "headers": [{"name": "Authorization",
                             "value": f"Bearer {self.token}"}]}

    async def start(self):
        self.server = await asyncio.start_server(
            self._serve, host="127.0.0.1", port=0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass
            self.server = None

    # ------------------------------------------------------------ HTTP ---

    async def _serve(self, reader, writer):
        try:
            while True:
                request = await asyncio.wait_for(self._read_request(reader),
                                                 IDLE_TIMEOUT)
                if request is None:
                    return
                method, path, headers, body = request
                status, payload, extra = await self._route(method, path,
                                                           headers, body)
                await self._respond(writer, status, payload, extra)
                if headers.get("connection", "").lower() == "close":
                    return
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - the peer may already be gone
                pass

    async def _read_request(self, reader):
        line = await reader.readline()
        if not line:
            return None
        try:
            method, path, _version = line.decode("latin-1").strip().split(" ", 2)
        except ValueError:
            return None
        headers = {}
        while True:
            raw = await reader.readline()
            if raw in (b"\r\n", b"\n", b""):
                break
            name, _, value = raw.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        body = b""
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await reader.readline()
                    break
                if len(body) + size > MAX_BODY:
                    return None
                body += await reader.readexactly(size)
                await reader.readline()
        else:
            length = int(headers.get("content-length") or 0)
            if length > MAX_BODY:
                return None
            if length:
                body = await reader.readexactly(length)
        return method.upper(), path, headers, body

    async def _respond(self, writer, status, payload, extra=None):
        reasons = {200: "OK", 202: "Accepted", 400: "Bad Request",
                   401: "Unauthorized", 404: "Not Found",
                   405: "Method Not Allowed"}
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        head = [f"HTTP/1.1 {status} {reasons.get(status, 'OK')}",
                f"Content-Length: {len(body)}"]
        if payload is not None:
            head.append("Content-Type: application/json")
        for name, value in (extra or {}).items():
            head.append(f"{name}: {value}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()

    async def _route(self, method, path, headers, body):
        if path.split("?", 1)[0].rstrip("/") != "/mcp":
            return 404, None, {}
        given = headers.get("authorization", "")
        if not hmac.compare_digest(given.encode("utf-8"),
                                   f"Bearer {self.token}".encode("utf-8")):
            return 401, None, {}
        if method == "DELETE":
            return 200, None, {}
        if method != "POST":
            # No server-initiated stream: every answer rides its POST.
            return 405, None, {"Allow": "POST, DELETE"}
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, _error(None, -32700, "parse error"), {}
        if isinstance(message, list):
            # 2025-03-26 batches; answer each request, skip notifications.
            out = [r for r in [await self._message(m) for m in message] if r]
            return (200, out, {}) if out else (202, None, {})
        reply = await self._message(message)
        return (200, reply, {}) if reply else (202, None, {})

    # ------------------------------------------------------------- MCP ---

    async def _message(self, message):
        if not isinstance(message, dict) or "method" not in message:
            return None                 # a response or junk: nothing to say
        rid = message.get("id")
        if rid is None:
            return None                 # a notification (initialized, ...)
        method = message.get("method") or ""
        params = message.get("params") or {}
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            return _result(rid, {
                "protocolVersion": (asked if asked in PROTOCOL_VERSIONS
                                    else PROTOCOL_VERSIONS[0]),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Vira",
                               "version": "1"}})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t["parameters"]} for t in self.tools]})
        if method == "tools/call":
            name = str(params.get("name") or "")
            if name not in {t["name"] for t in self.tools}:
                return _error(rid, -32602, f"unknown tool: {name}")
            args = params.get("arguments")
            try:
                text, is_error = await self.on_call(
                    name, args if isinstance(args, dict) else {})
            except Exception as e:  # noqa: BLE001 - a tool failure is a result
                text, is_error = f"error: {e}", True
            return _result(rid, {"content": [{"type": "text", "text": text}],
                                 "isError": bool(is_error)})
        return _error(rid, -32601, f"method not found: {method}")


def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}

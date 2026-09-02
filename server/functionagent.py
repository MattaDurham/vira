"""Provider-neutral function-calling sessions for Gemini and Grok.

These providers do not expose Codex App Server or Claude Agent SDK's local
coding harness. Vira therefore supplies the part it owns: the same native
tool registry, permission gate, owner-question cards, durable history, reply
window, and branch/read-only policy. Capability reporting keeps workspace
shell/file tools and in-flight interruption false until a provider transport
can actually supply them.
"""
import asyncio
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import joblog, models, settings, viratools

SESSION_DIR = settings.ROOT / "data" / "model-sessions"
MAX_TOOL_ROUNDS = 32


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _post(url, body, headers, timeout):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[-800:]
        raise RuntimeError(f"model API returned HTTP {e.code}: {detail}") from e


def _text(result):
    return "\n".join(
        str(item.get("text") or "")
        for item in (result or {}).get("content") or []
        if isinstance(item, dict) and item.get("type") == "text")


def _allowed(result):
    return type(result).__name__ == "PermissionResultAllow"


class FunctionSession:
    def __init__(self, runner, provider, key, model, timeout=120):
        self.runner = runner
        self.spec = runner.spec
        self.provider = provider
        self.key = key
        self.model = model
        self.timeout = timeout
        self.session_id = ""
        self.path = None
        self.data = {}
        self.tools = viratools.function_tool_specs(
            read_only=bool(self.spec.get("read_only")))

    async def interrupt(self):
        # urllib has no cooperative in-flight cancellation. Runner has already
        # marked the turn interrupted; the bounded HTTP timeout is the hard
        # stop and capability reporting discloses this limitation.
        return None

    def _transport(self):
        return f"{self.provider}-function-api"

    def start(self):
        resume = (self.spec.get("resume_session") or "").strip()
        if resume:
            prior_id = self.spec.get("resumed_from") or ""
            prior = joblog.get_record(prior_id) if prior_id else None
            if not prior or prior.get("session_transport") != self._transport():
                raise RuntimeError(
                    f"cannot resume {self.provider} session: its durable "
                    "function-call history is unavailable")
            self.session_id = resume
        else:
            self.session_id = uuid.uuid4().hex
        self.path = SESSION_DIR / self.provider / f"{self.session_id}.json"
        self.data = _read(self.path)
        if not self.data:
            self.data = {"provider": self.provider, "model": self.model,
                         "contents": [], "messages": []}
        self.runner.state["session_id"] = self.session_id
        self.runner.state["model_used"] = self.model
        self.runner.flush_state()
        joblog.record_session(self.spec["id"], self.session_id,
                              transport=self._transport())
        joblog.record_model_used(self.spec["id"], self.model)
        self.runner.append(
            f"[vira] {models.PROVIDERS[self.provider]['sub_name']} "
            f"{self.model} working… (Vira function session "
            f"{self.session_id[:8]})\n")

    async def invoke(self, name, arguments):
        args = arguments if isinstance(arguments, dict) else {}
        fqname = f"mcp__vira__{name}"
        if not viratools.has_tool(name):
            return f"error: unknown Vira tool {name}"
        permission = await self.runner.gate(fqname, args, None)
        if not _allowed(permission):
            return "Denied by Vira's permission policy."
        self.runner.record_tool(fqname, args)
        result = await viratools.invoke(
            name, args, read_only=bool(self.spec.get("read_only")),
            ask_owner=self.runner.ask_owner)
        return _text(result)

    def _preamble(self):
        return viratools.preamble(
            native=True, worktree_path=self.spec.get("worktree") or "",
            branch=self.spec.get("branch") or "",
            live_root=self.spec.get("live_root") or "", tool_prefix="")

    async def _google(self, prompt):
        contents = list(self.data.get("contents") or [])
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        last_text = ""
        for _round in range(MAX_TOOL_ROUNDS):
            body = {
                "systemInstruction": {"parts": [{"text": self._preamble()}]},
                "contents": contents,
                "tools": [{"functionDeclarations": [
                    {"name": row["name"], "description": row["description"],
                     "parameters": row["parameters"]} for row in self.tools]}],
            }
            payload = await asyncio.to_thread(
                _post,
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent", body,
                {"x-goog-api-key": self.key}, self.timeout)
            candidates = payload.get("candidates") or []
            if not candidates:
                raise RuntimeError("Gemini returned no candidates")
            content = (candidates[0].get("content") or {})
            parts = content.get("parts") or []
            contents.append(content)
            text = "".join(str(part.get("text") or "") for part in parts)
            if text:
                last_text = text
            calls = [part.get("functionCall") for part in parts
                     if isinstance(part, dict) and part.get("functionCall")]
            if not calls:
                self.data["contents"] = contents
                _write(self.path, self.data)
                return last_text
            responses = []
            for call in calls:
                name = str(call.get("name") or "")
                result = await self.invoke(name, call.get("args") or {})
                responses.append({"functionResponse": {
                    "name": name, "response": {"result": result}}})
            contents.append({"role": "user", "parts": responses})
            self.data["contents"] = contents
            _write(self.path, self.data)
        raise RuntimeError("Gemini exceeded Vira's tool-call round limit")

    async def _xai(self, prompt):
        messages = list(self.data.get("messages") or [])
        messages.append({"role": "user", "content": prompt})
        last_text = ""
        for _round in range(MAX_TOOL_ROUNDS):
            # xAI's chat-completions contract accepts full local history,
            # including assistant tool_calls and role=tool results. Keeping
            # that history under data/model-sessions makes resume independent
            # of provider-side response retention.
            body = {
                "model": self.model,
                "messages": ([{"role": "system", "content": self._preamble()}]
                             + messages),
                "tools": [{"type": "function", "function": {
                    "name": row["name"], "description": row["description"],
                    "parameters": row["parameters"]}} for row in self.tools],
            }
            payload = await asyncio.to_thread(
                _post, "https://api.x.ai/v1/chat/completions", body,
                {"authorization": "Bearer " + self.key}, self.timeout)
            choices = payload.get("choices") or []
            if not choices:
                raise RuntimeError("Grok returned no choices")
            message = choices[0].get("message") or {}
            messages.append(message)
            text = message.get("content") or ""
            if text:
                last_text = str(text)
            calls = message.get("tool_calls") or []
            if not calls:
                self.data["messages"] = messages
                _write(self.path, self.data)
                return last_text
            for call in calls:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self.invoke(
                    str(function.get("name") or ""), args)
                messages.append({"role": "tool",
                                 "tool_call_id": call.get("id"),
                                 "content": result})
            self.data["messages"] = messages
            _write(self.path, self.data)
        raise RuntimeError("Grok exceeded Vira's tool-call round limit")

    async def turn(self, prompt):
        if self.provider == "google":
            return await self._google(prompt)
        if self.provider == "xai":
            return await self._xai(prompt)
        raise RuntimeError(f"no function adapter for {self.provider}")


async def run_session(runner, provider):
    """Run a Gemini/Grok session on Runner's durable control plane."""
    key = models.api_key(provider)
    if not key:
        raise RuntimeError(
            f"{models.PROVIDERS[provider]['sub_name']} needs an API key")
    model = (runner.spec.get("model_resolved") or runner.spec.get("model")
             or "")
    if not model:
        model = await asyncio.to_thread(models.default_api_model, provider)
    if not model:
        raise RuntimeError(f"no verified {provider} model is configured")
    session = FunctionSession(runner, provider, key, model)
    result_text = ""
    ok = False
    try:
        session.start()
        runner.client = session
        prompt = runner.spec["prompt"]
        while True:
            result_text = await session.turn(prompt)
            ok = True
            if result_text:
                runner.append(result_text + "\n")
            if runner.closing or runner.interrupted:
                break
            steered = False
            while not runner.inbox.empty():
                item = runner.inbox.get_nowait()
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
            reply = (await runner.await_reply()
                     if runner.parks_at_turn_end() else None)
            if reply is None:
                break
            runner.finished_cleanly = False
            runner.interrupted = False
            runner.append("[vira] reply delivered\n")
            runner.state["turn"] = int(runner.state.get("turn") or 0) + 1
            runner.flush_state()
            prompt = reply
        return result_text, ok
    finally:
        runner.client = None

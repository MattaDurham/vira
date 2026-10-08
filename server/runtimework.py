"""In-process work that a deliberate restart must disclose and drain."""
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps

lock = threading.RLock()
_admission = threading.Condition(lock)
_active = {}
_frozen = False


class Restarting(ValueError):
    pass


@contextmanager
def activity(title, kind="background", wait=False):
    token = uuid.uuid4().hex
    with lock:
        # A periodic worker pauses during an update instead of dying from
        # admission refusal. If the update fails or finds nothing to pull,
        # thaw wakes it; a successful restart replaces the whole process.
        while _frozen and wait:
            _admission.wait()
        if _frozen:
            raise Restarting("Vira is restarting; try again after it reconnects")
        _active[token] = {"id": token, "title": title, "kind": kind,
                          "started": time.time(), "survives": False,
                          "detail": "Interrupted by an immediate restart."}
    try:
        yield
    finally:
        with lock:
            _active.pop(token, None)


def tracked(title):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            with activity(title, wait=True):
                return fn(*args, **kwargs)
        return run
    return decorate


def snapshot():
    with lock:
        return [dict(row) for row in _active.values()]


def freeze(value):
    global _frozen
    with lock:
        _frozen = value
        if not value:
            _admission.notify_all()


class Middleware:
    """Track handlers through their response, including generated streams.

    The event subscription is passive, so it is never a restart blocker.
    Restart controls themselves must remain usable while the app drains.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or not path.startswith("/api/")
                or path == "/api/stream" or path.startswith("/api/restart")
                or path == "/api/update/apply"):
            return await self.app(scope, receive, send)
        # Enter separately: a ValueError raised BY a handler must not be
        # mistaken for admission refusal after it already sent a response.
        names = {"brief": "Preparing the daily brief", "world": "Loading World",
                 "evidence": "Preparing evidence", "find": "Searching your sources",
                 "chat": "Generating a chat reply", "send": "Sending a message",
                 "research": "Research request", "reader": "Loading the library"}
        section = path.split("/")[2]
        title = names.get(section, "Loading app data" if scope["method"] == "GET"
                          else "Saving app changes")
        ctx = activity(title, "request")
        try:
            ctx.__enter__()
        except Restarting as exc:
            from fastapi.responses import JSONResponse
            return await JSONResponse({"detail": str(exc)}, status_code=503,
                                      headers={"Retry-After": "2"})(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            ctx.__exit__(None, None, None)

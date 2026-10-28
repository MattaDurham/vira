"""In-process work that a deliberate restart must disclose and drain, and
that Fleet shows as what is running inside each Vira instance."""
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps

lock = threading.RLock()
_admission = threading.Condition(lock)
_active = {}
_frozen = False
# The activity each thread is inside, innermost last, so progress() can
# find its own row without the caller threading a token through.
_current = threading.local()


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
                          "detail": "Interrupted by an immediate restart.",
                          "done": None, "total": None, "step": ""}
    stack = getattr(_current, "stack", None)
    if stack is None:
        stack = _current.stack = []
    stack.append(token)
    try:
        yield
    finally:
        if stack and stack[-1] == token:
            stack.pop()
        with lock:
            _active.pop(token, None)


def progress(done=None, total=None, step=None):
    """Report how far the current thread's activity is: `done` of `total`
    items, and `step`, the thing being worked on now. Fleet shows the
    row with these; a thread outside any activity is ignored."""
    stack = getattr(_current, "stack", None)
    if not stack:
        return
    with lock:
        row = _active.get(stack[-1])
        if row is None:
            return
        if done is not None:
            row["done"] = done
        if total is not None:
            row["total"] = total
        if step is not None:
            row["step"] = str(step)[:120]


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

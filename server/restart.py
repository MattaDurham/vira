"""A cancellable, server-owned restart queue. No durable owner state."""
import threading
import uuid

from . import runtimework, update


def activities():
    from . import atlas, circuits, session
    rows = runtimework.snapshot()
    # Fail closed: if a source cannot be read, a queued restart must not
    # turn missing evidence into "nothing running".
    for name, read in (("sessions", session.sessions.restart_activity),
                       ("flows", circuits.restart_activity)):
        try:
            rows.extend(read())
        except Exception as exc:
            rows.append({"id": name, "kind": "unknown", "title": f"Could not check {name}",
                         "detail": str(exc), "survives": False})
    if atlas._building.is_set():
        rows.append({"id": "atlas", "kind": "background", "title": "Contact Atlas rebuild",
                     "detail": "Interrupted by an immediate restart.", "survives": False})
    return rows


def _schedule(callback):
    timer = threading.Timer(1.0, callback)  # let the response flush before any restart
    timer.daemon = True
    timer.start()


class Coordinator:
    def __init__(self, read=activities, restart=update._restart, schedule=_schedule):
        self.read = read
        self.restart = restart
        self.schedule = schedule
        self.lock = threading.RLock()
        self.boot_id = uuid.uuid4().hex
        self.phase = "idle"
        self.operation = "restart"
        self.mode = "when_idle"
        self.error = ""
        self.note = ""
        self.ticket = None

    def status(self):
        with self.lock:
            kind, name = update.supervisor()
            return {"boot_id": self.boot_id, "phase": self.phase,
                    "operation": self.operation, "mode": self.mode,
                    "error": self.error, "note": self.note,
                    "available": bool(name), "supervisor": kind,
                    "unavailable_reason": "" if name else
                    "Automatic restart needs a configured startup service. "
                    "Set up Vira's startup service, or restart from a terminal.",
                    "activities": self.read()}

    def request(self, mode="when_idle", operation="restart"):
        if mode not in ("now", "when_idle") or operation not in ("restart", "update"):
            raise ValueError("unknown restart mode or operation")
        with self.lock:
            if not update.supervisor()[1]:
                raise ValueError(self.status()["unavailable_reason"])
            if self.phase in ("waiting", "restarting", "updating"):
                raise ValueError("A restart is already queued or in progress")
            self.operation, self.mode, self.error = operation, mode, ""
            self.note = ""
            self.phase = "waiting"
            self.ticket = uuid.uuid4().hex
            ticket = self.ticket
            self.schedule(lambda: self._tick(ticket, mode))
            return self.status()

    def cancel(self):
        with self.lock:
            if self.phase in ("restarting", "updating"):
                raise ValueError("Restart has already begun")
            self.ticket = None
            self.phase, self.error, self.note = "idle", "", ""
            return self.status()

    def _tick(self, ticket, mode):
        with self.lock:
            if ticket != self.ticket or self.phase != "waiting":
                return
            # Freeze admission AND inspect under the same lock used by
            # new work. Work arriving during the wait joins the drain.
            with runtimework.lock:
                runtimework.freeze(True)
                try:
                    if not update.supervisor()[1]:
                        raise ValueError("Startup service is no longer configured; restart cancelled")
                    # Existing in-process work is already enough to wait.
                    # Avoid acquiring store locks it may itself be holding.
                    busy = ((runtimework.snapshot() or self.read())
                            if mode == "when_idle" else [])
                except Exception as exc:
                    runtimework.freeze(False)
                    self.phase, self.error = "failed", str(exc)
                    return
                if mode == "when_idle" and busy:
                    runtimework.freeze(False)
                    self.schedule(lambda: self._tick(ticket, mode))
                    return
                self.phase = "updating" if self.operation == "update" else "restarting"
        try:
            if self.operation == "update":
                result = update.pull()
                if not result.get("updated"):
                    with self.lock:
                        runtimework.freeze(False)
                        self.phase, self.ticket = "idle", None
                        self.note = result.get("note") or "Already up to date; no restart needed."
                    return
            self.restart()
        except Exception as exc:
            with self.lock:
                runtimework.freeze(False)
                self.phase, self.error, self.ticket = "failed", str(exc), None


coordinator = Coordinator()

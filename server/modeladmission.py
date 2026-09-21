"""Cross-process admission for model execution, shared by agents and drafts.

SQLite transactions reserve a slot before execution. Foreground work has first
claim on queued capacity; a reserved slot keeps background work from occupying
the entire pool. No running operation is killed or evicted. Parked turns release
their lease, and must re-enter the queue before executing again. Dead processes
are reclaimed using OS liveness, never a timeout guessed to mean death.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

STORE = Path(__file__).resolve().parent.parent / "data" / "model-admission.sqlite3"
PRIORITY = {"foreground": 0, "background": 1, "auxiliary": 2}
# Admission waits bound waiting without cancelling someone else's computation.
QUEUE_TIMEOUT = 120.0
POLL_S = 0.1
MAX_QUEUED = 256  # bounds waiting runner/process pressure, not retained history
_BORROW_LOCK = threading.Lock()
_BORROWERS = {}


class QueueTimeout(TimeoutError):
    pass


def _alive(pid):
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless probe on every Windows runtime.
        # Query the process handle; never send a termination signal.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() != 87  # only invalid PID proves absence
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # an observation failure is not proof of process death


def _capacity():
    from . import settings
    try:
        return max(1, int(settings.raw().get("session_max_live", 4)))
    except (TypeError, ValueError):
        return 4


@contextmanager
def _connect(path=None):
    path = Path(path or STORE)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, "
                   "pid INTEGER NOT NULL, owner TEXT, work_class TEXT NOT NULL, "
                   "priority INTEGER NOT NULL, status TEXT NOT NULL, "
                   "created REAL NOT NULL, started REAL, finished REAL, reason TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS ticket_queue ON tickets(status,priority,created)")
        with db:
            yield db
    finally:
        db.close()


def borrowed(receipt):
    """A nested transformation may use its parent's still-held execution slot."""
    if not isinstance(receipt, dict) or receipt.get("pid") != os.getpid():
        return False
    with _connect(receipt.get("path")) as db:
        row = db.execute("SELECT status,pid FROM tickets WHERE id=?", (receipt.get("id"),)).fetchone()
        return bool(row and row[0] == "running" and row[1] == os.getpid())


@contextmanager
def borrow_execution(receipt, timeout=QUEUE_TIMEOUT):
    """Serialize nested model transformations under one parent execution slot.

    A tool can fan out local reads. Its nested model calls still consume only
    one execution at a time, avoiding both cap=1 deadlocks and slot borrowing
    that would otherwise multiply simultaneous model calls without a bound.
    """
    key = (str(receipt.get("path")), receipt.get("id"))
    with _BORROW_LOCK:
        entry = _BORROWERS.setdefault(key, {"lock": threading.RLock(), "users": 0})
        entry["users"] += 1
    acquired = False
    try:
        acquired = entry["lock"].acquire(timeout=max(0, float(timeout)))
        if not acquired:
            raise QueueTimeout("nested model queue wait expired; no model call was started")
        if borrowed(receipt):
            yield
        else:
            with execution("nested completion", "auxiliary", timeout=timeout, path=receipt.get("path")):
                yield
    finally:
        if acquired:
            entry["lock"].release()
        with _BORROW_LOCK:
            entry["users"] -= 1
            if not entry["users"]:
                _BORROWERS.pop(key, None)


class Lease:
    def __init__(self, owner="", work_class="background", capacity=None, path=None):
        self.id = uuid.uuid4().hex
        self.owner = str(owner)
        self.work_class = work_class if work_class in PRIORITY else "background"
        self.capacity = max(1, int(capacity or _capacity()))
        self.path = path or STORE
        self.registered = False
        self.active = False

    def poll(self):
        """Atomically register/check this ticket; return a public queue receipt."""
        now = time.time()
        with _connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            live = db.execute("SELECT id,pid FROM tickets WHERE status IN ('queued','running')").fetchall()
            for row in live:
                if not _alive(row["pid"]):
                    db.execute("UPDATE tickets SET status='orphaned',finished=?,reason=? WHERE id=?",
                               (now, "owning process ended", row["id"]))
            if not self.registered:
                queued = db.execute("SELECT COUNT(*) FROM tickets WHERE status='queued'").fetchone()[0]
                if queued >= MAX_QUEUED:
                    raise QueueTimeout("model queue is full; retry when queued work finishes")
                db.execute("INSERT INTO tickets VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL)",
                           (self.id, os.getpid(), self.owner, self.work_class,
                            PRIORITY[self.work_class], now))
                self.registered = True
            row = db.execute("SELECT * FROM tickets WHERE id=?", (self.id,)).fetchone()
            if row["status"] == "queued":
                waiting = db.execute("SELECT * FROM tickets WHERE status='queued' ORDER BY priority,created,id").fetchall()
                running = db.execute("SELECT work_class FROM tickets WHERE status='running'").fetchall()
                nonforeground = sum(r[0] != "foreground" for r in running)
                # One slot is reserved when capacity > 1; cap=1 still makes
                # forward progress for background work while no owner waits.
                room = len(running) < self.capacity
                permitted = self.work_class == "foreground" or self.capacity == 1 or nonforeground < self.capacity - 1
                if waiting and waiting[0]["id"] == self.id and room and permitted:
                    db.execute("UPDATE tickets SET status='running',started=? WHERE id=?", (now, self.id))
                    row = db.execute("SELECT * FROM tickets WHERE id=?", (self.id,)).fetchone()
                position = next((i + 1 for i, x in enumerate(waiting) if x["id"] == self.id), None)
            else:
                position = None
            self.active = row["status"] == "running"
            return {"id": self.id, "status": row["status"], "work_class": self.work_class,
                    "queue_position": position if not self.active else None,
                    "queued_at": row["created"], "started_at": row["started"]}

    def release(self, reason="completed"):
        if self.registered:
            with _connect(self.path) as db:
                db.execute("UPDATE tickets SET status='released',finished=?,reason=? "
                           "WHERE id=? AND status IN ('queued','running')",
                           (time.time(), reason, self.id))
        self.active = False

    def acquire(self, timeout=QUEUE_TIMEOUT, on_wait=None, cancel=None):
        end = time.monotonic() + max(0, float(timeout))
        try:
            while True:
                if cancel and cancel():
                    raise QueueTimeout("model queue cancelled")
                receipt = self.poll()
                if on_wait:
                    on_wait(receipt)
                if self.active:
                    return self
                if time.monotonic() >= end:
                    raise QueueTimeout("model queue wait expired; no model call was started")
                time.sleep(min(POLL_S, max(0, end - time.monotonic())))
        except BaseException:
            self.release("queue cancelled or expired")
            raise


@contextmanager
def execution(owner="", work_class="background", timeout=QUEUE_TIMEOUT, **kwargs):
    lease = Lease(owner, work_class, **kwargs)
    lease.acquire(timeout)
    try:
        yield lease
    finally:
        lease.release()

"""Runtime identity and ownership for parallel Vira processes.

Each checkout owns its jobs, flows, indexes and UI state. Accounts and their
automatic actions are shared resources: one process holds the automation
lease while every instance retains the normal interactive application.
"""
import errno
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
_guard = threading.RLock()
_lease = None
_legacy_until = 0.0
_legacy_present = False
_coordinating = False


def primary_root():
    value = os.environ.get("VIRA_PRIMARY_ROOT")
    return Path(value).expanduser().resolve() if value else ROOT


def primary_id():
    return "primary"


def id():
    return os.environ.get("VIRA_INSTANCE_ID") or primary_id()


def is_branch():
    return id() != primary_id()


def _url(value):
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("instance URL must be an http(s) origin")
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("instance URL must be an origin without credentials or a path")
    return value


def primary_url():
    return _url(os.environ.get("VIRA_PRIMARY_URL", "http://localhost:8377"))


def api_url():
    value = os.environ.get("VIRA_INSTANCE_URL")
    if is_branch() and not value:
        raise ValueError("a branch instance needs its own VIRA_INSTANCE_URL")
    return _url(value or primary_url())


url = api_url


def service_label():
    return os.environ.get("VIRA_SERVICE_LABEL", "")


def shared_root():
    """Private machine-local coordination, shared by related checkouts."""
    configured = os.environ.get("VIRA_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser()
    key = hashlib.sha256(str(primary_root()).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".vira" / "runtime" / key


def metadata():
    return {"id": id(), "kind": "branch" if is_branch() else "primary",
            "url": api_url(), "primary_url": primary_url(),
            "service_label": service_label(), "coordination_version": 1}


def owns(record):
    """Legacy records belong to primary; a clone never adopts their PIDs."""
    return (record.get("instance_id") or primary_id()) == id()


def record_view(record):
    """Keep copied history visible without presenting somebody else's job as live."""
    row = dict(record)
    row["instance_id"] = row.get("instance_id") or primary_id()
    row["imported"] = not owns(row)
    if row["imported"] and row.get("status") == "running":
        row["source_status"] = row["status"]
        row["status"] = "snapshot"
        row["instance_url"] = row.get("instance_url") or primary_url()
    return row


def child_env():
    env = dict(os.environ)
    env.update(VIRA_INSTANCE_ID=id(), VIRA_INSTANCE_URL=api_url(),
               VIRA_PRIMARY_ROOT=str(primary_root()),
               VIRA_PRIMARY_URL=primary_url(), VIRA_SERVICE_LABEL=service_label())
    return env


class Lease:
    """Nonblocking process lease; the OS releases it even after a crash."""
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def _legacy_primary_running():
    """Coexist with a primary that predates process leases, without restarting it."""
    global _legacy_until, _legacy_present
    if not is_branch():
        return False
    if time.monotonic() < _legacy_until:
        return _legacy_present
    try:
        with urlopen(primary_url() + "/api/instance", timeout=1) as response:
            payload = json.load(response)
        present = not bool(payload.get("coordination_version"))
    except HTTPError:
        present = True  # A running older server does not have this route.
    except (URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        # A slow/unreachable older server may still be running its workers.
        # Only a definite connection refusal permits taking over its work.
        present = getattr(reason, "errno", None) != errno.ECONNREFUSED
    except (ValueError, TypeError, AttributeError):
        present = True
    _legacy_present, _legacy_until = present, time.monotonic() + 10
    return present


def owns_automation():
    """Only the lease holder runs account-wide automatic actions.

    This does not authorize or disable any interactive operation. Reads,
    manual jobs and their completion notifications belong to each instance.
    """
    global _lease
    with _guard:
        # Library/CLI use has no server lifecycle. Avoid creating runtime
        # state merely to inspect a module or execute a unit test.
        if not _coordinating:
            return not is_branch()
        if _legacy_primary_running():
            if _lease is not None:
                _lease.close()
                _lease = None
            return False
        if _lease is not None and _lease.handle is not None:
            return True
        lease = Lease(shared_root() / "automation.lock")
        if not lease.acquire():
            return False
        _lease = lease
        return True


def start_automation():
    """Coordinate shared effects; every instance starts its own normal workers."""
    global _coordinating
    _coordinating = True
    def coordinate():
        while True:
            owns_automation()
            time.sleep(10)
    thread = threading.Thread(target=coordinate, daemon=True,
                              name="vira-account-automation")
    thread.start()
    return thread

"""On-disk protocol for detached jobs — the seam between the Vira server
and the runner processes that outlive it.

Every detached job owns a directory under data/jobs/<job-id>/ :

  job.json      — immutable launch spec, written once by the server:
                  { id, prompt, cwd, model (raw request), model_resolved,
                    permission_mode, publish_plan, idea_id, mode,
                    vault_destination (stable source id), vault_context,
                    auto_allow: [tool names], permission_timeout: float }
  state.json    — runner-owned, atomic tmp+rename on every change plus a
                  ~2s heartbeat: { id, status, started, finished,
                    session_id, awaiting, pending: [cards], result_text,
                    heartbeat: epoch, pid, mode, live, error }
  output.log    — runner-owned append-only transcript (the same rendered
                  lines the in-process path produced; the server tails it
                  into snapshots).
  control.jsonl — server-owned append-only command stream the runner
                  tails: {"op":"say","text":…} · {"op":"permission",
                  "req_id":…, "allow":bool, "scope":…, "reason":…} ·
                  {"op":"interrupt"} · {"op":"close"}
  runner.log    — the runner process's own stdout/stderr (spawn errors,
                  tracebacks), for debugging only.

Liveness = state.json heartbeat freshness, backstopped by pid aliveness.
The server supervisor re-attaches to running job dirs at boot; a dead
runner (stale heartbeat + dead pid) is finalized as "orphaned".
"""
import json
import os
import time
from pathlib import Path

from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "data" / "jobs"

# A runner heartbeats every ~2s; past this age with a dead pid it is gone.
STALE_AFTER = 20.0


def job_dir(jid):
    return JOBS_DIR / jid


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_json_atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str))
    tmp.replace(path)


def append_control(jdir, obj):
    """Append one command line for the runner. Serialized under the file
    lock so concurrent server threads never interleave partial lines."""
    ctl = Path(jdir) / "control.jsonl"
    with locked(ctl):
        with open(ctl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def read_control(jdir, consumed):
    """All complete command lines past index `consumed`; returns
    (new_consumed, [parsed objects]). A trailing partial line (mid-append)
    is left for the next poll."""
    ctl = Path(jdir) / "control.jsonl"
    try:
        text = ctl.read_text(encoding="utf-8")
    except OSError:
        return consumed, []
    end = text.rfind("\n")
    if end < 0:
        return consumed, []
    lines = text[:end].split("\n")
    out = []
    for line in lines[consumed:]:
        try:
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            pass  # never let one bad line wedge the stream
    return len(lines), out


def tail_output(jdir, cap):
    """The last `cap` bytes of the transcript, decoded leniently."""
    path = Path(jdir) / "output.log"
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > cap:
                fh.seek(size - cap)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _windows_pid_alive(pid):
    """Query a process handle without sending signals or requesting termination."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        # ERROR_INVALID_PARAMETER means the PID does not identify a process.
        # Access denied and other query failures do not prove a runner died.
        return ctypes.get_last_error() != 87
    try:
        # A process handle is signaled only after exit. Timeout or a failed
        # query conservatively keeps the runner alive for the next heartbeat.
        return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0
    finally:
        kernel.CloseHandle(handle)


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows os.kill(pid, 0) calls TerminateProcess; it is never a probe.
        return _windows_pid_alive(pid) if pid <= 0xFFFFFFFF else False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # The process exists but belongs to another user.
    except (OSError, OverflowError):
        return False


def runner_dead(state):
    """True when a status="running" state can no longer have a live runner
    behind it: heartbeat stale AND the recorded pid is gone."""
    hb = float(state.get("heartbeat") or 0)
    if time.time() - hb <= STALE_AFTER:
        return False
    return not pid_alive(state.get("pid"))

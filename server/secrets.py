"""One place Vira keeps its secrets, on any OS.

Until P0 every credential read shelled out to macOS `security`, which
made the Keychain a hard dependency of five modules (mercury, mail,
msgraph, models, onboard). This module is the portable seam: callers ask
for a secret by (service, account) and the ladder answers from the best
store this machine has —

  1. the OS store: macOS Keychain via `security`, or Windows Credential
     Manager via ctypes/advapi32 — nothing to install on either;
  2. a locked file, data/secrets.json — the fallback when there is no OS
     store (Linux) or the store call fails. Owner-only permissions where
     the filesystem supports them; writes are filelock-serialized and
     atomic.

Per-key env overrides (VIRA_ANTHROPIC_KEY and friends) stay at the
caller layer — they are documented interfaces of those modules, not a
generic layer here. Service names arrive ALREADY prefixed through
settings.keychain_service(), so sandbox namespacing keeps working
unchanged on every backend, including the file.

Reads never raise: a broken store answers "" like a missing secret.
Writes raise RuntimeError when every reachable store refused, because a
silently dropped credential is a debugging nightmare (the msgraph
refresh token rotates — losing one logs the account out).
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import settings
from .filelock import locked

# Module-level copies (not aliases into settings) so tests can patch
# secrets.IS_WIN without touching the rest of the app.
IS_MAC = settings.IS_MAC
IS_WIN = settings.IS_WIN


# ---------- macOS Keychain ----------

def _sec_quote(s):
    """Quoting per security(1): double quotes with backslash escapes."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mac_available():
    return IS_MAC and bool(shutil.which("security"))


# security(1) exits 44 when the item does not exist: a definite "no such
# secret", unlike a locked Keychain or a timeout, which say nothing.
_MAC_NOT_FOUND = 44


def _mac_lookup(service, account):
    """(value or None, definite). `definite` is False when the store could
    not answer, so a caller never remembers a failed read as a fact."""
    cmd = ["security", "find-generic-password"]
    if account:
        cmd += ["-a", account]
    cmd += ["-s", service, "-w"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — reads never raise
        return None, False
    if res.returncode == 0:
        return res.stdout.strip(), True
    return None, res.returncode == _MAC_NOT_FOUND


def _mac_get(service, account):
    return _mac_lookup(service, account)[0]


def _mac_set(service, account, value):
    # `security -i` reads the command from stdin, so the secret never
    # rides argv — argv is visible in ps for the subprocess duration
    # (audit P1-1). -U upserts in place.
    value = str(value).replace("\n", "").replace("\r", "")
    parts = ["add-generic-password -U"]
    if account:
        parts.append(f"-a {_sec_quote(account)}")
    parts.append(f"-s {_sec_quote(service)}")
    parts.append(f"-w {_sec_quote(value)}")
    cmd = " ".join(parts) + "\n"
    res = subprocess.run(["security", "-i"], input=cmd,
                         capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"keychain write failed: {res.stderr.strip()[:200]}")


def _mac_delete(service, account):
    cmd = ["security", "delete-generic-password", "-s", service]
    if account:
        cmd[2:2] = ["-a", account]
    subprocess.run(cmd, capture_output=True, text=True, timeout=10)


# ---------- Windows Credential Manager ----------
# ctypes over advapi32 — stdlib-only, same zero-install contract as
# shelling to `security` on a Mac. Target name folds service+account.

def _win_target(service, account):
    return f"{service}/{account}" if account else service


def _win_get(service, account):
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD),
                    ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                    ("LastWritten", wt.FILETIME),
                    ("CredentialBlobSize", wt.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                    ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]

    adv = ctypes.windll.advapi32
    pcred = ctypes.POINTER(CREDENTIAL)()
    ok = adv.CredReadW(_win_target(service, account), 1, 0,
                       ctypes.byref(pcred))  # 1 = CRED_TYPE_GENERIC
    if not ok:
        return None
    try:
        n = pcred.contents.CredentialBlobSize
        raw = ctypes.string_at(pcred.contents.CredentialBlob, n)
        return raw.decode("utf-8", "replace")
    finally:
        adv.CredFree(pcred)


def _win_set(service, account, value):
    import ctypes
    import ctypes.wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD),
                    ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                    ("LastWritten", wt.FILETIME),
                    ("CredentialBlobSize", wt.DWORD),
                    ("CredentialBlob", ctypes.c_void_p),
                    ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]

    blob = str(value).encode("utf-8")
    cred = CREDENTIAL()
    cred.Type = 1                       # CRED_TYPE_GENERIC
    cred.TargetName = _win_target(service, account)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(ctypes.create_string_buffer(blob, len(blob)),
                                      ctypes.c_void_p)
    cred.Persist = 2                    # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = account or "vira"
    if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise RuntimeError(
            f"credential manager write failed (WinError {ctypes.GetLastError()})")


def _win_delete(service, account):
    import ctypes
    ctypes.windll.advapi32.CredDeleteW(_win_target(service, account), 1, 0)


# ---------- locked-file fallback ----------

def _file_path():
    return settings.ROOT / "data" / "secrets.json"


def _file_key(service, account):
    return f"{service}/{account}" if account else service


def _file_load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _file_get(service, account):
    val = _file_load(_file_path()).get(_file_key(service, account))
    return val if isinstance(val, str) else None


def _file_mutate(fn):
    path = _file_path()
    with locked(path):
        data = _file_load(path)
        fn(data)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                   prefix=".secrets-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass  # Windows: NTFS ACLs already scope %USERPROFILE% to the owner
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _file_set(service, account, value):
    _file_mutate(lambda d: d.__setitem__(_file_key(service, account), str(value)))


def _file_delete(service, account):
    _file_mutate(lambda d: d.pop(_file_key(service, account), None))


# ---------- the ladder ----------

# Recent Keychain answers, for callers that pass max_age. Each Keychain
# read starts a `security` process; the desktop page's startup burst asked
# for the same model key 17 times in one load (measured 2026-11-06).
_recent = {}
_recent_lock = threading.Lock()
_reading = {}             # (service, account) -> lock held during a read


def _recent_hit(key, max_age):
    with _recent_lock:
        hit = _recent.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    return None


_forgotten = {}           # (service, account) -> times forgotten


def _forget(service, account):
    key = (service, account or None)
    with _recent_lock:
        _recent.pop(key, None)
        _forgotten[key] = _forgotten.get(key, 0) + 1


def get(service, account=None, max_age=0):
    """The secret, or "" when nowhere. Never raises. OS store first, then
    the locked file — so a secret written on a store-less platform is
    still found, and a healthy Keychain always wins.

    `max_age` (seconds) lets a caller reuse a Keychain answer that recent.
    Only for secrets that are not rotated behind this process's back: a
    value another process rewrote is seen up to max_age late. A write or
    delete through this module forgets its own entry at once, and a read
    the store could not answer is never remembered."""
    if _mac_available():
        if max_age <= 0:
            val = _mac_get(service, account)
            return val if val is not None else (_file_get(service, account) or "")
        key = (service, account or None)
        hit = _recent_hit(key, max_age)
        if hit is not None:
            return hit
        # Callers asking for the same secret at once wait for one read.
        with _recent_lock:
            gate = _reading.setdefault(key, threading.Lock())
        with gate:
            hit = _recent_hit(key, max_age)
            if hit is not None:
                return hit
            with _recent_lock:
                generation = _forgotten.get(key, 0)
            val, definite = _mac_lookup(service, account)
            if val is None:
                val = _file_get(service, account) or ""
            with _recent_lock:
                # A write that landed during this read makes it stale.
                if definite and _forgotten.get(key, 0) == generation:
                    _recent[key] = (time.monotonic(), val)
            return val
    elif IS_WIN:
        try:
            val = _win_get(service, account)
        except Exception:  # noqa: BLE001
            val = None
        if val is not None:
            return val
    return _file_get(service, account) or ""


def set(service, account, value):
    """Store the secret in the best reachable store. Raises RuntimeError
    only when every candidate refused."""
    # Forgotten before AND after: a read racing the write must not keep
    # the old value it saw mid-write.
    _forget(service, account)
    try:
        return _store(service, account, value)
    finally:
        _forget(service, account)


def _store(service, account, value):
    if _mac_available():
        try:
            _mac_set(service, account, value)
            return "keychain"
        except Exception:  # noqa: BLE001 — fall through to the file
            pass
    elif IS_WIN:
        try:
            _win_set(service, account, value)
            return "credential-manager"
        except Exception:  # noqa: BLE001
            pass
    _file_set(service, account, value)
    return "file"


def delete(service, account=None):
    """Best-effort removal from every store (a secret must not resurrect
    from the fallback after the primary copy is deleted)."""
    _forget(service, account)
    try:
        _remove(service, account)
    finally:
        _forget(service, account)


def _remove(service, account):
    if _mac_available():
        try:
            _mac_delete(service, account)
        except Exception:  # noqa: BLE001
            pass
    elif IS_WIN:
        try:
            _win_delete(service, account)
        except Exception:  # noqa: BLE001
            pass
    try:
        _file_delete(service, account)
    except Exception:  # noqa: BLE001
        pass

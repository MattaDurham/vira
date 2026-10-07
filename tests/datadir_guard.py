"""Refuse, and remember, every write a test makes into a checkout's data/.

data/ holds the stores a running Vira owns: config, ledgers, queues, the
fixture CRM copy. The suite runs in fresh worktrees and in the live
checkout alike, so a test that reaches a module-level store path it forgot
to pin either leaves junk in a fresh tree or rewrites one of the owner's
real stores. Pinning the path to a tmp dir is the fix; this is the backstop
that turns a forgotten pin into a failure instead of a write.

arm() installs a process-wide audit hook watching this checkout's data/
and, when VIRA_PRIMARY_ROOT names another checkout (a dispatched session or
a branch instance), that checkout's data/ as well - the root that
instance.primary_root() hands to whatsapp and vaultwrite. A write under a
watched root is refused with RealStoreWrite, a PermissionError, so code that
tolerates a read-only store degrades the way it would there. It is also
recorded in `violations`, because that same code usually swallows the
error and the case still passes; tests/test_zz_data_untouched.py fails on
the record.

In-process only: a child process the suite spawns is not hooked. In a fresh
tree that case still shows, as a data/ that did not exist at arm time.
"""
import errno
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RealStoreWrite(PermissionError):
    """A test reached a store under a checkout's data/."""


violations = []     # one line per refused write: what, where, which test
watched = ()        # normcased data/ roots, fixed when the guard arms
existed = {}        # root -> whether it existed when the guard armed

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
# audit event -> (position of a path it changes, position of the dir_fd that
# path may be relative to). os.replace raises os.rename; copytree, makedirs
# and rmtree also raise the per-file events underneath.
_PATH_ARGS = {
    "os.mkdir": ((0, 2),), "os.rmdir": ((0, 1),), "os.remove": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)), "os.truncate": ((0, None),),
    "os.utime": ((0, 3),), "os.chmod": ((0, 2),), "os.chown": ((0, 3),),
    "os.link": ((1, 3),), "os.symlink": ((1, 2),),
    "shutil.copyfile": ((1, None),), "shutil.copytree": ((1, None),),
    "shutil.move": ((0, None), (1, None)), "shutil.rmtree": ((0, 1),),
    "sqlite3.connect": ((0, None),),
}


def _inside(path, dir_fd=None):
    if isinstance(path, int):
        return None
    try:
        p = os.fsdecode(path)
    except (TypeError, ValueError):
        return None
    if not os.path.isabs(p) and isinstance(dir_fd, int) and dir_fd >= 0:
        # relative to an open directory, as rmtree walks a tree: resolving
        # it against the cwd would read a tmp tree's own data/ as ours
        return None
    p = os.path.normcase(os.path.abspath(p))
    for root in watched:
        if p == root or p.startswith(root + os.sep):
            return p
    return None


def _culprit():
    """The server calls that wrote, outermost first, and the test that led
    there - or, for a thread a test left running, the thread's name."""
    server = os.path.join(str(ROOT), "server") + os.sep
    calls, test = [], None
    frame = sys._getframe(2)
    while frame is not None and test is None:
        name = frame.f_code.co_filename
        here = f"{os.path.basename(name)}:{frame.f_lineno} {frame.f_code.co_name}"
        if name.startswith(server):
            calls.append(here)
        elif os.path.basename(name).startswith("test_"):
            test = here
        frame = frame.f_back
    origin = test or f"thread {threading.current_thread().name}"
    return f"{' > '.join(reversed(calls)) or '?'} <- {origin}"


def _hook(event, args):
    if event == "open":
        mode = args[1] if isinstance(args[1], str) else ""
        flags = args[2] if isinstance(args[2], int) else 0
        if not (flags & _WRITE_FLAGS or set(mode) & set("wax+")):
            return
        paths = ((args[0], None),)
    else:
        spec = _PATH_ARGS.get(event)
        if spec is None:
            return
        paths = [(args[i], args[fd] if fd is not None and fd < len(args) else None)
                 for i, fd in spec if i < len(args)]
    for path, dir_fd in paths:
        hit = _inside(path, dir_fd)
        if hit:
            violations.append(f"{event} {hit}  from {_culprit()}")
            raise RealStoreWrite(
                errno.EACCES, "a test may not write a checkout's data/ - "
                "pin this store to a tmp dir", hit)


def arm():
    """Install the hook, once per process: an audit hook cannot be removed."""
    global watched
    if watched:
        return
    roots = [ROOT / "data"]
    primary = os.environ.get("VIRA_PRIMARY_ROOT")
    if primary:
        roots.append(Path(primary).expanduser().resolve() / "data")
    watched = tuple(dict.fromkeys(os.path.normcase(str(r)) for r in roots))
    existed.update((r, os.path.isdir(r)) for r in watched)
    sys.addaudithook(_hook)

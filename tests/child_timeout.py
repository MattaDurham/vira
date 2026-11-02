"""Give a test's child process (node, git, python) more time on Windows.

The Windows CI runner starts children far slower than a Mac and stalls now
and then: on main on 2026-11-02 a node harness that finishes in 0.2 s on a
Mac ran past its 20 s limit there, and a `git add` of a few files ran past
15 s. Each passed on rerun. A test writes the limit it needs on a Mac and
passes it through child_timeout(), which keeps it as is elsewhere and
multiplies it on Windows: a slow runner gets the headroom, and a real hang
still fails within minutes, far under the job's own limit.
"""
import os

WINDOWS_FACTOR = 4


def child_timeout(seconds):
    return seconds * WINDOWS_FACTOR if os.name == "nt" else seconds

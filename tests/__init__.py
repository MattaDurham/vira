"""Armed on import of the package, before any case runs: a test that writes
into a checkout's data/ is refused and recorded (see datadir_guard)."""
from . import datadir_guard

datadir_guard.arm()

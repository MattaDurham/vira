"""One SQLite build per FTS5 index: the self-heal every index runs on open.

An FTS5 table that stores its own content (every `fts` table here) deletes
a row by re-tokenizing the stored text with the SQLite build doing the
delete, and its integrity check re-tokenizes every row with the build
doing the check. unicode61 is not identical across builds: SQLite 3.54.0
tokenizes U+FFFE and U+FFFF, 3.53.4 and macOS's /usr/bin/sqlite3 3.51.0
treat them as separators. So a second build's delete leaves the first
build's tokens behind, and a second build's check calls a healthy index
malformed even when nothing is wrong. Both "corruptions" of the owner's
media index (2026-10-16, 2026-10-23) were Apple's shell reading an index
Homebrew's SQLite wrote; the text and CRM indexes have the same table
shape and the same exposure.

Hence, for every index: a `state` row records which build last verified
it; the first open per process runs FTS5's own integrity check under the
current build with a short busy timeout and rebuilds a malformed index
under that build, logging one line that names both builds; a locked
database skips the check, and the next open retries it. The modules keep
their own `state` accessors and log destinations and hand them in here.

    ftsguard.verify_once(con, str(DB), log=..., get_state=..., set_state=...)

`report` and `repair` back each module's `fts-check` / `fts-rebuild`
command: the repair path that uses the venv's SQLite, never a shell.

The same venv is why a CLI needs a gate. It links Homebrew's libsqlite3,
so a `brew upgrade sqlite` moves every NEW process to the new build while
the running server keeps the old one mapped until its restart; a CLI
backfill started in between is the second build writing the index. So
each in-server Indexer records its build and the time of every tick in
its index's `state` table (`record_indexer_heartbeat`), and a CLI
refuses to write while that heartbeat is fresh on another build
(`cli_may_write`, which logs `mixed_build_refusal`'s reason).
"""
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

STATE_KEY = "fts_sqlite"    # state row: the build that last verified or rebuilt the table
BUSY_MS = 2000              # the open-time check waits this long for a lock

INDEXER_SQLITE_KEY = "indexer_sqlite"   # state row: the build the in-server Indexer runs on
INDEXER_SEEN_KEY = "indexer_seen"       # state row: UTC time of its last tick
INDEXER_FRESH_S = 300                   # a tick this recent means the server writes

_verified = set()           # index paths this process has checked
_lock = threading.Lock()


def log_line(path, msg, echo=True):
    """Append one timestamped line to `path`; `echo` repeats it on stderr,
    the server log, for the events the owner must see."""
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if echo:
        print(line, file=sys.stderr)


def check(con, table="fts"):
    """FTS5's own integrity check under this process's SQLite build: None
    when the index agrees with its stored content, else the error text.
    A busy database raises sqlite3.OperationalError, as any write does."""
    try:
        con.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
        con.commit()
        return None
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as e:
        con.rollback()
        return str(e)


def rebuild(con, table="fts"):
    """Re-derive the whole inverted index from the stored content under this
    build. Returns (rows, seconds). About a second per 20,000 rows."""
    t0 = time.time()
    con.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
    con.commit()
    rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return rows, time.time() - t0


def verify(con, *, log, get_state, set_state, table="fts", busy_ms=None):
    """Check the index under this build and rebuild it when the check
    fails; record the build that verified it. Returns 'ok', 'rebuilt',
    'skipped' (locked: try again at the next open) or 'failed'.

    `get_state(con, key)` / `set_state(con, key, val)` are the module's
    accessors for its `state` table; `log(msg)` takes one line."""
    current = sqlite3.sqlite_version
    previous = get_state(con, STATE_KEY)
    # the connection's own busy timeout comes back after the check: a
    # writer waits a minute for the index, but a check only waits BUSY_MS
    # before leaving the index to the next open
    was = con.execute("PRAGMA busy_timeout").fetchone()[0]
    con.execute(f"PRAGMA busy_timeout={int(busy_ms if busy_ms is not None else BUSY_MS)}")
    try:
        err = check(con, table)
    except sqlite3.OperationalError as e:
        log(f"fts: check skipped, {e}; next open retries")
        return "skipped"
    finally:
        con.execute(f"PRAGMA busy_timeout={int(was)}")
    result = "ok"
    if err:
        log(f"fts: malformed under SQLite {current} (last verified by "
            f"SQLite {previous or 'an unrecorded build'}): {err}; rebuilding")
        try:
            rows, secs = rebuild(con, table)
        except sqlite3.DatabaseError as e:
            log(f"fts: rebuild failed under SQLite {current}: {e}; "
                f"searches may miss rows until the index is rebuilt")
            return "failed"
        log(f"fts: rebuilt {rows} rows in {secs:.1f}s under SQLite {current}")
        result = "rebuilt"
    elif previous and previous != current:
        log(f"fts: SQLite build changed, {previous} -> {current}; index "
            f"passes under {current}")
    set_state(con, STATE_KEY, current)
    con.commit()
    return result


def verify_once(con, key, **kw):
    """`verify`, once per process per index (`key` is the index's path). A
    skipped check is forgotten, so the next open runs it again."""
    with _lock:
        if key in _verified:
            return None
        _verified.add(key)
    result = verify(con, **kw)
    if result == "skipped":
        with _lock:
            _verified.discard(key)
    return result


def report(con, *, get_state, table="fts"):
    """`fts-check`: what this build finds, without fixing it."""
    t0 = time.time()
    err = check(con, table)
    return {"sqlite": sqlite3.sqlite_version, "ok": err is None,
            "error": err,
            "rows": con.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
            "seconds": round(time.time() - t0, 2),
            "last_verified_by": get_state(con, STATE_KEY)}


def repair(con, *, log, set_state, table="fts"):
    """`fts-rebuild`: rebuild under this build and record it, whatever the
    check says."""
    rows, secs = rebuild(con, table)
    err = check(con, table)
    set_state(con, STATE_KEY, sqlite3.sqlite_version)
    con.commit()
    log(f"fts: rebuilt {rows} rows in {secs:.1f}s under SQLite "
        f"{sqlite3.sqlite_version} (fts-rebuild)")
    return {"sqlite": sqlite3.sqlite_version, "ok": err is None,
            "error": err, "rows": rows, "seconds": round(secs, 2)}


# ---------- the CLI gate: no second build while the server writes ----------

def _utcnow():
    return datetime.now(timezone.utc)


def record_indexer_heartbeat(con, *, set_state):
    """The in-server Indexer's tick: which build it writes with, and when.
    Recorded every tick, so a heartbeat older than INDEXER_FRESH_S means
    the server is not writing (stopped, or still in its boot wait)."""
    set_state(con, INDEXER_SQLITE_KEY, sqlite3.sqlite_version)
    set_state(con, INDEXER_SEEN_KEY, _utcnow().isoformat(timespec="seconds"))
    con.commit()


def mixed_build_refusal(con, *, get_state, now=None):
    """Why a CLI must not write the index now, or None: the server's Indexer
    ticked within INDEXER_FRESH_S on another SQLite build."""
    build = get_state(con, INDEXER_SQLITE_KEY)
    seen = get_state(con, INDEXER_SEEN_KEY)
    if not build or not seen or build == sqlite3.sqlite_version:
        return None
    try:
        seen_at = datetime.fromisoformat(seen)
    except ValueError:
        return None
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=timezone.utc)
    if ((now or _utcnow()) - seen_at).total_seconds() > INDEXER_FRESH_S:
        return None
    return (f"the Vira server is indexing with SQLite {build} while this "
            f"process has SQLite {sqlite3.sqlite_version}; two builds "
            f"writing one FTS5 index leave it malformed, because unicode61 "
            f"tokenizes differently across builds. Restart the server so "
            f"both run the current build, then run this again.")


def cli_may_write(con, *, get_state, log=print, index="the index"):
    """The gate every CLI backfill passes before touching an index: False,
    with the reason logged once, while the server indexes it on another
    build. `index` names it in that line ("the media index")."""
    why = mixed_build_refusal(con, get_state=get_state)
    if why:
        log(f"refusing to write {index}: {why}")
        return False
    return True

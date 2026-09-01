"""Vira's own copy of every iMessage attachment it can still reach.

macOS treats ~/Library/Messages/Attachments as an EVICTABLE CACHE, not a
store: Messages in iCloud offloads the bytes under storage pressure and
chat.db keeps the row, so a conversation's media history quietly becomes a
list of filenames. Measured on the owner's machine 2026-09-01: 15,679 of
17,104 attachments (92%, 32.9 GB) had no file on disk, spread evenly across
every year rather than oldest-first — the eviction is driven by free space
(the volume was 89.6% full), not by age.

Nothing is lost. The originals are in iCloud (16,570 synced CloudKit
attachment records) and macOS re-downloads them when a conversation is
opened — proven on that machine: 235 files on disk had arrived more than 60
days after their message, three of them photos sent in July 2016 that landed
on 2026-08-29. But Vira cannot SERVE what Apple has evicted, and there is no
supported way to force a re-download (brctl covers only CloudDocs). So the
durable answer is to stop reading through to Apple's cache: copy the bytes
into Vira's own store while they are still there, and serve from that.

Content-addressed, so one photo sent to three people is stored once.

THE INDEX LIVES INSIDE THE ARCHIVE ROOT, not as a column on
media-index.sqlite. That sidecar is regenerable by design and is rebuilt
whenever the media pipeline changes; a rebuild would orphan every blob here,
leaving a directory of unreachable hex-named files. Keeping the map beside
the bytes also means the whole archive can be moved to an external drive as
one directory — which matters, since the disk pressure that causes the
eviction is the same disk this would otherwise fill.

Reads always work. WRITES REFUSE UNDER VIRA_PASSIVE: with the default root
a test clone would grow a duplicate archive, and with the root pointed at an
external drive it would write into the owner's real one — the plans.py
boundary. A passive instance still serves whatever the archive already holds,
which is what makes the surface testable on a branch.
"""
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import settings
from .imessage import _connect

_DATA = Path(__file__).resolve().parent.parent / "data"

# Kept in step with media.SKIP_NAMES by a parity test rather than imported:
# media.py imports this module for the eviction fallback, so a module-level
# import back would be circular.
SKIP_NAMES = (".pluginpayloadattachment",)

CHUNK = 1 << 20          # 1 MiB — hashlib releases the GIL on buffers this size
COMMIT_EVERY = 25        # rows per transaction; file copies happen OUTSIDE it

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs(
  att_id  INTEGER PRIMARY KEY,   -- chat.db attachment ROWID
  sha     TEXT NOT NULL,
  size    INTEGER,
  mime    TEXT,
  name    TEXT,
  stored  TEXT
);
CREATE INDEX IF NOT EXISTS idx_blobs_sha ON blobs(sha);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT);
"""


class ArchiveFull(RuntimeError):
    """The configured size cap is reached. Raised rather than returning None
    so the caller reports it — a cap that silently stops archiving reads as
    'everything is archived'."""


def enabled():
    return bool(settings.get("media_archive_enabled"))


def root():
    """Archive directory. Configurable so it can live on an external volume
    instead of the boot disk whose fullness caused the eviction."""
    raw = (settings.get("media_archive_root") or "").strip()
    return Path(raw).expanduser() if raw else _DATA / "media-archive"


def max_bytes():
    gb = settings.get("media_archive_max_gb") or 0
    try:
        return int(float(gb) * 1e9)
    except (TypeError, ValueError):
        return 0


def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


def _db(create=True):
    """Connection to the archive's own index, or None when the root is
    unreachable (an external drive that is not mounted). Never raises —
    an absent archive degrades to 'no archived copy', which is honest."""
    r = root()
    path = r / "index.sqlite"
    try:
        if not path.exists():
            if not create:
                return None
            r.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(SCHEMA)
        return con
    except (OSError, sqlite3.Error):
        return None


def _is_sha(s):
    return isinstance(s, str) and len(s) == 64 and all(
        c in "0123456789abcdef" for c in s)


def blob_path(sha):
    """Path for a content hash, or None if it is not a hash. Validated
    rather than trusted: the index is a file on disk, and a poisoned row
    must not be able to address anything outside the archive."""
    if not _is_sha(sha):
        return None
    return root() / "blobs" / sha[:2] / sha


# ---------- reads (always available, passive included) ----------

def lookup(att_id):
    """(path, mime, name) for an archived attachment, or (None, None, None).
    Only files under the archive root are ever served — the containment
    check media.attachment_path applies to ~/Library/Messages/Attachments."""
    con = _db(create=False)
    if con is None:
        return None, None, None
    try:
        row = con.execute(
            "SELECT sha, mime, name FROM blobs WHERE att_id=?",
            (int(att_id),)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return None, None, None
    finally:
        con.close()
    if not row:
        return None, None, None
    path = blob_path(row[0])
    if path is None:
        return None, None, None
    # blob_path's hex check is what actually makes escape impossible, so this
    # is unreachable today and no test pins it — kept as a backstop in case
    # blob_path ever learns to accept something other than a bare hash. Do
    # not read it as the load-bearing check and relax _is_sha.
    try:
        path = path.resolve()
        path.relative_to(root().resolve())
    except (OSError, ValueError):
        return None, None, None
    if not path.exists():
        return None, None, None
    return path, row[1] or "application/octet-stream", row[2] or path.name


def file_for(att_id):
    """Just the path — the byte source when chat.db's original is evicted."""
    return lookup(att_id)[0]


def have_many(att_ids):
    """The subset of att_ids this archive holds. One query per chunk, so a
    4,500-attachment conversation costs a handful of statements rather than
    a lookup per row."""
    ids = [int(i) for i in att_ids if i is not None]
    if not ids:
        return set()
    con = _db(create=False)
    if con is None:
        return set()
    out = set()
    try:
        for i in range(0, len(ids), 900):
            part = ids[i:i + 900]
            q = ",".join("?" * len(part))
            out.update(r[0] for r in con.execute(
                f"SELECT att_id FROM blobs WHERE att_id IN ({q})", part))
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def stats():
    """Counts and on-disk size. `missing_blob` is a row whose bytes are gone
    (an unmounted or pruned archive) — reported, never quietly treated as
    absent."""
    out = {"enabled": enabled(), "root": str(root()), "files": 0,
           "blobs": 0, "bytes": 0, "cap_gb": (max_bytes() / 1e9) or None}
    con = _db(create=False)
    if con is None:
        out["available"] = False
        return out
    out["available"] = True
    try:
        out["files"] = con.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
        out["blobs"] = con.execute(
            "SELECT COUNT(DISTINCT sha) FROM blobs").fetchone()[0]
        out["bytes"] = _used_bytes(con)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


# ---------- writes ----------

def store(att_id, path, mime=None, name=None):
    """Copy one attachment into the archive; returns its sha, or the existing
    sha when already held. Single pass — hashed while copied, then renamed
    into its content address, so identical bytes from a second conversation
    cost no extra space."""
    if _passive():
        raise PermissionError(
            "passive instance — the archive is the owner's real store")
    src = Path(path)
    con = _db()
    if con is None:
        raise OSError(f"archive root unreachable: {root()}")
    try:
        row = con.execute("SELECT sha FROM blobs WHERE att_id=?",
                          (int(att_id),)).fetchone()
        if row:
            return row[0]
        cap = max_bytes()
        if cap:
            used = _used_bytes(con)
            if used >= cap:
                raise ArchiveFull(
                    f"archive at cap ({used/1e9:.1f} of {cap/1e9:.1f} GB)")
        sha, size, _new = _copy_in(src)
        _record(con, att_id, sha, size, mime, name or src.name)
        con.commit()
        return sha
    finally:
        con.close()


def _copy_in(src):
    """Hash-and-copy in one read. Returns (sha, size, new) — `new` is False
    when these exact bytes were already held, so a deduped copy costs no
    space and must not count against the cap. The temp file carries the pid
    so a CLI sweep and the background thread cannot collide."""
    r = root()
    tmpdir = r / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f"{os.getpid()}-{threading.get_ident()}.part"
    h = hashlib.sha256()
    size = 0
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                buf = fin.read(CHUNK)
                if not buf:
                    break
                h.update(buf)
                fout.write(buf)
                size += len(buf)
        sha = h.hexdigest()
        dest = blob_path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        new = not dest.exists()
        if new:
            tmp.replace(dest)
        else:
            tmp.unlink(missing_ok=True)      # already hold these bytes
        return sha, size, new
    finally:
        tmp.unlink(missing_ok=True)


def _used_bytes(con):
    """Space the blobs occupy — distinct sha only, since several attachments
    share one blob."""
    try:
        return con.execute(
            "SELECT COALESCE(SUM(size),0) FROM "
            "(SELECT sha, MAX(size) AS size FROM blobs GROUP BY sha)"
        ).fetchone()[0]
    except sqlite3.Error:
        return 0


def _record(con, att_id, sha, size, mime, name):
    con.execute(
        "INSERT OR REPLACE INTO blobs(att_id, sha, size, mime, name, stored) "
        "VALUES (?,?,?,?,?,?)",
        (int(att_id), sha, size, mime or "", name or "",
         datetime.now().isoformat(timespec="seconds")))


def _candidates(con_chat, known):
    """Attachments still on disk that the archive does not hold yet."""
    rows = con_chat.execute(
        "SELECT ROWID, filename, mime_type, transfer_name FROM attachment "
        "WHERE filename IS NOT NULL").fetchall()
    out = []
    for att_id, fname, mime, tname in rows:
        if att_id in known:
            continue
        name = (tname or Path(fname).name or "")
        if name.lower().endswith(SKIP_NAMES):
            continue          # sticker / plugin payloads: 0.66 GB of noise
        p = Path(fname).expanduser()
        try:
            if not p.is_file() or p.stat().st_size == 0:
                continue      # evicted, or a placeholder — nothing to copy
        except OSError:
            continue
        out.append((att_id, p, mime, name))
    return out


def sweep(log=print, limit=None):
    """Archive every attachment still on disk that is not held yet.

    Cheap to repeat: the work is bounded by what macOS has re-downloaded
    since the last pass, which on a settled machine is nothing."""
    if not enabled():
        log("archive: disabled")
        return 0
    if _passive():
        log("archive: passive instance — not writing")
        return 0
    con = _db()
    if con is None:
        log(f"archive: root unreachable ({root()}) — nothing archived")
        return 0
    try:
        known = {r[0] for r in con.execute("SELECT att_id FROM blobs")}
        chat = _connect()
        try:
            todo = _candidates(chat, known)
        finally:
            chat.close()
        if limit:
            capped = len(todo) - limit
            todo = todo[:limit]
        else:
            capped = 0
        cap = max_bytes()
        used = _used_bytes(con) if cap else 0
        n = failed = 0
        pending = 0
        for i, (att_id, path, mime, name) in enumerate(todo):
            if cap and used >= cap:
                con.commit()
                log(f"archive: cap reached at {used/1e9:.2f} of "
                    f"{cap/1e9:.2f} GB; {len(todo) - i} files not archived")
                return n
            try:
                sha, size, new_bytes = _copy_in(path)
            except OSError:
                failed += 1
                continue
            _record(con, att_id, sha, size, mime, name)
            if new_bytes:
                used += size
            n += 1
            pending += 1
            if pending >= COMMIT_EVERY:
                con.commit()
                pending = 0
        con.commit()
        msg = f"archive: {n} newly archived"
        if failed:
            msg += f", {failed} unreadable"
        if capped > 0:
            msg += f", {capped} left for the next pass (limit {limit})"
        log(msg)
        return n
    finally:
        con.close()


class Archiver(threading.Thread):
    """Keeps Vira's copy current. Its own thread rather than a stage of
    mediaindex.Indexer on purpose: archiving is not indexing, it needs none
    of the model stack, and it must keep running while a CLI backfill owns
    the index stage scans. Slow cadence — the only new work between ticks is
    whatever macOS has downloaded."""

    def __init__(self, interval_min=None):
        super().__init__(daemon=True)
        self.interval = max(1, int(
            interval_min or settings.get("media_archive_interval_min") or 30
        )) * 60

    def _log(self, msg):
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}\n"
        try:
            with open(_DATA / "media-archive.log", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def run(self):
        time.sleep(90)          # let the server settle; this is never urgent
        while True:
            try:
                if enabled():
                    sweep(log=self._log)
            except Exception as e:      # noqa: BLE001 — never kill the thread
                self._log(f"archiver error: {e}")
            time.sleep(self.interval)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "status":
        print(json.dumps(stats(), indent=2))
    elif args and args[0] == "sweep":
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else None
        sweep(limit=lim)
    else:
        print("usage: python -m server.mediaarchive status | sweep [--limit N]")

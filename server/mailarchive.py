"""Vira's own copy of every email: the raw message, attachments inside.

Texts already have a durable local story. chat.db is Apple's store on
this Mac, the text index holds every body, and server/mediaarchive.py
keeps the attachments macOS evicts. Email had none: a mailbox lives on
the provider, the text index keeps a stripped body, the attachment index
(server/mailindex.py) keeps the files it chose, and neither can give a
message back. The owner asked on 2026-10-13 for email to be backed up
locally the way texts are. This is that: one RFC 822 file per message,
exactly as the server holds it (headers, every body form, the
attachments), in a content-addressed store with its index beside it.

The shape is mediaarchive's on purpose:

  - Content-addressed blobs (sha256 of the raw bytes), so a message that
    turns up twice, the same Message-ID in two folders or a re-walk after
    a UIDVALIDITY reset, is stored once.
  - THE INDEX LIVES INSIDE THE ARCHIVE ROOT. The store moves to an
    external drive as one directory, and no regenerable sidecar can
    orphan the blobs.
  - A configurable root, a size cap that is REPORTED rather than silent,
    a slow background sweep, and a CLI for the backlog.

Each blob carries the .eml suffix, so a message opens in Mail or drags
into any client as it is: the archive is a restore, not a rendering.

Sources: IMAP walks the All Mail folder (or INBOX) newest first: new mail
above a high mark, then the history downward from a low mark, so the mail
the owner needs first is held within hours and a 300,000-message Gmail
history over the days after (the owner's call, 2026-10-13). Graph
(M365/Outlook) pages the mailbox newest first and fetches each message's
MIME through /$value. A message that fails to
fetch is recorded and retried on the next sweep, never skipped quietly;
more than three consecutive failures read as the connection dying and
end the pass with the watermark held.

CLI:  .venv/bin/python -m server.mailarchive status
      .venv/bin/python -m server.mailarchive sweep [--limit N]
      .venv/bin/python -m server.mailarchive backfill [--account ADDR] [--workers N]
"""
import email.parser
import email.utils
import hashlib
import imaplib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import channels, runtimework, settings
from . import mail as mailmod

# A full-history mailbox walk starts at UID watermark 0, and the UID SEARCH
# reply listing a 20-year mailbox overflows imaplib's default 1MB line cap
# ("got more than 1000000 bytes"). textindex hit it on its first backlog
# run, 2026-07-28; this is the same walk.
imaplib._MAXLINE = max(imaplib._MAXLINE, 50_000_000)

_DATA = Path(__file__).resolve().parent.parent / "data"

GRAPH_PAGE = 50
SWEEP_LIMIT = 200            # messages per account per background tick;
                             # the backlog is the CLI's job
MAX_CONSECUTIVE_FAILURES = 3
WORKER_RECONNECTS = 5        # stalled connections a history walker replaces per pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- arrival order: the cursor the
                                                 -- text and attachment indexes follow
  key        TEXT UNIQUE NOT NULL,   -- account|<Message-ID>; without one,
                                     -- account|uid:<validity>:<uid> or account|graph:<id>
  account    TEXT NOT NULL,
  message_id TEXT,
  sha        TEXT NOT NULL,
  size       INTEGER,
  date       TEXT,               -- ISO 8601 UTC from the Date header
  stored     TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_sha ON messages(sha);
CREATE INDEX IF NOT EXISTS idx_messages_account_date ON messages(account, date);
CREATE TABLE IF NOT EXISTS seen_uids(
  account  TEXT NOT NULL,        -- every IMAP uid fetched (held or gone), per
  validity TEXT NOT NULL,        -- UIDVALIDITY, so a span met again costs a
  uid      INTEGER NOT NULL,     -- SEARCH and not a fetch
  PRIMARY KEY(account, validity, uid)
);
CREATE TABLE IF NOT EXISTS failures(
  account  TEXT NOT NULL,
  ref      TEXT NOT NULL,        -- IMAP uid or Graph message id
  attempts INTEGER,
  error    TEXT,
  last     TEXT,
  PRIMARY KEY(account, ref)
);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT);
"""


class ArchiveFull(RuntimeError):
    """The configured size cap is reached. Raised rather than returning
    quietly so the caller reports it: a cap that silently stops archiving
    reads as 'everything is archived'."""


def enabled():
    return bool(settings.get("mail_archive_enabled"))


def root():
    """Archive directory. Configurable so it can live on an external
    volume, the same choice the media archive offers."""
    raw = (settings.get("mail_archive_root") or "").strip()
    return Path(raw).expanduser() if raw else _DATA / "mail-archive"


def max_bytes():
    gb = settings.get("mail_archive_max_gb") or 0
    try:
        return int(float(gb) * 1e9)
    except (TypeError, ValueError):
        return 0


def _accounts():
    return channels.mail_accounts(mailmod.ACCOUNTS)


def _db(create=True):
    """Connection to the archive's own index, or None when the root is
    unreachable (an external drive that is not mounted). Never raises:
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
        # Write-ahead mode, as the other indexes: a status read never
        # waits on a sweep. Skipped while another connection holds a
        # transaction; taken on the next open.
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        con.executescript(SCHEMA)
        _migrate(con)
        return con
    except (OSError, sqlite3.Error):
        return None


def _migrate(con):
    """The first shape of `messages` (2026-10-13, morning) had no `seq`:
    the row's arrival order is what lets the text index and the attachment
    index read the archive instead of downloading a message a second and
    third time. Rebuilt in place, rows in the order they arrived. Two
    connections may race here; the loser re-checks and is content."""
    if "seq" in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
        return
    try:
        con.executescript("""
            CREATE TABLE messages_v2(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
              account TEXT NOT NULL, message_id TEXT, sha TEXT NOT NULL,
              size INTEGER, date TEXT, stored TEXT);
            INSERT INTO messages_v2(key, account, message_id, sha, size, date, stored)
              SELECT key, account, message_id, sha, size, date, stored
              FROM messages ORDER BY rowid;
            DROP TABLE messages;
            ALTER TABLE messages_v2 RENAME TO messages;
            CREATE INDEX IF NOT EXISTS idx_messages_sha ON messages(sha);
            CREATE INDEX IF NOT EXISTS idx_messages_account_date ON messages(account, date);
        """)
    except sqlite3.OperationalError:
        if "seq" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            raise


def get_state(con, key, default=None):
    row = con.execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, val):
    con.execute("INSERT OR REPLACE INTO state(key, val) VALUES (?, ?)",
                (key, str(val)))


def _is_sha(s):
    return isinstance(s, str) and len(s) == 64 and all(
        c in "0123456789abcdef" for c in s)


def blob_path(sha):
    """Path for a content hash, or None if it is not a hash. Validated
    rather than trusted: the index is a file on disk, and a poisoned row
    must not be able to address anything outside the archive."""
    if not _is_sha(sha):
        return None
    return root() / "blobs" / sha[:2] / (sha + ".eml")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _iso(value):
    """A date as ISO 8601 UTC, so rows sort by string. None stays None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _describe(raw, fallback_date=None):
    """(message_id, iso_date) from the headers alone: a 30 MB message is
    not parsed in full for two header fields."""
    try:
        hdr = email.parser.BytesHeaderParser().parsebytes(raw)
    except Exception:  # noqa: BLE001 - unparseable headers still archive
        return "", _iso(fallback_date)
    mid = (hdr.get("Message-ID") or "").strip()
    date = None
    try:
        if hdr.get("Date"):
            date = email.utils.parsedate_to_datetime(hdr.get("Date"))
    except (TypeError, ValueError):
        date = None
    return mid, _iso(date) or _iso(fallback_date)


# ---------- reads ----------

def _contained(path):
    """Only files under the archive root are ever handed out. blob_path's
    hex check already makes escape impossible; this is the backstop."""
    try:
        path.resolve().relative_to(root().resolve())
    except (OSError, ValueError):
        return False
    return path.exists()


def lookup(account, message_id):
    """Path of the archived .eml for a message, or None."""
    con = _db(create=False)
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT sha FROM messages WHERE account=? AND message_id=?",
            (account, message_id)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    path = blob_path(row[0])
    if path is None or not _contained(path):
        return None
    return path


def have_many(account, message_ids):
    """The subset of message_ids this archive holds for an account."""
    ids = [m for m in message_ids if m]
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
                f"SELECT message_id FROM messages WHERE account=? "
                f"AND message_id IN ({q})", [account, *part]))
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def covers(account):
    """True when the archive is where this account's mail is fetched: it
    is enabled and has begun on the account (a mark or a row). The text
    index and the attachment index then read the archive's files instead
    of downloading each message again (the owner's call, 2026-10-13:
    download once)."""
    if not enabled():
        return False
    con = _db(create=False)
    if con is None:
        return False
    try:
        if con.execute("SELECT 1 FROM state WHERE key=? OR key=?",
                       (f"wm:{account}", f"low:{account}")).fetchone():
            return True
        return con.execute("SELECT 1 FROM messages WHERE account=? LIMIT 1",
                           (account,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        con.close()


def iter_archived(account=None, after_seq=0, limit=None):
    """Archived messages in arrival order, after a cursor: the feed the
    indexes consume. Each row carries its `path`, or None when the blob
    is not there (an unmounted drive), which a consumer reports rather
    than treats as absent mail."""
    con = _db(create=False)
    if con is None:
        return
    try:
        sql = "SELECT seq, account, key, message_id, sha, date FROM messages WHERE seq>?"
        args = [int(after_seq or 0)]
        if account:
            sql += " AND account=?"
            args.append(account)
        sql += " ORDER BY seq"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        rows = con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return
    finally:
        con.close()
    for seq, acct, key, mid, sha, date in rows:
        path = blob_path(sha)
        yield {"seq": seq, "account": acct, "key": key, "message_id": mid,
               "sha": sha, "date": date,
               "path": path if path is not None and _contained(path) else None}


def message_bytes(row):
    """The raw message for an iter_archived row, or None."""
    path = row.get("path")
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _used_bytes(con):
    """Space the blobs occupy: distinct sha only, since one message can
    be indexed under two keys."""
    try:
        return con.execute(
            "SELECT COALESCE(SUM(size),0) FROM "
            "(SELECT sha, MAX(size) AS size FROM messages GROUP BY sha)"
        ).fetchone()[0]
    except sqlite3.Error:
        return 0


def _watermarks(con):
    """Every mark a walk moves: the new-mail marks and the IMAP history
    marks. The backlog loop's done signal."""
    return dict(con.execute(
        "SELECT key, val FROM state WHERE key LIKE 'wm:%' OR key LIKE 'low:%'"))


def stats():
    """Counts, on-disk size, and per account the span of dates held, so
    the status answers 'is everything backed up' without opening a blob.
    `failed` is messages the archive could not fetch and will retry."""
    out = {"enabled": enabled(), "root": str(root()), "messages": 0,
           "blobs": 0, "bytes": 0, "failed": 0,
           "cap_gb": (max_bytes() / 1e9) or None, "accounts": {}}
    con = _db(create=False)
    if con is None:
        out["available"] = False
        return out
    out["available"] = True
    try:
        out["messages"] = con.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]
        out["blobs"] = con.execute(
            "SELECT COUNT(DISTINCT sha) FROM messages").fetchone()[0]
        out["bytes"] = _used_bytes(con)
        out["failed"] = con.execute(
            "SELECT COUNT(*) FROM failures").fetchone()[0]
        for acct, n, oldest, newest in con.execute(
                "SELECT account, COUNT(*), MIN(date), MAX(date) "
                "FROM messages GROUP BY account"):
            low = get_state(con, f"low:{acct}")
            wm = get_state(con, f"wm:{acct}")
            out["accounts"][acct] = {
                "messages": n, "oldest": oldest, "newest": newest,
                "watermark": wm,
                # IMAP: the history mark reads 1 once the whole mailbox is
                # held. Graph: a stamped mark means a walk completed.
                "history_low": int(low) if low else None,
                "history_complete": (int(low) <= 1) if low else bool(wm)}
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


# ---------- writes ----------

def _write_blob(raw):
    """Returns (sha, size, new). `new` is False when these exact bytes are
    already held, so a deduped copy costs no space and must not count
    against the cap. The temp file carries the pid and thread so a CLI
    backfill and the background sweep cannot collide."""
    sha = hashlib.sha256(raw).hexdigest()
    dest = blob_path(sha)
    if dest.exists():
        return sha, len(raw), False
    tmpdir = root() / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f"{os.getpid()}-{threading.get_ident()}.part"
    try:
        tmp.write_bytes(raw)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():            # a concurrent writer got there first
            return sha, len(raw), False
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return sha, len(raw), True


class _Store:
    """One sweep's view of the index: the cap, the failure ledger, and the
    counts the sweep reports. Every message commits on its own: a sweep
    spends its time on network fetches, and a transaction held open across
    twenty-five of them (the media archive's cadence, where the work
    between commits is a local file copy) kept the index locked for
    longer than the other writer's 30 s wait. The background thread and
    the CLI backfill write the same index, and on 2026-10-13 the CLI's
    first pass died on exactly that."""

    def __init__(self, con):
        self.con = con
        self.cap = max_bytes()
        self.used = _used_bytes(con) if self.cap else 0
        self.archived = 0       # rows this sweep added, counted here so an
                                # error that ends a walk cannot lose them
        self.errors = 0         # walks this sweep ended in an error
        self.full = None        # the ArchiveFull message once the cap is hit;
                                # the walks stop and sweep reports it

    def known(self, key):
        return self.con.execute(
            "SELECT 1 FROM messages WHERE key=?", (key,)).fetchone() is not None

    def put(self, *, account, raw, fallback_key, fallback_date=None):
        """Archive one raw message. Returns 1 when the row is new, else 0.
        The key is the Message-ID when the headers carry one, so the same
        message met twice is one row; `fallback_key` names it otherwise."""
        if self.cap and self.used >= self.cap:
            raise ArchiveFull(
                f"archive at cap ({self.used / 1e9:.1f} of {self.cap / 1e9:.1f} GB)")
        mid, date = _describe(raw, fallback_date)
        key = f"{account}|{mid}" if mid else fallback_key
        if self.known(key):
            return 0
        sha, size, new_bytes = _write_blob(raw)
        cur = self.con.execute(
            "INSERT OR IGNORE INTO messages(key, account, message_id, sha, "
            "size, date, stored) VALUES (?,?,?,?,?,?,?)",
            (key, account, mid or None, sha, size, date, _now()))
        if new_bytes:
            self.used += size
        self.con.commit()
        new_row = int(cur.rowcount > 0)
        self.archived += new_row
        return new_row

    def commit(self):
        self.con.commit()

    def failed(self, account, ref, err):
        row = self.con.execute(
            "SELECT attempts FROM failures WHERE account=? AND ref=?",
            (account, str(ref))).fetchone()
        self.con.execute(
            "INSERT OR REPLACE INTO failures(account, ref, attempts, error, last) "
            "VALUES (?,?,?,?,?)",
            (account, str(ref), (row[0] if row else 0) + 1, str(err)[:300], _now()))

    def recovered(self, account, ref):
        self.con.execute("DELETE FROM failures WHERE account=? AND ref=?",
                         (account, str(ref)))

    def seen(self, account, validity, uid):
        return self.con.execute(
            "SELECT 1 FROM seen_uids WHERE account=? AND validity=? AND uid=?",
            (account, validity or "", int(uid))).fetchone() is not None

    def mark_seen(self, account, validity, uid):
        self.con.execute(
            "INSERT OR IGNORE INTO seen_uids(account, validity, uid) VALUES (?,?,?)",
            (account, validity or "", int(uid)))

    def retries(self, account):
        return [r[0] for r in self.con.execute(
            "SELECT ref FROM failures WHERE account=? ORDER BY ref", (account,))]


# ---------- IMAP ----------

def _uidvalidity(imap):
    """The selected mailbox's UIDVALIDITY, or "" when the server did not
    say. When it changes, every UID is reassigned and the watermark means
    nothing: the walk starts over, and the content hash keeps the re-walk
    from costing space."""
    try:
        _, data = imap.response("UIDVALIDITY")
    except Exception:  # noqa: BLE001 - a quiet server is not an error
        return ""
    if data and data[0]:
        return data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    return ""


BACKLOG_SPAN = 500           # uids per SEARCH while walking the history down


def _default_workers():
    try:
        return int(settings.get("mail_archive_workers") or 1)
    except (TypeError, ValueError):
        return 1


def _walk_history_parallel(addr, host, pw, box, validity, low, log, workers):
    """The history phase on `workers` connections at once (the CLI
    backfill; the background sweep stays on one). One message per round
    trip per connection is the rate limit, not bandwidth: four connections
    turned a six-day Gmail walk into a day and a half (2026-10-14). A
    dispenser hands each connection the next span down; the history mark
    moves only past spans that are complete and contiguous with it, and
    seen_uids makes a span met again cost a SEARCH, not a fetch. Returns
    (archived, low, cap_message, error): the error is raised by the
    caller once the marks are saved, so the backfill's retry applies."""
    lock = threading.Lock()
    st = {"next_hi": low - 1, "done": set(), "low": low,
          "n": 0, "full": None, "stop": None}

    def take():
        with lock:
            if st["stop"] is not None or st["full"] or st["next_hi"] < 1:
                return None
            hi = st["next_hi"]
            lo = max(1, hi - BACKLOG_SPAN + 1)
            st["next_hi"] = lo - 1
            return lo, hi

    def finish(lo, hi):
        with lock:
            st["done"].add((lo, hi))
            while True:
                top = next((sp for sp in st["done"] if sp[1] == st["low"] - 1), None)
                if top is None:
                    return st["low"]
                st["done"].discard(top)
                st["low"] = top[0]

    def run():
        con = _db()
        if con is None:
            with lock:
                st["stop"] = st["stop"] or OSError(f"archive root unreachable: {root()}")
            return
        store = _Store(con)
        fails = 0
        reconnects = 0
        imap = None

        def connect():
            c = imaplib.IMAP4_SSL(host, timeout=60)
            c.login(addr, pw)
            c.select(f'"{box}"', readonly=True)
            return c

        def fetch(c, uid):
            typ, md = c.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = (md[0][1] if typ == "OK" and md and
                   isinstance(md[0], tuple) else None)
            if raw is not None:
                got = store.put(account=addr, raw=raw,
                                fallback_key=f"{addr}|uid:{validity or 0}:{uid}")
                with lock:
                    st["n"] += got
            store.mark_seen(addr, validity, uid)
            store.commit()

        try:
            imap = connect()
            while True:
                span = take()
                if span is None:
                    return
                lo, hi = span
                typ, data = imap.uid("search", None, "UID", f"{lo}:{hi}")
                uids = []
                if typ == "OK" and data and data[0]:
                    uids = sorted((int(u) for u in data[0].split()
                                   if lo <= int(u) <= hi), reverse=True)
                for uid in uids:
                    if store.seen(addr, validity, uid):
                        continue
                    try:
                        fetch(imap, uid)
                        fails = 0
                        continue
                    except (ArchiveFull, sqlite3.OperationalError):
                        raise
                    except Exception as e:  # noqa: BLE001
                        fails += 1
                        if fails <= MAX_CONSECUTIVE_FAILURES:
                            # A lone failure: recorded for the next sweep,
                            # and the walk steps past it.
                            store.failed(addr, uid, e)
                            store.commit()
                            log(f"  imap {addr}: uid {uid} not archived, will retry: {e}")
                            continue
                    # Four in a row is the connection, not the mail: Gmail
                    # stalls a connection a few times a day. Replace it and
                    # try this uid once more; a walker that stopped here
                    # left the pass one connection poorer each time
                    # (2026-10-15). Only past the budget does it end, its
                    # span incomplete and holding the mark.
                    if reconnects >= WORKER_RECONNECTS:
                        log(f"  imap {addr}: a connection ended after repeated "
                            f"failures at uid {uid} and {reconnects} reconnects")
                        return
                    reconnects += 1
                    log(f"  imap {addr}: connection stalled at uid {uid}; "
                        f"reconnecting ({reconnects} of {WORKER_RECONNECTS})")
                    try:
                        imap.logout()
                    except Exception:  # noqa: BLE001
                        pass
                    imap = connect()
                    fails = 0
                    try:
                        fetch(imap, uid)
                    except (ArchiveFull, sqlite3.OperationalError):
                        raise
                    except Exception as e:  # noqa: BLE001 - poison after all
                        store.failed(addr, uid, e)
                        store.commit()
                        log(f"  imap {addr}: uid {uid} not archived, will retry: {e}")
                set_state(con, f"low:{addr}", finish(lo, hi))
                con.commit()
        except ArchiveFull as e:
            with lock:
                st["full"] = str(e)
        except Exception as e:  # noqa: BLE001 - a busy index, or a connection
            with lock:                          # that cannot even log in
                st["stop"] = st["stop"] or e
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:  # noqa: BLE001 - best-effort close
                    pass
            con.close()

    threads = [threading.Thread(target=run, name=f"mailarchive-{i}", daemon=True)
               for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return st["n"], st["low"], st["full"], st["stop"]


def _imap_sweep(acct, store, limit, log, workers=None):
    """Newest first. Two marks per account: `wm` (the newest uid covered;
    new mail lies above it) and `low` (how far down the history the walk
    has reached; 1 means the whole mailbox is held). A sweep retries its
    failures, takes new mail above `wm`, then spends the rest of its budget
    walking down from `low`, so the mail the owner needs first is backed up
    within hours and a 300,000-message history over the days after."""
    addr, host = acct["email"], acct.get("host", "")
    pw = mailmod.keychain_password(addr)
    if not pw:
        log(f"  imap {addr}: no keychain password, skipping")
        return 0
    con = store.con
    # timeout is load-bearing: imaplib's default is NO timeout, and one
    # stalled read wedged textindex's thread for a week.
    imap = imaplib.IMAP4_SSL(host, timeout=60)
    n = 0
    try:
        imap.login(addr, pw)
        box = channels.imap_special_folder(imap, "\\All", "INBOX")
        # readonly: a FETCH must never mark the owner's mail as read.
        imap.select(f'"{box}"', readonly=True)
        validity = _uidvalidity(imap)
        wm_key, low_key, val_key = f"wm:{addr}", f"low:{addr}", f"uidvalidity:{addr}"
        wm = int(get_state(con, wm_key, 0) or 0)
        low = int(get_state(con, low_key, 0) or 0)
        seen_validity = get_state(con, val_key)
        if validity and seen_validity and validity != seen_validity:
            log(f"  imap {addr}: UIDVALIDITY changed, walking the mailbox again")
            wm = low = 0
            con.execute("DELETE FROM failures WHERE account=?", (addr,))
            con.execute("DELETE FROM seen_uids WHERE account=?", (addr,))
        if validity:
            set_state(con, val_key, validity)
        if not low:
            # First sweep (or the ascending walk's state from before this
            # shape): new mail starts at today's newest uid, and the walk
            # down starts just above it. What an older ascending walk
            # already holds costs a known key, not a blob.
            wm = channels.imap_newest_uid(imap, f'"{box}"')
            low = wm + 1
            set_state(con, wm_key, wm)
            set_state(con, low_key, low)
            store.commit()
        budget = [limit]            # fetches left in this sweep, None = unbounded

        def spent():
            if budget[0] is None:
                return False
            budget[0] -= 1
            return budget[0] < 0

        fails = [0]

        def fetch_one(uid, skip_seen=False):
            """Archive one uid. Returns ("ok", rows), "seen", "gone",
            "failed" or "abort" (the connection is dying); raises on a
            busy index."""
            try:
                if skip_seen and store.seen(addr, validity, uid):
                    fails[0] = 0
                    return "seen"
                typ, md = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = (md[0][1] if typ == "OK" and md and
                       isinstance(md[0], tuple) else None)
                if raw is None:
                    store.mark_seen(addr, validity, uid)
                    fails[0] = 0
                    return "gone"              # gone between SEARCH and FETCH
                rows = store.put(account=addr, raw=raw,
                                 fallback_key=f"{addr}|uid:{validity or 0}:{uid}")
                store.mark_seen(addr, validity, uid)
                fails[0] = 0
                return ("ok", rows)
            except (ArchiveFull, sqlite3.OperationalError):
                raise
            except Exception as e:  # noqa: BLE001
                fails[0] += 1
                if fails[0] > MAX_CONSECUTIVE_FAILURES:
                    return "abort"
                # A lone failure: recorded for the next sweep, and the walk
                # steps past it so one poison message cannot hold the mailbox.
                store.failed(addr, uid, e)
                log(f"  imap {addr}: uid {uid} not archived, will retry: {e}")
                return "failed"

        try:
            # 1. Failures from earlier sweeps.
            for ref in store.retries(addr):
                try:
                    uid = int(ref)
                except ValueError:
                    store.recovered(addr, ref)    # not a uid: nothing to retry
                    continue
                if spent():
                    break
                r = fetch_one(uid)
                if r == "abort":
                    log(f"  imap {addr}: pass ended after repeated failures at uid {uid}")
                    return n
                if r != "failed":
                    store.recovered(addr, uid)
                if isinstance(r, tuple):
                    n += r[1]
                store.commit()
            # 2. New mail above the high mark, oldest of it first so the
            #    mark only ever moves up.
            typ, data = imap.uid("search", None, "UID", f"{wm + 1}:*")
            fresh = []
            if typ == "OK" and data and data[0]:
                # "N:*" with N past the last uid answers with the last uid
                # itself; only what lies above the mark is new.
                fresh = sorted(int(u) for u in data[0].split() if int(u) > wm)
            for uid in fresh:
                if spent():
                    break
                r = fetch_one(uid)
                if r == "abort":
                    log(f"  imap {addr}: pass ended after repeated failures at uid {uid}; "
                        f"new-mail mark held at {wm}")
                    return n
                if isinstance(r, tuple):
                    n += r[1]
                wm = max(wm, uid)
                set_state(con, wm_key, wm)
                store.commit()
            # 3. The history, newest first, down from the low mark. The CLI
            #    backfill (no budget) walks it on several connections.
            workers = int(workers or _default_workers())
            if limit is None and workers > 1 and low > 1:
                # Nothing of ours may hold the index while the walkers
                # write: an open write here is a 30 s wait for each of them.
                store.commit()
                got, low, full, stop = _walk_history_parallel(
                    addr, host, pw, box, validity, low, log, workers)
                n += got
                store.archived += got
                set_state(con, low_key, low)
                store.commit()
                if full:
                    store.full = full
                if stop is not None:
                    raise stop
            else:
                while low > 1 and not (budget[0] is not None and budget[0] <= 0):
                    hi = low - 1
                    lo = max(1, hi - BACKLOG_SPAN + 1)
                    typ, data = imap.uid("search", None, "UID", f"{lo}:{hi}")
                    uids = []
                    if typ == "OK" and data and data[0]:
                        uids = sorted((int(u) for u in data[0].split()
                                       if lo <= int(u) <= hi), reverse=True)
                    stopped = False
                    for uid in uids:
                        if spent():
                            stopped = True
                            break
                        r = fetch_one(uid, skip_seen=True)
                        if r == "abort":
                            log(f"  imap {addr}: pass ended after repeated failures at uid {uid}; "
                                f"history mark held at {low}")
                            return n
                        if isinstance(r, tuple):
                            n += r[1]
                        low = uid
                        set_state(con, low_key, low)
                        store.commit()
                    if stopped:
                        break
                    low = lo                 # the span is done, gaps included
                    set_state(con, low_key, low)
                    store.commit()
        finally:
            set_state(con, wm_key, wm)
            set_state(con, low_key, low)
            store.commit()
            log(f"  imap {addr}: {n} archived (new mail to uid {wm}, history down to uid {low})")
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001 - best-effort close
            pass
    return n


# ---------- Graph (Outlook / M365) ----------

def _graph_mime(msgraph, account, graph_id):
    return msgraph.get_bytes(
        account, "/me/messages/" + urllib.parse.quote(str(graph_id), safe="")
        + "/$value")


def _graph_sweep(acct, store, limit, log):
    """Graph pages newest-first and fetches each unknown message's MIME.
    The watermark (newest receivedDateTime seen) is stamped only when a
    walk COMPLETES; an interrupted or capped pass re-pages the overlap
    next time, and known keys cost a page row, not a fetch."""
    from . import msgraph
    account = acct["email"]
    con = store.con
    wm_key = f"wm:{account}"
    n = 0
    for ref in store.retries(account):
        if limit and n >= limit:
            break
        try:
            raw = _graph_mime(msgraph, account, ref)
            n += store.put(account=account, raw=raw,
                           fallback_key=f"{account}|graph:{ref}")
            store.recovered(account, ref)
        except ArchiveFull as e:
            store.full = str(e)
            log(f"  graph {account}: {e}")
            store.commit()
            return n
        except sqlite3.OperationalError:
            raise                       # the index, not the message
        except Exception as e:  # noqa: BLE001
            store.failed(account, ref, e)
            log(f"  graph {account}: retry of {ref} failed again: {e}")
        store.commit()
    since = get_state(con, wm_key)
    flt = f"receivedDateTime ge {since}" if since else ""
    path = ("/me/messages?" + (f"$filter={urllib.parse.quote(flt)}&" if flt else "")
            + f"$top={GRAPH_PAGE}&$select=id,internetMessageId,receivedDateTime")
    newest = None
    truncated = False
    fails = 0
    try:
        while path:
            out = msgraph._graph_request(account, path)
            for m in out.get("value", []):
                if limit and n >= limit:
                    truncated = True
                    break
                gid = m.get("id") or ""
                received = m.get("receivedDateTime") or ""
                newest = max(newest or "", received)
                mid = (m.get("internetMessageId") or "").strip()
                key = f"{account}|{mid}" if mid else f"{account}|graph:{gid}"
                if store.known(key):
                    continue
                try:
                    raw = _graph_mime(msgraph, account, gid)
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    if fails > MAX_CONSECUTIVE_FAILURES:
                        log(f"  graph {account}: pass ended after repeated "
                            f"failures; watermark held")
                        return n
                    store.failed(account, gid, e)
                    log(f"  graph {account}: message not archived, will retry: {e}")
                    continue
                fails = 0
                try:
                    n += store.put(account=account, raw=raw,
                                   fallback_key=f"{account}|graph:{gid}",
                                   fallback_date=received)
                except ArchiveFull as e:
                    store.full = str(e)
                    log(f"  graph {account}: {e}")
                    truncated = True
                    break
                store.commit()
            store.commit()
            if truncated:
                break
            nxt = out.get("@odata.nextLink")
            path = (nxt[len(msgraph.GRAPH):]
                    if nxt and nxt.startswith(msgraph.GRAPH) else nxt or None)
        if path is None and not truncated and newest:
            set_state(con, wm_key, newest)
    finally:
        store.commit()
    log(f"  graph {account}: {n} archived")
    return n


# ---------- the sweep ----------

def _sweep(log, limit, account, workers=None):
    """(archived, errored) for one pass over the accounts."""
    if not enabled():
        log("mail archive: disabled")
        return 0, False
    con = _db()
    if con is None:
        log(f"mail archive: root unreachable ({root()}), nothing archived")
        return 0, False
    try:
        store = _Store(con)
        for acct in _accounts():
            addr = acct.get("email") or ""
            if not addr or (account and addr != account):
                continue
            try:
                if acct.get("type") == "graph":
                    _graph_sweep(acct, store, limit, log)
                else:
                    _imap_sweep(acct, store, limit, log, workers=workers)
            except Exception as e:  # noqa: BLE001 - one bad mailbox never
                store.errors += 1                   # stops the others
                log(f"  {addr}: walk ended early: {e}")
            if store.full:
                log(f"mail archive: {store.full}; nothing more archived")
                break
        store.commit()
        log(f"mail archive: {store.archived} newly archived"
            + (f", {store.errors} walk(s) ended early" if store.errors else ""))
        return store.archived, store.errors > 0
    finally:
        con.close()


@runtimework.tracked("Mail archiving")
def sweep(log=print, limit=None, account=None, workers=None):
    """Archive what each account holds that the archive does not. Bounded
    per account by `limit` (the background tick) or unbounded (the CLI).
    Returns the number of messages archived, counted as they land, so a
    walk that ends in an error still reports what it did.

    Cheap to repeat once the history is in: IMAP asks for UIDs above the
    watermark and Graph for mail received since it."""
    return _sweep(log, limit, account, workers)[0]


BACKFILL_RETRIES = 3     # passes ending in an error before the CLI gives up


def backfill(log=print, account=None, workers=None):
    """The backlog: unbounded sweeps until a clean pass archives nothing
    and moves no watermark. A pass that ends in an error (the index busy
    under the background thread, a dying connection) is retried after a
    pause, a few times in a row before giving up; its rows are already in,
    and its watermark sits where the mail is. Resumable at any point."""
    total = 0
    errored_in_a_row = 0
    while True:
        con = _db()
        before = _watermarks(con) if con is not None else {}
        if con is not None:
            con.close()
        n, errored = _sweep(log, None, account, workers)
        total += n
        log(f"mail archive backlog: +{n} (total {total})")
        con = _db()
        after = _watermarks(con) if con is not None else {}
        if con is not None:
            con.close()
        if errored:
            errored_in_a_row += 1
            if errored_in_a_row >= BACKFILL_RETRIES:
                log(f"mail archive backlog: giving up after {errored_in_a_row} "
                    f"passes ending in errors; rerun to resume")
                return total
            log(f"mail archive backlog: pass ended in an error, retrying "
                f"({errored_in_a_row} of {BACKFILL_RETRIES})")
            time.sleep(5)
            continue
        errored_in_a_row = 0
        if n == 0 and after == before:
            return total


class Archiver(threading.Thread):
    """Keeps Vira's copy current. Its own thread, like mediaarchive's: this
    is not indexing, it needs none of the model stack, and it must keep
    running while a CLI backfill owns the index scans. Slow cadence and a
    bounded tick: the only new work between ticks is what arrived."""

    def __init__(self, interval_min=None):
        super().__init__(daemon=True)
        self.interval = max(1, int(
            interval_min or settings.get("mail_archive_interval_min") or 30
        )) * 60

    def _log(self, msg):
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}\n"
        try:
            with open(_DATA / "mail-archive.log", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def run(self):
        time.sleep(90)          # let the server settle; this is never urgent
        while True:
            try:
                if enabled():
                    sweep(log=self._log, limit=SWEEP_LIMIT)
            except Exception as e:      # noqa: BLE001 - never kill the thread
                self._log(f"archiver error: {e}")
            time.sleep(self.interval)


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "status":
        print(json.dumps(stats(), indent=2))
    elif cmd == "sweep":
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else SWEEP_LIMIT
        sweep(limit=lim)
    elif cmd == "backfill":
        who = args[args.index("--account") + 1] if "--account" in args else None
        workers = int(args[args.index("--workers") + 1]) if "--workers" in args else None
        backfill(account=who, workers=workers)
        print(json.dumps(stats(), indent=2))
    else:
        print("usage: python -m server.mailarchive status | sweep [--limit N] "
              "| backfill [--account ADDR] [--workers N]")

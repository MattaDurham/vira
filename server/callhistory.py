"""The Mac's call history: phone and FaceTime calls, read-only.

macOS keeps one call log for the whole Apple account, and the iPhone's
calls sync into it (Settings > Phone > Calls on Other Devices, or the
FaceTime "Calls from iPhone" switch on the Mac). It is a Core Data SQLite
store, `~/Library/Application Support/CallHistoryDB/CallHistory.storedata`,
and like chat.db it needs Full Disk Access, which the venv's Python holds.

Apple system databases are read-only to Vira: this module opens the store
with `mode=ro` and never writes. A missing store (any non-Mac install, or a
Mac that never placed a call) and an unreadable one (no Full Disk Access)
are named statuses, never an exception that reaches a caller.

ZCALLRECORD columns read here (measured on macOS 26, 2026-11-19):
ZUNIQUE_ID (stable per call), ZADDRESS (the other side's number or email),
ZDATE (call start, seconds since 2001-01-01 UTC), ZDURATION (seconds),
ZANSWERED, ZORIGINATED (1 = the owner placed it), ZCALLTYPE (1 phone,
8 FaceTime video, 16 FaceTime audio) and ZSERVICE_PROVIDER.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

DB = Path.home() / "Library" / "Application Support" / "CallHistoryDB" / "CallHistory.storedata"
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
# One read returns at most this many calls. The caller asks for a window of
# a few hours; a person places a handful of calls in that time, so the cap
# only bounds a damaged or misread watermark.
MAX_ROWS = 500
KINDS = {1: "phone", 8: "FaceTime video", 16: "FaceTime audio"}


class Unavailable(Exception):
    """The store cannot be read; `status` is "missing" or "unreadable"."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _connect():
    if not DB.exists():
        raise Unavailable("missing", "No call history on this Mac.")
    uri = DB.resolve().as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.execute("SELECT 1 FROM ZCALLRECORD LIMIT 1")
        return con
    except sqlite3.Error as exc:
        raise Unavailable("unreadable", "The call history cannot be read "
                          f"({type(exc).__name__}); Vira's Python needs Full Disk Access.") from exc


def _instant(apple_s):
    return APPLE_EPOCH + dt.timedelta(seconds=float(apple_s or 0))


def _apple(instant):
    return (instant - APPLE_EPOCH).total_seconds()


def _kind(call_type, provider):
    if call_type in KINDS:
        return KINDS[call_type]
    return "FaceTime" if "facetime" in str(provider or "").lower() else "phone"


def calls_ended_since(since):
    """Calls that ended at or after `since` (an aware datetime), oldest
    first. Raises Unavailable when the store cannot be read."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT ZUNIQUE_ID, Z_PK, ZADDRESS, ZDATE, ZDURATION, ZANSWERED, "
            "ZORIGINATED, ZCALLTYPE, ZSERVICE_PROVIDER FROM ZCALLRECORD "
            "WHERE ZDATE + COALESCE(ZDURATION, 0) >= ? ORDER BY ZDATE LIMIT ?",
            (_apple(since), MAX_ROWS)).fetchall()
    except sqlite3.Error as exc:
        raise Unavailable("unreadable", f"The call history cannot be read ({type(exc).__name__}).") from exc
    finally:
        con.close()
    out = []
    for uid, pk, address, start, duration, answered, originated, call_type, provider in rows:
        began = _instant(start)
        seconds = max(0.0, float(duration or 0))
        out.append({
            "id": str(uid or f"pk-{pk}"),
            "handle": str(address or ""),
            "started_at": began.isoformat(),
            "ended_at": (began + dt.timedelta(seconds=seconds)).isoformat(),
            "seconds": round(seconds),
            "answered": bool(answered),
            "outgoing": bool(originated),
            "kind": _kind(call_type, provider),
        })
    return out


def status():
    """"ok", "missing" or "unreadable", with the reason."""
    try:
        _connect().close()
        return {"status": "ok", "detail": ""}
    except Unavailable as exc:
        return {"status": exc.status, "detail": exc.detail}

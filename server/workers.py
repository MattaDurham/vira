"""Background work, one row per worker: its switch, its last run, its error.

Config > Background work reads this. Each worker already keeps its own
state somewhere (a JSON store, a sqlite meta row, a thread's status
string, a log); this module only gathers those cheap reads into one
shape so the owner can see every sweep and watcher in one card, with the
setting that switches it beside the time it last ran. Nothing here starts,
stops or runs a worker; the switch is a plain settings key and the worker
reads it on its next tick.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import settings

_DATA = settings.ROOT / "data"
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _row(wid, title, *, enabled_key=None, enabled=None, cadence="",
         last_run=None, last_error=None, detail="", running=None):
    return {"id": wid, "title": title, "enabled_key": enabled_key,
            "enabled": enabled, "cadence": cadence, "last_run": last_run,
            "last_error": last_error, "detail": detail, "running": running}


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _log_last(path):
    """The timestamp on the last line of a worker's own log, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        m = _STAMP.match(line)
        if m:
            return m.group(1)
    return None


def _assistant():
    from . import contactintel, executive
    cfg = executive.config()
    state = _read_json(executive.STATE)
    try:
        contact = contactintel.status()
    except Exception:  # noqa: BLE001 - a status read never blocks the card
        contact = {}
    cadence = f"every message, {cfg['assistant_debounce_s']} s debounce"
    error = state.get("last_error") or contact.get("last_error")
    return _row("assistant", "Assistant",
                enabled_key="assistant_enabled", enabled=bool(cfg["assistant_enabled"]),
                cadence=cadence, last_run=state.get("last_run") or contact.get("last_run"),
                last_error=error,
                detail=(f"{contact.get('pending_contacts', 0)} contact(s) queued"
                        if isinstance(contact.get("pending_contacts"), int) else ""))


def _intake():
    from . import correspondence
    cfg = correspondence.config()
    state = correspondence._state()
    counts = {}
    for item in state.get("items", {}).values():
        counts[item.get("state")] = counts.get(item.get("state"), 0) + 1
    waiting = counts.get("review", 0) + counts.get("pending", 0)
    return _row("intake", "Intake (filing messages into vaults)",
                enabled_key=None, enabled=bool(cfg.get("enabled")),
                cadence=f"every message; first scan {cfg.get('catchup_days', 14)} days back",
                last_run=state.get("last_run"), last_error=correspondence._worker_error,
                detail=f"{waiting} waiting for review" if waiting else "")


def _text_index():
    from . import textindex
    st = textindex.status()
    detail = (f"{st.get('messages', 0):,} texts, {st.get('emails', 0):,} emails indexed"
              if st.get("available") else "not indexed yet")
    return _row("textindex", "Message index",
                enabled_key="mail_body_index", enabled=bool(settings.get("mail_body_index")),
                cadence="every 3 minutes; the switch adds email bodies",
                last_run=None, detail=detail)


def _media_archive():
    from . import mediaarchive
    st = mediaarchive.stats()
    gb = st.get("bytes", 0) / 1e9
    detail = (f"{st.get('files', 0):,} attachments, {gb:.1f} GB" if st.get("available")
              else "archive not created yet")
    return _row("media_archive", "Media archive",
                enabled_key="media_archive_enabled", enabled=bool(st.get("enabled")),
                cadence=f"every {settings.get('media_archive_interval_min')} minutes",
                last_run=_log_last(_DATA / "media-archive.log"), detail=detail)


def _mail_archive():
    from . import mailarchive
    st = mailarchive.stats()
    gb = st.get("bytes", 0) / 1e9
    if st.get("available"):
        detail = f"{st.get('messages', 0):,} messages, {gb:.1f} GB"
        if st.get("failed"):
            detail += f", {st['failed']} to retry"
    else:
        detail = "archive not created yet"
    return _row("mail_archive", "Mail archive",
                enabled_key="mail_archive_enabled", enabled=bool(st.get("enabled")),
                cadence=f"every {settings.get('mail_archive_interval_min')} minutes",
                last_run=_log_last(_DATA / "mail-archive.log"), detail=detail)


def _ai_health():
    from . import aihealth
    s = aihealth.summary()
    return _row("ai_health", "AI login watch",
                enabled_key="ai_health_enabled", enabled=bool(settings.get("ai_health_enabled")),
                cadence=f"every {settings.get('ai_health_interval_min')} minutes",
                last_run=s.get("checked_at") or None,
                # aihealth states: green | red | setup (nothing connected yet)
                last_error=s.get("detail") if s.get("state") == "red" else None,
                detail=f"login {s.get('state', 'unknown')}" + (f" ({s['backend']})" if s.get("backend") else ""))


def _receipts(thread_status):
    last = None
    m = re.search(r"last sweep (\S+)", thread_status or "")
    if m:
        last = m.group(1)
    error = thread_status if (thread_status or "").startswith("error") else None
    return _row("receipts", "Receipt sweep",
                enabled_key="receipts_sweep_days", enabled=None,
                cadence=f"every {settings.get('receipts_sweep_days')} days",
                last_run=last, last_error=error, detail=thread_status or "")


def _boards():
    from . import jobboards
    h = jobboards.health()
    errors = h.get("errors") or {}
    return _row("boards", "Job board scoring",
                enabled_key="boards_auto_score", enabled=bool(settings.get("boards_auto_score")),
                cadence=f"every {settings.get('boards_poll_minutes') or 15} minutes",
                last_run=h.get("fetched") or None,
                last_error=(f"{len(errors)} board(s) failed on the last sweep" if errors else None),
                detail=(f"{h.get('registered', 0)} board(s) registered" if h.get("registered")
                        else "no boards registered"))


def status(receipts_status=""):
    """Every worker, in the order the owner meets them. A worker whose
    status read fails shows its error instead of hiding the row."""
    out = []
    for build, args in ((_assistant, ()), (_intake, ()), (_text_index, ()),
                        (_media_archive, ()), (_mail_archive, ()), (_ai_health, ()),
                        (_receipts, (receipts_status,)), (_boards, ())):
        try:
            out.append(build(*args))
        except Exception as exc:  # noqa: BLE001 - one broken read never hides the rest
            out.append(_row(build.__name__.strip("_"), build.__name__.strip("_").replace("_", " "),
                            last_error=f"status unavailable ({type(exc).__name__})"))
    return {"workers": out, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

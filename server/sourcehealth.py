"""Source-health sentinel: "nothing found" must not pass for "nothing there".

When a source dies quietly - the calendar store loses its sync, a mailbox
stops delivering, chat.db falls behind Full Disk Access, the vault index is
emptied - every reader built on it keeps answering. The brief says "Clear
calendar.", the calendar tool says "No events found", mail search says "no
matches", and each of those reads as a fact about the owner's life when it
is really a fact about a broken pipe. This module tells the two apart.

It keeps a BASELINE per source, learned from its own daily readings:

  calendar:local     events today on the local Calendar store, per weekday
  calendar:m365:<a>  events today on each M365 work calendar, per weekday
  mail:<account>     messages archived from each mailbox in the last 24 h
  imessage           messages in chat.db in the last 24 h
  vault              notes indexed, plus the share of vault searches that
                     found anything (recorded by the vault_search tool)

A source LOOKS BROKEN when it reads zero (or cannot be read at all) while
its baseline says it is normally busy. Then:

  - the brief carries a `source_health` section and the window shows a
    "looks broken since X" banner above the sections that source feeds;
  - every tool answer drawn from that source (viratools.invoke) starts
    with the same label, so a model reads an empty result as unknown;
  - Attention's Now lane shows one health row per broken source;
  - Vira files ONE fix-it item in the ideas backlog (status `proposed`, so
    it waits in the Decide queue for the owner), once per breakage, never
    again while that item is still open.

THE RULES IT IS BENT AROUND:

1. **A day on which the source looked broken never enters its baseline.**
   Otherwise a week of breakage teaches the baseline that zero is normal
   and the alarm clears itself.
2. **No baseline, no verdict.** Until a source has enough clean days
   (MIN_SAMPLES) it is `learning`; a source that never read above zero is
   `unavailable`, not broken. A new install, a Windows machine without
   chat.db, or a quiet weekend is never called broken.
3. **Deterministic and cheap.** COUNT queries and a stat; the M365 probe is
   the calendar call the brief already makes. No model call, ever, and the
   checks run in a background worker (Watcher), never on a request path.
   Readers (`status`, `tool_notice`, the brief, Attention) only read the
   store.
4. **Recovery is automatic, the fix-it item is not.** A source that reads
   above zero again goes back to `ok` with `recovered_at` set; the backlog
   item stays until the owner closes it, because the owner may want to know
   why it broke.

The store, data/source-health.json, is derived: losing it costs a few weeks
of relearning, never data, so it is not in backup.FILES.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
import threading
import time
from pathlib import Path

from . import jsonstore, settings

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "source-health.json"

# How much history a source keeps: eight of each weekday is enough for a
# weekday median and small enough to read on every tool call.
HISTORY_DAYS = 60
# Clean readings a baseline needs before it may call a source broken. A
# weekday baseline counts only that weekday, so three is three weeks.
MIN_SAMPLES = {"weekday": 3, "daily": 5}
# The median a source must normally reach for a zero to mean "broken"
# rather than "a quiet day". Calendar asks for more than one event so a
# light weekday that happens to be empty does not raise a fix-it item.
MIN_BASELINE = {"calendar": 3, "mail": 3, "imessage": 3, "vault": 1}
# Vault searches: the day is judged only after this many searches, and
# only when the clean days' hit share was at least VAULT_HIT_SHARE.
VAULT_MIN_SEARCHES = 5
VAULT_HIT_SHARE = 0.5
WINDOW_H = 24     # rolling window for the mail and iMessage counts
APPLE_EPOCH = 978307200

KIND_LABELS = {"calendar": "calendar", "mail": "mail",
               "imessage": "iMessage", "vault": "knowledge vault"}

# Which sources each native tool's answer is drawn from. A tool absent here
# reads nothing a sentinel watches (CRM, ideas, writes).
TOOL_KINDS = {
    "calendar": ("calendar",),
    "daily_brief": ("calendar", "mail", "imessage"),
    "mail_search": ("mail",),
    "imessage_thread": ("imessage",),
    "message_context": ("imessage",),
    "thread_read": ("imessage",),
    "media_search": ("imessage",),
    "vault_search": ("vault",),
    "vault_note": ("vault",),
    "vault_query": ("vault",),
    "find": ("vault", "imessage", "mail"),
}

_lock = threading.Lock()   # one check at a time inside this process


def _default():
    return {"sources": {}, "vault_searches": {}, "checked_at": None}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _local_date(t):
    return t.astimezone().date().isoformat()


def since_label(iso):
    """'Oct 25, 9:14 AM' in local time, the X of 'broken since X'."""
    try:
        t = dt.datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return iso or "?"
    return settings.strf(t, "%b %-d, %-I:%M %p")


# ---------------------------------------------------------------- probes
# A probe returns readings: {key, kind, label, mode, value, error, absent}.
#   value  an int count, or None when it could not be read
#   error  why it could not be read (a broken store)
#   absent the source does not exist on this machine (no verdict at all)

def _reading(key, kind, label, mode, value=None, error=None, absent=None):
    return {"key": key, "kind": kind, "label": label, "mode": mode,
            "value": value, "error": error, "absent": absent}


def _probe_calendar():
    from . import brief, channels, msgraph
    out = []
    label = "Local calendar"
    if not settings.IS_MAC or not brief.CAL_DB.exists():
        out.append(_reading("calendar:local", "calendar", label, "weekday",
                            absent="no local Calendar store on this machine"))
    else:
        events = brief._occurrences(*brief._day_bounds(0))
        err = brief._cal_error
        out.append(_reading(
            "calendar:local", "calendar", label, "weekday",
            value=None if err else sum(1 for e in events if not e["birthday"]),
            error=err))
    start, end = brief._day_bounds(0)
    for acct in channels.graph_accounts():
        addr = acct["email"]
        key = f"calendar:m365:{addr.lower()}"
        lab = f"M365 calendar ({addr})"
        try:
            n = len(msgraph.calendar_events(addr, start.isoformat(),
                                            end.isoformat()))
            out.append(_reading(key, "calendar", lab, "weekday", value=n))
        except Exception as e:  # noqa: BLE001 - a failed read is a reading
            out.append(_reading(key, "calendar", lab, "weekday",
                                error=str(e)[:160] or type(e).__name__))
    return out


def _probe_imessage():
    from . import imessage
    label = "iMessage"
    if not settings.IS_MAC or not imessage.CHAT_DB.exists():
        return [_reading("imessage", "imessage", label, "daily",
                         absent="no Messages database on this machine")]
    cutoff_ns = int((time.time() - WINDOW_H * 3600 - APPLE_EPOCH) * 1e9)
    try:
        con = imessage._connect()
        try:
            n = con.execute("SELECT COUNT(*) FROM message WHERE date > ?",
                            (cutoff_ns,)).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        return [_reading("imessage", "imessage", label, "daily",
                         error=f"chat.db unreadable: {str(e)[:120]}")]
    return [_reading("imessage", "imessage", label, "daily", value=int(n))]


def _probe_mail():
    from . import mailarchive
    accounts = [a.get("email") for a in mailarchive._accounts()
                if a.get("email")]
    if not accounts:
        return []
    if not mailarchive.enabled():
        # The archive is the one durable record of what arrived when; the
        # watcher's feed starts empty after every restart, so counting it
        # would call every mailbox broken for the first day after a boot.
        return [_reading(f"mail:{a.lower()}", "mail", f"Mail ({a})", "daily",
                         absent="mail archive is off, so arrivals are not "
                                "counted") for a in accounts]
    cutoff = _iso(_now() - dt.timedelta(hours=WINDOW_H))
    con = mailarchive._db(create=False)
    if con is None:
        return [_reading(f"mail:{a.lower()}", "mail", f"Mail ({a})", "daily",
                         error="mail archive unreachable") for a in accounts]
    out = []
    try:
        for a in accounts:
            n = con.execute(
                "SELECT COUNT(*) FROM messages WHERE account = ? AND date >= ?",
                (a, cutoff)).fetchone()[0]
            out.append(_reading(f"mail:{a.lower()}", "mail", f"Mail ({a})",
                                "daily", value=int(n)))
    except sqlite3.Error as e:
        out = [_reading(f"mail:{a.lower()}", "mail", f"Mail ({a})", "daily",
                        error=f"mail archive unreadable: {str(e)[:120]}")
               for a in accounts]
    finally:
        con.close()
    return out


def _probe_vault():
    from . import vault
    try:
        st = vault.status()
    except Exception as e:  # noqa: BLE001
        return [_reading("vault", "vault", "Knowledge vault", "daily",
                         error=str(e)[:160])]
    if not st.get("vaults"):
        return [_reading("vault", "vault", "Knowledge vault", "daily",
                         absent="no vault configured")]
    if not st.get("available"):
        errs = [v.get("error") for v in st["vaults"] if v.get("error")]
        return [_reading("vault", "vault", "Knowledge vault", "daily",
                         error=(errs[0] if errs else "vault index unavailable"))]
    return [_reading("vault", "vault", "Knowledge vault", "daily",
                     value=int(st.get("notes") or 0))]


PROBES = (_probe_calendar, _probe_mail, _probe_imessage, _probe_vault)


def probe_all(probes=None):
    """Every reading, never raising: a probe that crashes is one source
    read as an error, not a sentinel that stops."""
    out = []
    for fn in probes or PROBES:
        try:
            out.extend(fn())
        except Exception as e:  # noqa: BLE001
            name = fn.__name__.removeprefix("_probe_")
            out.append(_reading(name, name, KIND_LABELS.get(name, name),
                                "daily", error=f"probe failed: {str(e)[:120]}"))
    return out


# ---------------------------------------------------------------- verdicts

def _baseline(history, today, mode):
    """Median of the clean readings before today (same weekday in weekday
    mode), or None while there are fewer than MIN_SAMPLES of them."""
    wd = dt.date.fromisoformat(today).weekday()
    vals = [h["value"] for h in history
            if h["date"] != today and not h.get("broken")
            and h.get("value") is not None
            and (mode != "weekday"
                 or dt.date.fromisoformat(h["date"]).weekday() == wd)]
    if len(vals) < MIN_SAMPLES[mode]:
        return None, len(vals)
    return statistics.median(vals), len(vals)


def _vault_search_verdict(state, today):
    """(broken, detail) from the vault_search tally: today's searches all
    came back empty while clean days usually found something."""
    days = state.get("vault_searches") or {}
    t = days.get(today) or {}
    if int(t.get("searches") or 0) < VAULT_MIN_SEARCHES or t.get("hits"):
        return False, ""
    clean = [d for k, d in days.items() if k != today
             and int(d.get("searches") or 0) >= VAULT_MIN_SEARCHES
             and not d.get("broken")]
    if len(clean) < MIN_SAMPLES["daily"]:
        return False, ""
    share = (sum(int(d.get("hits") or 0) for d in clean)
             / max(1, sum(int(d.get("searches") or 0) for d in clean)))
    if share < VAULT_HIT_SHARE:
        return False, ""
    return True, (f"all {t['searches']} vault searches today found nothing; "
                  f"usually {round(share * 100)}% find something")


def _unit(kind, mode, today):
    """(what was counted, the baseline's qualifier)."""
    if kind == "calendar":
        day = dt.date.fromisoformat(today).strftime("%A")
        return "events today", f" on {day}s"
    if kind == "vault":
        return "notes indexed", ""
    return f"messages in the last {WINDOW_H} hours", ""


def _judge(entry, r, state, today, now_iso):
    """Fold one reading into its entry. Returns True on a new breakage."""
    was_broken = entry.get("state") == "broken"
    hist = [h for h in entry.get("history") or [] if h.get("date") != today]
    seen_positive = any((h.get("value") or 0) > 0 for h in hist) \
        or bool(entry.get("seen_positive"))
    entry.update(kind=r["kind"], label=r["label"], mode=r["mode"],
                 checked_at=now_iso, value=r["value"])
    broken, detail = False, ""
    if r["absent"] or r["error"]:
        why = r["absent"] or r["error"]
        if seen_positive:
            broken, detail = True, f"cannot be read: {why}"
        else:
            entry.update(state="unavailable", detail=why, baseline=None)
    else:
        value = r["value"]
        hist.append({"date": today, "value": value})
        base, n = _baseline(hist, today, r["mode"])
        unit, on = _unit(r["kind"], r["mode"], today)
        entry["baseline"] = base
        if value == 0 and base is not None \
                and base >= MIN_BASELINE.get(r["kind"], 1):
            broken = True
            detail = f"0 {unit}; usually {base:g}{on}"
        elif r["kind"] == "vault":
            broken, detail = _vault_search_verdict(state, today)
        if not broken:
            if base is None:
                entry.update(state="learning",
                             detail=f"{value} {unit}; learning its baseline "
                                    f"({n} of {MIN_SAMPLES[r['mode']]} days)")
            else:
                entry.update(state="ok",
                             detail=f"{value} {unit}; usually {base:g}{on}")
        hist[-1]["broken"] = broken
        if value:
            seen_positive = True
    if broken:
        entry.update(state="broken", detail=detail,
                     since=entry.get("since") if was_broken else now_iso)
        entry.pop("recovered_at", None)
    else:
        entry["since"] = None
        if was_broken:
            entry["recovered_at"] = now_iso
    cutoff = (dt.date.fromisoformat(today)
              - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    entry["history"] = [h for h in hist if h["date"] >= cutoff]
    entry["seen_positive"] = seen_positive
    return broken and not was_broken


def check(probes=None, file_items=True):
    """Probe every source, fold the readings into the store, and file a
    fix-it item for each source that just broke. Returns status()."""
    with _lock:
        readings = probe_all(probes)   # probes run outside the store lock
        now = _now()
        now_iso, today = _iso(now), _local_date(now)
        fresh = []

        def fold(state):
            srcs = state.setdefault("sources", {})
            for r in readings:
                entry = srcs.setdefault(r["key"], {})
                if _judge(entry, r, state, today, now_iso):
                    fresh.append(r["key"])
            vs = state.setdefault("vault_searches", {})
            if today in vs:
                vs[today]["broken"] = (srcs.get("vault") or {}).get(
                    "state") == "broken"
            for k in sorted(vs)[:-HISTORY_DAYS]:
                vs.pop(k, None)
            state["checked_at"] = now_iso
            return state

        jsonstore.mutate(STORE, fold, _default())
        if file_items:
            for key in fresh:
                _file_fix_it(key)
    return status()


def _file_fix_it(key):
    """One proposed backlog item per breakage. Filed outside the store lock
    (ideas has its own), and skipped while an earlier item for the same
    source is still waiting, so a flapping source asks once."""
    from . import ideas
    entry = (jsonstore.read(STORE, _default()).get("sources") or {}).get(key)
    if not entry or entry.get("state") != "broken":
        return None
    prior = entry.get("idea_id")
    if prior:
        for it in ideas.list_items():
            if it["id"] == prior and it.get("status") in (
                    "proposed", "open", "on-hold"):
                return prior
    text = (f"Fix the {entry['label']} source: it looks broken since "
            f"{since_label(entry.get('since'))} ({entry.get('detail')})")
    note = ("Filed by the source-health sentinel. Until it recovers, the "
            "brief and every tool answer drawn from this source carry a "
            "'looks broken' label. The label clears itself when the source "
            "reads normally again; this item does not.")
    try:
        item = ideas.add(text, status="proposed", source="source-health",
                         note=note)
    except (OSError, ValueError):
        return None

    def save(state):
        e = (state.get("sources") or {}).get(key)
        if e is not None:
            e["idea_id"] = item["id"]
        return state
    jsonstore.mutate(STORE, save, _default())
    return item["id"]


def note_vault_search(found):
    """Tally one vault search for today (the vault_search tool calls this).
    Never raises: a tally that cannot be written is not a failed search."""
    today = _local_date(_now())

    def bump(state):
        d = state.setdefault("vault_searches", {}).setdefault(
            today, {"searches": 0, "hits": 0})
        d["searches"] = int(d.get("searches") or 0) + 1
        d["hits"] = int(d.get("hits") or 0) + (1 if found else 0)
        return state
    try:
        jsonstore.mutate(STORE, bump, _default())
    except OSError:
        pass


# ---------------------------------------------------------------- readers

def _public(key, e):
    return {"key": key, "kind": e.get("kind"), "label": e.get("label"),
            "state": e.get("state") or "learning", "detail": e.get("detail"),
            "since": e.get("since"), "value": e.get("value"),
            "baseline": e.get("baseline"), "checked_at": e.get("checked_at"),
            "recovered_at": e.get("recovered_at"),
            "idea_id": e.get("idea_id"), "label_text": label_text(e)}


def label_text(e):
    if (e or {}).get("state") != "broken":
        return ""
    return (f"{e.get('label')} source looks broken since "
            f"{since_label(e.get('since'))}: {e.get('detail')}")


def status():
    """Every source and its verdict, read from the store only."""
    state = jsonstore.read(STORE, _default())
    rows = [_public(k, e) for k, e in sorted((state.get("sources") or {}).items())]
    return {"sources": rows,
            "broken": [r for r in rows if r["state"] == "broken"],
            "checked_at": state.get("checked_at"),
            "enabled": bool(settings.get("source_health_enabled"))}


def broken(kinds=None):
    try:
        rows = status()["broken"]
    except Exception:  # noqa: BLE001 - a health read never breaks a reader
        return []
    return [r for r in rows if kinds is None or r["kind"] in kinds]


def tool_notice(tool_name):
    """The label a tool answer leads with, or '' when its sources are
    healthy. Read-only and cheap: it runs on every tool call."""
    kinds = TOOL_KINDS.get(str(tool_name or "").removeprefix("mcp__vira__"))
    if not kinds:
        return ""
    rows = broken(kinds)
    if not rows:
        return ""
    lines = ["SOURCE HEALTH WARNING - an empty or thin answer below may mean "
             "the source is broken, not that nothing is there. Say so "
             "rather than reporting absence as fact:"]
    lines += [f"- {r['label_text']}" for r in rows]
    return "\n".join(lines)


def brief_section():
    """The brief's `source_health` payload: only what needs saying."""
    return {"broken": [{k: r[k] for k in ("key", "kind", "label", "since",
                                          "detail", "label_text", "idea_id")}
                       for r in broken()]}


def attention_rows(make_row):
    """Attention Now rows, one per broken source. `make_row` is
    attention._row, passed in so this module does not import attention."""
    out = []
    for r in broken():
        since = r.get("since") or ""
        out.append(make_row(
            f"health:source:{r['key']}", "health", f"since{since}", True,
            f"{r['label']} source looks broken",
            f"since {since_label(since)}: {r.get('detail') or ''}"[:160],
            verb="open", activity_at=since))
    return out


# ---------------------------------------------------------------- worker

class Watcher:
    """Runs check() on a cadence. Off in a fixture preview: its probes read
    the host's real stores, which a synthetic instance must not see."""

    def __init__(self):
        self._stop = threading.Event()
        self._t = None

    def start(self):
        if not settings.get("source_health_enabled"):
            print("source-health: sentinel disabled by config")
            return
        if settings.fixture_mode():
            return
        self._t = threading.Thread(target=self._loop, daemon=True,
                                   name="vira-source-health")
        self._t.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        self._stop.wait(90)   # let the indexers and watchers settle first
        while not self._stop.is_set():
            try:
                check()
            except Exception as e:  # noqa: BLE001 - the watcher never dies
                print(f"source-health: check failed: {e}")
            minutes = int(settings.get("source_health_interval_min") or 30)
            self._stop.wait(max(5, minutes) * 60)


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m server.sourcehealth")
    ap.add_argument("cmd", choices=("status", "check"))
    ap.add_argument("--no-file", action="store_true",
                    help="check without filing fix-it items")
    a = ap.parse_args(argv)
    out = check(file_items=not a.no_file) if a.cmd == "check" else status()
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Appointments: reminder texts and mail promoted to real calendar entries.

A doctor's SMS short code, a booking site's confirmation mail and a patient
portal's "see you Tuesday" are the only record of many appointments; none of
them is on the calendar, so the brief used to say "nothing scheduled" on a
day with a 2:30 visit. This module

  1. reads the admin inbox's institutional messages (server/admininbox.py:
     chat.db 1:1 threads and the mail watcher feed) and parses, with no
     model call, the ones that state a booked appointment: a date and a
     clock time in the text itself, next to an appointment word;
  2. keeps one ledger row per appointment in data/appointments.json. The
     row's id is its start minute, so the 3-day, 1-day and same-day
     reminders for one visit (and the mail copy of the same SMS) collapse
     into one row with several sources. A later "has been cancelled" or
     "rescheduled" message from the same sender retires the earlier row;
  3. adds the owner's travel buffer around an in-person appointment (none
     for a video or phone visit), so the brief shows when to leave and a
     meeting inside the travel time reads as a conflict;
  4. stages each future appointment that is not already on a calendar as a
     calendarplan draft (schedule_kind "appointment") and, when the
     `appointment_calendar_entries` setting and the assistant allow it,
     writes it to the personal calendar with no invitees.

Brief rows are derived at read time: an appointment already on a calendar
annotates that event (leave-by time, appointment tag); one that is not yet
on a calendar becomes its own row, labelled as found in a message. The
ledger is rebuildable from the messages that are still in the look-back
window, like the admin inbox's cache, so it is not in backup.FILES.
"""

import copy
import datetime as dt
import hashlib
import re
import time

from . import admininbox, jsonstore, settings
from .filelock import locked

STORE = settings.ROOT / "data" / "appointments.json"
STATUSES = ("upcoming", "cancelled", "superseded", "dismissed")
# Message keys already applied to the ledger. A key is applied once, so a
# cancellation is never re-applied over a later confirmation; this bounds the
# memory of keys long gone from the 14-day look-back window.
SEEN_KEEP = 1000
# A finished appointment stays this long (the brief only looks at today and
# tomorrow; a week covers a late reminder for one that already happened).
KEEP_PAST_DAYS = 7
# The brief refreshes the ledger at most this often; the scan re-reads two
# weeks of chat.db 1:1 threads and must not run on every brief poll.
REFRESH_S = 120
# Twin tolerance: a calendar event starting this close to the reminder's time
# is the same appointment (a portal rounds, an owner types 2:30 for 2:25).
TWIN_MINUTES = 15

_last_refresh = {"at": 0.0, "status": None}

# ---------- parsing ----------

_APPT_RX = re.compile(
    r"\b(?:appointments?|appts?|visits?|check-?ups?|consultations?|consults?|"
    r"procedures?|exams?|cleanings?|follow-?ups?|surgery|screenings?|"
    r"physicals?|vaccinations?|sessions?)\b", re.I)
# A message that only invites the owner to book is not an appointment.
_SOLICIT_RX = re.compile(
    r"\b(?:book|schedule|request|make)\s+(?:an?|your)\s+(?:next\s+|new\s+|annual\s+)?"
    r"(?:appointment|appt|visit|exam|check-?up)", re.I)
_BOOKED_RX = re.compile(
    r"\b(?:scheduled|confirmed|booked|reminder|reminding|you have an?|upcoming|"
    r"see you|is (?:on|at|set|tomorrow|today)|rescheduled|cancel{1,2}ed)\b", re.I)
_CANCEL_RX = re.compile(
    r"\b(?:has been|have been|was|is|been)\s+cancel{1,2}ed\b|"
    r"\bcancel{1,2}ation (?:confirmed|notice)\b|\byou(?:'ve| have) cancel{1,2}ed\b|"
    r"\b(?:appointment|appt|visit)\s+cancel{1,2}ed\b", re.I)
_RESCHEDULE_RX = re.compile(
    r"\brescheduled\b|\bhas been (?:moved|changed)\b|\bnew (?:date|time) (?:is|for)\b", re.I)
_VIRTUAL_RX = re.compile(
    r"\b(?:video (?:visit|call|appointment)|virtual (?:visit|appointment)|"
    r"telehealth|telemedicine|tele-?visit|phone (?:visit|appointment|call)|"
    r"telephone (?:visit|appointment)|zoom|visit link|(?:join|joining) (?:link|instructions)|"
    r"instructions for joining|link to join)\b", re.I)

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_RX = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?", re.I)
_ISO_RX = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RELATIVE_RX = re.compile(r"\b(today|tonight|tomorrow)\b", re.I)
_WEEKDAY_RX = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)(?:day|s|sday|nesday|rsday|rs|r|urday)?\b\.?", re.I)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CLOCK_RX = re.compile(
    r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?|"
    r"(?<![\d:+-])(\d{1,2}):(\d{2})(?!\d|:\d|\s*[ap]\.?\s?m\b)|"
    r"\b(noon)\b", re.I)
_RANGE_GLUE_RX = re.compile(r"\s*(?:-|–|to|until)\s*$", re.I)
# "Please arrive by 2:15" / "check-in time: 2:15": a time right after an
# arrival phrase. "Arrive 15 minutes early for your 2:30" is not one.
_ARRIVAL_RX = re.compile(r"(?:arriv(?:e|al)|check[- ]?in)\b[^.\d]{0,16}$", re.I)
_DOCTOR_RX = re.compile(r"\b(?:Dr\.?|Doctor)\s+((?:[A-Z][A-Za-z'\-]+\.?\s?){1,3})")
# "with Jane Q. Roe, MD": a clinician named by credential instead of title.
_CREDENTIAL_RX = re.compile(
    r"\bwith\s+((?:[A-Z][A-Za-z'\-]*\.?\s){1,3}?[A-Z][A-Za-z'\-]+),?\s+"
    r"(MD|DO|NP|PA(?:-C)?|DDS|DMD|DPM|OD|LCSW|LMHC|PhD|PsyD|RN|FNP(?:-BC)?|PMHNP(?:-BC)?)\b")
# "Hi, it's Example Clinic." / "Example Clinic here." / "This is Example Clinic"
_SELF_NAMED_RX = re.compile(
    r"\b(?:it's|it is|this is)\s+((?:[A-Z][\w&'.-]*\s?){1,4})|"
    r"(?:^|[.!?,]\s*)((?:[A-Z][\w&'.-]*\s){1,4})here\b")
_DURATION_RX = re.compile(
    r"\b(?:last|lasts|take|takes|run|runs|for|duration:?|length:?)\s+"
    r"(?:up to\s+|about\s+|approximately\s+|around\s+)?(\d{1,3})\s*(min(?:ute)?s?|hours?|hrs?)\b", re.I)
_AT_ORG_RX = re.compile(r"\bat\s+((?:[A-Z][\w&'.-]*\s?){1,5})")
_LOCATION_RX = re.compile(r"\b(?:location|address|where)\s*:\s*([^\n]{3,120})", re.I)
_STREET_RX = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z0-9][\w.'-]*\s+){1,4}"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Pl|Place|Way|Dr|Drive|Ln|Lane|"
    r"Broadway|Pkwy|Parkway|Ct|Court|Sq|Square)\b\.?"
    r"(?:,?\s+(?:Suite|Ste\.?|Floor|Fl\.?|Unit|#)\s*[\w-]+)?")
_NOT_A_NAME = {"on", "at", "for", "with", "your", "the", "monday", "tuesday", "wednesday",
               "thursday", "friday", "saturday", "sunday", "today", "tomorrow"}


def _local_zone():
    return admininbox._zone()


def _received(when):
    """The message's received instant, aware; None when unreadable."""
    try:
        value = dt.datetime.fromisoformat((when or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.astimezone()


def _dates(text, base):
    """(position, end, date) for every date the text states, absolute first.

    A stated calendar date beats "tomorrow", which beats a bare weekday:
    "Tue, 11/3" is November 3 whatever weekday it falls on.
    """
    found = []
    for m in admininbox._NUMERIC_DATE_RX.finditer(text):
        month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
        year = int(year) + (2000 if len(year) == 2 else 0) if year else base.year
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        if not m.group(3) and date < base - dt.timedelta(days=31):
            date = date.replace(year=year + 1)
        found.append((0, m.start(), m.end(), date))
    for m in _ISO_RX.finditer(text):
        try:
            found.append((0, m.start(), m.end(), dt.date(*map(int, m.groups()))))
        except ValueError:
            pass
    for m in _MONTH_RX.finditer(text):
        month = _MONTHS[m.group(1)[:3].lower()]
        year = int(m.group(3)) if m.group(3) else base.year
        try:
            date = dt.date(year, month, int(m.group(2)))
        except ValueError:
            continue
        if not m.group(3) and date < base - dt.timedelta(days=31):
            date = date.replace(year=year + 1)
        found.append((0, m.start(), m.end(), date))
    for m in _RELATIVE_RX.finditer(text):
        extra = 1 if m.group(1).lower() == "tomorrow" else 0
        found.append((1, m.start(), m.end(), base + dt.timedelta(days=extra)))
    for m in _WEEKDAY_RX.finditer(text):
        target = _WEEKDAYS.index(m.group(1).lower()[:3])
        found.append((2, m.start(), m.end(), base + dt.timedelta(days=(target - base.weekday()) % 7)))
    found.sort(key=lambda row: (row[0], row[1]))
    return [(start, end, date) for _rank, start, end, date in found]


def _clocks(text):
    """(position, end, (hour, minute)) for every clock time in the text."""
    out = []
    for m in _CLOCK_RX.finditer(text):
        if m.group(6):
            out.append((m.start(), m.end(), (12, 0)))
        elif m.group(3):
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if 1 <= hour <= 12 and minute < 60:
                hour = hour % 12 + (12 if m.group(3).lower() == "p" else 0)
                out.append((m.start(), m.end(), (hour, minute)))
        else:
            hour, minute = int(m.group(4)), int(m.group(5))
            if hour < 24 and minute < 60:
                out.append((m.start(), m.end(), (hour, minute)))
    return out


def _pick_clock(text, clocks, after):
    """The appointment's start time, and its stated end when it is a range.

    An "arrive by 2:15" time yields to the visit's own time when the message
    states both; a start written as a range ("2:30 - 3:15 PM") gives the end.
    """
    # A bare "2:30" in a range borrows the meridiem of its partner.
    usable = [c for c in clocks if not _ARRIVAL_RX.search(text[max(0, c[0] - 30):c[0]])] or clocks
    later = [c for c in usable if c[0] >= after] or usable
    start = later[0]
    end = None
    following = [c for c in clocks if c[0] > start[1]]
    if following and _RANGE_GLUE_RX.search(text[start[1]:following[0][0]]) is not None \
            and len(text[start[1]:following[0][0]]) <= 8:
        end = following[0]
        if (start[2][0] < 12 and end[2][0] >= 12 and not re.search(r"[ap]\.?\s?m", text[start[0]:start[1]], re.I)
                and start[2][0] + 12 <= end[2][0]):
            start = (start[0], start[1], (start[2][0] + 12, start[2][1]))
    return start, end


def _person_name(raw):
    """ALEXANDRA ROE -> Alexandra Roe; a mixed-case name is kept as typed."""
    name = _clean_name(raw)
    return name.title() if name.isupper() else name


def _org_name(raw):
    """An organization ends at sentence punctuation: "Example Care. You" is "Example Care"."""
    return _clean_name(re.split(r"[.!?,;:](?:\s|$)", raw or "")[0])


def _clean_name(raw):
    words = raw.strip().rstrip(".,").split()
    while words and words[-1].lower().rstrip(".,") in _NOT_A_NAME:
        words.pop()
    return " ".join(words).rstrip(".,")


def _sender_label(sender):
    sender = (sender or "").strip()
    if not sender or sender.lower().startswith("short code") or re.fullmatch(r"[\d+() -]+", sender):
        return ""
    return sender


def parse(text, when, sender=""):
    """One message's appointment, or None. Deterministic; no model call.

    Returns kind ("booked" | "cancelled" | "rescheduled"), the local start
    and end (end None when unstated), the quote that states them, and what
    the message says about who and where.
    """
    text = text or ""
    received = _received(when)
    if not received or not _APPT_RX.search(text):
        return None
    cancelled = bool(_CANCEL_RX.search(text))
    if _SOLICIT_RX.search(text) and not _BOOKED_RX.search(text) and not cancelled:
        return None
    zone = _local_zone()
    base = received.astimezone(zone).date()
    dates = _dates(text, base)
    clocks = _clocks(text)
    if not dates or not clocks:
        return {"kind": "cancelled", "start": None} if cancelled else None
    d_start, d_end, date = dates[0]
    (c_start, c_end, (hour, minute)), stated_end = _pick_clock(text, clocks, d_start)
    naive = dt.datetime.combine(date, dt.time(hour, minute))
    start = naive.replace(tzinfo=zone) if zone else naive.astimezone()
    if start < received - dt.timedelta(hours=12) and not cancelled:
        return None   # "your visit on 10/2 at 3 PM is complete": spent, not upcoming
    end = None
    if stated_end:
        end_naive = dt.datetime.combine(date, dt.time(*stated_end[2]))
        end = end_naive.replace(tzinfo=zone) if zone else end_naive.astimezone()
        if end <= start:
            end = None
        else:
            c_end = max(c_end, stated_end[1])
    if end is None:
        m = _DURATION_RX.search(text)
        if m and int(m.group(1)) > 0:
            minutes = int(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1)
            if minutes <= 12 * 60:
                end = start + dt.timedelta(minutes=minutes)
    lo, hi = min(d_start, c_start), max(d_end, c_end)
    doctor = ""
    m = _DOCTOR_RX.search(text)
    if m and _clean_name(m.group(1)):
        doctor = "Dr. " + _person_name(m.group(1))
    else:
        m = _CREDENTIAL_RX.search(text)
        if m:
            doctor = f"{_person_name(m.group(1))}, {m.group(2)}"
    org = _sender_label(sender)
    if not org:
        m = _SELF_NAMED_RX.search(text)
        org = _org_name(m.group(1) or m.group(2)) if m else ""
    if not org:
        m = _AT_ORG_RX.search(text)
        org = _org_name(m.group(1)) if m else ""
    location = ""
    m = _LOCATION_RX.search(text)
    if m:
        location = m.group(1).strip().rstrip(".;,")
    else:
        m = _STREET_RX.search(text)
        location = m.group(0).strip() if m else ""
    return {
        "kind": "cancelled" if cancelled else
                ("rescheduled" if _RESCHEDULE_RX.search(text) else "booked"),
        "start": start, "end": end, "end_stated": end is not None,
        "quote": text[lo:hi].strip(),
        "doctor": doctor,
        "org": org, "location": location[:160],
        "virtual": bool(_VIRTUAL_RX.search(text)),
    }


def title_for(appt):
    who = appt.get("doctor") or ""
    org = appt.get("org") or ""
    label = "Video visit" if appt.get("virtual") else "Appointment"
    if who and org and org.lower() not in who.lower():
        return f"{label}: {who} ({org})"
    return f"{label}: {who or org}" if (who or org) else label


# ---------- settings ----------

def _minutes(key, low, high, fallback):
    value = settings.get(key)
    return value if type(value) is int and low <= value <= high else fallback


def default_minutes():
    return _minutes("appointment_minutes", 5, 480, 60)


def travel_minutes(appt):
    return 0 if appt.get("virtual") else _minutes("appointment_travel_minutes", 0, 240, 30)


# ---------- ledger ----------

def _empty():
    return {"appointments": {}, "seen": []}


def _read():
    state = jsonstore.read(STORE, None)
    if not isinstance(state, dict):
        return _empty()
    if not isinstance(state.get("appointments"), dict):
        state["appointments"] = {}
    if not isinstance(state.get("seen"), list):
        state["seen"] = []
    return state


def _id_for(start):
    stamp = start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return "appt_" + hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:16]


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _source(item, parsed):
    return {"key": item["key"], "channel": item["channel"], "handle": item.get("handle") or "",
            "sender": item.get("sender") or "", "subject": item.get("subject") or "",
            "when": item.get("when") or "", "kind": parsed["kind"],
            "quote": parsed.get("quote") or "", "text": item.get("text") or ""}


def _ts(value):
    return admininbox._ts(value)


def _from_handle(appt, handle):
    return any(s.get("handle") == handle for s in appt.get("sources", []))


def _apply(state, item, parsed):
    """Fold one message into the ledger. Messages arrive oldest first."""
    rows = state["appointments"]
    handle = item.get("handle") or ""
    received = item.get("when") or ""
    if parsed["start"] is None:
        # A cancellation that names no time retires the sender's one
        # upcoming appointment; with two or more it cannot say which.
        mine = [a for a in rows.values() if a["status"] == "upcoming" and _from_handle(a, handle)
                and _ts(a.get("last_seen")) <= _ts(received)]
        if len(mine) == 1:
            mine[0].update(status="cancelled", status_at=received)
            mine[0]["sources"].append(_source(item, parsed))
        return
    appt_id = _id_for(parsed["start"])
    row = rows.get(appt_id)
    if parsed["kind"] == "cancelled":
        if row and row["status"] == "upcoming":
            row.update(status="cancelled", status_at=received)
            row["sources"].append(_source(item, parsed))
        return
    if parsed["kind"] == "rescheduled":
        for other in rows.values():
            if (other["id"] != appt_id and other["status"] == "upcoming"
                    and _from_handle(other, handle) and _ts(other.get("last_seen")) <= _ts(received)
                    and _ts(other["start"]) > _now().timestamp()):
                other.update(status="superseded", status_at=received, superseded_by=appt_id)
    if row is None:
        row = {"id": appt_id, "start": parsed["start"].isoformat(), "end": "",
               "end_stated": False, "doctor": "", "org": "", "location": "",
               "virtual": False, "status": "upcoming", "status_at": "",
               "first_seen": received, "last_seen": received, "sources": []}
        rows[appt_id] = row
    elif row["status"] in ("cancelled", "superseded") and _ts(received) > _ts(row.get("status_at")):
        # A newer confirmation for the same minute re-arms it.
        row.update(status="upcoming", status_at=received)
    for field in ("doctor", "org", "location"):
        if parsed.get(field) and not row.get(field):
            row[field] = parsed[field]
    row["virtual"] = row["virtual"] or parsed["virtual"]
    if parsed["end"] and not row["end_stated"]:
        row.update(end=parsed["end"].isoformat(), end_stated=True)
    if _ts(received) >= _ts(row.get("last_seen")):
        row["last_seen"] = received
    row["sources"].append(_source(item, parsed))
    row["sources"] = row["sources"][-8:]   # a daily reminder for a month is one row, not 30 quotes


def _prune(state):
    cutoff = (_now() - dt.timedelta(days=KEEP_PAST_DAYS)).isoformat()
    for appt_id, row in list(state["appointments"].items()):
        try:
            past = dt.datetime.fromisoformat(row["start"]).astimezone(dt.timezone.utc).isoformat() < cutoff
        except (KeyError, TypeError, ValueError):
            past = True
        if past:
            del state["appointments"][appt_id]
    state["seen"] = state["seen"][-SEEN_KEEP:]


def scan(feed_items=None):
    """Read the admin inbox's messages and fold new ones into the ledger.

    Returns {"added": n, "status": {...}}. Every source the admin inbox
    reads is reported by name; a missing source is a status, not an error.
    """
    if settings.fixture_mode():
        return {"added": 0, "status": {"appointments": "fixture"}}
    got = admininbox.collect(feed_items, include_hidden=True)
    items = sorted((it for it in got["items"] if it.get("when")), key=lambda it: _ts(it["when"]))
    # Parsing happens outside the store lock; only the fold is locked.
    parsed = [(it, parse(it.get("text") or "", it.get("when"), it.get("sender") or ""))
              for it in items]
    parsed = [(it, p) for it, p in parsed if p]
    added = 0
    with locked(STORE):
        state = _read()
        seen = set(state["seen"])
        for it, p in parsed:
            if it["key"] in seen:
                continue
            before = len(state["appointments"])
            _apply(state, it, p)
            added += len(state["appointments"]) - before
            state["seen"].append(it["key"])
            seen.add(it["key"])
        _prune(state)
        state["scanned_at"] = _now().isoformat()
        jsonstore.write_atomic(STORE, state, indent=1, ensure_ascii=False)
    return {"added": added, "status": got["status"]}


def refresh(feed_items=None, force=False):
    """scan() at most every REFRESH_S seconds; the brief calls this."""
    if not force and time.monotonic() - _last_refresh["at"] < REFRESH_S and _last_refresh["at"]:
        return _last_refresh["status"]
    _last_refresh["at"] = time.monotonic()
    try:
        out = scan(feed_items)
        status = "ok" if all(v == "ok" for v in out["status"].values()) else \
            "; ".join(f"{k}: {v}" for k, v in out["status"].items() if v != "ok")
    except Exception as exc:  # noqa: BLE001 - the brief never fails on this
        status = f"scan failed: {str(exc)[:160]}"
    _last_refresh["status"] = status
    return status


def _window(row):
    start = dt.datetime.fromisoformat(row["start"])
    end = (dt.datetime.fromisoformat(row["end"]) if row.get("end")
           else start + dt.timedelta(minutes=default_minutes()))
    return start, end


def view(row):
    out = copy.deepcopy(row)
    start, end = _window(row)
    out["end"] = end.isoformat()
    out["title"] = title_for(row)
    out["travel_minutes"] = travel_minutes(row)
    for src in out.get("sources", []):
        src.pop("text", None)
    return out


def upcoming(include_closed=False):
    rows = _read()["appointments"].values()
    rows = [r for r in rows if include_closed or r.get("status") == "upcoming"]
    return [view(r) for r in sorted(rows, key=lambda r: r.get("start", ""))]


def current(appt_id):
    """The ledger row for an id, as viewed; None when unknown."""
    row = _read()["appointments"].get(appt_id)
    return view(row) if row else None


def dismiss(appt_id):
    """Owner says this is not an appointment (or not theirs to track)."""
    with locked(STORE):
        state = _read()
        row = state["appointments"].get(appt_id)
        if not row:
            raise ValueError("Appointment not found.")
        row.update(status="dismissed", status_at=_now().isoformat())
        jsonstore.write_atomic(STORE, state, indent=1, ensure_ascii=False)
    draft_id = row.get("calendar_draft")
    if draft_id:
        from . import calendarplan
        try:
            calendarplan.dismiss(draft_id)
        except ValueError:
            pass   # already on the calendar: the owner manages it there
    return view(row)


# ---------- calendar twins ----------

_GENERIC = {"appointment", "appointments", "video", "visit", "with", "health", "medical",
            "center", "clinic", "group", "the", "and", "for"}
_CAL_APPT_RX = re.compile(
    r"\b(?:dr|doctor|dentist|dental|derm\w*|clinic|medical|physio|physical therapy|"
    r"pt|ortho\w*|pediatric\w*|appt|appointment|checkup|check-up|visit|exam)\b", re.I)


def _words(value):
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", (value or "").lower()) if w not in _GENERIC}


def is_twin(appt, event):
    """Is this calendar event the same appointment? Close in time, and
    either the same title, a shared name, or an appointment-like title."""
    if event.get("all_day") or event.get("appointment_found"):
        return False
    try:
        ev_start = dt.datetime.fromisoformat(event["start"])
        start = dt.datetime.fromisoformat(appt["start"]).astimezone().replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        return False
    if ev_start.tzinfo is not None:
        ev_start = ev_start.astimezone().replace(tzinfo=None)
    if abs(ev_start - start) > dt.timedelta(minutes=TWIN_MINUTES):
        return False
    title = event.get("title") or ""
    if title.strip().lower() == title_for(appt).lower():
        return True
    names = _words(appt.get("doctor")) | _words(appt.get("org"))
    return bool(names & _words(title)) or bool(_CAL_APPT_RX.search(title))


def _local_naive(value):
    return dt.datetime.fromisoformat(value).astimezone().replace(tzinfo=None)


def _hm(value):
    return settings.strf(value, "%-I:%M %p")


def merge(events, lo, hi, appts=None):
    """Fold the ledger's appointments into brief-shaped calendar events.

    `lo`/`hi` are local naive datetimes (brief._day_bounds). An appointment
    already on a calendar annotates that event; one that is not becomes its
    own row (`appointment_found`). A calendar event that twins a cancelled
    or superseded appointment is tagged so the owner can delete it.
    Each in-person appointment gets a travel row before it and a busy
    window covering the buffer on both sides, for conflict flagging.
    """
    if appts is None:
        try:
            appts = upcoming(include_closed=True)
        except Exception:  # noqa: BLE001 - an unreadable ledger adds nothing
            appts = []
    out = list(events)
    for appt in appts:
        try:
            start, end = _local_naive(appt["start"]), _local_naive(appt["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not lo <= start < hi or appt.get("status") == "dismissed":
            continue
        twins = [e for e in out if is_twin(appt, e)]
        if appt.get("status") != "upcoming":
            for ev in twins:
                ev["appointment_status"] = appt["status"]
            continue
        buffer = dt.timedelta(minutes=appt.get("travel_minutes") or 0)
        src = (appt.get("sources") or [{}])[-1]
        info = {"appointment": True, "appointment_id": appt["id"],
                "busy_start": (start - buffer).isoformat(), "busy_end": (end + buffer).isoformat(),
                "leave_by_hm": _hm(start - buffer) if buffer else "",
                "travel_minutes": appt.get("travel_minutes") or 0,
                "found_in": f'{src.get("sender") or "a reminder"} ({src.get("channel", "")})',
                "found_quote": src.get("quote") or ""}
        if twins:
            twins[0].update(info)
            twins[0].setdefault("location", appt.get("location") or "")
        else:
            out.append({
                "title": appt["title"], "calendar": "Found in " + (
                    "a text" if src.get("channel") == "imessage" else "mail"),
                "family": False, "birthday": False, "all_day": False,
                "start": start.isoformat(), "end": end.isoformat(),
                "start_hm": _hm(start), "end_hm": _hm(end),
                "conflict": False, "remote": bool(appt.get("virtual")),
                "location": appt.get("location") or "",
                "end_stated": bool(appt.get("end_stated")),
                "appointment_found": True, **info})
        if buffer:
            out.append({
                "title": "Travel to " + appt["title"].split(": ", 1)[-1],
                "calendar": "Travel buffer", "family": False, "birthday": False,
                "all_day": False, "travel": True, "appointment_id": appt["id"],
                "start": (start - buffer).isoformat(), "end": start.isoformat(),
                "start_hm": _hm(start - buffer), "end_hm": _hm(start),
                "conflict": False, "remote": False})
    out.sort(key=lambda e: e.get("start") or "")
    return out


# ---------- promotion to the calendar ----------

def _on_calendar(appt):
    """Is the appointment already on a calendar the brief reads? Errors mean
    "cannot tell", which stages nothing (a duplicate is worse than a wait)."""
    from . import brief
    start = _local_naive(appt["start"])
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    events = brief._occurrences(day, day + dt.timedelta(days=1))
    if brief._cal_error:
        raise RuntimeError(brief._cal_error)
    today, _ = brief._day_bounds(0)
    offset = (day - today).days
    if offset in (0, 1):
        m365 = brief._m365_events()
        events = events + list(m365["today" if offset == 0 else "tomorrow"])
    return any(is_twin(appt, ev) for ev in events)


def tick():
    """One assistant pass: scan, then stage and (when allowed) write every
    future appointment that no calendar shows yet."""
    from . import calendarplan
    out = {"scanned": scan(), "staged": 0, "created": 0, "notes": []}
    now = _now()
    for appt in upcoming():
        if dt.datetime.fromisoformat(appt["start"]) <= now:
            continue
        draft_id = appt.get("calendar_draft")
        if draft_id:
            prior = calendarplan.get(draft_id)
            if prior and prior.get("status") in ("created", "dismissed", "creating", "uncertain"):
                continue
        try:
            if _on_calendar(appt):
                continue
        except (RuntimeError, OSError, ValueError) as exc:
            out["notes"].append(f"Calendar check unavailable: {exc}")
            break
        draft = calendarplan.plan_appointment(appt)
        out["staged"] += 1
        with locked(STORE):
            state = _read()
            row = state["appointments"].get(appt["id"])
            if row:
                row["calendar_draft"] = draft["id"]
                jsonstore.write_atomic(STORE, state, indent=1, ensure_ascii=False)
        # A blocked draft (switch off, no destination) is retried hourly,
        # not every pass: each attempt rewrites the calendar ledger.
        if draft.get("status") == "suggested" or (
                draft.get("status") == "blocked"
                and now.timestamp() - _ts(draft.get("updated_at")) >= 3600):
            draft = calendarplan.create_owner_event(draft["id"])
        if draft.get("status") == "created":
            out["created"] += 1
        elif draft.get("reason"):
            out["notes"].append(f'{appt["title"]}: {draft["reason"]}')
    return out


def check_current(appt_id, start, end):
    """Why a staged calendar draft no longer matches its appointment, or ""."""
    row = current(appt_id) if appt_id else None
    if not row:
        return "The appointment this entry came from is no longer in the ledger."
    if row["status"] != "upcoming":
        return f"The appointment was {row['status']}; it stays off the calendar."
    try:
        same = (dt.datetime.fromisoformat(row["start"]) == dt.datetime.fromisoformat(start)
                and dt.datetime.fromisoformat(row["end"]) == dt.datetime.fromisoformat(end))
    except (TypeError, ValueError):
        same = False
    return "" if same else "The appointment's time changed; review the new entry."

"""Childcare coverage: notice a pickup nobody is covering, block it, ask once.

THE ROUTINE IS THE OWNER'S. Each child has a pickup time, the school days
it applies to, who normally covers each of those days, and optionally the
school's calendar (a Calendar.app calendar name or an ICS feed URL). The
caregivers are named people with optional message handles. All of it is
owner-entered in the Childcare window and lives in data/coverage.json; the
product never assumes a child, a time, or a caregiver.

THREE THINGS OPEN A GAP:

  - a cancellation in iMessage. A sentence that names a caregiver (or is
    sent by one) and says they can't come, can't make it, are out sick, or
    need to cancel. Its date is the date the sentence names; with none, the
    first day in the next week that caregiver still has a pickup to do
    after the message arrived. Only the children that caregiver normally
    covers that day are in the gap.
  - a half day on a school calendar. The pickup moves to the time the
    event states (or the timed event's end), and whoever usually covers is
    asked to confirm, because the routine was built around the usual time.
  - a closure on a school calendar on a day nobody in the routine covers.

WHAT A GAP DOES, ONCE. It puts a timed pickup block on the family calendar
(server/calendarplan.py's coverage path; the time is the routine's or the
school's, never a guess), puts one "who's taking it?" card in the Decide
queue (server/reviewqueue.py), and texts the owner once when pings are on.
A gap's id is its date, cause and children, so a second "the sitter can't
come" for the same day never asks again. Answering records who is taking
it; "not needed" withdraws any block not yet written. No path edits or
deletes an event already on the calendar.

Deterministic throughout: regular expressions over message text and event
titles, no model call. ICS feeds are read without recurrence expansion;
schools publish closures and half days as single events.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
import urllib.request

from . import jsonstore, settings

STORE = settings.ROOT / "data" / "coverage.json"
UTC = dt.timezone.utc
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHOOL_DAYS = DAYS[:5]
ME = "me"
NOT_NEEDED = "not needed"
# Owner-entered lists, capped so a pasted blob cannot grow the store or the
# question card without bound; a household has a handful of each.
MAX_KIDS = 8
MAX_CAREGIVERS = 12
# Message ids already read, for de-duplication when the watcher re-delivers.
SEEN_KEEP = 500
# A school feed is re-read at most this often; closures are published weeks
# ahead, and a failed read is retried on the next pass rather than cached.
ICS_REFRESH_S = 6 * 3600
ICS_TIMEOUT_S = 20
# One feed body; a district calendar for a year is well under a megabyte.
ICS_MAX_BYTES = 4 * 1024 * 1024
# How far ahead an undated cancellation can reach.
CANCEL_REACH_DAYS = 7

_CANCEL = re.compile(
    r"\b(?:can(?:no|')?t|can not|won'?t|will not|unable to|not able to|isn'?t able to|"
    r"won'?t be able to|not going to be able to)\s+"
    r"(?:come|make it|do (?:the )?pick ?-?up|pick (?:\w+ )?up|get (?:the kids|them|her|him)|cover)\b"
    r"|\b(?:not coming|out sick|is sick|am sick|i'?m sick|"
    r"(?:have|has|need|needs) to cancel|cancell?(?:ing|ed)\b|calling out)", re.I)
_HALF_DAY = re.compile(
    r"\b(?:half[- ]?day|early (?:dismissal|release|closing)|noon dismissal|"
    r"minimum day|shortened day|early pick ?-?up)\b", re.I)
_CLOSURE = re.compile(
    r"\b(?:no school|school(?:s)? closed|closed|closure|holiday|recess|break|"
    r"vacation|day off|no classes|staff development|professional development|"
    r"teacher (?:work|planning) day|clerical day|in-?service)\b", re.I)


def _now():
    return dt.datetime.now(UTC)


def _stamp():
    return _now().isoformat()


def _cfg(key, default=None):
    """A setting with the schema's default; `default` covers a key read
    before the table knew it (never in practice, kept for tests)."""
    try:
        return settings.get(key)
    except KeyError:
        return default


def enabled():
    return _cfg("coverage_enabled", False) is True


def _zone():
    from . import calendarplan
    try:
        return calendarplan._zone("")
    except ValueError:
        return None


def _local(instant):
    zone = _zone()
    return instant.astimezone(zone) if zone else instant.astimezone()


def _at(date, hhmm):
    """A local wall-clock time on a date, as an aware datetime."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    naive = dt.datetime.combine(date, dt.time(hour, minute))
    zone = _zone()
    return naive.replace(tzinfo=zone) if zone else naive.astimezone()


def _clock_label(hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    suffix = "am" if hour < 12 else "pm"
    return f"{hour % 12 or 12}:{minute:02d}{suffix}"


# ------------------------------------------------------------------ store

def _empty():
    # Built fresh each call: a shared template would let nested buckets
    # accumulate across passes.
    return {"routine": {"kids": [], "caregivers": [], "day_start": "08:00"},
            "gaps": {}, "seen": [], "feeds": {}, "school_errors": [], "checked_at": ""}


def _read():
    state = jsonstore.read(STORE, _empty())
    if not isinstance(state, dict):
        raise ValueError("Coverage store is invalid; restore data/coverage.json.")
    base = _empty()
    base.update({k: v for k, v in state.items() if k in base})
    return base


def _mutate(fn):
    def apply(state):
        full = _empty()
        if isinstance(state, dict):
            full.update({k: v for k, v in state.items() if k in full})
        fn(full)
        return full
    return jsonstore.mutate(STORE, apply, _empty(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- routine

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _hhmm(value, field):
    value = str(value or "").strip()
    match = _HHMM.match(value)
    if not match:
        raise ValueError(f"{field} must be a 24-hour time such as 14:55.")
    return f"{int(match[1]):02d}:{match[2]}"


def _name(value, field):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value or len(value) > 60 or "\x00" in value:
        raise ValueError(f"{field} needs a name of 1-60 characters.")
    return value


def validate_routine(raw):
    """The one shape the routine is saved in; every field is checked."""
    if not isinstance(raw, dict):
        raise ValueError("The routine must be an object.")
    caregivers, names = [], set()
    for row in raw.get("caregivers") or []:
        if not isinstance(row, dict):
            raise ValueError("Each caregiver must be an object.")
        name = _name(row.get("name"), "Caregiver")
        if name.lower() in names or name.lower() in (ME, NOT_NEEDED):
            raise ValueError(f"Caregiver {name!r} is listed twice or uses a reserved word.")
        names.add(name.lower())
        handles = row.get("handles") or []
        if isinstance(handles, str):
            handles = [h for h in re.split(r"[,\s]+", handles) if h]
        if not isinstance(handles, list) or any(not isinstance(h, str) for h in handles):
            raise ValueError(f"{name}'s handles must be phone numbers or addresses.")
        caregivers.append({"name": name, "handles": [h.strip() for h in handles if h.strip()][:6]})
    if len(caregivers) > MAX_CAREGIVERS:
        raise ValueError(f"At most {MAX_CAREGIVERS} caregivers.")
    known = {c["name"].lower(): c["name"] for c in caregivers}
    kids, kid_names = [], set()
    for row in raw.get("kids") or []:
        if not isinstance(row, dict):
            raise ValueError("Each child must be an object.")
        name = _name(row.get("name"), "Child")
        if name.lower() in kid_names:
            raise ValueError(f"{name} is listed twice.")
        kid_names.add(name.lower())
        days = row.get("days") or list(SCHOOL_DAYS)
        if not isinstance(days, list) or any(d not in DAYS for d in days):
            raise ValueError(f"{name}'s school days must be from {', '.join(DAYS)}.")
        cover = {}
        for day, who in (row.get("cover") or {}).items():
            if day not in DAYS:
                raise ValueError(f"{name}'s cover has an unknown day {day!r}.")
            who = str(who or "").strip()
            if who and who.lower() != ME and who.lower() not in known:
                raise ValueError(f"{name}'s {day} cover {who!r} is not a listed caregiver.")
            cover[day] = ME if who.lower() == ME else known.get(who.lower(), "")
        minutes = row.get("minutes", None)
        if minutes in (None, ""):
            minutes = None
        elif type(minutes) is not int or not 5 <= minutes <= 180:
            raise ValueError(f"{name}'s pickup block must be 5-180 minutes.")
        calendar = str(row.get("school_calendar") or "").strip()[:120]
        ics = str(row.get("school_ics") or "").strip()
        if ics and not ics.lower().startswith(("https://", "http://", "webcal://")):
            raise ValueError(f"{name}'s school feed must be an https or webcal URL.")
        kids.append({"name": name, "pickup": _hhmm(row.get("pickup"), f"{name}'s pickup"),
                     "place": str(row.get("place") or "").strip()[:120],
                     "days": [d for d in DAYS if d in days],
                     "cover": {d: cover.get(d, "") for d in DAYS if d in days},
                     "minutes": minutes, "school_calendar": calendar, "school_ics": ics[:500]})
    if len(kids) > MAX_KIDS:
        raise ValueError(f"At most {MAX_KIDS} children.")
    return {"kids": kids, "caregivers": caregivers,
            "day_start": _hhmm(raw.get("day_start") or "08:00", "Day start")}


def routine():
    return _read()["routine"]


def save_routine(raw):
    clean = validate_routine(raw)
    _mutate(lambda state: state.update(routine=clean))
    return clean


def _block_minutes(kid):
    return kid.get("minutes") or int(_cfg("coverage_block_minutes", 30))


# --------------------------------------------------------------- the plan

def _kids_on(rt, date):
    day = DAYS[date.weekday()]
    return [k for k in rt["kids"] if day in k["days"]]


def _usual(kid, date):
    return kid["cover"].get(DAYS[date.weekday()], "")


def _pickup(kid, date, hhmm=None, time_from="routine", start_hhmm=None):
    hhmm = hhmm or kid["pickup"]
    if start_hhmm:
        start = _at(date, start_hhmm)
        end = _at(date, hhmm) + dt.timedelta(minutes=_block_minutes(kid))
    else:
        start = _at(date, hhmm)
        end = start + dt.timedelta(minutes=_block_minutes(kid))
    return {"kid": kid["name"], "time": hhmm, "start": start.isoformat(),
            "end": end.isoformat(), "usual": _usual(kid, date), "time_from": time_from,
            "place": kid.get("place", "")}


def _gap_id(date, cause, kids, who=""):
    key = "|".join([date.isoformat(), cause, who.lower(), ",".join(sorted(k.lower() for k in kids))])
    return "gap_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _open_gap(state, date, cause, pickups, reason, source, who=""):
    """Record a gap once. An existing gap with the same id is left alone,
    which is what makes the question get asked only once."""
    gid = _gap_id(date, cause, [p["kid"] for p in pickups], who)
    if gid in state["gaps"]:
        return None
    state["gaps"][gid] = {
        "id": gid, "date": date.isoformat(), "cause": cause, "who": who,
        "pickups": pickups, "reason": reason, "source": source,
        "status": "open", "taker": "", "answered_at": "", "asked_at": "",
        "notified": False, "calendar": {}, "calendar_note": "",
        "created_at": _stamp()}
    return gid


# ----------------------------------------------------------- cancellations

def _sentences(text):
    return [m.group(0).strip() for m in re.finditer(r"[^.!?\n]+[.!?]*", text or "") if m.group(0).strip()]


def _caregiver_for(sentence, item, caregivers):
    """The caregiver a cancellation is about: one named in the sentence, else
    the sender when the sender is a caregiver speaking for themself."""
    named = [c for c in caregivers
             if re.search(r"(?<!\w)" + re.escape(c["name"]) + r"(?!\w)", sentence, re.I)]
    if len(named) == 1:
        return named[0]
    if named:
        return None
    handle = str(item.get("handle") or "").strip().lower()
    sender = str(item.get("person_name") or "").strip().lower()
    for c in caregivers:
        if handle and handle in {h.lower() for h in c["handles"]}:
            return c
        first = sender.split(" ")[0] if sender else ""
        if first and first == c["name"].lower().split(" ")[0]:
            return c
    return None


def _cancel_dates(sentence, when, rt, caregiver):
    from . import calendarplan
    zone = _zone()
    try:
        named = calendarplan._date_mentions(sentence, when.isoformat(), zone)
    except (ValueError, TypeError):
        named = []
    if named:
        return [dt.date.fromisoformat(d) for d in dict.fromkeys(named)]
    local = _local(when)
    for offset in range(CANCEL_REACH_DAYS + 1):
        date = local.date() + dt.timedelta(days=offset)
        for kid in _kids_on(rt, date):
            if (_usual(kid, date) or "").lower() == caregiver["name"].lower() \
                    and _at(date, kid["pickup"]) > when:
                return [date]
    return []


def detect_cancellations(items, rt=None, today=None):
    """Gap proposals from message items (the iMessage watcher's shape)."""
    rt = rt or routine()
    if not rt["kids"] or not rt["caregivers"]:
        return []
    today = today or _local(_now()).date()
    found = []
    for item in items:
        text = str(item.get("text") or "")
        if not text or not _CANCEL.search(text):
            continue
        try:
            when = dt.datetime.fromisoformat(str(item.get("when") or "").replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError
        except ValueError:
            when = _now()
        for sentence in _sentences(text):
            # A question ("Can't the sitter come?") is not a cancellation.
            if sentence.endswith("?") or not _CANCEL.search(sentence):
                continue
            caregiver = _caregiver_for(sentence, item, rt["caregivers"])
            if not caregiver:
                continue
            for date in _cancel_dates(sentence, when, rt, caregiver):
                if date < today:
                    continue
                kids = [k for k in _kids_on(rt, date)
                        if (_usual(k, date) or "").lower() == caregiver["name"].lower()]
                if not kids:
                    continue
                found.append({
                    "date": date, "cause": "cancellation", "who": caregiver["name"],
                    "pickups": [_pickup(k, date) for k in kids],
                    "reason": f"{caregiver['name']} can't cover",
                    "source": {"channel": item.get("channel") or "imessage",
                               "id": str(item.get("rowid") or item.get("id") or ""),
                               "when": when.isoformat(), "text": sentence,
                               "from": item.get("person_name") or item.get("handle") or ""}})
            break
    return found


def consume(items):
    """The iMessage watcher's hook: new inbound items, read once each."""
    if not enabled() or not items:
        return []
    opened = []
    def record(state):
        seen = set(state["seen"])
        fresh = [i for i in items if str(i.get("rowid") or "") not in seen]
        for gap in detect_cancellations(fresh, state["routine"]):
            gid = _open_gap(state, gap["date"], gap["cause"], gap["pickups"],
                            gap["reason"], gap["source"], gap["who"])
            if gid:
                opened.append(gid)
        state["seen"] = (state["seen"] + [str(i.get("rowid")) for i in fresh if i.get("rowid")])[-SEEN_KEEP:]
    _mutate(record)
    return opened


# ---------------------------------------------------------- school events

def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_time(value, params):
    """An ICS DTSTART/DTEND value: (datetime or date, is_all_day)."""
    value = value.strip()
    if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date(), True
    stamp = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return stamp.replace(tzinfo=UTC), False
    tzid = re.search(r"TZID=([^;:]+)", params)
    if tzid:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return stamp.replace(tzinfo=ZoneInfo(tzid[1].strip('"'))), False
        except (ZoneInfoNotFoundError, ValueError):
            pass
    zone = _zone()
    return (stamp.replace(tzinfo=zone) if zone else stamp.astimezone()), False


def parse_ics(text):
    """Single VEVENTs from an ICS body: uid, summary, start, end, all_day."""
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(text or ""), re.S):
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            fields.setdefault(name.upper(), (value, params))
        if "DTSTART" not in fields or "SUMMARY" not in fields:
            continue
        try:
            start, all_day = _ics_time(*fields["DTSTART"])
            end = _ics_time(*fields["DTEND"])[0] if "DTEND" in fields else None
        except ValueError:
            continue
        summary = fields["SUMMARY"][0].replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        description = fields.get("DESCRIPTION", ("", ""))[0].replace("\\n", " ").replace("\\,", ",")
        events.append({"uid": fields.get("UID", ("", ""))[0].strip() or summary,
                       "summary": summary, "description": description[:400],
                       "start": start, "end": end, "all_day": all_day})
    return events


def _fetch_ics(url):
    url = "https://" + url[len("webcal://"):] if url.lower().startswith("webcal://") else url
    request = urllib.request.Request(url, headers={"User-Agent": "Vira coverage watcher"})
    with urllib.request.urlopen(request, timeout=ICS_TIMEOUT_S) as response:
        body = response.read(ICS_MAX_BYTES + 1)
    if len(body) > ICS_MAX_BYTES:
        raise ValueError("school feed is larger than 4 MB")
    return body.decode("utf-8", errors="replace")


def _calendar_events(name, start, end):
    """Occurrences on one named Calendar.app calendar (read-only)."""
    from . import brief
    con = brief._cal_connect()
    try:
        rows = con.execute(
            """SELECT ci.ROWID, ci.summary, ci.description, ci.all_day,
                      COALESCE(oc.occurrence_start_date, oc.occurrence_date),
                      COALESCE(oc.occurrence_end_date, oc.occurrence_date)
               FROM OccurrenceCache oc
               JOIN CalendarItem ci ON oc.event_id = ci.ROWID
               JOIN Calendar c ON ci.calendar_id = c.ROWID
               WHERE c.title = ?
                 AND COALESCE(oc.occurrence_start_date, oc.occurrence_date) >= ?
                 AND COALESCE(oc.occurrence_start_date, oc.occurrence_date) < ?""",
            (name, start.timestamp() - brief.APPLE_EPOCH - 86400,
             end.timestamp() - brief.APPLE_EPOCH)).fetchall()
    finally:
        con.close()
    out = []
    for rowid, summary, description, all_day, low, high in rows:
        if not summary or not isinstance(low, (int, float)):
            continue
        first = dt.datetime.fromtimestamp(low + brief.APPLE_EPOCH, UTC)
        last = dt.datetime.fromtimestamp((high or low) + brief.APPLE_EPOCH, UTC)
        out.append({"uid": f"{rowid}@{int(low)}", "summary": summary,
                    "description": (description or "")[:400],
                    "start": _local(first).date() if all_day else first,
                    "end": None if all_day else last, "all_day": bool(all_day)})
    return out


def _school_events(state, kid, start, end):
    """A child's school events in the window, and any read error by name."""
    events, errors = [], []
    if kid.get("school_calendar"):
        if not settings.IS_MAC or settings.fixture_mode():
            errors.append(f"{kid['name']}: Calendar.app school calendars need the connected Mac.")
        else:
            try:
                events += [dict(e, via="calendar") for e in _calendar_events(kid["school_calendar"], start, end)]
            except Exception as exc:  # noqa: BLE001 - named, never fatal
                errors.append(f"{kid['name']}: school calendar unreadable ({type(exc).__name__}).")
    url = kid.get("school_ics")
    if url:
        feed = state["feeds"].get(url) or {}
        fresh = feed.get("fetched_at") and (
            _now() - dt.datetime.fromisoformat(feed["fetched_at"])).total_seconds() < ICS_REFRESH_S
        if not fresh:
            try:
                body = _fetch_ics(url)
                feed = {"fetched_at": _stamp(), "error": "",
                        "events": [_event_json(e) for e in parse_ics(body)]}
            except Exception as exc:  # noqa: BLE001 - a failed read is not cached as a fact
                feed = dict(feed, error=f"{type(exc).__name__}: {str(exc)[:120]}", tried_at=_stamp())
            state["feeds"][url] = feed
        if feed.get("error"):
            errors.append(f"{kid['name']}: school feed unreadable ({feed['error']}).")
        events += [dict(_event_load(e), via="ics") for e in feed.get("events") or []]
    return events, errors


def _event_json(event):
    out = dict(event)
    for key in ("start", "end"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


def _event_load(event):
    out = dict(event)
    for key in ("start", "end"):
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = (dt.date.fromisoformat(value) if len(value) == 10
                        else dt.datetime.fromisoformat(value))
    return out


def _event_date(event):
    start = event["start"]
    return start if isinstance(start, dt.date) and not isinstance(start, dt.datetime) else _local(start).date()


def _names_other_kid(event, kid, kids):
    """An event that names only other children is not this child's."""
    text = event["summary"] + " " + event.get("description", "")
    named = [k for k in kids if re.search(r"(?<!\w)" + re.escape(k["name"]) + r"(?!\w)", text, re.I)]
    return bool(named) and kid not in named


def _half_day_time(event, kid):
    from . import calendarplan
    clocks = calendarplan._clock_mentions(event["summary"] + " " + event.get("description", ""))
    if clocks:
        hour, minute = clocks[-1] if len(clocks) > 1 else clocks[0]
        return f"{hour:02d}:{minute:02d}"
    if not event["all_day"] and isinstance(event.get("end"), dt.datetime):
        local = _local(event["end"])
        return f"{local.hour:02d}:{local.minute:02d}"
    return ""


def school_gaps(state, today=None):
    """Gap proposals from every child's school calendar, plus read errors."""
    rt = state["routine"]
    today = today or _local(_now()).date()
    horizon = today + dt.timedelta(days=int(_cfg("coverage_lookahead_days", 14)))
    start = _at(today, "00:00")
    end = _at(horizon, "23:59")
    by_day, errors = {}, []
    for kid in rt["kids"]:
        events, problems = _school_events(state, kid, start, end)
        errors += problems
        for event in events:
            date = _event_date(event)
            if not today <= date <= horizon or DAYS[date.weekday()] not in kid["days"]:
                continue
            if _names_other_kid(event, kid, rt["kids"]):
                continue
            text = event["summary"] + " " + event.get("description", "")
            source = {"channel": "school-calendar", "id": str(event["uid"]),
                      "when": start.isoformat(), "text": event["summary"],
                      "from": kid.get("school_calendar") or "school feed"}
            if _HALF_DAY.search(text):
                hhmm = _half_day_time(event, kid)
                if hhmm and hhmm >= kid["pickup"]:
                    continue
                pickup = _pickup(kid, date, hhmm, "school") if hhmm else dict(
                    _pickup(kid, date), start="", end="", time="", time_from="unknown")
                by_day.setdefault((date, "half_day"), ([], source))[0].append(pickup)
            elif _CLOSURE.search(text) and not _usual(kid, date):
                pickup = _pickup(kid, date, start_hhmm=rt.get("day_start") or "08:00")
                by_day.setdefault((date, "closure"), ([], source))[0].append(pickup)
    gaps = []
    for (date, cause), (pickups, source) in sorted(by_day.items(), key=lambda kv: kv[0]):
        names = " and ".join(p["kid"] for p in pickups)
        reason = (f"Half day: {source['text']}" if cause == "half_day"
                  else f"School closed and nobody is covering {names}: {source['text']}")
        gaps.append({"date": date, "cause": cause, "who": "", "pickups": pickups,
                     "reason": reason, "source": source})
    return gaps, errors


# ------------------------------------------------------------- settle gaps

def _title(gap, pickup):
    label = _clock_label(pickup["time"]) if pickup.get("time") else ""
    if gap["cause"] == "closure":
        return f"Childcare: {pickup['kid']} (school closed)"
    why = {"cancellation": f"{gap['who']} can't come", "half_day": "half day"}.get(gap["cause"], "")
    return f"Pickup: {pickup['kid']} {label}" + (f" ({why})" if why else "")


def _plan_blocks(gap):
    """Stage and, when allowed, write one calendar block per pickup."""
    from . import calendarplan
    notes = []
    for pickup in gap["pickups"]:
        if not pickup.get("start"):
            notes.append(f"{pickup['kid']}: the school did not give a dismissal time, so no block was placed.")
            continue
        description = (f"{gap['reason']}. Vira asked who is taking it; answer in Attention > Decide."
                       + (f" Usually: {pickup['usual']}." if pickup.get("usual") else "")
                       + (f" Place: {pickup['place']}." if pickup.get("place") else ""))
        source = {"id": gap["source"]["id"] or gap["id"], "channel": gap["source"]["channel"],
                  "when": gap["source"]["when"], "text": gap["source"]["text"]}
        draft = calendarplan.plan_coverage(gap["id"], pickup, source, _title(gap, pickup), description)
        if draft.get("status") in ("suggested", "blocked"):
            draft = calendarplan.create_owner_event(draft["id"])
        gap["calendar"][pickup["kid"]] = draft["id"]
        if draft.get("status") != "created" and draft.get("reason"):
            notes.append(f"{pickup['kid']}: {draft['reason']}")
    gap["calendar_note"] = " ".join(notes)


def _question(gap):
    day = settings.strf(dt.date.fromisoformat(gap["date"]), "%A %b %-d")
    if gap["cause"] == "closure":
        kids = " and ".join(p["kid"] for p in gap["pickups"])
        return f"Who has {kids} on {day}? {gap['reason']}."
    kids = " and ".join(
        f"{p['kid']}" + (f" at {_clock_label(p['time'])}" if p.get("time") else "") for p in gap["pickups"])
    return f"Who's taking {kids} on {day}? {gap['reason']}."


def _ask(gap):
    from . import notify
    if gap["notified"]:
        return
    gap["asked_at"] = _stamp()
    gap["notified"] = True
    try:
        notify.agent_ping("Childcare: " + _question(gap) + " Answer in Vira > Attention.",
                          key="coverage:" + gap["id"])
    except Exception:  # noqa: BLE001 - the Decide card still carries the question
        pass


def tick(now=None):
    """One pass: read school calendars, open gaps, block and ask once."""
    if not enabled():
        return {"enabled": False}
    today = _local(now or _now()).date()
    snapshot = _read()
    # The network read happens outside the store lock (jsonstore rule).
    gaps, errors = school_gaps(snapshot, today)
    def record(state):
        state["feeds"] = snapshot["feeds"]
        for gap in gaps:
            _open_gap(state, gap["date"], gap["cause"], gap["pickups"], gap["reason"], gap["source"])
        for gap in state["gaps"].values():
            if gap["status"] == "open" and gap["date"] < today.isoformat():
                gap["status"] = "past"
        state["school_errors"] = errors
        state["checked_at"] = _stamp()
    _mutate(record)
    pending = [g for g in _read()["gaps"].values()
               if g["status"] == "open" and (not g["calendar"] or not g["notified"])]
    for gap in pending:
        work = copy.deepcopy(gap)
        if not work["calendar"]:
            try:
                _plan_blocks(work)
            except Exception as exc:  # noqa: BLE001 - the question still goes out
                work["calendar_note"] = f"Calendar block needs attention ({type(exc).__name__})."
                work["calendar"] = {"_error": ""}
        _ask(work)
        def save(state, work=work):
            current = state["gaps"].get(work["id"])
            if current and current["status"] == "open":
                current.update({k: work[k] for k in ("calendar", "calendar_note", "notified", "asked_at")})
        _mutate(save)
    return {"enabled": True, "opened": len(gaps), "errors": errors}


# ------------------------------------------------------------------ answers

def options(gap, rt=None):
    """The answers a gap offers: me, each caregiver except the one who
    cancelled, and not needed."""
    rt = rt or routine()
    names = [c["name"] for c in rt["caregivers"]
             if gap.get("cause") != "cancellation" or c["name"].lower() != (gap.get("who") or "").lower()]
    return [ME] + names + [NOT_NEEDED]


def answer(gap_id, taker):
    from . import calendarplan
    taker = str(taker or "").strip()
    state = _read()
    gap = state["gaps"].get(gap_id)
    if not gap:
        raise KeyError(f"no coverage gap {gap_id}")
    if gap["status"] != "open":
        raise ValueError("This gap was already answered.")
    allowed = {o.lower(): o for o in options(gap, state["routine"])}
    if taker.lower() not in allowed:
        raise ValueError(f"Choose one of: {', '.join(allowed.values())}.")
    taker = allowed[taker.lower()]
    notes = []
    if taker == NOT_NEEDED:
        for draft_id in gap["calendar"].values():
            draft = calendarplan.get(draft_id) if draft_id else None
            if not draft:
                continue
            if draft["status"] == "created":
                notes.append("A block is already on the calendar; remove it in Calendar.app if you no longer want it.")
            elif draft["status"] in ("suggested", "blocked"):
                calendarplan.dismiss(draft_id)
    def save(state):
        current = state["gaps"].get(gap_id)
        if not current or current["status"] != "open":
            raise ValueError("This gap was already answered.")
        current.update(status="dismissed" if taker == NOT_NEEDED else "answered",
                       taker="" if taker == NOT_NEEDED else taker, answered_at=_stamp())
    _mutate(save)
    out = copy.deepcopy(_read()["gaps"][gap_id])
    out["note"] = " ".join(notes)
    return out


# ------------------------------------------------------------------ views

def gaps(include_closed=False):
    state = _read()
    rows = [copy.deepcopy(g) for g in state["gaps"].values()
            if include_closed or g["status"] == "open"]
    for row in rows:
        row["question"] = _question(row)
        row["options"] = options(row, state["routine"])
    return sorted(rows, key=lambda g: (g["date"], g["created_at"]))


def week(days=None, today=None):
    """Who covers each pickup over the coming days, with gaps folded in."""
    state = _read()
    rt = state["routine"]
    today = today or _local(_now()).date()
    days = days or int(_cfg("coverage_lookahead_days", 14))
    by_kid_day = {}
    for gap in state["gaps"].values():
        if gap["status"] in ("open", "answered", "dismissed"):
            for p in gap["pickups"]:
                by_kid_day[(gap["date"], p["kid"])] = (gap, p)
    out = []
    for offset in range(days):
        date = today + dt.timedelta(days=offset)
        rows = []
        for kid in _kids_on(rt, date):
            gap, pickup = by_kid_day.get((date.isoformat(), kid["name"]), (None, None))
            who = _usual(kid, date)
            row = {"kid": kid["name"], "time": kid["pickup"], "who": who, "state": "covered" if who else "nobody"}
            if gap:
                row.update(time=pickup.get("time") or "", gap=gap["id"], cause=gap["cause"])
                if gap["status"] == "open":
                    row["state"] = "gap"
                elif gap["status"] == "answered":
                    row.update(state="covered", who=gap["taker"])
                else:
                    row["state"] = "not needed"
            rows.append(row)
        if rows:
            out.append({"date": date.isoformat(), "day": DAYS[date.weekday()], "pickups": rows})
    return out


def status():
    state = _read()
    return {"enabled": enabled(),
            "blocks": _cfg("coverage_calendar_blocks", True) is True,
            "assistant": _cfg("assistant_enabled", False) is True,
            "routine": state["routine"], "gaps": gaps(), "week": week(),
            "checked_at": state.get("checked_at", ""),
            "school_errors": state.get("school_errors", []),
            "me": ME, "not_needed": NOT_NEEDED}

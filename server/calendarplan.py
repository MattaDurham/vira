"""Evidence-linked calendar suggestions and tightly scoped personal blocks.

Source-only drafts never talk to a calendar. The only writer creates an
attendee-free Calendar.app event on the selected or verified current-default calendar. Work blocks
for grounded commitments choose a configured duration and a free working-hours
slot, explicitly labeled as the assistant's choice. Direct scheduling requests
need their complete time range in the source. No path sends invitations,
accepts invitations, or edits an existing event.
A durable claim precedes the OS call: an interrupted or ambiguous write is
reported for review, never automatically replayed.
"""
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import jsonstore, settings
from .calinvite import _ical_fold
from .filelock import locked

STORE = settings.ROOT / "data" / "calendar-plans.json"
UTC = dt.timezone.utc
CLOSED = {"created", "dismissed"}
STATUSES = CLOSED | {"suggested", "blocked", "creating", "uncertain"}
_destination_cache = {"at": 0, "value": None}
_destination_lock = threading.Lock()

# Some Calendar versions advertise calendarIdentifier but fail when it is
# read. The object specifier still carries the native identifier; parse its
# documented display representation as data, never evaluate it as code.
_CALENDAR_ID_SCRIPT = r'''
function calendarId(calendar) {
  try {
    const display = Automation.getDisplayString(calendar);
    const match = display.match(/\.calendars\.byId\(("(?:[^"\\]|\\.)*")\)$/);
    if (match) return JSON.parse(match[1]);
  } catch (error) {}
  try { const id = calendar.calendarIdentifier(); if (typeof id === 'string') return id; } catch (error) {}
  return '';
}
'''
_METADATA_SCRIPT = _CALENDAR_ID_SCRIPT + r'''
function run() {
  const app = Application('com.apple.iCal');
  const calendars = app.calendars().map(c => ({native_id:calendarId(c), name:c.name(), writable:c.writable()}));
  let defaultId = '', policy = '', defaultSource = '';
  try {
    ObjC.import('CoreFoundation');
    function preference(key) {
      const value = $.CFPreferencesCopyAppValue($(key), $('com.apple.iCal'));
      if (!value) return '';
      const plain = ObjC.deepUnwrap(ObjC.castRefToObject(value));
      return typeof plain === 'string' ? plain : '';
    }
    const configured = preference('CalDefaultCalendar');
    const remembered = preference('defaultCalendarID');
    const lastSelected = preference('last selected calendar list item');
    if (configured === 'UseLastSelectedAsDefaultCalendar') {
      policy = 'last_selected';
      if (remembered && remembered === lastSelected) defaultId = remembered;
    } else {
      policy = 'fixed';
      defaultId = configured || remembered;
    }
    if (defaultId) defaultSource = 'calendar_preferences';
  } catch (error) {}
  return JSON.stringify({calendars:calendars, default_id:defaultId, default_policy:policy, default_source:defaultSource});
}
'''


def _metadata_native():
    """Calendar names, identifiers, writability and default only; no events."""
    result = subprocess.run(["osascript", "-l", "JavaScript", "-"], input=_METADATA_SCRIPT,
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Calendar metadata could not be read.")
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("calendars"), list):
        raise ValueError("Calendar metadata has an unreadable shape.")
    calendars = []
    for row in value["calendars"]:
        if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                or not row["name"] or type(row.get("writable")) is not bool
                or not isinstance(row.get("native_id", ""), str)):
            raise ValueError("Calendar metadata contains an unreadable destination.")
        native_id = row.get("native_id", "")
        token = native_id or "name:" + hashlib.sha256(row["name"].encode("utf-8")).hexdigest()[:24]
        calendars.append({"id": token, "native_id": native_id, "name": row["name"],
                          "writable": row["writable"], "is_default": bool(
                              native_id and native_id == value.get("default_id"))})
    return {"calendars": calendars, "default_id": str(value.get("default_id") or ""),
            "default_policy": str(value.get("default_policy") or ""),
            "default_source": str(value.get("default_source") or "")}


def destinations(refresh=False):
    """Discover an explicit destination or the verified system default.

    A sole writable calendar is an explicit fallback, never mislabeled as
    the system default. No owner's calendar names are product heuristics.
    """
    empty = {"available": False, "calendars": [], "selected": None, "selection": "",
             "default_id": "", "default_policy": "", "default_source": "", "error": "", "reason": ""}
    if os.environ.get("VIRA_PASSIVE") or settings.sandboxed() or settings.fixture_mode():
        return dict(empty, reason="Calendar discovery is disabled in passive, sandbox and fixture instances.")
    if not settings.IS_MAC:
        return dict(empty, reason="Calendar destination discovery requires macOS.")
    with _destination_lock:
        if refresh or _destination_cache["value"] is None or time.monotonic() - _destination_cache["at"] >= 60:
            try:
                value = dict(_metadata_native(), available=True, error="")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                value = dict(empty, error="Calendar metadata is unavailable: " + str(exc))
            _destination_cache.update(value=value, at=time.monotonic())
        out = dict(empty, **copy.deepcopy(_destination_cache["value"]))
    calendars = out["calendars"]
    cfg = settings.raw()
    explicit_id = str(cfg.get("assistant_calendar_id") or "").strip()
    explicit_name = str(cfg.get("assistant_calendar_name") or "").strip()
    if explicit_id:
        matches, selection = [c for c in calendars if c["id"] == explicit_id], "configured_id"
    elif explicit_name:
        matches, selection = [c for c in calendars if c["name"] == explicit_name], "configured_name"
    else:
        matches, selection = [c for c in calendars if c["is_default"]], "system_default"
        if not matches:
            matches = [c for c in calendars if c["writable"]]
            selection = "only_writable" if len(matches) == 1 else ""
    if len(matches) == 1 and matches[0]["writable"]:
        selected = matches[0]
        # Without a native identifier, only an exact unique name can be
        # selected reliably at the final native creation boundary.
        if selected["native_id"] or sum(c["name"] == selected["name"] for c in calendars) == 1:
            out.update(selected=selected, selection=selection, reason="")
            return out
    if out["error"]:
        out["reason"] = out["error"]
    elif len(matches) == 1 and not matches[0]["writable"]:
        out["reason"] = "The selected calendar is read-only; choose a writable calendar from the list."
    elif explicit_id or explicit_name:
        out["reason"] = "The saved calendar destination is missing or ambiguous; choose a calendar from the list."
    elif not any(c["writable"] for c in calendars):
        out["reason"] = "No writable calendar is available; connect one in Calendar.app."
    else:
        out["reason"] = "Choose a writable calendar from the list; the current system default could not be verified."
    return out
_SOLO = re.compile(
    r"\b(?:remind me|block (?:out )?(?:time|my calendar)|"
    r"(?:add|put) (?:this |it )?(?:on|to|in) my calendar|"
    r"(?:schedule|reserve|book|block)\b[^.!?\n]*\b(?:for myself|for me alone|solo))\b",
    re.I)
# Negative/conditional requests are suggestions; an affirmative fragment of
# "do not remind me" must never authorize a write.
_UNCERTAIN = re.compile(
    r"\b(?:not|don['’]?t|never|cancel(?:led|ed)?|maybe|might|if|unless|"
    r"possibly|perhaps|hypothetical|example|said|wrote|asked|used to|should)\b", re.I)
_COMPANY = re.compile(r"\b(?:with|invite|attendees?|we|us|our|together)\b", re.I)
_MONTHS = {name.lower(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
_WEEKDAYS = {name.lower(): i for i, name in enumerate(
    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}


def _now():
    return dt.datetime.now(UTC)


def _stamp():
    return _now().isoformat()


def _read():
    # This is a write ledger, not a regenerable cache. Treating corruption
    # or a read error as an empty store could replay a Calendar.app write.
    try:
        state = json.loads(Path(STORE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"drafts": {}}
    except (OSError, ValueError) as exc:
        raise ValueError("Calendar suggestion store is unreadable; restore it before creating events.") from exc
    if not isinstance(state, dict) or not isinstance(state.get("drafts"), dict):
        raise ValueError("Calendar suggestion store is invalid; restore it before creating events.")
    if any(not isinstance(d, dict) or d.get("id") != key or d.get("status") not in STATUSES
           for key, d in state["drafts"].items()):
        raise ValueError("Calendar suggestion ledger is invalid; restore it before creating events.")
    return state


def _text(value, field, required=False):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if "\x00" in value:
        raise ValueError(f"{field} contains an invalid character")
    return value


def _date(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Calendar times need full ISO dates, times and UTC offsets.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Calendar times must be valid ISO dates and times.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Calendar times need an explicit UTC offset.")
    if parsed.second or parsed.microsecond:
        raise ValueError("Calendar suggestions use whole minutes.")
    return parsed


def _dates(draft):
    start, end = _date(draft.get("start")), _date(draft.get("end"))
    if end <= start:
        raise ValueError("Calendar end must be after its start; no duration is assumed.")
    return start, end


def _date_mentions(quote, source_when, zone):
    dates = []
    for match in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", quote):
        try:
            dates.append((match.start(), dt.date.fromisoformat(match[0]).isoformat()))
        except ValueError:
            pass
    for match in re.finditer(
            r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})\b", quote, re.I):
        month, number, year = match.groups()
        month = _MONTHS.get(month.lower())
        if month:
            try:
                dates.append((match.start(), dt.date(int(year), month, int(number)).isoformat()))
            except ValueError:
                pass
    for match in re.finditer(r"\b(today|tomorrow)\b", quote, re.I):
        try:
            instant = dt.datetime.fromisoformat(source_when.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.utcoffset() is None:
                continue
            source_date = instant.astimezone(zone).date()
            if match[1].lower() == "tomorrow":
                source_date += dt.timedelta(days=1)
            dates.append((match.start(), source_date.isoformat()))
        except (ValueError, AttributeError):
            pass
    for match in re.finditer(r"\b(?:(next|this)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", quote, re.I):
        try:
            instant = dt.datetime.fromisoformat(source_when.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.utcoffset() is None:
                continue
            source_date = instant.astimezone(zone).date()
            days = (_WEEKDAYS[match[2].lower()] - source_date.weekday()) % 7
            prefix = (match[1] or "").lower()
            if prefix == "next" and days == 0:
                days = 7
            if prefix == "this" and _WEEKDAYS[match[2].lower()] < source_date.weekday():
                continue
            dates.append((match.start(), (source_date + dt.timedelta(days=days)).isoformat()))
        except (ValueError, AttributeError):
            pass
    for match in re.finditer(r"\bin\s+(\d{1,2})\s+days?\b", quote, re.I):
        try:
            instant = dt.datetime.fromisoformat(source_when.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.utcoffset() is None:
                continue
            date = instant.astimezone(zone).date() + dt.timedelta(days=int(match[1]))
            dates.append((match.start(), date.isoformat()))
        except (ValueError, AttributeError):
            pass
    return [date for _, date in sorted(dates)]


def _clock_mentions(quote):
    clocks = []
    # An unqualified "at 3" is ambiguous. 24-hour HH:MM and explicit AM/PM
    # both carry enough information to check an extracted appointment.
    for match in re.finditer(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", quote, re.I):
        hour, minute, meridiem = match.groups()
        if 1 <= int(hour) <= 12 and int(minute or 0) < 60:
            clocks.append((match.start(), (int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0), int(minute or 0))))
    for match in re.finditer(r"(?<![\d:+-])(\d{1,2}):(\d{2})(?!\d|:\d|\s*(?:am|pm)\b)", quote, re.I):
        hour, minute = map(int, match.groups())
        if hour < 24 and minute < 60:
            clocks.append((match.start(), (hour, minute)))
    return [clock for _, clock in sorted(clocks)]


def _zone(quote):
    """A source's named zone wins; otherwise use the owner's configured zone.

    An empty setting means the machine's local zone (astimezone(None)),
    resolved for the event date so daylight saving is applied correctly.
    Unknown timezone abbreviations are held rather than guessed.
    """
    offsets = set()
    for match in re.finditer(r"(?<!\d)([+-])(\d{2}):?(\d{2})(?!\d)", quote):
        sign, hours, minutes = match.groups()
        if int(hours) > 23 or int(minutes) > 59:
            raise ValueError("The source contains an invalid UTC offset.")
        offset = dt.timedelta(hours=int(hours), minutes=int(minutes))
        offsets.add(offset if sign == "+" else -offset)
    named = re.findall(r"\b[A-Za-z_]+(?:/[A-Za-z_+-]+)+\b", quote)
    if re.search(r"(?<![/\w])(?:UTC|GMT)\b(?!\s*[+-]\d)", quote):
        named.append("UTC")
    if len(set(named)) > 1 or len(offsets) > 1 or (named and offsets):
        raise ValueError("The source names multiple timezones; review the times before creating an event.")
    if offsets:
        return dt.timezone(offsets.pop())
    if not named and re.search(r"\b(?:[ECMP][DS]?T|BST|IST|CET|CEST|A[ECW][DS]?T)\b", quote):
        raise ValueError("The source uses a timezone abbreviation; use a full timezone before creating an event.")
    name = named[0] if named else settings.raw().get("assistant_timezone", "")
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Set a valid Assistant timezone before creating an event.") from exc


def _grounded(draft):
    quote = draft.get("time_quote", "")
    zone = _zone(quote)
    dates = _date_mentions(quote, draft["source"].get("when", ""), zone)
    clocks = _clock_mentions(quote)
    start, end = _dates(draft)
    if len(dates) not in (1, 2) or len(clocks) != 2:
        return "Keep as a suggestion: the source must state both dates and both start/end times."
    if ([start.date().isoformat(), end.date().isoformat()] != [dates[0], dates[-1]]
            or [(start.hour, start.minute), (end.hour, end.minute)] != clocks):
        return "Keep as a suggestion: the proposed dates and time range do not match the source."
    for point in (start, end):
        local = point.astimezone(zone)
        if local.replace(tzinfo=None) != point.replace(tzinfo=None):
            return "Keep as a suggestion: the UTC offsets do not match the source or Assistant timezone."
        # A repeated autumn clock time needs owner review even when one of
        # its two offsets happens to match the model's guess.
        if zone is not None and local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            return "Keep as a suggestion: this time occurs twice at the daylight-saving transition."
    return ""


def _validate(proposal, source):
    if not isinstance(proposal, dict) or not isinstance(source, dict):
        raise ValueError("Calendar proposal and source must be objects.")
    source_text = _text(source.get("text", ""), "source.text", True)
    source_id = source.get("id")
    if not isinstance(source_id, (str, int)) or isinstance(source_id, bool):
        raise ValueError("source.id must identify the ingested message")
    src = {"id": _text(str(source_id), "source.id", True),
           "channel": _text(source.get("channel", ""), "source.channel", True),
           "when": _text(source.get("when", ""), "source.when"),
           "is_from_me": source.get("is_from_me") is True, "text": source_text}
    draft = {"source": src}
    for field in ("title", "quote", "time_quote", "owner_only_quote", "location", "description"):
        draft[field] = _text(proposal.get(field, ""), field, field in {"title", "quote"})
    for field in ("quote", "time_quote", "owner_only_quote"):
        if draft[field] and draft[field] not in source_text:
            raise ValueError(f"{field} must quote the actual source message exactly.")
    attendees = proposal.get("attendees", [])
    if not isinstance(attendees, list) or any(not isinstance(a, str) or not a.strip() for a in attendees):
        raise ValueError("attendees must be a list of names or addresses.")
    draft["attendees"] = list(dict.fromkeys(a.strip() for a in attendees))
    if not isinstance(proposal.get("owner_only", False), bool):
        raise ValueError("owner_only must be a boolean.")
    draft["owner_only"] = proposal.get("owner_only", False)
    for field in ("start", "end"):
        draft[field] = _text(proposal.get(field, ""), field)
        if draft[field]:
            _date(draft[field])
    if draft["start"] and draft["end"]:
        _dates(draft)
    # A model may choose a different title or supporting substring on a
    # retry. The same source and time range still identify the same event.
    contexts = [part.strip() for part in re.split(r"[.!?\n]", source_text)
                if draft["owner_only_quote"] and draft["time_quote"]
                and draft["owner_only_quote"] in part and draft["time_quote"] in part]
    identity = (["owner request", contexts[0]] if len(contexts) == 1 else
                [_date(draft[f]).astimezone(UTC).isoformat() for f in ("start", "end")]
                if draft["start"] and draft["end"] else [draft["quote"]])
    key = json.dumps([src["channel"], src["id"], identity], ensure_ascii=False)
    draft["id"] = "cal_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    draft["source_key"] = f'{src["channel"]}:{src["id"]}'
    return draft


def stage(proposal, source):
    """Stage a suggestion from a proposal and its trusted input message.

    Source must be copied from the ingested message by the caller, never
    taken from model output. Every quote is rechecked against that text.
    """
    draft = _validate(proposal, source)
    with locked(STORE):
        state = _read()
        prior = state["drafts"].get(draft["id"])
        if prior and prior.get("status") in CLOSED | {"creating", "uncertain"}:
            return _view(prior)
        draft.update(status="suggested", reason="",
                     created_at=prior.get("created_at", _stamp()) if prior else _stamp(), updated_at=_stamp())
        state["drafts"][draft["id"]] = draft
        jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
    return _view(draft)


def _commitment_details(loop):
    owner_deadline = bool(isinstance(loop, dict) and loop.get("due_updated_by_owner"))
    if (not isinstance(loop, dict) or loop.get("source") != "vira-assistant"
            or loop.get("owed_by") != "me" or loop.get("status") == "closed"
            or loop.get("edited") or not loop.get("assistant_key")):
        raise ValueError("The source task is no longer an unchanged, open owner commitment.")
    what = _text(loop.get("what", ""), "commitment.what", True)
    due_text = _text(loop.get("due", ""), "commitment.due", True)
    due_quote = _text(loop.get("due_quote", ""), "commitment.due_quote", not owner_deadline)
    evidence = loop.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("A work block needs source evidence for the commitment and its due date.")
    matches = [e for e in evidence if isinstance(e, dict) and e.get("id")
               and isinstance(e.get("quote"), str) and e["quote"].strip()
               and (owner_deadline or due_quote in e["quote"])]
    if not matches:
        raise ValueError("The commitment's deadline quote needs a saved source reference.")
    zone = _zone("" if owner_deadline else due_quote)
    try:
        if len(due_text) == 10:
            deadline = dt.datetime.combine(dt.date.fromisoformat(due_text), dt.time.max)
            deadline = deadline.replace(tzinfo=zone) if zone else deadline.astimezone()
        else:
            deadline = _date(due_text)
    except ValueError as exc:
        raise ValueError("The commitment needs a valid explicit deadline.") from exc
    if not owner_deadline and not any(
            deadline.date().isoformat() in _date_mentions(due_quote, e.get("when", ""), zone) for e in matches):
        raise ValueError("The recorded deadline does not match its source quote.")
    if len(due_text) != 10:
        if not owner_deadline and (deadline.hour, deadline.minute) not in _clock_mentions(due_quote):
            raise ValueError("The recorded deadline time does not match its source quote.")
        if deadline.astimezone(zone).replace(tzinfo=None) != deadline.replace(tzinfo=None):
            raise ValueError("The deadline's UTC offset does not match the source timezone.")
    return {"what": what, "due": due_text, "due_quote": due_quote,
            "deadline": deadline, "evidence": copy.deepcopy(evidence), "source": matches[0],
            "deadline_authority": "owner" if owner_deadline else "source",
            "due_updated_by_owner": loop.get("due_updated_by_owner", "")}


def commitment_identity(loop, subject_key):
    """Keep a task's calendar identity when inbox work moves into CRM."""
    origin = loop.get("assistant_origin")
    if (isinstance(origin, dict) and isinstance(origin.get("subject_key"), str)
            and origin["subject_key"] and isinstance(origin.get("assistant_key"), str)
            and origin["assistant_key"]):
        return origin["subject_key"], origin["assistant_key"]
    return subject_key, loop.get("assistant_key")


def _current_commitment(draft):
    # The executive combines CRM contacts and owner/service correspondence.
    # Import at call time to avoid an initialization dependency in workers.
    from . import executive
    identity = draft.get("subject_key"), draft.get("commitment_key")
    return next((row["loop"] for row in executive.commitment_records()
                 if commitment_identity(row["loop"], row["subject_key"]) == identity), None)


def _work_settings():
    cfg = settings.raw()
    start = cfg.get("assistant_calendar_work_start", 9)
    end = cfg.get("assistant_calendar_work_end", 17)
    minutes = cfg.get("assistant_calendar_block_minutes", 30)
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= 24
            or type(minutes) is not int or not 15 <= minutes <= 240):
        raise ValueError("Set a working-hours window and a 15-240 minute personal block duration.")
    return _zone(""), start, end, minutes


def _calendar_busy(start, end):
    """Read expanded Calendar.app occurrences, including overnight overlaps.

    Calendar's scripting dictionary exposes recurring series. Its local
    occurrence cache supplies the actual instances. A failed coverage read
    never means a free calendar. Connected M365 calendars are checked too.
    """
    from . import brief, channels, msgraph
    rows = []
    try:
        connection = brief._cal_connect()
        try:
            rows = connection.execute(
                """SELECT COALESCE(oc.occurrence_start_date, oc.occurrence_date),
                          COALESCE(oc.occurrence_end_date, oc.occurrence_date), ci.all_day
                   FROM OccurrenceCache oc
                   JOIN CalendarItem ci ON oc.event_id = ci.ROWID
                   WHERE COALESCE(oc.occurrence_start_date, oc.occurrence_date) < ?
                     AND (COALESCE(oc.occurrence_end_date, oc.occurrence_date) > ?
                          OR (ci.all_day = 1 AND oc.occurrence_date > ?))""",
                (end.timestamp() - brief.APPLE_EPOCH, start.timestamp() - brief.APPLE_EPOCH,
                 start.timestamp() - brief.APPLE_EPOCH - 26 * 3600)).fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("Calendar.app occurrence coverage is unavailable; check calendar access.") from exc
    busy = []
    for low, high, all_day in rows:
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            raise RuntimeError("Calendar.app returned an unreadable busy interval.")
        first = dt.datetime.fromtimestamp(low + brief.APPLE_EPOCH, UTC)
        last = dt.datetime.fromtimestamp(high + brief.APPLE_EPOCH, UTC)
        if last <= first:
            if all_day:
                last = first + dt.timedelta(hours=26)
            else:
                raise RuntimeError("Calendar.app returned a busy event without a usable end time.")
        busy.append((first, last))
    for account in channels.graph_accounts():
        email = account["email"]
        # calendar_events() is a display helper with a 50-row cap. Scheduling
        # must follow every page or report missing coverage.
        query = "/me/calendarView?" + urllib.parse.urlencode({
            "startDateTime": start.isoformat(), "endDateTime": end.isoformat(),
            "$select": "start,end,showAs,isCancelled", "$top": 250})
        pages = set()
        while query:
            if query in pages or len(pages) >= 40:
                raise RuntimeError("M365 calendar coverage was incomplete; review before planning a work block.")
            pages.add(query)
            result = msgraph._graph_request(email, query, scope=msgraph.SCOPE_CAL,
                                           headers={"Prefer": 'outlook.timezone="UTC"'})
            if not isinstance(result, dict) or not isinstance(result.get("value"), list):
                raise RuntimeError("M365 returned unreadable calendar coverage.")
            for event in result["value"]:
                if not isinstance(event, dict):
                    raise RuntimeError("M365 returned an unreadable busy event.")
                if event.get("isCancelled") is True or event.get("showAs") == "free":
                    continue
                points = []
                try:
                    for key in ("start", "end"):
                        item = event[key]
                        # UTC preference is explicit. An unexpected zone
                        # cannot silently turn a busy meeting into a gap.
                        if item.get("timeZone") not in ("UTC", "Etc/UTC"):
                            raise ValueError("unexpected Graph timezone")
                        point = dt.datetime.fromisoformat(item["dateTime"].replace("Z", "+00:00"))
                        if point.tzinfo is not None and point.utcoffset() != dt.timedelta(0):
                            raise ValueError("Graph UTC response has a non-UTC offset")
                        points.append(point.replace(tzinfo=UTC) if point.tzinfo is None else point)
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError("M365 returned an unreadable busy interval.") from exc
                if points[1] <= points[0]:
                    raise RuntimeError("M365 returned a busy event without a usable end time.")
                busy.append(tuple(points))
            next_page = result.get("@odata.nextLink")
            if next_page:
                if not isinstance(next_page, str):
                    raise RuntimeError("M365 returned an unreadable calendar continuation.")
                parsed = urllib.parse.urlsplit(next_page)
                if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
                    raise RuntimeError("M365 returned an unexpected calendar continuation.")
                query = parsed.path[len("/v1.0"):] + "?" + parsed.query
            else:
                query = ""
    return busy


def _free_slot(deadline, exclude_id=None):
    zone, work_start, work_end, minutes = _work_settings()
    now = _now()
    end = min(deadline, now + dt.timedelta(days=7))
    if end <= now:
        return None
    busy = list(_calendar_busy(now, end))
    # Reserve staged work blocks too: several tasks planned before the OS
    # writes must not all choose the same free half hour.
    for draft in _read()["drafts"].values():
        if (draft.get("status") == "dismissed" or draft.get("id") == exclude_id
                or draft.get("owner_only") is not True or draft.get("attendees")):
            continue
        try:
            busy.append(_dates(draft))
        except ValueError:
            pass
    busy.sort(key=lambda pair: pair[0])
    day = now.astimezone(zone).date()
    duration = dt.timedelta(minutes=minutes)
    for offset in range(8):
        date = day + dt.timedelta(days=offset)
        if date.weekday() >= 5:
            continue
        first = dt.datetime.combine(date, dt.time(work_start))
        last = dt.datetime.combine(date + dt.timedelta(days=work_end == 24), dt.time(work_end % 24))
        first = first.replace(tzinfo=zone) if zone else first.astimezone()
        last = last.replace(tzinfo=zone) if zone else last.astimezone()
        candidate = max(first, now + dt.timedelta(minutes=5))
        # Minute rounding is explicit; no task is inserted in an already
        # begun minute or before the owner has time to see the result.
        if candidate.second or candidate.microsecond:
            candidate = candidate.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        limit = min(last, end)
        for occupied_start, occupied_end in busy:
            if occupied_end <= candidate:
                continue
            if candidate + duration <= min(occupied_start, limit):
                return candidate, candidate + duration
            if occupied_start < candidate + duration:
                candidate = max(candidate, occupied_end)
                if candidate.second or candidate.microsecond:
                    candidate = candidate.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        if candidate + duration <= limit:
            return candidate, candidate + duration
    return None


def plan_commitment(loop, subject_key, person_name=""):
    """Plan a personal work block before a grounded, open task deadline.

    The assistant chooses the slot and labels that choice. The source is
    evidence for the task/deadline, never misrepresented as stating this
    appointment's time. Returns None for tasks without suitable evidence.
    """
    try:
        details = _commitment_details(loop)
    except ValueError:
        return None
    subject_key = _text(subject_key, "subject_key", True)
    subject_key, commitment_key = commitment_identity(loop, subject_key)
    key = json.dumps([subject_key, commitment_key])
    draft_id = "cal_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    with locked(STORE):
        state = _read()
        prior = state["drafts"].get(draft_id)
        if prior:
            if prior.get("status") in CLOSED | {"creating", "uncertain"}:
                return _view(prior)
            if prior.get("status") == "suggested" and prior.get("due") == details["due"]:
                try:
                    if _dates(prior)[0] > _now() and not _commitment_eligibility(prior):
                        return _view(prior)
                except ValueError:
                    pass
            checked = prior.get("planned_at") or prior.get("updated_at")
            try:
                retry_due = _now() - dt.datetime.fromisoformat(checked) >= dt.timedelta(minutes=5)
            except (ValueError, TypeError):
                retry_due = True
            if not retry_due:
                return _view(prior)
        source = details["source"]
        source = {"id": str(source["id"]), "channel": source.get("channel", ""),
                  "when": source.get("when", ""), "text": source["quote"], "is_from_me": False}
        draft = _validate({"title": "Work on: " + details["what"], "quote": source["text"],
                           "owner_only": True, "description": "Personal work block for an open commitment. "
                           "The time and duration are chosen by Vira, not stated in the source message.",
                           "attendees": []}, source)
        draft.update(id=draft_id, schedule_kind="commitment", time_chosen_by="assistant",
                     subject_key=subject_key, person_name=person_name, commitment_key=commitment_key,
                     commitment_what=details["what"], due=details["due"], due_quote=details["due_quote"],
                     deadline_authority=details["deadline_authority"],
                     due_updated_by_owner=details["due_updated_by_owner"],
                     evidence=details["evidence"], status="suggested", reason="",
                     created_at=prior.get("created_at", _stamp()) if prior else _stamp(), updated_at=_stamp())
        if os.environ.get("VIRA_PASSIVE") or settings.sandboxed() or settings.fixture_mode() or not settings.IS_MAC:
            draft["reason"] = "Free-time planning needs the connected Mac's calendars; this task remains a suggestion."
        elif settings.raw().get("assistant_enabled") is not True:
            draft["reason"] = "Enable the assistant to choose a personal work-block time."
        else:
            try:
                slot = _free_slot(details["deadline"], exclude_id=draft_id)
                if slot:
                    draft.update(start=slot[0].isoformat(), end=slot[1].isoformat(),
                                 planned_at=_stamp())
                else:
                    draft["reason"] = "No free working-hours slot before this deadline within the next seven days."
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                draft["reason"] = "Free-time planning needs review: " + str(exc)
        state["drafts"][draft_id] = draft
        jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
    return _view(draft)


def _commitment_eligibility(draft):
    try:
        details = _commitment_details(_current_commitment(draft))
        if (details["what"] != draft.get("commitment_what") or details["due"] != draft.get("due")
                or details["due_quote"] != draft.get("due_quote")):
            return "The source commitment changed; review its existing calendar suggestion."
        start, end = _dates(draft)
        zone, work_start, work_end, minutes = _work_settings()
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        if (end > details["deadline"] or start <= _now()
                or end - start != dt.timedelta(minutes=minutes)
                or local_start.weekday() >= 5
                or (local_start.date() != local_end.date()
                    and not (work_end == 24 and local_end.date() == local_start.date() + dt.timedelta(days=1)
                             and local_end.hour == local_end.minute == 0))
                or local_start.hour < work_start
                or local_end.hour + local_end.minute / 60 > work_end):
            return "The planned work block no longer fits the deadline or configured working hours."
    except ValueError as exc:
        return str(exc)
    return ""


def _eligibility(draft, automatic=True):
    cfg = settings.raw()
    if os.environ.get("VIRA_PASSIVE") or settings.sandboxed():
        return "Calendar writes are disabled in passive and sandbox instances."
    if settings.fixture_mode():
        return "Connect real sources before creating calendar events."
    if cfg.get("assistant_enabled") is not True:
        return "Enable the assistant before creating calendar events."
    if automatic and cfg.get("assistant_calendar_auto_create") is not True:
        return "Automatic calendar creation is off; this remains a suggestion."
    if not settings.IS_MAC:
        return "Personal calendar creation requires Calendar.app on macOS; export the draft instead."
    target = destinations()
    if not target.get("selected"):
        return target["reason"] or "Choose a writable calendar from the discovered list."
    if draft.get("attendees") or draft.get("owner_only") is not True:
        return "Events involving other people remain suggestions; Vira does not send invitations."
    if draft.get("schedule_kind") == "commitment":
        return _commitment_eligibility(draft)
    if draft.get("source", {}).get("is_from_me") is not True:
        return "Only the owner's own scheduling request can create a personal event."
    solo_quote = draft.get("owner_only_quote", "")
    source_text = draft.get("source", {}).get("text", "")
    if (not source_text or solo_quote not in source_text or not _SOLO.search(solo_quote)
            or _UNCERTAIN.search(source_text) or _COMPANY.search(source_text)
            or re.search(r"(?:^|\n)\s*>|[\"“”]", source_text)):
        return "A clear request from the owner to schedule personal time is required."
    contexts = [part for part in re.split(r"[.!?\n]", source_text)
                if solo_quote in part and draft.get("time_quote", "") in part]
    if len(contexts) != 1:
        return "The personal scheduling request and its times must refer to the same event."
    try:
        start, _ = _dates(draft)
        # Check the whole owner request, not a cherry-picked time fragment
        # from another appointment in that source message.
        reason = _grounded(dict(draft, time_quote=contexts[0]))
    except ValueError as exc:
        return str(exc)
    if reason:
        return reason
    if start <= _now():
        return "The suggested start is in the past; review its date before creating an event."
    return ""


def _view(draft):
    out = copy.deepcopy(draft)
    reason = _eligibility(out, automatic=False)
    out["can_create"] = out.get("status") in {"suggested", "blocked"} and not reason
    try:
        _dates(out)
        out["can_export"] = True
    except ValueError:
        out["can_export"] = False
    out["reason"] = out.get("reason") or reason
    # The visible quote is the evidence; the full source is retained only
    # for the write-time intent check, not repeated in every overview.
    out.get("source", {}).pop("text", None)
    return out


def get(draft_id):
    draft = _read()["drafts"].get(draft_id)
    return _view(draft) if draft else None


def list_drafts(include_closed=False):
    drafts = _read()["drafts"].values()
    return [_view(d) for d in sorted(drafts, key=lambda d: d.get("created_at", ""), reverse=True)
            if include_closed or d.get("status") not in CLOSED]


def dismiss(draft_id):
    with locked(STORE):
        state = _read()
        draft = state["drafts"].get(draft_id)
        if not draft:
            raise ValueError("Calendar suggestion not found.")
        if draft.get("status") == "created":
            raise ValueError("This event already exists; manage it in Calendar.app.")
        if draft.get("status") == "creating":
            raise ValueError("Calendar creation is in progress; wait for its result.")
        draft.update(status="dismissed", updated_at=_stamp())
        jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
    return _view(draft)


class CalendarRefused(RuntimeError):
    """Adapter refusal proved to occur before a calendar mutation."""


# Constant script + JSON argv prevents source message text becoming code.
# Calendar's scripting dictionary has read-only attendee properties; this
# adapter never mentions them in a creation payload or invokes invitation APIs.
_SCRIPT = _CALENDAR_ID_SCRIPT + r'''
function run(argv) {
  const p = JSON.parse(argv[0]);
  const app = Application('com.apple.iCal');
  const calendars = p.calendar_id ? app.calendars().filter(c => calendarId(c) === p.calendar_id)
    : app.calendars.whose({name: p.calendar})();
  if (calendars.length !== 1) return JSON.stringify({refused: 'Choose a unique Calendar.app calendar name; found ' + calendars.length + ' matches.'});
  const cal = calendars[0];
  if (!cal.writable()) return JSON.stringify({refused: 'The configured calendar is read-only. Choose a writable calendar.'});
  const existing = cal.events.whose({description: {_contains: p.marker}})();
  if (existing.length > 1) throw Error('Multiple events carry this Vira marker; inspect Calendar.app.');
  if (existing.length) return JSON.stringify({uid: existing[0].uid(), existing: true});
  if (p.avoid_conflicts) {
    const all = app.calendars();
    for (let i = 0; i < all.length; i++) {
      const busy = all[i].events.whose({startDate: {_lessThan: new Date(p.end)},
        endDate: {_greaterThan: new Date(p.start)}})();
      if (busy.length) return JSON.stringify({refused: 'Calendar changed: this personal work-block time is now busy.'});
    }
  }
  const event = app.Event({summary: p.title, startDate: new Date(p.start),
    endDate: new Date(p.end), description: p.description + '\n\n' + p.marker,
    location: p.location, alldayEvent: false});
  cal.events.push(event);
  return JSON.stringify({uid: event.uid(), existing: false});
}
'''


def _description(draft):
    source = draft.get("source", {})
    provenance = f'Source: {source.get("channel", "")} {source.get("id", "")} ({source.get("when", "")})'
    parts = [draft.get("description", ""), provenance, "Source quote: " + draft.get("quote", "")]
    if draft.get("schedule_kind") == "commitment":
        parts.append("Task deadline: " + draft.get("due", "") + (
            " (corrected by the owner)" if draft.get("deadline_authority") == "owner" else " (from source evidence)"))
        for ref in draft.get("evidence", []):
            if ref.get("quote") and ref["quote"] != draft.get("quote"):
                parts.append(f'Task evidence ({ref.get("channel", "")} {ref.get("id", "")}): {ref["quote"]}')
    return "\n\n".join(p for p in parts if p)


def _calendar_create(draft, calendar_name, calendar_id=""):
    payload = {key: draft.get(key, "") for key in ("title", "start", "end", "description", "location")}
    payload["description"] = _description(draft)
    payload.update(calendar=calendar_name, marker=f'Vira personal event: {draft["id"]}')
    payload["calendar_id"] = calendar_id
    payload["avoid_conflicts"] = draft.get("schedule_kind") == "commitment"
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-", json.dumps(payload, ensure_ascii=False)],
        input=_SCRIPT, capture_output=True, text=True, encoding="utf-8", timeout=45)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Calendar.app could not create the event.")
    try:
        out = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Calendar.app returned an unreadable result; inspect the calendar before retrying.") from exc
    if isinstance(out, dict) and isinstance(out.get("refused"), str) and out["refused"]:
        raise CalendarRefused(out["refused"])
    if not isinstance(out, dict) or not isinstance(out.get("uid"), str) or not out["uid"]:
        raise RuntimeError("Calendar.app did not return an event identifier; inspect the calendar.")
    return out


def create_owner_event(draft_id, automatic=True):
    """Create one personal block, or return an actionable refusal.

    automatic=False is for an explicit owner click only. It bypasses the
    auto-create toggle, never the source, attendee, instance or date checks.
    """
    with locked(STORE):
        state = _read()
        draft = state["drafts"].get(draft_id)
        if not draft:
            raise ValueError("Calendar suggestion not found.")
        if draft.get("status") in CLOSED | {"creating", "uncertain"}:
            return _view(draft)
        reason = _eligibility(draft, automatic=automatic)
        if reason:
            draft.update(status="blocked", reason=reason, updated_at=_stamp())
            jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
            return _view(draft)
        if draft.get("schedule_kind") == "commitment":
            try:
                start, end = _dates(draft)
                if any(low < end and high > start for low, high in _calendar_busy(start, end)):
                    reason = "Calendar changed: this personal work-block time is now busy."
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                reason = "Calendar availability could not be rechecked: " + str(exc)
            if reason:
                draft.update(status="blocked", reason=reason, updated_at=_stamp())
                jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
                return _view(draft)
        # Resolve fresh immediately before the claim. A cached name or a
        # changed system default must not silently redirect this event.
        destination = destinations(refresh=True)
        selected = destination.get("selected")
        if not selected:
            draft.update(status="blocked", reason=destination["reason"], updated_at=_stamp())
            jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
            return _view(draft)
        calendar_name = selected["name"]
        # Persist before talking to the OS. A process crash after Calendar
        # saves but before the reply is recorded cannot cause a replay.
        draft.update(status="creating", reason="Calendar creation started; if interrupted, inspect Calendar.app before any retry.", updated_at=_stamp())
        jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
        try:
            result = _calendar_create(draft, calendar_name, calendar_id=selected["native_id"])
        except CalendarRefused as exc:
            draft.update(status="blocked", reason=str(exc), updated_at=_stamp())
        except Exception as exc:  # noqa: BLE001 - an uncertain OS write must be recorded
            draft.update(status="uncertain", reason=f"Calendar result needs review: {exc}", updated_at=_stamp())
        else:
            draft.update(status="created", event_uid=result["uid"], event_calendar=calendar_name,
                         event_calendar_id=selected["id"],
                         reason="Personal event created without invitees.", updated_at=_stamp())
        jsonstore.write_atomic(STORE, state, indent=2, ensure_ascii=False)
        return _view(draft)


def _ical_text(value):
    return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def ics(draft_id):
    """Portable draft with no scheduling METHOD, ORGANIZER or ATTENDEE.

    Suggested invitees stay visible in Vira. Importing this file cannot
    emit a meeting invitation on their behalf.
    """
    draft = get(draft_id)
    if not draft:
        raise ValueError("Calendar suggestion not found.")
    start, end = _dates(draft)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vira//Calendar Draft//EN",
             "CALSCALE:GREGORIAN", "BEGIN:VEVENT", f'UID:{draft["id"]}@vira.local',
             "DTSTAMP:" + _now().strftime("%Y%m%dT%H%M%SZ"),
             "DTSTART:" + start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
             "DTEND:" + end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
             "SUMMARY:" + _ical_text(draft["title"]),
             "DESCRIPTION:" + _ical_text(_description(draft)),
             "LOCATION:" + _ical_text(draft.get("location", "")),
             "STATUS:TENTATIVE", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_ical_fold(line) for line in lines) + "\r\n"

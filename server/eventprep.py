"""Event prep: one countdown checklist per upcoming dated event.

A birthday weekend, a trip, a festival or a dinner tends to scatter its prep
across the owner's world: a hotel to book in one thread, a gift to order in
another, a calendar block with no notes. This module anchors on the event
itself and gathers every open item tied to it into one checklist the brief
shows with a countdown.

Anchors come from two places, both deterministic local reads:

- the calendar (the brief's own expanded occurrences): all-day and multi-day
  events, birthday-calendar entries, and timed events whose title names an
  occasion ("dinner", "trip", "festival"). Routine meetings never anchor.
- messages: open loops (CRM relationship loops and private assistant
  commitments, both extracted from messages with evidence) that carry a due
  date and name an occasion. Two loops naming the same occasion on the same
  date are one event; a loop that matches a calendar anchor joins it instead.

Items are then clustered onto the best anchor by shared distinctive words
(a place, a person's name) or, for a generic occasion word, by also falling
due before the event. Each item gets a kind: gift, packing, logistics or
to-do. The owner can tick items and add their own (a packing list rarely
exists in any message); that state is the only thing this module stores.

An event clears from the brief the day after it ends, and its stored check
state is pruned a while later. Ticking an item here records it on the
checklist only; it never closes or rewrites the source loop, which stays
owned by the CRM or the assistant's commitment store.
"""
import datetime as dt
import hashlib
import re

from . import jsonstore, settings

STORE = settings.ROOT / "data" / "event-prep.json"

# How far ahead an event starts collecting prep. Three weeks covers booking a
# table or ordering a gift; a festival planned months out still appears here
# once it is close enough for its prep to be actionable.
HORIZON_DAYS = 21
# Event check state is kept this long after the event ends, then pruned:
# long enough for an owner who looks back the following week, short enough
# that the store never grows without bound.
KEEP_DAYS = 30
# Events shown in the brief at once. More than this is a cluttered brief, and
# the list is sorted soonest first so nothing urgent is the one cut.
MAX_EVENTS = 6
# Items per event shown in the brief; the payload says how many were cut.
MAX_ITEMS = 12
# Owner-added items per event and characters per item: a checklist line, not
# a document.
MAX_ADDED = 40
ITEM_CHARS = 200

# Words that make a calendar entry or a loop an occasion worth prepping for.
OCCASIONS = {
    "birthday", "bday", "anniversary", "wedding", "engagement", "shower",
    "graduation", "party", "celebration", "reunion", "dinner", "brunch",
    "lunch", "trip", "travel", "flight", "vacation", "holiday", "getaway",
    "weekend", "festival", "concert", "show", "game", "match", "recital",
    "camp", "retreat", "visit", "housewarming", "bachelor", "bachelorette",
    "christening", "bar", "bat", "mitzvah", "funeral", "memorial", "gala",
    "tournament", "race", "marathon", "cruise", "ski", "beach",
}
# Occasion words too common on their own to anchor a timed calendar event
# ("lunch" with a colleague is a meeting, not an occasion).
WEAK_TIMED = {"lunch", "show", "game", "match", "visit", "bar", "bat",
              "weekend", "travel", "race"}

KINDS = (
    ("gift", {"gift", "gifts", "present", "presents", "card", "flowers",
              "cake", "wrap", "wrapping", "registry", "balloons"}),
    ("packing", {"pack", "packing", "bring", "luggage", "suitcase", "bag",
                 "passport", "charger", "sunscreen", "outfit", "clothes",
                 "swimsuit", "toiletries", "jacket", "camera"}),
    ("logistics", {"book", "booking", "booked", "reserve", "reservation",
                   "flight", "flights", "hotel", "airbnb", "rental", "car",
                   "train", "uber", "lyft", "taxi", "parking", "tickets",
                   "ticket", "rsvp", "confirm", "itinerary", "check-in",
                   "transfer", "ride", "pickup", "babysitter", "sitter",
                   "directions", "address", "venue", "table"}),
)
KIND_ORDER = {"logistics": 0, "gift": 1, "todo": 2, "packing": 3}

STOP = {
    "a", "an", "the", "and", "or", "of", "to", "for", "with", "on", "in",
    "at", "by", "from", "my", "your", "our", "their", "his", "her", "its",
    "is", "are", "be", "was", "this", "that", "next", "about", "up", "out",
    "get", "got", "make", "send", "buy", "need", "needs", "plan", "plans",
    "ask", "check", "sort", "figure", "follow", "reply", "re", "it", "me",
    "we", "us", "them", "they", "you", "will", "should", "can", "before",
    "after", "into", "day", "days", "s", "all", "fri", "sat", "sun", "mon",
    "tue", "wed", "thu", "am", "pm", "new", "back",
}
_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def _tokens(text):
    out = []
    for w in _WORD.findall(str(text or "").casefold()):
        w = w.strip("'-")
        if w.endswith("'s"):
            w = w[:-2]
        if w:
            out.append(w)
    return out


def _distinct(text):
    """Words that can tie an item to one event: not filler, not an occasion
    word (two different dinners share "dinner" and nothing else)."""
    return {w for w in _tokens(text)
            if len(w) >= 3 and w not in STOP and w not in OCCASIONS
            and not w.isdigit()}


def _occasions(text):
    return {w for w in _tokens(text) if w in OCCASIONS}


def kind_of(text):
    words = set(_tokens(text))
    for kind, vocab in KINDS:
        if words & vocab:
            return kind
    return "todo"


def _day(value):
    """A loop due date or an event timestamp as a local date, or None.
    Stored timestamps are converted to local time before taking the day,
    never sliced (a UTC evening is tomorrow's date)."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            return dt.date.fromisoformat(value)
        point = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if point.tzinfo is not None:
        point = point.astimezone()
    return point.date()


def _key(*parts):
    raw = "|".join(" ".join(str(p or "").casefold().split()) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


# ---------- sources (seams the tests patch) ----------

def _calendar_events(day_from, day_to):
    """The brief's expanded local calendar occurrences between two local
    dates. An unreadable store answers [] (brief records why)."""
    from . import brief
    lo = dt.datetime.combine(day_from, dt.time.min)
    hi = dt.datetime.combine(day_to, dt.time.min)
    return brief._occurrences(lo, hi)


def _loops(include_commitments=True):
    """Open loops the owner owes, from CRM profiles and (outside fixture
    previews) the private assistant commitment store."""
    from . import data as crm
    rows = []
    corpus = crm._load()
    for pid, profile in (corpus.get("profiles") or {}).items():
        person = (corpus.get("by_id") or {}).get(pid) or {}
        if person.get("class_hint") == "company":
            continue
        name = person.get("name") or profile.get("name") or pid
        for lp in profile.get("open_loops") or []:
            if isinstance(lp, dict):
                rows.append({"person_id": pid, "person_name": name,
                             "subject_key": pid, "loop": lp})
    if include_commitments:
        from . import commitments
        try:
            subjects = commitments.all_subjects()
        except ValueError:
            subjects = {}   # a store needing repair is named by its own window
        for key, subject in subjects.items():
            for lp in subject.get("open_loops") or []:
                if isinstance(lp, dict):
                    rows.append({"person_id": None,
                                 "person_name": subject.get("person_name") or "Inbox",
                                 "subject_key": key, "loop": lp})
    out = []
    for r in rows:
        lp = r["loop"]
        what = str(lp.get("what") or "").strip()
        if not what or lp.get("status") == "closed" or lp.get("owed_by") != "me":
            continue
        out.append({**r, "what": what, "due": _day(lp.get("due")),
                    "id": _key(r["subject_key"], lp.get("assistant_key") or what)})
    return out


# ---------- anchors ----------

def _calendar_anchors(events, today):
    by_title = {}
    for e in events:
        title = str(e.get("title") or "").strip()
        if not title or e.get("work"):
            continue
        occ = _occasions(title)
        start, end = _day(e.get("start")), _day(e.get("end"))
        if not start:
            continue
        if end is None or end < start:
            end = start
        # an all-day event's end is the exclusive midnight after its last day
        if e.get("all_day") and end > start:
            end -= dt.timedelta(days=1)
        multi = end > start
        if not (e.get("birthday") or e.get("all_day") or multi
                or (occ - WEAK_TIMED)):
            continue
        if end < today:
            continue
        # OccurrenceCache repeats a multi-day event once per day and a
        # recurring one once per instance: the next instance is the event.
        k = " ".join(title.casefold().split())
        cur = by_title.get(k)
        if cur and cur["start"] <= start:
            if start <= cur["end"] + dt.timedelta(days=1):
                cur["end"] = max(cur["end"], end)
            continue
        by_title[k] = {"title": title, "start": start, "end": end,
                       "source": "calendar",
                       "birthday": bool(e.get("birthday")),
                       "calendar": e.get("calendar") or "",
                       "time": "" if e.get("all_day") else e.get("start_hm") or "",
                       "family": bool(e.get("family"))}
    return list(by_title.values())


_LEAD_STOP = STOP | {"plan", "book", "booking", "reserve", "for", "pack"}


def _message_title(what, occasion):
    """The occasion phrase inside a loop: up to three words leading into the
    occasion word ("Book hotel for Sam's birthday weekend" -> "Sam's birthday
    weekend")."""
    words = str(what).split()
    low = [w.casefold().strip(".,:;!?()") for w in words]
    idx = next((i for i, w in enumerate(low)
                if w.removesuffix("'s") == occasion), None)
    if idx is None:
        return occasion.capitalize()
    start = idx
    while start > 0 and idx - start < 3:
        prev = low[start - 1].removesuffix("'s")
        if prev in _LEAD_STOP or not prev:
            break
        start -= 1
    end = idx + 1
    if end < len(low) and low[end] in OCCASIONS:
        end += 1   # "birthday weekend", "bachelor party"
    phrase = " ".join(w.strip(".,:;!?()") for w in words[start:end])
    return phrase[:1].upper() + phrase[1:]


def _match_score(item, anchor):
    """How strongly a loop belongs to an anchor; 0 means it does not."""
    shared = _distinct(item["what"]) & anchor["_words"]
    if item.get("person_name") and anchor.get("birthday"):
        # a birthday calendar entry is titled with the person's name
        shared |= _distinct(item["person_name"]) & anchor["_words"]
    due = item.get("due")
    in_window = due is not None and due <= anchor["end"] and \
        (anchor["start"] - due).days <= HORIZON_DAYS
    occ = bool(_occasions(item["what"]) & anchor["_occ"])
    # One shared name alone is weak: "Dinner with Casey" must not swallow
    # every loop about Casey. It needs a second signal: an occasion word, a
    # due date before the event, a prep kind (gift, packing, logistics), or
    # a second shared word.
    if shared and not (occ or in_window or len(shared) >= 2
                       or kind_of(item["what"]) != "todo"):
        shared = set()
    score = 2 * len(shared)
    if occ and (in_window or shared):
        score += 1
    if shared and due is not None and not in_window:
        score -= 1   # same words, wrong date: probably another occasion
    return max(score, 0)


def build(today=None, include_calendar=True, include_commitments=True):
    """Upcoming events with their clustered checklists, soonest first."""
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=HORIZON_DAYS)
    anchors = []
    if include_calendar:
        anchors = _calendar_anchors(
            _calendar_events(today, horizon + dt.timedelta(days=1)), today)
    loops = _loops(include_commitments)

    for a in anchors:
        a["_words"] = _distinct(a["title"])
        a["_occ"] = _occasions(a["title"])
        if a["birthday"]:
            a["_occ"] = a["_occ"] | {"birthday", "bday"}

    # Loops that name an occasion and carry a date become anchors themselves
    # unless an anchor (calendar, or an earlier loop) already covers them.
    for item in sorted(loops, key=lambda r: (r["due"] or dt.date.max, r["id"])):
        due = item["due"]
        occ = _occasions(item["what"])
        if not occ or due is None or due < today or due > horizon:
            continue
        main = sorted(occ, key=lambda w: (w in WEAK_TIMED, w))[0]
        title = _message_title(item["what"], main)
        covering = next((a for a in anchors if _match_score(item, a) >= 2 or
                         (occ & a["_occ"]
                          and a["start"] <= due + dt.timedelta(days=1)
                          and due <= a["end"] + dt.timedelta(days=1))), None)
        if covering is not None:
            # Two loops about one occasion: keep the more specific name
            # ("Sam's birthday weekend" over "Birthday weekend").
            if (covering["source"] == "messages"
                    and len(_distinct(title)) > len(covering["_words"])):
                covering.update(title=title, _words=_distinct(title))
            continue
        anchors.append({"title": title,
                        "start": due, "end": due, "source": "messages",
                        "birthday": main in ("birthday", "bday"),
                        "calendar": "", "time": "", "family": False,
                        "_words": _distinct(title), "_occ": occ,
                        # keyed by occasion and date, not the title, so a
                        # renamed anchor keeps the owner's ticks
                        "_id": ("messages", main)})

    for a in anchors:
        a["key"] = _key(*a.get("_id", (a["title"],)), a["start"].isoformat())
        a["items"] = []

    # Each loop joins at most one event: the best-scoring one, soonest on a
    # tie. A loop that names an occasion with a date inside an anchor's span
    # always joins it (it is how message anchors got their first item).
    for item in loops:
        best, best_score = None, 0
        for a in sorted(anchors, key=lambda a: a["start"]):
            s = _match_score(item, a)
            due = item["due"]
            if (s == 0 and due and _occasions(item["what"]) & a["_occ"]
                    and a["start"] - dt.timedelta(days=1) <= due <= a["end"]):
                s = 1
            if s > best_score:
                best, best_score = a, s
        if best is not None and best_score >= 1:
            best["items"].append(item)

    state = _state_for_read()
    out = []
    for a in anchors:
        saved = state["events"].get(a["key"]) or {}
        if saved.get("dismissed"):
            continue
        checked = saved.get("checked") or {}
        rows = [{"id": it["id"], "text": it["what"], "kind": kind_of(it["what"]),
                 "source": "loop", "person_id": it["person_id"],
                 "person_name": it["person_name"],
                 "due": it["due"].isoformat() if it["due"] else None,
                 "done": it["id"] in checked}
                for it in a["items"]]
        rows += [{"id": ad["id"], "text": ad["text"],
                  "kind": ad.get("kind") or kind_of(ad["text"]),
                  "source": "owner", "person_id": None, "person_name": None,
                  "due": None, "done": ad["id"] in checked}
                 for ad in saved.get("added") or []
                 if isinstance(ad, dict) and ad.get("id") and ad.get("text")]
        # An anchor from the calendar with nothing to prep is still a real
        # event the owner may want to add to; a message anchor always has
        # the loop that created it.
        rows.sort(key=lambda r: (r["done"], KIND_ORDER.get(r["kind"], 9),
                                 r["due"] or "9999", r["text"].casefold()))
        days = (a["start"] - today).days
        out.append({
            "key": a["key"], "title": a["title"],
            "start": a["start"].isoformat(), "end": a["end"].isoformat(),
            "time": a["time"], "source": a["source"],
            "calendar": a["calendar"], "birthday": a["birthday"],
            "family": a["family"],
            "days": max(days, 0), "in_progress": days <= 0, "started": days < 0,
            "urgency": "now" if days <= 2 else ("soon" if days <= 7 else "ahead"),
            "total": len(rows), "open": sum(1 for r in rows if not r["done"]),
            "items": rows[:MAX_ITEMS], "truncated": max(len(rows) - MAX_ITEMS, 0),
        })
    # An event with open prep outranks a bare calendar entry on the same day.
    out.sort(key=lambda e: (e["start"], e["open"] == 0, e["title"].casefold()))
    return {"events": out[:MAX_EVENTS],
            "hidden": max(len(out) - MAX_EVENTS, 0),
            "horizon_days": HORIZON_DAYS}


# ---------- owner check state ----------

def _empty():
    return {"events": {}}


def _state_for_read():
    s = jsonstore.read(STORE, _empty())
    if not isinstance(s, dict) or not isinstance(s.get("events"), dict):
        return _empty()
    return s


def _prune(s, today):
    """Drop check state for events that ended more than KEEP_DAYS ago. Loads
    the whole store itself (never a caller's list) so a live event is never
    mistaken for a finished one."""
    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for k in [k for k, v in s["events"].items()
              if not isinstance(v, dict) or (v.get("end") or "") < cutoff]:
        s["events"].pop(k, None)


def _mutate(event_key, end, fn, today=None):
    if not isinstance(event_key, str) or not re.fullmatch(r"[0-9a-f]{20}", event_key):
        raise ValueError("An exact event key is required")
    if _day(end) is None:
        raise ValueError("The event's end date is required")
    today = today or dt.date.today()

    def apply(s):
        if not isinstance(s, dict) or not isinstance(s.get("events"), dict):
            s = _empty()
        _prune(s, today)
        ev = s["events"].setdefault(event_key, {"checked": {}, "added": []})
        ev["end"] = _day(end).isoformat()
        ev.setdefault("checked", {})
        ev.setdefault("added", [])
        fn(ev)
        return s

    return jsonstore.mutate(STORE, apply, _empty(), indent=1,
                            ensure_ascii=False)["events"].get(event_key)


def set_checked(event_key, end, item_id, done=True):
    if not isinstance(item_id, str) or not item_id or len(item_id) > 64:
        raise ValueError("An exact item id is required")
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()

    def fn(ev):
        if done:
            ev["checked"][item_id] = stamp
        else:
            ev["checked"].pop(item_id, None)
    return _mutate(event_key, end, fn)


def add_item(event_key, end, text):
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("An item needs text")
    text = text[:ITEM_CHARS]
    item = {"id": "own-" + _key(event_key, text, dt.datetime.now().isoformat())[:12],
            "text": text, "kind": kind_of(text),
            "added_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    def fn(ev):
        if len(ev["added"]) >= MAX_ADDED:
            raise ValueError(f"An event holds at most {MAX_ADDED} added items")
        ev["added"].append(item)
    _mutate(event_key, end, fn)
    return item


def remove_item(event_key, end, item_id):
    def fn(ev):
        before = len(ev["added"])
        ev["added"] = [a for a in ev["added"] if a.get("id") != item_id]
        if len(ev["added"]) == before:
            raise KeyError(item_id)
        ev["checked"].pop(item_id, None)
    return _mutate(event_key, end, fn)


def dismiss(event_key, end, restore=False):
    def fn(ev):
        if restore:
            ev.pop("dismissed", None)
        else:
            ev["dismissed"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return _mutate(event_key, end, fn)

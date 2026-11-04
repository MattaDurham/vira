"""Family moments: when a family event happens, relay the day's best shots
to the people who would want them.

THE SETUP IS THE OWNER'S. Two owner-entered lists live in
data/moments.json; the product never assumes a family member, a partner, or
an occasion:

  - sources: the people whose shared photos are the family's photos (a
    partner who takes most of the pictures, a family group's regular
    poster). A source is never suggested as a recipient: they took the
    pictures.
  - relay: the people who usually want family photos (grandparents, an
    aunt, a godparent), each with a relation label and, optionally, the
    occasion words they care about. An empty word list means every moment.

WHAT MAKES A MOMENT, for one local day. Deterministic, no model call:

  - a calendar event that day whose title names a family occasion
    ("Halloween parade", "Spring recital", "First day of school"). Events
    on a calendar tagged family (`family_calendars`) need no occasion word.
  - a burst of photos from the sources that day (BURST_MIN or more) with no
    calendar anchor: the day's messages from the sources lend it a title
    when they name an occasion, otherwise it is "Photos from <weekday>".
  - one the owner adds by hand (a title and a date).

WHAT A MOMENT SHOWS. The day's photos and videos from the media index
(server/mediaindex.py): items in a source's 1:1 thread, either direction,
and items a source posted into a group. They are ranked by what the index
already knows - faces found, faces it could name, a caption, and against
screenshots (OCR text) and stickers - and near-identical burst shots are
folded by their scene embeddings, so the top picks are different pictures.
The best BEST_PICKS are pre-selected for every recipient; the owner can
change each person's picks.

WHO IS SUGGESTED. Everyone on the relay list whose words fit the moment,
plus anyone who sent a gift for the occasion: a message in the GIFT_LOOKBACK
days before it that names a gift and one of the moment's own words,
received from them or a thank-you the owner sent them. Each suggestion says
why, quoting the message.

THE DRAFT AND THE SEND. One short message per person from a fixed template
(a thank-you for a gift sender), editable in the window. Send delivers the
text, then each picked file, through server/send.py (the same
iMessage-then-SMS ladder as every other send). Messages only attaches files
it can read from a user folder, so each file is staged under
~/Pictures/Vira Moments first (pruned after STAGE_KEEP_DAYS). A file
macOS has offloaded to iCloud with no archived copy is skipped and named in
the result, never silently dropped. A sent recipient is recorded and is
never sent twice.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import shutil
import sqlite3
import time
from pathlib import Path

from . import jsonstore, settings

STORE = settings.ROOT / "data" / "moments.json"
STAGE_DIR = Path.home() / "Pictures" / "Vira Moments"
# Staged copies only need to outlive the Messages hand-off; a few days
# covers a send the owner retries the next morning.
STAGE_KEEP_DAYS = 3
# Owner-entered lists, capped so a pasted blob cannot grow the store; a
# family has a handful of each.
MAX_SOURCES = 8
MAX_RELAY = 30
MAX_WORDS = 12
# Shots attached per message by default: a few, not an album.
BEST_PICKS = 3
# Photos shown per moment to choose from; a parade day can produce hundreds
# and the owner picks from the best, not from all of them.
MAX_CANDIDATES = 24
# Files one send may carry; Messages delivers each as its own balloon.
MAX_SEND_FILES = 6
# A photo burst with no calendar event is a moment only past this size;
# two snapshots on a Tuesday are not an occasion.
BURST_MIN = 6
# How far back a gift for the occasion is looked for, and how many
# messages are read when looking. A birthday parcel usually arrives within
# a few weeks of the day.
GIFT_LOOKBACK_DAYS = 30
GIFT_SCAN_LIMIT = 400
# Two scene embeddings this close are the same picture (a burst or a
# re-send); only the better one is kept.
NEAR_DUP = 0.94
# Days back the Decide queue and the window look for moments still waiting.
PENDING_DAYS = 2
# The queue reader runs on every brief; one computed answer is reused this
# long. Only a successful read is kept.
PENDING_CACHE_S = 600
MAX_ADDED = 20
TEXT_MAX = 600
TITLE_MAX = 80
# Moments kept in the store after their day, then pruned.
KEEP_DAYS = 60

# Words that make a calendar entry a family occasion.
OCCASIONS = {
    "halloween", "parade", "recital", "concert", "play", "performance",
    "birthday", "bday", "graduation", "ceremony", "christmas", "hanukkah",
    "chanukah", "thanksgiving", "easter", "passover", "diwali", "eid",
    "kwanzaa", "communion", "christening", "baptism", "mitzvah", "game",
    "match", "tournament", "meet", "show", "pageant", "festival",
    "fair", "carnival", "school", "kindergarten",
    "preschool", "camp", "field", "picnic", "party", "celebration",
    "wedding", "anniversary", "reunion", "holiday", "first",
}
# Occasion words that only count beside another one or on a family
# calendar ("game" with a colleague, "school board meeting").
WEAK = {"game", "match", "meet", "show", "school", "first", "field", "fair",
        "party", "play", "camp", "holiday"}
# A calendar title that is a task about an occasion, not the occasion
# ("Work on: send the costume", "Buy recital flowers") never anchors one.
TASK = re.compile(
    r"^\s*(?:work on|todo|to do|remind(?:er)?|buy|order|send|bring|pack|pick ?up|drop ?off|"
    r"sign up|register|rsvp|book|call|email|text|pay|prep(?:are)?)\b", re.I)
GIFT = re.compile(
    r"\b(?:gifts?|presents?|package|parcel|little something|sent (?:you|her|him|them|the kids|"
    r"something)|costumes?|flowers|balloons|card for|check for)\b", re.I)
THANKS = re.compile(r"\bthank(?:s| you)\b.{0,40}\bfor\b", re.I)
STOP = {
    "a", "an", "the", "and", "or", "of", "to", "for", "with", "on", "in",
    "at", "by", "from", "my", "our", "day", "today", "s", "is", "am", "pm",
    "photos", "pictures", "pics",
}
_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _today():
    return _now().astimezone().date()


def _stamp():
    return _now().isoformat()


def _tokens(text):
    out = []
    for w in _WORD.findall(str(text or "").casefold()):
        w = w.strip("'-")
        if w.endswith("'s"):
            w = w[:-2]
        if w:
            out.append(w)
    return out


def _clean(value, limit, field):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field} needs 1-{limit} characters.")
    return value


def _key(*parts):
    raw = "|".join(" ".join(str(p or "").casefold().split()) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _first(name):
    return (str(name or "").split() or ["there"])[0]


# ------------------------------------------------------------------ store

def _empty():
    # Built fresh each call: a shared template would let nested buckets
    # accumulate across passes.
    return {"setup": {"sources": [], "relay": []}, "added": [], "moments": {}}


def _read():
    state = jsonstore.read(STORE, _empty())
    base = _empty()
    if isinstance(state, dict):
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


def _person(row, field):
    if not isinstance(row, dict):
        raise ValueError(f"Each {field} must be an object.")
    name = _clean(row.get("name"), 60, field.capitalize())
    pid = str(row.get("person_id") or "").strip()[:80]
    handle = str(row.get("handle") or "").strip()[:80]
    return {"name": name, "person_id": pid, "handle": handle}


def validate_setup(raw):
    """The one shape the setup is saved in; every field is checked."""
    if not isinstance(raw, dict):
        raise ValueError("The setup must be an object.")
    sources, seen = [], set()
    for row in raw.get("sources") or []:
        p = _person(row, "source")
        if not p["person_id"] and not p["handle"]:
            raise ValueError(f"{p['name']} needs a CRM person or a handle.")
        ident = p["person_id"] or p["handle"]
        if ident not in seen:
            seen.add(ident)
            sources.append(p)
    relay, seen = [], set()
    for row in raw.get("relay") or []:
        p = _person(row, "person")
        if not p["person_id"] and not p["handle"]:
            raise ValueError(f"{p['name']} needs a CRM person or a phone/iMessage handle.")
        ident = p["person_id"] or p["handle"]
        if ident in seen:
            continue
        seen.add(ident)
        p["relation"] = re.sub(r"\s+", " ", str(row.get("relation") or "")).strip()[:40]
        words = row.get("occasions") or []
        if isinstance(words, str):
            words = re.split(r"[,\s]+", words)
        p["occasions"] = sorted({w for w in (_tokens(" ".join(map(str, words)))) if w})[:MAX_WORDS]
        relay.append(p)
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"At most {MAX_SOURCES} photo sources.")
    if len(relay) > MAX_RELAY:
        raise ValueError(f"At most {MAX_RELAY} people on the relay list.")
    return {"sources": sources, "relay": relay}


def save_setup(raw):
    clean = validate_setup(raw)

    def apply(state):
        state["setup"] = clean
    _mutate(apply)
    _pending_cache.clear()
    return clean


def add_moment(title, day):
    title = _clean(title, TITLE_MAX, "A moment")
    try:
        day = dt.date.fromisoformat(str(day))
    except ValueError as e:
        raise ValueError("The date must be YYYY-MM-DD.") from e
    if day > _today():
        raise ValueError("A moment is a day that already happened.")
    mid = _key("added", day.isoformat(), title)

    def apply(state):
        added = [a for a in state["added"] if a.get("id") != mid]
        added.append({"id": mid, "title": title, "date": day.isoformat()})
        state["added"] = added[-MAX_ADDED:]
    _mutate(apply)
    _pending_cache.clear()
    return mid


# ---------- sources (seams the tests patch) ----------

def _calendar_events(day):
    """The brief's expanded local calendar occurrences for one local day."""
    from . import brief
    lo = dt.datetime.combine(day, dt.time.min)
    return brief._occurrences(lo, lo + dt.timedelta(days=1))


def _day_ns(day):
    from .imessage import apple_ns
    lo = dt.datetime.combine(day, dt.time.min).astimezone()
    return apple_ns(lo), apple_ns(lo + dt.timedelta(days=1))


def _media_items(day, pids):
    """Photos and videos shared that local day in a source's 1:1 thread or
    posted by a source anywhere, with what the media index knows about
    each. [] when the index does not exist yet."""
    from . import mediaindex
    if not pids or not mediaindex.DB.exists():
        return []
    lo, hi = _day_ns(day)
    marks = ",".join("?" * len(pids))
    con = mediaindex._db()
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"""SELECT i.seq, i.id, i.kind, i.mime, i.name, i.date_ns, i.from_me,
                       i.sender_pid, i.chat_pid,
                       COALESCE(c.ocr, '') AS ocr, COALESCE(c.caption, '') AS caption,
                       COALESCE(c.context, '') AS context,
                       (SELECT COUNT(*) FROM faces f WHERE f.seq = i.seq) AS faces,
                       (SELECT COUNT(*) FROM faces f WHERE f.seq = i.seq
                          AND f.person_id IS NOT NULL AND f.person_id != '') AS known,
                       v.v AS vec
                FROM items i
                LEFT JOIN content c ON c.seq = i.seq
                LEFT JOIN vec_scene v ON v.seq = i.seq
                WHERE i.kind IN ('photo', 'video') AND i.source = 'imessage'
                  AND COALESCE(i.purged, 0) = 0
                  AND i.date_ns >= ? AND i.date_ns < ?
                  AND (i.chat_pid IN ({marks}) OR i.sender_pid IN ({marks}))
                ORDER BY i.date_ns""",
            (lo, hi, *pids, *pids)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _messages(day_from, day_to, limit=GIFT_SCAN_LIMIT, q=None, person=None):
    """Indexed messages between two local dates (inclusive), newest first.
    An index that is not built yet answers []."""
    from . import textindex
    out = textindex.query_messages(
        q, limit=limit, person=person, since=day_from.isoformat(),
        until=(day_to + dt.timedelta(days=1)).isoformat(), order="recent")
    return out.get("rows") or []


def _people():
    from . import data as crm
    return crm._load()["by_id"]


def _send_text(text, person_id, handle):
    from . import send
    return send.send_message(text, person_id=person_id or None, handle=handle or None)


def _send_file(path, person_id, handle, channel):
    from . import send
    return send.send_file(path, person_id=person_id or None, handle=handle or None,
                          channel=channel)


def _file_for(att_id):
    """The bytes of an attachment: the original, else Vira's archived copy.
    None when neither is on this Mac."""
    from . import media
    recorded, _mime, name = media._resolve_media(att_id, allow_missing=True)
    path = media._bytes_path(att_id, recorded)
    if path is None or not Path(path).exists():
        return None, name
    return Path(path), name or Path(path).name


# ---------- moments ----------

def _occasion_words(title, family=False):
    words = [w for w in _tokens(title) if w in OCCASIONS]
    strong = [w for w in words if w not in WEAK]
    if strong or family or len(words) >= 2:
        return words or ["family"]
    return []


def _source_ids(setup):
    return [s["person_id"] or s["handle"] for s in setup["sources"]]


def _anchors(day, state, items):
    """The moments of one day: calendar occasions, owner-added ones, and a
    photo burst nobody put on the calendar."""
    out = []
    for ev in _calendar_events(day):
        if ev.get("birthday"):
            continue  # a contact's birthday, not a family event
        if TASK.search(ev.get("title") or ""):
            continue
        words = _occasion_words(ev.get("title"), ev.get("family"))
        if words:
            out.append({"id": _key("cal", day.isoformat(), ev["title"]), "title": ev["title"],
                        "date": day.isoformat(), "origin": "calendar",
                        "words": sorted(set(words) | _distinct(ev["title"]))})
    for a in state["added"]:
        if a.get("date") == day.isoformat():
            out.append({"id": a["id"], "title": a["title"], "date": a["date"],
                        "origin": "added",
                        "words": sorted(set(_occasion_words(a["title"], True)) | _distinct(a["title"]))})
    if not out and len(items) >= BURST_MIN:
        title, words = _burst_title(day, items)
        out.append({"id": _key("burst", day.isoformat()), "title": title,
                    "date": day.isoformat(), "origin": "photos", "words": words})
    return out


def _distinct(title):
    return {w for w in _tokens(title) if len(w) >= 3 and w not in STOP and not w.isdigit()}


def _burst_title(day, items):
    """A burst's title: the occasion its own messages name, else the day."""
    counts = {}
    for it in items:
        for w in _tokens(it.get("context")):
            if w in OCCASIONS and w not in WEAK:
                counts[w] = counts.get(w, 0) + 1
    if counts:
        word = max(sorted(counts), key=lambda w: counts[w])
        return word.capitalize(), [word]
    return "Photos from " + settings.strf(dt.datetime.combine(day, dt.time()), "%A"), []


def _vec(blob):
    try:
        import numpy as np
    except ImportError:
        return None
    if not blob:
        return None
    v = np.frombuffer(blob, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else None


def _score(it):
    """How good a shot this is to send, from what the index already knows."""
    s = 1.0
    s += 1.5 * min(int(it.get("faces") or 0), 3)
    s += 1.0 * min(int(it.get("known") or 0), 3)
    if (it.get("caption") or "").strip():
        s += 0.5
    if len((it.get("ocr") or "").strip()) > 40:
        s -= 3.0  # a screenshot, a flyer, a document
    mime = (it.get("mime") or "").lower()
    name = (it.get("name") or "").lower()
    if "gif" in mime or name.endswith(".gif") or "sticker" in name:
        s -= 2.5
    if it.get("kind") == "video":
        s -= 0.25  # heavier to send; a still says it as well
    return round(s, 2)


def rank(items):
    """Items best first, near-identical shots folded into the better one."""
    scored = sorted(items, key=lambda it: (-_score(it), it.get("date_ns") or 0))
    kept, vecs = [], []
    for it in scored:
        v = _vec(it.get("vec"))
        if v is not None and any(float(v @ k) >= NEAR_DUP for k in vecs if k.shape == v.shape):
            continue
        if v is not None:
            vecs.append(v)
        kept.append(it)
    return kept


def _shot(it):
    from .imessage import apple_dt
    when = apple_dt(it.get("date_ns"))
    return {"id": it["id"], "kind": it["kind"], "score": _score(it),
            "faces": int(it.get("faces") or 0),
            "when": when.isoformat() if when else None,
            "from_me": bool(it.get("from_me")),
            "caption": (it.get("caption") or "")[:160]}


# ---------- recipients ----------

def _fits(person, words):
    wanted = set(person.get("occasions") or [])
    return not wanted or bool(wanted & set(words))


def _gift_senders(moment, exclude):
    """People who sent a gift for this occasion, each with the message that
    says so. A gift message must also name one of the moment's own words."""
    words = set(moment["words"]) - WEAK - {"family"}
    if not words:
        return []
    day = dt.date.fromisoformat(moment["date"])
    try:
        rows = _messages(day - dt.timedelta(days=GIFT_LOOKBACK_DAYS), day,
                         q=" ".join(sorted(words)))
    except (ValueError, OSError, sqlite3.Error) as e:
        _errors.append(f"gift messages unreadable: {str(e)[:120]}")
        return []
    out = {}
    for m in rows:
        text = m.get("text") or ""
        if not (set(_tokens(text)) & words) or not GIFT.search(text):
            continue
        if m.get("from_me"):
            if m.get("is_group") or not THANKS.search(text):
                continue
        pid = m.get("person_id")
        if not pid or pid == "me" or pid in exclude or pid in out:
            continue
        out[pid] = {"person_id": pid, "name": m.get("person") or "", "handle": "",
                    "why": "sent a gift", "quote": text[:200],
                    "quote_when": m.get("when"), "gift": True}
    return list(out.values())


def _phrase(moment):
    """The moment as the object of "a few from ...": "the Halloween parade",
    or "the day" for a burst nobody named."""
    if moment.get("origin") == "photos" and not moment.get("words"):
        return "the day"
    title = moment["title"]
    first = (title.split() or [""])[0].casefold()
    if first in {"the", "a", "an", "my", "our", "his", "her", "their"} or first.endswith("'s"):
        return title
    return "the " + title


def _draft(person, moment):
    first = _first(person.get("name"))
    what = _phrase(moment)
    if person.get("gift"):
        return (f"{first}, thank you again for the gift! Here are a few from "
                f"{what} - it made the day.")
    return f"Hi {first}! A few from {what} - thought you'd love these."


def _recipients(moment, setup, people):
    """The relay list's people whose words fit, then the gift senders. A
    relay person who also sent a gift keeps one row, with the thank-you."""
    exclude = set(_source_ids(setup)) | {"me"}
    gifts = {g["person_id"]: g for g in _gift_senders(moment, exclude)}
    out = []
    for p in setup["relay"]:
        if not _fits(p, moment["words"]):
            continue
        row = {"person_id": p["person_id"], "name": p["name"], "handle": p["handle"],
               "why": p.get("relation") or "on your relay list", "gift": False}
        g = gifts.pop(p["person_id"], None) if p["person_id"] else None
        if g:
            row.update(gift=True, quote=g["quote"], quote_when=g["quote_when"],
                       why=row["why"] + " - sent a gift")
        out.append(row)
    for g in gifts.values():
        g["name"] = g["name"] or (people.get(g["person_id"]) or {}).get("name") or "Someone"
        out.append(g)
    for r in out:
        r["key"] = r["person_id"] or r["handle"]
        r["draft"] = _draft(r, moment)
    return out


def _moment_view(moment, items, setup, people, saved):
    shots = [_shot(it) for it in rank(items)[:MAX_CANDIDATES]]
    best = [s["id"] for s in shots if s["kind"] == "photo"][:BEST_PICKS] or \
        [s["id"] for s in shots][:BEST_PICKS]
    recips = _recipients(moment, setup, people)
    rstate = saved.get("recipients") or {}
    for r in recips:
        own = rstate.get(r["key"]) or {}
        r["text"] = own.get("text") or r["draft"]
        r["picks"] = [p for p in own.get("picks", best) if p in {s["id"] for s in shots}] \
            if "picks" in own else list(best)
        r["sent_at"] = own.get("sent_at") or ""
        r["skipped"] = bool(own.get("skipped"))
        r["result"] = own.get("result") or None
    waiting = [r for r in recips if not r["sent_at"] and not r["skipped"]]
    return dict(moment, shots=shots, best=best, total_shots=len(items),
                recipients=recips, waiting=len(waiting),
                dismissed=bool(saved.get("dismissed")))


# Why a source read came back empty, for the window's status line; reset
# by each status() so a fixed index stops being reported.
_errors: list = []


def day_view(day=None):
    """Every moment of one local day, ready for the window."""
    day = day or _today()
    state = _read()
    setup = state["setup"]
    try:
        items = _media_items(day, _source_ids(setup))
    except (sqlite3.Error, OSError) as e:
        _errors.append(f"media index unreadable: {str(e)[:120]}")
        items = []
    people = _people()
    return [_moment_view(m, items, setup, people, state["moments"].get(m["id"]) or {})
            for m in _anchors(day, state, items)]


def status(day=None):
    day = day or _today()
    state = _read()
    _errors.clear()
    days = []
    for back in range(PENDING_DAYS + 1):
        d = day - dt.timedelta(days=back)
        days.append({"date": d.isoformat(), "moments": day_view(d)})
    return {"date": day.isoformat(), "setup": state["setup"], "days": days,
            "best_picks": BEST_PICKS, "stage_dir": str(STAGE_DIR),
            "media_index": _index_ready(), "errors": sorted(set(_errors))}


def _index_ready():
    from . import mediaindex
    return mediaindex.DB.exists()


def _find(moment_id, day):
    for m in day_view(day):
        if m["id"] == moment_id:
            return m
    raise KeyError(moment_id)


def _moment_day(moment_id, date):
    try:
        day = dt.date.fromisoformat(str(date))
    except ValueError as e:
        raise ValueError("The moment's date must be YYYY-MM-DD.") from e
    if day > _today():
        raise ValueError("That day has not happened yet.")
    return day


def _save_recipient(moment_id, date, key, **fields):
    def apply(state):
        m = state["moments"].setdefault(moment_id, {"date": date, "recipients": {}})
        m["date"] = date
        r = m.setdefault("recipients", {}).setdefault(key, {})
        r.update(fields)
        _prune(state)
    _mutate(apply)
    _pending_cache.clear()


def _prune(state):
    cutoff = (_today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["moments"] = {k: v for k, v in state["moments"].items()
                        if str(v.get("date") or "") >= cutoff}
    state["added"] = [a for a in state["added"] if str(a.get("date") or "") >= cutoff]


def update_recipient(moment_id, date, key, text=None, picks=None, skipped=None):
    """Keep the owner's edits to one person's message: the text, the picks,
    or a skip."""
    day = _moment_day(moment_id, date)
    m = _find(moment_id, day)
    r = next((r for r in m["recipients"] if r["key"] == key), None)
    if r is None:
        raise KeyError(key)
    fields = {}
    if text is not None:
        fields["text"] = _clean(text, TEXT_MAX, "The message")
    if picks is not None:
        known = {s["id"] for s in m["shots"]}
        fields["picks"] = [int(p) for p in picks if int(p) in known][:MAX_SEND_FILES]
    if skipped is not None:
        fields["skipped"] = bool(skipped)
    _save_recipient(moment_id, day.isoformat(), key, **fields)
    return _find(moment_id, day)


def dismiss(moment_id, date, restore=False):
    day = _moment_day(moment_id, date)

    def apply(state):
        m = state["moments"].setdefault(moment_id, {"date": day.isoformat(), "recipients": {}})
        m["dismissed"] = not restore
        _prune(state)
    _mutate(apply)
    _pending_cache.clear()
    return {"ok": True, "dismissed": not restore}


def _stage(path, moment_id):
    """Copy a file where Messages can attach it from; prune old copies."""
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - STAGE_KEEP_DAYS * 86400
    for old in STAGE_DIR.glob("*"):
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    target = STAGE_DIR / f"{moment_id[:8]}-{path.name}"
    if not target.exists():
        shutil.copy2(path, target)
    return target


def send(moment_id, date, key, text=None, picks=None):
    """Send one person their message and shots. Records the send so the same
    person is never sent the same moment twice."""
    day = _moment_day(moment_id, date)
    m = _find(moment_id, day)
    r = next((r for r in m["recipients"] if r["key"] == key), None)
    if r is None:
        raise KeyError(key)
    if r["sent_at"]:
        raise ValueError(f"{r['name']} was already sent this moment.")
    text = _clean(text if text is not None else r["text"], TEXT_MAX, "The message")
    known = {s["id"] for s in m["shots"]}
    chosen = [int(p) for p in (picks if picks is not None else r["picks"])
              if int(p) in known][:MAX_SEND_FILES]
    files, missing = [], []
    for att in chosen:
        path, name = _file_for(att)
        if path is None:
            missing.append(name or str(att))
        else:
            files.append(path)
    res = _send_text(text, r["person_id"], r["handle"])
    if res.get("ok") is False:
        raise RuntimeError(res.get("note") or "Messages did not deliver the text.")
    sent, failed = 0, []
    for path in files:
        try:
            _send_file(_stage(path, moment_id), r["person_id"], res.get("handle") or r["handle"],
                       res.get("channel"))
            sent += 1
        except (RuntimeError, OSError) as e:
            failed.append(f"{path.name}: {str(e)[:120]}")
    notes = [res.get("note")] if res.get("note") else []
    if missing:
        notes.append(f"{len(missing)} not on this Mac (offloaded to iCloud): " + ", ".join(missing[:3]))
    if failed:
        notes.append(f"{len(failed)} did not attach: " + "; ".join(failed[:2]))
    result = {"ok": True, "handle": res.get("handle"), "channel": res.get("channel"),
              "files": sent, "missing": len(missing), "failed": len(failed),
              "note": " ".join(notes) or None}
    _save_recipient(moment_id, day.isoformat(), key, text=text, picks=chosen,
                    sent_at=_stamp(), result=result)
    return result


# ---------- Decide queue ----------

_pending_cache: dict = {}


def pending():
    """Moments of the last few days with people still waiting, newest first.
    Cached briefly for the queue; a failed read raises and is not cached."""
    hit = _pending_cache.get("value")
    if hit and time.monotonic() - hit[0] < PENDING_CACHE_S:
        return hit[1]
    state = _read()
    if not state["setup"]["sources"] and not state["setup"]["relay"]:
        return []
    out = []
    today = _today()
    for back in range(PENDING_DAYS + 1):
        for m in day_view(today - dt.timedelta(days=back)):
            if m["waiting"] and m["shots"] and not m["dismissed"]:
                out.append(m)
    _pending_cache["value"] = (time.monotonic(), out)
    return out

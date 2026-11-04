"""Reading-room pages: which rooms exist and what a Reader tab shows for one.

Reading-room pages are personal-layer pages (never committed) that render
consumption queues - lists of things to watch, listen to or read. Rooms are
store-native since 2026-07-27 (server/readingroom.py); a legacy page still on
disk under static/reading/ is listed beside them until it is migrated.

There is no read state here. Done-marks were removed on 2026-11-04 (owner's
call): having read something does not mean it is finished with, so every
item stays exactly as available as the day it arrived.
"""
import json
import re
from pathlib import Path

PAGES_DIR = Path(__file__).resolve().parent.parent / "static" / "reading"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
SUBTITLE_RE = re.compile(r"<header>.*?<p>(.*?)</p>", re.I | re.S)
BUILT_RE = re.compile(r"Built (\d{4}-\d{2}-\d{2})")
# Two generations of room page embed their items differently: the generator
# (server/readingroom.py) writes `window.DATA=[...]`, the pre-generator
# hand-built room writes `const DATA = [...]`. Both are one JSON array, so the
# marker just locates the `[` and json handles the rest.
DATA_RE = re.compile(r"(?:window\.DATA\s*=|const DATA\s*=)\s*(\[)")


def list_pages():
    """Every reading room: store-native ones first (the canonical kind since
    2026-07-27), then any legacy page still on disk that has not been
    migrated. The url stays /reading/<slug>.html for both — a native room's
    is served as an on-demand export from the store.

    The Reader launcher calls this to decide whether to show itself at all
    and what to list."""
    from . import readingroom               # lazy: readingroom imports us too
    pages = []
    seen = set()
    for r in readingroom.list_rooms():
        seen.add(r["slug"])
        pages.append({"name": r["slug"], "title": r["title"],
                      "url": f"/reading/{r['slug']}.html", "native": True})
    try:
        files = sorted(PAGES_DIR.glob("*.html"))
    except OSError:
        return pages
    for p in files:
        if p.stem in seen:
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        m = TITLE_RE.search(head)
        title = " ".join(m.group(1).split()) if m else p.stem
        pages.append({"name": p.stem, "title": title,
                      "url": f"/reading/{p.name}", "native": False})
    return pages


def _room_items(text):
    """The item list embedded in a room page, or None when it cannot be read.

    None (unparseable) and [] (genuinely empty) are different answers: a card
    facing an unparseable page should omit the count, not claim zero."""
    m = DATA_RE.search(text)
    if not m:
        return None
    try:
        items, _ = json.JSONDecoder().raw_decode(text, m.start(1))
    except (ValueError, TypeError):
        return None
    return items if isinstance(items, list) else None


def page_details():
    """list_pages() plus what a card needs: subtitle, item count, and the date the room was last built/refreshed. A native room answers
    straight from its store; a legacy page is parsed best-effort — a field a
    page does not yield is simply absent from the row, never an exception."""
    from . import readingroom
    out = []
    for row in list_pages():
        if row.get("native"):
            room = readingroom.load_room(row["name"]) or {"items": []}
            row["subtitle"] = room.get("subtitle", "")
            row["built"] = (room.get("updated") or "")[:10] \
                or room.get("built", "")
            row["items"] = len(room["items"])
            out.append(row)
            continue
        p = PAGES_DIR / f"{row['name']}.html"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out.append(row)
            continue
        m = SUBTITLE_RE.search(text[:4096])
        if m:
            row["subtitle"] = " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())[:300]
        m = BUILT_RE.search(text)
        if m:
            row["built"] = m.group(1)
        items = _room_items(text)
        if items is not None:
            row["items"] = len(items)
        out.append(row)
    return out


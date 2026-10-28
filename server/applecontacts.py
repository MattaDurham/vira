"""Apple Contacts write-back - the Apple spoke of the CRM sync engine.

Contact-card saves, and the Sync tab's passes (server/contactsync.py),
flow through to the matching card(s) in macOS Contacts.app, which
iCloud/CardDAV-syncs to the owner's phone - so an edit made in Vira is
an edit made everywhere, not an edit made in one database the phone
never reads (the gap that prompted this, 2026-07-24: a contact's
employer was corrected in Vira while their phone card kept the old one).

Design per the 2026-07-22 engine session: the CRM is the hub and owns
every field; this module is the Apple SPOKE - AppleScript against
Contacts.app, never raw abcddb writes. The June 2026 dedup campaign
proved the mechanism (crm/scripts/dedup_apply.py: person-id addressing,
make-new-email, set-organization, save).

What it pushes (widened 2026-10-28 on the owner's "all the content"
rule): company, job title, birthday, the email/phone lists - ORDER and
LABELS - a postal address when the card has none and the CRM knows a
location, URLs from the card's custom rows, and the NOTE. The note
carries a Vira block between `=== Vira ===` markers holding every card
field and every profile section (relationship, how we met, summary,
facts, topics, hooks, open loops, style, cadence, last exchange). Text
outside the markers is the phone's own note and is never touched; the
first merge over such text is reported (`foreign_note`) so the Sync tab
can flag the card for the owner to read. A scalar field whose phone
value differs from the CRM's is set to the CRM value and the phone's
value is kept in the block under "Previously on phone" (carried
forward on every later rewrite), so nothing is lost and the conflict is
reported for review. The handle order is the card's rank made visible:
primary first, archived last wearing an "old" label; `use` maps
personal->home, work->work.

What it never does: DELETE an address, URL or note text that exists
only on the Apple card (deletes never auto-propagate - the engine rule),
or touch the photo. Creating a card is a separate, explicit act
(`create_card`), used by the Sync tab for CRM people who have no card;
a plain push still targets existing matched cards only.

Matching: the AddressBook sqlite stores are scanned read-only (the
photos.py discovery pattern); a card matches when it shares a
normalized handle with the person. Every matched card gets the same
push, so a person carded in several stores stays consistent in the
unified view the phone renders. `load_cards` reads every store once
with notes, addresses, URLs, titles, birthdays and creation dates, so a
whole-CRM pass costs one scan instead of one per person.

Gotchas, learned live 2026-07-24:
- Contacts.app must be RUNNING. `tell application "Contacts"` from a
  sandboxed server context cannot auto-launch it (error -600);
  _ensure_running uses `open -g -j -a Contacts` (background, hidden)
  and retries once.
- Birthdays are assembled from date components (set year/month/day on
  a `current date` copy) - `date "..."` string literals parse by the
  system locale and are not portable across machines.
- Email/phone REORDERING is a full rewrite of the list (delete every
  entry, re-add in order). That is why the plan is computed as a pure
  union first - the rewrite must re-add every Apple-side address, or a
  reorder would silently become a delete.
- A multi-line note is passed as `"line" & linefeed & "line"`: a raw
  newline inside an AppleScript string literal survives osascript, but
  a carriage return does not, and the join form is unambiguous.
"""

import datetime
import json
import os
import re
import sqlite3
import subprocess
import time
from glob import glob
from pathlib import Path

from . import settings

OSA_TIMEOUT = 30
DATA = Path(__file__).resolve().parent.parent / "data"
# Apple's built-in labels arrive from sqlite wrapped like _$!<Home>!$_ -
# unwrap for re-use; anything else is a custom label and passes through.
_LABEL_WRAP = re.compile(r"^_\$!<(.+)>!\$_$")
ARCHIVED_LABEL = "old"
_USE_LABEL = {"personal": "home", "work": "work"}
# Core Data stores dates as seconds since 2001-01-01 UTC.
MAC_EPOCH = 978307200

NOTE_BEGIN = "=== Vira ==="
NOTE_END = "=== end Vira ==="
PREVIOUS_PREFIX = "Previously on phone: "
# The block is read on a phone screen: 6,000 characters is several
# screens of one contact's note, and keeps the per-card AppleScript far
# below the argument limit osascript -e tolerates. A longer dossier ends
# with a pointer back to Vira instead of being cut mid-sentence.
NOTE_CAP = 6000


def enabled():
    """The push runs only on a real, owner-mode Mac that asked for it."""
    if not settings.IS_MAC:
        return False
    if os.environ.get("VIRA_SANDBOX"):
        return False
    return bool(settings.get("apple_contacts_push"))


# ---------- reading the Apple side (sqlite, read-only) ----------

def _stores():
    home = Path.home()
    base = home / "Library/Application Support/AddressBook"
    paths = [base / "AddressBook-v22.abcddb"]
    paths += [Path(p) for p in glob(str(base / "Sources/*/AddressBook-v22.abcddb"))]
    return [p for p in paths if p.exists()]


def _norm_email(v):
    return (v or "").strip().lower()


def _norm_phone(v):
    d = re.sub(r"\D", "", v or "")
    return d[-10:] if len(d) >= 10 else d


def _unwrap_label(label):
    if not label:
        return ""
    m = _LABEL_WRAP.match(label)
    return (m.group(1) if m else label).strip().lower()


def _tables(con):
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _cols(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _grouped(con, tables, table, value_col):
    """{owner: [{value, label}]} in Z_PK order; {} when the store lacks
    the table (a fixture, an older schema)."""
    out = {}
    if table not in tables:
        return out
    try:
        for r in con.execute(f"SELECT ZOWNER, {value_col}, ZLABEL, Z_PK "
                             f"FROM {table} ORDER BY Z_PK"):
            out.setdefault(r[0], []).append(
                {"value": (r[1] or "").strip(), "label": _unwrap_label(r[2])})
    except sqlite3.Error:
        pass
    return out


def _mac_seconds(v):
    try:
        return float(v) + MAC_EPOCH if v is not None else None
    except (TypeError, ValueError):
        return None


def _mac_ymd(v):
    """A Core Data date -> (year, month, day), read in UTC: Contacts stores
    a birthday as a date-only value pinned to UTC, so the UTC calendar day
    is the one the owner typed."""
    s = _mac_seconds(v)
    if s is None:
        return None
    try:
        d = datetime.datetime.fromtimestamp(s, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return (d.year, d.month, d.day)


def load_cards(stores=None):
    """Every person card in the local AddressBook stores, read-only:
    [{uid, store, pk, first, last, name, org, title, nickname, note,
    birthday (y, m, d) or None, created, modified (epoch seconds or None),
    emails/phones/urls [{value, label}], addresses [{label, street, city,
    state, zip, country}]}]. Never raises on a store it cannot open, and
    tolerates stores that lack the optional columns and tables."""
    cards = []
    for db in (stores if stores is not None else _stores()):
        con = None
        people = []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            tables = _tables(con)
            cols = _cols(con, "ZABCDRECORD")
            optional = ["ZFIRSTNAME", "ZLASTNAME", "ZJOBTITLE", "ZNICKNAME",
                        "ZBIRTHDAY", "ZCREATIONDATE", "ZMODIFICATIONDATE"]
            sel = ["Z_PK", "ZUNIQUEID", "ZORGANIZATION"] + \
                  [c for c in optional if c in cols]
            people = con.execute(
                f"SELECT {', '.join(sel)} FROM ZABCDRECORD "
                "WHERE ZUNIQUEID LIKE '%:ABPerson'").fetchall()
            emails_by = _grouped(con, tables, "ZABCDEMAILADDRESS", "ZADDRESS")
            phones_by = _grouped(con, tables, "ZABCDPHONENUMBER", "ZFULLNUMBER")
            urls_by = _grouped(con, tables, "ZABCDURLADDRESS", "ZURL")
            notes_by = {}
            if "ZABCDNOTE" in tables:
                for r in con.execute("SELECT ZCONTACT, ZTEXT FROM ZABCDNOTE"):
                    if r[1] and str(r[1]).strip():
                        notes_by[r[0]] = str(r[1])
            addr_by = {}
            if "ZABCDPOSTALADDRESS" in tables:
                for r in con.execute(
                        "SELECT ZOWNER, ZLABEL, ZSTREET, ZCITY, ZSTATE, "
                        "ZZIPCODE, ZCOUNTRYNAME FROM ZABCDPOSTALADDRESS "
                        "ORDER BY Z_PK"):
                    addr_by.setdefault(r[0], []).append({
                        "label": _unwrap_label(r[1]),
                        "street": (r[2] or "").strip(),
                        "city": (r[3] or "").strip(),
                        "state": (r[4] or "").strip(),
                        "zip": (r[5] or "").strip(),
                        "country": (r[6] or "").strip()})
        except sqlite3.Error:
            continue
        finally:
            if con is not None:
                con.close()
        for p in people:
            keys = p.keys()
            first = (p["ZFIRSTNAME"] if "ZFIRSTNAME" in keys else "") or ""
            last = (p["ZLASTNAME"] if "ZLASTNAME" in keys else "") or ""
            org = p["ZORGANIZATION"] or ""
            name = " ".join(x.strip() for x in (first, last) if x.strip()) \
                or org.strip()
            cards.append({
                "uid": p["ZUNIQUEID"], "store": db.parent.name, "pk": p["Z_PK"],
                "first": first.strip(), "last": last.strip(), "name": name,
                "org": org,
                "title": ((p["ZJOBTITLE"] if "ZJOBTITLE" in keys else "") or "").strip(),
                "nickname": ((p["ZNICKNAME"] if "ZNICKNAME" in keys else "") or "").strip(),
                "note": notes_by.get(p["Z_PK"], ""),
                "birthday": _mac_ymd(p["ZBIRTHDAY"]) if "ZBIRTHDAY" in keys else None,
                "created": _mac_seconds(p["ZCREATIONDATE"]) if "ZCREATIONDATE" in keys else None,
                "modified": _mac_seconds(p["ZMODIFICATIONDATE"]) if "ZMODIFICATIONDATE" in keys else None,
                "emails": emails_by.get(p["Z_PK"], []),
                "phones": phones_by.get(p["Z_PK"], []),
                "urls": urls_by.get(p["Z_PK"], []),
                "addresses": addr_by.get(p["Z_PK"], []),
            })
    return cards


def index_cards(cards):
    """{"email": {norm: [i]}, "phone": {norm: [i]}} over `cards`."""
    idx = {"email": {}, "phone": {}}
    for i, c in enumerate(cards):
        for e in c.get("emails", []):
            k = _norm_email(e["value"])
            if k:
                idx["email"].setdefault(k, []).append(i)
        for ph in c.get("phones", []):
            k = _norm_phone(ph["value"])
            if k:
                idx["phone"].setdefault(k, []).append(i)
    return idx


def match_cards(cards, idx, emails, phones10):
    """The cards sharing a normalized handle with the person, in store
    order, each once."""
    hits = set()
    for e in emails or []:
        hits.update(idx["email"].get(_norm_email(e), []))
    for p in phones10 or []:
        hits.update(idx["phone"].get(_norm_phone(p), []))
    return [cards[i] for i in sorted(hits)]


def find_cards(emails, phones10):
    """Every Apple card sharing a handle with the person. Reads the stores
    read-only; never raises on a store it cannot open."""
    cards = load_cards()
    return match_cards(cards, index_cards(cards), emails, phones10)


# ---------- the plan (pure, unit-tested) ----------

def plan_handles(rows, existing, kind):
    """The rewritten list for one kind: [(value, label)] in final order.

    `rows` are the card's handle rows (contactcard._handle_rows shape,
    already rank-sorted: primary, secondary, unranked, archived last);
    `existing` is the Apple card's current list [{value, label}]. Rules:
    - CRM rows lead, in rank order; archived rows sink to the end of the
      CRM block wearing the "old" label.
    - `use` maps to home/work for active rows; a row with no use keeps
      the label the Apple card already had (or none).
    - An address only the Apple card knows is APPENDED with its label -
      never dropped. Deletes do not propagate.
    """
    norm = _norm_email if kind == "email" else _norm_phone
    have = {}
    for h in existing:
        have.setdefault(norm(h["value"]), h)
    out, used = [], set()
    active = [r for r in rows if r["rank"] != "archived"]
    archived = [r for r in rows if r["rank"] == "archived"]
    for r in active + archived:
        key = norm(r["value"])
        if not key or key in used:
            continue
        used.add(key)
        ex = have.get(key)
        # Prefer the Apple card's own spelling/formatting of the value.
        value = ex["value"] if ex else r["value"]
        if r["rank"] == "archived":
            label = ARCHIVED_LABEL
        else:
            label = _USE_LABEL.get(r["use"]) or (ex["label"] if ex else "")
        out.append((value, label))
    for h in existing:
        key = norm(h["value"])
        if key and key not in used:
            used.add(key)
            out.append((h["value"], h["label"]))
    return out


def _same_handles(planned, existing, kind):
    """Nothing to write when every planned value is on the card with the
    planned label. ORDER is not compared: Contacts stores a rewritten list
    in its own order, so comparing order made every pass rewrite the same
    cards (seen live 2026-10-28, 223 of 916). A planned empty label means
    "no opinion" and matches whatever label the card gives the entry."""
    norm = _norm_email if kind == "email" else _norm_phone
    cur = {}
    for h in existing:
        cur.setdefault(norm(h["value"]), (h["label"] or "").lower())
    if len(cur) != len({norm(v) for v, _ in planned}):
        return False
    for v, l in planned:
        k = norm(v)
        if k not in cur:
            return False
        if l and l.lower() != cur[k]:
            return False
    return True


def split_note(text):
    """(foreign, block): the phone's own text outside the Vira markers,
    and the current block body (None when the note has no block)."""
    text = text or ""
    i = text.find(NOTE_BEGIN)
    if i < 0:
        return text.strip(), None
    j = text.find(NOTE_END, i)
    if j < 0:
        inner = text[i + len(NOTE_BEGIN):]
        foreign = text[:i]
    else:
        inner = text[i + len(NOTE_BEGIN):j]
        foreign = text[:i] + text[j + len(NOTE_END):]
    return foreign.strip(), inner.strip("\n")


def merge_note(foreign, block):
    """The phone's own text first, then the block."""
    body = f"{NOTE_BEGIN}\n{block}\n{NOTE_END}"
    return f"{foreign}\n\n{body}" if foreign else body


def _lines(value):
    return [ln.strip() for ln in str(value or "").splitlines() if ln.strip()]


def _item_text(it, keys):
    if isinstance(it, dict):
        parts = [str(it.get(k)).strip() for k in keys
                 if it.get(k) not in (None, "", [], {})]
        return parts
    return [str(it).strip()] if it not in (None, "") else []


def note_block(card, detail, *, foreign=False, previous=()):
    """The Vira block for one person: every card field and every profile
    section, in a fixed order with no timestamps, so an unchanged person
    yields an identical block and no rewrite. `previous` are the
    "Previously on phone" lines to carry: the ones already in the card's
    block plus any new conflict found in this pass."""
    fields = {f["key"]: f["value"] for f in (card or {}).get("fields", [])}
    prof = (detail or {}).get("profile") or {}
    master = (detail or {}).get("master") or {}
    out = []
    if foreign:
        out.append("Phone note kept above.")
    name = fields.get("display_name") or ""
    if fields.get("pronouns"):
        name = f"{name} ({fields['pronouns']})".strip()
    if name:
        out.append(f"Name: {name}")
    rel = fields.get("relationship") or prof.get("relationship_class") \
        or master.get("relationship") or ""
    if rel:
        out.append(f"Relationship: {rel}")
    for key, label in (("title", "Title"), ("company", "Company"),
                       ("location", "Location"), ("birthday", "Birthday"),
                       ("note", "Card note")):
        if fields.get(key):
            out.append(f"{label}: {fields[key]}")
    for c in (card or {}).get("custom", []):
        if c.get("label") and c.get("value"):
            out.append(f"{c['label']}: {c['value']}")
    if prof.get("how_we_met"):
        out.append(f"How we met: {' '.join(_lines(prof['how_we_met']))}")
    if prof.get("relationship_summary"):
        out.append(f"Summary: {' '.join(_lines(prof['relationship_summary']))}")
    facts = [it for it in (prof.get("personal_facts") or []) if it]
    if facts:
        out.append("Facts:")
        for it in facts:
            parts = _item_text(it, ("fact",))
            if not parts:
                continue
            asof = it.get("as_of") if isinstance(it, dict) else None
            out.append(f"- {parts[0]}" + (f" (as of {asof})" if asof else ""))
    topics = []
    for it in prof.get("topics") or []:
        if isinstance(it, dict) and it.get("topic"):
            t = str(it["topic"]).strip()
            if it.get("status"):
                t += f" ({it['status']})"
            topics.append(t)
        elif isinstance(it, str) and it.strip():
            topics.append(it.strip())
    if topics:
        out.append("Topics: " + "; ".join(topics))
    hooks = [it for it in (prof.get("hooks") or []) if it]
    if hooks:
        out.append("Hooks:")
        for it in hooks:
            parts = _item_text(it, ("angle", "detail"))
            if parts:
                out.append("- " + ": ".join(parts[:2]))
    loops = [it for it in (prof.get("open_loops") or []) if it]
    if loops:
        out.append("Open loops:")
        for it in loops:
            parts = _item_text(it, ("what",))
            if not parts:
                continue
            tail = []
            if isinstance(it, dict):
                if it.get("owed_by"):
                    tail.append(f"owed by {it['owed_by']}")
                if it.get("since"):
                    tail.append(f"since {it['since']}")
            out.append(f"- {parts[0]}" + (f" ({', '.join(tail)})" if tail else ""))
    if prof.get("comms_style"):
        out.append(f"Style: {' '.join(_lines(prof['comms_style']))}")
    cad = prof.get("cadence") or {}
    last = prof.get("last_exchange") or {}
    bits = []
    if isinstance(cad, dict) and cad.get("typical_gap_days"):
        try:
            bits.append(f"typically every {int(round(float(cad['typical_gap_days'])))} days")
        except (TypeError, ValueError):
            pass
    if isinstance(last, dict) and last.get("date"):
        s = f"last exchange {str(last['date'])[:10]}"
        if last.get("channel"):
            s += f" via {last['channel']}"
        if last.get("summary"):
            s += f": {' '.join(_lines(last['summary']))}"
        bits.append(s)
    if bits:
        out.append("Cadence: " + "; ".join(bits))
    seen = set()
    for ln in previous:
        ln = ln.strip()
        if not ln:
            continue
        if not ln.startswith(PREVIOUS_PREFIX):
            ln = PREVIOUS_PREFIX + ln
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    text = "\n".join(out)
    if len(text) > NOTE_CAP:
        text = text[:NOTE_CAP].rsplit("\n", 1)[0] + "\n(more in Vira)"
    return text


def _previous_lines(block):
    return [ln.strip() for ln in (block or "").splitlines()
            if ln.strip().startswith(PREVIOUS_PREFIX)]


def _address_parts(location):
    """"City, ST" or "City, State" -> {city, state}; anything else is the
    city line as typed."""
    loc = (location or "").strip()
    if not loc:
        return None
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(parts) >= 2:
        return {"city": parts[0], "state": ", ".join(parts[1:])}
    return {"city": loc, "state": ""}


_URL_RX = re.compile(r"^https?://", re.I)


def _norm_url(u):
    return (u or "").strip().lower().rstrip("/")


def card_plan(card, detail, apple):
    """What one matched Apple card needs to read like the CRM: {kw (the
    build_script kwargs), changed [field names], conflicts [{field, crm,
    phone}], foreign_note (bool: this write merges over the phone's own
    note for the first time)}. Pure; never writes."""
    fields = {f["key"]: f["value"] for f in card.get("fields", [])}
    rows = card.get("handles", [])
    kw, changed, conflicts = {}, [], []
    company = fields.get("company") or ""
    if company and company != (apple.get("org") or ""):
        kw["company"] = company
        changed.append("company")
        if apple.get("org"):
            conflicts.append({"field": "company", "crm": company,
                              "phone": apple["org"]})
    title = fields.get("title") or ""
    if title and title != (apple.get("title") or ""):
        kw["title"] = title
        changed.append("title")
        if apple.get("title"):
            conflicts.append({"field": "title", "crm": title,
                              "phone": apple["title"]})
    bday = _birthday_parts(fields.get("birthday"))
    if bday and bday != apple.get("birthday"):
        kw["birthday"] = bday
        changed.append("birthday")
        if apple.get("birthday"):
            y, mo, d = apple["birthday"]
            conflicts.append({"field": "birthday", "crm": fields.get("birthday"),
                              "phone": f"{mo}/{d}/{y}"})
    eplan = plan_handles([r for r in rows if r["kind"] == "email"],
                         apple.get("emails", []), "email")
    if not _same_handles(eplan, apple.get("emails", []), "email"):
        kw["emails"] = eplan
        changed.append("emails")
    pplan = plan_handles([r for r in rows if r["kind"] == "phone"],
                         apple.get("phones", []), "phone")
    if not _same_handles(pplan, apple.get("phones", []), "phone"):
        kw["phones"] = pplan
        changed.append("phones")
    addr = _address_parts(fields.get("location"))
    if addr and not apple.get("addresses"):
        kw["address"] = addr
        changed.append("address")
    have_urls = {_norm_url(u["value"]) for u in apple.get("urls", [])}
    add_urls = []
    for c in card.get("custom", []):
        v = (c.get("value") or "").strip()
        if _URL_RX.match(v) and _norm_url(v) not in have_urls:
            have_urls.add(_norm_url(v))
            add_urls.append((c.get("label") or "url", v))
    if add_urls:
        kw["urls"] = add_urls
        changed.append("urls")
    foreign, cur_block = split_note(apple.get("note") or "")
    previous = _previous_lines(cur_block) + [
        f'{PREVIOUS_PREFIX}{c["field"]} "{c["phone"]}"' for c in conflicts]
    block = note_block(card, detail, foreign=bool(foreign), previous=previous)
    new_note = merge_note(foreign, block)
    foreign_note = False
    if new_note != (apple.get("note") or "").strip():
        kw["note"] = new_note
        changed.append("note")
        foreign_note = bool(foreign) and cur_block is None
    return {"kw": kw, "changed": changed, "conflicts": conflicts,
            "foreign_note": foreign_note}


# ---------- the AppleScript ----------

def _esc(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"') \
                    .replace("\n", " ").replace("\r", " ").strip()


def _lit(text):
    """A multi-line AppleScript string literal."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return " & linefeed & ".join(f'"{_esc(ln)}"' for ln in lines) or '""'


def _birthday_parts(value):
    """The card's birthday string -> (year, month, day) or None. Accepts
    M/D/YYYY (the card's placeholder format) and YYYY-MM-DD."""
    v = (value or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", v)
    if m:
        return int(m.group(3)), int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", v)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _field_lines(target, *, company=None, title=None, birthday=None,
                 emails=None, phones=None, note=None, address=None,
                 urls=None, rewrite_lists=True):
    lines = []
    if company is not None:
        lines.append(f'set organization of {target} to "{_esc(company)}"')
    if title is not None:
        lines.append(f'set job title of {target} to "{_esc(title)}"')
    if birthday is not None:
        y, mo, d = birthday
        lines += ["set bd to current date",
                  f"set year of bd to {y}",
                  f"set month of bd to {_MONTHS[mo - 1]}",
                  f"set day of bd to {d}",
                  "set time of bd to 0",
                  f"set birth date of {target} to bd"]
    for prop, plan in (("email", emails), ("phone", phones)):
        if plan is None:
            continue
        if rewrite_lists:
            lines.append(f"delete every {prop} of {target}")
        for value, label in plan:
            props = f'{{value:"{_esc(value)}"}}' if not label else \
                f'{{label:"{_esc(label)}", value:"{_esc(value)}"}}'
            lines.append(f"make new {prop} at end of {prop}s of {target} "
                         f"with properties {props}")
    if address is not None:
        props = [f'label:"home"', f'city:"{_esc(address.get("city"))}"']
        if address.get("state"):
            props.append(f'state:"{_esc(address["state"])}"')
        lines.append(f"make new address at end of addresses of {target} "
                     f"with properties {{{', '.join(props)}}}")
    for label, value in (urls or []):
        lines.append(f"make new url at end of urls of {target} with "
                     f'properties {{label:"{_esc(label)}", value:"{_esc(value)}"}}')
    if note is not None:
        lines.append(f"set note of {target} to {_lit(note)}")
    return lines


def build_script(uid, *, company=None, title=None, birthday=None,
                 emails=None, phones=None, note=None, address=None,
                 urls=None):
    """The per-card AppleScript. Only the pieces that changed are
    included; None means leave that aspect of the card alone."""
    lines = [f'set P to person id "{_esc(uid)}"']
    lines += _field_lines("P", company=company, title=title,
                          birthday=birthday, emails=emails, phones=phones,
                          note=note, address=address, urls=urls)
    body = "\n".join(lines)
    return f'tell application "Contacts"\n{body}\nsave\nend tell\nreturn "ok"'


def split_name(name):
    """"Pat Q. Example" -> ("Pat Q.", "Example"); a comma-suffixed name
    ("Pat Example, M.D.") drops the suffix, which the note still carries."""
    base = (name or "").split(",")[0].strip()
    parts = base.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def build_create_script(name, *, company=None, title=None, birthday=None,
                        emails=None, phones=None, note=None, address=None,
                        urls=None):
    """A new card with everything the CRM knows; returns the new card's
    id so the creation can be logged (and undone)."""
    first, last = split_name(name)
    props = [f'first name:"{_esc(first)}"']
    if last:
        props.append(f'last name:"{_esc(last)}"')
    lines = [f"set P to make new person with properties {{{', '.join(props)}}}"]
    lines += _field_lines("P", company=company, title=title,
                          birthday=birthday, emails=emails, phones=phones,
                          note=note, address=address, urls=urls,
                          rewrite_lists=False)
    body = "\n".join(lines)
    return f'tell application "Contacts"\n{body}\nsave\nreturn id of P\nend tell'


def build_delete_script(uid):
    return ('tell application "Contacts"\n'
            f'delete person id "{_esc(uid)}"\nsave\nend tell\nreturn "ok"')


def _ensure_running():
    r = subprocess.run(["pgrep", "-x", "Contacts"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["open", "-g", "-j", "-a", "Contacts"],
                       capture_output=True, timeout=15)
        time.sleep(2)


def _osascript(script):
    _ensure_running()
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=OSA_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return (r.stdout or "").strip()


def backup_vcard(uid):
    """Export the card's vCard next to the other data backups before the
    first mutating touch - restore is re-import. Best-effort."""
    try:
        out = _osascript('tell application "Contacts"\n'
                         f'return vcard of person id "{_esc(uid)}"\n'
                         "end tell")
        if not out:
            return None
        bdir = DATA / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9-]", "", uid.split(":")[0])[:40]
        path = bdir / f"applecard-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.vcf"
        path.write_text(out, encoding="utf-8")
        return str(path)
    except Exception:
        return None


# ---------- the push ----------

def _view(pid):
    """(card, detail) for a person, or (None, None) when unknown."""
    from . import contactcard, data as crm
    detail = crm.get_person(pid)
    if not detail:
        return None, None
    return contactcard.compose(pid, detail), detail


def plan_person(pid, matches=None, card=None, detail=None):
    """[(apple_card, plan)] for every matched card; None for an unknown
    person; [] when no card matches. No writes."""
    if card is None or detail is None:
        card, detail = _view(pid)
        if not card:
            return None
    if matches is None:
        rows = card["handles"]
        matches = find_cards([r["value"] for r in rows if r["kind"] == "email"],
                             [r["value"] for r in rows if r["kind"] == "phone"])
    return [(m, card_plan(card, detail, m)) for m in matches]


def apply_plan(apple, plan):
    """Write one planned change set to one card: vCard backup first, then
    the script. Raises on an AppleScript failure."""
    if not plan["kw"]:
        return {"uid": apple["uid"], "changed": [], "conflicts": [],
                "foreign_note": False}
    backup_vcard(apple["uid"])
    _osascript(build_script(apple["uid"], **plan["kw"]))
    return {"uid": apple["uid"], "changed": plan["changed"],
            "conflicts": plan["conflicts"], "foreign_note": plan["foreign_note"]}


def push_person(pid, matches=None):
    """Push a person's card state to every matched Apple card. Returns
    {ok, cards: [{uid, changed, conflicts, foreign_note}], detail} and
    never raises - a failed push must not fail the save that triggered it."""
    if not enabled():
        return {"ok": False, "detail": "apple push not available here",
                "cards": []}
    try:
        plans = plan_person(pid, matches)
        if plans is None:
            return {"ok": False, "detail": "unknown person", "cards": []}
        if not plans:
            return {"ok": False, "detail": "no Apple contact matched",
                    "cards": []}
        results = [apply_plan(apple, plan) for apple, plan in plans]
        n = sum(1 for r in results if r["changed"])
        return {"ok": True, "cards": results,
                "detail": (f"updated {n} Apple card" + ("s" if n != 1 else "")
                           if n else "Apple contact already current")}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:300], "cards": []}


def create_card(pid, card=None, detail=None):
    """Create an Apple card for a CRM person who has none, carrying every
    field the push would. Returns {ok, uid, detail}; never raises. Refuses
    when a card already matches, so a stale list cannot make duplicates."""
    if not enabled():
        return {"ok": False, "detail": "apple push not available here",
                "uid": None}
    try:
        if card is None or detail is None:
            card, detail = _view(pid)
            if not card:
                return {"ok": False, "detail": "unknown person", "uid": None}
        rows = card["handles"]
        emails = [r["value"] for r in rows if r["kind"] == "email"]
        phones = [r["value"] for r in rows if r["kind"] == "phone"]
        if find_cards(emails, phones):
            return {"ok": False, "detail": "an Apple card already matches",
                    "uid": None}
        fields = {f["key"]: f["value"] for f in card["fields"]}
        name = fields.get("display_name") or detail["person"].get("name") or ""
        if not name.strip() or not (emails or phones):
            return {"ok": False, "detail": "needs a name and a handle",
                    "uid": None}
        kw = {
            "company": fields.get("company") or None,
            "title": fields.get("title") or None,
            "birthday": _birthday_parts(fields.get("birthday")),
            "emails": plan_handles([r for r in rows if r["kind"] == "email"],
                                   [], "email") or None,
            "phones": plan_handles([r for r in rows if r["kind"] == "phone"],
                                   [], "phone") or None,
            "address": _address_parts(fields.get("location")),
            "urls": [(c.get("label") or "url", c["value"]) for c in card.get("custom", [])
                     if _URL_RX.match((c.get("value") or "").strip())] or None,
            "note": merge_note("", note_block(card, detail)),
        }
        uid = _osascript(build_create_script(name, **kw))
        return {"ok": True, "uid": uid or None,
                "detail": f"created an Apple card for {name}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:300], "uid": None}


def delete_card(uid):
    """Remove a card Vira created (the undo for create_card). Backs the
    vCard up first; never raises."""
    try:
        backup_vcard(uid)
        _osascript(build_delete_script(uid))
        return {"ok": True, "detail": "deleted"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:300]}

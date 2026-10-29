"""Unknown-sender triage: surface unresolved iMessage handles (using the CRM
enrichment verdicts as prefill) and append user-approved people to the CRM
registry (people.json — still the source of truth, so it gets a timestamped
backup before every write). Deterministic file reads/writes, no AI.

Since 2026-10-28 this is also where the registry's small owner decisions
are written, each backup-first through _read_people_backed_up: a Skip
(the record is tiered out and its handles dismissed, undone by a later
name), a shared line (handles.lines: an office number or team mailbox
that is nobody's match signal), and a batch of handle adds in one write.
"""
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import data as crm
from . import imessage
from . import jsonstore

STATE = Path(__file__).resolve().parent.parent / "data" / "triage-state.json"


def _enrichment():
    return crm._crm() / "imessage-enrichment.json"


def _people():
    return crm._crm() / "people.json"


def _backups():
    return crm._crm() / "backups"


_lock = threading.Lock()


def _dismissed():
    return set(jsonstore.read(STATE, {}).get("dismissed") or [])


def dismiss(handle):
    return dismiss_many([handle])


def dismiss_many(handles):
    """Dismiss several handles in one write of the triage state."""
    want = [h for h in handles if h]

    def fn(s):
        d = set(s.get("dismissed") or [])
        d.update(want)
        s["dismissed"] = sorted(d)
        return s

    jsonstore.mutate(STATE, fn, {}, indent=1)
    return {"dismissed": True, "n": len(want)}


def _verdicts():
    try:
        return json.loads(_enrichment().read_text())["verdicts"]
    except (OSError, json.JSONDecodeError, KeyError):
        return []


def _key(handle):
    h = handle or ""
    return h.lower() if "@" in h else crm.norm_digits(h)


def verdict_for(handle):
    k = _key(handle)
    if not k:
        return None
    for v in _verdicts():
        if _key(v.get("handle")) == k:
            return v
    return None


PLACEHOLDER_NAMES = {"(unidentified)", "unidentified", ""}


def _is_placeholder(name):
    n = (name or "").strip()
    return n.lower() in PLACEHOLDER_NAMES or n.startswith("+1") or n.isdigit()


# The CRM's own tier words for a person Vira leaves alone: a `*skip`
# master tier, an `excluded` profile tier. Vira reads them and applies one
# rule everywhere (triage, the phone sync's card_worthy) rather than
# knowing any list.
SKIP_TIER = "D-skip"
EXCLUDED_TIER = "excluded"


def skipped(p):
    """True for a record the CRM or the owner tiered out."""
    tier = str(p.get("master_tier") or "").lower()
    return p.get("profile_tier") == EXCLUDED_TIER or tier.endswith("skip")


def _record_handles(p):
    """Every handle string a registry record carries, as the triage list
    shows them (the raw imessage spellings first)."""
    h = p.get("handles") or {}
    out = []
    for x in (h.get("imessage") or []) + (h.get("emails") or []) \
            + ["+1" + ph for ph in (h.get("phones10") or [])]:
        if x and x not in out:
            out.append(x)
    return out


def _handle_keys(p):
    """The resolution keys of a record's handles (data.handle_key)."""
    return {crm.handle_key(x) for x in _record_handles(p)} - {""}


# ---------- business/automated-sender detection ----------
# Automated company senders (banks, notification services) should surface as
# "add a company", not sit in the person queue. All signals are deterministic:
# the handle shape, the enrichment verdict's own wording, and the content of
# recent inbound messages.

TOLL_FREE_PREFIXES = ("800", "833", "844", "855", "866", "877", "888")

_AUTOMATED_RX = re.compile(
    r"automated (?:message|msg|text|alert)|do not reply|no-?reply|"
    r"reply (?:stop|help)|text stop|msg ?& ?data rates|"
    r"is your (?:security |verification |one-?time )?code|verification code|"
    r"one-?time (?:pass|code)|fraud alert|unsubscribe", re.I)

_VERDICT_RX = re.compile(
    r"automated|notifications?\b|alerts?\b|no-?reply|verification|"
    r"one-way|promotional|marketing|robocall", re.I)

# Company-name guesses only from explicit self-identification. The name may
# carry abbreviation periods ("U.S. Bank"), so the capture must end on a
# lowercase/digit character — that is what stops it at the sentence period.
_FROM_RX = (
    re.compile(r"(?:automated (?:message|msg|text)|message|alert|text) from "
               r"([A-Z](?:[A-Za-z0-9.&'’ -]*?[a-z0-9]))(?=[.,!\n]|$)"),
    re.compile(r"^([A-Z][A-Za-z0-9.&'’ -]{1,30}?) Alerts?:"),
)


def _recent_inbound(handle, limit=6):
    """Last few inbound message texts from this handle — the content signal
    ("this is an automated message from …"). Returns [] for email handles or
    when chat.db is unreadable (no Full Disk Access): detection degrades to
    the handle/verdict signals."""
    if not handle or "@" in handle:
        return []
    digits = re.sub(r"\D", "", handle)
    if not digits:
        return []
    try:
        con = imessage._connect()
        try:
            rows = con.execute(
                """SELECT m.text, m.attributedBody FROM message m
                   JOIN handle h ON m.handle_id = h.ROWID
                   WHERE m.is_from_me = 0 AND h.id LIKE ?
                   ORDER BY m.date DESC LIMIT ?""",
                (f"%{digits}%", limit)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — probe is best-effort, never breaks triage
        return []
    return [t for text, blob in rows if (t := imessage.msg_text(text, blob))]


def business_signals(handle, verdict=None, texts=None):
    """Deterministic reasons this sender looks like an automated/company
    sender rather than a person. Returns (signals, company_guess) — empty
    signals list means "treat as a person"."""
    sig = []
    digits = re.sub(r"\D", "", handle or "")
    local = digits[-10:] if len(digits) >= 10 else digits
    if handle and "@" not in handle and 3 <= len(digits) <= 6:
        sig.append("short-code sender")
    if len(local) == 10 and local[:3] in TOLL_FREE_PREFIXES:
        sig.append("toll-free number")
    v = verdict or {}
    vtext = " ".join(str(v.get(k) or "") for k in ("relationship", "evidence"))
    if _VERDICT_RX.search(vtext):
        sig.append("enrichment: automated/notifications")
    guess = ""
    for t in texts or []:
        if _AUTOMATED_RX.search(t) and "message content: automated" not in sig:
            sig.append("message content: automated")
        if not guess:
            for rx in _FROM_RX:
                m = rx.search(t)
                if m:
                    guess = m.group(1).strip()
                    break
    return sig, guess


def candidates():
    """Three sources, merged: (a) enrichment verdicts whose handle is still
    absent from the CRM, (b) placeholder "(unidentified)" entries the
    enrichment merge already appended to people.json — naming those is the
    bulk of the triage work — and (c) unresolved senders uploaded by the
    Android companion app (server/companion.py). Sorted contact-worthy
    first, then by volume."""
    dis = _dismissed()
    by_key = {}
    for v in _verdicts():
        k = _key(v.get("handle"))
        if k:
            by_key[k] = v

    out = []
    seen = set()
    for p in crm._load()["people"]:
        if not _is_placeholder(p["name"]) or skipped(p):
            continue
        handles = p.get("handles", {})
        hs = (handles.get("imessage") or handles.get("emails")
              or ["+1" + ph for ph in handles.get("phones10", [])])
        h = next((x for x in hs if "(smsft)" not in x), hs[0] if hs else None)
        if not h or h in dis:
            continue
        v = by_key.get(_key(h), {})
        seen.add(_key(h))
        out.append({
            "handle": h,
            "person_id": p["id"],
            "name": v.get("confirmed_name") or "",
            "relationship": v.get("relationship") or "",
            "evidence": v.get("evidence") or "",
            "contact_worthy": v.get("contact_worthy"),
            "confidence": v.get("confidence"),
            "action": v.get("action") or "needs_name",
            "tier": p.get("master_tier"),
            "msgs": p.get("activity", {}).get("imsg_n", 0),
        })
    for v in _verdicts():
        h = v.get("handle", "")
        if not h or h in dis or _key(h) in seen:
            continue
        if v.get("action") in ("skip", "already_saved", "flag_bad_import"):
            continue
        if crm.resolve_handle(h):  # resolved since the verdict was written
            continue
        out.append({
            "handle": h,
            "person_id": None,
            "name": v.get("confirmed_name") or "",
            "relationship": v.get("relationship") or "",
            "evidence": v.get("evidence") or "",
            "contact_worthy": v.get("contact_worthy"),
            "confidence": v.get("confidence"),
            "action": v.get("action"),
            "tier": v.get("tier_target"),
            "msgs": 0,
        })
    from . import companion
    for u in companion.unknown_senders():
        h = u["handle"]
        if not h or h in dis or _key(h) in seen or crm.resolve_handle(h):
            continue
        seen.add(_key(h))
        out.append({
            "handle": h,
            "person_id": None,
            "name": "",
            "relationship": f"texts via the companion app ({u['channel']})",
            "evidence": (u["texts"][0][:140] if u.get("texts") else ""),
            "contact_worthy": None,
            "confidence": None,
            "action": "needs_name",
            "tier": None,
            "msgs": u["msgs"],
            "_texts": u.get("texts") or [],
        })

    from . import resolver
    for c in out:
        sig, guess = business_signals(
            c["handle"], by_key.get(_key(c["handle"])),
            c.pop("_texts", None) or _recent_inbound(c["handle"]))
        c["business"] = bool(sig)
        c["business_signals"] = sig
        c["company_guess"] = guess
        # a referrer named in the evidence -> the card auto-resolves on open
        # (a person card only; automated senders never carry a referral)
        c["referral_hint"] = ("" if c["business"] else resolver.referrer_from_text(
            " ".join([c.get("relationship") or "", c.get("evidence") or ""])))
    # people first (contact-worthy, then volume); likely businesses form
    # their own band at the end — they want "add as company", not naming.
    order = {"yes": 0, "unsure": 1}
    out.sort(key=lambda x: (x["business"], order.get(x["contact_worthy"], 2),
                            -(x["msgs"] or 0), x["handle"]))
    return out


# How many people.json snapshots the CRM's backups/ keeps. Every registry
# write takes one, and a sync pass that lands a dozen handles used to take a
# dozen in a minute; the newest twenty reach back further than anyone has
# needed to restore, and the dated merge-* folders beside them are kept.
BACKUPS_KEEP = 20


def _prune_backups(backups):
    try:
        old = sorted(backups.glob("people-*.json"))
        for stale in old[:-BACKUPS_KEEP]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _read_people_backed_up(root=None):
    """Read people.json after taking a timestamped backup. Every write path
    into the registry goes through this — the backup is the guarantee, and a
    guarantee with more than one implementation is not one."""
    people_path = root / "people.json" if root is not None else _people()
    try:
        doc = json.loads(people_path.read_text(encoding="utf-8"))
        backups = root / "backups" if root is not None else _backups()
        backups.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = backups / f"people-{stamp}.json"
        for n in range(1, 100):
            if not dest.exists():
                break
            dest = backups / f"people-{stamp}-{n:02d}.json"
        shutil.copy2(people_path, dest)
        _prune_backups(backups)
    except FileNotFoundError:
        # first person ever: mint the registry, so a CRM can grow from
        # zero through triage alone (v9 onboarding)
        people_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"people": []}
    return doc


def _write_people(doc, root=None):
    path = root / "people.json" if root is not None else _people()
    jsonstore.write_atomic(path, doc, indent=1, ensure_ascii=False)
    crm.invalidate()


def rename_person(person_id, name):
    """Rename an existing registry entry in place. The contact card's name
    field writes through to here rather than into Vira's overlay: a name is
    registry data, and an overlaid one would leave the card reading something
    the feed, brief, and people list did not."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    with _lock:
        crm.invalidate()
        doc = _read_people_backed_up()
        person = next((p for p in doc["people"] if p["id"] == person_id), None)
        if not person:
            raise KeyError(person_id)
        person["name"] = name
        person.setdefault("refs", {})["vira_named"] = time.strftime("%Y-%m-%d")
        _unskip(person)
        _write_people(doc)
        return person


def _unskip(person):
    """A named person is no longer skipped: a Skip (skip_person) is undone
    the moment the record gets a real name, restoring the tiers it had."""
    sk = (person.get("refs") or {}).get("skipped")
    if not isinstance(sk, dict) or _is_placeholder(person.get("name")):
        return False
    prev = sk.get("previous") or {}
    person["master_tier"] = prev.get("master_tier") or "C-review"
    if prev.get("profile_tier"):
        person["profile_tier"] = prev["profile_tier"]
    else:
        person.pop("profile_tier", None)
    person["refs"].pop("skipped", None)
    person["refs"]["unskipped"] = time.strftime("%Y-%m-%d")
    return True


def skip_person(person_id):
    """Skip a registry record: tiered out (the CRM's own words, D-skip and
    excluded), its handles dismissed from triage, and never a phone card.
    The tiers it had are kept under refs.skipped so a later name restores
    them. Backup-first, one write."""
    with _lock:
        crm.invalidate()
        doc = _read_people_backed_up()
        person = next((p for p in doc["people"] if p["id"] == person_id), None)
        if not person:
            raise KeyError(person_id)
        refs = person.setdefault("refs", {})
        if not isinstance(refs.get("skipped"), dict):
            refs["skipped"] = {"when": time.strftime("%Y-%m-%d"), "by": "triage",
                               "previous": {"master_tier": person.get("master_tier"),
                                            "profile_tier": person.get("profile_tier")}}
        person["master_tier"] = SKIP_TIER
        person["profile_tier"] = EXCLUDED_TIER
        _write_people(doc)
        handles = _record_handles(person)
    if handles:
        dismiss_many(handles)
    return {"skipped": True, "person_id": person_id, "dismissed": len(handles)}


def skip_no_verdicts():
    """Skip every candidate whose enrichment verdict says it is not worth a
    contact: records are skipped, verdict-only handles dismissed."""
    out = {"skipped": 0, "dismissed": 0}
    for c in candidates():
        if c.get("contact_worthy") != "no":
            continue
        if c.get("person_id"):
            skip_person(c["person_id"])
            out["skipped"] += 1
        else:
            dismiss(c["handle"])
            out["dismissed"] += 1
    return out


def mark_lines(items, unmark=False):
    """Mark handles as shared lines (or clear the mark). `items` is a list
    of {pid, value}; each value must be a handle of that record. One
    backup, one write. Returns the keys that changed."""
    with _lock:
        crm.invalidate()
        doc = _read_people_backed_up()
        by_id = {p["id"]: p for p in doc["people"]}
        changed = []
        for it in items:
            person = by_id.get(it.get("pid"))
            if not person:
                raise KeyError(it.get("pid"))
            key = crm.handle_key(it.get("value"))
            if not key or key not in _handle_keys(person):
                raise ValueError(f"{it.get('value')} is not a handle of "
                                 f"{person.get('name') or person['id']}")
            lines = person.setdefault("handles", {}).setdefault("lines", [])
            if unmark:
                if key in lines:
                    lines.remove(key)
                    changed.append(key)
            elif key not in lines:
                lines.append(key)
                changed.append(key)
        if changed:
            _write_people(doc)
    return changed


def _land_handles(person, handles, known):
    """Append handles to one record held in a document already read. `known`
    is {handle key: pid} over the whole document, kept current as handles
    land, so a batch cannot give one handle to two people. Returns
    (landed normalized handles, [(value, why it was refused)])."""
    h = person.setdefault("handles", {})
    for bucket in ("imessage", "phones10", "emails"):
        h.setdefault(bucket, [])
    landed, errors = [], []
    for item in handles:
        kind, value = item.get("kind"), (item.get("value") or "").strip()
        if not value:
            continue
        if kind == "email":
            norm = value.lower()
            bucket, existing = "emails", {e.lower() for e in h["emails"]}
        else:
            norm = crm.norm_digits(value)
            bucket, existing = "phones10", set(h["phones10"])
            if len(norm) < 10:
                errors.append((value, f"{value} is not a full phone number"))
                continue
        owner = known.get(norm)
        if owner and owner != person["id"]:
            errors.append((value, f"handle {value} already belongs to "
                                  "another CRM person"))
            continue
        if norm in existing:
            continue
        h[bucket].append(norm)
        if norm not in h["imessage"]:
            h["imessage"].append(norm)
        known[norm] = person["id"]
        landed.append(norm)
    if landed:
        person.setdefault("refs", {})["vira_handles"] = time.strftime("%Y-%m-%d")
    return landed, errors


def _known_handles(doc):
    known = {}
    for p in doc["people"]:
        for k in _handle_keys(p):
            known.setdefault(k, p["id"])
    return known


def add_handles(person_id, handles):
    """Append emails/phones to an existing registry entry. `handles` is a list
    of {kind: email|phone, value: ...}. A handle already owned by someone else
    is refused rather than moved — reassigning one silently would re-point
    every past message that resolved through it.

    Mirrors add_person's shape: the raw handle also joins `imessage`, which is
    the list send.best_handle falls back to.
    """
    with _lock:
        crm.invalidate()
        doc = _read_people_backed_up()
        person = next((p for p in doc["people"] if p["id"] == person_id), None)
        if not person:
            raise KeyError(person_id)
        landed, errors = _land_handles(person, handles, _known_handles(doc))
        if errors:
            raise ValueError(errors[0][1])    # nothing written: all or none
        if landed:
            _write_people(doc)
        return landed


def add_handles_batch(groups):
    """Several people's handle adds in ONE backup and ONE write of
    people.json: `groups` is [(person_id, [{kind, value}])]. A refused
    item (an unknown person, a handle owned elsewhere, a short number)
    costs only itself. Returns {landed: [(pid, norm)], errors: [(pid,
    value, message)]}."""
    out = {"landed": [], "errors": []}
    with _lock:
        crm.invalidate()
        doc = _read_people_backed_up()
        by_id = {p["id"]: p for p in doc["people"]}
        known = _known_handles(doc)
        for pid, handles in groups:
            person = by_id.get(pid)
            if not person:
                for it in handles:
                    out["errors"].append((pid, it.get("value"), "unknown person"))
                continue
            landed, errors = _land_handles(person, handles, known)
            out["landed"] += [(pid, norm) for norm in landed]
            out["errors"] += [(pid, value, why) for value, why in errors]
        if out["landed"]:
            _write_people(doc)
    return out


def add_person(name, handles, class_hint=None, note=None, person_id=None):
    """Append a person to people.json — or, when person_id names an existing
    placeholder entry, rename it in place. Record shape matches
    build_people.py output; id scheme matches assign_ids.py."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    clean = [h.strip() for h in handles if h and h.strip()]
    if not clean and not person_id:
        raise ValueError("at least one handle required")
    with _lock:
        crm.invalidate()
        doc = _read_people_backed_up()

        if person_id:
            person = next((p for p in doc["people"] if p["id"] == person_id), None)
            if not person:
                raise ValueError(f"unknown person id {person_id}")
            person["name"] = name
            if class_hint:
                person["class_hint"] = class_hint
            person.setdefault("refs", {})["vira_named"] = time.strftime("%Y-%m-%d")
            if note:
                person["refs"]["note"] = note
            _unskip(person)
        else:
            for h in clean:
                if crm.resolve_handle(h):
                    raise ValueError(f"handle {h} already belongs to a CRM person")
            emails = sorted({h.lower() for h in clean if "@" in h})
            phones = sorted({crm.norm_digits(h) for h in clean if "@" not in h} - {""})
            person = {
                "id": "p_" + uuid.uuid4().hex[:12],
                "name": name,
                "class_hint": class_hint or None,
                "refs": {"vira_added": time.strftime("%Y-%m-%d"),
                         **({"note": note} if note else {})},
                "handles": {"imessage": clean, "phones10": phones,
                            "emails": emails},
                "master_tier": "C-review",
                "activity": {},
            }
            doc["people"].append(person)

        _write_people(doc)
        return person

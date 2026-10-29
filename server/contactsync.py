"""Contacts sync: the CRM and the phone's address book reconciled, both ways.

Before this module (2026-10-28) nothing moved contacts on its own: a card
added on the phone reached the Mac over iCloud and stopped there, the only
import was the all-or-nothing button in Config > Contacts, and the Apple
spoke fired only when a contact card was saved in the People window. This
module is the standing pass between the two, surfaced as People > Sync.

Inbound (phone -> CRM), additive only:
- A card created after the watermark with no CRM match becomes a CRM
  person through onboard.import_contacts (backup-first, review tier). The
  watermark starts at the creation date of the newest card the CRM
  already knows, so the first pass brings in only what arrived after the
  CRM was built; everything older is the backlog, counted and importable
  from the Sync tab in one explicit step (the Config import).
- An email or phone on a matched card the CRM lacks joins the person's
  handles (triage.add_handles). One that belongs to another CRM person
  marks a card two people share (a duplicate, a family card): it is never
  moved, never retried, and the card is listed once under shared cards
  and in the review list. Nothing is pushed to a shared card either, or
  the two people would overwrite each other on alternate passes.
- An empty card field (company, title, birthday, when we met, location)
  is filled from the phone card, and the phone's own note is kept in Vira
  as a "Phone note" custom row. Nothing the CRM already says is
  overwritten.

Outbound (CRM -> phone): applecontacts.card_plan for every matched card,
applied in one batch when the owner presses Push, or on every pass when
contacts_sync_push_auto is on. Creating cards for CRM people who have
none is its own explicit step (Create), logged so it can be undone.

Reconciliation rule (the owner's, 2026-10-28): when both sides hold
content, keep both and flag it. The Apple note keeps the phone's text and
gains the Vira block; a differing scalar is set to the CRM value with the
phone's value carried in the block; every such case, every refused
handle, every new person and every created card lands in the review list
the Sync tab shows until the owner marks it read.

Store: data/contacts-sync.json (jsonstore): the watermark, the last
report, the review list, the pass log, and the created-card ledger. The
watermark and report are regenerable; the ledgers are history.

Cadence: a daemon thread runs a pass every contacts_sync_interval minutes
while contacts_sync is on; off, the Sync tab still surveys on demand.
CLI: python -m server.contactsync survey | pass [--push] [--create]
[--no-inbound] | undo-created.
"""

import argparse
import json
import logging
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from . import applecontacts as ac
from . import jsonstore, runtimework, settings, triage

log = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data"
STORE = DATA / "contacts-sync.json"
# The review list and the pass log are owner-facing history, not a
# corpus: the newest 2,000 review entries and 300 passes are more than
# anyone reads, and keep the store a quick fresh read on every request.
MAX_REVIEW = 2000
MAX_LOG = 300
# The stored report keeps the first rows of each list beside the full
# count: the Sync tab reads it whole on every open, and the lists exist
# to be scanned, not paged.
LIST_CAP = 300
# How much of a phone note travels into a review entry: enough to read
# what the phone said without copying a whole dossier into the store.
NOTE_EXCERPT = 2000
PHONE_NOTE_LABEL = "Phone note"
# First pass after boot waits for the indexers and the CRM load to settle.
BOOT_DELAY_S = 120

_run_lock = threading.Lock()
_status = {"running": False, "phase": "", "started": None, "finished": None,
           "error": None, "reason": None, "progress": None}


def _progress(done=None, total=None, step=None):
    """The running pass's counter, for the Sync tab (status()) and for
    Fleet (runtimework): done of total, and the name being worked on."""
    cur = dict(_status.get("progress") or {})
    if done is not None:
        cur["done"] = done
    if total is not None:
        cur["total"] = total
    if step is not None:
        cur["step"] = str(step)[:120]
    _status["progress"] = cur
    runtimework.progress(done=done, total=total, step=step)


# ---------- store ----------

def _blank():
    return {"since": None, "report": None, "review": [], "log": [],
            "created": [], "kept_lines": []}


def read_store():
    s = jsonstore.read(STORE, None)
    if not isinstance(s, dict):
        s = _blank()
    for k, v in _blank().items():
        s.setdefault(k, v)
    return s


def _mutate(fn):
    def wrap(s):
        for k, v in _blank().items():
            s.setdefault(k, v)
        return fn(s)
    return jsonstore.mutate(STORE, wrap, _blank(), indent=1, ensure_ascii=False)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _iso(ts):
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# ---------- gates ----------

def available():
    """(ok, why): the pass needs a real, owner-mode Mac with Contacts
    stores and a connected CRM. Everything else degrades to a named
    status, never an exception."""
    if not settings.IS_MAC:
        return False, "Contacts sync runs on macOS only."
    if settings.sandboxed():
        return False, "Not in a sandbox instance."
    if settings.fixture_mode():
        return False, "Connect your contacts first (Config > Contacts)."
    if not ac._stores():
        return False, "No Contacts stores found on this Mac."
    return True, ""


def enabled():
    ok, _ = available()
    return ok and bool(settings.get("contacts_sync"))


# ---------- the survey (read-only) ----------

def _handles(p):
    h = p.get("handles") or {}
    emails = [e for e in (h.get("emails") or []) if e]
    emails += [e for e in (h.get("imessage") or []) if "@" in e and e not in emails]
    phones = [x for x in (h.get("phones10") or []) if x]
    return emails, phones


def card_worthy(p, lines=()):
    """A CRM person the phone should carry: really named (a triage
    placeholder such as "(unidentified)" or a bare number is not a name),
    reachable through a handle of their own (a shared line does not
    count: a card made from it alone would never match back), and not one
    the CRM itself marked as skipped or excluded. The tier words are the
    CRM's own (a `*skip` master tier, an `excluded` profile tier); Vira
    reads them and applies this one rule rather than knowing any list."""
    if triage.skipped(p) or triage._is_placeholder(p.get("name")):
        return False
    emails, phones = _handles(p)
    own = [e for e in emails if ac._norm_email(e) not in lines] + \
          [x for x in phones if ac._norm_phone(x) not in lines]
    return bool(own)


def _apple_location(apple):
    for a in apple.get("addresses", []):
        city, state = a.get("city") or "", a.get("state") or ""
        if city and state:
            return f"{city}, {state}"
        if city:
            return city
    return ""


def _apple_met(apple):
    """The card's "When we met" custom date as the card field's M/D/YYYY,
    or "" when the phone has none."""
    for d in apple.get("dates", []):
        if (d.get("label") or "").lower() == ac.MET_LABEL.lower() and d.get("date"):
            y, mo, dd = d["date"]
            return f"{mo}/{dd}/{y}"
    return ""


def _card_row(cd):
    return {"uid": cd["uid"], "name": cd["name"], "org": cd.get("org") or "",
            "created": _iso(cd.get("created")),
            "emails": len(cd.get("emails", [])),
            "phones": len(cd.get("phones", [])),
            "importable": bool(cd["name"] and (cd.get("emails") or cd.get("phones")))}


def survey(cards=None, store=None):
    """The read-only picture: who matches whom, what is new on the phone,
    what the CRM would change on the phone, and who is missing on either
    side. Keys starting with "_" carry the objects apply() needs and are
    stripped from the stored report."""
    from . import contactcard, data as crm
    if cards is None:
        cards = ac.load_cards()
    idx = ac.index_cards(cards)
    store = store if store is not None else read_store()
    c = crm._load()
    people = list(c["by_id"].values())
    lines = set(c.get("lines") or ())
    matched, matched_uids, owners = {}, set(), {}
    for p in people:
        emails, phones = _handles(p)
        if not (emails or phones):
            continue
        ms = ac.match_cards(cards, idx, emails, phones, lines=lines)
        if ms:
            matched[p["id"]] = ms
            matched_uids.update(m["uid"] for m in ms)
            for m in ms:
                owners.setdefault(m["uid"], []).append(p["id"])
    # A card two CRM people both match (a duplicate registry entry, a
    # family card) would take one person's fields on one pass and the
    # other's on the next. Nothing is pushed to it until the CRM says who
    # it is; it is listed once, with both names.
    shared_uids = {uid for uid, ps in owners.items() if len(ps) > 1}
    shared_cards = []
    for uid in sorted(shared_uids):
        cd = next((x for x in cards if x["uid"] == uid), None)
        shared_cards.append({"uid": uid, "card": cd["name"] if cd else "",
                             "people": [{"pid": q, "name": c["by_id"][q]["name"]}
                                        for q in owners[uid]]})
    newest_known = max((m["created"] for ms in matched.values() for m in ms
                        if m.get("created")), default=None)
    since = store.get("since")
    if since is None:
        since = newest_known
    orphans = [cd for cd in cards if cd["uid"] not in matched_uids]
    new_cards = [cd for cd in orphans
                 if since is not None and cd.get("created") and cd["created"] > since]
    new_uids = {cd["uid"] for cd in new_cards}
    backlog = [cd for cd in orphans if cd["uid"] not in new_uids]

    handle_adds, handle_conflicts, fills, changes, plans = [], [], [], [], []
    for pid, ms in matched.items():
        p = c["by_id"][pid]
        detail = crm.get_person(pid)
        card = contactcard.compose(pid, detail) if detail else None
        if not card:
            continue
        crm_e = {ac._norm_email(r["value"]) for r in card["handles"] if r["kind"] == "email"}
        crm_p = {ac._norm_phone(r["value"]) for r in card["handles"] if r["kind"] == "phone"}

        def place(kind, value, m, pid=pid, name=p["name"]):
            # A handle another CRM person already owns marks a card two
            # people share (a duplicate, a family card). It is never moved;
            # it is listed once as a shared card, not retried as pending work.
            # A shared LINE (an office number the owner marked) is neither:
            # it stays where it is and is nobody's conflict.
            if crm.handle_key(value) in lines:
                return
            owner = crm.resolve_handle(value)
            if owner and owner != pid:
                other = c["by_id"].get(owner) or {}
                handle_conflicts.append({"pid": pid, "name": name, "kind": kind,
                                         "value": value, "other_pid": owner,
                                         "other_name": other.get("name") or owner,
                                         "uid": m["uid"], "card": m.get("name") or ""})
            else:
                handle_adds.append({"pid": pid, "name": name, "kind": kind,
                                    "value": value})

        for m in ms:
            for e in m.get("emails", []):
                k = ac._norm_email(e["value"])
                if k and "@" in k and k not in crm_e:
                    crm_e.add(k)
                    place("email", e["value"].strip(), m)
            for ph in m.get("phones", []):
                k = ac._norm_phone(ph["value"])
                if len(k) == 10 and k not in crm_p:
                    crm_p.add(k)
                    place("phone", k, m)
        fields = {f["key"]: f["value"] for f in card["fields"]}
        fill = {}
        for m in ms:
            if not fields.get("company") and m.get("org") and "company" not in fill:
                fill["company"] = m["org"]
            if not fields.get("title") and m.get("title") and "title" not in fill:
                fill["title"] = m["title"]
            if not fields.get("birthday") and m.get("birthday") and "birthday" not in fill:
                y, mo, d = m["birthday"]
                fill["birthday"] = f"{mo}/{d}/{y}"
            if not fields.get("met") and "met" not in fill:
                met = _apple_met(m)
                if met:
                    fill["met"] = met
            if not fields.get("location") and "location" not in fill:
                loc = _apple_location(m)
                if loc:
                    fill["location"] = loc
        phone_note = ""
        has_note_row = any((cu.get("label") or "").lower() == PHONE_NOTE_LABEL.lower()
                           for cu in card.get("custom", []))
        if not has_note_row:
            for m in ms:
                foreign, _ = ac.split_note(m.get("note") or "")
                if foreign:
                    phone_note = foreign
                    break
        if fill or phone_note:
            fills.append({"pid": pid, "name": p["name"], "fields": fill,
                          "phone_note": phone_note})
        own = [m for m in ms if m["uid"] not in shared_uids]
        for m, plan in ac.plan_person(pid, own, card=card, detail=detail):
            if plan["changed"]:
                plans.append((pid, p["name"], m, plan))
                changes.append({"pid": pid, "name": p["name"], "uid": m["uid"],
                                "card": m["name"], "changed": plan["changed"],
                                "conflicts": plan["conflicts"],
                                "foreign_note": plan["foreign_note"]})
    unmatched = [p for p in people if p["id"] not in matched]
    missing = [{"pid": p["id"], "name": p["name"],
                "tier": p.get("profile_tier") or p.get("master_tier")}
               for p in unmatched if card_worthy(p, lines)]
    kept = set(store.get("kept_lines") or [])
    shared_lines, lines_kept = _shared_lines(
        c, cards, idx, shared_uids, owners, handle_conflicts, lines, kept)
    return {
        "generated": _now(), "cards": len(cards), "people": len(people),
        "matched": len(matched), "since": since, "newest_known": newest_known,
        "new_cards": [_card_row(cd) for cd in new_cards],
        "backlog": len(backlog),
        "backlog_sample": [_card_row(cd) for cd in backlog],
        "handle_adds": handle_adds, "handle_conflicts": handle_conflicts,
        "shared_cards": shared_cards, "shared_lines": shared_lines,
        "shared_lines_kept": lines_kept, "lines": len(lines),
        "fills": fills, "changes": changes,
        "missing": missing, "unmatched_skipped": len(unmatched) - len(missing),
        "_new_cards": new_cards, "_plans": plans,
    }


def _line_key(row):
    return row["key"].split(":", 1)[1]


def _shared_lines(c, cards, idx, shared_uids, owners, handle_conflicts, lines, kept):
    """The Shared lines list: one row per handle that explains a shared
    card, a handle conflict or a handle two registry records both carry,
    with the phone cards and the CRM people on it, and a suggestion when
    it looks like a switchboard (three or more cards, or two surnames).
    A handle on a shared card counts only when it belongs to a CRM person
    whose name is NOT the card's: the card's own person sharing a card
    with a second record is a duplicate, not a line, and is left to the
    Duplicates list. Rows the owner kept apart ("Not the same office")
    are dropped and counted."""
    from . import contactcard, names
    carriers = {}
    for p in c["by_id"].values():
        for k in triage._handle_keys(p):
            carriers.setdefault(k, []).append(p["id"])
    rows = {}

    def row(key):
        kind = "email" if "@" in key else "phone"
        k = f"{kind}:{key}"
        r = rows.get(k)
        if r is None:
            hits = idx[kind].get(key, [])
            r = rows[k] = {
                "key": k, "kind": kind,
                "value": key if kind == "email" else contactcard.format_phone(key),
                "cards": [{"uid": cards[i]["uid"], "name": cards[i].get("name") or ""}
                          for i in hits],
                "people": [{"pid": q, "name": (c["by_id"].get(q) or {}).get("name") or q}
                           for q in carriers.get(key, [])],
                "shared_cards": [], "conflicts": [], "why": [], "suggest": False,
                "mark": [{"pid": q, "value": key} for q in carriers.get(key, [])],
            }
        return r

    by_uid = {cd["uid"]: cd for cd in cards}
    for uid in sorted(shared_uids):
        cd = by_uid.get(uid)
        if not cd:
            continue
        named = {q for q in owners.get(uid, [])
                 if names.compatible(cd.get("name") or "", (c["by_id"].get(q) or {}).get("name") or "")}
        handles = [(ac._norm_email(e["value"])) for e in cd.get("emails", [])] + \
                  [(ac._norm_phone(ph["value"])) for ph in cd.get("phones", [])]
        for key in handles:
            if not key or key in lines:
                continue
            if not [q for q in carriers.get(key, []) if q not in named]:
                continue
            r = row(key)
            if uid not in r["shared_cards"]:
                r["shared_cards"].append(uid)
    for hc in handle_conflicts:
        key = ac._norm_email(hc["value"]) if hc["kind"] == "email" else ac._norm_phone(hc["value"])
        if not key or key in lines:
            continue
        if names.compatible(hc.get("name") or "", hc.get("other_name") or ""):
            continue        # two records for one person: a duplicate, not a line
        if names.compatible(hc.get("card") or "", hc.get("other_name") or ""):
            continue        # the card's own person's handle: the line is elsewhere
        r = row(key)
        r["conflicts"].append({"pid": hc["pid"], "name": hc["name"],
                               "other_pid": hc["other_pid"], "other_name": hc["other_name"],
                               "uid": hc.get("uid"), "card": hc.get("card") or ""})
    for key, pids in carriers.items():
        if len(pids) > 1 and key not in lines:
            row(key)
    out, n_kept = [], 0
    for r in rows.values():
        if r["key"] in kept:
            n_kept += 1
            continue
        surnames = set()
        for q in r["people"]:
            surnames.add(names.surname_key(names.split(q["name"])[1]))
        for cd in r["cards"]:
            surnames.add(names.surname_key(names.split(cd["name"])[1]))
        surnames.discard("")
        if len(r["cards"]) >= 3:
            r["why"].append(f"on {len(r['cards'])} phone cards")
        if len(surnames) >= 2:
            r["why"].append("two surnames share it")
        if len(r["people"]) > 1:
            r["why"].append(f"on {_count(len(r['people'])).lower()} CRM people")
        r["suggest"] = len(r["cards"]) >= 3 or len(surnames) >= 2
        out.append(r)
    out.sort(key=lambda r: (not r["suggest"], -len(r["cards"]), r["value"]))
    return out, n_kept


def _public(s):
    """The stored/served form: lists capped, totals kept, objects dropped."""
    out = {}
    for k, v in s.items():
        if k.startswith("_"):
            continue
        if isinstance(v, list):
            out[k] = v[:LIST_CAP]
            out[k + "_total"] = len(v)
        else:
            out[k] = v
    out["since"] = _iso(s.get("since"))
    out["newest_known"] = _iso(s.get("newest_known"))
    out["fills"] = [{"pid": f["pid"], "name": f["name"],
                     "fields": sorted(f["fields"]),
                     "phone_note": bool(f["phone_note"])}
                    for f in s.get("fills", [])[:LIST_CAP]]
    return out


# ---------- the pass (writes) ----------

_WORDS = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven",
          8: "Eight", 9: "Nine"}


def _count(n):
    return _WORDS.get(n, str(n))


def _join(names):
    """'A and B'; 'A, B and C'."""
    names = list(names)
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _review(kind, pid, name, *, uid=None, detail="", text="", key=None, card=None):
    """One entry for the owner to read. `key` is what makes it the same
    case on a later pass (the default folds in the detail text). `card`
    carries the phone card a possible-duplicate entry can still act on."""
    out = {"id": "rv_" + uuid.uuid4().hex[:10], "kind": kind, "pid": pid,
           "name": name, "uid": uid, "when": _now(), "detail": detail,
           "text": (text or "")[:NOTE_EXCERPT], "reviewed": False,
           "key": key or f"{kind}:{pid}:{uid}:{detail}"}
    if card is not None:
        out["card"] = card
    return out


review_entry = _review      # the name other modules file entries under

# Kinds that tell the owner something happened rather than ask for a
# decision: the Sync tab folds them closed.
INFORMATIONAL_KINDS = ("note-merged", "phone-note", "created-card", "merged")


_LEGACY_CONFLICT = re.compile(r"carries (email|phone) (\S+?)[:,]")


def _review_key(r):
    """The dedup key of a stored entry; entries written before keys
    existed get one derived from their text."""
    if r.get("key"):
        return r["key"]
    if r.get("kind") == "handle-conflict":
        m = _LEGACY_CONFLICT.search(r.get("detail") or "")
        if m:
            return f"handle-conflict:{r.get('pid')}:{m.group(2)}"
    return f"{r.get('kind')}:{r.get('pid')}:{r.get('uid')}:{r.get('detail')}"


def apply(inbound=True, push=False, create=False, reason="manual"):
    """One pass. Returns {ok, done, report} or {busy} when a pass is
    already running; never raises."""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "detail": "a pass is already running"}
    ok, why = available()
    if not ok:
        _run_lock.release()
        return {"ok": False, "detail": why}
    now = _now()
    _status.update(running=True, phase="survey", started=now, error=None,
                   reason=reason, progress=None)
    try:
        from . import contactcard, data as crm, onboard, triage
        with runtimework.activity(f"Contacts sync ({reason})", kind="sync"):
            return _apply_inner(inbound, push, create, reason, now,
                                contactcard, crm, onboard, triage)
    except Exception as e:
        log.exception("contacts sync pass failed")
        _status["error"] = str(e)[:300]
        return {"ok": False, "detail": str(e)[:300]}
    finally:
        _status.update(running=False, phase="", finished=_now(), progress=None)
        _run_lock.release()


def _apply_inner(inbound, push, create, reason, now, contactcard, crm, onboard, triage):
    _progress(step="surveying the cards and the CRM")
    cards = ac.load_cards()
    s = survey(cards)
    done = {"imported": [], "handles": [], "fills": 0, "pushed": [],
            "created": [], "errors": [], "review": 0}
    reviews = []
    if inbound:
        _status["phase"] = "inbound"
        contacts = []
        for cd in s["_new_cards"]:
            emails = sorted({e["value"].strip().lower()
                             for e in cd.get("emails", []) if e["value"].strip()})
            phones = sorted({ac._norm_phone(p["value"]) for p in cd.get("phones", [])
                             if len(ac._norm_phone(p["value"])) == 10})
            if cd["name"] and (emails or phones):
                contacts.append({"name": cd["name"], "company": cd.get("org") or "",
                                 "title": cd.get("title") or "",
                                 "emails": emails, "phones10": phones,
                                 "uid": cd["uid"]})
        if contacts:
            r_imp = onboard.import_contacts(contacts, "apple-contacts")
            held = {h["name"] for h in r_imp.get("held") or []}
            crm.invalidate()
            for ct in contacts:
                if ct["name"] in held:
                    continue        # filed as a possible duplicate instead
                pid = None
                for h in ct["emails"] + ct["phones10"]:
                    pid = crm.resolve_handle(h)
                    if pid:
                        break
                done["imported"].append({"pid": pid, "name": ct["name"]})
                where = f" ({ct['company']})" if ct["company"] else ""
                reviews.append(_review(
                    "new-person", pid, ct["name"],
                    detail=f"New on the phone: {ct['name']}{where}. "
                           "Set the relationship and check the card."))
        newest = max((cd["created"] for cd in s["_new_cards"] if cd.get("created")),
                     default=None)
        marks = [x for x in (s["since"], newest) if x is not None]
        if marks:
            mark = max(marks)
            _mutate(lambda st: st.__setitem__("since", mark))
        # Every handle the phone adds lands in ONE backup and ONE write of
        # people.json (a pass once took thirteen in a minute).
        groups = {}
        for item in s["handle_adds"]:
            groups.setdefault(item["pid"], []).append(
                {"kind": item["kind"], "value": item["value"]})
        if groups:
            try:
                r_add = triage.add_handles_batch(list(groups.items()))
            except OSError as e:
                r_add = {"landed": [], "errors": [
                    (pid, it["value"], str(e)[:120]) for pid, its in groups.items() for it in its]}
            landed = set(r_add["landed"])
            refused = {(pid, value): why for pid, value, why in r_add["errors"]}
            for item in s["handle_adds"]:
                norm = ac._norm_email(item["value"]) if item["kind"] == "email" \
                    else ac._norm_phone(item["value"])
                if (item["pid"], norm) in landed:
                    done["handles"].append(item)
                    continue
                why = refused.get((item["pid"], item["value"]))
                if why is None:
                    continue        # already on the record
                if "unknown person" in why:
                    done["errors"].append(f"{item['name']}: {why}")
                    continue
                reviews.append(_review(
                    "handle-conflict", item["pid"], item["name"],
                    detail=f"The phone card carries {item['kind']} "
                           f"{item['value']}: {why}",
                    key=f"handle-conflict:{item['pid']}:{item['value']}"))
        _progress(done=0, total=len(s["fills"]), step="filling cards from the phone")
        for n_f, f in enumerate(s["fills"], 1):
            _progress(done=n_f, step=f["name"])
            draft = {"fields": dict(f["fields"])}
            if f["phone_note"]:
                card = contactcard.compose(f["pid"])
                custom = list(card["custom"]) if card else []
                custom.append({"label": PHONE_NOTE_LABEL,
                               "value": f["phone_note"][:contactcard.MAX_VALUE]})
                draft["custom"] = custom
                reviews.append(_review(
                    "phone-note", f["pid"], f["name"],
                    detail="The phone card had its own note. It is now the "
                           f"'{PHONE_NOTE_LABEL}' row on the card here and "
                           "stays on the phone above the Vira block.",
                    text=f["phone_note"]))
            try:
                contactcard.save(f["pid"], draft, integrate=False)
                done["fills"] += 1
            except (KeyError, ValueError, OSError) as e:
                done["errors"].append(f"{f['name']}: {str(e)[:120]}")
        if done["imported"] or done["handles"] or done["fills"]:
            crm.invalidate()
            s = survey(cards)       # the push plans see the filled CRM
    apple_changed = False
    if push:
        _status["phase"] = "push"
        if not ac.enabled():
            done["errors"].append("Apple push is off (Config > Contacts > "
                                  "Update Apple Contacts).")
        todo = s["_plans"] if ac.enabled() else []
        _progress(done=0, total=len(todo), step="writing phone cards")
        for n_p, (pid, name, apple, plan) in enumerate(todo, 1):
            _progress(done=n_p, step=name)
            try:
                r = ac.apply_plan(apple, plan)
                apple_changed = True
                done["pushed"].append({"pid": pid, "name": name,
                                       "uid": apple["uid"], "changed": r["changed"]})
                for cf in r["conflicts"]:
                    reviews.append(_review(
                        "field-conflict", pid, name, uid=apple["uid"],
                        detail=f"{cf['field']}: Vira says \"{cf['crm']}\", the "
                               f"phone said \"{cf['phone']}\". The phone now "
                               "shows Vira's value; the old one is kept in the "
                               "card's note."))
                if r["foreign_note"]:
                    foreign, _ = ac.split_note(apple.get("note") or "")
                    reviews.append(_review(
                        "note-merged", pid, name, uid=apple["uid"],
                        detail="The phone card had its own note; Vira's block "
                               "was added below it. Read the card.",
                        text=foreign))
            except Exception as e:
                done["errors"].append(f"{name}: {str(e)[:160]}")
    if create:
        _status["phase"] = "create"
        if not ac.enabled():
            done["errors"].append("Apple push is off (Config > Contacts > "
                                  "Update Apple Contacts).")
        todo = s["missing"] if ac.enabled() else []
        _progress(done=0, total=len(todo), step="creating phone cards")
        for n_c, mp in enumerate(todo, 1):
            _progress(done=n_c, step=mp["name"])
            r = ac.create_card(mp["pid"])
            if r["ok"]:
                apple_changed = True
                done["created"].append({"pid": mp["pid"], "name": mp["name"],
                                        "uid": r["uid"]})
                reviews.append(_review(
                    "created-card", mp["pid"], mp["name"], uid=r["uid"],
                    detail="Created on the phone from the CRM."))
            else:
                done["errors"].append(f"{mp['name']}: {r['detail']}")
    _status["phase"] = "report"
    _progress(step="re-surveying")
    final = survey(ac.load_cards() if apple_changed else cards)
    for sc in final["shared_cards"]:
        names = [q["name"] for q in sc["people"]]
        reviews.append(_review(
            "shared-card", sc["people"][0]["pid"], sc["card"] or _join(names),
            uid=sc["uid"],
            detail=f"{_count(len(names))} CRM people match this phone card: "
                   f"{_join(names)}. Nothing is pushed to it until they are "
                   "merged in the CRM or the card is split.",
            key=f"shared-card:{sc['uid']}"))
    done["review"] = len(reviews)
    public = _public(final)

    def fn(st):
        st["report"] = public
        have = {}
        for r in st["review"]:
            have.setdefault(_review_key(r), r)
        for rv in reviews:
            old = have.get(rv["key"])
            if old is None:
                have[rv["key"]] = rv
                st["review"].append(rv)
            elif (old.get("detail"), old.get("name")) != (rv["detail"], rv["name"]):
                # the same case told differently (a renamed person, one
                # more person on the card): keep the entry, refresh its words
                old["detail"], old["name"] = rv["detail"], rv["name"]
        st["review"] = st["review"][-MAX_REVIEW:]
        st["log"].append({
            "when": now, "reason": reason, "inbound": inbound,
            "push": push, "create": create,
            "imported": len(done["imported"]), "handles": len(done["handles"]),
            "fills": done["fills"], "pushed": len(done["pushed"]),
            "created": len(done["created"]), "review": len(reviews),
            "errors": done["errors"][:20]})
        st["log"] = st["log"][-MAX_LOG:]
        for cr in done["created"]:
            st["created"].append({**cr, "when": now, "undone": False})
    _mutate(fn)
    return {"ok": True, "done": done, "report": public}


def run_async(inbound=True, push=False, create=False, reason="manual"):
    """Start a pass on its own thread; {started} or {busy}."""
    if _status["running"]:
        return {"started": False, "busy": True}
    threading.Thread(target=apply, kwargs={"inbound": inbound, "push": push,
                                           "create": create, "reason": reason},
                     daemon=True, name="vira-contacts-sync-pass").start()
    return {"started": True, "busy": False}


def push_person(pid):
    """The card-save hook: one person's push, with its conflicts and note
    merges filed to the review list like a pass would."""
    r = ac.push_person(pid)
    reviews = []
    from . import data as crm
    p = crm._load()["by_id"].get(pid) or {}
    name = p.get("name") or pid
    for cd in r.get("cards", []):
        for cf in cd.get("conflicts", []):
            reviews.append(_review(
                "field-conflict", pid, name, uid=cd["uid"],
                detail=f"{cf['field']}: Vira says \"{cf['crm']}\", the phone "
                       f"said \"{cf['phone']}\". The phone now shows Vira's "
                       "value; the old one is kept in the card's note."))
        if cd.get("foreign_note"):
            reviews.append(_review(
                "note-merged", pid, name, uid=cd["uid"],
                detail="The phone card had its own note; Vira's block was "
                       "added below it. Read the card."))
    if reviews:
        file_reviews(reviews)
    return r


def file_reviews(reviews):
    """Append entries to the review list, one per key (an entry already
    filed for the same case is left as it is). Returns how many landed."""
    counted = {"n": 0}

    def fn(st):
        seen = {_review_key(x) for x in st["review"]}
        for rv in reviews:
            if rv["key"] not in seen:
                seen.add(rv["key"])
                st["review"].append(rv)
                counted["n"] += 1
        st["review"] = st["review"][-MAX_REVIEW:]
    if reviews:
        _mutate(fn)
    return counted["n"]


def mark_reviewed(ids=None, everything=False, kinds=None):
    """Mark entries read: by id, every entry of the given kinds, or all."""
    want = set(ids or [])
    kinds = set(kinds or [])
    counted = {"n": 0}

    def fn(st):
        for r in st["review"]:
            if r.get("reviewed"):
                continue
            if everything or r.get("id") in want or r.get("kind") in kinds:
                r["reviewed"] = True
                r["reviewed_at"] = _now()
                counted["n"] += 1
    _mutate(fn)
    return {"marked": counted["n"]}


def set_lines(marks, keep=()):
    """The Shared lines buttons. "Same office" marks handles as lines
    (`marks`: [{pid, value}], written through triage, backup-first);
    "Not the same office" keeps a row apart (`keep`: row keys) so it is
    not listed again. Both prune the stored report so the tab is right
    at once; the next pass recomputes it from the cards."""
    from . import data as crm
    changed = triage.mark_lines(marks) if marks else []
    crm.invalidate()
    now_lines = crm.lines()
    keep = [k for k in (keep or []) if isinstance(k, str) and k]

    def fn(st):
        kept = set(st.get("kept_lines") or [])
        kept.update(keep)
        st["kept_lines"] = sorted(kept)
        rep = st.get("report")
        if isinstance(rep, dict):
            _prune_report(rep, now_lines, kept)
    _mutate(fn)
    return {"marked": changed, "kept": keep}


def _prune_report(rep, lines, kept):
    rows = rep.get("shared_lines") or []
    gone = [r for r in rows if _line_key(r) in lines or r["key"] in kept]
    stay = [r for r in rows if r not in gone]
    rep["shared_lines"] = stay
    rep["shared_lines_total"] = len(stay)
    rep["shared_lines_kept"] = int(rep.get("shared_lines_kept") or 0) + \
        sum(1 for r in gone if r["key"] in kept and _line_key(r) not in lines)
    rep["lines"] = len(lines)
    still = {uid for r in stay for uid in r.get("shared_cards", [])}
    dropped = {uid for r in gone if _line_key(r) in lines
               for uid in r.get("shared_cards", [])} - still
    # a conflict on the line itself, or on a card nobody shares any more
    hc = [h for h in rep.get("handle_conflicts") or []
          if (ac._norm_email(h["value"]) if h["kind"] == "email"
              else ac._norm_phone(h["value"])) not in lines
          and h.get("uid") not in dropped]
    rep["handle_conflicts"] = hc
    rep["handle_conflicts_total"] = len(hc)
    sc = [s for s in rep.get("shared_cards") or [] if s.get("uid") not in dropped]
    rep["shared_cards"] = sc
    rep["shared_cards_total"] = len(sc)


def resolve_duplicate(review_id, action):
    """A possible-duplicate entry's two answers. "same": the held phone
    card's handles join the CRM person it looked like. "different": the
    card is imported as its own person after all. Either way the entry
    is marked read."""
    from . import data as crm, onboard
    st = read_store()
    row = next((r for r in st["review"] if r.get("id") == review_id
                and r.get("kind") == "possible-duplicate"), None)
    if not row:
        raise KeyError(review_id)
    card = row.get("card") or {}
    if action == "same":
        handles = [{"kind": "email", "value": e} for e in card.get("emails", [])] + \
                  [{"kind": "phone", "value": p} for p in card.get("phones10", [])]
        out = {"landed": triage.add_handles(row["pid"], handles)}
    elif action == "different":
        r = onboard.import_contacts([card], card.get("source") or "apple-contacts",
                                    check_names=False)
        out = {"added": r["added"]}
    else:
        raise ValueError("action must be same or different")
    crm.invalidate()
    mark_reviewed([review_id])
    return {"ok": True, **out}


def undo_created(uids=None):
    """Delete the cards Vira created (all not yet undone, or the given
    uids). Each is vCard-backed-up first by delete_card."""
    st = read_store()
    want = set(uids or [])
    todo = [c for c in st["created"]
            if not c.get("undone") and c.get("uid") and (not want or c["uid"] in want)]
    out = {"deleted": 0, "errors": []}
    for c in todo:
        r = ac.delete_card(c["uid"])
        if not r["ok"]:
            out["errors"].append(f"{c.get('name')}: {r['detail']}")
            continue
        out["deleted"] += 1
        uid = c["uid"]

        def fn(s2, uid=uid):
            for x in s2["created"]:
                if x.get("uid") == uid:
                    x["undone"] = True
                    x["undone_at"] = _now()
        _mutate(fn)
    return out


def status():
    """What the Sync tab renders."""
    st = read_store()
    ok, why = available()
    pending = [r for r in st["review"] if not r.get("reviewed")]
    kinds = {}
    for r in pending:
        kinds[r.get("kind") or ""] = kinds.get(r.get("kind") or "", 0) + 1
    return {
        "available": ok, "why": why, "enabled": enabled(),
        "push_enabled": ac.enabled(),
        "push_auto": bool(settings.get("contacts_sync_push_auto")),
        "interval": settings.get("contacts_sync_interval"),
        "status": dict(_status),
        "since": _iso(st.get("since")),
        "report": st.get("report"),
        "review": pending[-LIST_CAP:][::-1],
        "review_total": len(pending),
        "review_kinds": kinds,
        "informational_kinds": list(INFORMATIONAL_KINDS),
        "log": st["log"][-10:][::-1],
        "created": sum(1 for c in st["created"] if not c.get("undone")),
    }


# ---------- the worker ----------

def _loop():
    time.sleep(BOOT_DELAY_S)
    while True:
        try:
            if enabled():
                apply(inbound=True,
                      push=bool(settings.get("contacts_sync_push_auto")),
                      create=False, reason="timer")
        except Exception:
            log.exception("contacts sync timer pass failed")
        try:
            minutes = int(settings.get("contacts_sync_interval"))
        except (TypeError, ValueError):
            minutes = 30
        time.sleep(max(5, minutes) * 60)


def start():
    threading.Thread(target=_loop, daemon=True,
                     name="vira-contacts-sync").start()


# ---------- CLI ----------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m server.contactsync")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("survey", help="print the read-only report")
    p = sub.add_parser("pass", help="run one pass")
    p.add_argument("--push", action="store_true", help="also write CRM changes to the Apple cards")
    p.add_argument("--create", action="store_true", help="also create cards for CRM people with none")
    p.add_argument("--no-inbound", action="store_true", help="skip the phone -> CRM half")
    sub.add_parser("undo-created", help="delete every card Vira created that is not yet undone")
    sub.add_parser("status", help="print the Sync tab's status document")
    a = ap.parse_args(argv)
    if a.cmd == "survey":
        out = _public(survey())
    elif a.cmd == "pass":
        out = apply(inbound=not a.no_inbound, push=a.push, create=a.create,
                    reason="cli")
    elif a.cmd == "undo-created":
        out = undo_created()
    elif a.cmd == "status":
        out = status()
    else:
        ap.print_help()
        return 2
    sys.stdout.write(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

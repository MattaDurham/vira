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
  moved, never retried, and is listed once under shared cards and in the
  review list.
- An empty card field (company, title, birthday, location) is filled from
  the phone card, and the phone's own note is kept in Vira as a "Phone
  note" custom row. Nothing the CRM already says is overwritten.

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
from . import jsonstore, settings

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
           "error": None, "reason": None}


# ---------- store ----------

def _blank():
    return {"since": None, "report": None, "review": [], "log": [],
            "created": []}


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


def card_worthy(p):
    """A CRM person the phone should carry: named, reachable, and not one
    the CRM itself marked as skipped or excluded. The tier words are the
    CRM's own (a `*skip` master tier, an `excluded` profile tier); Vira
    reads them and applies this one rule rather than knowing any list."""
    tier = str(p.get("master_tier") or "").lower()
    if p.get("profile_tier") == "excluded" or tier.endswith("skip"):
        return False
    emails, phones = _handles(p)
    return bool((p.get("name") or "").strip()) and bool(emails or phones)


def _apple_location(apple):
    for a in apple.get("addresses", []):
        city, state = a.get("city") or "", a.get("state") or ""
        if city and state:
            return f"{city}, {state}"
        if city:
            return city
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
    matched, matched_uids = {}, set()
    for p in people:
        emails, phones = _handles(p)
        if not (emails or phones):
            continue
        ms = ac.match_cards(cards, idx, emails, phones)
        if ms:
            matched[p["id"]] = ms
            matched_uids.update(m["uid"] for m in ms)
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

        def place(kind, value, pid=pid, name=p["name"]):
            # A handle another CRM person already owns marks a card two
            # people share (a duplicate, a family card). It is never moved;
            # it is listed once as a shared card, not retried as pending work.
            owner = crm.resolve_handle(value)
            if owner and owner != pid:
                other = c["by_id"].get(owner) or {}
                handle_conflicts.append({"pid": pid, "name": name, "kind": kind,
                                         "value": value, "other_pid": owner,
                                         "other_name": other.get("name") or owner})
            else:
                handle_adds.append({"pid": pid, "name": name, "kind": kind,
                                    "value": value})

        for m in ms:
            for e in m.get("emails", []):
                k = ac._norm_email(e["value"])
                if k and "@" in k and k not in crm_e:
                    crm_e.add(k)
                    place("email", e["value"].strip())
            for ph in m.get("phones", []):
                k = ac._norm_phone(ph["value"])
                if len(k) == 10 and k not in crm_p:
                    crm_p.add(k)
                    place("phone", k)
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
        for m, plan in ac.plan_person(pid, ms, card=card, detail=detail):
            if plan["changed"]:
                plans.append((pid, p["name"], m, plan))
                changes.append({"pid": pid, "name": p["name"], "uid": m["uid"],
                                "card": m["name"], "changed": plan["changed"],
                                "conflicts": plan["conflicts"],
                                "foreign_note": plan["foreign_note"]})
    unmatched = [p for p in people if p["id"] not in matched]
    missing = [{"pid": p["id"], "name": p["name"],
                "tier": p.get("profile_tier") or p.get("master_tier")}
               for p in unmatched if card_worthy(p)]
    return {
        "generated": _now(), "cards": len(cards), "people": len(people),
        "matched": len(matched), "since": since, "newest_known": newest_known,
        "new_cards": [_card_row(cd) for cd in new_cards],
        "backlog": len(backlog),
        "backlog_sample": [_card_row(cd) for cd in backlog],
        "handle_adds": handle_adds, "handle_conflicts": handle_conflicts,
        "fills": fills, "changes": changes,
        "missing": missing, "unmatched_skipped": len(unmatched) - len(missing),
        "_new_cards": new_cards, "_plans": plans,
    }


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

def _review(kind, pid, name, *, uid=None, detail="", text="", key=None):
    """One entry for the owner to read. `key` is what makes it the same
    case on a later pass (the default folds in the detail text)."""
    return {"id": "rv_" + uuid.uuid4().hex[:10], "kind": kind, "pid": pid,
            "name": name, "uid": uid, "when": _now(), "detail": detail,
            "text": (text or "")[:NOTE_EXCERPT], "reviewed": False,
            "key": key or f"{kind}:{pid}:{uid}:{detail}"}


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
                   reason=reason)
    try:
        from . import contactcard, data as crm, onboard, triage
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
                                     "emails": emails, "phones10": phones})
            if contacts:
                onboard.import_contacts(contacts, "apple-contacts")
                crm.invalidate()
                for ct in contacts:
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
            for item in s["handle_adds"]:
                try:
                    triage.add_handles(item["pid"], [{"kind": item["kind"],
                                                      "value": item["value"]}])
                    done["handles"].append(item)
                except ValueError as e:
                    reviews.append(_review(
                        "handle-conflict", item["pid"], item["name"],
                        detail=f"The phone card carries {item['kind']} "
                               f"{item['value']}: {e}",
                        key=f"handle-conflict:{item['pid']}:{item['value']}"))
                except (KeyError, OSError) as e:
                    done["errors"].append(f"{item['name']}: {str(e)[:120]}")
            for item in s["handle_conflicts"]:
                reviews.append(_review(
                    "handle-conflict", item["pid"], item["name"],
                    detail=f"The phone card carries {item['kind']} {item['value']}, "
                           f"which belongs to {item['other_name']} in the CRM: the "
                           "two share one phone card. Merge them in the CRM or "
                           "split the card; nothing was moved.",
                    key=f"handle-conflict:{item['pid']}:{item['value']}"))
            for f in s["fills"]:
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
            for pid, name, apple, plan in (s["_plans"] if ac.enabled() else []):
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
            for mp in (s["missing"] if ac.enabled() else []):
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
        final = survey(ac.load_cards() if apple_changed else cards)
        done["review"] = len(reviews)
        public = _public(final)

        def fn(st):
            st["report"] = public
            seen = {_review_key(r) for r in st["review"]}
            for rv in reviews:
                if rv["key"] not in seen:
                    seen.add(rv["key"])
                    st["review"].append(rv)
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
    except Exception as e:
        log.exception("contacts sync pass failed")
        _status["error"] = str(e)[:300]
        return {"ok": False, "detail": str(e)[:300]}
    finally:
        _status.update(running=False, phase="", finished=_now())
        _run_lock.release()


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
        def fn(st):
            seen = {_review_key(x) for x in st["review"]}
            for rv in reviews:
                if rv["key"] not in seen:
                    seen.add(rv["key"])
                    st["review"].append(rv)
            st["review"] = st["review"][-MAX_REVIEW:]
        _mutate(fn)
    return r


def mark_reviewed(ids=None, everything=False):
    want = set(ids or [])
    counted = {"n": 0}

    def fn(st):
        for r in st["review"]:
            if not r.get("reviewed") and (everything or r.get("id") in want):
                r["reviewed"] = True
                r["reviewed_at"] = _now()
                counted["n"] += 1
    _mutate(fn)
    return {"marked": counted["n"]}


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

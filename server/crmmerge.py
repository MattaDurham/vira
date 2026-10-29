"""Duplicates in the CRM, and the merge that folds one record into another.

Until 2026-10-28 a duplicate registry record was fixed by hand: the June
2026 cleanup merged 22 pairs with a one-off script, and the ids it retired
went dark everywhere they were still written. This module is the standing
tool: `candidates()` lists the pairs that read as one person, the owner
decides each one from People > Sync (Merge, or Not the same), and
`merge()` does what the script did, through Vira's own write paths.

What makes a pair a candidate, all deterministic:
- two registry records carry the same email or phone, and that handle is
  not a shared line (an office number the owner marked, server/triage.py)
  and the names could be one person (server/names.py) or one of them is a
  triage placeholder;
- two records match the same phone card (the sync report's shared cards)
  and their names could be one person; a shared card whose people have
  different names is an office line and belongs to the Shared lines list;
- two records share a surname and a first name, or nickname-equivalent
  first names ("Matt" and "Matthew").
A pair both of whose records are skipped is nobody's problem and is left
out. "Not the same" is remembered in data/crm-merge.json, so the pair is
never asked again.

The merge keeps one record and drops the other, nothing deleted that is
not carried: handles are unioned, survivor first; the dropped record's
card overrides (data/contact-cards.json) fill what the survivor's lack;
its profile's hooks, open loops, facts and topics are appended to the
survivor's profile, each stamped `carried_from`, and its profile file
is retired into the CRM's backups; `refs.merged_from` records the
retired id, which still resolves to the survivor everywhere
(data.canonical_id); one "merged" entry lands in the review list. The
registry is backed up before the write, as every write through triage
is. Phone duplicates (several cards for one person) are listed beside the
CRM pairs from applecontacts.find_duplicates and folded by its fold_cards.
"""
import json
import shutil
import time
from pathlib import Path

from . import applecontacts as ac
from . import contactcard, contactsync, jsonstore, names, triage
from . import data as crm
from .filelock import locked

DATA = Path(__file__).resolve().parent.parent / "data"
STORE = DATA / "crm-merge.json"


def _blank():
    return {"not_same": [], "not_same_cards": [], "merges": [], "folds": []}


def _store():
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


def pair_key(a, b):
    return ":".join(sorted([a, b]))


def group_key(uids):
    return "|".join(sorted(uids))


def _activity(p):
    a = p.get("activity") or {}
    return (a.get("imsg_n") or 0) + (a.get("email_n") or 0)


def _row(p, profiles):
    h = p.get("handles") or {}
    return {"pid": p["id"], "name": p.get("name") or "",
            "tier": p.get("profile_tier") or p.get("master_tier"),
            "emails": [e for e in (h.get("emails") or []) if e][:4],
            "phones": [contactcard.format_phone(x) for x in (h.get("phones10") or []) if x][:4],
            "messages": _activity(p), "has_profile": p["id"] in profiles,
            "skipped": triage.skipped(p)}


def _keep_first(a, b, profiles):
    """Which of a pair to suggest keeping: the one with a dossier, then
    more messages, then more handles, then the longer name."""
    def score(p):
        h = p.get("handles") or {}
        return (not triage.skipped(p), p["id"] in profiles, _activity(p),
                len(h.get("emails") or []) + len(h.get("phones10") or []),
                len(p.get("name") or ""))
    return score(a) >= score(b)


def candidates(report=None):
    """CRM pairs that read as one person, best-evidenced first. `report`
    is the stored sync report (its shared cards); None reads it."""
    c = crm._load()
    people = list(c["by_id"].values())
    profiles = c["profiles"]
    lines = set(c.get("lines") or ())
    not_same = set(_store()["not_same"])
    pairs = {}

    def add(a, b, why):
        if a["id"] == b["id"]:
            return
        if triage.skipped(a) and triage.skipped(b):
            return
        k = pair_key(a["id"], b["id"])
        if k in not_same:
            return
        if not _keep_first(a, b, profiles):
            a, b = b, a
        row = pairs.setdefault(k, {"key": k, "keep": a["id"], "drop": b["id"],
                                   "a": _row(a, profiles), "b": _row(b, profiles),
                                   "why": []})
        if why not in row["why"]:
            row["why"].append(why)

    def one_person(a, b):
        return names.compatible(a.get("name") or "", b.get("name") or "") \
            or triage._is_placeholder(a.get("name")) \
            or triage._is_placeholder(b.get("name"))

    carriers = {}
    for p in people:
        for key in triage._handle_keys(p):
            carriers.setdefault(key, []).append(p)
    for key, ps in carriers.items():
        if key in lines or len(ps) < 2:
            continue
        shown = key if "@" in key else contactcard.format_phone(key)
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                if one_person(ps[i], ps[j]):
                    add(ps[i], ps[j], f"both carry {shown}")
    if report is None:
        report = contactsync.read_store().get("report") or {}
    for sc in (report or {}).get("shared_cards") or []:
        ps = [c["by_id"].get(q.get("pid")) for q in sc.get("people") or []]
        ps = [p for p in ps if p]
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                if names.compatible(ps[i].get("name") or "", ps[j].get("name") or ""):
                    add(ps[i], ps[j], f"both match the phone card {sc.get('card') or sc.get('uid')}")
    by_surname = {}
    for p in people:
        first, last = names.split(p.get("name") or "")
        if first and last:
            by_surname.setdefault(names.surname_key(last), []).append((first, p))
    for rows in by_surname.values():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                (fa, pa), (fb, pb) = rows[i], rows[j]
                why = names.why(pa["name"], pb["name"])
                if why:
                    add(pa, pb, why)
    out = list(pairs.values())
    out.sort(key=lambda r: (-len(r["why"]), r["a"]["name"].lower()))
    return out


def phone_groups(cards=None):
    """Phone cards that read as one person (applecontacts.find_duplicates),
    minus the groups the owner kept apart."""
    if cards is None:
        cards = ac.load_cards()
    kept = set(_store()["not_same_cards"])
    out = []
    for g in ac.find_duplicates(cards, lines=crm.lines()):
        g["key"] = group_key(g["uids"])
        if g["key"] not in kept:
            out.append(g)
    return out


def duplicates(cards=None, with_phone=True):
    """What the Duplicates section renders."""
    out = {"pairs": candidates(), "phone_groups": []}
    if with_phone and ac.enabled():
        try:
            out["phone_groups"] = phone_groups(cards)
        except Exception as e:  # noqa: BLE001 - the CRM half still renders
            out["phone_error"] = str(e)[:200]
    return out


def not_same(key=None, uids=None):
    """Remember a decision: this pair (or this group of cards) is not one
    person, so it is never listed again."""
    def fn(st):
        if key:
            if key not in st["not_same"]:
                st["not_same"].append(key)
        if uids:
            k = group_key(uids)
            if k not in st["not_same_cards"]:
                st["not_same_cards"].append(k)
    _mutate(fn)
    return {"ok": True}


def _union(a, b):
    return list(a) + [x for x in b if x not in a]


def _carry_profile(keep, drop, keep_name, stamp):
    """The dropped record's profile sections join the survivor's profile,
    each carried item stamped with where it came from; the dropped file is
    retired into the CRM's profile backups. Returns what was carried."""
    drop_path = crm._profile_path(drop)
    if not drop_path.exists():
        return {}
    keep_path = crm._profile_path(keep)
    carried = {}
    with crm._write_lock, locked(keep_path):
        prof = crm._load_profile_for_write(keep, {"name": keep_name})
        try:
            dp = json.loads(drop_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            dp = {}
        for field in ("hooks", "open_loops", "personal_facts", "topics"):
            have = [json.dumps(x, sort_keys=True) for x in prof.get(field) or []]
            n = 0
            for x in dp.get(field) or []:
                if json.dumps(x, sort_keys=True) in have:
                    continue
                if isinstance(x, dict):
                    x = dict(x, carried_from=drop)
                prof.setdefault(field, []).append(x)
                n += 1
            if n:
                carried[field] = n
                prof[f"{field}_updated_by_vira"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        for field in ("relationship_summary", "how_we_met", "comms_style",
                      "relationship_class"):
            if not prof.get(field) and dp.get(field):
                prof[field] = dp[field]
                carried[field] = 1
        prof.setdefault("merged_from_profile", [])
        if isinstance(prof["merged_from_profile"], str):
            prof["merged_from_profile"] = [prof["merged_from_profile"]]
        prof["merged_from_profile"].append(drop)
        prof.setdefault("id", keep)
        prof.setdefault("name", keep_name)
        keep_path.parent.mkdir(parents=True, exist_ok=True)
        jsonstore.write_atomic(keep_path, prof, indent=1, ensure_ascii=False)
        retired = crm._backups()
        retired.mkdir(parents=True, exist_ok=True)
        shutil.move(str(drop_path), str(retired / f"retired-{drop}-{stamp}.json"))
    return carried


def _carry_overrides(keep, drop):
    """The dropped record's card overrides fill what the survivor's lack."""
    moved = {"n": 0}

    def fn(st):
        cards = st.setdefault("cards", {})
        dc = cards.pop(drop, None)
        if not dc:
            return st
        kc = cards.setdefault(keep, {"fields": {}, "handles": {}, "custom": []})
        for f, v in (dc.get("fields") or {}).items():
            if v and not (kc.get("fields") or {}).get(f):
                kc.setdefault("fields", {})[f] = v
                moved["n"] += 1
        for h, v in (dc.get("handles") or {}).items():
            if kc.setdefault("handles", {}).setdefault(h, v) is v:
                moved["n"] += 1
        have = {(x.get("label"), x.get("value")) for x in kc.get("custom") or []}
        for x in dc.get("custom") or []:
            if (x.get("label"), x.get("value")) not in have \
                    and len(kc.setdefault("custom", [])) < contactcard.MAX_CUSTOM:
                kc["custom"].append(x)
                moved["n"] += 1
        kc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return st
    jsonstore.mutate(contactcard.STORE, fn, contactcard._blank(), indent=1,
                     ensure_ascii=False)
    return moved["n"]


def merge(keep, drop):
    """Fold registry record `drop` into `keep`. Backup-first, one registry
    write; the dropped id keeps resolving to the survivor."""
    if not keep or not drop or keep == drop:
        raise ValueError("merge needs two different person ids")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    today = time.strftime("%Y-%m-%d")
    with triage._lock:
        crm.invalidate()
        doc = triage._read_people_backed_up()
        by_id = {p["id"]: p for p in doc["people"]}
        if keep not in by_id:
            raise KeyError(keep)
        if drop not in by_id:
            raise KeyError(drop)
        k, d = by_id[keep], by_id[drop]
        kh = k.setdefault("handles", {})
        dh = d.get("handles") or {}
        n_handles = 0
        for bucket in ("imessage", "phones10", "emails", "lines"):
            before = len(kh.get(bucket) or [])
            kh[bucket] = _union(kh.get(bucket) or [], dh.get(bucket) or [])
            if bucket != "imessage":
                n_handles += len(kh[bucket]) - before
        refs = k.setdefault("refs", {})
        refs["card_names"] = sorted((set(refs.get("card_names") or [])
                                     | set((d.get("refs") or {}).get("card_names") or [])
                                     | {d.get("name") or ""}) - {""})
        merged = list(refs.get("merged_from") or [])
        merged.append({"id": drop, "name": d.get("name") or "", "when": today,
                       "by": "crmmerge"})
        merged += [m for m in (d.get("refs") or {}).get("merged_from") or []
                   if m not in merged]
        refs["merged_from"] = merged
        for f in ("master_tier", "profile_tier", "class_hint"):
            if not k.get(f) and d.get(f):
                k[f] = d[f]
        if triage.skipped(k) and not triage.skipped(d):
            k["master_tier"], k["profile_tier"] = d.get("master_tier"), d.get("profile_tier")
        a, b = k.get("activity") or {}, d.get("activity") or {}
        act = dict(a)
        for f in ("imsg_n", "email_n"):
            act[f] = (a.get(f) or 0) + (b.get(f) or 0)
        for f, pick in (("imsg_first", min), ("imsg_last", max), ("email_last", max)):
            vals = [x for x in (a.get(f), b.get(f)) if x]
            if vals:
                act[f] = pick(vals)
        k["activity"] = act
        doc["people"] = [p for p in doc["people"] if p["id"] != drop]
        keep_name, drop_name = k.get("name") or keep, d.get("name") or drop
        triage._write_people(doc)
    carried = _carry_profile(keep, drop, keep_name, stamp)
    n_over = _carry_overrides(keep, drop)
    crm.invalidate()
    what = [f"{n_handles} handles joined"]
    if carried:
        what.append("profile notes carried: " + ", ".join(
            f"{v} {f.replace('_', ' ')}" for f, v in carried.items()))
    if n_over:
        what.append(f"{n_over} card rows carried")
    contactsync.file_reviews([contactsync.review_entry(
        "merged", keep, keep_name,
        detail=f"{drop_name} was merged into {keep_name}: {'; '.join(what)}. "
               "The old record's id still opens this person.",
        key=f"merged:{keep}:{drop}")])
    _mutate(lambda st: st["merges"].append(
        {"when": stamp, "keep": keep, "drop": drop, "keep_name": keep_name,
         "drop_name": drop_name, "handles": n_handles, "carried": carried,
         "card_rows": n_over}))
    return {"ok": True, "keep": keep, "drop": drop, "handles": n_handles,
            "carried": carried, "card_rows": n_over}


def fold(keep_uid, drop_uids):
    """Fold duplicate phone cards (applecontacts.fold_cards) and log it."""
    r = ac.fold_cards(keep_uid, drop_uids)
    if r.get("ok"):
        _mutate(lambda st: st["folds"].append(
            {"when": time.strftime("%Y%m%d-%H%M%S"), "keep": keep_uid,
             "dropped": r.get("deleted", []), "pushed": r.get("pushed", [])}))
    return r

"""Event radar: the plans hiding in the owner's threads become calendar
entries and one-tap replies.

The mental load this removes: a group thread mentions a barbecue Saturday,
an invite lands in a 1:1, a school thing gets proposed — and the owner has
to notice it, remember it, put it somewhere, tell their partner, and
eventually answer. Vira already reads every one of those threads; nothing
was joining the dots. This module does:

  scan     one deterministic sweep of every chat active in the window
           (groups AND 1:1s), a cheap regex gate, then ONE cached model
           pass per changed thread extracting concrete event proposals —
           the groupchat.brief() pattern exactly (sha1 key, MAX-ROWID
           invalidation stamp, jsonstore, defensive sanitizer).
  hold     each future event auto-lands on the calendar as a tentative
           hold via Calendar.app AppleScript (the applecontacts.py spoke
           pattern: component-built dates, _esc, ensure-running) — so the
           plan exists BEFORE the RSVP, and if the target calendar is one
           shared with the owner's partner, they see it with zero sends.
  drafts   for each event needing an answer, two messages are drafted at
           scan time in the owner's evidenced voice: the RSVP back into
           the source thread, and the partner FYI. Drafts are stored, not
           sent.
  one tap  the review queue (and therefore the Daily Brief) carries one
           row per open event with the draft text visible on the row —
           send.py's confirmation contract — and actions that post the
           send / create the hold / drop it. Zero new UI: reviewqueue
           rows are fully server-driven.

Nothing here sends or writes anywhere on its own except the tentative
calendar hold, which the owner enables by naming a target calendar in
config ("events_calendar"). Every outbound message is one explicit tap in
the review queue. Passive instances (VIRA_PASSIVE) never scan, never
write the store, and send.py already refuses their sends.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import data as crm
from . import groupchat, imessage, jsonstore, settings
from .jobshared import now_iso

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "event-radar.json"

SCAN_DAYS = 14            # how far back a proposal can sit and still matter
TAIL = 60                 # messages of context per thread for the model
MAX_MODEL_THREADS = 12    # cost ceiling per scan: only this many changed,
                          # event-ish threads get a model pass
OSA_TIMEOUT = 30

# The cheap gate: a thread earns a model pass only if the window contains
# something event-shaped. Wide on purpose — the model is the precision
# layer; this only pays for recall.
EVENTISH = re.compile(
    r"\b(party|parties|dinner|lunch|brunch|breakfast|drinks|bbq|barbecue|"
    r"birthday|bday|wedding|shower|playdate|play date|sleepover|invite|"
    r"invited|invitation|rsvp|join us|come over|come by|stop by|swing by|"
    r"get together|get-together|hang out|hangout|game night|movie night|"
    r"potluck|picnic|reunion|housewarming|reservation|tickets?|concert|"
    r"recital|graduation|bar mitzvah|bat mitzvah|christening|baptism|"
    r"save the date|what time works|are you (?:guys )?free|you guys free|"
    r"can you (?:guys )?(?:come|make it)|we'?re hosting|hosting)\b", re.I)

_scan_lock = threading.Lock()


def _owner():
    """The owner's own person_id, so the radar never reads the owner's
    self-thread (Vira's notification channel) as a social thread."""
    try:
        from . import atlas
        return atlas.owner_pid()
    except Exception:  # noqa: BLE001
        return None


def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


# ---------- the sweep: which threads were active, what did they say ------

def _cutoff_ns(days):
    return imessage.apple_ns(datetime.now().astimezone()
                             - timedelta(days=days))


def _active_chats(days=SCAN_DAYS):
    """Every chat with a real message in the window, one query.
    style 43 = group, 45 = 1:1."""
    con = imessage._connect()
    try:
        return con.execute(
            """SELECT c.ROWID, c.style, MAX(m.ROWID), COUNT(*)
               FROM message m
               JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
               JOIN chat c ON c.ROWID = cmj.chat_id
               WHERE m.date > ?
                 AND (m.associated_message_type = 0
                      OR m.associated_message_type IS NULL)
                 AND m.item_type = 0
               GROUP BY c.ROWID""", (_cutoff_ns(days),)).fetchall()
    finally:
        con.close()


def _windowed_messages(chat_ids, days=SCAN_DAYS):
    """Recent real messages across a set of chat legs, oldest first, with
    sender attribution resolved through the CRM."""
    q = ",".join("?" * len(chat_ids))
    con = imessage._connect()
    try:
        rows = con.execute(
            f"""SELECT m.ROWID, m.date, m.is_from_me, m.text,
                       m.attributedBody, h.id
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE cmj.chat_id IN ({q}) AND m.date > ?
                  AND (m.associated_message_type = 0
                       OR m.associated_message_type IS NULL)
                  AND m.item_type = 0
                ORDER BY m.date""",
            (*chat_ids, _cutoff_ns(days))).fetchall()
    finally:
        con.close()
    out, names = [], {}
    for rowid, dt, from_me, text, blob, handle in rows:
        t = imessage.msg_text(text, blob)
        if not t:
            continue
        if from_me:
            who, pid = "me", None
        else:
            if handle not in names:
                p = crm.resolve_handle(handle)
                nm = (crm._load()["by_id"].get(p) or {}).get("name") if p else None
                names[handle] = (nm or handle or "?", p)
            who, pid = names[handle]
        when = imessage.apple_dt(dt)
        out.append({"rowid": rowid, "when": when, "who": who, "pid": pid,
                    "from_me": bool(from_me), "text": t})
    return out


# ---------- extraction: one cached model pass per changed thread ---------

EXTRACT_PROMPT = """You are Vira, {owner}'s chief of staff, reading one
message thread for CONCRETE upcoming plans and invitations.

Today is {today}. Thread: {label}
Participants: {participants}
{known}

THREAD (oldest first; "me" = {owner}):
{tail}

Find every SPECIFIC event proposal: a gathering, meal, party, visit, or
appointment with at least an approximate date. Ignore vague someday-talk
("we should hang out sometime"), past events, and anything already
declined in the thread.

Return ONLY a JSON object:
{{"events": [{{"title": "short specific name, e.g. Alex's BBQ",
   "date": "YYYY-MM-DD",
   "time": "HH:MM 24h or empty if unstated",
   "end_time": "HH:MM or empty",
   "location": "as stated or empty",
   "organizer": "participant name or 'me'",
   "status": "proposed|confirmed",
   "needs_reply": true/false,
   "confidence": 0.0-1.0,
   "quote": "the single message line that proposes it, verbatim"}}]}}

Ground every field in the thread — never invent a date, time, or place.
If the thread names a weekday without a date, resolve it to the NEXT such
weekday after today. Empty list is a fine answer."""


def _clean_events(raw):
    if not isinstance(raw, dict):
        raise ValueError("not a dict")
    out = []
    for e in (raw.get("events") or [])[:8]:
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "").strip()[:80]
        date = str(e.get("date") or "").strip()[:10]
        try:
            datetime.fromisoformat(date)
        except ValueError:
            continue
        if not title:
            continue
        out.append({
            "title": title, "date": date,
            "time": str(e.get("time") or "")[:5],
            "end_time": str(e.get("end_time") or "")[:5],
            "location": str(e.get("location") or "")[:120],
            "organizer": str(e.get("organizer") or "")[:60],
            "status": e.get("status") if e.get("status") in
                      ("proposed", "confirmed") else "proposed",
            "needs_reply": bool(e.get("needs_reply")),
            "confidence": max(0.0, min(1.0, float(e.get("confidence") or 0))),
            "quote": str(e.get("quote") or "")[:200],
        })
    return out


def event_key(thread_key, title, date):
    """Content-stable across rescans: the review queue's act() address."""
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(f"{thread_key}|{norm}|{date}".encode()).hexdigest()[:16]


# ---------- drafting: the two messages, ready before the ask -------------

DRAFT_PROMPT = """You are drafting two messages for {owner}. Match
{owner}'s own voice as evidenced in the thread (the "me" lines) — length,
warmth, punctuation habits. Never invent facts.

The event: {event}
Thread: {label} — participants: {participants}
{link_context}
THREAD (oldest first; "me" = {owner}):
{tail}

{facts}

Message 1 — the reply into this thread answering the proposal. Warm,
specific to what was actually proposed (name the day), and it should
close the loop: a clear yes-shaped answer that still leaves room to
confirm details ("we're in for Saturday — what can we bring?" energy,
in {owner}'s voice, not that literal text).

Message 2 — a short FYI to {partner}, {owner}'s partner, who may not be
in this thread: what the event is, when, who's hosting, so the two of
them can talk before {owner} formally confirms. Plain and warm.

Keep each message under 300 characters — it is read in full on a
review row before the tap.

Return ONLY a JSON object:
{{"reply": "...", "partner_fyi": "..."}}"""


def _link_context(msgs):
    """URLs shared in the window, quoted to the model verbatim. They are
    NOT fetched: the scan runs unattended in the background, and fetching
    attacker-suppliable URLs from message text is an egress nobody
    approved (the adversarial review flagged the fetching version)."""
    urls = []
    for m in msgs:
        urls += re.findall(r"https?://\S+", m["text"])[:2]
    urls = list(dict.fromkeys(urls))[:3]
    if not urls:
        return ""
    return "Links shared in the thread (not fetched — quote what the " \
           "thread itself says about them):\n" + \
           "\n".join(f"- {u[:160]}" for u in urls)


def _draft(ev, msgs, label, participants):
    from . import suggest, threadread
    owner = settings.get("owner_name") or "the owner"
    partner_pid = fyi_person()
    partner = ((crm._load()["by_id"].get(partner_pid) or {}).get("name")
               or "their partner") if partner_pid else "their partner"
    facts = ""
    org_pid = next((m["pid"] for m in msgs
                    if m["pid"] and m["who"] == ev.get("organizer")), None)
    if org_pid:
        try:
            facts = threadread.facts_block(org_pid)
        except Exception:  # noqa: BLE001
            facts = ""
    tail = "\n".join(f"[{m['who']}] {m['text'][:200]}" for m in msgs[-TAIL:])
    prompt = DRAFT_PROMPT.format(
        owner=owner, partner=partner,
        event=json.dumps({k: ev[k] for k in
                          ("title", "date", "time", "location", "organizer",
                           "quote")}),
        label=label, participants=", ".join(participants)[:300],
        link_context=_link_context(msgs), tail=tail[:12000], facts=facts)
    raw = suggest._extract_json(suggest.complete(prompt))
    return {"reply": str(raw.get("reply") or "")[:900],
            "partner_fyi": str(raw.get("partner_fyi") or "")[:600]}


SPOUSE = re.compile(r"\b(wife|husband|spouse)\b", re.I)
NOT_SPOUSE = re.compile(r"\b(ex[- ]|former|business|estranged|late)\b", re.I)


def fyi_person():
    """The partner who gets the FYI. Config wins. Auto-detection is
    deliberately strict — this chooses the recipient of an outbound
    message, and the review found the loose version picking a business
    partner over the actual spouse on live data. Only a FAMILY-classed
    contact whose relationship says wife/husband/spouse (and not ex-,
    former, business...) qualifies; anything less returns None and the
    action tells the owner to set events_fyi_person."""
    pid = settings.get("events_fyi_person")
    if pid:
        return pid
    c = crm._load()
    for cand, prof in (c.get("profiles") or {}).items():
        if (prof.get("relationship_class") or "").lower() != "family":
            continue
        rel = str((c.get("master", {}).get(cand) or {})
                  .get("relationship") or "")
        if SPOUSE.search(rel) and not NOT_SPOUSE.search(rel):
            return cand
    return None


# ---------- the tentative calendar hold ----------------------------------

def _esc(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"') \
                    .replace("\n", " ").replace("\r", " ").strip()


MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _date_lines(var, dt):
    """Component-built AppleScript dates (never locale literals); day set
    to 1 first so a day-31 start can't roll the month."""
    return [f"set {var} to current date",
            f"set day of {var} to 1",
            f"set year of {var} to {dt.year}",
            f"set month of {var} to {MONTHS[dt.month - 1]}",
            f"set day of {var} to {dt.day}",
            f"set time of {var} to {dt.hour * 3600 + dt.minute * 60}"]


def _hm(dt, hm, default="12:00"):
    h, m = (hm or default).split(":")
    return dt.replace(hour=int(h), minute=int(m))


def build_event_script(cal, title, date_iso, hm, end_hm, location, notes):
    d = datetime.fromisoformat(date_iso)
    start = _hm(d, hm)
    if end_hm:
        end = _hm(d, end_hm)
        if end <= start:          # "7pm to 1am" crosses midnight
            end += timedelta(days=1)
    elif hm:
        end = start + timedelta(hours=2)   # real arithmetic: 23:00 -> 01:00
    else:                                  # all-day-ish default block
        end = _hm(d, "14:00")
    lines = _date_lines("d1", start)
    lines += _date_lines("d2", end)
    props = (f'{{summary:"{_esc(title)}", start date:d1, end date:d2'
             + (f', location:"{_esc(location)}"' if location else "")
             + f', description:"{_esc(notes)}"}}')
    lines += ['tell application "Calendar"',
              f'  tell calendar "{_esc(cal)}" to make new event '
              f'with properties {props}',
              "end tell", 'return "ok"']
    return "\n".join(lines)


def _ensure_calendar_running():
    r = subprocess.run(["pgrep", "-x", "Calendar"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["open", "-g", "-j", "-a", "Calendar"],
                       capture_output=True, timeout=15)
        time.sleep(2)


def create_hold(ev):
    """The tentative hold: on the calendar before the RSVP, so the plan is
    visible (to both partners, when the target calendar is shared) while
    it is still a conversation."""
    cal = settings.get("events_calendar")
    if not cal:
        raise RuntimeError('no target calendar configured — set '
                           '"events_calendar" in config.json to the '
                           'Calendar.app calendar tentative holds land on '
                           '(a calendar shared with your partner makes the '
                           'FYI automatic)')
    if not settings.IS_MAC or settings.fixture_mode() or _passive():
        raise RuntimeError("calendar holds need macOS (Calendar.app) and "
                           "an active (non-passive) instance")
    notes = (f"Tentative — detected by Vira from \"{ev.get('thread_label')}\""
             f"\nProposed: {ev.get('quote') or ''}"
             f"\nReply from the Daily Brief when decided.")
    script = build_event_script(
        cal, f"Tentative · {ev['title']}", ev["date"], ev.get("time"),
        ev.get("end_time"), ev.get("location"), notes)
    _ensure_calendar_running()
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=OSA_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return True


# ---------- the scan ------------------------------------------------------

def _thread_units(days=SCAN_DAYS):
    """Active chats folded into logical threads: merged groups (deduped by
    leg set) and 1:1 chats, each carrying its cache key and send address."""
    units, seen_groups = [], set()
    for rowid, style, latest, _count in _active_chats(days):
        if style == 43:
            g = groupchat.resolve_group(rowid)
            if not g:
                continue
            legs = frozenset(g["chat_ids"])
            if legs in seen_groups:
                continue
            seen_groups.add(legs)
            names = [p.get("name") or p.get("handle") or "?"
                     for p in g.get("participants") or []]
            units.append({
                "key": hashlib.sha1(",".join(
                    str(i) for i in sorted(g["chat_ids"])).encode()
                    ).hexdigest()[:16],
                "chat_ids": list(g["chat_ids"]),
                "label": g.get("name") or ", ".join(names[:4]),
                "participants": names,
                "send": g.get("send"),
            })
        elif style == 45:
            units.append({"key": f"c{rowid}", "chat_ids": [rowid],
                          "label": "", "participants": [], "send": None})
    return units


def scan(force=False, days=SCAN_DAYS):
    """The whole pass. Deterministic sweep -> regex gate -> cached model
    extraction -> store merge -> auto holds + drafts for what's new.
    Per-thread failures are contained; the scan always completes."""
    if settings.fixture_mode() or _passive():
        return {"status": "skipped"}
    from . import suggest
    with _scan_lock:
        snapshot = jsonstore.read(STORE, {})
        threads = dict(snapshot.get("threads") or {})
        events = dict(snapshot.get("events") or {})
        stamps, changes = {}, {}   # what this pass learned — merged at the
        touched = modeled = 0      # end into a FRESH read, so an owner tap
        for u in _thread_units(days):        # landing mid-scan survives
            try:
                latest = groupchat._latest_rowid(u["chat_ids"])
                hit = threads.get(u["key"])
                if hit and hit.get("latest") == latest and not force:
                    continue
                msgs = _windowed_messages(u["chat_ids"], days)
                if not msgs:
                    continue
                if not u["label"]:   # 1:1 — label by the counterpart
                    other = next((m for m in msgs if not m["from_me"]), None)
                    if other and other["pid"] and other["pid"] == _owner():
                        # the owner's own notify channel — Vira reading
                        # Vira is a loop nobody asked for
                        stamps[u["key"]] = {"latest": latest,
                                            "seen": now_iso()}
                        continue
                    u["label"] = other["who"] if other else "1:1"
                    u["participants"] = [u["label"]]
                text = " ".join(m["text"] for m in msgs)
                if not EVENTISH.search(text):
                    stamps[u["key"]] = {"latest": latest, "seen": now_iso()}
                    continue
                if modeled >= MAX_MODEL_THREADS:
                    continue   # NO stamp: the ceiling defers, never drops —
                               # the next scan picks this thread up
                modeled += 1
                tail = "\n".join(f"[{m['who']}] {m['text'][:200]}"
                                 for m in msgs[-TAIL:])
                prompt = EXTRACT_PROMPT.format(
                    owner=settings.get("owner_name") or "the owner",
                    today=datetime.now().strftime("%A %Y-%m-%d"),
                    label=u["label"],
                    participants=", ".join(u["participants"])[:300],
                    known=_known_lines(events, u["key"]),
                    tail=tail[:14000])
                found = _clean_events(
                    suggest._extract_json(suggest.complete(prompt)))
                touched += _merge(events, changes, found, u, msgs, latest)
                # the stamp lands ONLY on success: a failed model pass
                # leaves the thread unstamped and the next scan retries
                # (same defer-never-drop contract as the ceiling above)
                stamps[u["key"]] = {"latest": latest, "seen": now_iso()}
            except Exception:  # noqa: BLE001 — one thread never sinks the scan
                continue
        today = datetime.now().date().isoformat()

        def commit(s):
            s.setdefault("threads", {}).update(stamps)
            se = s.setdefault("events", {})
            for k, ev in changes.items():
                cur = se.get(k)
                if cur:
                    # the owner's decisions on disk outrank the scan's
                    # stale snapshot — always
                    for f in ("state", "decided", "reply_sent", "fyi_sent"):
                        if f in cur:
                            ev[f] = cur[f]
                se[k] = ev
            for e in se.values():   # past events leave the queue
                if e.get("date", "") < today and \
                        e.get("state") in (None, "new", "calendared"):
                    e["state"] = "past"
            return s

        jsonstore.mutate(STORE, commit, {})
        return {"status": "ok", "threads_touched": touched,
                "model_passes": modeled, "open": len(pending())}


def _tokens(title):
    return set(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def _find_existing(events, thread_key, title, date):
    """Exact key, else a same-thread same-date event whose title shares
    most of its words — model titles drift between rescans ("BBQ" vs
    "Alex's BBQ") and a drifted title must never mint a second hold."""
    k = event_key(thread_key, title, date)
    if k in events:
        return k
    t = _tokens(title)
    for ek, e in events.items():
        if e.get("thread_key") != thread_key or e.get("date") != date:
            continue
        u = _tokens(e.get("title", ""))
        if t and u and len(t & u) / min(len(t), len(u)) >= 0.5:
            return ek
    return None


def _known_lines(events, thread_key):
    """What is already tracked for this thread, told to the extractor so
    it reuses titles and dates instead of minting drifted duplicates."""
    known = [e for e in events.values()
             if e.get("thread_key") == thread_key
             and e.get("state") not in ("past",)]
    if not known:
        return ""
    return "Already tracked from this thread (reuse these exact titles " \
           "and dates if they are the same event):\n" + "\n".join(
               f"- {e['title']} on {e['date']}" for e in known[:6])


def _merge(events, changes, found, unit, msgs, latest):
    """New events enter the store once, keep their state forever after,
    and pick up their hold + drafts on arrival. Everything this pass
    learns goes into `changes`; scan()'s commit merges it into a fresh
    read so mid-scan owner decisions survive."""
    n = 0
    today = datetime.now().date().isoformat()
    for ev in found:
        if ev["date"] < today:
            continue
        k = _find_existing(events, unit["key"], ev["title"], ev["date"]) \
            or event_key(unit["key"], ev["title"], ev["date"])
        if k in events:      # state (dismissed/replied/...) survives rescans
            cur = dict(events[k])
            cur.update({x: ev[x] for x in
                        ("status", "time", "end_time", "location")})
            # drafts retry on earlier failure, refresh when the thread
            # has moved on under a still-unsent draft
            if cur.get("state") in ("new", "calendared") \
                    and cur.get("needs_reply") \
                    and not cur.get("reply_sent") \
                    and (not cur.get("drafts")
                         or cur.get("draft_rowid") != latest):
                try:
                    cur["drafts"] = _draft(cur, msgs, unit["label"],
                                           unit["participants"])
                    cur["draft_rowid"] = latest
                except Exception:  # noqa: BLE001 — keep the old draft
                    pass
            events[k] = changes[k] = cur
            continue
        ev.update({"key": k, "state": "new", "detected": now_iso(),
                   "thread_key": unit["key"],
                   "thread_label": unit["label"],
                   "participants": unit["participants"][:12],
                   "send": unit["send"],
                   "chat_ids": unit["chat_ids"]})
        org_pid = next((m["pid"] for m in msgs
                        if m["pid"] and m["who"] == ev.get("organizer")),
                       None)
        ev["organizer_pid"] = org_pid
        if ev["needs_reply"] and ev["confidence"] >= 0.5:
            try:
                ev["drafts"] = _draft(ev, msgs, unit["label"],
                                      unit["participants"])
                ev["draft_rowid"] = latest
            except Exception:  # noqa: BLE001 — retried on the next rescan
                ev["drafts"] = None
        if settings.get("events_auto_calendar") and \
                settings.get("events_calendar") and \
                ev["confidence"] >= 0.5:   # same bar as the drafts —
            try:                           # low-confidence rows keep the
                create_hold(ev)            # "calendar" action instead
                ev["state"] = "calendared"
            except Exception:  # noqa: BLE001 — the row keeps the action
                pass
        events[k] = changes[k] = ev
        n += 1
    return n


# ---------- what the rest of the app reads --------------------------------

def _exhausted(e):
    """Every offered action taken: replied (or nothing to reply), FYI sent
    (or none drafted), hold on the calendar. The reply and the FYI are
    independent sends — answering the thread must not hide the row before
    the partner heard about it."""
    d = e.get("drafts") or {}
    return ((e.get("reply_sent") or not d.get("reply"))
            and (e.get("fyi_sent") or not d.get("partner_fyi"))
            and e.get("state") == "calendared")


def pending():
    """Open future events, soonest first — the review queue reader and
    every cross-module consumer share this."""
    today = datetime.now().date().isoformat()
    evs = (jsonstore.read(STORE, {}).get("events") or {}).values()
    out = [e for e in evs
           if e.get("state") in ("new", "calendared")
           and e["date"] >= today and not _exhausted(e)]
    return sorted(out, key=lambda e: (e["date"], e.get("detected") or ""))


def upcoming_for_person(pid):
    """Events this person organizes or attends — the dossier, radar, and
    reply drafts all read this."""
    name = (crm._load()["by_id"].get(pid) or {}).get("name")
    return [e for e in pending()
            if e.get("organizer_pid") == pid
            or (name and name in (e.get("participants") or []))]


def for_thread_keys(keys):
    return [e for e in pending() if e.get("thread_key") in keys]


def act(key, action):
    """The review queue's actor. Sends are one explicit tap; the draft was
    on the row before the tap (send.py's confirmation contract). Every
    branch is idempotent — a double tap or replayed POST answers
    {already: true} instead of sending twice."""
    from . import send
    if _passive():
        raise RuntimeError("passive instance — review actions are for the "
                           "live Vira")
    ev = (jsonstore.read(STORE, {}).get("events") or {}).get(key)
    if not ev:
        raise ValueError("unknown event")
    if action == "drop":
        _set_state(key, "dismissed")
        return {"ok": True, "action": action, "id": key}
    if ev.get("state") in ("dismissed", "past"):
        return {"ok": True, "already": True, "action": action, "id": key}
    if action == "calendar":
        if ev.get("state") == "calendared":
            return {"ok": True, "already": True, "action": action, "id": key}
        create_hold(ev)
        _set_state(key, "calendared")
        return {"ok": True, "action": action, "id": key}
    if action == "reply":
        if ev.get("reply_sent"):
            return {"ok": True, "already": True, "action": action, "id": key}
        text = (ev.get("drafts") or {}).get("reply")
        if not text:
            raise ValueError("no draft on this event yet — rescan first")
        if ev.get("send"):
            send.send_to_group(ev["send"]["guid"], text,
                               chat_ids=ev.get("chat_ids"))
        elif ev.get("organizer_pid"):
            send.send_message(text, person_id=ev["organizer_pid"])
        else:
            raise ValueError("no send address for this thread")
        _stamp(key, "reply_sent")
        return {"ok": True, "action": action, "id": key}
    if action == "tell partner":
        if ev.get("fyi_sent"):
            return {"ok": True, "already": True, "action": action, "id": key}
        text = (ev.get("drafts") or {}).get("partner_fyi")
        pid = fyi_person()
        if not text:
            raise ValueError("no FYI draft on this event yet")
        if not pid:
            raise ValueError('no partner configured — set '
                             '"events_fyi_person" in config.json '
                             '(auto-detection requires a family contact '
                             'recorded as wife/husband/spouse)')
        send.send_message(text, person_id=pid)
        _stamp(key, "fyi_sent")
        return {"ok": True, "action": action, "id": key}
    raise ValueError(f"unsupported action {action!r}")


def _stamp(key, field):
    jsonstore.mutate(STORE, lambda s: s["events"][key].update(
        {field: now_iso()}) or s, {})


def _set_state(key, st):
    jsonstore.mutate(STORE, lambda s: s["events"][key].update(
        {"state": st, "decided": now_iso()}) or s, {})


# ---------- background cadence -------------------------------------------

def start_background():
    """A scan every few hours, first one shortly after boot. Quiet threads
    cost nothing (stamp cache); passive instances never run."""
    if settings.fixture_mode() or _passive() or not settings.IS_MAC:
        return
    def loop():
        time.sleep(90)
        while True:
            try:
                scan()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(30, int(
                settings.get("events_scan_interval_min"))) * 60)
    threading.Thread(target=loop, daemon=True,
                     name="event-radar").start()

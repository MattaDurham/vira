"""Circle intelligence — the atlas's social clusters as named, evolving
subjects.

The Visual Network's label propagation finds the owner's social circles on
its own, and until this module it could say nothing about them: a cluster
was `c10`, its label was "circle 11", and both were POSITIONAL — the next
rebuild could hand the same forty people a different number. A circle is a
real thing in the owner's life (the NYC crew, the high-school friends, the
old office) and deserves what a person already gets in Vira: a stable
identity, a name, a story of how the owner is connected to it and how its
members are connected to each other, and a record of how it changes.

Three parts, in the find.py / evidence.py shape:

  IDENTITY   `match()` carries a circle's id across rebuilds by member
             overlap (Jaccard >= MATCH_MIN, greedy one-to-one). Members
             joining or leaving become history entries rather than a new
             circle. Deterministic, no model.
  EVIDENCE   `evidence()` reads the world: the group chats the members
             share (LIVE off chat.db in one pass — display names, sizes,
             message counts, first/last dates; the CRM's archive index is
             the fallback), each member's dossier (class, employer, city,
             how the owner met them, the relationship summary), the rare
             topics they share, the ties inside the circle and who its hub
             is. Deterministic, budgeted through modelbudget.
  THE READ   `read_circle()` is ONE `suggest.complete` per circle, strict
             JSON: a 2-4 word label, one sentence naming the evidence
             behind it, how the owner is connected, how they connect to
             each other, the hub, the year it dates to, and — on a
             re-read — what the new evidence shows. GROUNDED-OR-HELD: a
             label whose words do not appear in the evidence is HELD (kept
             on the record, never applied) and the deterministic fallback
             name stands; a hub that is not a member is dropped; a year the
             evidence never mentions is dropped. The resolver.py / journal
             discipline — a confident wrong name on a legend chip is worse
             than an honest generic one.

EVOLVING. `sync()` is the one entry point: it re-matches identities against
the current graph, refreshes each circle's evidence, and re-reads only the
circles that EARNED it — never read, members changed, a new shared group
chat appeared, `REREAD_MSGS` new messages landed in its chats, or the read
is older than `STALE_DAYS`. Each pass spends at most `READS_PER_PASS` model
calls, so a backlog names itself over a few passes and a busy chat cannot
turn into an hourly spend. The Watcher thread runs a pass every
`circle_refresh_min`; a graph rebuild kicks one; the atlas routes run one
on demand. Nothing waits on it: until a circle has been read it wears a
deterministic fallback label (its most-covering NAMED group chat, else
"<hub>'s circle"), so the legend never reads "circle 11" once a single pass
has run, even on a machine with no AI connected.

The store is `data/atlas-circles.json` (jsonstore discipline). Labels and
stories are derived and regenerable; the HISTORY and the owner's renames
are not, which is why the file rides the backup rotation. `apply()` overlays
the store onto a composed graph at read time — the contactcard.py pattern —
so the graph cache stays pristine-derived and the atlas-groups override
layer (promote / dissolve / assign) works on top of it unchanged.
"""
import hashlib
import json
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import data as crm
from . import jsonstore, settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "atlas-circles.json"

MATCH_MIN = 0.4        # Jaccard needed to carry an identity across rebuilds
READS_PER_PASS = 4     # model calls one pass may spend
REREAD_MSGS = 25       # new messages in a circle's chats that earn a re-read
STALE_DAYS = 14        # a read older than this is re-read regardless
MAX_CHATS = 12         # group chats named to the model per circle
MAX_TOPICS = 12
# A topic is a token RARE across the whole graph and shared inside the
# circle - atlas._topic_edges's own band (2 <= df <= 12); without the
# rarity test the list is "friend", "last", "became" - words every
# profile carries, which say nothing about this circle in particular.
TOPIC_DF_MAX = 12
MAX_GROUP_EVIDENCE = 40   # a chat past this many participants is a blast
HISTORY_KEEP = 60
LABEL_MAX = 40
# The per-member excerpt (how_we_met + summary) is sized by modelbudget
# against the members count; this is the ceiling one member may take so a
# 43-person circle and a 3-person one both fit and neither drowns the rest.
MEMBER_CHARS_MAX = 700

# Words a label may carry without the evidence having to contain them —
# generic descriptors, not claims. Everything else in a proposed label must
# appear whole-word in the evidence or the label is held.
LABEL_STOP = {
    "the", "and", "of", "for", "from", "with", "crew", "circle", "friends",
    "friend", "group", "gang", "guys", "people", "club", "team", "family",
    "old", "new", "close", "core", "era", "days", "set", "world", "scene",
    "network", "colleagues", "pals", "buddies", "mates", "crowd",
    "thread", "chat", "chats",
}

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty():
    return {"next": 1, "circles": {}, "map": {}}


def _read():
    s = jsonstore.read(STORE, None)
    if not isinstance(s, dict):
        s = _empty()
    for k, v in _empty().items():
        s.setdefault(k, v)
    return s


def _mutate(fn):
    return jsonstore.mutate(STORE, fn, _empty(), ensure_ascii=False, indent=1)


def _jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------- identity ----------

def derived_circles(graph):
    """[(cid, members)] for every cluster the build called a circle."""
    from .atlaslens import _cluster_kind
    members = defaultdict(list)
    for pid, cid in (graph.get("node_cluster") or {}).items():
        members[cid].append(pid)
    out = []
    for c in graph.get("clusters", []):
        if _cluster_kind(c) != "circle":
            continue
        ms = sorted(members.get(c["id"], []))
        if ms:
            out.append((c["id"], ms))
    return out


def _person_name(c, pid):
    p = (c.get("by_id") or {}).get(pid) or {}
    return p.get("name") or pid


def _first(name):
    return (name or "").split()[0] if name else ""


def match(store, graph, c, now=None):
    """Carry identities across a rebuild. Mutates `store`: assigns every
    derived circle a stable id (existing on overlap >= MATCH_MIN, greedy
    best-first and one-to-one, else freshly minted), records joins and
    leaves as history, revives a dissolved circle that comes back, and
    stamps `dissolved` on stored circles the build no longer finds.
    Returns {cid: sid}."""
    now = now or _now()
    derived = derived_circles(graph)
    circles = store["circles"]
    pairs = []
    for cid, members in derived:
        for sid, rec in circles.items():
            j = _jaccard(members, rec.get("members") or [])
            if j >= MATCH_MIN:
                # a live circle outranks a dissolved one at equal overlap
                pairs.append((j, 0 if not rec.get("dissolved") else 1,
                              cid, sid))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
    mapping, taken = {}, set()
    for _j, _d, cid, sid in pairs:
        if cid in mapping or sid in taken:
            continue
        mapping[cid] = sid
        taken.add(sid)
    for cid, members in derived:
        sid = mapping.get(cid)
        if sid is None:
            sid = f"k{store['next']}"
            store["next"] += 1
            circles[sid] = {
                "id": sid, "label": "", "owner_label": "", "why": "",
                "story": {}, "held": None, "members": list(members),
                "formed": now, "read_at": None, "read_members": [],
                "read_reason": "", "chat_msgs": {}, "chat_ids": [],
                "ev": {}, "history": [{"when": now, "kind": "formed",
                                      "what": f"formed with {len(members)} "
                                              "people"}],
                "dissolved": None,
            }
            mapping[cid] = sid
            continue
        rec = circles[sid]
        old = set(rec.get("members") or [])
        new = set(members)
        joined = sorted(new - old)
        left = sorted(old - new)
        hist = rec.setdefault("history", [])
        if joined:
            names = ", ".join(_first(_person_name(c, p)) for p in joined[:6])
            more = f" +{len(joined) - 6}" if len(joined) > 6 else ""
            hist.append({"when": now, "kind": "joined",
                         "what": f"{names}{more} joined", "pids": joined})
        if left:
            names = ", ".join(_first(_person_name(c, p)) for p in left[:6])
            more = f" +{len(left) - 6}" if len(left) > 6 else ""
            hist.append({"when": now, "kind": "left",
                         "what": f"{names}{more} left", "pids": left})
        if rec.get("dissolved"):
            hist.append({"when": now, "kind": "revived",
                         "what": "came back together"})
            rec["dissolved"] = None
        rec["members"] = list(members)
        del hist[:-HISTORY_KEEP]
    for sid, rec in circles.items():
        if sid not in taken and sid not in mapping.values() \
                and not rec.get("dissolved"):
            rec["dissolved"] = now
            rec.setdefault("history", []).append(
                {"when": now, "kind": "dissolved",
                 "what": "no longer found as one circle"})
    store["map"] = mapping
    return mapping


# ---------- evidence ----------

def _apple_date(ns):
    from .imessage import apple_dt
    d = apple_dt(ns)
    return d.date().isoformat() if d else None


def live_groups(c, pid_set):
    """Every group chat (style 43) in chat.db that holds >= 2 of `pid_set`,
    in ONE pass over the store: the chat rows, the handle joins, and one
    grouped scan of the message join. Legs of one logical group (SMS and
    iMessage rows with the same member set and name) merge, the
    groups_for_person rule. Raises on an unreadable chat.db so the caller
    can fall back to the archive index."""
    from . import imessage
    if settings.fixture_mode():
        raise OSError("fixture mode")
    con = imessage._connect()
    try:
        chats = {r[0]: (r[1] or "") for r in con.execute(
            "SELECT ROWID, display_name FROM chat WHERE style = 43")}
        handles = defaultdict(list)
        for chat_id, hid in con.execute(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj "
                "JOIN handle h ON h.ROWID = chj.handle_id"):
            if chat_id in chats:
                handles[chat_id].append(hid)
        stats = {r[0]: r[1:] for r in con.execute(
            "SELECT cmj.chat_id, COUNT(*), MIN(m.date), MAX(m.date), "
            "MAX(m.ROWID) FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "GROUP BY cmj.chat_id")}
    finally:
        con.close()
    resolved = {}

    def pid_of(h):
        if h not in resolved:
            resolved[h] = crm.resolve_handle(h)
        return resolved[h]

    merged = {}
    for chat_id, name in chats.items():
        hs = sorted(set(handles.get(chat_id, [])))
        if not hs or len(hs) > MAX_GROUP_EVIDENCE:
            continue
        pids = [pid_of(h) for h in hs]
        inside = sorted({p for p in pids if p and p in pid_set})
        if len(inside) < 2:
            continue
        n, first, last, top = stats.get(chat_id) or (0, None, None, 0)
        key = (tuple(hs), name)
        g = merged.get(key)
        if g:
            g["chat_ids"].append(chat_id)
            g["messages"] += n or 0
            g["first"] = min(x for x in (g["first"], _apple_date(first)) if x) \
                if (g["first"] or first) else None
            g["last"] = max(x for x in (g["last"], _apple_date(last)) if x) \
                if (g["last"] or last) else None
            g["max_rowid"] = max(g["max_rowid"], top or 0)
            continue
        # a synthetic label names the RESOLVED members only - an
        # unresolved handle is a phone number, and a phone number is not
        # a name for a group (it would ride into the prompt and the card)
        others = [_first(_person_name(c, p)) for p in pids if p]
        unknown = sum(1 for p in pids if not p)
        synth = "group: " + ", ".join(others[:3]) if others else "group chat"
        if unknown:
            synth += f" +{unknown}"
        merged[key] = {
            "chat_ids": [chat_id], "title": name or None,
            "named": bool(name),
            "label": name or synth,
            "members": inside, "total": len(hs),
            "messages": n or 0, "first": _apple_date(first),
            "last": _apple_date(last), "max_rowid": top or 0,
        }
    return list(merged.values())


def archive_groups(c, pid_set):
    """The CRM archive index's view of the same thing — what the atlas
    itself builds edges from. No live counts, so a re-read triggered by
    message volume cannot fire from this source; membership and new chats
    still can."""
    seen, out = set(), []
    for lst in (c.get("chats_by_person") or {}).values():
        for e in lst:
            if e.get("type") != "group":
                continue
            key = e.get("file") or id(e)
            if key in seen:
                continue
            seen.add(key)
            parts = e.get("participants") or []
            if len(parts) > MAX_GROUP_EVIDENCE:
                continue
            inside = sorted({p.get("person_id") for p in parts
                             if p.get("person_id") in pid_set})
            if len(inside) < 2:
                continue
            title = e.get("title") or ""
            named = bool(title) and not title.startswith("group: ")
            out.append({
                "chat_ids": [e.get("chat_id")] if e.get("chat_id") else [],
                "title": title if named else None, "named": named,
                "label": title or "group chat", "members": inside,
                "total": len(parts), "messages": e.get("messages") or 0,
                "first": e.get("date_first"), "last": e.get("date_last"),
                "max_rowid": 0,
            })
    return out


def groups_for(c, pid_set):
    """Live first, archive when chat.db cannot be read. Reports which."""
    try:
        return live_groups(c, pid_set), "chat.db"
    except Exception:  # noqa: BLE001 — no FDA, fixture, missing store
        return archive_groups(c, pid_set), "archive"


def _member_excerpt(prof, limit):
    hm = prof.get("how_we_met")
    if not isinstance(hm, str):
        hm = ""
    rs = prof.get("relationship_summary")
    if not isinstance(rs, str):
        rs = ""
    half = max(limit // 2, 80)
    hm = re.sub(r"\s+", " ", hm).strip()[:half]
    rs = re.sub(r"\s+", " ", rs).strip()[:max(limit - len(hm), 80)]
    return hm, rs


def graph_df(c, graph):
    """Token document-frequency across every node in the graph - the
    rarity baseline `evidence` scores shared topics against. Computed once
    per sync, not per circle."""
    from .atlas import _fingerprints
    df = Counter()
    for toks in _fingerprints(c, {n["id"] for n in graph.get("nodes", [])}
                              ).values():
        df.update(toks)
    return df


def evidence(c, graph, members, current_label="", df_all=None):
    """The deterministic dossier of one circle. `text` is the flattened
    corpus a proposed label is grounded against. `df_all` (graph_df) makes
    the topics list a RARE-token list; without it only the owner's own
    name is excluded."""
    from . import atlas, radar
    from .atlaslens import _ab_index
    try:
        ab = _ab_index()
    except Exception:  # noqa: BLE001 — AddressBook is optional
        ab = {}
    from . import modelbudget
    _total, per = modelbudget.split("standard", parts=max(len(members), 1))
    per = min(per, MEMBER_CHARS_MAX)
    mset = set(members)
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    people, fps = [], {}
    for pid in members:
        p = (c.get("by_id") or {}).get(pid) or {}
        prof = (c.get("profiles") or {}).get(pid) or {}
        master = (c.get("master") or {}).get(pid) or {}
        node = nodes.get(pid) or {}
        hm, rs = _member_excerpt(prof, per)
        people.append({
            "id": pid, "name": p.get("name") or pid,
            "class": prof.get("relationship_class") or "",
            "company": (master.get("company") or node.get("company") or
                        ab.get(pid, {}).get("org") or "")[:60],
            "title": (master.get("title") or node.get("title") or "")[:60],
            "city": ab.get(pid, {}).get("city") or "",
            "degree": node.get("degree"), "act": node.get("act") or 0,
            "how_we_met": hm, "summary": rs,
        })
        toks = radar.person_tokens(p, prof, master) if p else set()
        if toks:
            fps[pid] = toks
    df = Counter()
    for toks in fps.values():
        df.update(toks)
    own = _tokens(settings.get("owner_name") or "")
    shared = [(t, n) for t, n in df.items() if n >= 2 and t not in own]
    if df_all:
        shared = [(t, n) for t, n in shared
                  if 2 <= df_all.get(t, 0) <= TOPIC_DF_MAX]
        shared.sort(key=lambda tn: (-tn[1], df_all.get(tn[0], 0), tn[0]))
    else:
        shared.sort(key=lambda tn: (-tn[1], tn[0]))
    topics = [t for t, _n in shared][:MAX_TOPICS]

    chats, source = groups_for(c, mset)
    chats.sort(key=lambda g: (-len(g["members"]), -(g["messages"] or 0)))
    chats = chats[:MAX_CHATS]

    ties, weight = Counter(), Counter()
    photos = 0
    for e in graph.get("edges", []):
        if e["a"] in mset and e["b"] in mset:
            for s in e.get("signals", []):
                ties[s["type"]] += 1
                if s["type"] == "photo_cooccur":
                    photos += 1
            w = e.get("weight") or sum(
                atlas.COEF.get(s["type"], 0) for s in e.get("signals", []))
            weight[e["a"]] += w
            weight[e["b"]] += w
    hub = max(weight, key=weight.get) if weight else \
        (max(people, key=lambda x: x["act"])["id"] if people else None)
    years = sorted({(g.get("first") or "")[:4] for g in chats
                    if g.get("first")})
    since = years[0] if years else ""
    last = max((g.get("last") or "" for g in chats), default="")

    parts = [current_label]
    for x in people:
        parts += [x["name"], x["class"], x["company"], x["title"],
                  x["city"], x["how_we_met"], x["summary"]]
    parts += [g["label"] for g in chats] + topics
    text = " ".join(p for p in parts if p)
    return {
        "members": list(members), "people": people, "chats": chats,
        "chat_source": source, "topics": topics,
        "ties": dict(ties), "photos": photos, "hub": hub,
        "since": since, "last": last, "current_label": current_label,
        "text": text,
    }


# ---------- grounding ----------

_TOKEN = re.compile(r"[a-z0-9']+")


def _tokens(text):
    return {t.rstrip("'").removesuffix("'s") for t in
            _TOKEN.findall((text or "").lower())}


def grounded(label, text):
    """Every significant word of a label appears whole-word in the
    evidence. A label made only of generic descriptors ("old friends") is
    grounded by construction — it claims nothing specific."""
    words = [w.removesuffix("'s") for w in _TOKEN.findall(label.lower())]
    sig = [w for w in words if len(w) >= 3 and w not in LABEL_STOP]
    have = _tokens(text)
    return all(w in have for w in sig)


def fallback_label(ev):
    """The deterministic name a circle wears until (or instead of) a
    grounded read: the NAMED group chat covering the most members when it
    covers at least half of them, else "<hub>'s circle"."""
    n = max(len(ev.get("members") or []), 1)
    named = [g for g in ev.get("chats") or [] if g.get("named")]
    if named:
        best = named[0]
        if len(best["members"]) * 2 >= n:
            return best["title"][:LABEL_MAX]
    hub = ev.get("hub")
    if hub:
        name = next((p["name"] for p in ev.get("people") or []
                     if p["id"] == hub), "")
        if name:
            return f"{_first(name)}'s circle"
    return "circle"


# ---------- the read ----------

READ_PROMPT = """You are {owner}'s chief of staff, studying one of the \
social circles the Visual Network found among {owner}'s contacts. Below is \
everything Vira knows about it: each member's dossier, the group chats they \
share, the topics that recur across their profiles, and the ties inside \
the circle. {rereading}

Write, grounded ONLY in this evidence and never inventing a fact:

- "label": a 2-4 word name {owner} would recognise on sight. Use the \
words the evidence uses - a place, an era, a shared thing that binds THESE \
members, or a NAMED chat's own title. Never a bare number, never a \
member's first name, never a person's surname unless the circle is that \
family, never "{owner}'s friends".
- "why": ONE sentence naming the evidence the label rests on.
- "you": 2-3 sentences on how {owner} is connected to this circle — where \
it comes from, what it is in {owner}'s life now.
- "them": 2-3 sentences on how these people are connected to EACH OTHER — \
who anchors it, which sub-groups sit inside it, what binds them.
- "hub": the member id who most holds the circle together, or "".
- "since": the four-digit year the circle dates to in the evidence, or "".
- "whats_new": on a re-read, ONE sentence on what the new evidence shows \
(a new chat, someone joining, the circle going quiet or busy); else "".

Return ONLY a JSON object with exactly those keys.

CURRENT NAME: {current}
FALLBACK NAME (answer with it when the evidence supports nothing more \
specific - a plain true name beats an inventive one): {fallback}
NAMES OTHER CIRCLES ALREADY CARRY (pick something distinct): {taken}
{prior}
MEMBERS ({n_members}):
{members}

GROUP CHATS THEY SHARE ({chat_source}):
{chats}

SHARED TOPICS: {topics}
TIES INSIDE THE CIRCLE: {ties}
"""


def _prior_block(rec, changes):
    if not rec.get("read_at"):
        return ""
    st = rec.get("story") or {}
    lines = ["PREVIOUS READ (" + (rec.get("read_at") or "")[:10] + "):",
             f"  label: {rec.get('label')}",
             f"  why: {rec.get('why')}",
             f"  you: {st.get('you', '')}",
             f"  them: {st.get('them', '')}"]
    if changes:
        lines.append("WHAT CHANGED SINCE: " + "; ".join(changes))
    return "\n".join(lines) + "\n"


def _members_block(ev):
    out = []
    for x in ev["people"]:
        bits = [f"{x['name']} [{x['id']}]"]
        if x["class"]:
            bits.append(x["class"])
        if x["company"] or x["title"]:
            bits.append(" / ".join(b for b in (x["title"], x["company"])
                                   if b))
        if x["city"]:
            bits.append(x["city"])
        line = " - ".join(bits)
        if x["how_we_met"]:
            line += f"\n    how they met: {x['how_we_met']}"
        if x["summary"]:
            line += f"\n    summary: {x['summary']}"
        out.append("- " + line)
    return "\n".join(out) or "- (none)"


def _chats_block(ev):
    """A NAMED chat is quoted by its title - a name the owner chose, and
    fair game for a label. An unnamed chat is described by the circle
    members in it, never by a participant list dressed as a title: the
    first cut quoted 'group: Zach, Max, Nick' and got the label "Zach,
    Max, Nick thread" back, naming a non-member."""
    out = []
    n = len(ev["members"])
    names = {p["id"]: _first(p["name"]) for p in ev["people"]}
    for g in ev["chats"]:
        span = " - ".join(d for d in (g.get("first"), g.get("last")) if d)
        if g.get("named"):
            head = f"{g['label']!r} (a named chat)"
        else:
            who = ", ".join(names.get(p, "") for p in g["members"]
                            if names.get(p))
            head = f"an unnamed chat with {who}"
        out.append(f"- {head}: {len(g['members'])} of {n} members "
                   f"(of {g['total']} in the chat), {g['messages']} messages"
                   + (f", {span}" if span else ""))
    return "\n".join(out) or "- (none found)"


def compose_prompt(ev, rec=None, changes=None, taken=()):
    owner = settings.get("owner_name") or "the owner"
    rec = rec or {}
    ties = ", ".join(f"{k} x{v}" for k, v in sorted(ev["ties"].items())) \
        or "none recorded"
    if ev.get("photos"):
        ties += f"; {ev['photos']} pairs photographed together"
    rereading = ("You have read this circle before; the previous read and "
                 "what changed are below. Keep what still holds, revise "
                 "what the new evidence contradicts.") \
        if rec.get("read_at") else ""
    return READ_PROMPT.format(
        owner=owner, rereading=rereading,
        current=ev.get("current_label") or "(unnamed)",
        fallback=fallback_label(ev),
        taken=", ".join(t for t in taken if t) or "none yet",
        prior=_prior_block(rec, changes or []),
        n_members=len(ev["members"]), members=_members_block(ev),
        chat_source=ev.get("chat_source") or "archive",
        chats=_chats_block(ev), topics=", ".join(ev["topics"]) or "none",
        ties=ties)


def _s(v, cap):
    return re.sub(r"\s+", " ", v).strip()[:cap] if isinstance(v, str) else ""


def clean_read(parsed, ev):
    """Validate a model's read against the evidence. Returns the fields
    to record; `held` carries an ungrounded label and the reason."""
    if not isinstance(parsed, dict):
        raise ValueError("read is not an object")
    label = _s(parsed.get("label"), LABEL_MAX)
    held = None
    if not label:
        held = {"label": "", "reason": "no label"}
    elif re.fullmatch(r"(circle|group)\s*\d*", label.lower()):
        held = {"label": label, "reason": "a number is not a name"}
    elif not grounded(label, ev["text"]):
        held = {"label": label,
                "reason": "uses words the evidence does not"}
    why = _s(parsed.get("why"), 300)
    you = _s(parsed.get("you"), 900)
    them = _s(parsed.get("them"), 900)
    if not (why and you and them):
        raise ValueError("read is missing why/you/them")
    hub = _s(parsed.get("hub"), 40)
    if hub not in set(ev["members"]):
        hub = ev.get("hub") or ""
    since = _s(parsed.get("since"), 4)
    if not (re.fullmatch(r"(19|20)\d\d", since)
            and since in _tokens(ev["text"]) | {ev.get("since") or ""}):
        since = ev.get("since") or ""
    return {"label": "" if held else label, "held": held, "why": why,
            "story": {"you": you, "them": them, "hub": hub,
                      "since": since},
            "whats_new": _s(parsed.get("whats_new"), 300)}


def read_circle(ev, rec=None, changes=None, taken=()):
    """ONE model call. Raises on a backend or contract failure; the caller
    keeps the fallback label and records the error."""
    from . import suggest
    prompt = compose_prompt(ev, rec, changes, taken=taken)
    parsed = suggest._extract_json(suggest.complete(prompt))
    return clean_read(parsed, ev)


# ---------- sync ----------

def _ev_summary(ev):
    """What the store keeps of the evidence so the card serves without a
    chat.db pass: the chats, topics, ties, hub, dates."""
    return {
        "chats": [{"label": g["label"], "named": g["named"],
                   "covers": len(g["members"]), "total": g["total"],
                   "messages": g["messages"], "first": g.get("first"),
                   "last": g.get("last"), "chat_ids": g["chat_ids"]}
                  for g in ev["chats"]],
        "chat_source": ev["chat_source"], "topics": ev["topics"],
        "ties": ev["ties"], "photos": ev["photos"], "hub": ev["hub"],
        "since": ev["since"], "last": ev["last"], "at": _now(),
    }


def _chat_key(g):
    return "|".join(str(x) for x in sorted(g["chat_ids"])) or g["label"]


def _changes(rec, ev, c):
    """Why a circle earns a re-read — each reason in plain words, so the
    record (and the prompt) can say it. Empty = nothing earned it."""
    out = []
    if not rec.get("read_at"):
        return ["never read"]
    old_m, new_m = set(rec.get("read_members") or []), set(ev["members"])
    joined, left = sorted(new_m - old_m), sorted(old_m - new_m)
    if joined:
        out.append("joined: " + ", ".join(
            _first(_person_name(c, p)) for p in joined[:8]))
    if left:
        out.append("left: " + ", ".join(
            _first(_person_name(c, p)) for p in left[:8]))
    old_msgs = rec.get("chat_msgs") or {}
    new_total, new_chats = 0, []
    for g in ev["chats"]:
        k = _chat_key(g)
        if k not in old_msgs:
            if g["messages"]:
                new_chats.append(g["label"])
            continue
        new_total += max(0, (g["messages"] or 0) - (old_msgs[k] or 0))
    if new_chats:
        out.append("new shared chats: " + ", ".join(new_chats[:4]))
    if new_total >= REREAD_MSGS:
        out.append(f"{new_total} new messages in shared chats")
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(rec["read_at"])).days
    except (ValueError, TypeError):
        age = STALE_DAYS + 1
    if age >= STALE_DAYS:
        out.append(f"last read {age} days ago")
    return out


def _taken_labels(store, sid):
    """Names every OTHER live circle wears, casefolded - the legend and
    the atlas-groups dissolve list both key on the label, so two circles
    may never share one."""
    out = {}
    for other, rec in store["circles"].items():
        if other == sid or rec.get("dissolved"):
            continue
        for lab in (rec.get("owner_label"), rec.get("label")):
            if lab:
                out[lab.casefold()] = lab
    return out


def _apply_read(sid, read, ev, changes, reason):
    now = _now()

    def fn(s):
        nonlocal read
        rec = s["circles"].get(sid)
        if not rec:
            return
        hist = rec.setdefault("history", [])
        old_label = rec.get("label")
        if read["label"] and read["label"].casefold() in _taken_labels(s, sid):
            read = dict(read, label="", held={
                "label": read["label"],
                "reason": "another circle already carries this name"})
        rec["why"] = read["why"]
        rec["story"] = read["story"]
        rec["held"] = read["held"]
        if read["label"]:
            rec["label"] = read["label"]
        elif not rec.get("label") or \
                rec["label"].casefold() in _taken_labels(s, sid):
            # nothing grounded to apply, and the name it wears is either
            # missing or one another circle already carries - the
            # deterministic name is the honest fallback
            rec["label"] = fallback_label(ev)
        if old_label and rec["label"] != old_label and not read["held"]:
            hist.append({"when": now, "kind": "renamed",
                         "what": f"read as {rec['label']!r} "
                                 f"(was {old_label!r})"})
        if read.get("whats_new"):
            hist.append({"when": now, "kind": "read",
                         "what": read["whats_new"]})
        for ch in changes:
            if ch.startswith("new shared chats"):
                hist.append({"when": now, "kind": "chat", "what": ch})
        rec["read_at"] = now
        rec["read_members"] = list(ev["members"])
        rec["read_reason"] = reason
        rec["read_error"] = ""
        rec["chat_msgs"] = {_chat_key(g): g["messages"] for g in ev["chats"]}
        rec["ev"] = _ev_summary(ev)
        del hist[:-HISTORY_KEEP]
    _mutate(fn)


def _apply_evidence(sid, ev, error=""):
    def fn(s):
        rec = s["circles"].get(sid)
        if not rec:
            return
        if not rec.get("label"):
            rec["label"] = fallback_label(ev)
        if not rec.get("read_at"):
            # first sight: baseline the chat counts, or the first read
            # would report every message ever as new
            rec.setdefault("chat_msgs", {})
            for g in ev["chats"]:
                rec["chat_msgs"].setdefault(_chat_key(g), g["messages"])
        rec["ev"] = _ev_summary(ev)
        if error:
            rec["read_error"] = error[:200]
    _mutate(fn)


def _overridden_graph(graph=None):
    """The graph the Circles lens actually shows: the build plus the
    owner's group edits (a dissolved circle is gone, a promoted one is a
    custom group)."""
    from . import atlas
    if graph is None:
        with atlas._lock:
            graph = atlas._read()
    if not graph:
        return None
    graph = json.loads(json.dumps(graph))
    atlas.apply_overrides(graph)
    return graph


def sync(graph=None, force=False, limit=READS_PER_PASS, sids=None):
    """Match identities, refresh evidence, read what earned it. Serialized;
    a second caller waits. Returns a report, never raises."""
    report = {"circles": 0, "read": [], "skipped": [], "errors": []}
    with _lock:
        try:
            g = _overridden_graph(graph)
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"graph: {e}")
            return report
        if not g:
            report["errors"].append("atlas not built yet")
            return report
        c = crm._load()
        try:
            df_all = graph_df(c, g)
        except Exception:  # noqa: BLE001 - topics degrade, the read does not
            df_all = None
        with locked(STORE):
            store = _read()
            mapping = match(store, g, c)
            jsonstore.write_atomic(STORE, store, ensure_ascii=False, indent=1)
        report["circles"] = len(mapping)
        by_sid = {sid: cid for cid, sid in mapping.items()}
        order = []
        for sid in by_sid:
            rec = store["circles"][sid]
            if sids and sid not in sids:
                continue
            order.append((0 if not rec.get("read_at") else 1,
                          rec.get("read_at") or "", sid))
        order.sort()
        spent = 0
        for _u, _r, sid in order:
            rec = store["circles"][sid]
            try:
                ev = evidence(c, g, rec["members"],
                              current_label=rec.get("owner_label")
                              or rec.get("label") or "", df_all=df_all)
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"{sid}: evidence: {e}")
                continue
            changes = ["forced"] if force else _changes(rec, ev, c)
            if not changes or spent >= limit:
                _apply_evidence(sid, ev)
                report["skipped"].append(
                    {"id": sid, "why": "budget" if changes else "current"})
                continue
            spent += 1
            try:
                read = read_circle(ev, rec, changes,
                                   taken=list(_taken_labels(
                                       _read(), sid).values()))
            except Exception as e:  # noqa: BLE001
                _apply_evidence(sid, ev, error=str(e))
                report["errors"].append(f"{sid}: read: {str(e)[:120]}")
                continue
            _apply_read(sid, read, ev, changes, "; ".join(changes))
            report["read"].append({"id": sid, "label": read["label"]
                                   or rec.get("label"),
                                   "held": bool(read["held"])})
    return report


def sync_async(graph=None, **kw):
    threading.Thread(target=sync, args=(graph,), kwargs=kw, daemon=True,
                     name="vira-circles-sync").start()


# ---------- serve ----------

def apply(graph):
    """Overlay the store onto a composed graph's clusters: a circle whose
    members still match wears its name (the owner's rename outranks the
    read), carries its stable id, and says whether it has a story. The
    raw build label is kept as `raw_label` so the atlas-groups dissolve
    list keyed on either spelling still matches."""
    store = _read()
    if not store["circles"]:
        return graph
    members = defaultdict(set)
    for pid, cid in (graph.get("node_cluster") or {}).items():
        members[cid].add(pid)
    from .atlaslens import _cluster_kind
    for cl in graph.get("clusters", []):
        if _cluster_kind(cl) != "circle":
            continue
        sid = store["map"].get(cl["id"])
        rec = store["circles"].get(sid) if sid else None
        if not rec or rec.get("dissolved"):
            continue
        if _jaccard(members.get(cl["id"], ()), rec.get("members") or []) \
                < MATCH_MIN:
            continue
        label = rec.get("owner_label") or rec.get("label")
        cl["raw_label"] = cl.get("label")
        if label:
            cl["label"] = label
        cl["circle"] = sid
        cl["story"] = bool(rec.get("read_at"))
        cl["kind"] = "circle"
    return graph


def circle(sid, graph=None):
    """The card payload: the record plus member names off the graph."""
    store = _read()
    rec = store["circles"].get(sid)
    if not rec:
        return None
    from . import atlas
    if graph is None:
        graph = atlas.compose()
    names = {n["id"]: n for n in (graph.get("nodes") or [])}
    c = crm._load()
    members = []
    for pid in rec.get("members") or []:
        n = names.get(pid) or {}
        members.append({"id": pid,
                        "name": n.get("name") or _person_name(c, pid),
                        "face": n.get("face"), "company": n.get("company"),
                        "class": n.get("relationship_class")})
    out = {k: v for k, v in rec.items()
           if k not in ("chat_msgs", "read_members")}
    out["members"] = members
    out["display_label"] = rec.get("owner_label") or rec.get("label") \
        or "circle"
    out["cid"] = next((cid for cid, s in store["map"].items() if s == sid),
                      None)
    return out


def brief(sid):
    """One line for the person page: the name and why."""
    rec = _read()["circles"].get(sid)
    if not rec:
        return None
    return {"id": sid,
            "label": rec.get("owner_label") or rec.get("label") or "",
            "why": rec.get("why") or "",
            "you": (rec.get("story") or {}).get("you") or ""}


def rename(sid, label):
    """The owner's name outranks every read; empty clears back to it."""
    label = _s(label, LABEL_MAX)
    now = _now()

    def fn(s):
        rec = s["circles"].get(sid)
        if not rec:
            raise ValueError("unknown circle")
        rec["owner_label"] = label
        rec.setdefault("history", []).append(
            {"when": now, "kind": "renamed",
             "what": f"you named it {label!r}" if label
                     else "your name cleared"})
        del rec["history"][:-HISTORY_KEEP]
    _mutate(fn)
    return circle(sid)


def list_all():
    store = _read()
    out = []
    for sid, rec in store["circles"].items():
        out.append({"id": sid,
                    "label": rec.get("owner_label") or rec.get("label"),
                    "why": rec.get("why"), "size": len(rec.get("members")
                                                       or []),
                    "read_at": rec.get("read_at"),
                    "dissolved": rec.get("dissolved"),
                    "held": rec.get("held")})
    out.sort(key=lambda r: (bool(r["dissolved"]), -r["size"]))
    return out


def status():
    store = _read()
    live = [r for r in store["circles"].values() if not r.get("dissolved")]
    return {"circles": len(live),
            "read": sum(1 for r in live if r.get("read_at")),
            "held": sum(1 for r in live if r.get("held")),
            "errors": sum(1 for r in live if r.get("read_error")),
            "dissolved": len(store["circles"]) - len(live),
            "store": str(STORE)}


def text_for_tools():
    """What a session or chat gets when it asks about the owner's circles:
    every live circle's name, story and members, newest history last."""
    store = _read()
    if not store["circles"]:
        return "No circles have been read yet."
    c = crm._load()
    blocks = []
    for r in sorted(store["circles"].values(),
                    key=lambda r: -len(r.get("members") or [])):
        if r.get("dissolved"):
            continue
        st = r.get("story") or {}
        names = ", ".join(_person_name(c, p) for p in
                          (r.get("members") or [])[:40])
        lines = [f"## {r.get('owner_label') or r.get('label')} "
                 f"({len(r.get('members') or [])} people, id {r['id']})"]
        if r.get("why"):
            lines.append(f"why: {r['why']}")
        if st.get("you"):
            lines.append(f"you: {st['you']}")
        if st.get("them"):
            lines.append(f"them: {st['them']}")
        if st.get("since"):
            lines.append(f"since: {st['since']}")
        lines.append(f"members: {names}")
        hist = [h for h in (r.get("history") or [])
                if h.get("kind") in ("joined", "left", "chat", "read")][-4:]
        for h in hist:
            lines.append(f"  {h['when'][:10]}: {h['what']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------- the worker ----------

class Watcher(threading.Thread):
    """A pass every `circle_refresh_min` (default 60). The first waits
    two minutes so a boot never competes with the atlas's own first build.
    Started outside VIRA_PASSIVE only, like every worker; a test clone
    syncs on demand through the route."""

    def __init__(self, interval_min=None, first_delay_s=120):
        super().__init__(daemon=True, name="vira-circles")
        self.interval = max(300.0, float(interval_min or 60) * 60.0)
        self.first_delay = first_delay_s
        self.last = None
        self.last_run = None
        self.runs = 0

    def run(self):
        time.sleep(self.first_delay)
        while True:
            try:
                self.last = sync()
            except Exception as e:  # noqa: BLE001 — never kill the thread
                self.last = {"error": str(e)[:200]}
            self.last_run = _now()
            self.runs += 1
            time.sleep(self.interval)

    def state(self):
        return {"runs": self.runs, "last_run": self.last_run,
                "interval_s": self.interval, "last": self.last}


# ---------- CLI ----------

if __name__ == "__main__":          # pragma: no cover
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "sync":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else READS_PER_PASS
        print(json.dumps(sync(limit=n, force="--force" in sys.argv),
                         indent=1, ensure_ascii=False))
    elif cmd == "list":
        print(json.dumps(list_all(), indent=1, ensure_ascii=False))
    elif cmd == "show":
        print(json.dumps(circle(sys.argv[2]), indent=1, ensure_ascii=False))
    elif cmd == "text":
        print(text_for_tools())
    else:
        print(json.dumps(status(), indent=1))

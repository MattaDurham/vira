"""The build story behind a module — right-click a window, "What is this?".

Answers with the module registry's own description, every document the
library holds about that surface (films, plans, dossiers, briefs, retros),
AND the module's own slice of the change log, all on one timeline. The
owner's framing, from the day the built-history library shipped: this stuff
is the story of how everything got built — and the module itself should be
its front door. The 2026-09-02 rebuild turned the panel from a list of
documents into a chronology: chapters by month, days as nodes, the retro
that narrates each day, the cards it produced, and every shipped line.

Everything is DERIVED at read time — the blurb from modulemap, the documents
from readinglist + doctags, the film thumbs from walkthroughs, the changes
from changelog — so the story cannot drift from the stores it reads (the
onboard.steps discipline). The one curated thing here is WINDOWS: which
doc-tag spellings, which registry entry and which key phrases each window
id means.

HOW A CHANGE JOINS A MODULE, strongest first:
  1. A retro-source entry inherits the module tags of the retro document
     it came from (`entry.retro` stem == the library row's title). Ideas
     inherit their own ideatags module axis; a job inherits its idea's tags
     or its session retro's. These are STRONG hits — a tag is a judgment
     the tagger already made about the whole document.
  2. Otherwise the entry's wording is matched against the window's KEY
     PHRASES: its multi-word tags ("job-runner", "reading-room"), the
     curated `keywords` on its row, and the registry's own multi-word
     keywords. A SINGLE WORD IS NEVER A KEY PHRASE — "find", "work",
     "atlas", "people" appear in sentences about everything — so single
     tags join only through rung 1. These are WEAK hits and the payload
     says so (`strong: false`), so the surface can tone them rather than
     pass them off as the tagger's verdict.
Nothing here guesses beyond that: an entry that joins nowhere rides the
payload as `hit: false`, which is what lets the surface offer the WHOLE
change log with this module's part of it lit — the owner's ask that
nothing in the changelog be missing.

Read-only; no store of its own; nothing here needs a passive guard.
"""
from __future__ import annotations

import re
from datetime import date as _date


# Window id -> the story's ingredients.
#   map       the modulemap registry id whose `what` describes this surface
#             to a stranger (the blurb at the top of the panel)
#   tags      module-axis doc tags that count as this window's story. The
#             window id itself always counts too, so a vocabulary that
#             converges on the id needs no table edit.
#   keywords  curated key PHRASES for the wording rung (multi-word only;
#             see the module docstring for why a single word never joins)
#   alias     companions resolve to their host window's story.
WINDOWS = {
    "feed":         {"map": "feed-win",
                     "tags": ["sources", "mail", "send", "whatsapp"],
                     "keywords": ["incoming feed", "feed card", "the feed",
                                  "swipe to hide", "mark all read"]},
    "people":       {"map": "people-win",
                     "tags": ["people", "radar", "contact-card", "groups"],
                     "keywords": ["person page", "people window",
                                  "contact card", "group profile",
                                  "open loops", "networking tab"]},
    "work":         {"map": "work-win",
                     "tags": ["queue", "job-runner", "session-cockpit",
                              "routines", "circuits", "flows", "forge",
                              "orphan-work"],
                     "keywords": ["the forge", "work window", "the queue",
                                  "flow run", "unlanded work",
                                  "live session", "agent session"]},
    "attention":    {"map": "attention-win",
                     "tags": ["attention", "brief", "review",
                              "morning-picker", "decision-layer"],
                     "keywords": ["attention window", "daily brief",
                                  "needs review", "decision card",
                                  "morning picker"]},
    "brief":        {"alias": "attention"},
    "review":       {"alias": "attention"},
    "journal":      {"map": "journal-win",
                     "tags": ["journal", "decisions"],
                     "keywords": ["tell vira", "the journal",
                                  "journal note", "journal window",
                                  "unapplied instruction"]},
    "triage":       {"alias": "people"},
    "applications": {"map": "applications-win",
                     "tags": ["applications", "job-search", "job-boards"],
                     "keywords": ["applications window", "job board",
                                  "cover letter", "application package",
                                  "the universe", "rescore"]},
    "find":         {"map": "find-win",
                     "tags": ["find", "search-and-recall", "brain",
                              "define"],
                     "keywords": ["find window", "concept cloud",
                                  "vault chat", "chat with vira",
                                  "the definition", "define card"]},
    "find-cloud":   {"alias": "find"},
    "find-related": {"alias": "find"},
    "find-define":  {"alias": "find"},
    "evidence":     {"map": "evidence-win",
                     "tags": ["evidence-ledger", "evidence"],
                     "keywords": ["evidence ledger", "case study",
                                  "case studies"]},
    "atlas":        {"map": "atlas-win", "tags": ["atlas", "network"],
                     "keywords": ["visual network", "contact atlas",
                                  "face graph", "the web", "3d graph",
                                  "orbit anchor"]},
    "imageatlas":   {"map": "image-atlas",
                     "tags": ["image-atlas", "gallery", "galaxy"],
                     "keywords": ["image atlas", "the galaxy", "chaska",
                                  "vault ops"]},
    "map":          {"map": "map-win", "tags": ["module-map", "system-map"],
                     "keywords": ["system map", "module map",
                                  "module registry", "modules page"]},
    "subs":         {"map": "subs-win", "tags": ["subscriptions"],
                     "keywords": ["subscriptions window", "renewal radar",
                                  "the ledger", "mercury poll",
                                  "receipts pass"]},
    "subsviz":      {"alias": "attention"},
    "design":       {"map": "design-studio",
                     "tags": ["design-studio", "skins", "genre-studio"],
                     "keywords": ["design studio", "genre studio",
                                  "the skin", "phosphor console",
                                  "component gallery"]},
    "reader":       {"map": "reader-win",
                     "tags": ["reader", "reading-room", "reading-list"],
                     "keywords": ["the reader", "reading room",
                                  "reading rooms", "reading list",
                                  "the library", "the inflow"]},
    "research":     {"map": "research-win",
                     "tags": ["research", "claims"],
                     "keywords": ["research window", "claim graph",
                                  "claim pages", "company research"]},
    "setup":        {"map": "setup-win",
                     "tags": ["config", "models", "onboarding", "sources",
                              "environment-doctor"],
                     "keywords": ["config window", "the wizard",
                                  "first run", "first-run",
                                  "full disk access", "setup window",
                                  "model roster"]},
}

# How a story orders its shelves; the client renders one section per kind.
KIND_ORDER = ("walkthrough", "dossier", "plan", "brief", "retro")

# Importance, 0..1 — what the surface reads to decide what a HIGHLIGHT is.
# A film or a dossier IS the story; a brief mentions it in passing.
DOC_WEIGHT = {"walkthrough": 1.0, "dossier": 0.95, "plan": 0.8,
              "brief": 0.3, "retro": 0.45}
ENTRY_WEIGHT = {("ship", True): 0.6, ("ship", False): 0.4,
                ("done", True): 0.55, ("done", False): 0.5,
                ("dropped", True): 0.25, ("dropped", False): 0.2,
                ("job", True): 0.3, ("job", False): 0.15}

_STEM_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} (\d{2})(\d{2})\b")   # the stem dates a retro


_VIRA_RETRO = re.compile(r"\bvira\b", re.I)


def is_vira_retro(title):
    """A retro stem in the change log's own scope (`... vira` /
    `... vira - slug`)."""
    return bool(_VIRA_RETRO.search(title or ""))


def resolve(win_id):
    """The canonical window id a story is asked about, or None."""
    row = WINDOWS.get(win_id)
    if row and row.get("alias"):
        win_id = row["alias"]
        row = WINDOWS.get(win_id)
    return (win_id, row) if row else (win_id, None)


def _registry_entry(map_id):
    """The modulemap row behind a window, or {} — a missing registry (fresh
    install, fixture clone) is a thinner story, never an error."""
    if not map_id:
        return {}
    try:
        from . import modulemap
        for m in modulemap.list_modules():
            if m.get("id") == map_id:
                return m
    except Exception:
        pass
    return {}


def key_phrases(row, reg):
    """The wording rung's vocabulary for one window: multi-word tags with
    their hyphens read as spaces, the row's curated keywords, and the
    registry's multi-word keywords. Single words are excluded on purpose
    (module docstring). Lower-cased, de-duplicated, order kept."""
    out = []
    seen = set()

    def add(p):
        p = re.sub(r"[\s_-]+", " ", (p or "").strip().lower())
        if " " in p and p not in seen:
            seen.add(p)
            out.append(p)

    for t in row.get("tags") or []:
        add(t)
    for k in row.get("keywords") or []:
        add(k)
    for k in (reg or {}).get("keywords") or []:
        add(k)
    return out


def _phrase_re(phrases):
    if not phrases:
        return None
    alts = [r"\b" + r"[\s\-]+".join(map(re.escape, p.split(" "))) + r"\b"
            for p in phrases]
    return re.compile("|".join(alts), re.I)


def _phrase_hits(rx, text):
    if not rx or not text:
        return []
    return sorted({re.sub(r"[\s\-]+", " ", m.group(0).lower())
                   for m in rx.finditer(text)})


def _doc_ts(d):
    """A library row's timestamp. Retros are dated to the day by the
    backfill; their stem carries the clock (`YYYY-MM-DD HHMM ...`), which
    is what orders two sessions on one day."""
    created = d.get("created") or ""
    if d.get("kind") == "retro":
        m = _STEM_TIME.match(d.get("title") or "")
        if m:
            return f"{m.group(0)[:10]}T{m.group(1)}:{m.group(2)}:00"
    return created


def _slim(d):
    """What the timeline needs of a library row — enough to open it, draw
    its face and say which tags joined it, nothing the panel never reads."""
    keep = ("id", "title", "kind", "locator", "locator_kind", "ref",
            "created", "read", "missing", "thumb", "film")
    out = {k: d.get(k) for k in keep if d.get(k) is not None}
    out["module_tags"] = list((d.get("tags") or {}).get("module") or [])
    return out


# How many retro files a story may open to read a goal the change log did
# not supply (pre-Vira sessions the library tags to a module). Each is one
# small file read; the cap keeps a 300-retro module's story under a second.
GOAL_READS = 80


def _retro_goal(doc):
    """The `## Goal` (or title line) of a retro the library holds, or ""
    when it cannot be read - never a guess."""
    try:
        from . import changelog, readinglist
        p = readinglist.source_path(doc)
        if not p or not p.is_file():
            return ""
        return changelog._parse_retro(p).get("goal") or ""
    except Exception:
        return ""


def _month_label(key):
    y, m = key.split("-")
    return _date(int(y), int(m), 1).strftime("%B %Y")


def _inputs():
    """The shared reads a story (or the coverage audit) is composed from,
    done ONCE: the annotated library, the change log, the idea tags."""
    from . import doctags, readinglist, walkthroughs
    docs = doctags.annotate(readinglist.library())
    films = {}
    try:
        films = {f["url"]: f for f in
                 walkthroughs.films(readinglist.WALKTHROUGH_DIR)}
    except Exception:
        pass
    groups = []
    try:
        from . import changelog
        groups = changelog.groups()
    except Exception:
        groups = []
    idea_tags = {}
    try:
        from . import ideas, ideatags
        for it in ideatags.annotate(ideas.list_items()):
            mods = (it.get("tags") or {}).get("module") or []
            if mods:
                idea_tags[it["id"]] = set(mods)
    except Exception:
        idea_tags = {}
    # The library holds every project's session retros (TC-IL, the CRM);
    # the module tags are Vira's vocabulary, so a TC-IL ingest session
    # tagged `sources` would otherwise narrate Incoming's story. Retros
    # join only when they are Vira's own - the change log's `* vira.md`
    # scope, applied to the library. Plans, films and dossiers carry no
    # project marker and join on their tags alone.
    docs = [d for d in docs
            if d.get("kind") != "retro" or is_vira_retro(d.get("title"))]
    retro_tags = {}
    retro_docs = {}
    for d in docs:
        if d.get("kind") == "retro":
            retro_docs[d.get("title") or ""] = d
            mods = (d.get("tags") or {}).get("module") or []
            if mods:
                retro_tags[d.get("title") or ""] = set(mods)
    return {"docs": docs, "films": films, "groups": groups,
            "idea_tags": idea_tags, "retro_tags": retro_tags,
            "retro_docs": retro_docs}


def story(win_id, inputs=None):
    """The build story for one window, or None for chrome / unknown ids.

    {id, title, what, tags, keywords, docs, counts, pending,
     events, days, eras, stats}

    `docs` is every library entry (read and unread — the build story is
    mostly read documents) whose module-axis tags intersect the window's
    tag set, film metadata joined the same way /api/reading/list joins it.
    `events` is the timeline: every non-retro document hit plus EVERY
    change-log entry, each flagged `hit` (this module's) and `strong` (a
    tag join rather than wording), oldest first. `days` narrates each day:
    the retros written that day, which of them are this module's, and the
    library document behind each so the surface can open it. `eras` buckets
    the timeline by month; `stats` is what the hero states. `pending` is
    how many library documents have no tags yet, so a thin story reads as
    still-being-tagged rather than as all there ever was."""
    win_id, row = resolve(win_id)
    if not row:
        return None
    inp = inputs or _inputs()
    docs, films, groups = inp["docs"], inp["films"], inp["groups"]
    idea_tags, retro_tags = inp["idea_tags"], inp["retro_tags"]
    retro_docs = inp["retro_docs"]

    tags = set(row.get("tags") or []) | {win_id}
    hits = [d for d in docs
            if tags & set((d.get("tags") or {}).get("module") or [])]
    for d in hits:
        f = films.get(d.get("locator"))
        if f:
            d["film"] = {"thumb": f.get("thumb"), "motion": f.get("motion"),
                         "project": f.get("project"),
                         "subject": f.get("subject"),
                         "description": f.get("description")}
    try:
        from . import docthumbs
        docthumbs.annotate(hits)
    except Exception:
        pass

    reg = _registry_entry(row.get("map"))
    phrases = key_phrases(row, reg)
    rx = _phrase_re(phrases)

    counts = {}
    for d in hits:
        counts[d.get("kind")] = counts.get(d.get("kind"), 0) + 1

    # ---- the timeline ----
    events = []
    hit_retro_stems = set()
    for d in hits:
        if d.get("kind") == "retro":
            hit_retro_stems.add(d.get("title") or "")
            continue                    # a retro narrates its day (below)
        why = sorted(tags & set((d.get("tags") or {}).get("module") or []))
        ts = _doc_ts(d)
        events.append({"kind": d.get("kind"), "ts": ts, "day": ts[:10],
                       "title": d.get("title") or "", "doc": _slim(d),
                       "weight": DOC_WEIGHT.get(d.get("kind"), 0.3),
                       "hit": True, "strong": True,
                       "why": ["tag:" + t for t in why]})

    days = {}
    ships_by_stem = {}
    for g in groups:
        day = g["date"]
        for e in g.get("entries") or []:
            why = []
            stem = e.get("retro") or ""
            if stem and stem in retro_tags:
                why += ["tag:" + t for t in sorted(tags & retro_tags[stem])]
            iid = e.get("idea_id")
            if iid and iid in idea_tags:
                why += ["tag:" + t for t in sorted(tags & idea_tags[iid])]
            why = sorted(set(why))
            strong = bool(why)
            if not why:
                why = ["word:" + p for p in _phrase_hits(rx, e.get("text"))]
            hit = bool(why)
            ev = {"kind": e.get("kind"), "ts": e.get("ts") or "", "day": day,
                  "text": e.get("text") or "", "source": e.get("source"),
                  "hit": hit, "strong": strong, "why": why,
                  "weight": ENTRY_WEIGHT.get((e.get("kind"), strong), 0.3)}
            for k in ("job_id", "idea_id", "session_id"):
                if e.get(k):
                    ev[k] = e[k]
            if stem:
                ev["retro"] = stem
                if hit and e.get("kind") == "ship":
                    ships_by_stem[stem] = ships_by_stem.get(stem, 0) + 1
                    hit_retro_stems.add(stem)
            events.append(ev)
        days[day] = {"goal": g.get("goal") or "", "time": g.get("time") or "",
                     "no_retro": bool(g.get("no_retro")),
                     "retros": [dict(r) for r in g.get("retros") or []]}

    # Retros narrate their day. A retro joins the module when the tagger
    # said so OR when one of its shipped lines did; the library row (when
    # the backfill has registered it) is what the surface opens.
    for day, info in days.items():
        for r in info["retros"]:
            stem = r.get("stem") or ""
            r["hit"] = stem in hit_retro_stems
            r["ships"] = ships_by_stem.get(stem, 0)
            d = retro_docs.get(stem)
            r["doc"] = _slim(d) if d else None
            if not r.get("goal") and d and (d.get("tags") or {}):
                r["goal"] = ""
        info["hit"] = any(r["hit"] for r in info["retros"])
    # A hit retro the change log never saw still narrates - it is a
    # document, dated, about this module. The library reaches retros from
    # the CRM and TC-IL sessions that predate Vira's own log (the module
    # tags are Vira's vocabulary, so a CRM-era session about mail joins
    # Incoming's story), and those carry no goal from the log - so the
    # goal is read off the retro file itself, bounded (GOAL_READS), never
    # invented from the stem.
    narrated = {r.get("stem") for info in days.values()
                for r in info["retros"]}
    reads = 0
    for stem in sorted(hit_retro_stems):
        if stem in narrated:
            continue
        d = retro_docs.get(stem)
        if not d:
            continue
        day = (_doc_ts(d) or "")[:10]
        if not day:
            continue
        goal = ""
        if reads < GOAL_READS:
            reads += 1
            goal = _retro_goal(d)
        m = _STEM_TIME.match(stem)
        row = {"stem": stem, "goal": goal,
               "time": f"{m.group(1)}:{m.group(2)}" if m else "",
               "session_id": "", "hit": True, "ships": 0, "doc": _slim(d)}
        info = days.setdefault(day, {"goal": "", "time": "",
                                     "no_retro": True, "retros": []})
        info["retros"].append(row)
        info["hit"] = True
        if not info.get("goal") and goal:
            info["goal"] = goal

    events.sort(key=lambda e: (e.get("ts") or "", e.get("kind") or ""))

    eras = {}
    for e in events:
        key = (e.get("day") or "")[:7]
        if len(key) != 7:
            continue
        b = eras.setdefault(key, {"key": key, "label": _month_label(key),
                                  "hits": 0, "total": 0, "days": set()})
        b["total"] += 1
        if e["hit"]:
            b["hits"] += 1
            b["days"].add(e["day"])
    for day, info in days.items():
        if info.get("hit"):
            key = day[:7]
            b = eras.setdefault(key, {"key": key, "label": _month_label(key),
                                      "hits": 0, "total": 0, "days": set()})
            b["days"].add(day)
    era_rows = []
    for key in sorted(eras):
        b = eras[key]
        era_rows.append({"key": key, "label": b["label"], "hits": b["hits"],
                         "total": b["total"], "days": len(b["days"])})

    hit_days = {e["day"] for e in events if e["hit"] and e.get("day")}
    hit_days |= {d for d, i in days.items() if i.get("hit")}
    hit_entries = [e for e in events if e["hit"] and "text" in e]
    stats = {
        "films": counts.get("walkthrough", 0),
        "plans": counts.get("plan", 0),
        "dossiers": counts.get("dossier", 0),
        "briefs": counts.get("brief", 0),
        "sessions": len(hit_retro_stems),
        "changes": len(hit_entries),
        "strong": sum(1 for e in hit_entries if e["strong"]),
        "days": len(hit_days),
        "first": min(hit_days) if hit_days else "",
        "last": max(hit_days) if hit_days else "",
        "changelog_total": sum(1 for e in events if "text" in e),
        "changelog_first": min((e["day"] for e in events if "text" in e),
                               default=""),
        "changelog_last": max((e["day"] for e in events if "text" in e),
                              default=""),
    }

    return {
        "id": win_id,
        "title": reg.get("name") or "",
        "what": reg.get("what") or "",
        "tags": sorted(tags),
        "keywords": phrases,
        "docs": hits,
        "counts": counts,
        "pending": sum(1 for d in docs if not d.get("tagged")),
        "events": events,
        "days": days,
        "eras": era_rows,
        "stats": stats,
    }


# Below these a story is THIN and the audit says why. Measured against the
# live library on 2026-09-02: every window with a real build history clears
# them; the two that do not (Research, Image Atlas) are new modules.
THIN_WHAT = 160          # a registry blurb shorter than this reads as a caption
THIN_CHANGES = 3
THIN_DOCS = 3


def coverage():
    """The audit behind the owner's 2026-09-02 ask ("some build stories are
    missing, others incomplete"): one row per window with a story of its
    own — which registry entry it reads, how much of the library and the
    change log it reaches, and the reasons it is thin, named. Aliases are
    listed under their host. Read-only, derived from the same inputs as
    story(), so the audit cannot disagree with the panel."""
    inp = _inputs()
    rows = []
    for wid, row in WINDOWS.items():
        if row.get("alias"):
            continue
        s = story(wid, inp)
        reg = _registry_entry(row.get("map"))
        thin = []
        if not reg:
            thin.append(f"registry entry {row.get('map')!r} not found")
        elif len(reg.get("what") or "") < THIN_WHAT:
            thin.append("registry description is a caption "
                        f"({len(reg.get('what') or '')} chars)")
        st = s["stats"]
        if st["changes"] < THIN_CHANGES:
            thin.append(f"only {st['changes']} change-log entries join")
        rich = st["films"] + st["plans"] + st["dossiers"]
        if rich < THIN_DOCS:
            thin.append(f"only {rich} films/plans/dossiers")
        if not st["films"]:
            thin.append("no session film")
        rows.append({
            "id": wid, "title": s["title"], "map": row.get("map"),
            "aliases": sorted(k for k, r in WINDOWS.items()
                              if r.get("alias") == wid),
            "registry": bool(reg), "what_chars": len(s["what"]),
            "counts": s["counts"], "stats": st, "thin": thin,
        })
    return {"windows": rows, "pending": inp and
            sum(1 for d in inp["docs"] if not d.get("tagged"))}

"""Views of the history store: trails, entries, groupings, the automation
grid and the time map. Pure reads; nothing here writes.

TRAILS. Events that share an identifier are one request: the ask, every
session that worked on it, the waits, the pull request, the merge (or the
drop), the notes it made, the texts Vira sent about it, and the owner's
choices. The identifiers that join are UNION_KEYS (job, session, idea,
journal note, chat, branch, pull request, commit, Flow run, Program run). A
Flow or Program id never joins (every run of an hourly capture would be one
trail), and neither does a session's parent (a session started from another
is its own request).

ENTRIES. What one row shows depends on the question (the `group`
parameter): a trail by day, request, module or project; one session by
session; one run by automation. Every entry has the same shape (entry()),
so a front end draws them all with one component.

STATUS. A trail's word says where it ended up (settle()): Running or
Waiting on you while anything is; then the code's fate (Not merged while a
branch or pull request is open, else Merged, else Dropped); then the idea's
(Dropped when set aside); then Failed for a failed automation or session;
Ran for an automation; Answered otherwise.

Repeating automations belong to the grid: list views leave a trail made
only of runs out unless it Failed or waits on the owner, or the caller asks
for them (kind=run, or automations=1).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta

from . import history as H

UNION_KEYS = ("job", "session", "idea", "note", "chat", "branch", "pr", "commit",
              "flow_run", "program_run")
GROUPS = ("day", "request", "module", "project", "automation", "session", "none")
AUTOMATION_SUBS = ("flow", "step", "program", "contacts")
# Gaps between two events of a trail longer than this show as a wait.
WAIT_GAP_S = 3600
LIST_LIMIT = 200

_lock = threading.Lock()
_cache = {"rev": None, "index": None}


# ---------------------------------------------------------------- the index

class Index:
    """Every event, its trail, and each trail's summary, built once per
    store revision."""

    def __init__(self, events):
        self.events = {}
        for eid, e in events.items():
            e = dict(e)
            e["ts"] = H.to_ts(e.get("at"))
            e["end_ts"] = H.to_ts(e.get("end")) or e["ts"]
            self.events[eid] = e
        self.by_key = {}
        for eid, e in self.events.items():
            for k, vals in (e.get("links") or {}).items():
                for v in vals:
                    self.by_key.setdefault(f"{k}:{v}", []).append(eid)
        self._branch_fates()
        self._trails()

    def _branch_fates(self):
        """A session's own word is "Not merged" while its code is on a
        branch; once that branch merged (or was discarded) the session's
        word says so. The stored line keeps the session's own word
        (`own_status`)."""
        fate = {}
        for e in self.events.values():
            if e["kind"] != "code":
                continue
            links = e.get("links") or {}
            for b in list(links.get("branch", [])) + list(links.get("pr", [])):
                if e["sub"] in ("merged", "merged_branch") or (
                        e["sub"] == "pr_opened" and e["status"] == H.MERGED):
                    fate[b] = H.MERGED
                elif e["sub"] == "discarded" and fate.get(b) != H.MERGED:
                    fate[b] = H.DROPPED
        for e in self.events.values():
            if e["kind"] != "session":
                continue
            links = e.get("links") or {}
            if e["status"] == H.NOT_MERGED:
                for b in links.get("branch", []):
                    if b in fate:
                        e["own_status"], e["status"] = e["status"], fate[b]
                        break
            elif e["status"] == H.WAITING:
                # A session that ended saying it waits on a review whose
                # every branch and pull request has since merged is merged.
                keys = list(links.get("branch", [])) + list(links.get("pr", []))
                if keys and all(fate.get(k) == H.MERGED for k in keys):
                    e["own_status"], e["status"] = e["status"], H.MERGED

    def _trails(self):
        parent = {eid: eid for eid in self.events}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for k in UNION_KEYS:
            prefix = k + ":"
            for key, ids in self.by_key.items():
                if not key.startswith(prefix) or len(ids) < 2:
                    continue
                a = find(ids[0])
                for b in ids[1:]:
                    rb = find(b)
                    if rb != a:
                        parent[rb] = a
        groups = {}
        for eid in self.events:
            groups.setdefault(find(eid), []).append(eid)
        self.trails, self.trail_of = {}, {}
        for members in groups.values():
            evs = sorted((self.events[i] for i in members), key=lambda e: (e["ts"], e["id"]))
            root = _root(evs)
            tid = "trail:" + root["id"]
            self.trails[tid] = {"id": tid, "root": root["id"], "members": [e["id"] for e in evs]}
            for e in evs:
                self.trail_of[e["id"]] = tid
        for t in self.trails.values():
            t.update(_summarize(self, t))

    def trail_events(self, tid):
        t = self.trails.get(tid)
        return [self.events[i] for i in t["members"]] if t else []

    def resolve(self, ident):
        """A trail id, an event id, or "<link kind>:<value>" (job:..,
        idea:.., branch:vira:claude/x, pr:vira#178, session:..) -> trail id."""
        ident = str(ident or "")
        if ident in self.trails:
            return ident
        if ident in self.events:
            return self.trail_of[ident]
        ids = self.by_key.get(ident)
        if ids:
            return self.trail_of[ids[0]]
        return None


def index():
    """The index for the store as it is now, rebuilt when it changed."""
    rev = H.revision()
    with _lock:
        if _cache["rev"] == rev and _cache["index"] is not None:
            return _cache["index"]
    idx = Index(H.load())
    with _lock:
        _cache.update(rev=rev, index=idx)
    return idx


def _root(evs):
    asks = [e for e in evs if e["kind"] == "ask"]
    if asks:
        return asks[0]
    for kind in ("session", "run", "code"):
        got = [e for e in evs if e["kind"] == kind and (kind != "run" or e["sub"] == "flow")]
        if got:
            return got[0]
    return evs[0]


# ---------------------------------------------------------------- summaries

def _open_code(evs):
    """Code that has not reached main: branches still not merged, pull
    requests still open."""
    out = []
    for e in evs:
        if e["kind"] != "code":
            continue
        if e["sub"] == "not_merged" and e["status"] == H.NOT_MERGED:
            out.append(e)
        elif e["sub"] == "pr_opened" and e["status"] == H.NOT_MERGED:
            out.append(e)
        elif e["sub"] == "worktree" and e["status"] == H.NOT_MERGED:
            out.append(e)
    return out


def _all_code_merged(evs):
    """True when the trail has code and every branch and pull request it
    names reached main (nothing still open, nothing discarded)."""
    code = [e for e in evs if e["kind"] == "code"]
    if not code or _open_code(evs):
        return False
    fate = {}
    for e in code:
        links = e.get("links") or {}
        keys = list(links.get("branch") or []) + list(links.get("pr") or [])
        merged = e["sub"] in ("merged", "merged_branch") or e["status"] == H.MERGED
        for k in keys:
            if merged:
                fate[k] = H.MERGED
            elif e["status"] == H.DROPPED and fate.get(k) != H.MERGED:
                fate[k] = H.DROPPED
            else:
                fate.setdefault(k, e["status"])
    named = set()
    for e in evs:
        links = e.get("links") or {}
        named.update(links.get("branch") or [])
        named.update(links.get("pr") or [])
    return bool(named) and all(fate.get(k) == H.MERGED for k in named)


def _automated(e):
    """A session an automation started: a Flow step or a routine run."""
    links = e.get("links") or {}
    return bool(links.get("flow_run") or links.get("schedule")
                or (e.get("data") or {}).get("kind_label") == "Routine")


def trail_kind(evs):
    """request: something asked, worked on or decided. automation: runs,
    and the sessions automations started. record: only things made or
    texts sent, with nothing else around them."""
    kinds = {e["kind"] for e in evs}
    if kinds & {"ask", "code", "decide", "answer"}:
        return "request"
    sessions = [e for e in evs if e["kind"] == "session"]
    if sessions and not all(_automated(e) for e in sessions):
        return "request"
    if "run" in kinds or sessions:
        return "automation"
    return "record"


def settle(evs):
    """The one word for a group of events: where the work ended up."""
    statuses = [e["status"] for e in evs]
    if H.RUNNING in statuses:
        return H.RUNNING
    # Every branch and pull request on the trail reached main: the work is
    # merged, whatever a session's last summary said about waiting.
    if _all_code_merged(evs):
        return H.MERGED
    merged_at = max((e["ts"] for e in evs if e["kind"] == "code"
                     and e["status"] == H.MERGED), default=0)
    # A wait the code has since moved past (a merge after it) is over.
    if any(e["status"] == H.WAITING and max(e["ts"], e["end_ts"]) >= merged_at
           for e in evs if e["kind"] in ("session", "run", "code")):
        return H.WAITING
    if _open_code(evs):
        return H.NOT_MERGED
    code = [e for e in evs if e["kind"] == "code"]
    decided = [e for e in evs if e["kind"] == "decide"]
    if any(e["status"] == H.MERGED for e in code) or any(
            e["sub"] == "landing" and e["status"] == H.MERGED for e in decided):
        return H.MERGED
    if any(e["status"] == H.DROPPED for e in code) or any(
            e["sub"] in ("landing", "discard") and e["status"] == H.DROPPED for e in decided):
        return H.DROPPED
    ideas = [e for e in evs if e["kind"] == "ask" and e["sub"] == "idea"]
    sessions = [e for e in evs if e["kind"] == "session"]
    if ideas and all(e["status"] == H.DROPPED for e in ideas):
        return H.DROPPED
    runs_on = [e for e in evs if e["kind"] == "run"]
    if ideas and not sessions and not runs_on and any(
            e["status"] in (H.WAITING, H.NOT_STARTED) for e in ideas):
        # No session has started on it: it stays in the Ideas tab.
        return H.NOT_STARTED
    kind = trail_kind(evs)
    runs = [e for e in evs if e["kind"] == "run"]
    if kind == "automation":
        tops = [e for e in runs if e["sub"] in ("flow", "program", "contacts")] or runs \
            or [e for e in evs if e["kind"] == "session"]
        top = max(tops, key=lambda e: e["ts"])
        if top["status"] == H.FAILED or any(e["status"] == H.FAILED for e in tops):
            return H.FAILED
        return H.RAN
    if kind == "record":
        top = max(evs, key=lambda e: e["ts"])
        return top["status"] if top["status"] in (H.RAN, H.FAILED, H.DROPPED) else H.RAN
    if sessions and all(e["status"] == H.FAILED for e in sessions):
        return H.FAILED
    if sessions and not ideas:
        newest = max(sessions, key=lambda e: e["ts"])
        if newest["status"] in (H.RAN, H.FAILED):
            return newest["status"]
    # A session on a branch git never saw a commit on has nothing to merge.
    return H.ANSWERED


def trail_title(evs):
    """The title rule over a whole trail: PR title, idea title, Flow or
    Program name with counts, the app's session title, then the first
    sentence of the ask."""
    pr = next((e["title"] for e in reversed(evs) if e["kind"] == "code"
               and e["sub"] == "pr_opened" and e["title"]), "")
    if not pr:
        pr = next((e["title"] for e in reversed(evs) if e["kind"] == "code"
                   and e["sub"] == "merged" and (e.get("links") or {}).get("pr")
                   and e["title"]), "")
    idea = next((e["title"] for e in evs if e["kind"] == "ask" and e["sub"] == "idea"), "")
    flow = next((e["title"] for e in evs if e["kind"] == "run" and e["sub"] == "flow"), "")
    auto = flow or next((e["title"] for e in evs if e["kind"] == "run"
                         and e["sub"] in ("program", "contacts")), "")
    app = next((e["title"] for e in evs if e["kind"] == "session"
                and e["sub"] in ("claude", "codex") and e["title"]), "")
    ask = next((e["title"] for e in evs if e["kind"] == "ask" and e["sub"] != "idea"), "") \
        or next((e["title"] for e in evs if e["kind"] == "session" and e["title"]), "") \
        or next((e["title"] for e in evs if e["title"]), "")
    return H.pick_title(pr, idea, auto, app, "") or ask or "Untitled"


def _sum_diff(evs):
    files = {}
    for e in evs:
        for row in (e.get("data") or {}).get("files_changed") or []:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                continue
            cur = files.setdefault(row[0], [row[0], 0, 0])
            cur[1] += int(row[1] or 0)
            cur[2] += int(row[2] or 0)
    rows = sorted(files.values(), key=lambda r: -(r[1] + r[2]))
    return {"files": len(rows), "added": sum(r[1] for r in rows),
            "removed": sum(r[2] for r in rows), "by_file": rows}


def diff_of(evs):
    """Outcome lines: what reached main (the merges), else what is still
    on a branch, else the commits seen."""
    merges = [e for e in evs if e["kind"] == "code" and e["sub"] in ("merged",)]
    if merges:
        return dict(_sum_diff(merges), basis="merged into main")
    open_ = [e for e in _open_code(evs) if e["sub"] == "not_merged"]
    if open_:
        return dict(_sum_diff(open_), basis="on a branch, not merged")
    commits = [e for e in evs if e["kind"] == "code" and e["sub"] == "commit"]
    if commits:
        return dict(_sum_diff(commits), basis="commits")
    return {"files": 0, "added": 0, "removed": 0, "by_file": [], "basis": "no code"}


def cost_time(evs):
    sessions = [e for e in evs if e["kind"] == "session"]
    usd, known, unknown, tokens, active = 0.0, 0, 0, 0, 0.0
    rows = []
    for e in sessions:
        d = e.get("data") or {}
        c = d.get("cost_usd")
        if c is None:
            if d.get("tokens"):
                unknown += 1
        else:
            usd += float(c)
            known += 1
        tokens += int(d.get("tokens") or 0)
        active += float(d.get("active_s") or 0)
        rows.append({"id": e["id"], "app": d.get("app") or e["sub"], "model": d.get("model") or "",
                     "start": e.get("at"), "end": e.get("end") or e.get("at"),
                     "tokens": int(d.get("tokens") or 0), "cost_usd": c,
                     "active_s": float(d.get("active_s") or 0), "status": e["status"],
                     "title": e["title"]})
    for e in evs:
        if e["kind"] == "answer" and e["sub"] == "chat":
            d = e.get("data") or {}
            if d.get("cost_usd") is not None:
                usd += float(d["cost_usd"])
                known += 1
            tokens += int(d.get("tokens") or 0)
    first = min((e["ts"] for e in evs if e["ts"]), default=0)
    last = max((max(e["ts"], e["end_ts"]) for e in evs), default=0)
    return {"cost_usd": round(usd, 4) if known else None, "cost_basis": H.COST_BASIS if known else "",
            "cost_sessions_priced": known, "cost_sessions_unpriced": unknown,
            "tokens": tokens, "active_s": round(active, 1),
            "wall_s": round(last - first, 1) if first and last else 0,
            "sessions": rows}


def _modules(evs):
    counts = {}
    for e in evs:
        for m, n in (e.get("modules") or {}).items():
            counts[m] = counts.get(m, 0) + int(n or 0)
        if e.get("module") and not e.get("modules"):
            counts[e["module"]] = counts.get(e["module"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _summarize(idx, t):
    evs = [idx.events[i] for i in t["members"]]
    kinds = {}
    for e in evs:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    projects = []
    for e in evs:
        p = e.get("project")
        if p and p not in projects and p != "No folder":
            projects.append(p)
    mods = _modules(evs)
    prs = []
    for e in evs:
        for k in (e.get("links") or {}).get("pr", []):
            if k not in prs:
                prs.append(k)
    branches = []
    for e in evs:
        for k in (e.get("links") or {}).get("branch", []):
            if k not in branches:
                branches.append(k)
    first = min((e["ts"] for e in evs if e["ts"]), default=0)
    last = max((max(e["ts"], e["end_ts"]) for e in evs), default=0)
    sessions = [e for e in evs if e["kind"] == "session"]
    return {
        "status": settle(evs), "title": trail_title(evs),
        "kind": trail_kind(evs),
        "first_ts": first, "last_ts": last, "projects": projects,
        "project": projects[0] if projects else "", "modules": mods,
        "module": next(iter(mods), ""), "kinds": kinds, "prs": prs, "branches": branches,
        "sessions": len(sessions), "apps": sorted({(e.get("data") or {}).get("app") or e["sub"]
                                                   for e in sessions}),
    }


# ---------------------------------------------------------------- entries

def _ago_days(first, last):
    if not first or not last:
        return 0
    return max(1, (datetime.fromtimestamp(last).date() - datetime.fromtimestamp(first).date()).days + 1)


def entry_of_trail(idx, tid):
    t = idx.trails[tid]
    evs = idx.trail_events(tid)
    d = diff_of(evs)
    ct = cost_time(evs)
    return {
        "id": tid, "type": "trail", "kind": t["kind"], "status": t["status"],
        "title": t["title"], "at": H.iso(t["last_ts"]), "start": H.iso(t["first_ts"]),
        "project": t["project"], "projects": t["projects"], "module": t["module"],
        "modules": t["modules"], "prs": t["prs"], "branches": t["branches"],
        "sessions": t["sessions"], "apps": t["apps"], "days": _ago_days(t["first_ts"], t["last_ts"]),
        "events": len(evs), "kinds": t["kinds"],
        "lines": {"files": d["files"], "added": d["added"], "removed": d["removed"],
                  "basis": d["basis"]},
        "cost_usd": ct["cost_usd"], "tokens": ct["tokens"], "active_s": ct["active_s"],
        "wall_s": ct["wall_s"], "actions": actions_for(idx, tid),
        "open": H.STATUSES.index(t["status"]) < 2,
    }


def entry_of_event(idx, e):
    d = e.get("data") or {}
    diff = d.get("diff") or {}
    return {
        "id": e["id"], "type": "event", "kind": e["kind"], "sub": e["sub"],
        "status": e["status"], "title": e["title"], "at": e.get("end") or e.get("at"),
        "start": e.get("at"), "project": e.get("project") or "",
        "projects": [e["project"]] if e.get("project") else [], "module": e.get("module") or "",
        "modules": e.get("modules") or {}, "prs": (e.get("links") or {}).get("pr", []),
        "branches": (e.get("links") or {}).get("branch", []),
        "sessions": 1 if e["kind"] == "session" else 0,
        "apps": [d.get("app")] if d.get("app") else [], "days": 1, "events": 1,
        "kinds": {e["kind"]: 1},
        "lines": {"files": diff.get("files", 0), "added": diff.get("added", 0),
                  "removed": diff.get("removed", 0), "basis": "this event"},
        "cost_usd": d.get("cost_usd"), "tokens": d.get("tokens") or 0,
        "active_s": d.get("active_s") or 0,
        "wall_s": round(max(0.0, e["end_ts"] - e["ts"]), 1),
        "counts": d.get("counts") or {}, "trail": idx.trail_of.get(e["id"]),
        "actions": actions_for(idx, idx.trail_of.get(e["id"]), event=e),
        "open": e["status"] in H.OPEN_STATUSES,
    }


# ---------------------------------------------------------------- filters

def _day_bounds(value, end=False):
    """A date ("2026-11-04"), a local datetime or a UTC ISO time -> epoch.
    A bare date's end bound is the end of that local day."""
    if not value:
        return None
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        d = datetime.strptime(s, "%Y-%m-%d")
        if end:
            d = d + timedelta(days=1)
        return d.timestamp()
    ts = H.to_ts(s)
    return ts or None


def _on(v):
    return str(v or "").lower() in ("1", "true", "yes", "on")


def parse_filters(*, since=None, until=None, project=None, module=None, status=None,
                  kind=None, q=None, app=None, automations=None, unstarted=None):
    """`unstarted` puts ideas no session has started ("Not started") back
    into the lists; naming the word in `status` does the same."""
    def split(v):
        return [x.strip() for x in str(v or "").split(",") if x.strip()]
    return {"since": _day_bounds(since), "until": _day_bounds(until, end=True),
            "project": split(project), "module": split(module), "status": split(status),
            "kind": split(kind), "q": [w.lower() for w in str(q or "").split() if w],
            "app": [a.lower() for a in split(app)],
            "automations": _on(automations), "unstarted": _on(unstarted)}


def _text_of(e):
    d = e.get("data") or {}
    bits = [e.get("title") or "", e.get("project") or "", e.get("module") or "",
            " ".join((e.get("links") or {}).get("branch", [])),
            " ".join((e.get("links") or {}).get("pr", [])),
            str(d.get("text") or ""), str(d.get("name") or ""), str(d.get("app") or ""),
            str(d.get("model") or "")]
    return " ".join(bits).lower()


def _event_passes(e, f, with_status=True):
    if f["since"] and max(e["ts"], e["end_ts"]) < f["since"]:
        return False
    if f["until"] and e["ts"] and e["ts"] >= f["until"]:
        return False
    if f["project"] and (e.get("project") or "") not in f["project"]:
        return False
    if f["module"]:
        mods = set((e.get("modules") or {}).keys()) | ({e["module"]} if e.get("module") else set())
        if not any(m == want or m.startswith(want + "/") for m in mods for want in f["module"]):
            return False
    if f["kind"] and e["kind"] not in f["kind"] and e.get("sub") not in f["kind"]:
        return False
    if f["app"] and str((e.get("data") or {}).get("app") or e.get("sub") or "").lower() \
            not in f["app"] and (e.get("sub") or "") not in f["app"]:
        return False
    if with_status and f["status"] and e["status"] not in f["status"]:
        return False
    if with_status and not _unstarted_shown(e["status"], f):
        return False
    return True


def _unstarted_shown(status, f):
    """Ideas no session has started stay out of the lists unless the
    filter asks for them (`unstarted`, or the word in `status`)."""
    return status not in H.UNSTARTED or f.get("unstarted") or status in (f.get("status") or [])


def _trail_passes(idx, tid, f):
    t = idx.trails[tid]
    evs = idx.trail_events(tid)
    if f["status"] and t["status"] not in f["status"]:
        return False
    if not _unstarted_shown(t["status"], f):
        return False
    if t["kind"] != "request" and not (
            f["automations"] or f["q"] or t["status"] in (H.FAILED, H.WAITING)
            or set(f["kind"]) & {"run", "made", "tell", "flow", "program", "note", "retro",
                                 "document", "contact", "contacts"}):
        return False
    if f["q"]:
        text = " ".join(_text_of(e) for e in evs) + " " + t["title"].lower()
        if not all(w in text for w in f["q"]):
            return False
    plain = dict(f, q=[], status=[])
    return any(_event_passes(e, plain, with_status=False) for e in evs)


# ---------------------------------------------------------------- the list

def _group_label(group, key):
    if group == "day":
        if key == "now":
            return "Now"
        try:
            d = datetime.strptime(key, "%Y-%m-%d")
            today = datetime.now().date()
            if d.date() == today:
                return "Today"
            if d.date() == today - timedelta(days=1):
                return "Yesterday"
            return d.strftime("%a %b ") + str(d.day)
        except ValueError:
            return key
    return key or {"module": "No module", "project": "No folder"}.get(group, "Other")


def list_entries(group="day", filters=None, limit=LIST_LIMIT, offset=0, unit=None):
    """Entries for one question. group: day, request, module, project,
    automation, session or none. unit "event" lists events instead of
    trails (the finest grain), grouped the same way."""
    if group not in GROUPS:
        raise ValueError(f"unknown group {group!r}; one of {', '.join(GROUPS)}")
    f = filters or parse_filters()
    idx = index()
    if group in ("automation", "session") and unit is None:
        unit = "event"
    unit = unit or "trail"
    entries = []
    if unit == "event":
        for e in idx.events.values():
            if group == "automation" and not (e["kind"] == "run" and e["sub"] in AUTOMATION_SUBS
                                              and e["sub"] != "step"):
                continue
            if group == "session" and e["kind"] != "session":
                continue
            if not _event_passes(e, f):
                continue
            if f["q"] and not all(w in _text_of(e) for w in f["q"]):
                continue
            entries.append(e)
        rows = [entry_of_event(idx, e) for e in entries]
    else:
        tids = [tid for tid in idx.trails if _trail_passes(idx, tid, f)]
        rows = [entry_of_trail(idx, tid) for tid in tids]
    left_out = unstarted_left_out(idx, f, unit)
    rows.sort(key=lambda r: (H.to_ts(r["at"]), r["id"]), reverse=True)
    groups, order = {}, []

    def add(key, row):
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    for r in rows:
        if group == "day":
            # Unfinished work with something in flight (a session, a run,
            # a branch) is pinned on top; a bare waiting idea stays on its day.
            pin = r["open"] and unit == "trail" and (
                r["sessions"] or r["kinds"].get("run") or r["kinds"].get("code"))
            add("now" if pin else H.local_day(H.to_ts(r["at"])), r)
        elif group in ("request", "none"):
            add("all", r)
        elif group == "module":
            mods = list((r.get("modules") or {}).keys()) or [""]
            for m in mods:
                add(m, r)
        elif group == "project":
            for p in r.get("projects") or [""]:
                add(p, r)
        elif group == "automation":
            e = idx.events[r["id"]]
            links = e.get("links") or {}
            key = (links.get("flow") or links.get("program") or [e["title"]])[0]
            add(key, r)
        elif group == "session":
            add(H.local_day(H.to_ts(r["start"])), r)
    if group == "day" and "now" in order:
        order.remove("now")
        order.insert(0, "now")
    if group in ("module", "project"):
        order.sort(key=lambda k: (-len(groups[k]), k))
    total = len(rows)
    out_groups, shown = [], 0
    for key in order:
        items = groups[key]
        out_groups.append({"key": key, "label": _automation_label(idx, key, items) if group == "automation"
                           else _group_label(group, key), "count": len(items),
                           "entries": items})
    # Paging is over entries in group order.
    flat = [(g, e) for g in out_groups for e in g["entries"]]
    page = flat[offset:offset + limit]
    paged, seen = [], {}
    for g, e in page:
        if g["key"] not in seen:
            seen[g["key"]] = {"key": g["key"], "label": g["label"], "count": g["count"],
                              "entries": []}
            paged.append(seen[g["key"]])
        seen[g["key"]]["entries"].append(e)
        shown += 1
    return {"group": group, "unit": unit, "total": total, "offset": offset, "limit": limit,
            "shown": shown, "more": offset + shown < len(flat), "groups": paged,
            "statuses": _status_counts(rows), "unstarted_left_out": left_out,
            "revision": H.revision()}


def unstarted_left_out(idx, f, unit="trail"):
    """How many ideas no session has started the filter left out (0 when
    it shows them), so a list can say they are in the Ideas tab."""
    if f.get("unstarted") or set(f.get("status") or []) & set(H.UNSTARTED):
        return 0
    g = dict(f, unstarted=True)
    if unit == "event":
        return sum(1 for e in idx.events.values() if e["status"] in H.UNSTARTED
                   and _event_passes(e, g)
                   and not (g["q"] and not all(w in _text_of(e) for w in g["q"])))
    return sum(1 for tid, t in idx.trails.items() if t["status"] in H.UNSTARTED
               and _trail_passes(idx, tid, g))


def _status_counts(rows):
    out = {s: 0 for s in H.STATUSES}
    for r in rows:
        if r["status"] in out:
            out[r["status"]] += 1
    return out


def _automation_label(idx, key, items):
    if items:
        e = idx.events[items[0]["id"]]
        return (e.get("data") or {}).get("name") or re.sub(r":.*$", "", e["title"]) or key
    return key


# ---------------------------------------------------------------- detail

def detail(ident):
    """Everything about one entry: outcome and diff, the whole trail, what
    it touched, cost and time, and the actions available. `ident` is a
    trail id, an event id, or a link key (idea:<id>, job:<id>, ...)."""
    idx = index()
    tid = idx.resolve(ident)
    if not tid:
        raise KeyError(ident)
    evs = idx.trail_events(tid)
    focus = idx.events.get(ident)
    entry = entry_of_trail(idx, tid)
    d = diff_of(evs)
    ending = _ending(evs, entry["status"])
    return {
        "entry": entry,
        "focus": entry_of_event(idx, focus) if focus else None,
        "outcome": {"status": entry["status"], "means": H.STATUS_MEANS[entry["status"]],
                    "ended_by": ending, "diff": d},
        "trail": _trail_items(evs),
        "touched": _touched(evs),
        "cost_time": cost_time(evs),
        "actions": entry["actions"],
        "open": _openers(evs),
    }


def _ending(evs, status):
    want = {H.MERGED: lambda e: e["kind"] == "code" and e["status"] == H.MERGED,
            H.DROPPED: lambda e: e["status"] == H.DROPPED,
            H.ANSWERED: lambda e: e["kind"] in ("answer", "session"),
            H.RAN: lambda e: e["kind"] == "run",
            H.FAILED: lambda e: e["status"] == H.FAILED}.get(status)
    if not want:
        return None
    hit = [e for e in evs if want(e)]
    if not hit:
        return None
    e = max(hit, key=lambda x: (x["end_ts"], x["ts"]))
    return {"id": e["id"], "kind": e["kind"], "sub": e["sub"], "title": e["title"],
            "at": e.get("end") or e.get("at")}


_WAIT_TITLES = {"landing": "Waited for your merge choice", "ask": "Waited for your answer",
                "question": "Waited for your answer", "permission": "Waited for your permission",
                "plan": "Waited for your approval of the plan"}
_ITEM_DATA = ("app", "model", "tokens", "cost_usd", "active_s", "counts", "errors",
              "error", "error_lines", "diff", "verdict", "answer", "number", "url", "state",
              "pr_url", "kind_label", "logs", "log_dir", "path", "text", "name", "trigger",
              "open_url", "transcript", "commits", "approved", "channel")


def _trail_items(evs):
    items = []
    for e in evs:
        d = e.get("data") or {}
        items.append({"type": "event", "id": e["id"], "kind": e["kind"], "sub": e["sub"],
                      "status": e["status"], "title": e["title"], "at": e.get("at"),
                      "end": e.get("end") or "", "project": e.get("project") or "",
                      "module": e.get("module") or "",
                      "links": {k: v for k, v in (e.get("links") or {}).items()
                                if k not in ("files", "commit")},
                      "data": {k: d[k] for k in _ITEM_DATA if k in d and d[k] not in (None, "", [], {})}})
        for c in d.get("cards") or []:
            if c.get("at"):
                kind = c.get("kind") or "card"
                items.append({"type": "wait", "id": f"{e['id']}:card:{c.get('req_id')}",
                              "kind": "wait", "sub": kind, "status": H.WAITING,
                              "title": _WAIT_TITLES.get(kind, "Waited for you"),
                              "at": c["at"], "end": "", "links": {},
                              "data": {"session": e["id"]}})
    items.sort(key=lambda x: (H.to_ts(x["at"]), x["id"]))
    # Close each wait at the decision that answered it, and show long quiet
    # spells between events as waits too.
    decisions = [x for x in items if x["kind"] == "decide"]
    out, prev = [], None
    for x in items:
        if x["type"] == "wait":
            nxt = next((dcs for dcs in decisions if H.to_ts(dcs["at"]) >= H.to_ts(x["at"])), None)
            if nxt:
                x["end"] = nxt["at"]
                x["status"] = H.ANSWERED
        if prev and x["type"] == "event" and prev["type"] == "event":
            gap = H.to_ts(x["at"]) - max(H.to_ts(prev["at"]), H.to_ts(prev["end"]))
            if gap > WAIT_GAP_S:
                out.append({"type": "gap", "id": f"gap:{prev['id']}", "kind": "wait", "sub": "quiet",
                            "status": "", "title": "Nothing happened", "at": prev["end"] or prev["at"],
                            "end": x["at"], "links": {}, "data": {"seconds": round(gap)}})
        out.append(x)
        prev = x
    return out


def _touched(evs):
    files = {}
    for e in evs:
        for f in (e.get("links") or {}).get("files", []):
            files[f] = files.get(f, 0) + 1
    notes = [{"id": e["id"], "title": e["title"], "path": (e.get("data") or {}).get("path")
              or ((e.get("links") or {}).get("notes") or [""])[0], "at": e.get("at")}
             for e in evs if e["kind"] == "made" and e["sub"] in ("note", "retro", "document")]
    contacts = [{"id": e["id"], "title": e["title"], "at": e.get("at")}
                for e in evs if e["kind"] == "made" and e["sub"] == "contact"]
    projects = []
    for e in evs:
        if e.get("project") and e["project"] not in projects:
            projects.append(e["project"])
    return {"modules": _modules(evs), "projects": projects,
            "files": sorted(files, key=lambda k: -files[k])[:500], "files_n": len(files),
            "made": notes, "contacts": contacts,
            "branches": sorted({b for e in evs for b in (e.get("links") or {}).get("branch", [])}),
            "prs": sorted({p for e in evs for p in (e.get("links") or {}).get("pr", [])})}


def _openers(evs):
    out = []
    for e in evs:
        d = e.get("data") or {}
        if e["kind"] == "session":
            if e["sub"] == "vira":
                job = (e["links"].get("job") or [""])[0]
                out.append({"what": "session", "label": "Open session", "job_id": job,
                            "event": e["id"]})
            elif d.get("open_url"):
                out.append({"what": "session", "label": f"Open in {d.get('app')}",
                            "url": d["open_url"], "event": e["id"]})
        if d.get("pr_url") or (e["kind"] == "code" and d.get("url")):
            out.append({"what": "pr", "label": "Open pull request",
                        "url": d.get("pr_url") or d.get("url"), "event": e["id"]})
        if e["kind"] == "run" and d.get("log_dir"):
            out.append({"what": "log", "label": "Open log", "path": d["log_dir"],
                        "program": (e["links"].get("program") or [""])[0],
                        "run": (e["links"].get("program_run") or [""])[0], "event": e["id"]})
    return out


# ---------------------------------------------------------------- actions

def _vira_jobs(evs):
    return [e for e in evs if e["kind"] == "session" and e["sub"] == "vira"]


def actions_for(idx, tid, event=None):
    """The buttons this entry's work allows, each naming the action
    endpoint and what it confirms first. Merge and Discard always carry
    `confirm`: the endpoint refuses them without confirm=true."""
    if not tid:
        return []
    evs = idx.trail_events(tid)
    acts = []
    running = any(e["status"] == H.RUNNING for e in evs if e["kind"] == "session")
    open_code = [e for e in _open_code(evs) if e["sub"] in ("not_merged", "worktree")]
    branch_names = []
    for e in open_code:
        name = (e.get("data") or {}).get("branch_name") or ""
        if name and name not in branch_names:
            branch_names.append(name)
    waiting = [e for e in _vira_jobs(evs) if e["status"] == H.WAITING
               and any(c.get("kind") == "landing" for c in (e.get("data") or {}).get("cards") or [])]
    for name in branch_names:
        if name.startswith("claude/") and not running:
            acts.append({"action": "merge", "label": "Merge", "branch": name,
                         "confirm": f"Merge {name} into live main?"})
            acts.append({"action": "discard", "label": "Discard", "branch": name,
                         "confirm": f"Discard {name}? This deletes the branch."})
        if not running:
            acts.append({"action": "resume", "label": "Resume", "branch": name})
    for e in waiting:
        job = (e["links"].get("job") or [""])[0]
        if not any(a["action"] == "merge" for a in acts):
            acts.append({"action": "merge", "label": "Merge", "job": job,
                         "confirm": "Merge this session's branch into live main?"})
            acts.append({"action": "discard", "label": "Discard", "job": job,
                         "confirm": "Discard this session's branch? This deletes it."})
    runs = [e for e in evs if e["kind"] == "run" and e["sub"] in ("flow", "program")]
    if event is not None and event["kind"] == "run" and event["sub"] in ("flow", "program"):
        runs = [event]
    if runs:
        r = max(runs, key=lambda e: e["ts"])
        if r["status"] not in (H.RUNNING, H.WAITING):
            acts.append({"action": "rerun", "label": "Rerun", "run": r["id"]})
    done_code = [e for e in _vira_jobs(evs) if (e.get("data") or {}).get("code")
                 and e["status"] not in (H.RUNNING, H.WAITING)]
    if done_code:
        job = (max(done_code, key=lambda e: e["ts"])["links"].get("job") or [""])[0]
        acts.append({"action": "judge", "label": "Judge", "job": job})
    for a in acts:
        a.update(method="POST", url="/api/history/action", body={
            "id": tid, "action": a["action"],
            **{k: a[k] for k in ("branch", "job", "run") if k in a}})
    return acts


# ---------------------------------------------------------------- the grid

def _schedules():
    """Enabled Schedule rows: {id, every_hours, daily_at, weekday, flow,
    created} from data/routines.json, read fresh."""
    from . import routines, jsonstore
    s = jsonstore.read(routines.STORE, {"routines": []}) or {}
    out = []
    for r in s.get("routines") or []:
        if not isinstance(r, dict) or not r.get("enabled") or r.get("archived"):
            continue
        out.append({"id": r.get("id"), "name": r.get("name") or "",
                    "every_hours": float(r.get("every_hours") or 0),
                    "daily_at": r.get("daily_at") or "", "weekday": (r.get("weekday") or "").lower(),
                    "flow": r.get("circuit_id") or "", "kind": r.get("kind") or "",
                    "created": H.to_ts(r.get("created"))})
    return out


def _expected(sched, start, end):
    """Expected fire times of one schedule in [start, end), local time."""
    from .routines import WEEKDAYS
    out = []
    if sched["daily_at"]:
        try:
            hh, mm = [int(x) for x in sched["daily_at"].split(":")[:2]]
        except ValueError:
            return out
        day = datetime.fromtimestamp(start).replace(hour=0, minute=0, second=0, microsecond=0)
        while day.timestamp() < end:
            if not sched["weekday"] or WEEKDAYS[day.weekday()] == sched["weekday"]:
                t = day.replace(hour=hh, minute=mm).timestamp()
                if start <= t < end:
                    out.append(t)
            day += timedelta(days=1)
    elif sched["every_hours"] > 0:
        step = sched["every_hours"] * 3600
        t = start - (start % step) + step if start % step else start
        while t < end:
            out.append(t)
            t += step
    return out


def grid(span="day", since=None, until=None, project=None):
    """One row per Flow and per Program (and the contacts sync), one cell
    per local day or hour. A cell's outcome is Failed when any run in it
    failed, Running or Waiting on you while one is, Ran otherwise; "missed"
    when a schedule expected a run there and none happened; "none" when
    nothing was due. Gaps are only judged from a row's first stored run
    (no false misses before history began) and up to now."""
    if span not in ("day", "hour"):
        raise ValueError("span is day or hour")
    idx = index()
    now = time.time()
    step = 86400 if span == "day" else 3600
    end_ts = _day_bounds(until, end=True) or now
    if span == "day":
        start_ts = _day_bounds(since) or (datetime.fromtimestamp(end_ts).replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(days=13)).timestamp()
        start_ts = datetime.fromtimestamp(start_ts).replace(hour=0, minute=0, second=0,
                                                            microsecond=0).timestamp()
    else:
        start_ts = _day_bounds(since) or (end_ts - 48 * 3600)
        start_ts -= start_ts % 3600
    cells_t = []
    t = start_ts
    while t < end_ts:
        if span == "day":
            nxt = (datetime.fromtimestamp(t) + timedelta(days=1)).timestamp()
        else:
            nxt = t + step
        cells_t.append((t, nxt))
        t = nxt
    rows = {}
    flow_sched, prog_sched = {}, {}
    for e in idx.events.values():
        links = e.get("links") or {}
        routine = (e["kind"] == "session" and links.get("schedule")
                   and (e.get("data") or {}).get("kind_label") == "Routine")
        if not routine and (e["kind"] != "run" or e["sub"] not in ("flow", "program", "contacts")):
            continue
        if routine:
            key = "routine:" + links["schedule"][0]
            flow_sched.setdefault(key, set()).add(links["schedule"][0])
        elif e["sub"] == "flow":
            key = "flow:" + (links.get("flow") or [e["title"]])[0]
        else:
            key = "program:" + (links.get("program") or [e["title"]])[0]
        if project and e.get("project") and e["project"] != project:
            continue
        row = rows.setdefault(key, {"id": key, "type": "routine" if routine else
                                    e["sub"] if e["sub"] != "contacts" else "program",
                                    "name": (e.get("data") or {}).get("name") or e["title"],
                                    "project": e.get("project") or "", "first_ts": e["ts"],
                                    "runs": []})
        row["first_ts"] = min(row["first_ts"] or e["ts"], e["ts"])
        row["runs"].append(e)
        for s in links.get("schedule", []):
            flow_sched.setdefault(key, set()).add(s)
        if e["kind"] == "run" and e["sub"] == "program":
            fr = (links.get("flow_run") or [""])[0]
            fe = idx.events.get("run:flow:" + fr) if fr else None
            if fe:
                for s in (fe.get("links") or {}).get("schedule", []):
                    prog_sched.setdefault(key, set()).add(s)
    scheds = {s["id"]: s for s in _schedules()}
    out_rows = []
    for key, row in rows.items():
        sched_ids = flow_sched.get(key, set()) | prog_sched.get(key, set())
        row_scheds = [scheds[s] for s in sched_ids if s in scheds]
        expected = []
        for s in row_scheds:
            since_ok = max(row["first_ts"], s["created"] or 0)
            expected += [x for x in _expected(s, start_ts, min(end_ts, now)) if x >= since_ok]
        cells = []
        for a, b in cells_t:
            runs = [r for r in row["runs"] if a <= r["ts"] < b]
            due = [x for x in expected if a <= x < b]
            if runs:
                st = [r["status"] for r in runs]
                outcome = (H.FAILED if H.FAILED in st else H.RUNNING if H.RUNNING in st
                           else H.WAITING if H.WAITING in st else H.DROPPED
                           if all(x == H.DROPPED for x in st) else H.RAN)
            elif due and b <= now + step:
                # A slot whose time has not come yet is not missed.
                outcome = "missed" if max(due) + 600 < now else "none"
            else:
                outcome = "none"
            counts = {}
            for r in runs:
                for k, v in ((r.get("data") or {}).get("counts") or {}).items():
                    if isinstance(v, (int, float)):
                        counts[k] = counts.get(k, 0) + v
            cells.append({"start": H.iso(a), "end": H.iso(b), "outcome": outcome,
                          "tone": {H.FAILED: "fail", H.RUNNING: "live", H.WAITING: "wait",
                                   H.RAN: "ok", H.DROPPED: "dropped", "missed": "missed"}.get(outcome, "empty"),
                          "runs": [{"id": r["id"], "status": r["status"], "at": r.get("at"),
                                    "title": r["title"]} for r in sorted(runs, key=lambda r: r["ts"])],
                          "expected": len(due), "counts": counts})
        in_span = [r for r in row["runs"] if start_ts <= r["ts"] < end_ts]
        out_rows.append({
            "id": key, "type": row["type"], "name": H.clean_title(row["name"]) or key,
            "project": row["project"],
            "schedules": [{"id": s["id"], "name": s["name"], "every_hours": s["every_hours"],
                           "daily_at": s["daily_at"], "weekday": s["weekday"]} for s in row_scheds],
            "known_since": H.iso(row["first_ts"]), "runs": len(in_span),
            "failed": sum(1 for r in in_span if r["status"] == H.FAILED),
            "missed": sum(1 for c in cells if c["outcome"] == "missed"),
            "last": max((r.get("at") for r in row["runs"]), default=""),
            "cells": cells})
    out_rows.sort(key=lambda r: (-r["runs"], r["name"]))
    return {"span": span, "start": H.iso(start_ts), "end": H.iso(end_ts),
            "columns": [{"start": H.iso(a), "end": H.iso(b),
                         "label": (datetime.fromtimestamp(a).strftime("%a ") + str(datetime.fromtimestamp(a).day))
                         if span == "day" else datetime.fromtimestamp(a).strftime("%H:00")}
                        for a, b in cells_t],
            "rows": out_rows, "revision": H.revision()}


# ---------------------------------------------------------------- the time map

def timemap(since=None, until=None, lane="project", filters=None):
    """Lanes per project or per module for a span: sessions as spans,
    merges as points, automation runs as ticks."""
    if lane not in ("project", "module"):
        raise ValueError("lane is project or module")
    idx = index()
    now = time.time()
    end_ts = _day_bounds(until, end=True) or now
    start_ts = _day_bounds(since) or end_ts - 7 * 86400
    f = filters or parse_filters()
    lanes = {}

    def lanes_of(e):
        if lane == "project":
            return [e.get("project") or "No folder"]
        mods = list((e.get("modules") or {}).keys()) or ([e["module"]] if e.get("module") else [])
        return mods or ["No module"]

    def add(name, bucket, item):
        ln = lanes.setdefault(name, {"key": name, "label": name, "spans": [], "points": [],
                                     "ticks": []})
        ln[bucket].append(item)

    for e in idx.events.values():
        hi = max(e["ts"], e["end_ts"])
        if not e["ts"] or hi < start_ts or e["ts"] >= end_ts:
            continue
        if not _event_passes(e, dict(f, since=None, until=None)):
            continue
        base = {"id": e["id"], "title": e["title"], "status": e["status"],
                "trail": idx.trail_of.get(e["id"])}
        if e["kind"] == "session":
            item = dict(base, start=e.get("at"), end=e.get("end") or e.get("at"),
                        app=(e.get("data") or {}).get("app") or e["sub"],
                        active_s=(e.get("data") or {}).get("active_s") or 0)
            for ln in lanes_of(e):
                add(ln, "spans", item)
        elif e["kind"] == "code" and e["sub"] == "merged":
            diff = (e.get("data") or {}).get("diff") or {}
            item = dict(base, at=e.get("at"), prs=(e.get("links") or {}).get("pr", []),
                        added=diff.get("added", 0), removed=diff.get("removed", 0))
            for ln in lanes_of(e):
                add(ln, "points", item)
        elif e["kind"] == "run" and e["sub"] in ("flow", "program", "contacts"):
            item = dict(base, at=e.get("at"), end=e.get("end") or e.get("at"), sub=e["sub"])
            for ln in lanes_of(e) if lane == "project" else ["Automations"]:
                add(ln, "ticks", item)
    out = sorted(lanes.values(), key=lambda ln: (-(len(ln["spans"]) + len(ln["points"])),
                                                 ln["key"]))
    for ln in out:
        for k in ("spans", "points", "ticks"):
            ln[k].sort(key=lambda x: H.to_ts(x.get("start") or x.get("at")))
    return {"lane": lane, "start": H.iso(start_ts), "end": H.iso(end_ts), "lanes": out,
            "revision": H.revision()}


# ---------------------------------------------------------------- facets

def facets(unstarted=False):
    """What the filters can be set to: projects, modules, kinds, apps, and
    the status words, each with how many trails settled on it. Ideas no
    session has started count as 0 unless `unstarted`; `unstarted_ideas`
    says how many there are either way."""
    idx = index()
    by_status = {}
    for t in idx.trails.values():
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    n_unstarted = sum(by_status.get(w, 0) for w in H.UNSTARTED)
    if not unstarted:
        for w in H.UNSTARTED:
            by_status[w] = 0
    projects, modules, kinds, apps = {}, {}, {}, {}
    for e in idx.events.values():
        if e.get("project"):
            projects[e["project"]] = projects.get(e["project"], 0) + 1
        for m in (e.get("modules") or {}):
            modules[m] = modules.get(m, 0) + 1
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        a = (e.get("data") or {}).get("app")
        if a:
            apps[a] = apps.get(a, 0) + 1
    srt = lambda d: [{"key": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])]
    first = min((e["ts"] for e in idx.events.values() if e["ts"]), default=0)
    return {"projects": srt(projects), "modules": srt(modules), "kinds": [
        {"key": k, "label": H.KIND_LABELS[k], "count": kinds.get(k, 0)} for k in H.KINDS],
        "apps": srt(apps), "statuses": [{"key": s, "means": H.STATUS_MEANS[s], "trails": by_status.get(s, 0),
                      "hidden_by_default": s in H.UNSTARTED} for s in H.STATUSES],
        "unstarted_ideas": n_unstarted,
        "groups": list(GROUPS), "events": len(idx.events), "trails": len(idx.trails),
        "since": H.iso(first), "revision": H.revision()}

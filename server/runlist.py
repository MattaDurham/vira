"""Everything that runs on this Mac, as one list: The Forge's left column.

The owner's rule (2026-10-29): Vira is the window into everything happening
on the computer, so every Flow, every routine Vira runs for itself, every
launchd job, every Claude scheduled task file and every session that is
working right now is one row in one list. The list filters by state and
groups by any column; nothing in it is read-only. Each row says which
engine runs it ("who"), and `ref` names what the editor opens.

Row fields, the same for every kind:
  id         kind + ":" + the thing's own id (unique in the list)
  kind       flow | routine | launchd | task | session
  who        Vira | launchd | Claude | Codex
  name, line what it is called, and what it does in one line
  state      running | scheduled | paused | manual | archived
  cadence    when it runs, in words; cadence_kind buckets it for grouping:
             hourly | hours | daily | weekly | login | kept | event | demand
             | now
  last_run, last_status   the newest run this source records, if any
  folder     the folder it runs in, when the source says
  origin     starter | mine | vira | mac | claude | session (the "Starters
             and mine" grouping)
  note       a consequence worth knowing before pausing or archiving it
  ref        what the editor needs to open it

A source that cannot be read becomes a named status in `sources`, never an
exception: the rest of the list still loads.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import circuits, claudetasks, flows, launchdjobs, routinerun, routines

# What stops being true when one of Vira's own routines is paused or
# archived: shown on its row and in the dialog before Pause or Archive.
CONSEQUENCE = {
    "muse": "Paused, no new idea proposals arrive in the Queue each morning.",
    "intro-scout": "Paused, the Radar window's groupings stop refreshing.",
    "atlas-refresh": "Paused, the Contact Atlas graph stops being rebuilt.",
    "pivot-scout": "Paused, the reconnect targets stop being re-ranked.",
    "room-scout": "Paused, the reading rooms stop gaining new items.",
    "lesson-recurrence": "Paused, the counts of rules broken again stop updating.",
    "system-map": "Paused, the system map stops following the change log.",
    "room-ingest": "Paused, reading-room material stops being staged into the vault.",
    "orphan-sweep": "Paused, the list of unlanded work in Runs stops refreshing.",
}
VIRA_SERVICE = "nyc.durham.vira"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hours_kind(hours):
    try:
        h = float(hours or 0)
    except (TypeError, ValueError):
        return "demand"
    if h <= 0:
        return "demand"
    if h <= 1:
        return "hourly"
    if h < 24:
        return "hours"
    if h < 168:
        return "daily"
    return "weekly"


def cadence_of(row):
    """A routine row's cadence in words, and its bucket."""
    if row.get("daily_at"):
        if row.get("weekday"):
            return f"Every {str(row['weekday']).capitalize()} at {row['daily_at']}", "weekly"
        return f"Every day at {row['daily_at']}", "daily"
    hours = row.get("every_hours")
    try:
        h = float(hours or 0)
    except (TypeError, ValueError):
        h = 0
    if not h:
        return "On demand", "demand"
    if h == 1:
        return "Every hour", "hourly"
    if h == 24:
        return "Every day", "daily"
    if h == 168:
        return "Every week", "weekly"
    shown = int(h) if h == int(h) else h
    return f"Every {shown} hours", _hours_kind(h)


_KIND_RANK = ["now", "hourly", "hours", "daily", "weekly", "login", "kept", "event", "demand"]


def _join_cadence(parts):
    """Several schedules on one board, read as one line; the bucket is the
    most frequent of them."""
    if not parts:
        return "On demand", "demand"
    words = [text for text, _ in parts]
    kinds = sorted((k for _, k in parts), key=_KIND_RANK.index)
    return " and ".join(dict.fromkeys(words)), kinds[0]


def _flow_rows(sources):
    try:
        all_flows = flows.list_flows()
        runs = circuits.list_runs(200)
    except Exception as e:  # noqa: BLE001 - the list still loads without it
        sources["flows"] = {"status": "error", "note": str(e)}
        return []
    newest = {}
    live = set()
    for run in runs:            # newest first
        cid = run.get("circuit_id")
        newest.setdefault(cid, run)
        if run.get("status") == "running":
            live.add(cid)
    out = []
    for flow in all_flows:
        triggers = flow.get("triggers") or []
        parts = [cadence_of(t) for t in triggers]
        cadence, ckind = _join_cadence(parts)
        if flow.get("archived"):
            state = "archived"
        elif flow["id"] in live:
            state = "running"
        elif any(t.get("enabled") for t in triggers):
            state = "scheduled"
        elif triggers:
            state = "paused"
        else:
            state = "manual"
        last = newest.get(flow["id"]) or {}
        folder = next((t.get("cwd") for t in triggers if t.get("cwd")), "")
        out.append({
            "id": f"flow:{flow['id']}", "kind": "flow", "who": "Vira",
            "name": flow.get("name") or flow["id"],
            "line": flow.get("description") or f"{len(flow.get('nodes') or [])} cards",
            "cards": len(flow.get("nodes") or []),
            "state": state, "cadence": cadence, "cadence_kind": ckind,
            "paused_schedules": sum(1 for t in triggers if not t.get("enabled")),
            "last_run": last.get("started"), "last_status": last.get("status"),
            "folder": folder or "",
            "origin": "starter" if flow.get("builtin") else "mine",
            "note": "", "archived": flow.get("archived"),
            "ref": {"flow_id": flow["id"]},
        })
    sources["flows"] = {"status": "ok", "count": len(out)}
    return out


def _routine_rows(sources):
    try:
        rows = routines.list_routines()
    except Exception as e:  # noqa: BLE001
        sources["routines"] = {"status": "error", "note": str(e)}
        return []
    seeded = {seed["id"] for seed in routines.SEEDS} | set(CONSEQUENCE)
    out = []
    for row in rows:
        if row.get("kind") == "circuit":
            continue            # a Schedule card on a Flow's board
        cadence, ckind = cadence_of(row)
        status = str(row.get("last_status") or "")
        live = status in ("running", "starting")
        if row.get("kind") == "command":
            try:
                live = routinerun.live(row["id"])
            except Exception:  # noqa: BLE001 - a lock probe never fails the list
                pass
        if row.get("archived"):
            state = "archived"
        elif live:
            state = "running"
        elif row.get("enabled"):
            state = "scheduled"
        else:
            state = "paused"
        out.append({
            "id": f"routine:{row['id']}", "kind": "routine", "who": "Vira",
            "name": row.get("name") or row["id"],
            "line": " ".join(str(row.get("description") or "").split()),
            "state": state, "cadence": cadence, "cadence_kind": ckind,
            "last_run": row.get("last_run"), "last_status": row.get("last_status"),
            "folder": row.get("cwd") or "",
            "origin": "vira" if row["id"] in seeded else "mine",
            "note": CONSEQUENCE.get(row["id"], ""), "archived": row.get("archived"),
            "engine": row.get("kind"),
            "ref": {"routine_id": row["id"]},
        })
    sources["routines"] = {"status": "ok", "count": len(out)}
    return out


def _launchd_rows(sources):
    try:
        inv = launchdjobs.inventory()
    except Exception as e:  # noqa: BLE001
        sources["launchd"] = {"status": "error", "note": str(e)}
        return []
    if inv.get("status") != "ok":
        sources["launchd"] = {"status": inv.get("status"), "note": inv.get("note") or ""}
        return []
    out = []
    jobs = [j for g in inv.get("groups") or [] for j in g.get("jobs") or []]
    jobs += inv.get("archived") or []
    for job in jobs:
        tone = job.get("tone") or ""
        if job.get("archived"):
            state = "archived"
        elif tone == "running":
            state = "running"
        elif tone == "loaded":
            state = "scheduled" if job.get("cadence_kind") != "demand" else "manual"
        else:
            state = "paused"
        note = ""
        if job.get("label") == VIRA_SERVICE:
            note = ("This is Vira's own server. Pausing or archiving it stops Vira "
                    "itself, and this window with it.")
        elif job.get("admin"):
            note = "macOS asks for your administrator password before it changes."
        out.append({
            "id": f"launchd:{job['path']}", "kind": "launchd", "who": "launchd",
            "name": job.get("label") or job.get("file"),
            "line": job.get("program") or job.get("error") or "",
            "state": state, "cadence": "; ".join(job.get("when") or []) or "On demand",
            "cadence_kind": job.get("cadence_kind") or "demand",
            "status_line": job.get("state") or "",
            "last_run": None, "last_status": job.get("state") or None,
            "folder": job.get("cwd") or "",
            "origin": "mac", "note": note, "archived": job.get("archived"),
            "ref": {"path": job["path"], "label": job.get("label")},
        })
    sources["launchd"] = {"status": "ok", "count": len(out)}
    return out


def _task_rows(sources):
    try:
        tasks = claudetasks.list_tasks()
    except Exception as e:  # noqa: BLE001
        sources["tasks"] = {"status": "error", "note": str(e)}
        return []
    out = []
    for task in tasks:
        out.append({
            "id": f"task:{task['name']}", "kind": "task", "who": "Claude",
            "name": task.get("title") or task["name"],
            "line": task.get("description") or "",
            "state": "archived" if task.get("archived") else "manual",
            "cadence": "Not scheduled by Vira", "cadence_kind": "demand",
            "last_run": None, "last_status": None,
            "folder": "", "origin": "claude",
            "note": ("The Claude app keeps its own schedule for this file, and Vira "
                     "cannot read it. A schedule set here runs it from Vira instead."),
            "archived": task.get("archived"),
            "ref": {"task": task["name"]},
        })
    sources["tasks"] = {"status": "ok", "count": len(out)}
    return out


def _fleet_live():
    """The Fleet window's live picture (the one seam tests patch)."""
    from . import fleet
    return fleet.FLEET.live()


def _session_rows(sources, flow_jobs):
    try:
        live = _fleet_live()
    except Exception as e:  # noqa: BLE001
        sources["sessions"] = {"status": "error", "note": str(e)}
        return []
    out = []
    for s in live.get("sessions") or []:
        if s.get("status") == "Idle":
            continue            # open, but doing nothing
        if s.get("viraJob") and s["viraJob"] in flow_jobs:
            continue            # a card of a Flow run: the Flow's row shows it
        started = s.get("turnStartedAt") or s.get("startedAt")
        when = (datetime.fromtimestamp(started / 1000, timezone.utc).isoformat(timespec="seconds")
                if isinstance(started, (int, float)) and started else None)
        tool = {"claude": "Claude", "codex": "Codex", "vira": "Vira"}.get(
            str(s.get("tool") or "").lower(), str(s.get("tool") or "Session"))
        out.append({
            "id": f"session:{s['id']}", "kind": "session", "who": tool,
            "name": s.get("title") or s.get("asked") or s["id"],
            "line": " ".join(str(s.get("lastText") or s.get("asked") or "").split())[:300],
            "state": "running", "cadence": s.get("status") or "Working",
            "cadence_kind": "now", "last_run": when, "last_status": s.get("status"),
            "folder": s.get("folder") or "", "origin": "session",
            "note": "", "archived": None,
            "ref": {"session": s["id"], "open_url": s.get("openUrl"),
                    "job": s.get("viraJob")},
        })
    sources["sessions"] = {"status": "ok", "count": len(out)}
    return out


def _flow_jobs():
    """The session ids running as a card of a live Flow run."""
    jobs = set()
    try:
        for run in circuits.list_runs(200):
            if run.get("status") != "running":
                continue
            for st in (run.get("stages") or {}).values():
                if st.get("job_id"):
                    jobs.add(st["job_id"])
    except Exception:  # noqa: BLE001
        pass
    return jobs


def everything():
    """Every row, and how each source read."""
    sources = {}
    rows = []
    rows += _flow_rows(sources)
    rows += _routine_rows(sources)
    rows += _launchd_rows(sources)
    rows += _task_rows(sources)
    rows += _session_rows(sources, _flow_jobs())
    return {"rows": rows, "sources": sources, "checked": _now()}

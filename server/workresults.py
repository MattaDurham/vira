"""One read-only inventory for Work's timeline, gallery and detail inspector.

Branches, jobs and flow stages describe the same work from different sources.
They join by recorded identifiers, never by a guessed title or nearby timestamp.
Source stores still own every action; this module neither dispatches nor writes.
An absent/invalid date remains unknown, and a broken source is reported alongside
the sources which could be read. Correspondence receipts can join via the small
provider seam without becoming another queue or a copy of the canonical vault.
"""
from __future__ import annotations

from . import instance

import hashlib
import logging
import math
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from . import changelog, circuits, joblog, orphanwork, settings, showroom

log = logging.getLogger(__name__)
router = APIRouter()
receipt_provider: Callable[[], list[dict]] = lambda: []


def timestamp(value):
    """A real stored time, or zero. Never manufacture recency at render time."""
    try:
        if isinstance(value, (int, float)):
            return float(value) if value > 0 and math.isfinite(value) else 0.0
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _date(ts):
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""
    except (ValueError, OverflowError, OSError):
        return ""


def _touch(row, value):
    ts = timestamp(value)
    if ts > row["updated_ts"]:
        row["updated_ts"], row["updated_at"] = ts, _date(ts)


def _new(identity, kind, title, when=None):
    row = {"id": identity, "kind": kind, "title": title or "Untitled work",
           "summary": "", "status": "unknown", "module": "other",
           "updated_ts": 0.0, "updated_at": "", "aliases": [identity],
           "branch": "", "job_ids": [], "session_ids": [], "flow_ids": [],
           "preview_url": "", "can_preview": False, "sources": []}
    _touch(row, when)
    return row


def _append(row, field, value):
    if value and value not in row[field]:
        row[field].append(value)


def _job_title(job):
    return job.get("title") or job.get("command") or (job.get("prompt") or "")[:140] or "Session"


def build_inventory(showroom_data=None, orphans=(), jobs=(), flows=(),
                    groups=(), live=(), receipts=()):
    """Pure identity join, shared by HTTP and isolated synthetic tests.

    A branch is the stable artifact identity even as its orphan re-arm key or
    latest job changes. An unbranched flow owns its stage jobs. A multi-branch
    flow stays its own result and links its distinct branch artifacts.
    """
    rows, by_job, by_flow = {}, {}, {}
    jobs_by_id = {r["id"]: dict(r) for r in jobs if r.get("id")}
    for j in live:
        if j.get("id"):
            jobs_by_id[j["id"]] = {**jobs_by_id.get(j["id"], {}), **j}

    def branch_row(branch, title=""):
        identity = "branch:" + branch
        if identity not in rows:
            rows[identity] = _new(identity, "branch", title or showroom.humanize(branch.split("/", 1)[-1]))
            rows[identity]["branch"] = branch
        return rows[identity]

    for b in (showroom_data or {}).get("items", []):
        branch = b.get("branch")
        if not branch:
            continue
        row = branch_row(branch, b.get("title"))
        row.update(summary=b.get("blurb") or "", status=b.get("band") or "unknown",
                   module=b.get("module") or b.get("module_guess") or "other",
                   can_preview=branch.startswith(("claude/", "codex/")))
        row["branch_info"] = {k: b.get(k) for k in (
            "band", "worktree", "tip", "ahead", "behind", "dirty", "instance",
            "serving", "action", "failure", "pr", "merged_at", "merged_sha",
            "orphan_key", "orphan_read", "areas", "asked", "blurb_source")}
        _touch(row, b.get("last_activity"))
        _touch(row, b.get("merged_at"))
        _append(row, "sources", "branch inventory")
        jid = (b.get("job") or {}).get("id")
        if jid:
            by_job[jid] = row
            _append(row, "job_ids", jid)
            _append(row, "aliases", "job:" + jid)
        if b.get("visual") and b.get("orphan_key"):
            row["preview_url"] = ("/api/orphanwork/visual?key=" + quote(b["orphan_key"], safe="")
                                  + "&path=" + quote(b["visual"], safe=""))

    for orphan in orphans:
        branch = orphan.get("branch")
        if not branch:
            continue
        row = branch_row(branch, orphan.get("subject"))
        # The sweeper owns current review eligibility; cached gallery facts
        # alone never resurrect a dismissed review key.
        row["orphan"] = orphan
        if orphan.get("kind") != "unpushed":
            row["status"] = "unlanded"
        row["summary"] = row["summary"] or (orphan.get("read") or {}).get("why", "")
        _touch(row, orphan.get("last_activity") or orphan.get("last_activity_iso"))
        _append(row, "sources", "branch review")
        _append(row, "aliases", "orphan:" + (orphan.get("key") or branch))
        jid = (orphan.get("job") or {}).get("id")
        if jid:
            by_job[jid] = row
            _append(row, "job_ids", jid)
            _append(row, "aliases", "job:" + jid)
        row["can_preview"] = branch.startswith(("claude/", "codex/"))

    # Bind all recorded branch jobs before flow ownership is decided. Stage
    # jobs need not be the latest job on a branch to join the right artifact.
    for jid, job in jobs_by_id.items():
        if job.get("branch"):
            by_job[jid] = branch_row(job["branch"], _job_title(job))

    for flow in flows:
        fid = flow.get("id")
        if not fid:
            continue
        stage_ids = [s.get("job_id") for s in (flow.get("stages") or {}).values() if s.get("job_id")]
        branch_ids = {by_job[j]["id"] for j in stage_ids if j in by_job}
        if len(branch_ids) == 1:
            row = rows[next(iter(branch_ids))]
        else:
            identity = "flow:" + fid
            row = rows.setdefault(identity, _new(identity, "flow", flow.get("circuit_name") or "Flow"))
            row["related_ids"] = sorted(branch_ids)
        by_flow[fid] = row
        _append(row, "flow_ids", fid)
        _append(row, "aliases", "flow:" + fid)
        _append(row, "sources", "flow")
        _touch(row, flow.get("finished") or flow.get("started"))
        if row["kind"] == "flow":
            row["status"] = flow.get("status") or "unknown"
            row["summary"] = flow.get("error") or (flow.get("input") or "")[:320]
        for jid in stage_ids:
            by_job.setdefault(jid, row)
            _append(by_job[jid], "job_ids", jid)
            _append(by_job[jid], "aliases", "job:" + jid)

    for jid, job in jobs_by_id.items():
        row = by_job.get(jid)
        if row is None:
            fid = (job.get("meta") or {}).get("circuit_run")
            row = by_flow.get(fid)
        if row is None:
            identity = "job:" + jid
            row = rows.setdefault(identity, _new(identity, "job", _job_title(job)))
        by_job[jid] = row
        _append(row, "job_ids", jid)
        _append(row, "aliases", "job:" + jid)
        _append(row, "session_ids", job.get("session_id"))
        if job.get("session_id"):
            _append(row, "aliases", "session:" + job["session_id"])
        _append(row, "sources", "session")
        _touch(row, job.get("finished") or job.get("started"))
        if row["kind"] == "job":
            row["status"] = job.get("awaiting") or job.get("status") or "unknown"
            row["summary"] = (job.get("result") or "")[:320]
        elif row["status"] == "unknown":
            row["status"] = job.get("status") or "unknown"
        if job.get("status") == "running":
            row["status"] = "waiting" if job.get("awaiting") else "running"

    for group in groups:
        for entry in group.get("entries", []):
            jid = entry.get("job_id")
            if jid and jid in by_job:
                row = by_job[jid]
                _append(row, "sources", "record")
                if entry.get("retro"):
                    row.setdefault("retros", [])
                    _append(row, "retros", entry["retro"])
                continue
            if entry.get("kind") == "job" and not jid:
                continue
            # Source identity intentionally excludes time and title corrections
            # where a durable idea/job id exists. A retro bullet has no id, so
            # its source stem and original text form an explicit content key.
            identity = ("job:" + jid if jid else "idea:" + entry["idea_id"]
                        if entry.get("idea_id") else "change:" + hashlib.sha256(
                            (str(entry.get("retro") or group.get("date") or "") + "\n"
                             + str(entry.get("text") or "")).encode("utf-8")).hexdigest()[:20])
            if identity in rows:
                continue
            row = _new(identity, "change", entry.get("text"), entry.get("ts"))
            row["status"] = entry.get("kind") or "recorded"
            row["summary"] = group.get("goal") or ""
            row["record"] = entry
            row["sources"] = ["record"]
            rows[identity] = row

    for receipt in receipts:
        rid = receipt.get("id") or receipt.get("key")
        if not rid:
            continue
        identity = "receipt:" + str(rid)
        row = _new(identity, "receipt", receipt.get("title") or "Correspondence filing",
                   receipt.get("updated_at") or receipt.get("updated") or receipt.get("created"))
        row.update(summary=receipt.get("summary") or receipt.get("reason") or "",
                   status=receipt.get("status") or "recorded", module="correspondence",
                   receipt=receipt, sources=["correspondence"])
        rows[identity] = row

    for row in rows.values():
        if row["branch"] and row["branch"] != "main" and not row["branch"].startswith("claude/"):
            row["action_limit"] = ("Merge, land, discard and cleanup are unavailable for this branch prefix. "
                                   "The branch tooling currently targets claude/* only. Read context or resume instead.")
    return sorted(rows.values(), key=lambda r: (-r["updated_ts"], r["id"]))


def _live_jobs():
    from .session import sessions
    return sessions.recent()


def _fixture():
    """Explicit sample work, with no access to the owner's other stores."""
    items = build_inventory(jobs=[
        {"id": "example-weekly-review", "title": "Weekly review", "status": "done",
         "started": "2026-01-06T09:00:00Z", "finished": "2026-01-06T09:08:00Z",
         "result": "Example result: a weekly summary with completed work, open questions and next steps."},
        {"id": "example-project-brief", "title": "Project brief", "status": "done",
         "started": "2026-01-05T14:00:00Z", "finished": "2026-01-05T14:12:00Z",
         "result": "Example result: a draft project brief ready to read and revise."}], receipts=[
        {"id": "example-household-budget", "title": "Household budget", "status": "saved",
         "updated_at": "2026-01-06T10:00:00Z", "vault_id": "personal",
         "destination": "Personal / Finances", "path": "raw/finances/example-budget.md",
         "summary": "Example filing receipt: a budget draft was saved with its source email."}])
    for item in items:
        item["fixture"] = True
        item["sources"] = ["example"]
    return {"items": items, "counts": {"job": 2, "receipt": 1}, "total": len(items),
            "errors": {}, "fixture": True, "instance": instance.metadata(),
            "last_sweep": None, "flow_limit": None}


def compose():
    if settings.fixture_mode():
        return _fixture()
    errors = {}

    def read(name, fn, default):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one failed source must not hide the rest
            log.warning("Work results source %s failed: %s", name, exc)
            errors[name] = str(exc)[:240]
            return default

    gallery = read("branches", showroom.compose, {})
    orphan_data = read("branch review", orphanwork.compose, {})
    jobs = read("sessions", joblog.list_records, [])
    flows = read("flows", lambda: circuits.list_runs(limit=200), [])
    groups = read("record", changelog.groups, [])
    live = read("live sessions", _live_jobs, [])
    receipts = read("correspondence", receipt_provider, [])
    items = build_inventory(gallery, orphan_data.get("items", []), jobs, flows,
                            groups, live, receipts)
    counts = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {"items": items, "counts": counts, "total": len(items), "errors": errors,
            "instance": instance.metadata(),
            "last_sweep": gallery.get("last_sweep"),
            "flow_limit": 200 if len(flows) == 200 else None}


def detail(identity):
    data = compose()
    item = next((r for r in data["items"] if identity in r["aliases"]), None)
    if item is None:
        raise KeyError(identity)
    out = {"item": item, "errors": dict(data["errors"]), "jobs": [], "flows": []}
    if data.get("fixture"):
        return out

    def read(name, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - preserve the readable evidence
            log.warning("Work result detail %s failed: %s", name, exc)
            out["errors"][name] = str(exc)[:240]
            return None

    for jid in item["job_ids"]:
        rec = read("session " + jid, lambda: joblog.get_record(jid))
        if rec:
            out["jobs"].append({k: rec.get(k) for k in (
                "id", "title", "prompt", "result", "status", "started", "finished",
                "session_id", "transcript", "branch", "judge", "model", "provider")})
    for fid in item["flow_ids"]:
        flow = read("flow " + fid, lambda: circuits.get_run(fid))
        if flow:
            out["flows"].append(flow)
    if item.get("branch_info"):
        out["branch_context"] = read("branch context", lambda: showroom.context(item["branch"]))
    elif item.get("orphan"):
        out["branch_context"] = {"orphan": read("branch context", lambda: orphanwork.context(item["orphan"]))}
    return out


@router.get("/api/work/results")
def api_results():
    return compose()


@router.get("/api/work/results/detail")
def api_result_detail(id: str):
    try:
        return detail(id)
    except KeyError:
        raise HTTPException(404, "This work is no longer in the inventory") from None

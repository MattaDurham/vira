"""One read-only inventory for Work's timeline, gallery and detail inspector.

Branches, jobs and flow stages describe the same work from different sources.
They join by recorded identifiers, never by a guessed title or nearby timestamp.
Source stores still own every action; this module neither dispatches nor writes.
An absent/invalid date remains unknown, and a broken source is reported alongside
the sources which could be read. Correspondence receipts can join via the small
provider seam without becoming another queue or a copy of the canonical vault.

Every row also carries a PHASE (owner, 2026-10-11): "attention" when it waits
on the owner, "active" while someone is working on it, "finished" once it has
reached an end state. Results lists finished work only; Attention is where
anything that needs a decision lives, so a piece of work is never in both.
A finished row's status names where the work ended up (shipped, discarded,
answered), never just that a session stopped: an Implement session that left
its code uncommitted on a branch once read "Complete" beside an Attention row
saying the same branch was unmerged.
"""
from __future__ import annotations

from . import instance

import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from . import changelog, circuits, joblog, orphanwork, prindex, settings, showroom

log = logging.getLogger(__name__)
router = APIRouter()
receipt_provider: Callable[[], list[dict]] = lambda: []

# Job kinds (joblog.kind_label) whose output is code on a branch. Their end
# state is the branch's - merged or not - never the session's own "done".
CODE_KINDS = frozenset(("Implement", "Plan", "Resume", "Land", "Diagnose and land"))
# What a finished non-code session produced, by kind; anything unnamed here
# reads "finished".
_DONE_BY_KIND = {"Chat": "answered", "Ask": "answered", "Routine": "ran"}
# Tools whose file_path says which worktree a session actually wrote in.
_WRITE_TOOLS = frozenset(("Edit", "Write", "MultiEdit", "NotebookEdit"))
# A server-made worktree's home (scripts/branch.sh WT_HOME): the branch is
# claude/<slug> even after the worktree itself has been torn down.
_WT_SLUG = re.compile(r"/\.worktrees/([A-Za-z0-9._-]+)(?:/|$)")
_written_cache: dict = {}


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


def _kind(job):
    try:
        return joblog.kind_label(job)
    except Exception:  # noqa: BLE001 - a label must never hide a row
        return "Session"


def _is_code(job):
    return _kind(job) in CODE_KINDS and not job.get("read_only")


def _branch_of_path(path, worktrees):
    """The branch a file path belongs to: a live worktree's branch, else the
    branch.sh layout for a torn-down one. "" when neither says."""
    # Windows paths arrive with backslashes from a transcript and forward
    # slashes from git; compare both in one form.
    path = str(path).replace("\\", "/")
    best, best_branch = "", ""
    for wt, branch in worktrees.items():
        wt = str(wt).replace("\\", "/").rstrip("/")
        if branch and (path == wt or path.startswith(wt + "/")) \
                and len(wt) > len(best):
            best, best_branch = wt, branch
    if best:
        return best_branch
    m = _WT_SLUG.search(path)
    return "claude/" + m.group(1) if m else ""


def _transcript_writes(path):
    """Every file path a Claude transcript's write tools touched, in order.
    Cached by size and mtime: a finished session's transcript never changes,
    and Results re-reads its inventory every few seconds."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _written_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    paths = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    content = (json.loads(line).get("message") or {}).get("content")
                except ValueError:
                    continue
                for block in content if isinstance(content, list) else ():
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") in _WRITE_TOOLS):
                        fp = (block.get("input") or {}).get("file_path")
                        if fp:
                            paths.append(fp)
    except OSError:
        return []
    _written_cache[str(path)] = (key, paths)
    return paths


def written_branch(job, worktrees):
    """The branch an UNPLACED coding session wrote its code on, from the
    file paths its own write tools recorded - the branch with the most
    writes. Sessions Vira placed carry `branch` and never need this; it
    exists for the ones that launched outside a repo (a stale default
    folder, 2026-10-10) and made their own worktree, which left Results
    showing the session and its branch as two unrelated pieces of work.
    Reading a neighbour's worktree is not writing to it, so it never
    counts."""
    counts = {}
    for fp in _transcript_writes(job.get("transcript") or ""):
        branch = _branch_of_path(fp, worktrees)
        if branch and branch != "main":
            counts[branch] = counts.get(branch, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _written_branches(jobs):
    """{job id: branch} for finished, unplaced coding sessions."""
    todo = [j for j in jobs if j.get("id") and not j.get("branch")
            and j.get("status") != "running" and j.get("transcript") and _is_code(j)]
    if not todo:
        return {}
    worktrees = {str(e["path"]): e["branch"] for e in orphanwork._porcelain_worktrees()}
    out = {}
    for job in todo:
        branch = written_branch(job, worktrees)
        if branch:
            out[job["id"]] = branch
    return out


def _settle(row, jobs_by_id, prs):
    """Set the row's phase, and for finished work the status that names its
    end state. Order matters: live work first, then the sweeper's verdict,
    then what the branch inventory, PR index and ledger recorded."""
    jobs = [jobs_by_id[j] for j in row["job_ids"] if j in jobs_by_id]
    live = [j for j in jobs if j.get("status") == "running"]
    if live:
        row["phase"] = "attention" if any(j.get("awaiting") for j in live) else "active"
        return
    orphan = row.get("orphan")
    if orphan:
        row["phase"] = "active" if orphan.get("in_motion") else "attention"
        return
    band = (row.get("branch_info") or {}).get("band")
    if band == "session":
        row["phase"], row["status"] = "active", "running"
        return
    if band == "unlanded":
        row["phase"], row["status"] = "attention", "unlanded"
        return
    if row["kind"] == "flow":
        status = row["status"]
        row["phase"] = ("active" if status in ("running", "pending") else
                        "attention" if status == "waiting" else "finished")
        return
    if row["kind"] == "receipt":
        row["phase"] = "active" if row["status"] == "processing" else "finished"
        return
    row["phase"] = "finished"
    if band == "landed":
        row["status"] = "shipped"
        return
    if row["kind"] == "branch":
        pr = prs.get(row["branch"]) or {}
        if pr.get("state") == "MERGED":
            row["status"] = "shipped"
            return
        if pr.get("state") == "CLOSED":
            row["status"] = "discarded"
            return
    if not jobs or row["status"] not in ("done", "unknown"):
        return                        # error, orphaned, a record's own kind
    newest = max(jobs, key=lambda j: timestamp(j.get("finished") or j.get("started")))
    if any(_is_code(j) for j in jobs):
        # Code whose branch is gone with no merged PR on record: what became
        # of it is not known, so say only that it ended.
        row["status"] = "ended"
    else:
        row["status"] = _DONE_BY_KIND.get(_kind(newest), "finished")


def build_inventory(showroom_data=None, orphans=(), jobs=(), flows=(),
                    groups=(), live=(), receipts=(), prs=None, written=None):
    """Pure identity join, shared by HTTP and isolated synthetic tests.

    A branch is the stable artifact identity even as its orphan re-arm key or
    latest job changes. An unbranched flow owns its stage jobs. A multi-branch
    flow stays its own result and links its distinct branch artifacts.
    `written` maps an unplaced coding session to the branch it wrote on
    (written_branch); `prs` is the PR index by branch.
    """
    rows, by_job, by_flow = {}, {}, {}
    prs, written = prs or {}, written or {}
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
            # Work in motion outside Vira is not ready for review yet.
            row["status"] = "running" if orphan.get("in_motion") else "unlanded"
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
        branch = job.get("branch") or written.get(jid)
        if branch:
            by_job[jid] = branch_row(branch, _job_title(job))

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
        _settle(row, jobs_by_id, prs)
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
    return {"items": items, "counts": {"job": 2, "receipt": 1},
            "phases": {"attention": 0, "active": 0, "finished": len(items)}, "total": len(items),
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
    prs = read("pull requests", prindex._read, {})
    written = read("session branches", lambda: _written_branches(jobs), {})
    items = build_inventory(gallery, orphan_data.get("items", []), jobs, flows,
                            groups, live, receipts, prs=prs, written=written)
    counts, phases = {}, {"attention": 0, "active": 0, "finished": 0}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        phases[item["phase"]] = phases.get(item["phase"], 0) + 1
    return {"items": items, "counts": counts, "phases": phases,
            "total": len(items), "errors": errors,
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

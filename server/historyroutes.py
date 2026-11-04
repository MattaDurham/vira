"""The history API: every front end reads history through these routes.

    GET  /api/history            entries for one question (group + filters)
    GET  /api/history/entry      one entry in full: outcome and diff, the
                                 trail, what it touched, cost and time, actions
    GET  /api/history/for        from the thing itself: an idea, a module, a
                                 branch, a PR, a session, a job, a Flow run
    GET  /api/history/grid       the automation status grid
    GET  /api/history/timemap    lanes of sessions, merges and runs over a span
    GET  /api/history/facets     what the filters can be set to
    POST /api/history/ask        a plain-words question -> a view spec (no prose)
    POST /api/history/action     Merge, Discard, Resume, Rerun, Judge
    GET  /api/history/rev        the store's revision (the poll hook)
    GET  /api/history/stream     server-sent events on every change
    POST /api/history/ingest     read the stores now, out of process

The README of modules/fleet/history documents each with an example.

The ingest never runs in the server's process: Ticker spawns
`python -m server.historyingest tick` (a `backfill` when the store is
empty) at most every TICK_S, one at a time, and keeps its last report.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import admission
from . import history as H
from . import historyview as V

router = APIRouter()

TICK_S = 60
# An ingest pass that has not finished in this long is stopped; the next
# tick resumes from the saved state. A first backfill of a busy Mac takes
# about a minute.
INGEST_TIMEOUT_S = 15 * 60
STREAM_POLL_S = 2.0


# ---------------------------------------------------------------- ingest runner

class Ticker:
    """Runs the ingest out of process, every TICK_S, never two at once."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.thread = None
        self.last = {}          # the last pass's report
        self.started_at = 0.0
        self.mode = ""
        self._stop = threading.Event()

    def spawn_args(self, mode):
        return [sys.executable, "-m", "server.historyingest", mode]

    def run_once(self, mode=None):
        """One pass now; returns False when one is already running."""
        with self.lock:
            if self.proc is not None:
                return False
            mode = mode or ("backfill" if not H.signature() else "tick")
            try:
                self.proc = subprocess.Popen(
                    self.spawn_args(mode), cwd=str(H.ROOT), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            except OSError as e:
                self.last = {"error": f"could not start the ingest: {e}", "at": H.now_iso()}
                return False
            self.started_at, self.mode = time.time(), mode
            proc = self.proc
        threading.Thread(target=self._wait, args=(proc,), daemon=True,
                         name="history-ingest-wait").start()
        return True

    def _wait(self, proc):
        try:
            out, err = proc.communicate(timeout=INGEST_TIMEOUT_S)
            try:
                rep = json.loads(out or "{}")
            except ValueError:
                rep = {"error": "the ingest printed no report", "stderr": (err or "")[-800:]}
            if proc.returncode not in (0, 1):
                rep.setdefault("error", f"the ingest exited {proc.returncode}")
                rep["stderr"] = (err or "")[-800:]
        except subprocess.TimeoutExpired:
            proc.kill()
            rep = {"error": f"the ingest ran past {INGEST_TIMEOUT_S} s and was stopped"}
        rep["mode"] = self.mode
        with self.lock:
            self.last, self.proc = rep, None

    def _loop(self):
        while not self._stop.wait(5 if not self.last else TICK_S):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the loop must survive any pass
                pass

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True,
                                           name="history-ingest")
            self.thread.start()

    def status(self):
        with self.lock:
            return {"running": self.proc is not None, "mode": self.mode if self.proc else "",
                    "started_at": H.iso(self.started_at) if self.proc else "",
                    "last": self.last}


ticker = Ticker()


# ---------------------------------------------------------------- reads

def _filters(since, until, project, module, status, kind, q, app, automations):
    for word in (status or "").split(","):
        if word.strip() and word.strip() not in H.STATUSES:
            raise HTTPException(400, f"unknown status word {word.strip()!r}; "
                                f"one of {', '.join(H.STATUSES)}")
    return V.parse_filters(since=since, until=until, project=project, module=module,
                           status=status, kind=kind, q=q, app=app, automations=automations)


@router.get("/api/history")
def api_history(group: str = "day", since: str = "", until: str = "", project: str = "",
                module: str = "", status: str = "", kind: str = "", q: str = "",
                app: str = "", automations: str = "", unit: str = "",
                limit: int = V.LIST_LIMIT, offset: int = 0):
    f = _filters(since, until, project, module, status, kind, q, app, automations)
    if unit and unit not in ("trail", "event"):
        raise HTTPException(400, "unit is trail or event")
    try:
        with admission.cpu("history.list"):
            return V.list_entries(group, f, limit=max(1, min(limit, 1000)),
                                  offset=max(0, offset), unit=unit or None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/history/entry")
def api_history_entry(id: str):
    try:
        with admission.cpu("history.entry"):
            return V.detail(id)
    except KeyError:
        raise HTTPException(404, "no history entry for that id") from None


@router.get("/api/history/for")
def api_history_for(idea: str = "", module: str = "", branch: str = "", pr: str = "",
                    session: str = "", job: str = "", flow_run: str = "", ref: str = "",
                    project: str = "", since: str = "", until: str = ""):
    """History opened from the thing itself. A module or project answers
    with its entries by day; anything that names one piece of work (an idea,
    a branch, a PR, a session, a job, a Flow run, or `ref`=<kind>:<value>)
    answers with that work's entry in full."""
    if module or (project and not any((idea, branch, pr, session, job, flow_run, ref))):
        f = _filters(since, until, project, module, "", "", "", "", "")
        with admission.cpu("history.for"):
            return {"for": {"module": module, "project": project},
                    "list": V.list_entries("day", f)}
    key = (ref or (f"idea:{idea}" if idea else f"branch:{branch}" if branch
                   else f"pr:{pr}" if pr else f"session:{session}" if session
                   else f"job:{job}" if job else f"flow_run:{flow_run}" if flow_run else ""))
    if not key:
        raise HTTPException(400, "name an idea, module, project, branch, pr, session, job, "
                                 "flow_run or ref")
    try:
        with admission.cpu("history.for"):
            return dict(V.detail(key), **{"for": key})
    except KeyError:
        raise HTTPException(404, f"no history for {key}") from None


@router.get("/api/history/grid")
def api_history_grid(span: str = "day", since: str = "", until: str = "", project: str = ""):
    try:
        with admission.cpu("history.grid"):
            return V.grid(span=span, since=since, until=until, project=project)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/history/timemap")
def api_history_timemap(since: str = "", until: str = "", lane: str = "project",
                        project: str = "", module: str = "", kind: str = "", app: str = ""):
    f = _filters("", "", project, module, "", kind, "", app, "")
    try:
        with admission.cpu("history.timemap"):
            return V.timemap(since=since, until=until, lane=lane, filters=f)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/history/facets")
def api_history_facets():
    with admission.cpu("history.facets"):
        return V.facets()


@router.get("/api/history/rev")
def api_history_rev():
    return {"revision": H.revision(), "events": len(H.load()), "ingest": ticker.status()}


@router.get("/api/history/stream")
async def api_history_stream(request: Request):
    """`event: rev` with {"revision": ...} on connect and on every change
    to the store; a keepalive comment every 15 s. Clients re-read what they
    show when the revision moves."""
    async def gen():
        last, quiet = None, 0.0
        while True:
            if await request.is_disconnected():
                return
            rev = await asyncio.to_thread(H.revision)
            if rev != last:
                last, quiet = rev, 0.0
                yield f"event: rev\ndata: {json.dumps({'revision': rev, 'at': H.now_iso()})}\n\n"
            await asyncio.sleep(STREAM_POLL_S)
            quiet += STREAM_POLL_S
            if quiet >= 15:
                quiet = 0.0
                yield ": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@router.post("/api/history/ingest")
def api_history_ingest():
    return {"started": ticker.run_once(), "ingest": ticker.status()}


# ---------------------------------------------------------------- ask

ASK_PROMPT = """You turn one question about past work into a view of Vira's history.
Reply with ONE JSON object and nothing else - no prose, no code fence.

Shape:
{{"view": "list" | "grid" | "timemap" | "entry",
  "group": "day" | "request" | "module" | "project" | "automation" | "session",
  "filters": {{"since": "YYYY-MM-DD or empty", "until": "YYYY-MM-DD or empty",
              "project": "one of the projects or empty", "module": "one of the modules or empty",
              "status": [zero or more of the status words],
              "kind": [zero or more of the kinds], "q": "search words or empty"}},
  "lane": "project" | "module", "span": "day" | "hour"}}

Use "grid" for whether automations ran, "timemap" for when work happened over a span,
"entry" with filters.q for one specific piece of work, "list" otherwise.
Today is {today} ({weekday}). Weeks start on Monday.
Projects: {projects}
Modules: {modules}
Status words: {statuses}
Kinds: {kinds}

Question: {question}
"""


def _clean_spec(raw, fac):
    """The model's spec, with every value checked against what exists.
    Anything unknown is dropped, never passed on."""
    projects = {p["key"] for p in fac["projects"]}
    modules = {m["key"] for m in fac["modules"]} | {m["key"].split("/")[0] for m in fac["modules"]}
    raw = raw if isinstance(raw, dict) else {}
    f = raw.get("filters") if isinstance(raw.get("filters"), dict) else {}

    def day(v):
        v = str(v or "").strip()[:10]
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            return ""

    def listed(v, allowed):
        vals = v if isinstance(v, list) else [v] if v else []
        return [x for x in (str(y) for y in vals) if x in allowed]

    project = str(f.get("project") or "")
    module = str(f.get("module") or "")
    spec = {"view": raw.get("view") if raw.get("view") in ("list", "grid", "timemap", "entry") else "list",
            "group": raw.get("group") if raw.get("group") in V.GROUPS else "day",
            "filters": {"since": day(f.get("since")), "until": day(f.get("until")),
                        "project": project if project in projects else "",
                        "module": module if module in modules else "",
                        "status": listed(f.get("status"), H.STATUSES),
                        "kind": listed(f.get("kind"), H.KINDS),
                        "q": str(f.get("q") or "")[:200]},
            "lane": raw.get("lane") if raw.get("lane") in ("project", "module") else "project",
            "span": raw.get("span") if raw.get("span") in ("day", "hour") else "day"}
    return spec


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def keyword_spec(question, fac, today=None):
    """A spec from the words alone, for when the model cannot answer. It is
    returned labelled as such, never in place of the model's."""
    from datetime import timedelta
    q = str(question or "").lower()
    today = today or datetime.now().date()
    spec = {"view": "list", "group": "day", "filters": {"since": "", "until": "", "project": "",
            "module": "", "status": [], "kind": [], "q": ""}, "lane": "project", "span": "day"}
    f = spec["filters"]
    if "yesterday" in q:
        f["since"] = f["until"] = (today - timedelta(days=1)).isoformat()
    elif "today" in q:
        f["since"] = today.isoformat()
    elif "last week" in q:
        start = today - timedelta(days=today.weekday() + 7)
        f["since"], f["until"] = start.isoformat(), (start + timedelta(days=6)).isoformat()
    elif "this week" in q or "week" in q:
        f["since"] = (today - timedelta(days=today.weekday())).isoformat()
    for w in H.STATUSES:
        if w.lower() in q:
            f["status"].append(w)
    if "fail" in q and H.FAILED not in f["status"]:
        f["status"].append(H.FAILED)
    for p in fac["projects"]:
        if p["key"].lower() in q.split() or f" {p['key'].lower()} " in f" {q} ":
            f["project"] = p["key"]
            break
    if any(w in q for w in ("did ", "run", "capture", "automation", "flow", "program")):
        spec["view"], spec["group"] = "grid", "automation"
    elif "module" in q:
        spec["group"] = "module"
    return spec


class AskReq(BaseModel):
    q: str


@router.post("/api/history/ask")
def api_history_ask(req: AskReq):
    """A plain-words question -> {"spec": ..., "source": "model"} using
    Vira's configured model. No prose comes back: the spec says which view
    to open and how to filter it. When the model cannot answer, `status`
    names why and `fallback` carries a spec read from the words alone."""
    question = (req.q or "").strip()
    if not question:
        raise HTTPException(400, "ask a question")
    fac = V.facets()
    today = datetime.now()
    prompt = ASK_PROMPT.format(
        today=today.strftime("%Y-%m-%d"), weekday=_WEEKDAYS[today.weekday()],
        projects=", ".join(p["key"] for p in fac["projects"][:40]) or "(none)",
        modules=", ".join(m["key"] for m in fac["modules"][:120]) or "(none)",
        statuses=", ".join(H.STATUSES), kinds=", ".join(H.KINDS), question=question[:1000])
    from . import suggest
    try:
        raw = suggest.complete(prompt, tools=[], timeout=60)
    except Exception as e:  # noqa: BLE001 - a down backend is a named status
        return {"status": "model_unavailable", "message": str(e)[:300], "spec": None,
                "fallback": keyword_spec(question, fac), "fallback_source": "keywords"}
    try:
        spec = _clean_spec(suggest._extract_json(raw or ""), fac)
    except (ValueError, json.JSONDecodeError) as e:
        return {"status": "invalid_answer", "message": str(e)[:300], "spec": None,
                "fallback": keyword_spec(question, fac), "fallback_source": "keywords"}
    return {"status": "ok", "spec": spec, "source": "model"}


# ---------------------------------------------------------------- actions

class ActionReq(BaseModel):
    id: str
    action: str
    confirm: bool = False
    branch: str | None = None
    job: str | None = None
    run: str | None = None
    force: bool = False
    prompt: str | None = None
    model: str | None = None
    provider: str | None = None
    mode: str | None = None
    read_only: bool = False


def _pick(acts, req):
    for a in acts:
        if a["action"] != req.action:
            continue
        if req.branch and a.get("branch") != req.branch:
            continue
        if req.job and a.get("job") != req.job:
            continue
        if req.run and a.get("run") != req.run:
            continue
        return a
    return None


def _landing_card(jid):
    from . import session
    snap = session.sessions.get(jid) or {}
    for p in snap.get("pending") or []:
        if p.get("kind") == session.LANDING_KIND:
            return p
    return None


def _orphan_item(branch):
    from . import orphanwork
    try:
        items = orphanwork.compose().get("items") or []
    except Exception:  # noqa: BLE001
        items = []
    return next((it for it in items if it.get("branch") == branch), None)


def _branch_event(idx, tid, branch):
    for e in idx.trail_events(tid):
        if e["kind"] == "code" and (e.get("data") or {}).get("branch_name") == branch:
            return e
    return None


@router.post("/api/history/action")
def api_history_action(req: ActionReq):
    """Act on an entry. Only an action the entry offers (its `actions`) is
    carried out, through the same code the older screens use: Merge and
    Discard through branch.sh (orphanwork) or the session's own landing
    card, Resume through orphanwork.resume, Rerun through the Flow or the
    routine, Judge through judge.launch_judge. Merge and Discard answer 409
    with the confirm text until they are sent with confirm=true."""
    idx = V.index()
    tid = idx.resolve(req.id)
    if not tid:
        raise HTTPException(404, "no history entry for that id")
    act = _pick(V.actions_for(idx, tid), req)
    if act is None:
        raise HTTPException(409, f"{req.action} is not available for this entry")
    if act["action"] in ("merge", "discard") and not req.confirm:
        raise HTTPException(409, {"needs_confirm": True, "confirm": act["confirm"]})
    try:
        return {"action": act["action"], **_do(idx, tid, act, req)}
    except (ValueError, KeyError) as e:
        raise HTTPException(409, str(e)) from None


def _do(idx, tid, act, req):
    from . import orphanwork
    name = act["action"]
    if name in ("merge", "discard"):
        if act.get("job"):
            from . import session
            card = _landing_card(act["job"])
            if not card:
                raise ValueError("that session is no longer waiting on its landing card")
            verdict = session.sessions.answer(act["job"], card["req_id"],
                                              "Merge it" if name == "merge" else "Discard")
            return {"started": True, "via": "landing card", "verdict": verdict}
        branch = act["branch"]
        orphanwork.require_action_branch(branch)
        slug = branch.split("/", 1)[1]
        ok, detail = (orphanwork.merge(slug) if name == "merge"
                      else orphanwork.discard(slug, force=req.force))
        if not ok:
            raise ValueError(detail)
        return {"started": True, "via": "branch.sh", "branch": branch}
    if name == "resume":
        branch = act["branch"]
        item = _orphan_item(branch)
        if item is None:
            ev = _branch_event(idx, tid, branch) or {}
            d = ev.get("data") or {}
            item = {"branch": branch, "worktree": d.get("worktree") or "",
                    "subject": ev.get("title") or "", "pr": None}
        if not req.prompt:
            return {"needs_prompt": True, "prompt": orphanwork.resume_prompt(item),
                    "cwd": item.get("worktree") or ""}
        jid = orphanwork.resume(item, prompt=req.prompt, model=req.model,
                                provider=req.provider, mode=req.mode, read_only=req.read_only)
        return {"job_id": jid}
    if name == "rerun":
        return _rerun(idx, act["run"])
    if name == "judge":
        from . import judge
        return {"judge_job_id": judge.launch_judge(act["job"], model=req.model)}
    raise ValueError(f"unknown action {name!r}")


def _rerun(idx, run_event_id):
    """Run the same Flow again with the same input and the same Schedule
    entry; a Program run reruns the Flow run that started it, or its
    routine when it was a routine of its own."""
    from . import circuits, routines
    ev = idx.events.get(run_event_id)
    if not ev:
        raise KeyError(run_event_id)
    links = ev.get("links") or {}
    if ev["sub"] == "program":
        fr = (links.get("flow_run") or [""])[0]
        if fr and ("run:flow:" + fr) in idx.events:
            return _rerun(idx, "run:flow:" + fr)
        rid = (links.get("program") or [""])[0]
        r = routines.get_routine(rid) if rid else None
        if not r:
            raise ValueError("this Program run has no Flow run or routine to start again")
        return {"started": routines.dispatch(r), "via": "routine"}
    rid = (links.get("flow_run") or [""])[0]
    run = circuits.get_run(rid) or {}
    cid = run.get("circuit_id") or (links.get("flow") or [""])[0]
    data = ev.get("data") or {}
    text = run.get("input") or data.get("input") or ""
    entry = run.get("entry") if run else data.get("entry")
    new = circuits.start_run(cid, text, cwd=run.get("cwd") or ev.get("folder") or None,
                             source="history-rerun", entry=entry)
    return {"run_id": new["id"], "via": "flow"}

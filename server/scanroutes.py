"""HTTP surface for Scans (server/scans.py). Every route is a thin wrapper
over one engine call; the engine owns every rule.

The scanner app calls two of these: GET /api/scans/config to learn where to
save, and POST /api/scans/arrived after it saved, which makes Vira look at
the folder now instead of on its next tick.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import scans, settings

router = APIRouter(prefix="/api/scans")


class ArrivedReq(BaseModel):
    path: str = ""
    title: str = ""
    pages: int = 0


class DecisionReq(BaseModel):
    changes: dict = {}


class ActionReq(BaseModel):
    index: int
    done: bool = True


def _item(sid):
    it = scans.get(sid)
    if not it:
        raise HTTPException(404, "no such scan")
    return it


def _bad(e):
    raise HTTPException(400, str(e)) from e


@router.get("")
def api_scans():
    return scans.status()


@router.get("/config")
def api_scans_config():
    f = scans.folder()
    return {"enabled": scans.enabled(), "folder": str(f) if f else "",
            "auto": bool(settings.get("scans_auto"))}


@router.post("/look")
def api_scans_look():
    """Look at the folder now, and start reads for new scans."""
    threading.Thread(target=scans.tick, kwargs={"automatic": False},
                     daemon=True, name="vira-scans-look").start()
    return {"ok": True}


@router.post("/arrived")
def api_scans_arrived(req: ArrivedReq):
    """The scanner saved a file. The folder stays the source of truth: this
    only checks the path is in it and wakes the worker."""
    f = scans.folder()
    if not f:
        raise HTTPException(409, "scan filing is off: set Config > Intake > Scans folder")
    p = Path(req.path).expanduser()
    try:
        p.resolve().relative_to(f.resolve())
    except (OSError, ValueError):
        raise HTTPException(400, "that file is not in the scans folder") from None
    scans.wake()
    sha = ""
    if p.is_file():
        try:
            sha = scans._sha256(p)[:12]
        except OSError:
            sha = ""
    return {"ok": True, "id": sha}


@router.post("/open-scanner")
def api_scans_open_scanner():
    if not settings.IS_MAC:
        raise HTTPException(400, "the scanner app runs on macOS")
    bid = str(settings.get("scans_scanner_app") or "").strip()
    if not bid:
        raise HTTPException(400, "no scanner app is set (Config > Intake)")
    r = subprocess.run(["open", "-b", bid], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise HTTPException(404, f"the scanner app ({bid}) is not installed: "
                                 "build it with macos/scanner/scripts/build.sh")
    return {"ok": True}


@router.get("/{sid}")
def api_scan(sid: str):
    return scans.public(_item(sid), detail=True)


@router.get("/{sid}/file")
def api_scan_file(sid: str):
    it = _item(sid)
    p = Path(it["path"])
    if not p.is_file():
        raise HTTPException(404, "the scan's file is no longer there")
    media = "application/pdf" if p.suffix.lower() == ".pdf" else None
    return FileResponse(p, media_type=media, filename=p.name,
                        content_disposition_type="inline")


@router.get("/{sid}/thumb")
def api_scan_thumb(sid: str):
    it = _item(sid)
    t = scans.thumb_path(sid)
    if not t.is_file():
        t = scans.make_thumb(sid, it["path"])
    if not t or not Path(t).is_file():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(t, media_type="image/png")


@router.get("/{sid}/page/{n}")
def api_scan_page(sid: str, n: int):
    t = scans.page_image(_item(sid), n)
    if not t:
        raise HTTPException(404, "no such page")
    return FileResponse(t, media_type="image/png")


@router.post("/{sid}/read")
def api_scan_read(sid: str):
    it = _item(sid)
    try:
        return scans.public(scans.start_read(sid, force=it["state"] in ("failed", "undone", "ready", "reading")))
    except ValueError as e:
        _bad(e)


@router.put("/{sid}/decision")
def api_scan_decision(sid: str, req: DecisionReq):
    _item(sid)
    try:
        return scans.public(scans.set_decision(sid, req.changes), detail=True)
    except ValueError as e:
        _bad(e)


@router.post("/{sid}/file")
def api_scan_file_it(sid: str):
    _item(sid)
    try:
        return scans.public(scans.file_scan(sid, by_owner=True), detail=True)
    except ValueError as e:
        _bad(e)


@router.post("/{sid}/undo")
def api_scan_undo(sid: str):
    _item(sid)
    try:
        it, kept = scans.undo(sid)
    except ValueError as e:
        _bad(e)
    return {"item": scans.public(it, detail=True), "kept": kept}


@router.post("/{sid}/ignore")
def api_scan_ignore(sid: str):
    _item(sid)
    return scans.public(scans.ignore(sid))


@router.post("/{sid}/action")
def api_scan_action(sid: str, req: ActionReq):
    _item(sid)
    return scans.public(scans.mark_action(sid, req.index, req.done), detail=True)

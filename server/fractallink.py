"""Show a local Find note or filed scan in Fractal Hybrid on the same Mac."""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import scans, settings, vault, vaultwrite

router = APIRouter(prefix="/api/fractal")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class RevealRequest(BaseModel):
    kind: Literal["note", "scan"]
    ref: str
    copy_ref: str | None = Field(default=None, alias="copy")


def _local_browser(request):
    # A remote proxy can connect from loopback. Also require the local Host
    # and exact browser Origin, so a site opened on this Mac cannot launch it.
    host = request.headers.get("host", "")
    return (request.client is not None and request.client.host in _LOOPBACK
            and request.url.hostname in _LOOPBACK
            and request.headers.get("origin") == f"{request.url.scheme}://{host}"
            and request.headers.get("sec-fetch-site") not in {"cross-site", "same-site"})


def _existing(spec, rel):
    if spec is None:
        raise HTTPException(404, "This vault is no longer connected.")
    try:
        if vaultwrite.relative_path(rel) != rel:
            raise ValueError("not an exact relative path")
        target = vaultwrite.safe_path(spec, rel, operation="read")
    except (ValueError, OSError) as exc:
        raise HTTPException(403, "This file is outside the vault's current read access.") from exc
    if not target.is_file():
        raise HTTPException(404, "The file is no longer there. It may have moved, or the drive may be disconnected.")
    return target


def _note(ref):
    # Use the same primary / @source reference that Find gives the note viewer.
    specs = vault.source_specs()
    if ref.startswith("@"):
        source, _, rel = ref[1:].partition("/")
        spec = next((s for s in specs if s["id"] == source), None)
    else:
        rel = ref
        spec = next((s for s in specs if s["primary"]), None)
    if Path(rel).suffix.lower() != ".md":
        raise HTTPException(400, "Choose a Markdown note in Find.")
    return _existing(spec, rel)


def _scan(ref, copy):
    item = scans.get(ref)
    if not item or item.get("state") != "filed" or not item.get("filing"):
        raise HTTPException(404, "This scan is no longer filed in Vira.")
    spec = next((s for s in vault.source_specs() if s["id"] == item.get("vault_id")), None)
    if spec is None:
        raise HTTPException(404, "This scan's vault is no longer connected.")
    original = item["filing"].get("rel") or item.get("rel")
    # Compare the original recorded location even when the requested target
    # is a copy. Reconnecting the source ID elsewhere must not retarget it.
    if (not original or not item.get("path")
            or spec["root"].resolve() / original != Path(item["path"]).resolve()):
        raise HTTPException(404, "This scan's vault location changed since filing.")
    if copy is not None and not any(c.get("rel") == copy for c in item["filing"].get("copies", [])):
        raise HTTPException(404, "That copy is not in this scan's filing record.")
    return _existing(spec, original if copy is None else copy)


def _open(target):
    # quote(), rather than quote_plus(), matches macOS URLComponents decoding
    # for paths containing spaces, plus signs, Unicode, and literal hashes.
    url = "fractal-hybrid://reveal?path=" + quote(str(target), safe="")
    try:
        subprocess.run(["/usr/bin/open", url], check=True, capture_output=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "Could not open Fractal. Install or open a version of Fractal Hybrid that supports Show in Fractal, then try again.") from exc


@router.post("/reveal")
def reveal(req: RevealRequest, request: Request):
    if not settings.IS_MAC or not _local_browser(request):
        raise HTTPException(403, "Open Vira on this Mac to show a file in Fractal.")
    if not req.ref or (req.kind == "note" and req.copy_ref is not None):
        raise HTTPException(400, "Choose a note or a filed scan.")
    try:
        target = _note(req.ref) if req.kind == "note" else _scan(req.ref, req.copy_ref)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(404, "The file is unavailable on this Mac.") from exc
    _open(target)
    return {"ok": True}

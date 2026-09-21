"""Desktop pins onto canonical reminders, never a second task store.

Only identifiers and geometry persist here. Every read resolves current task
text and state from executive; an unavailable source is not called completed.
Branch previews can exercise layout changes in their isolated data snapshots;
canonical reminder sources are read without changing their task state.
"""
import json
import math
import re

from fastapi import APIRouter, HTTPException

from . import executive, jsonstore, settings, worktree
from .filelock import locked


router = APIRouter(prefix="/api/reminder-stickies")
STORE = settings.ROOT / "data" / "reminder-stickies.json"
MAX_PINS = 50
ID_RE = re.compile(r"[a-f0-9]{24}\Z")
DEFAULT_GEOMETRY = {"x": 48, "y": 96, "width": 288, "height": 250}
BOUNDS = {"x": (0, 20000), "y": (0, 20000),
          "width": (220, 600), "height": (180, 800)}


def isolated():
    return bool(settings.sandboxed())


def _snapshot_layout():
    """Allow local geometry only in a complete, unshared branch snapshot.

    The branch tool writes the marker after cloning data. A linked data root,
    store, or writer sidecar would defeat that isolation, so refuse it before
    reading sources or opening the lock. No Git subprocess is needed per poll.
    """
    try:
        root = settings.ROOT.resolve()
        data = root / "data"
        marker = data / ".test-snapshot"
        if (not worktree.is_worktree(root) or data.is_symlink()
                or data.resolve() != data or not marker.is_file()
                or STORE != data / "reminder-stickies.json"):
            return False
        for path in (marker, STORE, STORE.with_name(STORE.name + ".lock"),
                     STORE.with_name(STORE.name + ".tmp")):
            if path.is_symlink():
                return False
            if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                return False
        return True
    except OSError:
        return False


def layout_blocked():
    return isolated() and not _snapshot_layout()


def _id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("Use a canonical reminder identifier")
    return value


def _geometry(value, *, partial=False):
    if not isinstance(value, dict) or set(value) - BOUNDS.keys():
        raise ValueError("Only x, y, width and height describe a pin")
    out = {} if partial else dict(DEFAULT_GEOMETRY)
    for key, number in value.items():
        low, high = BOUNDS[key]
        if (type(number) not in (int, float) or not math.isfinite(number)
                or not low <= number <= high):
            raise ValueError(f"{key} must be a finite number from {low} to {high}")
        out[key] = round(number, 2)
    return out


def _read():
    try:
        state = json.loads(STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"pins": {}}
    except (OSError, ValueError) as exc:
        raise ValueError("Pinned reminder layout needs repair") from exc
    if (not isinstance(state, dict) or set(state) != {"pins"}
            or not isinstance(state["pins"], dict) or len(state["pins"]) > MAX_PINS):
        raise ValueError("Pinned reminder layout needs repair")
    for rid, geometry in state["pins"].items():
        _id(rid)
        if not isinstance(geometry, dict) or set(geometry) != BOUNDS.keys():
            raise ValueError("Pinned reminder layout needs repair")
        _geometry(geometry)
    return state


def _sources():
    """Include snoozed and explicitly closed tasks; absence is a third state."""
    rows = {row["id"]: row for row in executive.reminders(include_snoozed=True)}
    for record in executive.commitment_records():
        loop = record["loop"]
        if loop.get("status") != "closed":
            continue
        rid = executive._key(record["subject_key"], loop)
        rows[rid] = {"id": rid, "what": loop.get("what") or "Completed reminder",
                     "status": "closed", "person_id": record.get("person_id"),
                     "person_name": record.get("person_name"), "due": loop.get("due"),
                     "evidence": loop.get("evidence") or [], "closed_on": loop.get("closed_on")}
    return rows


def snapshot():
    if layout_blocked():
        return {"items": [], "eligible_ids": [], "read_only": True,
                "actions_read_only": True,
                "error": "Desktop reminder pins are disabled in a preview instance."}
    state = _read()
    error = None
    try:
        sources = _sources()
    except Exception:  # noqa: BLE001 - preserve pins when a source fails
        sources = {}
        error = "Reminder sources could not be read. Your pins are still saved."
    items = []
    for rid, geometry in state["pins"].items():
        source = sources.get(rid)
        items.append({"id": rid, **geometry, "reminder": source,
                      "state": source["status"] if source else "unavailable" if error else "missing"})
    return {"items": items, "eligible_ids": [rid for rid, row in sources.items()
            if row["status"] != "closed"], "read_only": False,
            "actions_read_only": isolated() or settings.fixture_mode(), "error": error}


def pin(rid, geometry):
    if layout_blocked():
        raise PermissionError("Pinning reminders is disabled in a preview instance")
    rid, geometry = _id(rid), _geometry(geometry)
    source = _sources().get(rid)
    if not source or source["status"] == "closed":
        raise KeyError(rid)
    with locked(STORE):
        state = _read()
        if rid not in state["pins"]:
            if len(state["pins"]) >= MAX_PINS:
                raise ValueError(f"At most {MAX_PINS} reminders can be pinned")
            state["pins"][rid] = geometry
            jsonstore.write_atomic(STORE, state, indent=1)
        return {"id": rid, **state["pins"][rid]}


def move(rid, geometry):
    if layout_blocked():
        raise PermissionError("Moving reminder pins is disabled in a preview instance")
    rid, geometry = _id(rid), _geometry(geometry, partial=True)
    with locked(STORE):
        state = _read()
        if rid not in state["pins"]:
            raise KeyError(rid)
        state["pins"][rid].update(geometry)
        jsonstore.write_atomic(STORE, state, indent=1)
        return {"id": rid, **state["pins"][rid]}


def unpin(rid):
    if layout_blocked():
        raise PermissionError("Unpinning reminders is disabled in a preview instance")
    rid = _id(rid)
    with locked(STORE):
        state = _read()
        if state["pins"].pop(rid, None) is not None:
            jsonstore.write_atomic(STORE, state, indent=1)
    return {"id": rid, "pinned": False}


def _route(call, *args):
    try:
        return call(*args)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Reminder or pin is no longer available") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def get_pins():
    return _route(snapshot)


@router.post("")
def create_pin(body: dict):
    data = dict(body)
    return _route(pin, data.pop("reminder_id", None), data)


@router.put("/{rid}")
def move_pin(rid: str, body: dict):
    return _route(move, rid, body)


@router.delete("/{rid}")
def delete_pin(rid: str):
    return _route(unpin, rid)

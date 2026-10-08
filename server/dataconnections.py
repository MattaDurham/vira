"""Inspect and connect owner stores without importing, copying or migrating them.

Preview receipts are stateless fingerprints of the request, configuration and
inspected files. Apply repeats validation under the configuration lock. Existing
CRM switches must retain all current identities: instance-local overlays and
queues refer to those IDs and cannot be migrated by changing a path.
"""
import hashlib
import json
import os
import re
from contextlib import ExitStack
from pathlib import Path

from . import settings


class ConnectionError(ValueError):
    pass


def _config():
    try:
        doc = json.loads(settings.CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConnectionError("Vira configuration could not be read; repair it before connecting data.") from exc
    if not isinstance(doc, dict):
        raise ConnectionError("Vira configuration must be an object.")
    return doc


def _path(value):
    if not isinstance(value, str) or not value.strip():
        raise ConnectionError("Choose an absolute folder path.")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ConnectionError("Choose an absolute folder path (or ~/...).")
    return path.resolve()


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectionError(f"{path.name} could not be read as UTF-8 JSON.") from exc


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry(root):
    doc = _json(root / "people.json")
    if not isinstance(doc, dict) or not isinstance(doc.get("people"), list):
        raise ConnectionError("people.json must contain a people list.")
    people = {}
    for row in doc["people"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"p_[A-Za-z0-9_-]+", row["id"]) or not isinstance(row.get("name"), str)):
            raise ConnectionError("Every person needs a Vira person ID and a name.")
        if row["id"] in people:
            raise ConnectionError("people.json contains duplicate person IDs.")
        handles = row.get("handles", {})
        if not isinstance(handles, dict):
            raise ConnectionError("Person handles must be an object.")
        for key in ("emails", "phones10", "imessage"):
            values = handles.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ConnectionError("Contact handle buckets must contain lists of strings.")
        for key in ("refs", "activity"):
            if key in row and not isinstance(row[key], dict):
                raise ConnectionError(f"Person {key} must be an object.")
        people[row["id"]] = row
    return people


def _crm(root, mode):
    warnings = []
    if mode == "fresh":
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise ConnectionError("Start fresh needs an empty folder. Use existing CRM for a populated folder.")
        parent = root if root.exists() else root.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise ConnectionError("Choose an empty writable folder or a new folder inside a writable parent.")
        return {"root": str(root), "people": 0, "profiles": 0,
                "warnings": ["Contacts will be imported here. Demo mode remains until contacts are imported."]}, {}, []
    if not root.is_dir():
        raise ConnectionError("The CRM folder is unavailable.")
    if not os.access(root, os.W_OK):
        raise ConnectionError("Vira needs a writable CRM folder for contact and profile updates.")
    people = _registry(root)
    stamps = [("people.json", _digest(root / "people.json"))]
    master = root / "master.json"
    if master.exists():
        rows = _json(master)
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in rows):
            raise ConnectionError("master.json must be a list of records with person IDs.")
        stamps.append(("master.json", _digest(master)))
        if any(r["id"] not in people for r in rows):
            warnings.append("Some master records have no matching person ID.")
    else:
        warnings.append("No master.json found; imported company and evidence details may be unavailable.")
    profiles = root / "profiles"
    count = orphaned = 0
    if profiles.exists() and not profiles.is_dir():
        raise ConnectionError("profiles must be a folder.")
    for file in sorted(profiles.glob("p_*.json")):
        profile = _json(file)
        if not isinstance(profile, dict):
            raise ConnectionError("Each profile must be a JSON object.")
        if "id" in profile and profile["id"] != file.stem:
            raise ConnectionError("A profile ID does not match its filename.")
        for key in ("hooks", "open_loops", "personal_facts", "topics", "resolved_loops"):
            if key in profile and not isinstance(profile[key], list):
                raise ConnectionError(f"Profile {key} must be a list.")
        stamps.append((str(file.relative_to(root)), _digest(file)))
        count += file.stem in people
        orphaned += file.stem not in people
    if orphaned:
        warnings.append(f"{orphaned} profiles have no matching person ID and will not appear as contacts.")
    if not count:
        warnings.append("No matching profiles found. You can build them after connecting contacts and AI.")
    return {"root": str(root), "people": len(people), "profiles": count,
            "warnings": warnings}, people, stamps


def _self_root(cfg, crm_root):
    if cfg.get("self_record"):
        return _path(cfg["self_record"])
    # A configured CRM has an independent real-store default even while a
    # fresh CRM still displays demo contacts. Never pin the demo self folder.
    return crm_root / "self"


def _self_info(cfg, root):
    crm_root = _path(cfg.get("crm_root") or settings.DEFAULTS["crm_root"])
    flag = cfg.get("fixture_mode")
    demo = flag if isinstance(flag, bool) else not (crm_root / "people.json").is_file()
    effective = settings.FIXTURE_CRM / "self" if demo and not cfg.get("self_record") else root
    canon = effective / "canon" / "MASTER_HISTORY.md"
    ready = canon.is_file() and os.access(canon, os.R_OK)
    packages = cfg.get("applications_packages_root")
    legacy_packages = Path.home() / "Documents" / "CV" / "15-applications"
    packages = (_path(packages) if packages else legacy_packages if legacy_packages.is_dir()
                else effective / "15-applications")
    return {"root": str(root), "explicit": bool(cfg.get("self_record")),
            "effective_root": str(effective),
            "available": root.is_dir(), "career_ready": ready,
            "canon": str(canon),
            "analysis": str(_path(cfg["applications_universe"]) if cfg.get("applications_universe") else effective / "analysis"),
            "packages": str(packages)}


def _handles(row):
    return {v.casefold() for values in row.get("handles", {}).values()
            if isinstance(values, list) for v in values if isinstance(v, str)}


def _compatible(old, new):
    if set(old) - set(new):
        raise ConnectionError("This CRM drops current person IDs. Connecting an unrelated CRM requires a migration; saved contact references are kept unchanged.")
    for pid, row in old.items():
        incoming = new[pid]
        before, after = _handles(row), _handles(incoming)
        if ((before and after and not before.intersection(after))
                or (not (before and after) and row["name"].casefold() != incoming["name"].casefold())):
            raise ConnectionError("A current person ID identifies a different contact in this folder. Reconcile identities before connecting.")


def _saved_person_ids():
    # These instance-owned stores keep person references even when an external
    # CRM volume is offline. Do not inspect arbitrary documents/transcripts.
    names = ("contact-cards.json", "atlas-groups.json", "atlas-circles.json",
             "brief-state.json", "contact-intelligence.json", "assistant-commitments.json",
             "calendar-plans.json", "reconnect.json", "send-channels.json")
    ids = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            # Dismissal/reconnect keys embed the ID between colon delimiters.
            for part in value.split(":"):
                if re.fullmatch(r"p_[A-Za-z0-9_-]+", part):
                    ids.add(part)

    for name in names:
        path = settings.CONFIG_PATH.parent / name
        if path.exists():
            visit(_json(path))
    return ids


def _request(request):
    if not isinstance(request, dict):
        raise ConnectionError("The connection request must be an object.")
    if set(request) - {"kind", "path", "mode", "self_choice", "label", "glob", "document_kind"}:
        raise ConnectionError("Unknown connection option.")
    kind, mode = request.get("kind"), request.get("mode", "existing")
    if kind not in ("crm", "self", "reader"):
        raise ConnectionError("Choose CRM, self record or Reader.")
    if mode not in (("existing", "fresh") if kind == "crm" else ("existing", "disconnect") if kind == "reader" else ("existing",)):
        raise ConnectionError("Unsupported connection mode.")
    choice = request.get("self_choice", "keep")
    if choice not in ("keep", "follow"):
        raise ConnectionError("Choose keep or follow for the self-record location.")
    return {**request, "kind": kind, "mode": mode, "path": str(_path(request.get("path"))), "self_choice": choice}


def _plan(request, cfg):
    request = _request(request)
    root = _path(request["path"])
    current_crm = _path(cfg.get("crm_root") or settings.DEFAULTS["crm_root"])
    current_self = _self_root(cfg, current_crm)
    kind = request["kind"]
    updates, stamps, warnings = {}, [], []
    if kind == "crm":
        info, people, stamps = _crm(root, request["mode"])
        warnings.extend(info["warnings"])
        if root != current_crm and (current_crm / "people.json").exists():
            old = _registry(current_crm)
            _compatible(old, people)
            stamps.append(("current-people", _digest(current_crm / "people.json")))
        if root != current_crm:
            saved_ids = _saved_person_ids()
            if saved_ids - set(people):
                raise ConnectionError("Saved contact references are missing from this CRM. Restore a compatible registry or migrate the instance state before switching.")
            stamps.append(("saved-person-ids", sorted(saved_ids)))
        updates = {"crm_root": str(root), "fixture_mode": None}
        if request["self_choice"] == "keep":
            # Pin only when the real default would move. Keeping an already
            # explicit choice preserves the owner's spelling and config.
            if not cfg.get("self_record") and root != current_crm:
                updates["self_record"] = str(current_self)
        else:
            updates["self_record"] = ""
        next_cfg = {**cfg, **updates}
        info["self_record_before"] = str(current_self)
        info["self_record_after"] = _self_info(next_cfg, _self_root(next_cfg, root))
        if info["self_record_after"]["root"] != str(current_self):
            warnings.append("The self-record location will change. Existing analysis and package overrides stay in place.")
    elif kind == "self":
        if not root.is_dir():
            raise ConnectionError("Choose an existing self-record folder.")
        # Enumerate once so inaccessible directories are not advertised as connected.
        next(root.iterdir(), None)
        updates = {"self_record": str(root)}
        info = _self_info({**cfg, **updates}, root)
        if not info["career_ready"]:
            warnings.append("No readable canon/MASTER_HISTORY.md found. Career evidence is not ready; the folder can still be connected.")
        canon = root / "canon" / "MASTER_HISTORY.md"
        if canon.is_file():
            stamps.append(("canon", _digest(canon)))
        warnings.append("Connecting does not add this folder to Brain or grant model access. Configure Brain separately.")
    else:
        rows = cfg.get("reader_sources", [])
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ConnectionError("Repair the Reader source configuration before changing connections.")
        matches = [r for r in rows if r.get("path") and _path(r["path"]) == root]
        if request["mode"] == "disconnect":
            if not matches:
                raise ConnectionError("This Reader folder is not connected.")
            updates = {"reader_sources": [r for r in rows if r not in matches]}
            info = {"root": str(root), "label": matches[0].get("label") or root.name}
        else:
            if not root.is_dir():
                raise ConnectionError("The Reader folder is unavailable.")
            next(root.iterdir(), None)
            if matches:
                raise ConnectionError("This Reader folder is already connected.")
            from . import readinglist
            pattern = request.get("glob", "*.html")
            label = request.get("label", "")
            document_kind = request.get("document_kind", "dossier")
            if not isinstance(label, str) or not isinstance(pattern, str) or not pattern or any(p in pattern for p in ("/", "\\", "..")):
                raise ConnectionError("Use a filename pattern inside the Reader folder, without path separators or parent hops.")
            if document_kind not in readinglist.READER_SOURCE_KINDS:
                raise ConnectionError("Unsupported Reader document kind.")
            row = {"path": str(root), "label": label.strip() or root.name, "glob": pattern, "kind": document_kind}
            updates = {"reader_sources": [*rows, row]}
            info = {"root": str(root), **{k: row[k] for k in ("label", "glob", "kind")}}
            warnings.append("Reader reads matching files and index.html bundles here without copying or editing them. Use Reader's Scan after connecting.")
    return request, updates, info, warnings, stamps


def preview(request, cfg=None):
    cfg = _config() if cfg is None else cfg
    try:
        request, updates, info, warnings, stamps = _plan(request, cfg)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"valid": False, "errors": [str(exc)], "warnings": []}
    receipt = json.dumps([request, cfg, info, warnings, stamps], sort_keys=True, ensure_ascii=False)
    return {"valid": True, "errors": [], "warnings": warnings,
            "request": request, "summary": info, "updates": updates,
            "revision": hashlib.sha256(receipt.encode("utf-8")).hexdigest()}


def _idle():
    from . import instance, joblog, jobrescore, onboard, session
    if onboard._build.get("running"):
        raise ConnectionError("Finish the profile build before changing data locations.")
    if any(r.get("status") == "running" for r in session.sessions.recent()):
        raise ConnectionError("Finish or close running Vira sessions before changing data locations.")
    if any(r.get("status") == "running" and instance.owns(r) for r in joblog._read()["jobs"]):
        raise ConnectionError("A detached Vira job is running. Finish or close it before changing data locations.")
    if jobrescore.bulk_status().get("running"):
        raise ConnectionError("Finish the bulk application rescore before changing data locations.")


def connect(request, revision):
    from . import atlas, contactintel, crmindex, data as crm, onboard, reconnect, triage
    if not isinstance(revision, str) or not revision:
        raise ConnectionError("Inspect this connection before saving it.")
    kind = request.get("kind") if isinstance(request, dict) else None
    with ExitStack() as stack:
        # No network/model calls under these locks. Contact intelligence holds
        # its tick lock during processing; refuse immediately if it is busy.
        if kind in ("crm", "self"):
            for label, lock in (("Contact maintenance", contactintel._tick_lock),
                                ("Network refresh", atlas._refresh_lock),
                                ("Reconnect refresh", reconnect._refresh_lock)):
                if not lock.acquire(blocking=False):
                    raise ConnectionError(f"{label} is running. Try again when it finishes.")
                stack.callback(lock.release)
            stack.enter_context(onboard._build_lock)
            stack.enter_context(onboard._lock)
            stack.enter_context(triage._lock)
            stack.enter_context(crm._write_lock)
            _idle()
        plan = preview(request)
        if not plan["valid"]:
            raise ConnectionError("; ".join(plan["errors"]))
        if plan["revision"] != revision:
            raise ConnectionError("The folder or configuration changed. Inspect again before connecting.")

        def validate(cfg):
            # Strict read also prevents jsonstore's tolerant default from
            # overwriting a concurrently corrupted config.
            if _config() != cfg:
                raise ConnectionError("The configuration changed. Inspect again.")
            fresh = preview(request, cfg)
            if not fresh["valid"] or fresh.get("revision") != revision:
                raise ConnectionError("The folder or configuration changed. Inspect again before connecting.")
            if kind in ("crm", "self"):
                _idle()

        onboard.config_set(_validate=validate, **plan["updates"])
        if kind == "crm":
            crm.invalidate()
            crmindex.invalidate()
        return {"connected": request.get("mode") != "disconnect", "kind": kind,
                "summary": plan["summary"], "warnings": plan["warnings"]}


def status():
    """Cheap configured/effective storage summary; no CRM scan on every poll."""
    cfg = _config()
    root = _path(cfg.get("crm_root") or settings.DEFAULTS["crm_root"])
    rows = cfg.get("reader_sources") or []
    return {"version": 1, "crm": {"root": str(root), "available": root.is_dir(),
                                   "registry_present": (root / "people.json").is_file()},
            "self_record": _self_info(cfg, _self_root(cfg, root)),
            "reader_sources": [{**row, "available": _path(row["path"]).is_dir()}
                               for row in rows if isinstance(row, dict) and row.get("path")]}

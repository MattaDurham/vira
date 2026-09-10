"""Private owner tasks from self messages and non-contact correspondence.

Known people's relationship loops belong in CRM. A bill, service appointment,
or owner reminder has no reason to create a person or rewrite the self-record.
This local store gives those commitments the same evidence and lifecycle.
"""
import copy
import hashlib
import json
import os
from datetime import datetime, timezone

from . import jsonstore, settings
from .filelock import locked

STORE = settings.ROOT / "data" / "assistant-commitments.json"


def _read():
    try:
        state = json.loads(STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"subjects": {}}
    if not isinstance(state, dict) or not isinstance(state.get("subjects"), dict):
        raise ValueError("Owner commitment state needs repair")
    for subject in state["subjects"].values():
        if not isinstance(subject, dict) or not isinstance(subject.get("open_loops"), list):
            raise ValueError("Owner commitment state needs repair")
    return state


def all_subjects():
    return _read()["subjects"]


def snapshot(subject_key):
    return all_subjects().get(subject_key, {"open_loops": []})


def import_candidates(sources):
    """Inbox tasks proven to originate in messages now assigned to a person."""
    identifiers = {(source["channel"], source["id"]) for source in sources}
    candidates = []
    for subject_key, subject in all_subjects().items():
        if not subject_key.startswith("sender:"):
            continue
        for loop in subject["open_loops"]:
            if (loop.get("source") == "vira-assistant" and loop.get("assistant_key")
                    and any((ref.get("channel"), ref.get("id")) in identifiers
                            for ref in loop.get("evidence") or [])):
                candidates.append({"subject_key": subject_key, "loop": copy.deepcopy(loop)})
    return candidates


def finish_import(candidates):
    """Remove only the exact source snapshots copied successfully into CRM.

    A concurrent owner edit stays here and forces another read; an interrupted
    import can retry safely after its destination write already succeeded.
    """
    if not candidates:
        return
    changed = False
    with locked(STORE):
        state = _read()
        for candidate in candidates:
            subject = state["subjects"].get(candidate["subject_key"])
            if not subject:
                continue
            loop = candidate["loop"]
            current = next((row for row in subject["open_loops"]
                            if row.get("assistant_key") == loop["assistant_key"]), None)
            if current is None:
                continue
            if current != loop:
                raise ValueError("Inbox task changed while it was being assigned to a contact")
            subject["open_loops"].remove(current)
            if not subject["open_loops"]:
                del state["subjects"][candidate["subject_key"]]
            changed = True
        if changed:
            jsonstore.write_atomic(STORE, state, indent=1, ensure_ascii=False)


def _norm(value):
    return " ".join(str(value or "").casefold().split())


def _evidence(item):
    evidence = item.get("evidence")
    if (not isinstance(evidence, list) or not evidence
            or any(not isinstance(e, dict) or not e.get("id")
                   or not e.get("quote") or not e.get("when") or not e.get("channel")
                   for e in evidence)):
        raise ValueError("A commitment needs source evidence")
    return evidence


def _newest(evidence):
    times = []
    for ref in evidence:
        try:
            value = datetime.fromisoformat(str(ref["when"]).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            times.append(value.timestamp())
        except (KeyError, TypeError, ValueError):
            continue
    return max(times, default=0)


def merge(subject_key, person_name, update, expected=None):
    if os.environ.get("VIRA_PASSIVE") or settings.sandboxed() or settings.fixture_mode():
        raise ValueError("Automatic commitments are disabled on a test instance")
    if not isinstance(subject_key, str) or not subject_key or len(subject_key) > 1000:
        raise ValueError("A stable subject is required")
    expected = expected or {"open_loops": []}
    counts = {"facts": 0, "summary": 0, "loops": 0, "closed": 0, "revised": 0, "held_deadlines": 0}
    now = datetime.now(timezone.utc).isoformat()
    with locked(STORE):
        state = _read()
        subject = state["subjects"].setdefault(subject_key, {
            "person_name": str(person_name or "Inbox")[:200], "open_loops": []})
        loops = subject["open_loops"]
        previous = {_norm(lp.get("what")): lp for lp in expected.get("open_loops", [])
                    if isinstance(lp, dict)}
        previous_keys = {lp.get("assistant_key"): lp for lp in expected.get("open_loops", [])
                         if isinstance(lp, dict) and lp.get("assistant_key")}
        changed_keys = set()
        for candidate in update.get("loops", []):
            if candidate.get("owed_by") != "me":
                continue
            normalized = _norm(candidate.get("what"))
            if not normalized:
                continue
            evidence = _evidence(candidate)
            key = hashlib.sha256(f"{subject_key}:{normalized}".encode("utf-8")).hexdigest()[:24]
            current = next((lp for lp in loops if _norm(lp.get("what")) == normalized
                            or lp.get("assistant_key") == key), None)
            if current:
                if (current.get("status") == "closed" or current.get("edited") or current.get("due_updated_by_owner")
                        or current.get("source") != "vira-assistant"
                        or current != (previous.get(normalized) or previous_keys.get(key))):
                    continue
                if ((candidate.get("due") or candidate.get("deadline_review"))
                        and (candidate.get("due") != current.get("due")
                             or candidate.get("deadline_review") != current.get("deadline_review"))
                        and _newest(evidence) > _newest(current.get("evidence") or [])):
                    old_refs = current.get("evidence") or []
                    merged = old_refs + [copy.deepcopy(e) for e in evidence if e not in old_refs]
                    current.update(due=candidate.get("due"), evidence=merged,
                                   due_quote=candidate.get("due_quote"),
                                   due_evidence=copy.deepcopy(candidate.get("due_evidence")),
                                   updated_at=now)
                    if candidate.get("deadline_review"):
                        current["deadline_review"] = copy.deepcopy(candidate["deadline_review"])
                        counts["held_deadlines"] += 1
                    else:
                        current.pop("deadline_review", None)
                    changed_keys.add(key)
                    counts["revised"] += 1
                continue
            entry = copy.deepcopy(candidate)
            entry.update(source="vira-assistant", status="open", updated_at=now,
                         quote=evidence[0]["quote"], channel=evidence[0]["channel"],
                         assistant_key=key)
            entry.setdefault("since", evidence[0]["when"][:10])
            loops.append(entry)
            changed_keys.add(key)
            if entry.get("deadline_review"):
                counts["held_deadlines"] += 1
            counts["loops"] += 1
        for closing in update.get("closed_loops", []):
            evidence = _evidence(closing)
            current = next((lp for lp in loops if lp.get("what") == closing.get("what")), None)
            if (current and current.get("status") != "closed" and not current.get("edited")
                    and current.get("source") == "vira-assistant"
                    and (current == previous.get(_norm(current.get("what")))
                         or current.get("assistant_key") in changed_keys)
                    and _newest(evidence) >= _newest(current.get("evidence") or [])):
                current.update(status="closed", closed_on=now[:10],
                               closed_evidence=copy.deepcopy(evidence), updated_at=now)
                counts["closed"] += 1
        if any(counts.values()):
            subject["updated_at"] = now
            jsonstore.write_atomic(STORE, state, indent=1, ensure_ascii=False)
    return counts


def close(subject_key, commitment_key):
    """An explicit owner action, also usable with synthetic preview tasks."""
    with locked(STORE):
        state = _read()
        subject = state["subjects"].get(subject_key)
        target = next((lp for lp in (subject or {}).get("open_loops", [])
                       if lp.get("assistant_key") == commitment_key and lp.get("status") != "closed"), None)
        if target is None:
            raise KeyError(commitment_key)
        target.update(status="closed", closed_on=datetime.now(timezone.utc).date().isoformat())
        jsonstore.write_atomic(STORE, state, indent=1, ensure_ascii=False)
        return target


def set_due(subject_key, assistant_key, due):
    """An explicit owner correction; evidence remains the task's origin."""
    if not isinstance(assistant_key, str) or not assistant_key:
        raise ValueError("An exact assistant task key is required")
    if not isinstance(due, str) or not due.strip():
        raise ValueError("A deadline is required")
    with locked(STORE):
        state = _read()
        subject = state["subjects"].get(subject_key)
        target = next((loop for loop in (subject or {}).get("open_loops", [])
                       if loop.get("assistant_key") == assistant_key), None)
        if target is None or target.get("status") == "closed":
            raise KeyError(assistant_key)
        now = datetime.now(timezone.utc).isoformat()
        target.update(due=due.strip(), due_updated_by_owner=now, updated_at=now)
        target.pop("deadline_review", None)
        subject["updated_at"] = now
        jsonstore.write_atomic(STORE, state, indent=1, ensure_ascii=False)
        return target

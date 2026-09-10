"""Standing assistant: commitments, quiet reminders, and visible coverage.

Contact intelligence owns evidence extraction; CRM profiles remain canonical.
This module derives reminders from those profiles and stores only delivery and
snooze state locally. A send is successful only when the sender says it was.
An interrupted send is held for review instead of being blindly repeated.
"""
import hashlib
import json
import os
import threading
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import data as crm
from . import commitments, jsonstore, notify, settings
from .filelock import locked

STATE = settings.ROOT / "data" / "assistant-state.json"
_tick_lock = threading.Lock()
_thread = None
_worker_error = None

DEFAULT_CONFIG = {
    "assistant_enabled": False,
    "assistant_contact_updates": True,
    "assistant_notify": False,
    "assistant_quiet_start": 22,
    "assistant_quiet_end": 8,
    "assistant_timezone": "",
    "assistant_calendar_auto_create": False,
    "assistant_calendar_name": "",
    "assistant_calendar_id": "",
    "assistant_calendar_work_start": 9,
    "assistant_calendar_work_end": 17,
    "assistant_calendar_block_minutes": 30,
    "assistant_stale_days": 3,
    "assistant_notify_daily_cap": 3,
    "assistant_due_soon_hours": 24,
    "assistant_debounce_s": 90,
    "assistant_batch_size": 3,
    "assistant_catchup_days": 14,
    "mail_body_index": False,
}
_RANGES = {
    "assistant_quiet_start": (0, 23), "assistant_quiet_end": (0, 23),
    "assistant_stale_days": (1, 90), "assistant_notify_daily_cap": (1, 20),
    "assistant_due_soon_hours": (1, 168), "assistant_debounce_s": (0, 3600),
    "assistant_batch_size": (1, 10), "assistant_catchup_days": (1, 90),
    "assistant_calendar_work_start": (0, 23), "assistant_calendar_work_end": (1, 24),
    "assistant_calendar_block_minutes": (15, 240),
}


def config():
    raw = settings.raw()
    return {k: raw.get(k, v) for k, v in DEFAULT_CONFIG.items()}


def save_config(updates):
    if not isinstance(updates, dict) or set(updates) - DEFAULT_CONFIG.keys():
        raise ValueError("unknown assistant setting")
    for key, value in updates.items():
        default = DEFAULT_CONFIG[key]
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ValueError(f"{key} must be true or false")
        elif key in _RANGES:
            lo, hi = _RANGES[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{key} must be an integer from {lo} to {hi}")
        elif not isinstance(value, str) or len(value) > 200:
            raise ValueError(f"{key} must be a short string")
        if key == "assistant_timezone" and value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("use a valid IANA timezone, or leave it empty") from exc
    proposed = dict(config(), **updates)
    if proposed["assistant_calendar_work_start"] >= proposed["assistant_calendar_work_end"]:
        raise ValueError("Calendar work hours must end after they start")
    jsonstore.mutate(settings.CONFIG_PATH, lambda s: s.update(updates), {})
    return config()


def _now():
    return datetime.now(timezone.utc)


def _zone(cfg):
    name = cfg["assistant_timezone"]
    return ZoneInfo(name) if name else None


def _local(now, cfg):
    return now.astimezone(_zone(cfg))


def _date(value, cfg, end_of_day=False):
    """Date-only deadlines run through the end of the owner's local day."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 10:
            d = datetime.strptime(value, "%Y-%m-%d").date()
            point = datetime.combine(d, time.max if end_of_day else time.min)
        else:
            point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if point.tzinfo is None:
            zone = _zone(cfg)
            point = point.replace(tzinfo=zone) if zone else point.astimezone()
        return point
    except (ValueError, TypeError, OverflowError):
        return None


def quiet(now=None, cfg=None):
    cfg = cfg or config()
    hour = _local(now or _now(), cfg).hour
    start, end = cfg["assistant_quiet_start"], cfg["assistant_quiet_end"]
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def enabled():
    return (config()["assistant_enabled"] is True
            and not os.environ.get("VIRA_PASSIVE")
            and not os.environ.get("VIRA_SANDBOX")
            and not settings.fixture_mode())


def _state():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"reminders": {}, "deliveries": []}
    if (not isinstance(state, dict)
            or not isinstance(state.get("reminders"), dict)
            or not isinstance(state.get("deliveries"), list)
            or not all(isinstance(r, dict) for r in state["reminders"].values())):
        raise ValueError("Assistant delivery state needs repair")
    return state


def _mutate(fn):
    with locked(STATE):
        state = _state()
        fn(state)
        jsonstore.write_atomic(STATE, state, indent=1)
        return state


def _key(pid, loop):
    origin = loop.get("assistant_origin")
    if isinstance(origin, dict) and origin.get("subject_key") and origin.get("assistant_key"):
        pid = origin["subject_key"]
        identity = origin["assistant_key"]
        return hashlib.sha256(f"{pid}:{identity}".encode("utf-8")).hexdigest()[:24]
    identity = loop.get("assistant_key") or " ".join(
        str(loop.get("what") or "").casefold().split())
    return hashlib.sha256(f"{pid}:{identity}".encode("utf-8")).hexdigest()[:24]


def commitment_records():
    """Canonical loops with subject identity, for reminders and scheduling."""
    corpus = crm._load()
    rows = []
    for pid, profile in corpus.get("profiles", {}).items():
        person = corpus.get("by_id", {}).get(pid, {})
        if person.get("class_hint") == "company":
            continue
        rows.extend({"subject_key": pid, "person_id": pid,
                     "person_name": person.get("name") or profile.get("name") or pid,
                     "loop": loop} for loop in profile.get("open_loops") or []
                    if isinstance(loop, dict))
    for key, profile in commitments.all_subjects().items():
        for loop in profile.get("open_loops") or []:
            if isinstance(loop, dict):
                rows.append({"subject_key": key, "person_id": None,
                             "person_name": profile.get("person_name") or "Inbox", "loop": loop})
    return rows


def reminders(now=None, include_snoozed=False):
    now, cfg, saved = now or _now(), config(), _state().get("reminders", {})
    rows = []
    for record in commitment_records():
        loop = record["loop"]
        if loop.get("status") == "closed":
            continue
        what = str(loop.get("what") or "").strip()
        if not what:
            continue
        rid = _key(record["subject_key"], loop)
        state = saved.get(rid, {})
        snooze = _date(state.get("snoozed_until"), cfg)
        if snooze and snooze > now and not include_snoozed:
            continue
        deadline = _date(loop.get("due"), cfg, end_of_day=True)
        since = _date(loop.get("since"), cfg)
        priority, reason, stage = "normal", "Open commitment", "open"
        if loop.get("deadline_review"):
            priority, reason, stage = "high", "Check deadline", "review"
        elif deadline and deadline < now:
            priority, reason, stage = "high", "Past the recorded due date", "overdue"
        elif deadline and deadline <= now + timedelta(hours=cfg["assistant_due_soon_hours"]):
            priority, reason, stage = "high", "Due soon", "due"
        elif not deadline and since and now - since >= timedelta(days=cfg["assistant_stale_days"]):
            priority, reason, stage = "medium", "Still open; worth checking", "stale"
        evidence = loop.get("evidence") if isinstance(loop.get("evidence"), list) else []
        rows.append({
                "id": rid, "person_id": record["person_id"],
                "person_name": record["person_name"], "subject_key": record["subject_key"],
                "assistant_key": loop.get("assistant_key"), "due_quote": loop.get("due_quote"),
                "source": loop.get("source"),
                "deadline_review": loop.get("deadline_review"),
                "what": what, "owed_by": loop.get("owed_by"),
                "due": loop.get("due"), "since": loop.get("since"),
                "reason": reason, "priority": priority, "stage": stage,
                "status": "snoozed" if snooze and snooze > now else "open",
                "snoozed_until": state.get("snoozed_until"),
                "evidence": evidence,
                # Old synthesized loops are useful to review, but must not
                # trigger a storm of newly enabled phone notifications.
                "notify_eligible": (loop.get("source") == "vira-assistant"
                                    and bool(evidence) and loop.get("owed_by") == "me"
                                    and stage not in ("open", "review")),
            })
    rank = {"high": 0, "medium": 1, "normal": 2}
    rows.sort(key=lambda r: (rank[r["priority"]], r["owed_by"] != "me",
                             str(r["due"] or r["since"] or ""), r["id"]))
    return rows


def reminder_action(rid, action, hours=24, due=None):
    row = next((r for r in reminders(include_snoozed=True) if r["id"] == rid), None)
    if row is None:
        raise KeyError(rid)
    if action == "done":
        if row["person_id"]:
            crm.update_loop(row["person_id"], row["what"], "close")
        else:
            commitments.close(row["subject_key"], row["assistant_key"])
    elif action == "date":
        if (not isinstance(due, str) or len(due) != 10 or not _date(due, config())):
            raise ValueError("Use a valid due date in YYYY-MM-DD format")
        if row["person_id"]:
            crm.set_loop_due(row["person_id"], row["assistant_key"], due)
        else:
            commitments.set_due(row["subject_key"], row["assistant_key"], due)
    elif action == "snooze":
        if type(hours) is not int or not 1 <= hours <= 24 * 30:
            raise ValueError("snooze hours must be from 1 to 720")
    else:
        raise ValueError("action must be done, snooze, or date")
    def save(store):
        entry = store.setdefault("reminders", {}).setdefault(rid, {})
        entry["updated_at"] = _now().isoformat()
        if action == "snooze":
            entry["snoozed_until"] = (_now() + timedelta(hours=hours)).isoformat()
        elif action == "done":
            entry["done_at"] = _now().isoformat()
            entry["delivery"] = "resolved"
    _mutate(save)
    return {"id": rid, "status": {"done": "closed", "snooze": "snoozed", "date": "open"}[action],
            "due": due if action == "date" else row.get("due")}


def _notify(now):
    cfg = config()
    if not enabled() or not cfg["assistant_notify"] or quiet(now, cfg):
        return
    ready = notify.config()
    if not ready["enabled"] or not ready["handle"]:
        return
    day = _local(now, cfg).date().isoformat()
    for row in reminders(now):
        if not row["notify_eligible"]:
            continue
        current_cfg = config()
        if (not enabled() or not current_cfg["assistant_notify"]
                or quiet(_now(), current_cfg)):
            break
        claimed = []
        stage_key = f"{row['stage']}:{row['due'] or row['since']}"
        def claim(store):
            entry = store.setdefault("reminders", {}).setdefault(row["id"], {})
            deliveries = store.setdefault("deliveries", [])
            # sending can mean the process died after a successful delivery;
            # repeating it without a receipt would risk a duplicate message.
            if entry.get("delivery") in ("sending", "uncertain"):
                return
            if stage_key in entry.get("notified", []):
                return
            retry = _date(entry.get("retry_after"), cfg)
            if retry and retry > now:
                return
            today = [d for d in deliveries if d.get("day") == day]
            if len(today) >= cfg["assistant_notify_daily_cap"]:
                return
            entry.update({"delivery": "sending", "attempted_at": now.isoformat()})
            deliveries.append({"id": row["id"], "day": day, "at": now.isoformat(), "stage": stage_key})
            store["deliveries"] = deliveries[-200:]
            claimed.append(True)
        _mutate(claim)
        if not claimed:
            continue
        # Re-read after reserving: a completion/snooze during extraction wins.
        current_cfg = config()
        fresh = next((r for r in reminders(now) if r["id"] == row["id"]), None)
        if (not enabled() or not current_cfg["assistant_notify"] or quiet(_now(), current_cfg)
                or not fresh or not fresh["notify_eligible"]
                or f"{fresh['stage']}:{fresh['due'] or fresh['since']}" != stage_key):
            def cancel(store):
                store["reminders"][row["id"]]["delivery"] = "cancelled"
                store["deliveries"] = [d for d in store["deliveries"]
                                       if not (d["id"] == row["id"] and d["at"] == now.isoformat()
                                               and d["stage"] == stage_key)]
            _mutate(cancel)
            continue
        row = fresh
        due = f" (due {row['due']})" if row["due"] else ""
        text = (f"{row['reason']}: {row['what'][:190]}{due}. "
                f"With {row['person_name']}. Check Attention > Day to close or snooze it.")
        receipt = notify.assistant_send(text, ref={
            "kind": "assistant", "reminder_id": row["id"],
            "person_id": row["person_id"], "what": row["what"],
        })
        def record(store):
            entry = store["reminders"][row["id"]]
            delivery = receipt.get("status", "uncertain")
            entry["delivery"] = delivery
            entry["delivery_detail"] = receipt.get("detail") or ""
            if delivery == "sent":
                entry.setdefault("notified", []).append(stage_key)
                entry["notified"] = entry["notified"][-20:]
                entry["last_sent"] = now.isoformat()
            elif delivery in ("failed", "blocked"):
                entry["retry_after"] = (now + timedelta(hours=1)).isoformat()
        _mutate(record)


def _calendar_plan_queue(records, previous, state, now):
    """Fair planning order; cooldown-only visits must not consume the batch.

    The planner deliberately retains unsuccessful drafts and can also
    decline a task before a draft exists. Persisted attempts cover both
    cases, so one unplannable early deadline cannot monopolize each cycle.
    """
    from . import calendarplan
    known = {(d.get("subject_key"), d.get("commitment_key")): d for d in previous
             if d.get("schedule_kind") == "commitment"}
    attempted = state.get("calendar_planning_attempts", {})
    if not isinstance(attempted, dict):
        raise ValueError("Calendar planning attempt state needs repair")
    cfg = config()
    queue = []
    for record in records:
        loop = record["loop"]
        if (loop.get("source") != "vira-assistant" or loop.get("status") == "closed"
                or loop.get("owed_by") != "me" or not loop.get("due")
                or loop.get("deadline_review") or not loop.get("assistant_key")):
            continue
        prior = known.get(calendarplan.commitment_identity(loop, record["subject_key"]))
        if prior and (prior.get("status") in ("created", "dismissed", "creating", "uncertain")
                      or (prior.get("status") == "suggested" and prior.get("can_create"))):
            continue
        key = _key(record["subject_key"], loop)
        stamps = [_date(attempted.get(key), cfg)]
        if prior:
            stamps.append(_date(prior.get("planned_at") or prior.get("updated_at"), cfg))
        latest = max((stamp for stamp in stamps if stamp), default=None)
        if latest and now - latest < timedelta(minutes=5):
            continue
        queue.append((latest.timestamp() if latest else 0,
                      str(loop.get("due") or ""), key, record))
    queue.sort(key=lambda item: item[:3])
    return [(key, record) for _, _, key, record in queue]


def tick():
    global _worker_error
    if not enabled() or not _tick_lock.acquire(blocking=False):
        return
    try:
        from . import calendarplan, contactintel
        _mutate(lambda s: s.update(last_run=_now().isoformat(), last_error=None))
        errors = []
        try:
            contactintel.tick()
        except Exception as exc:
            errors.append(f"Message processing needs attention ({type(exc).__name__}).")
        try:
            if calendarplan.destinations().get("selected"):
                previous = calendarplan.list_drafts(include_closed=True)
                records = commitment_records()
                active_keys = {_key(r["subject_key"], r["loop"]) for r in records
                               if r["loop"].get("status") != "closed"
                               and r["loop"].get("source") == "vira-assistant"
                               and r["loop"].get("owed_by") == "me"}
                def prune_attempts(state):
                    attempts = state.setdefault("calendar_planning_attempts", {})
                    if not isinstance(attempts, dict):
                        raise ValueError("Calendar planning attempt state needs repair")
                    state["calendar_planning_attempts"] = {k: v for k, v in attempts.items() if k in active_keys}
                saved = _mutate(prune_attempts)
                queue = _calendar_plan_queue(records, previous, saved, _now())
                for key, record in queue[:config()["assistant_batch_size"]]:
                    if not enabled():
                        break
                    # A None result or exception is still a real attempt.
                    # Record before the call, including when a process exits.
                    _mutate(lambda state, k=key: state.setdefault("calendar_planning_attempts", {}).update(
                        {k: _now().isoformat()}))
                    try:
                        calendarplan.plan_commitment(record["loop"], record["subject_key"], record["person_name"])
                    except Exception as exc:
                        errors.append(f"A calendar task needs attention ({type(exc).__name__}).")
            if config()["assistant_calendar_auto_create"]:
                ready = [d for d in calendarplan.list_drafts()
                         if d.get("status") in ("suggested", "blocked") and d.get("can_create")]
                for draft in ready[:10]:
                    if not enabled() or not config()["assistant_calendar_auto_create"]:
                        break
                    calendarplan.create_owner_event(draft["id"])
        except Exception as exc:
            errors.append(f"Calendar planning needs attention ({type(exc).__name__}).")
        try:
            _notify(_now())
        except Exception as exc:
            errors.append(f"Reminder delivery needs attention ({type(exc).__name__}).")
        _worker_error = " ".join(errors) or None
        def finish(state):
            state["last_error"] = _worker_error
            if not errors:
                state["last_success"] = _now().isoformat()
        _mutate(finish)
    except Exception as exc:  # one failed cycle stays visible and retries
        _worker_error = f"Assistant cycle failed ({type(exc).__name__}); check source status."
        try:
            _mutate(lambda s: s.update(last_error=_worker_error))
        except (OSError, ValueError):
            _worker_error = "Assistant delivery state needs repair; reminders are held."
    finally:
        _tick_lock.release()


def status():
    from . import calendarplan, contactintel
    cfg = config()
    try:
        state = _state()
        reminder_rows = reminders()
    except (OSError, ValueError):
        state = {"last_error": "Assistant delivery state needs repair; reminders are held."}
        reminder_rows = []
    try:
        contact = contactintel.status()
    except Exception as exc:
        contact = {"last_error": f"Message processing status unavailable ({type(exc).__name__})."}
    try:
        calendar = {"drafts": calendarplan.list_drafts()}
    except Exception as exc:
        calendar = {"drafts": [], "error": f"Calendar drafts need repair ({type(exc).__name__})."}
    try:
        calendar["destinations"] = calendarplan.destinations()
    except Exception:
        calendar["destinations"] = {"available": False, "calendars": [], "selected": None,
                                    "error": "Calendar selection could not be checked."}
    notices = []
    if not contact.get("mail_body_index"):
        notices.append("Email body indexing is off; full email history and sent replies are not covered.")
    if not contact.get("index_available"):
        notices.append("The message index is not available yet.")
    if not settings.fixture_mode() and not os.environ.get("VIRA_PASSIVE"):
        try:
            from . import mail
            accounts = mail.accounts_view()["accounts"]
            uncovered = sum(1 for a in accounts if a.get("state") != "ok" or a.get("stale"))
            if uncovered:
                notices.append(f"{uncovered} mailbox(es) have unavailable or stale coverage; check Mail in Config.")
        except Exception:
            notices.append("Mailbox coverage could not be checked; review Mail in Config.")
    drafts = calendar.get("drafts", [])
    uncertain_calendar = sum(1 for d in drafts if d.get("status") in ("creating", "uncertain"))
    if uncertain_calendar:
        notices.append(f"{uncertain_calendar} calendar write outcome(s) need checking; automatic retry is held.")
    unscheduled = sum(1 for d in drafts if d.get("schedule_kind") == "commitment"
                      and d.get("status") not in ("created", "dismissed", "creating", "uncertain")
                      and not d.get("start"))
    if unscheduled:
        notices.append(f"{unscheduled} task(s) have no calendar block yet; review their scheduling details.")
    uncertain = sum(1 for r in state.get("reminders", {}).values()
                    if r.get("delivery") in ("sending", "uncertain"))
    if uncertain:
        notices.append(f"{uncertain} text delivery outcome(s) need checking; automatic resend is held.")
    failed = sum(1 for r in state.get("reminders", {}).values() if r.get("delivery") == "failed")
    if failed:
        notices.append(f"{failed} text reminder(s) failed; delivery will retry within the daily limit.")
    return {
        "enabled": cfg["assistant_enabled"], "active": enabled(), "settings": cfg,
        "passive": bool(os.environ.get("VIRA_PASSIVE")), "fixture": settings.fixture_mode(),
        "worker_running": bool(_thread and _thread.is_alive()),
        "notification_ready": bool(notify.config()["enabled"] and notify.config()["handle"]),
        "contact": contact, "reminders": reminder_rows,
        "calendar": calendar, "coverage": notices,
        "last_run": state.get("last_run"), "last_success": state.get("last_success"),
        "last_error": state.get("last_error") or _worker_error,
    }


def attention_rows():
    if not config()["assistant_enabled"]:
        return []
    from . import attention
    snap = status()
    out = []
    error = (snap["last_error"] or snap["contact"].get("last_error")
             or snap["calendar"].get("error"))
    if not error and snap["contact"].get("errors"):
        error = "Some messages could not be processed; review the assistant's source status."
    if error:
        out.append(attention._row("health:assistant", "assistant", "blocked", True,
                                  "Assistant needs attention", str(error), "Open assistant",
                                  activity_at=snap["last_run"]))
    for draft in snap["calendar"].get("drafts", []):
        if draft.get("status") not in ("creating", "uncertain", "blocked") and not (
                draft.get("schedule_kind") == "commitment" and not draft.get("start")):
            continue
        out.append(attention._row(
            "assistant:calendar:" + draft["id"], "assistant", "blocked", True,
            draft.get("title") or "Calendar block needs attention",
            draft.get("reason") or "Review calendar scheduling status", "Open assistant",
            activity_at=draft.get("updated_at") or draft.get("created_at")))
    for row in snap["reminders"]:
        if row["priority"] != "high" or row["owed_by"] != "me":
            continue
        out.append(attention._row(
            "assistant:" + row["id"], "assistant", row["stage"], True,
            row["what"], f"{row['person_name']} - {row['reason']}", "Review",
            activity_at=row["due"] or row["since"], reminder_id=row["id"],
            trigger=f"assistant:{row['id']}@{row['stage']}:{row['due']}"))
    return out


def start():
    global _thread
    if os.environ.get("VIRA_PASSIVE") or os.environ.get("VIRA_SANDBOX"):
        return None
    if _thread and _thread.is_alive():
        return _thread
    def run():
        event = threading.Event()
        while not event.wait(60):
            tick()
    _thread = threading.Thread(target=run, name="vira-assistant", daemon=True)
    _thread.start()
    return _thread

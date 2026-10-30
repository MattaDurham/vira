"""Routines — Vira's standing agent loops.

The "different types of loops" engine: a routine is a recurring agent
dispatch — a prompt (or a whole circuit) on a cadence. Kinds:

  muse    — the idea generator. Composed server-side at dispatch: reads
            the current backlog + radar, searches the vault, and PROPOSES
            new ideas via the propose_idea native tool. Proposals land in
            the Ideas queue as status "proposed" — nothing runs without
            the owner's approval. The permissioned-autonomy loop.
  watch   — a watcher: run a prompt, notify when something needs eyes.
  digest  — a synthesizer: periodic summary/analysis runs.
  circuit — dispatch a whole circuits.py pipeline on a cadence.
  custom  — any prompt, any cadence.
  command — run a program (an argv, a folder, an environment) as its own
            detached process: a capture script, an export. No model call
            by Vira. Each run's record and log copy live in
            server/routinerun.py's store, and The Forge lists them.

A circuit row is a Schedule card on a Flow's board: its targets are the
cards it is wired to, and a run it starts covers those and what is
downstream of them (circuits.start_run entry). A row with no targets key
predates that and runs the whole board.

Archive: a row with archived set never fires; archive_routine remembers
whether it was on, and restore_routine puts that back.

Cadence: every_hours (float) OR daily_at "HH:MM" (local), which with a
weekday ("sunday") fires once a week, on that day at that time. The Scheduler
thread (60s tick) dispatches due routines as normal durable jobs / circuit
runs — terminals, ledger rows, restart survival all apply — records
last_run/last_job/last_status, skips while the previous run is still
live, and iMessages the owner on completion when notify is set.

Store: data/routines.json (atomic writes; server-only writer).
"""

from . import instance, modulemodels
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import re
import shlex

from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "routines.json"

KINDS = ("muse", "watch", "digest", "circuit", "custom", "command")
# datetime.weekday() order, so WEEKDAYS[now.weekday()] names today
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")
TICK_S = 60

_lock = threading.Lock()

SEEDS = [
    {
        "id": "muse",
        "name": "Muse — propose ideas",
        "kind": "muse",
        "daily_at": "07:30",
        "enabled": True,
        "notify": True,
        "model": "",
        "description": "Every morning Vira studies the backlog, the radar, "
                       "and the vault, then proposes 1-3 new ideas for the "
                       "owner to approve or decline.",
    },
    {
        # id kept from the introductions era: _load() never prunes store
        # rows, so a new id would orphan the live one (see RESEEDS below)
        "id": "intro-scout",
        "name": "Grouping scout — refresh groupings",
        "kind": "custom",
        "every_hours": 168,
        "enabled": True,
        "notify": False,
        "model": "",
        "prompt": "__refresh_groupings__",  # internal dispatch, no session
        "description": "Weekly refresh of the Radar window's groupings — "
                       "who to convene around what, and the conversation "
                       "markers riding on what people just shared.",
    },
    {
        "id": "atlas-refresh",
        "name": "Atlas — rebuild the contact graph",
        "kind": "digest",
        "every_hours": 168,
        "enabled": True,
        "notify": False,
        "model": "",
        "prompt": "__refresh_atlas__",    # internal dispatch, no session
        "description": "Weekly rebuild of the Contact Atlas materialized "
                       "graph (edges, degrees, clusters, edge narration).",
    },
    {
        "id": "pivot-scout",
        "name": "Pivot scout — refresh reconnect targets",
        "kind": "custom",
        "every_hours": 168,
        "enabled": True,
        "notify": False,
        "model": "",
        "prompt": "__refresh_reconnect__",   # internal dispatch, no session
        "description": "Weekly re-rank of dormant contacts against the "
                       "live job-search target picture.",
    },
    {
        "id": "room-scout",
        "name": "Room scout — refresh the reading rooms",
        "kind": "digest",
        "every_hours": 168,
        "enabled": True,
        "notify": False,   # build() pings per-room additions itself
        "model": "",
        "cwd": str(ROOT),
        "prompt": "__room_scout__",       # composed at dispatch (readingroom)
        "description": "Weekly sweep over every reading room: research what "
                       "is new on each subject since the last refresh and "
                       "rebuild it in place — the owner is pinged when a "
                       "room gains items, the applications-watch pattern.",
    },
    {
        "id": "lesson-recurrence",
        "name": "Lesson recurrence — count what keeps being broken",
        "kind": "digest",
        "every_hours": 168,
        "enabled": True,
        "notify": False,   # a tier-1 recurrence pings on its own edge
        "model": "",
        "prompt": "__lesson_recurrence__",  # in-process, but rung 2 spends
                                            # model calls — NOT an
                                            # _INTERNAL_TOKENS row, so the
                                            # _ai_ready gate applies
        "description": "Weekly pass over the corrections ledger and the "
                       "session retros: how many times has each standing "
                       "rule actually been broken since it was written, "
                       "and which tier-2 rules have earned a real "
                       "mechanism.",
    },
    {
        "id": "system-map",
        "name": "System map — refresh from the change log",
        "kind": "digest",
        "every_hours": 168,
        "enabled": True,
        "notify": False,
        "model": "",
        "cwd": str(ROOT),
        "prompt": "__module_map__",       # composed at dispatch (modulemap)
        "description": "Weekly pass over the Vira change log: bring the "
                       "system-map module registry up to date with what "
                       "actually shipped, via the validated "
                       "update_module_map tool.",
    },
    {
        "id": "room-ingest",
        "name": "Room ingest — stage material, link summaries",
        "kind": "digest",
        "every_hours": 24,
        "enabled": True,
        "notify": False,
        "model": "",
        "prompt": "__room_ingest__",      # internal dispatch, no session
        "description": "Daily full-ingest pass over every reading room: "
                       "stage new items' material (captions, article text) "
                       "into the vault's raw/reading-room/, link items "
                       "whose source-summary has landed, and retire their "
                       "obsolete pointer notes. Deterministic — the "
                       "synthesis itself belongs to the vault's own "
                       "nightly ingest.",
    },
    {
        "id": "orphan-sweep",
        "name": "Orphan sweep — unlanded work",
        "kind": "digest",
        "every_hours": 24,
        "enabled": True,
        "notify": False,
        "model": "",
        "prompt": "__orphan_sweep__",     # internal dispatch, no session
        "description": "Daily inventory of every worktree and branch for "
                       "work that never landed — uncommitted changes, "
                       "unpushed commits, unmerged branches, stalled "
                       "sessions — surfaced in The Forge > Runs.",
    },
]


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A seeded routine that gets renamed in a release needs its stored row
# brought along — seeds only apply to ids the store has never seen, so an
# instance that already has the row would keep the superseded name
# forever. id -> the exact old name that may be overwritten from SEEDS.
RESEEDS = {"intro-scout": "Intro scout — refresh introductions"}


def _load():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict) or "routines" not in s:
        s = {"routines": []}
    have = {r["id"] for r in s["routines"]}
    changed = False
    for seed in SEEDS:
        if seed["id"] not in have:
            r = dict(seed)
            r.setdefault("created", _now_iso())
            r.setdefault("last_run", None)
            r.setdefault("last_job", None)
            r.setdefault("last_run_id", None)
            r.setdefault("last_status", None)
            s["routines"].append(r)
            changed = True
            continue
        stale = RESEEDS.get(seed["id"])
        if not stale:
            continue
        for r in s["routines"]:
            # only an untouched row is reseeded — a name the owner edited
            # is theirs and stays
            if r["id"] == seed["id"] and r.get("name") == stale:
                r["name"] = seed["name"]
                r["description"] = seed["description"]
                r["prompt"] = seed["prompt"]
                changed = True
    if changed:
        _save(s)
    return s


def _save(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def list_routines():
    with _lock, locked(STORE):
        return _load()["routines"]


def get_routine(rid):
    return next((r for r in list_routines() if r["id"] == rid), None)


def save_routine(data, rid=None):
    """Create (rid=None) or update a routine from UI fields."""
    with _lock, locked(STORE):
        s = _load()
        if rid:
            r = next((r for r in s["routines"] if r["id"] == rid), None)
            if not r:
                raise KeyError(rid)
        else:
            r = {"id": "rt_" + uuid.uuid4().hex[:8], "created": _now_iso(),
                 "instance_id": instance.id(),
                 "last_run": None, "last_job": None, "last_run_id": None,
                 "last_status": None}
            s["routines"].append(r)
        for k in ("name", "kind", "prompt", "circuit_id", "model", "mode",
                  "cwd", "description", "daily_at"):
            if k in data and data[k] is not None:
                r[k] = str(data[k]).strip()
        if "every_hours" in data and data["every_hours"] is not None:
            try:
                r["every_hours"] = max(0.25, float(data["every_hours"]))
                # an interval replaces a time of day, and the weekday
                # that only meant something with it
                r.pop("daily_at", None)
                r.pop("weekday", None)
            except (TypeError, ValueError):
                pass
        if r.get("daily_at"):
            r.pop("every_hours", None)
        if "weekday" in data and data["weekday"] is not None:
            # "" clears it: the Schedule is daily again
            day = str(data["weekday"]).strip().lower()
            if day and day not in WEEKDAYS:
                raise ValueError("weekday must be one of "
                                 + ", ".join(WEEKDAYS))
            if day:
                r["weekday"] = day
            else:
                r.pop("weekday", None)
        if r.get("weekday") and not r.get("daily_at"):
            raise ValueError("a weekly routine needs a time of day (daily_at)")
        for k in ("enabled", "notify"):
            if k in data and data[k] is not None:
                r[k] = bool(data[k])
        if "targets" in data and data["targets"] is not None:
            if not isinstance(data["targets"], list):
                raise ValueError("targets must be a list of card ids")
            r["targets"] = [str(t).strip()[:100] for t in data["targets"]
                            if str(t).strip()][:80]
        if r.get("kind") not in KINDS:
            r["kind"] = "custom"
        _command_fields(r, data)
        if not (r.get("name") or "").strip():
            raise ValueError("a routine needs a name")
        if not r.get("daily_at") and not r.get("every_hours"):
            raise ValueError("a routine needs a cadence "
                             "(every_hours or daily_at)")
        if r["kind"] == "command" and not r.get("command"):
            raise ValueError("a command routine needs a command to run")
        if not instance.owns(r):
            r.update(instance_id=instance.id(), last_run=None, last_job=None,
                     last_run_id=None, last_status=None)
        r["updated"] = _now_iso()
        _save(s)
        return r


_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _abs(value, what):
    text = str(value or "").strip()
    if text and not Path(text).expanduser().is_absolute():
        raise ValueError(f"{what} must be an absolute path: {text}")
    return str(Path(text).expanduser()) if text else ""


def _split_command(line):
    """One command line into argv. POSIX rules on a Mac; on Windows,
    backslashes are path separators, so the non-POSIX split is used and
    each argument loses one pair of matching outer quotes, which that
    mode keeps."""
    from . import settings
    if not settings.IS_WIN:
        return shlex.split(line)
    return [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'"
            else a for a in shlex.split(line, posix=False)]


def _command_fields(r, data):
    """The fields only a command routine carries, validated in place:
    command (an argv list, or one line split like a shell would), cwd,
    env (name -> value overrides), output_log (where the program's output
    is also appended) and watch_logs (files whose new lines each run
    copies)."""
    if "command" in data and data["command"] is not None:
        cmd = data["command"]
        if isinstance(cmd, str):
            cmd = _split_command(cmd)
        if not isinstance(cmd, list) or not all(
                isinstance(a, str) and a.strip() for a in cmd):
            raise ValueError("command must be a list of arguments")
        r["command"] = [a for a in cmd]
    if r.get("kind") != "command":
        return
    if "cwd" in data and data["cwd"] is not None:
        r["cwd"] = _abs(data["cwd"], "the folder")
    if "env" in data and data["env"] is not None:
        env = data["env"]
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and _ENV_KEY.fullmatch(k)
                and isinstance(v, str) for k, v in env.items()):
            raise ValueError("env must map variable names to text")
        r["env"] = dict(env)
    if "output_log" in data and data["output_log"] is not None:
        r["output_log"] = _abs(data["output_log"], "the output log")
    if "watch_logs" in data and data["watch_logs"] is not None:
        logs = data["watch_logs"]
        if not isinstance(logs, list):
            raise ValueError("watch_logs must be a list of paths")
        r["watch_logs"] = [_abs(x, "a watched log") for x in logs if x]


def delete_routine(rid):
    with _lock, locked(STORE):
        s = _load()
        before = len(s["routines"])
        s["routines"] = [r for r in s["routines"] if r["id"] != rid]
        if len(s["routines"]) == before:
            raise KeyError(rid)
        _save(s)


def _about(r):
    """The standing loop's own description, for the session's long form:
    its cadence and what it is for, read off the routine record."""
    cad = (f"every {r['every_hours']}h" if r.get("every_hours")
           else f"{r['weekday']}s at {r['daily_at']}"
           if r.get("daily_at") and r.get("weekday")
           else f"daily at {r['daily_at']}" if r.get("daily_at") else "")
    desc = " ".join((r.get("description") or "").split())
    head = " ".join((r.get("prompt") or "").split())[:400]
    return "\n".join(x for x in (
        f"Standing loop '{r.get('name') or r['id']}' ({r.get('kind')}"
        + (f", {cad}" if cad else "") + ").",
        desc, "" if desc else head) if x)


def _stamp(rid, **fields):
    with _lock, locked(STORE):
        s = _load()
        r = next((r for r in s["routines"] if r["id"] == rid), None)
        if r:
            r.update(fields)
            _save(s)


# ---------- due logic ----------

def archive_routine(rid):
    """Stop a routine and file it under Archived; Restore turns it back
    on only if it was on."""
    with _lock, locked(STORE):
        s = _load()
        r = next((r for r in s["routines"] if r["id"] == rid), None)
        if not r:
            raise KeyError(rid)
        if not r.get("archived"):
            r["archived"] = _now_iso()
            r["archived_enabled"] = bool(r.get("enabled"))
            r["enabled"] = False
            r["updated"] = _now_iso()
            _save(s)
        return r


def restore_routine(rid):
    with _lock, locked(STORE):
        s = _load()
        r = next((r for r in s["routines"] if r["id"] == rid), None)
        if not r:
            raise KeyError(rid)
        if r.get("archived"):
            r["enabled"] = bool(r.pop("archived_enabled", False))
            r.pop("archived", None)
            r["updated"] = _now_iso()
            _save(s)
        return r


def is_due(r, now=None):
    if not r.get("enabled") or r.get("archived"):
        return False
    now = now or datetime.now().astimezone()
    last = r.get("last_run")
    if r.get("daily_at"):
        day = str(r.get("weekday") or "").lower()
        if day and WEEKDAYS[now.weekday()] != day:
            return False     # a weekly Schedule: not its day
        try:
            hh, mm = str(r["daily_at"]).split(":")
            gate = now.replace(hour=int(hh), minute=int(mm),
                               second=0, microsecond=0)
        except (ValueError, AttributeError):
            return False
        if now < gate:
            return False
        if last:
            # Today's run is one that started at or after today's gate. A
            # Run now earlier in the day does not use up the evening's
            # scheduled run, as a launchd calendar job behaves.
            try:
                last_dt = datetime.fromisoformat(last).astimezone()
                if last_dt >= gate:
                    return False
            except ValueError:
                pass
        return True
    hours = float(r.get("every_hours") or 0)
    if hours <= 0:
        return False
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last).astimezone()
    except ValueError:
        return True
    return (now - last_dt).total_seconds() >= hours * 3600


# ---------- dispatch ----------

def _muse_prompt():
    from . import ideas, settings
    owner = settings.get("owner_name") or "the owner"
    # Deferred ideas are on this list ON PURPOSE: the owner already saw
    # that idea and said "not now", so re-proposing it tomorrow would make
    # Defer meaningless. They are shown with their status so the muse can
    # tell "yet to be built" from "already declined for now".
    current = [i for i in ideas.list_items()
               if i["status"] in ("open", "on-hold", "proposed", "deferred")]
    backlog = "\n".join(
        f"- [{i['status']}] ({i.get('project', '?')}) {i['text'][:160]}"
        for i in current[:60]) or "(backlog is empty)"
    return (
        f"You are Vira's MUSE — the idea engine of {owner}'s AI chief of "
        "staff. Your job this morning: propose 1-3 genuinely NEW, "
        "buildable ideas that would make Vira (or another of the owner's "
        "projects) more valuable.\n\n"
        "How to work:\n"
        "1. Study the current backlog below — do NOT duplicate or lightly "
        "rephrase anything on it. Anything marked [deferred] is an idea "
        "the owner has already seen and set aside: never propose it "
        "again, in any wording.\n"
        "2. Use mcp__vira__daily_brief and mcp__vira__vault_search to "
        "ground yourself in what is actually going on — recent themes in "
        f"the vault, who {owner} is talking to, open loops, friction "
        "points.\n"
        "3. For each idea, call mcp__vira__propose_idea with a crisp "
        "one-to-two-sentence text (what + why it matters) and the project "
        "it belongs to. Quality over quantity — one sharp idea beats "
        "three vague ones.\n"
        "4. Finish with a short note on why you chose these.\n\n"
        "Ideas you propose are STAGED for the owner's approval — nothing "
        "builds until approved, so be ambitious but concrete.\n\n"
        f"CURRENT BACKLOG:\n{backlog}")


# Prompt tokens that resolve to deterministic in-process refreshes — no
# model session is launched, so they run fine on a machine with no AI.
_INTERNAL_TOKENS = ("__refresh_groupings__", "__refresh_intros__",
                    "__refresh_atlas__", "__refresh_reconnect__",
                    "__orphan_sweep__", "__room_ingest__")


# _ai_ready's probe shells out per provider, and a skipped routine stays
# due (see dispatch), so the scheduler re-asks every tick — cache the
# answer briefly to keep an AI-less machine's idle loop cheap.
_AI_PROBE_TTL = 300
_ai_cache = {"at": 0.0, "ok": False}


def _ai_ready():
    """Whether any provider is actually usable. A routine that launches a
    model session on a machine with no AI connected can only mint a dead
    job — which is exactly what a fresh install's first hour looked like
    (2026-07-28: muse + system-map dispatched within a minute of first
    boot, before the owner had connected anything, and sat in the ledger
    with no outcome). Deterministic probe, no model call. A broken probe
    must never silence routines, so errors read as ready."""
    now = time.monotonic()
    if now - _ai_cache["at"] < _AI_PROBE_TTL:
        return _ai_cache["ok"]
    try:
        from . import models
        ok = bool(models.connected())
    except Exception:
        ok = True
    _ai_cache.update(at=now, ok=ok)
    return ok


@modulemodels.scoped("work")
def dispatch(r, trigger="manual"):
    """Dispatch one routine now. Returns {job_id}, {run_id} or
    {command_run}."""
    from . import circuits
    from . import session
    if r.get("kind") == "command":
        # A program, not a model session: no AI gate. Only the instance
        # that owns the row may start it, because a branch instance's
        # copy of the store points at the same real files.
        from . import routinerun
        if not instance.owns(r):
            raise ValueError("this routine belongs to another Vira "
                             "instance; run it there")
        rec = routinerun.start(r, trigger=trigger)
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_command_run=rec["id"],
               last_status="running")
        return {"command_run": rec["id"], "status": rec["status"]}
    if (r.get("prompt") or "") not in _INTERNAL_TOKENS and not _ai_ready():
        # last_run is deliberately NOT stamped: the routine stays due, so
        # it fires within a probe-cache window of the owner connecting an
        # AI — instead of quietly losing a whole cadence period to the
        # skip. The stamp (write-once, or the still-due routine would
        # grind the store every tick) is what the Agent Loops panel shows.
        if r.get("last_status") != "skipped — no AI connected":
            _stamp(r["id"], last_status="skipped — no AI connected")
        return {"skipped": "no_ai"}
    if (r.get("prompt") or "").startswith("__research_topic__:"):
        from . import researchtopics
        topic = researchtopics.launch(r["prompt"].split(":", 1)[1])
        _stamp(r["id"], last_run=_now_iso(), last_run_id=topic["run_id"],
               last_job=None, last_status="running")
        return {"run_id": topic["run_id"]}
    if r["kind"] == "circuit":
        cid = r.get("circuit_id") or ""
        targets = r.get("targets") if isinstance(r.get("targets"), list) else None
        if targets == []:
            # Wired to nothing: there is nothing to run. Stamped, so the
            # scheduler does not retry it every minute until tomorrow.
            _stamp(r["id"], last_run=_now_iso(), last_job=None,
                   last_run_id=None, last_status="skipped: wired to no card")
            return {"skipped": "no_targets"}
        run = circuits.start_run(cid, r.get("prompt") or r["name"],
                                 cwd=r.get("cwd") or None,
                                 notify=bool(r.get("notify")),
                                 source=f"routine:{r['id']}",
                                 vault_destination=r.get("vault_destination"),
                                 vault_context=r.get("vault_context"),
                                 entry=targets)
        _stamp(r["id"], last_run=_now_iso(), last_run_id=run["id"],
               last_job=None, last_status="running")
        return {"run_id": run["id"]}
    # __refresh_intros__ is the pre-groupings token; a store row minted
    # before the reframe still carries it, so both dispatch the same way
    if (r.get("prompt") or "") in ("__refresh_groupings__",
                                   "__refresh_intros__"):
        from . import radar
        threading.Thread(target=radar.refresh_groupings, daemon=True,
                         name="vira-grouping-scout").start()
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "refresh_groupings"}
    if (r.get("prompt") or "") == "__refresh_atlas__":
        from . import atlas
        atlas.refresh(narrate=True)      # already runs in its own thread
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "refresh_atlas"}
    if (r.get("prompt") or "") == "__refresh_reconnect__":
        from . import reconnect
        threading.Thread(target=reconnect.refresh, daemon=True,
                         name="vira-pivot-scout").start()
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "refresh_reconnect"}
    if (r.get("prompt") or "") == "__lesson_recurrence__":
        # in-process like the refresh tokens, but deliberately NOT in
        # _INTERNAL_TOKENS: rung 2 spends model calls, so the _ai_ready
        # gate above applies. Rung 1 alone stays reachable through the
        # manual refresh route on a machine with no AI.
        from . import lessonwatch
        threading.Thread(target=lessonwatch.run_pass, daemon=True,
                         name="vira-lesson-recurrence").start()
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "lesson_recurrence"}
    if (r.get("prompt") or "") == "__orphan_sweep__":
        from . import orphanwork
        threading.Thread(target=orphanwork.refresh, daemon=True,
                         name="vira-orphan-sweep").start()
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "orphan_sweep"}
    if (r.get("prompt") or "") == "__room_ingest__":
        from . import fullingest
        threading.Thread(target=fullingest.run_all, daemon=True,
                         name="vira-room-ingest").start()
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "room_ingest"}
    prompt = _muse_prompt() if r["kind"] == "muse" else (r.get("prompt") or "")
    if prompt == "__module_map__":       # composed fresh per run, like muse
        from . import modulemap
        prompt = modulemap.refresh_prompt()
    if prompt == "__room_scout__":       # composed fresh per run (readingroom)
        from . import readingroom
        prompt = readingroom.refresh_all_prompt()
        if not prompt:                   # no rooms yet: a quiet no-op, not
            _stamp(r["id"], last_run=_now_iso(), last_job=None,   # a failure
                   last_run_id=None, last_status="done")
            return {"internal": "no_rooms"}
    if not prompt.strip():
        raise ValueError("routine has no prompt")
    model_module = {"__module_map__": "map", "__room_scout__": "reader"}.get(
        r.get("prompt"), modulemodels.current() or "work")
    with modulemodels.scope(model_module):
        jid = session.sessions.launch(prompt, cwd=r.get("cwd") or None,
                                      model=r.get("model") or None,
                                      # no stored mode -> the session_default_mode
                                      # config default, same as every dispatch
                                      mode=session.norm_mode(r.get("mode")),
                                      vault_destination=r.get("vault_destination"),
                                      vault_context=r.get("vault_context"),
                                      meta={"routine_id": r["id"],
                                            "kind": r["kind"]},
                                      subject=r.get("name") or r["id"],
                                      about=_about(r))
    _stamp(r["id"], last_run=_now_iso(), last_job=jid,
           last_run_id=None, last_status="running")
    return {"job_id": jid}


def _previous_live(r):
    from . import circuits, joblog, session
    if r.get("kind") == "command":
        from . import routinerun
        return routinerun.live(r["id"])
    if r.get("last_job"):
        snap = (session.sessions.get(r["last_job"])
                or joblog.get_record(r["last_job"]))
        if snap and snap.get("status") == "running":
            return True
    if r.get("last_run_id"):
        run = circuits.get_run(r["last_run_id"])
        if run and run.get("status") == "running":
            return True
    return False


class Scheduler(threading.Thread):
    """60s tick: dispatch due routines; settle finished ones (stamp
    last_status, fire the completion ping)."""

    def __init__(self):
        super().__init__(daemon=True, name="vira-routines")
        self._stop = threading.Event()

    def run(self):
        time.sleep(10)                   # let the rest of the app boot
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — the scheduler never dies
                pass
            self._stop.wait(TICK_S)

    def stop(self):
        self._stop.set()

    def tick(self):
        for r in list_routines():
            try:
                if not instance.owns(r):
                    continue
                self._settle(r)
                if is_due(r) and not _previous_live(r):
                    dispatch(r, trigger="schedule")
            except Exception:  # noqa: BLE001 — one routine never kills the rest
                pass

    def _settle(self, r):
        """When a dispatched job/run has finished since we last looked,
        record the outcome and notify once."""
        from . import circuits, joblog, session
        if r.get("kind") == "command":
            from . import routinerun
            routinerun.settle(r["id"])
        if r.get("last_status") != "running":
            return
        status = None
        detail = ""
        if r.get("kind") == "command":
            rec = routinerun.get(r.get("last_command_run")) or {}
            status = rec.get("status")
            if status in routinerun.LIVE:
                return
            counts = rec.get("counts") or {}
            if counts:
                detail = " — " + ", ".join(f"{n} {k}"
                                           for k, n in counts.items())
            _stamp(r["id"], last_status=status or "unknown",
                   last_summary=detail[3:])
            if r.get("notify"):
                try:
                    from . import notify
                    notify.agent_ping(
                        f"Vira: routine '{r['name']}' {status}{detail}",
                        key=f"routine:{r['id']}:{r.get('last_run')}")
                except Exception:  # noqa: BLE001
                    pass
            return
        if r.get("last_job"):
            snap = (session.sessions.get(r["last_job"])
                    or joblog.get_record(r["last_job"]) or {})
            status = snap.get("status")
            if r.get("kind") == "muse" and status == "done":
                from . import ideas
                fresh = [i for i in ideas.list_items()
                         if i["status"] == "proposed"]
                detail = f" — {len(fresh)} proposal(s) waiting"
        elif r.get("last_run_id"):
            run = circuits.get_run(r["last_run_id"]) or {}
            status = run.get("status")
        if not status or status == "running":
            return
        _stamp(r["id"], last_status=status)
        if r.get("notify") and not r.get("last_run_id"):
            # circuit runs ping from the driver's finalize; jobs ping here
            try:
                from . import notify
                notify.agent_ping(
                    f"Vira: routine '{r['name']}' {status}{detail}",
                    key=f"routine:{r['id']}:{r.get('last_run')}")
            except Exception:  # noqa: BLE001
                pass


scheduler = Scheduler()

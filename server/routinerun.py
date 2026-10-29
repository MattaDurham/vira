"""Command routines: a program Vira starts on a schedule, as its own process.

A standing loop of kind "command" (server/routines.py) runs a program
instead of an agent: a capture script, an export, anything with an argv.
The Scheduler decides when; this module carries out each run and keeps
its history.

Every run is a detached process (``python -m server.routinerun <run
dir>``) in its own session, the job runner's rule (session._spawn_runner):
a two-hour run never slows the server, and a server restart never kills
it. The runner:

  - holds a lock per routine for its whole life, so two runs of one
    routine never overlap. The lock lives in the runtime root that every
    instance of this checkout shares, so a branch test instance cannot
    start a second copy either;
  - starts the program with Vira's environment minus the session
    variables ``settings.strip_env`` removes and Vira's own instance
    variables, plus the routine's overrides (usually PATH);
  - copies the program's stdout and stderr into the run's output.log, and
    appends them to the routine's own log file when it names one, so a
    log the program has always written keeps growing where it always did;
  - notes each watched log file's size at the start and copies what the
    run appended, so a program that logs to its own file still shows that
    run's lines in Vira;
  - reads the result from that text: the last ``done: N word, N word``
    line gives the counts, and FAILED, Traceback and ``...Error:`` lines
    are the errors;
  - writes the finished record BEFORE it releases the lock. A record still
    marked running whose lock is free belongs to a runner that died (a
    reboot, a kill), and ``settle`` closes it as interrupted.

History: data/routine-runs.json, every run's record, newest last.
Logs: data/routine-runs/<run id>/.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import instance, jsonstore, settings

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "routine-runs.json"
RUNS = ROOT / "data" / "routine-runs"

# Every run stays in the history; past this many per routine the oldest
# records and their log folders are dropped, and "pruned" counts them so
# the panel can say how many went. 3000 is four months of an hourly routine.
KEEP_PER_ROUTINE = 3000
# The record carries only the first errors, for the Runs list; every line
# stays in the run's log files.
MAX_ERRORS = 20
MAX_ERROR_CHARS = 400
# A watched log's slice is copied up to this size. Past it the copy ends
# with a line saying so; the original file still holds everything.
MAX_SLICE = 4 * 1024 * 1024
# A log file served to the panel is cut to its last this-many bytes, with
# a note, so one runaway run cannot stall the page.
MAX_SERVE = 1024 * 1024
# A run is "starting" from the spawn until its runner takes the lock; one
# that has not taken it in this long never will.
START_GRACE_S = 60
# How long the runner retries the lock before it gives up. A settle pass
# probes the lock for an instant, and a runner that started in that
# instant must not mistake the probe for a second run.
LOCK_WAIT_S = 5
LIVE = ("starting", "running")

_DONE = re.compile(r"\bdone:\s*(.+?)\s*$")
_PAIR = re.compile(r"(\d+)\s+([A-Za-z][^,;()]*)")
_ERROR = re.compile(
    r"\bFAILED\b"
    r"|Traceback \(most recent call last\)"
    r"|\bfailed:"
    r"|^\s*(?:[\w.]+\.)?\w*(?:Error|Exception)\b:"
    r"|\bexit [1-9]\d*\)")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_s(stamp):
    try:
        return time.time() - datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return float("inf")


# ---------------------------------------------------------------- store

def _blank():
    return {"runs": [], "pruned": {}}


def _fix(s):
    if not isinstance(s, dict) or not isinstance(s.get("runs"), list):
        s = _blank()
    if not isinstance(s.get("pruned"), dict):
        s["pruned"] = {}
    return s


def _read():
    return _fix(jsonstore.read(STORE, _blank()))


def _mutate(fn):
    def apply(s):
        s = _fix(s)
        fn(s)
        return s
    return jsonstore.mutate(STORE, apply, _blank(), indent=1,
                            ensure_ascii=False)


def _update(run_id, **fields):
    def apply(s):
        for rec in s["runs"]:
            if rec.get("id") == run_id:
                rec.update(fields)
    _mutate(apply)


def get(run_id):
    if not run_id:
        return None
    return next((r for r in _read()["runs"] if r.get("id") == run_id), None)


def runs(rid, offset=0, limit=50):
    """One routine's runs, newest first: {runs, total, pruned}."""
    s = _read()
    mine = [r for r in s["runs"] if r.get("routine_id") == rid]
    mine.reverse()
    offset = max(0, int(offset))
    limit = max(1, min(500, int(limit)))
    return {"runs": [instance.record_view(r) for r in
                     mine[offset:offset + limit]],
            "total": len(mine), "pruned": int(s["pruned"].get(rid) or 0)}


def run_dir(run_id):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", str(run_id or "")):
        raise ValueError("not a run id")
    return RUNS / run_id


# How much of a finished run the wire may carry into the card after it in
# a Flow: the outcome, the counts, every recorded error, and the program's
# last lines. The same budget circuits.RESULT_TEXT_CAP gives a finished run
# row; the per-part handoff share (circuits._handoff_cap) trims it again at
# delivery, so the next card's window is never the thing this bounds.
RESULT_CAP = 12_000
# The last lines of output.log a result carries: enough for a capture
# script's per-item lines and its done line, not the whole night.
TAIL_LINES = 40


def result_text(rec, lines=TAIL_LINES):
    """A finished run in words, for the card after it in a Flow (the wire
    delivers it under that card's box, circuits._delivered): the outcome,
    when it ran, the counts, the errors, the runner's note, and the last
    lines the program wrote."""
    rec = rec or {}
    name = rec.get("routine") or rec.get("routine_id") or "loop"
    status = rec.get("status") or "unknown"
    code = rec.get("exit_code")
    head = f"LOOP {name}: run {rec.get('id') or '?'} {status}"
    if code not in (None, 0):
        head += f" (exit {code})"
    parts = [head]
    when = [f"{k} {rec[k]}" for k in ("started", "finished") if rec.get(k)]
    if when:
        parts.append(", ".join(when))
    counts = rec.get("counts") or {}
    if counts:
        parts.append("Counts: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    errors = [str(e) for e in rec.get("errors") or []]
    if errors:
        total = int(rec.get("error_count") or len(errors))
        parts.append(f"Errors ({total}):\n" + "\n".join(f"- {e}" for e in errors))
    if rec.get("note"):
        parts.append(str(rec["note"]))
    tail = _tail(rec.get("id"), lines)
    if tail:
        shown = tail.count("\n") + 1
        parts.append(f"Last {shown} line{'s' if shown != 1 else ''} of the output:\n{tail}")
    return "\n".join(parts)[:RESULT_CAP]


def _tail(run_id, lines):
    """The last `lines` lines of a run's output.log, or '' when it has none."""
    try:
        path = run_dir(run_id) / "output.log"
    except ValueError:
        return ""
    if not path.exists():
        return ""
    text = path.read_bytes()[-MAX_SERVE:].decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-max(1, int(lines)):]).strip()


# ---------------------------------------------------------------- lock

def lock_path(rid):
    return instance.shared_root() / "routine-locks" / f"{rid}.lock"


def held(rid):
    """Whether a runner of this routine is alive anywhere on the machine."""
    probe = instance.Lease(lock_path(rid))
    if probe.acquire():
        probe.close()
        return False
    return True


def live(rid):
    if held(rid):
        return True
    return any(r.get("routine_id") == rid and r.get("status") == "starting"
               and _age_s(r.get("queued")) < START_GRACE_S
               for r in _read()["runs"])


def settle(rid=None):
    """Close every run this instance owns whose runner is gone."""
    def dead(rec):
        if rec.get("status") not in LIVE or not instance.owns(rec):
            return False
        if rid and rec.get("routine_id") != rid:
            return False
        if rec.get("status") == "starting" \
                and _age_s(rec.get("queued")) < START_GRACE_S:
            return False
        return not held(rec.get("routine_id"))

    if not any(dead(r) for r in _read()["runs"]):
        return 0
    closed = []

    def apply(s):
        for rec in s["runs"]:
            if dead(rec):
                out = _out_file(rec.get("id"))
                end = (datetime.fromtimestamp(out.stat().st_mtime,
                                              timezone.utc)
                       .isoformat(timespec="seconds")
                       if out and out.exists() else _now())
                rec.update(status="interrupted", finished=end,
                           note="The runner stopped before it recorded an "
                                "end (a restart of the Mac, or a kill). "
                                "The log has whatever it wrote.")
                closed.append(rec["id"])
    _mutate(apply)
    return len(closed)


def _out_file(run_id):
    try:
        return run_dir(run_id) / "output.log"
    except ValueError:
        return None


# ---------------------------------------------------------------- start

def _spawn(argv, log):
    """The one place a runner process is started; tests patch it."""
    if settings.IS_WIN:
        detach = {"creationflags": (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)}
    else:
        detach = {"start_new_session": True}
    return subprocess.Popen(argv, cwd=str(ROOT), stdout=log,
                            stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL,
                            env=instance.child_env(), **detach)


def start(r, trigger="manual"):
    """Start one run of a command routine. Returns its record."""
    argv = r.get("command")
    if not isinstance(argv, list) or not argv \
            or not all(isinstance(a, str) and a for a in argv):
        raise ValueError("this routine has no command to run")
    rid = r["id"]
    settle(rid)
    if live(rid):
        raise ValueError("a run of this routine is still going")
    run_id = (datetime.now().strftime("%Y%m%d-%H%M%S") + "-"
              + uuid.uuid4().hex[:6])
    rdir = RUNS / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    spec = {"run_id": run_id, "routine_id": rid, "argv": argv,
            "cwd": r.get("cwd") or "", "env": dict(r.get("env") or {}),
            "output_log": r.get("output_log") or "",
            "watch_logs": list(r.get("watch_logs") or []),
            "lock": str(lock_path(rid))}
    jsonstore.write_atomic(rdir / "spec.json", spec, indent=1,
                           ensure_ascii=False)
    rec = {"id": run_id, "routine_id": rid, "routine": r.get("name") or rid,
           "trigger": trigger, "status": "starting", "queued": _now(),
           "started": None, "finished": None, "exit_code": None,
           "pid": None, "counts": {}, "errors": [], "error_count": 0,
           "logs": [], "note": "", "instance_id": instance.id()}
    dropped = []

    def apply(s):
        s["runs"].append(rec)
        mine = [x for x in s["runs"] if x.get("routine_id") == rid]
        extra = len(mine) - KEEP_PER_ROUTINE
        if extra > 0:
            gone = {x["id"] for x in mine[:extra]}
            dropped.extend(gone)
            s["runs"] = [x for x in s["runs"] if x.get("id") not in gone]
            s["pruned"][rid] = int(s["pruned"].get(rid) or 0) + extra
    _mutate(apply)
    for old in dropped:
        try:
            shutil.rmtree(run_dir(old), ignore_errors=True)
        except ValueError:
            pass
    log = open(rdir / "runner.log", "ab")
    try:
        proc = _spawn([sys.executable, "-m", "server.routinerun", str(rdir)],
                      log)
    except OSError as e:
        _update(run_id, status="failed", finished=_now(),
                note=f"Vira could not start the runner: {e}")
        raise ValueError(f"could not start the runner: {e}")
    finally:
        log.close()
    _update(run_id, pid=proc.pid)
    return {**rec, "pid": proc.pid}


# ---------------------------------------------------------------- read

def parse(text):
    """Counts and error lines from a run's text."""
    counts = {}
    errors = []
    total = 0
    for line in text.splitlines():
        m = _DONE.search(line)
        if m:
            found = {label.strip(): int(n)
                     for n, label in _PAIR.findall(m.group(1))}
            if found:
                counts = found
            continue
        if _ERROR.search(line):
            total += 1
            if len(errors) < MAX_ERRORS:
                errors.append(line.strip()[:MAX_ERROR_CHARS])
    return counts, errors, total


def log_files(run_id):
    """The run's log files, each cut to its last MAX_SERVE bytes."""
    rdir = run_dir(run_id)
    rec = get(run_id) or {}
    names = ["output.log"] + [x["file"] for x in rec.get("logs") or []
                              if x.get("file")]
    out = []
    for name in names:
        p = rdir / name
        if not p.is_file():
            continue
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > MAX_SERVE:
                f.seek(size - MAX_SERVE)
            data = f.read()
        src = next((x.get("source") for x in rec.get("logs") or []
                    if x.get("file") == name), "")
        out.append({"name": name, "source": src, "bytes": size,
                    "cut": size > MAX_SERVE,
                    "text": data.decode("utf-8", errors="replace")})
    return out


# ---------------------------------------------------------------- runner

def _env(overrides):
    env = {k: v for k, v in settings.strip_env().items()
           if not k.startswith("VIRA_")}
    env.update({str(k): str(v) for k, v in (overrides or {}).items()})
    return env


def _size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def _slice(src, start, dest):
    """Copy what `src` gained since `start` into `dest`. Returns bytes copied."""
    try:
        size = src.stat().st_size
    except OSError:
        return 0
    begin = start if start is not None and start <= size else 0
    want = size - begin
    with open(src, "rb") as f, open(dest, "wb") as out:
        f.seek(begin)
        out.write(f.read(min(want, MAX_SLICE)))
        if want > MAX_SLICE:
            out.write(f"\n[cut: this run added {want} bytes to {src}; "
                      f"the first {MAX_SLICE} are above]\n".encode("utf-8"))
    return want


def _take_lock(path):
    lease = instance.Lease(path)
    deadline = time.monotonic() + LOCK_WAIT_S
    while not lease.acquire():
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)
    return lease


def main(rdir):
    rdir = Path(rdir)
    spec = json.loads((rdir / "spec.json").read_text(encoding="utf-8"))
    run_id = spec["run_id"]
    lease = _take_lock(spec["lock"])
    if lease is None:
        _update(run_id, status="skipped", finished=_now(),
                note="Another run of this routine held its lock, so this "
                     "one did not start.")
        return 0
    try:
        return _run(rdir, spec)
    finally:
        lease.close()


def _run(rdir, spec):
    run_id = spec["run_id"]
    watch = [Path(p) for p in spec.get("watch_logs") or []]
    offsets = [_size(p) for p in watch]
    out_path = rdir / "output.log"
    started = _now()
    try:
        proc = subprocess.Popen(spec["argv"], cwd=spec.get("cwd") or None,
                                env=_env(spec.get("env")),
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except OSError as e:
        out_path.write_text(f"could not start: {e}\n", encoding="utf-8")
        _update(run_id, status="failed", started=started, finished=_now(),
                pid=os.getpid(), errors=[f"could not start: {e}"],
                error_count=1, note="The program did not start.")
        return 1
    _update(run_id, status="running", started=started, pid=os.getpid(),
            child_pid=proc.pid)
    notes = []
    legacy = None
    if spec.get("output_log"):
        try:
            legacy = open(spec["output_log"], "ab")
        except OSError as e:
            notes.append(f"Could not append to {spec['output_log']}: {e}")
    with open(out_path, "ab") as out:
        for chunk in iter(proc.stdout.readline, b""):
            out.write(chunk)
            out.flush()
            if legacy is not None:
                try:
                    legacy.write(chunk)
                    legacy.flush()
                except OSError as e:
                    notes.append(f"Stopped appending to "
                                 f"{spec['output_log']}: {e}")
                    legacy.close()
                    legacy = None
    if legacy is not None:
        legacy.close()
    proc.stdout.close()
    code = proc.wait()
    logs = []
    texts = [out_path.read_bytes()[-MAX_SLICE:]]
    for i, (src, start) in enumerate(zip(watch, offsets)):
        name = f"watched-{i + 1}-{src.name}"
        if _slice(src, start, rdir / name):
            logs.append({"file": name, "source": str(src)})
            texts.append((rdir / name).read_bytes())
    counts, errors, total = parse(
        "\n".join(t.decode("utf-8", errors="replace") for t in texts))
    _update(run_id, status="ok" if code == 0 else "failed",
            finished=_now(), exit_code=code, counts=counts, errors=errors,
            error_count=total, logs=logs, note=" ".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

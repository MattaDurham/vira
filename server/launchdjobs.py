"""Every launchd job defined on this Mac, and the controls to change one.

The Forge lists what runs in the background outside Vira beside what Vira
runs, so the owner sees and controls all of it in one place: every file in
~/Library/LaunchAgents, /Library/LaunchAgents and /Library/LaunchDaemons,
the program each one runs, when it runs, and whether launchd has it loaded
right now (inventory).

Nothing here is read-only (owner's rule, 2026-10-29). A job can be saved
(its whole job file, or its program, folder, environment, logs and
schedule as fields), paused and resumed (launchctl disable / enable, then
bootout / bootstrap), run now (kickstart), archived (booted out and its
file moved to ~/Library/LaunchAgents-archive, with a note of where it came
from) and restored. A file outside the owner's own LaunchAgents folder
belongs to root, so a change to it runs through macOS's administrator
prompt (osascript ... with administrator privileges): the owner types the
password into macOS's own dialog, and Vira never sees it. Every save keeps
the previous file in the archive folder's backups/.
"""
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import settings

LAUNCHCTL_TIMEOUT_S = 5
# The administrator prompt waits for the owner to type a password.
ADMIN_TIMEOUT_S = 180
ARCHIVE_NAME = "LaunchAgents-archive"
_WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def _dirs():
    """(key, folder, title, domain) for each place launchd reads jobs from."""
    return [
        ("user", Path.home() / "Library" / "LaunchAgents",
         "Your login agents", "gui"),
        ("agents", Path("/Library/LaunchAgents"),
         "Login agents for every user", "gui"),
        ("daemons", Path("/Library/LaunchDaemons"), "System daemons",
         "system"),
    ]


def _launchctl(*args):
    """stdout of one read-only launchctl call, or "" when it fails."""
    try:
        out = subprocess.run(["launchctl", *args], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=LAUNCHCTL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _gui_state():
    """label -> (pid or None, last exit) from `launchctl list`."""
    rows = {}
    for line in _launchctl("list").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            pid, status, label = parts
            rows[label] = (int(pid) if pid.isdigit() else None, status)
    return rows


def _system_state():
    """label -> (pid or None, last exit) from `launchctl print system`."""
    rows = {}
    inside = False
    for line in _launchctl("print", "system").splitlines():
        if line.strip() == "services = {":
            inside = True
            continue
        if inside:
            if line.strip() == "}":
                break
            m = re.match(r"\s*(\d+)\s+(\S+)\s+(\S+)\s*$", line)
            if m:
                pid, status, label = m.groups()
                rows[label] = (int(pid) or None, status)
    return rows


def _disabled(domain):
    """Labels launchd has been told not to start, in one domain."""
    out = set()
    for line in _launchctl("print-disabled", domain).splitlines():
        m = re.match(r'\s*"([^"]+)"\s*=>\s*(true|disabled)', line)
        if m:
            out.add(m.group(1))
    return out


def _every(seconds):
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return f"every {seconds} seconds"
    if s % 86400 == 0:
        n = s // 86400
        return "every day" if n == 1 else f"every {n} days"
    if s % 3600 == 0:
        n = s // 3600
        return "every hour" if n == 1 else f"every {n} hours"
    if s % 60 == 0:
        n = s // 60
        return "every minute" if n == 1 else f"every {n} minutes"
    return f"every {s} seconds"


def _calendar(entry):
    if not isinstance(entry, dict):
        return "on a calendar schedule"
    hour, minute = entry.get("Hour"), entry.get("Minute")
    at = (f"{int(hour):02d}:{int(minute or 0):02d}" if hour is not None
          else f"minute {int(minute):02d} of every hour"
          if minute is not None else "")
    if "Weekday" in entry:
        day = _WEEKDAYS[int(entry["Weekday"]) % 8]
        return f"every {day}" + (f" at {at}" if at else "")
    if "Day" in entry:
        month = f" of month {entry['Month']}" if "Month" in entry else ""
        return f"day {entry['Day']}{month}" + (f" at {at}" if at else "")
    if hour is not None:
        return f"daily at {at}"
    return at or "on a calendar schedule"


def when(data, daemon=False):
    """Plain-English triggers for one job's plist."""
    out = []
    if data.get("RunAtLoad"):
        out.append("at startup" if daemon else "at login")
    keep = data.get("KeepAlive")
    if keep is True:
        out.append("kept running (restarted if it exits)")
    elif isinstance(keep, dict) and keep:
        out.append("kept running while: " + ", ".join(sorted(keep)))
    if data.get("StartInterval"):
        out.append(_every(data["StartInterval"]))
    cal = data.get("StartCalendarInterval")
    for entry in (cal if isinstance(cal, list) else [cal] if cal else []):
        out.append(_calendar(entry))
    if data.get("WatchPaths"):
        out.append("when these change: " + ", ".join(
            str(p) for p in data["WatchPaths"]))
    if data.get("QueueDirectories"):
        out.append("when files appear in: " + ", ".join(
            str(p) for p in data["QueueDirectories"]))
    if data.get("StartOnMount"):
        out.append("when a disk is mounted")
    if data.get("MachServices") or data.get("Sockets"):
        out.append("on demand, when another program calls it")
    if data.get("LaunchEvents"):
        out.append("on system events")
    if not out:
        out.append("only when something starts it by hand")
    return out


def cadence_kind(data):
    """One bucket for grouping by cadence: kept (always running), hourly,
    hours, daily, weekly, login, event or demand."""
    keep = data.get("KeepAlive")
    if keep is True or (isinstance(keep, dict) and keep):
        return "kept"
    if data.get("StartInterval"):
        try:
            sec = int(data["StartInterval"])
        except (TypeError, ValueError):
            sec = 0
        return ("hourly" if sec <= 3600 else "hours" if sec < 86400
                else "daily" if sec < 7 * 86400 else "weekly")
    cal = data.get("StartCalendarInterval")
    entries = cal if isinstance(cal, list) else [cal] if cal else []
    if entries:
        kinds = []
        for e in entries:
            e = e if isinstance(e, dict) else {}
            if "Weekday" in e or "Day" in e or "Month" in e:
                kinds.append("weekly")
            elif "Hour" in e:
                kinds.append("daily")
            else:
                kinds.append("hourly")
        order = ["hourly", "daily", "weekly"]
        return sorted(kinds, key=order.index)[0]
    if data.get("RunAtLoad"):
        return "login"
    if any(data.get(k) for k in ("WatchPaths", "QueueDirectories", "StartOnMount",
                                 "MachServices", "Sockets", "LaunchEvents")):
        return "event"
    return "demand"


def _program(data):
    args = data.get("ProgramArguments")
    if isinstance(args, list) and args:
        return " ".join(str(a) for a in args)
    return str(data.get("Program") or "")


def _state(label, states, disabled, in_file_disabled):
    if label in disabled or in_file_disabled:
        return "disabled", "disabled"
    if label not in states:
        return "not loaded", "off"
    pid, status = states[label]
    if pid:
        return f"running (pid {pid})", "running"
    return f"loaded, not running (last exit {status})", "loaded"


def inventory():
    """Every job file in the three folders, grouped by folder."""
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not settings.IS_MAC:
        return {"status": "not_mac", "checked": checked, "groups": [],
                "note": "launchd runs only on macOS."}
    uid = _uid()
    gui, system = _gui_state(), _system_state()
    off = {"gui": _disabled(f"gui/{uid}"), "system": _disabled("system")}
    groups = []
    for key, folder, title, domain in _dirs():
        group = {"key": key, "title": title, "dir": str(folder), "jobs": [],
                 "error": ""}
        try:
            names = sorted(os.listdir(folder))
        except FileNotFoundError:
            names = []
        except OSError as e:
            group["error"] = f"could not read the folder: {e}"
            names = []
        states = system if domain == "system" else gui
        for name in names:
            path = folder / name
            if not path.is_file():
                continue
            job = {"file": name, "path": str(path), "label": name,
                   "program": "", "when": [], "state": "", "tone": "",
                   "error": ""}
            try:
                data = plistlib.loads(path.read_bytes())
                if not isinstance(data, dict):
                    raise ValueError("not a dictionary")
            except Exception as e:  # noqa: BLE001 — every file is listed
                job.update(error=f"not a readable job file: {e}",
                           state="unreadable", tone="off")
                group["jobs"].append(job)
                continue
            label = str(data.get("Label") or name)
            state, tone = _state(label, states, off[domain],
                                 bool(data.get("Disabled")))
            program = _program(data)
            job.update(label=label, program=program,
                       when=(when(data, daemon=domain == "system") if program
                             else ["never: the file names no program"]),
                       state=state, tone=tone, cwd=str(data.get("WorkingDirectory") or ""),
                       cadence_kind=cadence_kind(data) if program else "demand",
                       admin=key != "user", domain=domain)
            group["jobs"].append(job)
        groups.append(group)
    return {"status": "ok", "checked": checked, "groups": groups,
            "archived": archived_jobs(), "note": ""}


# ---------------------------------------------------------------- change

def archive_dir():
    return Path.home() / "Library" / ARCHIVE_NAME


def _allowed_dirs():
    return [folder for _, folder, _, _ in _dirs()]


def _job_path(path, archived=False):
    """The job file a request names, refused unless it sits directly in one
    of launchd's folders (or the archive folder, for archived=True)."""
    target = Path(str(path or "")).expanduser()
    if not target.is_absolute() or ".." in target.parts:
        raise ValueError("a job is named by its absolute path")
    homes = [archive_dir()] if archived else _allowed_dirs()
    if target.parent not in homes:
        raise ValueError(f"not a launchd job file: {target}")
    if not target.is_file():
        raise FileNotFoundError(str(target))
    return target


def _home_of(path):
    """(domain, admin) for a job file: who loads it, and whether changing
    it needs the administrator prompt."""
    for key, folder, _, domain in _dirs():
        if Path(path).parent == folder:
            return domain, key != "user"
    raise ValueError(f"not a launchd job file: {path}")


def _target(domain):
    return f"gui/{_uid()}" if domain == "gui" else "system"


def _uid():
    # os.getuid is POSIX only; launchd is macOS only, so elsewhere the
    # number is never used for a real call
    return getattr(os, "getuid", lambda: 0)()


def _applescript_text(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(argv, admin=False):
    """Run one command; admin=True asks macOS for the administrator
    password in its own dialog first. Returns (ok, output)."""
    if admin:
        script = " ".join(shlex.quote(str(a)) for a in argv)
        argv = ["osascript", "-e",
                f"do shell script {_applescript_text(script)} with administrator privileges"]
    try:
        out = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=ADMIN_TIMEOUT_S if admin else LAUNCHCTL_TIMEOUT_S * 4)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return out.returncode == 0, (out.stdout + out.stderr).strip()


def _read_plist(path):
    data = plistlib.loads(Path(path).read_bytes())
    if not isinstance(data, dict):
        raise ValueError("the job file is not a dictionary")
    return data


def _loaded(label, domain):
    states = _system_state() if domain == "system" else _gui_state()
    return label in states


def detail(path, archived=False):
    """One job as the editor shows it: the whole file as XML, and the keys
    the fields edit."""
    target = _job_path(path, archived=archived)
    origin = _origin(target) if archived else {}
    data = _read_plist(target)
    domain, admin = _home_of(origin.get("path") or target) if (origin or not archived) else ("gui", False)
    label = str(data.get("Label") or target.name)
    cal = data.get("StartCalendarInterval")
    entries = cal if isinstance(cal, list) else [cal] if cal else []
    simple = all(isinstance(e, dict) and set(e) <= {"Hour", "Minute"} and "Hour" in e
                 for e in entries)
    daily = [f"{int(e['Hour']):02d}:{int(e.get('Minute') or 0):02d}" for e in entries] if simple else []
    args = data.get("ProgramArguments")
    argv = [str(a) for a in args] if isinstance(args, list) else (
        [str(data["Program"])] if data.get("Program") else [])
    env = data.get("EnvironmentVariables") or {}
    if archived:
        state, tone = "archived", "off"
    else:
        uid_off = _disabled(_target(domain))
        state, tone = _state(label, _system_state() if domain == "system" else _gui_state(),
                             uid_off, bool(data.get("Disabled")))
    return {
        "path": str(target), "file": target.name, "label": label,
        "xml": plistlib.dumps(data, fmt=plistlib.FMT_XML).decode("utf-8"),
        "state": state, "tone": tone, "admin": admin, "domain": domain,
        "archived": bool(archived), "origin": origin.get("path") or "",
        "when": when(data, daemon=domain == "system"),
        "fields": {
            "command_line": shlex.join(argv) if argv else "",
            "cwd": str(data.get("WorkingDirectory") or ""),
            "env": "\n".join(f"{k}={v}" for k, v in env.items()) if isinstance(env, dict) else "",
            "stdout": str(data.get("StandardOutPath") or ""),
            "stderr": str(data.get("StandardErrorPath") or ""),
            "run_at_load": bool(data.get("RunAtLoad")),
            "keep_alive": data.get("KeepAlive") is True,
            "interval_minutes": (int(data["StartInterval"]) // 60
                                 if str(data.get("StartInterval") or "").isdigit() else None),
            "daily_at": daily, "calendar_simple": simple,
        },
    }


def _apply_fields(data, fields):
    """Edit a job's dictionary from the editor's fields, leaving every key
    the fields do not name as it was."""
    out = dict(data)

    def put(key, value):
        if value in (None, "", [], {}):
            out.pop(key, None)
        else:
            out[key] = value
    if "command_line" in fields:
        argv = shlex.split(str(fields.get("command_line") or ""))
        if not argv:
            raise ValueError("a job needs a program to run")
        out.pop("Program", None)
        out["ProgramArguments"] = argv
    if "cwd" in fields:
        cwd = str(fields.get("cwd") or "").strip()
        if cwd and not Path(cwd).expanduser().is_absolute():
            raise ValueError("the folder must be an absolute path")
        put("WorkingDirectory", str(Path(cwd).expanduser()) if cwd else "")
    if "env" in fields:
        env = {}
        for row in str(fields.get("env") or "").splitlines():
            row = row.strip()
            if not row or row.startswith("#"):
                continue
            if "=" not in row:
                raise ValueError(f"an environment line is NAME=value: {row}")
            k, v = row.split("=", 1)
            env[k.strip()] = v
        put("EnvironmentVariables", env)
    for key, plist_key in (("stdout", "StandardOutPath"), ("stderr", "StandardErrorPath")):
        if key in fields:
            put(plist_key, str(fields.get(key) or "").strip())
    if "run_at_load" in fields:
        put("RunAtLoad", True if fields.get("run_at_load") else None)
    if "keep_alive" in fields and not isinstance(out.get("KeepAlive"), dict):
        put("KeepAlive", True if fields.get("keep_alive") else None)
    if "interval_minutes" in fields:
        minutes = fields.get("interval_minutes")
        put("StartInterval", int(minutes) * 60 if minutes else None)
    if "daily_at" in fields:
        cal = data.get("StartCalendarInterval")
        entries = cal if isinstance(cal, list) else [cal] if cal else []
        if any(not (isinstance(e, dict) and set(e) <= {"Hour", "Minute"}) for e in entries):
            raise ValueError("this job's calendar has weekdays or dates; edit it in the job file")
        times = []
        for text in fields.get("daily_at") or []:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ValueError(f"a time of day is HH:MM: {text}")
            times.append({"Hour": int(m.group(1)), "Minute": int(m.group(2))})
        put("StartCalendarInterval", times[0] if len(times) == 1 else times)
    return out


def _write_file(target, data, admin):
    """Write a job file, keeping the previous copy in the archive's
    backups/. A root-owned file is written through the admin prompt."""
    backups = archive_dir() / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(target, backups / f"{target.name}.{stamp}")
    body = plistlib.dumps(data, fmt=plistlib.FMT_XML)
    if not admin:
        tmp = target.with_name(target.name + ".vira-tmp")
        tmp.write_bytes(body)
        tmp.replace(target)
        return
    with tempfile.NamedTemporaryFile("wb", suffix=".plist", delete=False) as fh:
        fh.write(body)
        staged = fh.name
    ok, out = _run(["/bin/sh", "-c",
                    f"cp {shlex.quote(staged)} {shlex.quote(str(target))} && "
                    f"chown root:wheel {shlex.quote(str(target))} && "
                    f"chmod 644 {shlex.quote(str(target))}"], admin=True)
    os.unlink(staged)
    if not ok:
        raise ValueError(f"macOS did not allow the change: {out or 'cancelled'}")


def save(path, xml=None, fields=None):
    """Save a job: the whole file as XML, or the editor's fields applied to
    it. A job that was loaded is reloaded so the change takes effect."""
    target = _job_path(path)
    domain, admin = _home_of(target)
    before = _read_plist(target)
    if xml is not None:
        try:
            data = plistlib.loads(str(xml).encode("utf-8"))
        except Exception as e:  # noqa: BLE001 - any parse failure is the same refusal
            raise ValueError(f"that is not a valid job file: {e}")
        if not isinstance(data, dict):
            raise ValueError("a job file is a dictionary")
    else:
        data = _apply_fields(before, fields or {})
    if not str(data.get("Label") or "").strip():
        raise ValueError("a job needs a Label")
    old_label = str(before.get("Label") or target.name)
    was_loaded = _loaded(old_label, domain)
    _write_file(target, data, admin)
    if was_loaded:
        _run(["launchctl", "bootout", _target(domain), str(target)], admin=admin)
        ok, out = _run(["launchctl", "bootstrap", _target(domain), str(target)], admin=admin)
        if not ok:
            raise ValueError(f"saved, but launchd did not load it again: {out}")
    return detail(target)


def set_paused(path, paused):
    """Pause: launchd is told not to start it (it stays off after a
    restart) and it is unloaded. Resume: allowed again and loaded."""
    target = _job_path(path)
    domain, admin = _home_of(target)
    label = str(_read_plist(target).get("Label") or target.name)
    where = _target(domain)
    if paused:
        ok, out = _run(["launchctl", "disable", f"{where}/{label}"], admin=admin)
        if not ok:
            raise ValueError(f"launchd refused to pause it: {out}")
        _run(["launchctl", "bootout", where, str(target)], admin=admin)
    else:
        ok, out = _run(["launchctl", "enable", f"{where}/{label}"], admin=admin)
        if not ok:
            raise ValueError(f"launchd refused to resume it: {out}")
        if not _loaded(label, domain):
            ok, out = _run(["launchctl", "bootstrap", where, str(target)], admin=admin)
            if not ok:
                raise ValueError(f"allowed again, but launchd did not load it: {out}")
    return detail(target)


def run_now(path):
    target = _job_path(path)
    domain, admin = _home_of(target)
    label = str(_read_plist(target).get("Label") or target.name)
    where = _target(domain)
    if not _loaded(label, domain):
        ok, out = _run(["launchctl", "bootstrap", where, str(target)], admin=admin)
        if not ok:
            raise ValueError(f"launchd did not load it: {out}")
    ok, out = _run(["launchctl", "kickstart", f"{where}/{label}"], admin=admin)
    if not ok:
        raise ValueError(f"launchd did not start it: {out}")
    return detail(target)


def _origin_file(archived):
    return archived.with_name(archived.name + ".origin.json")


def _origin(archived):
    try:
        return json.loads(_origin_file(archived).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def archive(path):
    """Unload a job and move its file to the archive folder, with a note of
    where it came from. launchd never reads that folder."""
    target = _job_path(path)
    domain, admin = _home_of(target)
    folder = archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / target.name
    n = 2
    while dest.exists():
        dest = folder / f"{target.stem}-{n}{target.suffix}"
        n += 1
    _run(["launchctl", "bootout", _target(domain), str(target)], admin=admin)
    if admin:
        ok, out = _run(["/bin/sh", "-c",
                        f"mv {shlex.quote(str(target))} {shlex.quote(str(dest))} && "
                        f"chown {_uid()} {shlex.quote(str(dest))}"], admin=True)
        if not ok:
            raise ValueError(f"macOS did not allow the move: {out or 'cancelled'}")
    else:
        shutil.move(str(target), str(dest))
    _origin_file(dest).write_text(json.dumps(
        {"path": str(target), "archived": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        indent=1), encoding="utf-8")
    return detail(dest, archived=True)


def restore(path):
    """Put an archived job file back where it came from and load it."""
    archived = _job_path(path, archived=True)
    origin = _origin(archived)
    home = Path(origin.get("path") or "")
    if not home.is_absolute() or home.parent not in _allowed_dirs():
        raise ValueError("the archive does not say where this job came from")
    if home.exists():
        raise ValueError(f"a job file is already at {home}")
    domain, admin = _home_of(home)
    if admin:
        ok, out = _run(["/bin/sh", "-c",
                        f"mv {shlex.quote(str(archived))} {shlex.quote(str(home))} && "
                        f"chown root:wheel {shlex.quote(str(home))} && chmod 644 {shlex.quote(str(home))}"],
                       admin=True)
        if not ok:
            raise ValueError(f"macOS did not allow the move: {out or 'cancelled'}")
    else:
        shutil.move(str(archived), str(home))
    _origin_file(archived).unlink(missing_ok=True)
    data = _read_plist(home)
    if not data.get("Disabled"):
        _run(["launchctl", "bootstrap", _target(domain), str(home)], admin=admin)
    return detail(home)


def archived_jobs():
    """Every job file in the archive folder, as inventory rows."""
    folder = archive_dir()
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        path = folder / name
        if not path.is_file() or name.endswith(".origin.json") or name.endswith(".vira-tmp"):
            continue
        origin = _origin(path)
        if not origin:
            continue            # a file Vira did not archive
        job = {"file": name, "path": str(path), "label": name, "program": "",
               "when": [], "state": "archived", "tone": "off", "error": "",
               "archived": origin.get("archived"), "origin": origin.get("path"),
               "cwd": "", "cadence_kind": "demand",
               "admin": Path(origin.get("path") or "").parent != _dirs()[0][1]}
        try:
            data = _read_plist(path)
            job.update(label=str(data.get("Label") or name), program=_program(data),
                       when=when(data), cwd=str(data.get("WorkingDirectory") or ""),
                       cadence_kind=cadence_kind(data))
        except Exception as e:  # noqa: BLE001 - listed either way
            job["error"] = f"not a readable job file: {e}"
        out.append(job)
    return out

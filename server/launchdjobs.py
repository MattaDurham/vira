"""Every launchd job defined on this Mac, read-only.

The Forge's "This Mac" library tab lists what runs in the background
outside Vira, so the owner can see all of it in one place: every file in
~/Library/LaunchAgents, /Library/LaunchAgents and /Library/LaunchDaemons,
the program each one runs, when it runs, and whether launchd has it
loaded right now.

Read-only by design. Nothing here loads, unloads, edits, moves or deletes
a job; the only commands it runs are `launchctl list`, `launchctl print
system` and `launchctl print-disabled`, which report state.
"""
import os
import plistlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import settings

LAUNCHCTL_TIMEOUT_S = 5
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
    uid = os.getuid()
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
                       state=state, tone=tone)
            group["jobs"].append(job)
        groups.append(group)
    return {"status": "ok", "checked": checked, "groups": groups,
            "note": ""}

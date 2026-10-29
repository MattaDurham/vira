"""Move a thing that runs somewhere else into Vira, as a Flow.

One noun (owner's rule, 2026-10-29): what Vira runs on a schedule is a
Flow. Three kinds of thing can become one:

  - a launchd job that starts a program at times (a calendar time or an
    interval): a Schedule card per time and a Program card with its
    argv, folder, environment and log; the job file is then archived
    (server/launchdjobs.py), so it cannot run a second time beside the
    Flow. A job launchd keeps running (a service) has no Vira equivalent
    and stays a launchd job;
  - a Claude scheduled task file: a Schedule card with the cadence the
    owner picks and an Agent card whose box is the file's prompt; the
    task folder is then archived (server/claudetasks.py);
  - a command routine (the loops Vira ran before Program cards): every
    Loop card over it becomes a Program card in place, keeping the
    routine's id as its run key so the lock and the run history carry
    on; if the routine's own cadence was on, it becomes a Schedule card
    wired to that Program card. With no Flow over it, it becomes a new
    Flow. The routine row is then deleted.

CLI: python -m server.adopt loops [--dry-run]   (every command routine)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import claudetasks, flows, launchdjobs, routines


def _schedule_nodes(times=(), every_hours=None, cwd="", enabled=True, x=-200, y=330):
    nodes = []
    for index, at in enumerate(times):
        nodes.append({"id": f"schedule-{index + 1}", "type": "trigger",
                      "name": f"Every day at {at}", "daily_at": at,
                      "enabled": enabled, "cwd": cwd, "x": x, "y": y + index * 170})
    if every_hours:
        hours = max(0.25, float(every_hours))
        shown = int(hours) if hours == int(hours) else hours
        nodes.append({"id": f"schedule-{len(nodes) + 1}", "type": "trigger",
                      "name": "Every hour" if hours == 1 else f"Every {shown} hours",
                      "every_hours": hours, "enabled": enabled, "cwd": cwd,
                      "x": x, "y": y + len(nodes) * 170})
    return nodes


def launchd_schedule(fields, data_when):
    """(times, every_hours) a launchd job's triggers map to, or raise
    ValueError saying why it stays a launchd job."""
    if fields.get("keep_alive"):
        raise ValueError("launchd keeps this job running all the time; a Vira "
                         "Schedule starts a program at times, so it stays a launchd job")
    times = list(fields.get("daily_at") or [])
    if not fields.get("calendar_simple"):
        raise ValueError("this job's calendar has weekdays or dates, which a Vira "
                         "Schedule does not have yet")
    minutes = fields.get("interval_minutes")
    every = (minutes / 60) if minutes else None
    if not times and not every:
        raise ValueError("this job starts only " + ("; ".join(data_when) or "by hand")
                         + ", so there is no schedule to move")
    return times, every


def adopt_launchd(path):
    job = launchdjobs.detail(path)
    fields = job["fields"]
    times, every = launchd_schedule(fields, job.get("when") or [])
    env = {}
    for row in (fields.get("env") or "").splitlines():
        if "=" in row:
            k, v = row.split("=", 1)
            env[k.strip()] = v
    program = {"id": "program", "type": "program", "name": job["label"],
               "x": 150, "y": 330,
               "program": {"command_line": fields["command_line"], "cwd": fields["cwd"],
                           "env": env, "output_log": fields.get("stdout") or ""}}
    schedules = _schedule_nodes(times, every, cwd=fields["cwd"])
    payload = {
        "name": job["label"], "description": f"Moved from launchd ({job['path']}).",
        "nodes": [{"id": "input", "type": "input", "prompt": f"Run {job['label']}",
                   "x": -200, "y": 130}, *schedules, program],
        "edges": [{"from": s["id"], "to": "program"} for s in schedules],
    }
    flow = flows.save_flow(payload, save_as=True)
    archived = launchdjobs.archive(job["path"])
    return {"flow": flow, "archived": archived["path"]}


def adopt_task(name, daily_at=None, every_hours=None, cwd=""):
    task = claudetasks.read(name)
    if not daily_at and not every_hours:
        raise ValueError("give it a time of day or an interval")
    schedules = _schedule_nodes([daily_at] if daily_at else [], every_hours, cwd=cwd or "")
    agent = {"id": "task", "type": "agent", "name": task["title"], "x": 150, "y": 330,
             "prompt": task["body"].strip() or task["text"], "read_only": False,
             "mode": "acceptEdits"}
    payload = {
        "name": task["title"],
        "description": task["description"] or f"Moved from the Claude task file {task['path']}.",
        "nodes": [{"id": "input", "type": "input", "x": -200, "y": 130,
                   "prompt": f"Run the scheduled task {task['title']}."},
                  *schedules, agent],
        "edges": [{"from": "input", "to": "task"}]
                 + [{"from": s["id"], "to": "task"} for s in schedules],
    }
    flow = flows.save_flow(payload, save_as=True)
    archived = claudetasks.archive(name)
    return {"flow": flow, "archived": archived["path"]}


def _program_from(row, node):
    return {**{k: node[k] for k in ("id", "name", "x", "y") if k in node},
            "type": "program",
            "program": {"command": list(row.get("command") or []), "cwd": row.get("cwd") or "",
                        "env": dict(row.get("env") or {}),
                        "output_log": row.get("output_log") or "",
                        "watch_logs": list(row.get("watch_logs") or []),
                        "run_key": row["id"]}}


def plan_command_routine(rid):
    """What adopting one command routine would change, without changing it."""
    row = routines.get_routine(rid)
    if not row or row.get("kind") != "command":
        raise KeyError(rid)
    hosts = []
    for flow in flows.list_flows():
        cards = [n for n in flow.get("nodes") or [] if n.get("type") == "native"
                 and (n.get("native") or {}).get("routine_id") == rid]
        if cards:
            hosts.append((flow, cards))
    own_schedule = bool(row.get("enabled")) and bool(row.get("daily_at") or row.get("every_hours"))
    return row, hosts, own_schedule


def adopt_command_routine(rid, dry_run=False):
    row, hosts, own_schedule = plan_command_routine(rid)
    report = {"routine": rid, "name": row.get("name"), "flows": [],
              "new_schedule": own_schedule, "dry_run": dry_run}
    times = [row["daily_at"]] if row.get("daily_at") else []
    every = row.get("every_hours") if not times else None
    if not hosts:
        report["flows"].append({"new": row.get("name")})
        if dry_run:
            return report
        schedules = _schedule_nodes(times, every, cwd=row.get("cwd") or "",
                                    enabled=bool(row.get("enabled")))
        program = _program_from(row, {"id": "program", "name": row.get("name") or rid,
                                      "x": 150, "y": 330})
        flow = flows.save_flow({
            "name": row.get("name") or rid, "description": row.get("description") or "",
            "nodes": [{"id": "input", "type": "input", "prompt": f"Run {row.get('name') or rid}",
                       "x": -200, "y": 130}, *schedules, program],
            "edges": [{"from": s["id"], "to": "program"} for s in schedules],
        }, save_as=True)
        report["flows"][-1]["id"] = flow["id"]
    else:
        for flow, cards in hosts:
            report["flows"].append({"id": flow["id"], "name": flow["name"],
                                    "cards": [c["id"] for c in cards]})
            if dry_run:
                continue
            nodes = [(_program_from(row, n) if n in cards else n) for n in flow["nodes"]]
            edges = list(flow["edges"])
            if own_schedule:
                for index, schedule in enumerate(_schedule_nodes(
                        times, every, cwd=row.get("cwd") or "",
                        x=min(n.get("x", 0) for n in nodes) - 60,
                        y=max(n.get("y", 0) for n in nodes) + 180)):
                    schedule["id"] = f"own-schedule-{index + 1}"
                    nodes.append(schedule)
                    edges += [{"from": schedule["id"], "to": c["id"]} for c in cards]
            flows.save_flow({**flow, "id": flow["id"], "nodes": nodes, "edges": edges})
    if not dry_run:
        routines.delete_routine(rid)
    return report


def adopt_all_command_routines(dry_run=False):
    return [adopt_command_routine(r["id"], dry_run=dry_run)
            for r in routines.list_routines() if r.get("kind") == "command"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m server.adopt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    loops = sub.add_parser("loops", help="turn every command routine into Program cards")
    loops.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "loops":
        print(json.dumps(adopt_all_command_routines(dry_run=args.dry_run), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

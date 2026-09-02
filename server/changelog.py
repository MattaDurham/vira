"""Change log — every shipped change, keyed by the day it actually happened.

Entries OWN the timeline; retros NARRATE it (redesigned 2026-09-01, plan:
TC-IL/plans/2026-09-01-1042-forge-changelog-redesign-retros-as-overlays.md).
The original 2026-07-09 design derived the log from session retros and
folded ideas/jobs into whichever retro shared their date — sound while
every session ended in a retro, but Forge jobs and mobile-resolved ideas
never close a session, so anything on a retro-less day landed in a
dateless "unfiled" bucket forever and the client rendered it "Today ·
shipped 0s ago". Inverted model:

- Every entry carries its own timestamp: a retro bullet gets the retro's
  date+time, a resolved idea its `updated`, a job its `finished or
  started`. Timestamps are PARSED AND CONVERTED to local time, never
  string-sliced — the ideas store writes UTC, the job ledger writes local,
  and `[:10]` across that mix filed entries a day off.
- Groups are LOCAL CALENDAR DAYS derived from the entries. A day with a
  retro takes its goal as the header; a day without one is `no_retro` and
  says so honestly. No entry can be unfiled, so nothing goes stale.
- Retros are overlays: zero-entry retros still contribute their goal (and
  session_id) as narrative. A job whose session_id matches a retro's
  frontmatter links to it (`entry.retro`).

PROJECT-SCOPED (2026-07-12): this change log is Vira's only. Ideas filter
to `project == "Vira"`; jobs to those run in the Vira checkout (or one of
its worktrees), or dispatched from a Vira-project idea.

Read-only: `GET /api/changelog` → { groups: [ {date, time, goal, no_retro,
retros: [{stem, time, goal, session_id}], entries: [{text, kind, ts, day,
session_id, source, job_id?, idea_id?, retro?}]} ], warnings: [str] },
newest first. kind ∈ {ship, done, dropped, job}; source ∈ {retro, idea,
job}. A retro-source entry carries `retro` (the stem of the retro it came
from) — the exact join modulestory uses to inherit that retro's module tags. A kind-"job" entry carries `job_id` so a client rendering the ledger
BESIDE the changelog — the Work window's merged Record stream — can drop
the changelog's copy of a job it already shows as a ledger row. Exclusions
are never silent: skipped files and unparseable timestamps land in
`warnings`.
"""
import re
from datetime import datetime
from pathlib import Path

from . import ideas as ideasstore
from . import joblog

SESSIONS = Path.home() / "TC-IL" / "Sessions"
PROJECT = ideasstore.DEFAULT_PROJECT          # "Vira"
REPO = Path(__file__).resolve().parent.parent  # this checkout

_STEM_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _is_project_idea(it):
    return (it.get("project") or PROJECT).strip().lower() == PROJECT.lower()


def _is_project_cwd(cwd):
    """The Vira checkout, anything inside it (worktrees live at
    .worktrees/<slug> since 2026-07-29), or a legacy sibling worktree
    named vira-<slug>. Exact-equality alone silently dropped every job
    that ran on a feature branch."""
    if not cwd:
        return False
    try:
        p = Path(cwd).expanduser().resolve()
        repo = REPO.resolve()
    except OSError:
        return False
    if p == repo or repo in p.parents:
        return True
    for anc in (p, *p.parents):
        if anc.parent == repo.parent and anc.name.startswith(repo.name + "-"):
            return True
    return False


def _clean(s):
    s = re.sub(r"`([^`]*)`", r"\1", s)      # drop code ticks
    s = s.replace("**", "")                  # drop bold markers
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _local_ts(iso):
    """(local ISO seconds, local YYYY-MM-DD) for a stored timestamp, or
    ("", "") when unparseable. Zone-aware values are converted to local;
    naive values are taken as local already."""
    if not iso:
        return ("", "")
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ("", "")
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return (dt.isoformat(timespec="seconds"), dt.strftime("%Y-%m-%d"))


def _parse_retro(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^date:\s*(.+)$", text, re.M)
    date = m.group(1).strip() if m else ""
    if not date:
        # Retro filenames lead with the day (`YYYY-MM-DD HHMM vira.md`,
        # `YYYY-MM-DD <slug>.md`); a hand-written file missing `date:`
        # frontmatter is still datable from its name, then `created:`.
        m = _STEM_DATE.match(path.stem)
        date = m.group(1) if m else ""
    if not date:
        m = re.search(r"^created:\s*(\d{4}-\d{2}-\d{2})", text, re.M)
        date = m.group(1) if m else ""
    m = re.search(r'^time:\s*"?([0-9:]+)"?', text, re.M)
    time = m.group(1).strip() if m else ""
    m = re.search(r"^session_id:\s*(\S+)", text, re.M)
    session_id = m.group(1).strip() if m else ""

    goal = ""
    gm = re.search(r"^##\s+Goal\s*\n+(.+?)(?=\n\n|\n##|\Z)", text, re.S | re.M)
    if gm:
        goal = _clean(gm.group(1))
    if not goal:
        # Day retros have no `## Goal`; their one-line title sits alone
        # under the `# …` heading and serves the same narrative purpose.
        gm = re.search(r"^#\s[^\n]+\n+([^#\n-][^\n]*)", text, re.M)
        if gm:
            goal = _clean(gm.group(1))

    entries = []
    sm = re.search(r"^##\s+Shipped\s*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    if sm:
        cur = None
        for line in sm.group(1).splitlines():
            if re.match(r"^- ", line):
                if cur is not None:
                    entries.append(cur)
                cur = line[2:]
            elif re.match(r"^\s+-\s", line) and cur is not None:
                cur += " · " + line.strip()[2:]
            elif line.strip() and cur is not None and not line.startswith("#"):
                cur += " " + line.strip()
        if cur is not None:
            entries.append(cur)
    entries = [{"text": _clean(e), "kind": "ship"} for e in entries if _clean(e)]
    return {"date": date, "time": time, "goal": goal,
            "session_id": session_id, "entries": entries}


def _job_entry(r, idea_texts):
    # The job's canonical name (an owner edit wins) heads the entry — the
    # same name the terminal title bar and Jobs list show, so a rename in
    # one place is the name everywhere. joblog.name reads the stored
    # title, falling back to the derived default.
    label = joblog.name(r, idea_texts.get(r.get("idea_id")))
    # "orphaned" used to mean "killed by server restart"; since the durable
    # runner, jobs survive restarts — orphaned now means the runner died.
    status = {"done": "done", "error": "failed", "running": "running",
              "orphaned": "runner died (orphaned)"}.get(
        r.get("status"), r.get("status", ""))
    bits = [label + " — " + status,
            Path(r.get("cwd") or "").name or "~"]
    if r.get("session_id"):
        bits.append("session " + r["session_id"][:8])
    bits.append("job " + r["id"][:8])
    if r.get("model"):
        bits.append(r["model"])
    # job_id is what lets a client that also renders the ledger dedupe —
    # the id inside the text is truncated to 8 chars and unusable as a key.
    return {"text": " · ".join(bits), "kind": "job", "job_id": r["id"]}


def _retro_files():
    """Both retro naming families: `YYYY-MM-DD HHMM vira.md` (auto and
    plain hand-written) and `YYYY-MM-DD HHMM vira — <slug>.md`
    (/close-session and named hand-written retros — 6 entry-bearing files
    the old glob never read). The `… vira (N).md` duplicate generations
    from the nightly writer's no-clobber suffix stay excluded — they are
    near-copies of a session already counted — but are counted in
    `warnings` rather than vanishing silently."""
    return sorted(SESSIONS.glob("* vira.md")) + \
        sorted(SESSIONS.glob("* vira — *.md"))


def build():
    """(groups, warnings) — the day-keyed ledger described in the module
    docstring."""
    warnings = []

    retros = []
    if SESSIONS.exists():
        for f in _retro_files():
            g = _parse_retro(f)
            if not g["date"]:
                warnings.append(
                    f"retro skipped: no recoverable date ({f.name})")
                continue
            g["stem"] = f.stem
            retros.append(g)
        dups = len(list(SESSIONS.glob("* vira ([0-9]*).md")))
        if dups:
            warnings.append(
                f"{dups} retro files skipped as duplicate generations")

    days = {}

    def bucket(day):
        return days.setdefault(day, {"entries": [], "retros": []})

    session_to_stem = {}
    for g in sorted(retros, key=lambda g: (g["date"], g["time"])):
        b = bucket(g["date"])
        b["retros"].append({"stem": g["stem"], "time": g["time"],
                            "goal": g["goal"],
                            "session_id": g["session_id"]})
        ts = g["date"] + "T" + (g["time"] or "00:00")
        for e in g["entries"]:
            # `retro` names the retro the bullet came from — the ONE exact
            # join a module story has from a shipped line back to the
            # library document (and its module tags) that narrates it.
            # session_id cannot carry that: hand-written and day retros
            # have none, which left 258 of 830 bullets unjoinable.
            b["entries"].append({**e, "ts": ts, "day": g["date"],
                                 "session_id": g["session_id"],
                                 "source": "retro", "retro": g["stem"]})
        if g["session_id"]:
            session_to_stem[g["session_id"]] = g["stem"]

    # Resolved ideas date themselves by `updated` (stored UTC → local).
    project_ideas = {}      # id -> text, for job labels + membership
    idea_entries = []
    for it in ideasstore.list_items():
        if not _is_project_idea(it):
            continue
        project_ideas[it["id"]] = it["text"]
        if it["status"] not in ("done", "dropped"):
            continue
        ts, day = _local_ts(it.get("updated") or "")
        if not day:
            warnings.append(
                f"idea skipped: no parseable updated ({it['id']})")
            continue
        idea_entries.append({"text": it["text"], "kind": it["status"],
                             "ts": ts, "day": day, "idea_id": it["id"],
                             "session_id": "", "source": "idea"})

    # Jobs date themselves by `finished or started` (already local).
    job_idea_days = set()
    job_entries = []
    for r in joblog.list_records():
        if not (_is_project_cwd(r.get("cwd"))
                or (r.get("idea_id") and r["idea_id"] in project_ideas)):
            continue
        ts, day = _local_ts(r.get("finished") or r.get("started") or "")
        if not day:
            warnings.append(
                f"job skipped: no parseable timestamp (job {r['id'][:8]})")
            continue
        e = _job_entry(r, project_ideas)
        e.update({"ts": ts, "day": day,
                  "session_id": r.get("session_id") or "", "source": "job"})
        if r.get("idea_id"):
            e["idea_id"] = r["idea_id"]
            job_idea_days.add((r["idea_id"], day))
        stem = session_to_stem.get(e["session_id"])
        if stem:
            e["retro"] = stem
        job_entries.append(e)

    # idea × job dedupe: joblog.name() titles an idea-dispatched job with
    # the idea's own text, so a resolved idea and its job on the same day
    # would render the same sentence twice — keep the job's telling.
    for e in idea_entries:
        if (e.get("idea_id"), e["day"]) in job_idea_days:
            continue
        bucket(e["day"])["entries"].append(e)
    for e in job_entries:
        bucket(e["day"])["entries"].append(e)

    out = []
    for day in sorted(days, reverse=True):
        b = days[day]
        if not b["entries"]:
            continue    # overlays only matter on days something happened
        b["entries"].sort(key=lambda e: e["ts"], reverse=True)
        goal, time = "", ""
        for r in sorted(b["retros"], key=lambda r: r["time"], reverse=True):
            if r["goal"]:
                goal, time = r["goal"], r["time"]
                break
        out.append({"date": day, "time": time, "goal": goal,
                    "no_retro": not b["retros"],
                    "retros": b["retros"], "entries": b["entries"]})
    return out, warnings


def groups():
    return build()[0]


def api():
    g, w = build()
    return {"groups": g, "warnings": w}

# History

Vira > [Modules](../../README.md) > [Fleet](../README.md) > History

One append-only store of every event on the Mac, and the API every history view reads.

This is the shared base for every history screen: Fleet's history, the
history inside a module's window, an idea card's trail. It holds no screens
of its own. Every view is a query against one store of events, at the finest
grain the sources record, kept forever.

## The store

`data/history/events-YYYY-MM.jsonl`: one JSON line per event, appended
under a file lock by the ingest process (and read by the server). An event
that changes (a session ends, a branch merges) is a new line with the same
`id`; readers keep the newest line per id, and no line is ever removed.
Month files older than the previous month are gzipped in place
(`events-YYYY-MM.jsonl.gz`); nothing else ever changes a file. The folder
is under `data/`, so each checkout (each test instance) has its own, and
it is in the daily backup (`backup.DIRS`).

An event:

| Field | What it holds |
|---|---|
| `id` | stable identity from the source's own ids: `session:vira:<job>`, `session:claude:<app session>`, `session:codex:<thread>`, `code:merged:<project>:<sha>`, `code:commit:<project>:<sha>`, `code:branch:<project>:<branch>`, `code:pr:<project>#<n>`, `run:flow:<run>`, `run:step:<run>:<card>`, `run:program:<run>`, `run:contacts:<when>`, `ask:idea:<id>`, `ask:note:<id>`, `ask:chat:<turn>`, `ask:job:<job>`, `answer:...`, `decide:...`, `made:retro:<file>`, `made:note:<id>`, `made:doc:<id>`, `made:contact:<card>`, `tell:<hash>` |
| `at`, `end` | UTC ISO times; `end` only for things with a span |
| `kind` | `ask` (you asked), `session` (a session ran), `code` (code moved), `run` (an automation ran), `made` (something was made), `answer` (an answer was given), `decide` (you decided), `tell` (Vira told you) |
| `sub` | the kind's kind: `journal`, `idea`, `chat`, `ask`, `dispatch`; `vira`, `claude`, `codex`; `commit`, `merged`, `pr_opened`, `not_merged`, `merged_branch`, `discarded`; `flow`, `step`, `program`, `contacts`; `retro`, `note`, `document`, `contact`; `landing`, `answer`, `permission`, `approval`, `review`, `approved`, `deferred`, `declined`, `dropped`, `merge`, `discard`; a channel for `tell` |
| `status` | the event's own word, one of the nine below |
| `title` | taken from the records (rules below) |
| `project` | the folder's lane, named the way Fleet names it (the repository's folder name, `Home folder`, `No folder`) |
| `folder`, `repo` | where it happened; the repository root |
| `module`, `modules` | the Vira submodule its files belong to (`server/moduletree` manifests) and `{submodule: files}` for all it touched; empty outside the Vira repository |
| `links` | lists of plain ids: `session`, `job`, `idea`, `note`, `chat`, `branch` (`<project>:<branch>`), `pr` (`<project>#<n>`), `commit`, `flow_run`, `flow`, `program_run`, `program`, `schedule`, `files`, `notes`, `doc`, `parent`, `person`, `branches_written` |
| `data` | what only that kind carries: `app`, `model`, `tokens`, `usage` (per model), `cost_usd`, `cost_basis`, `active_s`, `turns`, `cards`, `waits`, `transcript`, `open_url`, `diff` {files, added, removed}, `files_changed` [[path, added, removed]], `counts`, `errors`, `error_lines`, `logs`, `log_dir`, `text` (the ask) |

### Status words

One set for every event and every entry. Each says where the work ended up.

| Word | Means |
|---|---|
| Running | A session or run is working right now (activity in the last 10 minutes). |
| Waiting on you | It needs your answer, approval or merge: a live session blocked on you, a session that ended asking you (kept two days), a Flow approval, an open idea a session has already started on. |
| Not merged | Code on a branch, or a pull request, that has not reached main. |
| Merged | Code reached main. |
| Answered | A question got its answer, or you made a choice. |
| Ran | An automation finished with no errors. |
| Failed | It stopped with an error. |
| Dropped | You set it aside: an idea dropped or deferred, a branch discarded, a pull request closed. |
| Not started | An idea in your Ideas queue (proposed, open or on hold) that no session or Flow run has started. It is not waiting on you: it stays in the Ideas tab, and every list leaves it out unless asked (below). |

An entry's word is settled over its whole trail (`historyview.settle`):
Running, then Merged when every branch and pull request the trail names
reached main (whatever a session's last summary said), then Waiting on you
(unless a merge came after the wait), then Not merged while a branch or PR
is open, then Merged, then Dropped, then Not started for an idea with no
session or Flow run on its trail, then the automation's Ran or Failed, then
Failed when every session failed, else Answered. A session's own "Not
merged" turns into Merged or Dropped in the views once its branch did, and
its own "Waiting on you" turns into Merged once every branch and pull
request it names merged; the stored line keeps the session's own word
(`own_status` in the views).

**The idea rule (owner, 2026-11-04).** An idea counts as waiting on you
only once a session has started on it. Until then its ask is stored as
"Not started", its trail settles as "Not started", and the lists, the
`now` pin and the facet counts leave it out. `unstarted=1` (or `status=Not
started`) puts it back; every list answer says how many it left out in
`unstarted_left_out`. An idea's own history (`/entry`, `/for?idea=`) always
opens.

### Titles

Taken from the records in this order: the pull request title, the idea
title, the Flow or Program name with its counts ("Capture each link: 3
captured, 1 failed"), the title the app gave the session, the first sentence
of the ask. Session ids, branch names, hashes and record ids are removed
from every title (`history.clean_title`); a text that named a branch says
"the branch" instead.

### Cost and time

Tokens are read from Vira's per-turn usage events and from Claude Code
transcripts (each API message counted once, subagents included); Codex
reports its own token count. `cost_usd` is the API list price of those
tokens (`history.PRICES`, `cost_basis` says so); a model with no price gives
no cost rather than a partial one. `active_s` is the sum of Vira's turns, or
the time between transcript lines with gaps over five minutes left out.

## Trails

Events that share an identifier are one request (`historyview.UNION_KEYS`:
job, session, idea, journal note, chat, branch, pull request, commit, Flow
run, Program run). A trail runs from the ask through every session, wait,
pull request and merge (or drop) to where it ended, with the texts Vira
sent about it and your choices. A Flow or Program id never joins (each run
is its own trail), and neither does a session's parent.

A trail's `kind` is `request` (asked, worked on or decided), `automation`
(runs and the sessions automations started) or `record` (only things made
or texts sent). List views show requests; automations and records show
when they failed or wait on you, when a text query is given, when `kind`
names them, or with `automations=1`. Repeating automations are the grid's.

## Sources and ingest

`server/historyingest.py` reads every store that holds history:
`data/jobs-log.json` and `data/jobs/<id>/` (sessions, cards, landing
verdicts, answers, usage, turns), the Claude desktop app's session records
and their transcripts, terminal Claude Code transcripts, Codex's thread
database, git (every repository a session worked in: merges into the base
branch with their diff, each commit, branches left not merged, branches
deleted without a merge), pull requests from gh and `data/pr-index.json`,
`data/circuit-runs.json` plus `data/circuit-runs-archive.jsonl`,
`data/routine-runs.json`, `data/ideas.json`, `data/brief-journal.json`
plus its archive, `data/vira-chat.json`, `data/notify-log.json` plus its
archive, `data/review-decided.json`, `data/branch-actions.jsonl`,
`data/contacts-sync.json`, `data/reading-list.json`, the retro files in the
vault's Sessions folder (one document per retro, linked to its session) and
the Inflow's captured notes (linked to the run whose window holds the
note's creation time).

The stores that keep only their newest records now archive what they trim:
Flow runs past 120 (`circuits.RUNS_ARCHIVE()`), texts past 200
(`notify-log-archive.jsonl`), journal notes past 400
(`brief-journal-archive.jsonl`). Merge and Discard pressed on a branch are
logged to `branch-actions.jsonl` (`orphanwork.ACTIONS_LOG()`). Approvals,
Review choices, idea approve/defer/drop and landing-card verdicts are read
from where they are already kept; an idea's status change seen between two
passes is recorded as the decision.

The server runs `python -m server.historyingest tick` out of process every
minute (a `backfill` when the store is empty), one at a time
(`historyroutes.Ticker`). A pass reads only what changed: transcripts from
their last byte offset, git from the last base-branch tip, gh at most every
ten minutes. A pass that wrote anything rebuilds the views' lookup right
away on the runner's thread, so the next request does not pay for it.
State lives in `data/history/ingest-state.json`. By hand:

    .venv/bin/python -m server.historyingest backfill   # every source, all on disk
    .venv/bin/python -m server.historyingest tick
    .venv/bin/python -m server.historyingest status     # counts per kind and source

A source that fails is named in the report's `errors`; the others still
land. A second pass over unchanged stores writes nothing.

## The API

Every route is under `/api/history`. Times are UTC ISO; `since` and `until`
also take a local date (`2026-11-04`; `until` then means the end of that day).

### Entries: `GET /api/history`

| Parameter | Values |
|---|---|
| `group` | `day` (default; Running and Waiting work with something in flight is pinned in a `now` group first), `request`, `module`, `project`, `automation` (one entry per Flow, Program or contacts-sync run, grouped by automation), `session` (one entry per session, by day), `none` |
| `unit` | `trail` (default for day, request, module, project, none) or `event` (the finest grain) |
| `since`, `until` | date or time bounds |
| `project`, `module` | comma lists; a module matches its submodules (`work` matches `work/ideas`) |
| `status` | comma list of the status words (400 on any other); naming `Not started` includes unstarted ideas |
| `unstarted` | `1` to include ideas no session has started (left out by default) |
| `kind` | comma list of kinds or subs (`code`, `run`, `merged`, `retro`, ...) |
| `app` | `Vira`, `Claude Code`, `Codex` (or `vira`, `claude`, `codex`) |
| `q` | words that must all appear (titles, asks, projects, modules, branches, PRs, models) |
| `automations` | `1` to list automation and record trails too |
| `limit`, `offset` | paging over entries in group order (limit up to 1000, default 200) |

```json
{"group": "request", "unit": "trail", "total": 1, "offset": 0, "limit": 200, "shown": 1,
 "more": false, "revision": "56cd24ae274012f5", "unstarted_left_out": 1,
 "statuses": {"Running": 0, "Waiting on you": 0, "Not merged": 0, "Merged": 1,
              "Answered": 0, "Ran": 0, "Failed": 0, "Dropped": 0, "Not started": 0},
 "groups": [{"key": "all", "label": "all", "count": 1, "entries": [{
   "id": "trail:ask:note:note_a", "type": "trail", "kind": "request",
   "status": "Merged", "title": "Widget: draw in half the time",
   "at": "2026-11-03T13:36:35+00:00", "start": "2026-11-03T11:06:35+00:00",
   "project": "vira", "projects": ["vira"], "module": "work/ideas",
   "modules": {"work/ideas": 3}, "prs": ["vira#7"], "branches": ["vira:claude/widget"],
   "sessions": 1, "apps": ["Vira"], "days": 1, "events": 7,
   "kinds": {"ask": 2, "session": 1, "code": 2, "tell": 1, "decide": 1},
   "lines": {"files": 1, "added": 2, "removed": 0, "basis": "merged into main"},
   "cost_usd": 0.046, "tokens": 13000, "active_s": 660.0, "wall_s": 9000.0,
   "open": false,
   "actions": [{"action": "judge", "label": "Judge", "job": "j1", "method": "POST",
                "url": "/api/history/action",
                "body": {"id": "trail:ask:note:note_a", "action": "judge", "job": "j1"}}]}]}]}
```

An event entry (`unit=event`, `group=automation`, `group=session`) has the
same fields plus `sub`, `counts` and `trail` (the trail it belongs to).

### One entry in full: `GET /api/history/entry?id=`

`id` is a trail id, an event id, or a link key: `idea:<id>`, `job:<id>`,
`session:<id>`, `branch:<project>:<branch>`, `pr:<project>#<n>`,
`flow_run:<id>`, `program_run:<id>`, `note:<id>`. 404 when nothing matches.
The answer leads with what matters right after a session ends: the outcome
and diff, the whole trail, what it touched, cost and time, then the
actions and the things that open.

```json
{"entry": {"id": "trail:ask:note:note_a", "status": "Merged", "title": "Widget: draw in half the time", "...": "as in the list"},
 "focus": null,
 "outcome": {"status": "Merged", "means": "Code reached main.",
   "ended_by": {"id": "code:merged:vira:5e4a28cf...", "kind": "code", "sub": "merged",
                "title": "Widget: draw in half the time", "at": "2026-11-03T13:36:35+00:00"},
   "diff": {"files": 1, "added": 2, "removed": 0, "basis": "merged into main",
            "by_file": [["server/ideas.py", 2, 0]]}},
 "trail": [
   {"type": "event", "id": "ask:note:note_a", "kind": "ask", "sub": "journal", "status": "Answered",
    "title": "Make the widget faster.", "at": "2026-11-03T11:06:35+00:00",
    "links": {"note": ["note_a"], "idea": ["idea_a"], "job": ["j1"]},
    "data": {"text": "Make the widget faster. It drags."}},
   {"type": "event", "id": "ask:idea:idea_a", "kind": "ask", "sub": "idea", "status": "Merged",
    "title": "Make the widget faster", "at": "2026-11-03T11:36:35+00:00"},
   {"type": "event", "id": "session:vira:j1", "kind": "session", "sub": "vira", "status": "Merged",
    "title": "Widget: draw in half the time", "at": "2026-11-03T12:36:35+00:00",
    "end": "2026-11-03T13:24:35+00:00", "module": "work/ideas",
    "data": {"app": "Vira", "model": "claude-opus-5-5", "tokens": 13000, "cost_usd": 0.046,
             "active_s": 660.0, "kind_label": "Implement", "pr_url": "https://example.com/pull/7"}},
   {"type": "event", "id": "code:commit:vira:b0c00091...", "kind": "code", "sub": "commit",
    "status": "Merged", "title": "Widget: draw faster", "data": {"diff": {"files": 1, "added": 2, "removed": 0}}},
   {"type": "wait", "id": "session:vira:j1:card:c1", "kind": "wait", "sub": "landing",
    "status": "Answered", "title": "Waited for your merge choice",
    "at": "2026-11-03T13:06:35+00:00", "end": "2026-11-03T13:24:35+00:00"},
   {"type": "event", "id": "tell:0ab334dc85ca876f", "kind": "tell", "sub": "reply", "status": "Ran",
    "title": "waiting on you - the branch is ready - what happens to it?"},
   {"type": "event", "id": "decide:job:j1:c1", "kind": "decide", "sub": "landing", "status": "Merged",
    "title": "Merge it: Widget: draw in half the time", "data": {"verdict": "merge"}},
   {"type": "event", "id": "code:merged:vira:5e4a28cf...", "kind": "code", "sub": "merged",
    "status": "Merged", "title": "Widget: draw in half the time",
    "links": {"branch": ["vira:claude/widget"], "pr": ["vira#7"]},
    "data": {"diff": {"files": 1, "added": 2, "removed": 0}, "commits": 1}}],
 "touched": {"modules": {"work/ideas": 3}, "projects": ["vira"], "files": ["server/ideas.py"],
             "files_n": 1, "made": [], "contacts": [], "branches": ["vira:claude/widget"],
             "prs": ["vira#7"]},
 "cost_time": {"cost_usd": 0.046, "cost_basis": "API list price for these tokens",
               "cost_sessions_priced": 1, "cost_sessions_unpriced": 0, "tokens": 13000,
               "active_s": 660.0, "wall_s": 9000.0,
               "sessions": [{"id": "session:vira:j1", "app": "Vira", "model": "claude-opus-5-5",
                             "start": "...", "end": "...", "tokens": 13000, "cost_usd": 0.046,
                             "active_s": 660.0, "status": "Merged", "title": "..."}]},
 "actions": ["... as in the list"],
 "open": [{"what": "session", "label": "Open session", "job_id": "j1", "event": "session:vira:j1"},
          {"what": "pr", "label": "Open pull request", "url": "https://example.com/pull/7", "event": "session:vira:j1"}]}
```

Trail items are `event`s, `wait`s (a card the session raised, closed by
the decision that answered it) and `gap`s (more than an hour with nothing
happening). `open` lists what a front end can open: a Vira session by
`job_id` (the session replay), a Claude Code or Codex session by `url`, a
pull request, a Program run's log folder.

### From the thing itself: `GET /api/history/for`

`?idea=`, `?branch=`, `?pr=`, `?session=`, `?job=`, `?flow_run=` or
`?ref=<kind>:<value>` answer like `/entry` plus `"for": "<key>"`.
`?module=` or `?project=` (with optional `since`, `until`) answer
`{"for": {...}, "list": <the day list for it>}`.

### Automation grid: `GET /api/history/grid`

`span=day` (default, the last 14 local days) or `span=hour` (the last 48
hours); `since`, `until`, `project`. One row per Flow (`flow:<circuit id>`),
Program (`program:<run key>`), routine (`routine:<id>`) and the contacts
sync, one cell per day or hour. A cell's `outcome` is `Failed` if any run in
it failed, `Running` or `Waiting on you` while one is, `Ran` otherwise,
`missed` when a Schedule expected a run there and none started, `none` when
nothing was due. A Program inherits the Schedules of the Flow runs that ran
it. Misses are only judged from a row's first stored run (no false misses
before history began). A due time counts as missed only once its whole
interval and ten minutes more have passed (an hourly run may start at :44
and still be on time); a daily time, ten minutes after it. A cell's
`short` counts the due runs in it that never started, so a day that ran
23 of its 24 hourly runs reads `Ran` with `short: 1`; a row's `short` sums
its cells.
`tone` is a color hint: `ok`, `fail`, `live`, `wait`, `dropped`, `missed`,
`empty`.

```json
{"span": "hour", "start": "2026-11-04T05:00:00+00:00", "end": "2026-11-04T11:36:35+00:00",
 "columns": [{"start": "2026-11-04T05:00:00+00:00", "end": "2026-11-04T06:00:00+00:00", "label": "00:00"}],
 "rows": [{"id": "program:prog_export", "type": "program", "name": "Export notes",
           "project": "vault", "known_since": "2026-11-04T06:36:35+00:00",
           "schedules": [{"id": "rt_h", "name": "Every hour", "every_hours": 1.0,
                          "daily_at": "", "weekday": ""}],
           "runs": 3, "failed": 1, "missed": 2, "short": 2, "last": "2026-11-04T09:36:35+00:00",
           "cells": [{"start": "2026-11-04T08:00:00+00:00", "end": "2026-11-04T09:00:00+00:00",
                      "outcome": "missed", "tone": "missed", "runs": [], "expected": 1, "short": 1,
                      "counts": {}},
                     {"start": "2026-11-04T09:00:00+00:00", "end": "2026-11-04T10:00:00+00:00",
                      "outcome": "Ran", "tone": "ok", "expected": 1, "short": 0,
                      "counts": {"new": 2, "updated": 1},
                      "runs": [{"id": "run:program:P2", "status": "Ran",
                                "at": "2026-11-04T09:36:35+00:00",
                                "title": "Export notes: 2 new, 1 updated"}]}]}],
 "revision": "..."}
```

### Time map: `GET /api/history/timemap`

`since`, `until` (default the last 7 days), `lane=project` (default) or
`lane=module`, and the `project`, `module`, `kind`, `app` filters. Each
lane has `spans` (sessions: `start`, `end`, `app`, `active_s`), `points`
(merges into the base branch: `at`, `prs`, `added`, `removed`) and `ticks`
(automation runs: `at`, `end`, `sub`); every item carries `id`, `title`,
`status` and its `trail`. In module lanes the runs share one "Automations"
lane.

```json
{"lane": "project", "start": "...", "end": "...", "lanes": [
  {"key": "vira", "label": "vira",
   "spans": [{"id": "session:vira:j1", "title": "Widget: draw in half the time", "status": "Merged",
              "trail": "trail:ask:note:note_a", "start": "...", "end": "...", "app": "Vira", "active_s": 660.0}],
   "points": [{"id": "code:merged:vira:5e4a28cf...", "title": "Widget: draw in half the time",
               "status": "Merged", "trail": "trail:ask:note:note_a", "at": "...", "prs": ["vira#7"],
               "added": 2, "removed": 0}],
   "ticks": []}]}
```

### Filters: `GET /api/history/facets`

Each status word carries how many trails settled on it. "Not started"
counts 0 unless `unstarted=1`; `unstarted_ideas` says how many there are
either way.

```json
{"projects": [{"key": "vira", "count": 12}], "modules": [{"key": "work/ideas", "count": 5}],
 "kinds": [{"key": "ask", "label": "You asked", "count": 6}],
 "apps": [{"key": "Vira", "count": 1}],
 "statuses": [{"key": "Running", "means": "A session or run is working right now.",
               "trails": 1, "hidden_by_default": false},
              {"key": "Not started", "means": "An idea in your Ideas queue that no session has started. ...",
               "trails": 0, "hidden_by_default": true}],
 "unstarted_ideas": 1,
 "groups": ["day", "request", "module", "project", "automation", "session", "none"],
 "events": 31, "trails": 15, "since": "2026-11-02T09:36:35+00:00", "revision": "..."}
```

### Ask in plain words: `POST /api/history/ask`

Body `{"q": "did notes capture run yesterday?"}`. Vira's configured model
(`suggest.complete`, no tools) turns the question into a view spec; nothing
but the spec comes back, and every value is checked against what exists
(an unknown project, module or status word is dropped).

```json
{"status": "ok", "source": "model",
 "spec": {"view": "grid", "group": "automation", "lane": "project", "span": "day",
          "filters": {"since": "2026-11-03", "until": "2026-11-03", "project": "",
                      "module": "", "status": [], "kind": ["run"], "q": "notes capture"}}}
```

`view` is `list` (open `/api/history` with `group` and `filters`), `grid`,
`timemap` (with `lane`) or `entry` (search with `filters.q`, open the top
hit). When the model cannot answer, `status` is `model_unavailable` or
`invalid_answer`, `spec` is null, and `fallback` carries a spec read from
the words alone (`fallback_source: "keywords"`).

### Act on an entry: `POST /api/history/action`

Body `{"id": <entry or link key>, "action": "merge" | "discard" | "resume" |
"rerun" | "judge", "confirm": false, "branch"?, "job"?, "run"?, "force"?,
"prompt"?, "model"?, "provider"?, "mode"?, "read_only"?}`. Only an action
the entry lists in `actions` runs (409 otherwise), through the code the
older screens use:

- `merge`, `discard`: answer 409 `{"detail": {"needs_confirm": true,
  "confirm": "Merge claude/x into live main?"}}` until sent with
  `confirm: true`. A branch goes through `orphanwork.merge` /
  `orphanwork.discard` (branch.sh and its preflight; `force` for a dirty
  worktree); a live session waiting on its landing card is answered on that
  card (`session.answer`), so the merge waits for the session to end.
  Answers `{"started": true, "via": "branch.sh" | "landing card"}`.
- `resume`: without `prompt` answers `{"needs_prompt": true, "prompt": ...,
  "cwd": ...}` (show it for review, as Results does); with `prompt` starts
  the session through `orphanwork.resume` and answers `{"job_id": ...}`.
- `rerun`: the same Flow with the same input and Schedule entry
  (`circuits.start_run`, source `history-rerun`); a Program run reruns the
  Flow run that started it, or its routine. Answers `{"run_id": ...}`.
- `judge`: `judge.launch_judge` on the trail's newest finished coding
  session. Answers `{"judge_job_id": ...}`.

### Keeping a view current

- `GET /api/history/stream`: server-sent events; `event: rev` with
  `{"revision", "at"}` on connect and whenever the store changes, a
  keepalive comment every 15 s. Re-read what is on screen when the revision
  moves.
- `GET /api/history/rev`: `{"revision", "events", "ingest": {"running",
  "mode", "started_at", "last": <the last pass's report>}}` for polling.
- `POST /api/history/ingest`: start a pass now (`{"started": false}` when
  one is already running).

Every list, grid and time-map answer carries `revision`, so a client can
tell whether what it holds is current.

## What lives here

Everything below is generated from `module.json` and the files themselves. Scripts, assets, tests and documents that belong to this submodule are listed here even while they still sit in the shared top-level folders; see "Moving code" in the root README.

<!-- moduletree:begin - generated by `python -m server.moduletree index --write` from module.json; edit the manifest, not this block -->

**Reads from:** [Fleet / Readers](../readers/README.md), [Work / Sessions](../../work/sessions/README.md), [Work / Branches](../../work/branches/README.md), [Work / Ideas](../../work/ideas/README.md), [Work / Automations](../../work/automations/README.md)

**Feeds:** [Fleet / Window](../window/README.md)

### Files (7)

**Server**

| File | What it is |
|---|---|
| `server/history.py` | The history store: one append-only record of every event, kept forever. |
| `server/historyingest.py` | Fill the history store from every store that holds history today. |
| `server/historyroutes.py` | The history API: every front end reads history through these routes. |
| `server/historyview.py` | Views of the history store: trails, entries, groupings, the automation grid and the time map. |

**Tests**

| File | What it is |
|---|---|
| `tests/test_history.py` | The history store: appends, newest line wins, compression, times, titles, cost, the status words, and the transcript reader. |
| `tests/test_history_api.py` | Every /api/history route, on the synthetic machine of test_history_ingest.Sandbox. |
| `tests/test_history_ingest.py` | The history ingest and its joins, on a synthetic machine. |

### Routes (11)

Prefixes: `/api/history`

- `GET /api/history` (server/historyroutes.py)
- `GET /api/history/entry` (server/historyroutes.py)
- `GET /api/history/for` (server/historyroutes.py)
- `GET /api/history/grid` (server/historyroutes.py)
- `GET /api/history/timemap` (server/historyroutes.py)
- `GET /api/history/facets` (server/historyroutes.py)
- `GET /api/history/rev` (server/historyroutes.py)
- `GET /api/history/stream` (server/historyroutes.py)
- `POST /api/history/ingest` (server/historyroutes.py)
- `POST /api/history/ask` (server/historyroutes.py)
- `POST /api/history/action` (server/historyroutes.py)

### Stores (5)

Instance state under git-ignored `data/`; never hand-edit (see `AGENTS.local.md`, section 5).

- `data/history/`
- `data/circuit-runs-archive.jsonl`
- `data/notify-log-archive.jsonl`
- `data/brief-journal-archive.jsonl`
- `data/branch-actions.jsonl`

### System map

No registry record of its own yet; it is described by its module's records.

### Agent brief

`python -m server.moduletree brief fleet/history` prints this page and the full module docstring of every server file above.

<!-- moduletree:end -->

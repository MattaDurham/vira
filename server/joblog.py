"""Durable job ledger — every claude job Vira launches, recorded on disk.

data/jobs-log.json gets a record at launch, the claude session id as soon
as the CLI's init event names it (which makes the on-disk transcript at
~/.claude/projects/<cwd-slug>/<session>.jsonl findable ever after), and the
outcome at finish. Since the durable-runner build, jobs run as DETACHED
processes (server/runner.py) that outlive the server — so this module is
written for cross-process use: no in-memory cache (every operation re-reads
the store), and mutations serialize through an fcntl file lock
(server/filelock.py). The server writes the launch record; the runner
writes the session id and the finish; the supervisor writes orphan marks.

Orphan sweeping is NO LONGER automatic at import time — a runner process
imports this module too, and an import-time sweep would have marked every
other live job orphaned. The server's session supervisor calls
sweep_orphans(alive) at boot with the set of job ids it actually
re-attached to; only running records outside that set are dead.

The change log (server/changelog.py) folds these records in per date, so
every job — initial prompt, session id, outcome — shows in the Change log
tab alongside shipped work and resolved ideas. The Jobs window's History
tab renders them directly (GET /api/jobs/history).

Every record is named at launch (the Job naming section below — formerly
server/jobtitle.py): `command` is the immutable human first-command line
the terminal echoes, `title` is the short, editable session name used
everywhere the job appears (title bar, Jobs list, change log, retro).
set_title() overwrites `title` on an owner edit; `command` never changes.
The two strings derive from the launch record alone (no model call — a
job is named the instant it starts): idea dispatches read as the idea,
routine runs as the routine, right-click asks quote the owner's question,
machine-composed agent prompts name themselves by their "You are …" role,
and the triple-quoted block Vira's idea/ask prompts wrap the human text
in is the primary signal.

Record shape:
  { "id": job id, "session_id": claude session id ("" until init arrives),
    "transcript": absolute path to the CLI's session jsonl ("" until known),
    "prompt": full initial prompt, "cwd": str, "model": str|None,
    "provider": which engine answered (anthropic|openai|…),
    "permission_mode": str|None, "publish_plan": bool, "idea_id": str|None,
    "mode": str|None, "status": running|done|error|orphaned,
    "command": human first-command line, "title": short editable name,
    "subject": what the work is ABOUT (explicit, from the dispatch site),
    "about": the long-form explanation - what this is, the goal, what is
             being built (explicit, from the dispatch site),
    "kind_label": what this run IS (Land / Implement / Plan / ...; derived
                  when absent), "pr": {number, url, title, ...}|None,
    "title_edited": True once the owner has renamed it by hand,
    "started": ISO, "finished": ISO|None,
    "result": first 4000 chars of the job's final text }

The three-part name (2026-09-03). A session is displayed as
    PR #<n> - <subject> - <what this is>
composed at READ time by name(): the PR number is looked up through
server/prindex.py (it arrives after launch, when the session opens its
draft), the subject is the dispatch site's own statement of what the work
is about (falling back to the PR's title, the idea, the question, or a
humanized branch slug), and the kind is derived from the record. Before
this every Land dispatch read "Finishing stalled work in a branch-first
repository so it can LAND" - the prompt's preamble - because the name was
cut from the first line of a machine-composed prompt. An owner rename
still outranks all of it.
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from .filelock import locked

STORE = Path(__file__).resolve().parent.parent / "data" / "jobs-log.json"
RESULT_CAP = 4000
# The long-form explanation rides the ledger row; a dispatch site that
# wants to say more than this is writing a prompt, not a description.
ABOUT_CAP = 2400

_lock = threading.Lock()


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read():
    """Fresh read every time — runners and the server share this store, so
    a process-lifetime cache would clobber the other side's writes."""
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {"jobs": []}
    if not isinstance(s, dict) or "jobs" not in s:
        s = {"jobs": []}
    return s


def _write(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def _mutate(fn):
    """Run fn(store) under both locks (threads in this process + other
    processes) against a fresh read; persist if fn returns truthy."""
    with _lock, locked(STORE):
        s = _read()
        if fn(s):
            _write(s)


def _transcript_path(provider, cwd, session_id):
    """A provider-owned on-disk transcript, when Vira can verify one.

    Claude writes to the stable projects path below. Codex thread ids are
    App Server identities, not filenames, and pretending they live under
    ~/.claude made the History link confidently point at a nonexistent file.
    The session id remains the portable locator for other providers; their
    adapter may add a real transcript path once it can verify one.
    """
    if provider != "anthropic":
        return ""
    slug = re.sub(r"[/._]", "-", cwd or str(Path.home()))
    return str(Path.home() / ".claude" / "projects" / slug
               / f"{session_id}.jsonl")


# ---------- Job naming (folded in from server/jobtitle.py, 2026-07-21) ----

# Prompt scaffolding lines that are never the human intent — skipped when
# falling back to the prompt head for a free-form job.
_PREAMBLE = re.compile(
    r"^(you are |investigate|prefer read-only|finish with|click context|"
    r"the owner asks|this task comes from|research only|output only|"
    r"- component:|- person:|- text at|carry it out|end with|\"\"\")",
    re.I)

# "You are Vira's subs-visuals apply agent, running headless …" → the role
# ("subs-visuals apply agent"). Names machine-composed agent prompts by what
# the agent IS, since their role preamble spills across lines with no clean
# human ask to quote.
_ROLE = re.compile(
    r"^\s*you are\s+(?:vira'?s?\s+|an?\s+|the\s+)*(.+?)"
    r"(?:,|\s+running\b|\s+that\b|\s+which\b|\s+working\b|\s+spawned\b|"
    r"\s+inside\b|\.|\n|$)",
    re.I)


def _collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _short(text, limit):
    """Truncate to `limit` chars on a word boundary, trailing '…' if cut."""
    text = _collapse(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return (cut or text[:limit]).rstrip() + "…"


def _quoted(prompt):
    """The first triple-quoted block — where Vira wraps the human text."""
    m = re.search(r'"""\s*(.+?)\s*"""', prompt or "", re.S)
    return _collapse(m.group(1)) if m else None


def _routine_name(routine_id):
    try:
        from . import routines  # lazy — routines imports joblog back
        r = next((x for x in routines.list_routines()
                  if x.get("id") == routine_id), None)
        return r.get("name") if r else None
    except Exception:  # noqa: BLE001 — naming must never raise
        return None


def _prompt_head(prompt):
    """The human intent, or — for a machine-composed agent prompt — its
    role. Quoted block first, then the 'You are …' role, then the first
    substantive line past the scaffolding."""
    quoted = _quoted(prompt)
    if quoted:
        return quoted
    m = _ROLE.match(prompt or "")
    if m:
        role = _collapse(m.group(1))
        if role:
            return role[0].upper() + role[1:]
    for line in (prompt or "").splitlines():
        line = line.strip()
        if line and not _PREAMBLE.match(line):
            return _collapse(line)
    return _collapse(prompt)


def command(record, idea_text=None):
    """The human first-command line for a job record. Immutable — it is
    what was asked. A dispatch site that states its subject outright
    names the line as `<kind> — <subject>`, so the terminal's first-command
    echo reads as the work rather than as the prompt's preamble."""
    meta = record.get("meta") or {}
    explicit = _collapse(record.get("subject") or "")
    if explicit:
        return f"{kind_label(record)} — {explicit}"
    if meta.get("kind") == "map-refresh" or meta.get("routine_id") == "system-map":
        return "System map — refresh the registry from the change log"
    if meta.get("kind") == "judge" or meta.get("judge_of"):
        return "Judge — grade a finished job with fresh eyes"
    if meta.get("routine_id"):
        return "Routine — " + (_routine_name(meta["routine_id"])
                               or meta["routine_id"])
    if meta.get("stage"):
        return "Circuit step — " + str(meta["stage"])
    idea_text = idea_text or (_quoted(record.get("prompt", ""))
                              if record.get("idea_id") else None)
    if record.get("idea_id") and idea_text:
        verb = "Plan" if record.get("publish_plan") else "Implement"
        return f"{verb} — {_collapse(idea_text)}"
    head = _prompt_head(record.get("prompt", ""))
    quoted = _quoted(record.get("prompt", ""))
    if quoted and quoted == head and not record.get("idea_id"):
        return "Ask Vira — " + head
    return head or "(untitled job)"


# How wide a session's display name may be before it is cut with an
# ellipsis. Exported because a composer that WANTS its label to survive
# whole has to budget against the same number — applications.session_slug
# does, and a second copy of 64 would let the two drift.
TITLE_CAP = 64

# The separator between the three parts of a session's name. One string,
# read by the tests and by the client's parser.
SEP = " · "

# What a run IS, by the marks its launch record carries. Explicit
# `kind_label` on the record wins (a dispatch site that knows better);
# otherwise the ledger's own signals decide. `meta.kind` values are the
# ones dispatch sites have used since 2026-07; the fallthrough is a plain
# "Session" so nothing is ever labelled by a guess.
_KIND_BY_META = {
    "orphan-land": "Land", "orphan-resume": "Resume", "resume": "Resume",
    "text-reply": "Text reply", "chat": "Chat", "journal": "Journal",
    "board-score": "Score roles", "board-rescore": "Rescore roles",
    "profile-explore": "Profile explore", "map-refresh": "System map",
    "room-update": "Room refresh", "judge": "Judge",
    "muse": "Routine", "watch": "Routine", "digest": "Routine",
    "circuit": "Routine", "custom": "Routine",
}


def kind_label(record):
    """What this run IS - one or two words, never the subject."""
    explicit = _collapse(record.get("kind_label") or "")
    if explicit:
        return explicit
    meta = record.get("meta") or {}
    if meta.get("kind") == "orphan-land":
        return ("Diagnose and land" if meta.get("land_mode") == "diagnose"
                else "Land")
    if meta.get("kind") == "map-refresh" or meta.get("routine_id") == "system-map":
        return "System map"
    if meta.get("judge_of"):
        return "Judge"
    if meta.get("routine_id"):
        return "Routine"
    if meta.get("stage") or meta.get("circuit_run"):
        return "Flow step"
    if meta.get("define_term"):
        return "Define"
    k = _KIND_BY_META.get(meta.get("kind") or "")
    if k:
        return k
    if record.get("publish_plan"):
        return "Plan"
    if record.get("idea_id"):
        return "Implement"
    prompt = record.get("prompt", "")
    quoted = _quoted(prompt)
    if quoted and quoted == _prompt_head(prompt):
        return "Ask"
    return "Session"


# "Implement — the idea" / "Routine — muse": the command's own leading
# verb, which the three-part name states separately as the kind.
_VERB_PREFIX = re.compile(
    r"^(Implement|Plan|Ask Vira|Routine|Circuit step|Judge|System map)"
    r"\s+[—-]\s+", re.I)


def _humanize_branch(branch):
    b = (branch or "").split("/", 1)[-1]
    # the `-<jobid>` suffix session.launch appends never names anything
    b = re.sub(r"-[0-9a-f]{6}$", "", b)
    return _collapse(b.replace("-", " ").replace("_", " "))


def _pr_of(record):
    """The PR on the record, else the index's answer for its branch. The
    index is a dict read (server/prindex.py) - never a gh call."""
    pr = record.get("pr")
    if isinstance(pr, dict) and pr.get("number"):
        return pr
    branch = record.get("branch") or (record.get("meta") or {}).get("branch")
    if not branch:
        return None
    try:
        from . import prindex     # lazy: prindex imports nothing of ours
        return prindex.lookup(branch)
    except Exception:  # noqa: BLE001 — naming must never raise
        return None


def subject(record, idea_text=None):
    """What the work is ABOUT. The dispatch site's explicit statement
    wins; then the PR's own title (the session's statement of its
    subject, once it opened its draft); then the idea / question the
    command already quotes; then a humanized branch slug - which is what
    turns a legacy Land row from the prompt's preamble into the branch it
    was landing."""
    explicit = _collapse(record.get("subject") or "")
    if explicit:
        return explicit
    pr = _pr_of(record)
    if pr and pr.get("title"):
        return _collapse(pr["title"])
    cmd = command(record, idea_text)
    meta = record.get("meta") or {}
    branch = record.get("branch") or meta.get("branch")
    # A machine-composed prompt's role ("Finishing stalled work in a
    # branch-first repository so it can LAND") describes the KIND, not
    # the subject; the branch does better.
    if meta.get("kind") in ("orphan-land", "orphan-resume") and branch:
        return _humanize_branch(branch)
    stripped = _VERB_PREFIX.sub("", cmd)
    if stripped and stripped != "(untitled job)":
        return stripped
    if branch:
        return _humanize_branch(branch)
    return ""


def compose_name(pr, subj, kind, cap=TITLE_CAP):
    """`PR #n · subject · kind`. The PR and the kind ALWAYS survive whole;
    the subject is what gives way, cut on a word boundary (the
    applications.session_slug budgeting rule - the parts that say WHICH
    run this is must not be the parts an ellipsis eats)."""
    head = f"PR #{pr['number']}" if pr and pr.get("number") else ""
    tail = _collapse(kind or "")
    fixed = [x for x in (head, tail) if x]
    room = cap - sum(len(x) + len(SEP) for x in fixed)
    subj = _collapse(subj or "")
    if subj and room >= 12:
        subj = _short(subj, room)
    elif subj:
        subj = ""
    parts = [x for x in (head, subj, tail) if x]
    return SEP.join(parts) if parts else "(untitled job)"


def default_title(record, idea_text=None):
    """The session name a job is auto-given (before any edit): the
    three-part composition when the record has a subject to compose
    from, else the squeezed command as before."""
    subj = subject(record, idea_text)
    if not subj:
        return _short(command(record, idea_text), TITLE_CAP)
    return compose_name(_pr_of(record), subj, kind_label(record))


def _legacy_default(record, idea_text=None):
    return _short(command(record, idea_text), TITLE_CAP)


def name(record, idea_text=None):
    """The effective display name: an owner edit wins, else the default.

    record_launch has always STORED the derived default in `title`, so a
    stored title is not evidence of an edit. An edit is one stamped
    `title_edited` (set_title, since 2026-09-03) or - for rows written
    before the stamp existed - a title that differs from what the old
    derivation would have produced."""
    stored = (record.get("title") or "").strip()
    if stored:
        if "title_edited" in record:
            if record["title_edited"]:
                return stored
        elif stored not in (_legacy_default(record, idea_text),
                            _legacy_default(record)):
            # record_launch computed the stored default WITHOUT the idea
            # text (from the quoted block), so a legacy row is compared
            # against both readings before it counts as a hand-rename.
            return stored
    return default_title(record, idea_text)


def about(record):
    """The long-form explanation the dispatch site supplied, or ''."""
    return (record.get("about") or "").strip()


def describe(record, idea_text=None):
    """Every naming field a surface renders, derived once: the name, the
    kind, the subject, the PR, the long form, and the outcome (the
    session's own closing text, when it has one - what it ACTUALLY did,
    for rows dispatched before `about` existed)."""
    return {
        "title": name(record, idea_text),
        "kind_label": kind_label(record),
        "subject": subject(record, idea_text),
        "pr": _pr_of(record),
        "about": about(record),
        "outcome": (record.get("result") or "").strip()[:600],
    }


# ---------- ledger operations ----------

def list_records():
    return list(_read()["jobs"])


def recent(limit=100):
    """Newest-first slice for the Jobs window's History tab."""
    return list(reversed(_read()["jobs"]))[:max(1, min(int(limit), 500))]


def get_record(jid):
    return next((r for r in _read()["jobs"] if r["id"] == jid), None)


def record_launch(job):
    row = {
        "id": job["id"], "session_id": "", "transcript": "",
        "prompt": job["prompt"], "cwd": job["cwd"],
        "model": job.get("model"),
        # Which engine actually answered. Recorded at launch because the
        # live registry is the ONLY other place that knows it — once a job
        # ages out, a row without this reads as the gated Anthropic default
        # and the terminal banner can attribute the run to the wrong engine.
        "provider": job.get("provider") or "anthropic",
        "permission_mode": job.get("permission_mode"),
        "publish_plan": bool(job.get("publish_plan")),
        "idea_id": job.get("idea_id"),
        "mode": job.get("mode"),
        "read_only": bool(job.get("read_only")),
        # Where this session was allowed to write, and the tree it was kept
        # out of. Same reasoning as `provider` above: the job dir is pruned
        # at 400 and the live registry forgets on restart, so without these
        # the ledger's only forensic signal is `cwd` — which cannot tell
        # "placement declined by design" from "placement failed". Answering
        # "was that session guarded?" after the fact took a half-day
        # diagnostic on 2026-07-29; it should be one query.
        "worktree": job.get("worktree") or "",
        "branch": job.get("branch") or "",
        "live_root": job.get("live_root") or "",
        "meta": job.get("meta") or {},
        # The three-part name's inputs, from the dispatch site that knows
        # them. `subject` is what the work is about, `about` the long
        # form, `kind_label` an explicit override of the derived kind, `pr`
        # a PR already known at launch (a Land on an existing branch).
        "subject": _collapse(job.get("subject") or ""),
        "about": (job.get("about") or "").strip()[:ABOUT_CAP],
        "kind_label": _collapse(job.get("kind_label") or ""),
        "pr": job.get("pr") if isinstance(job.get("pr"), dict) else None,
        "status": "running", "started": _now(), "finished": None,
        "result": "",
    }
    # A continued conversation is about what the first one was about.
    src = job.get("resumed_from")
    if src and not row["subject"]:
        prior = get_record(src) or {}
        for k in ("subject", "about", "pr"):
            if not row.get(k) and prior.get(k):
                row[k] = prior[k]
    # Name the job at launch — the first-command line (immutable) and the
    # default session title (editable via set_title). idea_text resolves
    # from the record's own quoted block, so no cross-store read is needed.
    row["command"] = command(row)
    row["title"] = default_title(row)
    # Stamped False so name() can tell this stored default from an edit
    # without comparing strings; set_title flips it.
    row["title_edited"] = False

    def fn(s):
        s["jobs"].append(row)
        return True
    _mutate(fn)


def set_title(jid, title):
    """Rename a job (the owner's edit in the terminal title bar). Empty
    input clears the override so the derived default shows again. Returns
    the updated record, or None if the job is unknown."""
    title = (title or "").strip()[:120]
    out = {}

    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if not r:
            return False
        r["title"] = title
        # An empty title clears the edit: name() then composes the default
        # again, PR number and all.
        r["title_edited"] = bool(title)
        out["rec"] = r
        return True
    _mutate(fn)
    return out.get("rec")


def record_model_used(jid, model):
    """The generation the CLI actually resolved the launch's model to.

    `model` on the row is what was ASKED for — usually a tier alias like
    "opus" — and an alias does not always resolve to the newest generation
    of its tier. Without this the ledger cannot say which model did the
    work, and the UI shows the alias, which is a claim it has not checked.
    """
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r and model and r.get("model_used") != model:
            r["model_used"] = model
            return True
        return False
    _mutate(fn)


def record_pr(jid, number, url="", title=""):
    """Stamp the PR a session's branch now has. Called by whatever just
    learned it (the landing card opening a draft, a Land dispatch that read
    it off the sweep); the index answers for every other row."""
    if not isinstance(number, int):
        return

    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if not r:
            return False
        cur = r.get("pr") if isinstance(r.get("pr"), dict) else {}
        new = dict(cur)
        new.update({"number": number, "url": url or cur.get("url", "")})
        if title:
            new["title"] = title
        if new == cur:
            return False
        r["pr"] = new
        return True
    _mutate(fn)


def record_session(jid, session_id, transport=""):
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r and session_id:
            changed = False
            if not r["session_id"]:
                r["session_id"] = session_id
                r["transcript"] = _transcript_path(
                    r.get("provider") or "anthropic", r["cwd"], session_id)
                r["session_locator"] = session_id
                changed = True
            if transport and r.get("session_transport") != transport:
                r["session_transport"] = transport
                changed = True
            return changed
        return False
    _mutate(fn)


def record_finish(jid, status, result_text="", finished_by_owner=False):
    """`finished_by_owner` is persisted because the compose bar reads the
    LEDGER once a session leaves the live registry, and it is the one thing
    that decides whether that bar still offers to continue the conversation
    (session.resumable). Status cannot answer it: a run killed by a usage
    limit and one the owner deliberately closed both end "not running", and
    only the second is finished. Job dirs prune at ~400, so deriving it from
    state.json would silently start answering False on older sessions — the
    same shape as the provider grade that regraded itself after forty
    minutes."""
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r:
            r["status"] = status
            r["finished"] = _now()
            r["result"] = (result_text or "")[:RESULT_CAP]
            r["finished_by_owner"] = bool(finished_by_owner)
            return True
        return False
    _mutate(fn)


def record_judge(jid, verdict):
    """Stamp a fresh-eyes verdict (server/judge.py contract: grade, score,
    summary, findings, recommendation, judge_job) onto a job's record."""
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r:
            r["judge"] = verdict
            return True
        return False
    _mutate(fn)


def mark_orphaned(jid):
    """One dead job — the supervisor found its runner gone mid-flight."""
    def fn(s):
        r = next((r for r in s["jobs"] if r["id"] == jid), None)
        if r and r["status"] == "running":
            r["status"] = "orphaned"
            r["finished"] = r["finished"] or _now()
            return True
        return False
    _mutate(fn)


def sweep_orphans(alive=()):
    """Mark still-"running" records orphaned, EXCEPT the ids in `alive` —
    the detached runners the supervisor just re-attached to at boot. Called
    once at server boot by the session supervisor, never at import."""
    alive = set(alive)

    def fn(s):
        stale = [r for r in s["jobs"]
                 if r["status"] == "running" and r["id"] not in alive]
        for r in stale:
            r["status"] = "orphaned"
            r["finished"] = r["finished"] or _now()
        return bool(stale)
    _mutate(fn)

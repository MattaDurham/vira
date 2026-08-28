"""Why did that session stop?

A session that dies leaves everything needed to answer that on disk — the
recorded error, the transcript, the runner's own stderr — and until this
module nothing read any of it back. The cost was concrete and measured:
on 2026-08-28 three sessions on one branch died at the identical instant,
each one having re-read the same code and re-started the same edit, and
the only signal anywhere was a truncated error string in the job row.
Landing the branch dispatched a fourth session with a prompt that said
"carry the work to done" and no mention that three had already failed —
so it did what the others did.

Two rungs, and rung 1 stands alone:

  1. DETERMINISTIC classification. No model call, no network. Some
     failures are exactly identifiable from their own error text, and a
     harness limit is the clearest case there is: nothing about the work
     was wrong, so asking a model to "figure out what went wrong" spends
     tokens rediscovering a fact a string match already knows.

  2. The EVIDENCE BLOCK a diagnosing session is handed, so it starts from
     what is known rather than going hunting. Same discipline as
     jobboards.score_prompt and the journal dispatch: put the material in
     the prompt, and say what it is.

The split against aihealth.classify is deliberate. That module answers
"is the model backend healthy" and owns the auth/credit/limit vocabulary;
it is CALLED here rather than copied. This module answers a different
question — "why did THIS session stop, and what should be done about it"
— which includes failures the backend is not involved in at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import aihealth, jobfiles, joblog

# How much of a transcript is worth reading back. The tail is where a
# failure lives; the head is setup.
TAIL_CHARS = 4000
TOOL_LINES = 12


# ---------- the harness classes aihealth cannot see ----------

# The SDK frames the CLI's stdout as NDJSON and bounds ONE line. A message
# carrying a large file's content exceeds it and the session dies whole.
# Measured 2026-08-28: static/app.js is 1,062,221 bytes against a
# 1,048,576-byte default ceiling, and three sessions died on it in a row.
_BUFFER_RE = re.compile(
    r"exceeded maximum buffer size of (\d+) bytes", re.I)

# The transcript's own tool-call lines, written by _tool_summary:
#   "  → Edit /abs/path/static/app.js"
#   "  → Bash: grep -n ..."
_TOOL_RE = re.compile(r"^\s*→\s*(\w+)(?::\s*(.*)|\s+(.*))?$")

# A path argument inside a tool line. Absolute only — a bare word is not a
# path, and guessing one would put a fabricated filename in a diagnosis.
# A WINDOWS PATH IS A PATH. This matched only /... , so on Windows the
# transcript's C:\\Users\\...\\app.js never matched and the oversized file
# could never be named - the one fact this diagnosis exists to state.
# Shipped code a Windows install exercises, so skipping the test there
# would have hidden real coverage rather than stated a fact.
_PATH_RE = re.compile(r"([A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+)")


def _read_tail(path, limit=TAIL_CHARS):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""
    return text[-limit:]


def tool_calls(output_tail):
    """The tool calls visible in a transcript tail, oldest first. The LAST
    one is what the session was doing when it stopped, which is the single
    most useful fact about a hard failure."""
    out = []
    for line in (output_tail or "").splitlines():
        m = _TOOL_RE.match(line)
        if not m:
            continue
        tool = m.group(1)
        arg = (m.group(2) or m.group(3) or "").strip()
        out.append({"tool": tool, "arg": arg})
    return out[-TOOL_LINES:]


def _paths_in(call):
    return _PATH_RE.findall(call.get("arg") or "")


def _oversized(paths, limit):
    """Which of these paths are themselves at or over the limit. A fact
    read off the filesystem, never inferred — the point of naming a file
    in a diagnosis is that the owner can check it."""
    hits = []
    for p in paths:
        try:
            size = Path(p).stat().st_size
        except OSError:
            continue
        if limit and size >= limit * 0.9:
            hits.append({"path": p, "bytes": size})
    return hits


def classify(error, output_tail=""):
    """Why a session stopped, deterministically.

    Returns {kind, headline, why, fix, harness, certain}. `harness` marks
    a failure of the machinery rather than of the work — the distinction
    the owner needs first, because it decides whether retrying unchanged
    can possibly succeed. `certain` marks a classification that came from
    an exact tell rather than a fallback, so a caller can say "this is
    what happened" instead of "this may be what happened".

    Never raises: it is called from failure paths.
    """
    err = (error or "").strip()
    low = err.lower()

    m = _BUFFER_RE.search(err)
    if m:
        limit = int(m.group(1))
        calls = tool_calls(output_tail)
        last = calls[-1] if calls else None
        big = _oversized(_paths_in(last), limit) if last else []
        named = big[0] if big else None
        where = ""
        if last:
            where = f" It stopped on {last['tool']}"
            if named:
                where += (f" against {named['path']}, which is "
                          f"{named['bytes']:,} bytes — over the "
                          f"{limit:,}-byte ceiling")
            where += "."
        return {
            "kind": "buffer", "harness": True, "certain": True,
            "headline": "The harness refused to carry the message, not a "
                        "problem with the work",
            "why": (f"The Claude Agent SDK frames the CLI's output as one "
                    f"JSON message per line and bounds a single line at "
                    f"{limit:,} bytes. A message carrying a large file's "
                    f"content exceeds that and the session dies whole."
                    + where),
            "fix": ("Raise session_max_buffer_mb (the runner passes it as "
                    "max_buffer_size). Retrying the same edit without "
                    "raising it fails identically — the file is over the "
                    "ceiling every time."),
        }

    if not err:
        return {
            "kind": "interrupted", "harness": True, "certain": False,
            "headline": "The session stopped without recording an error",
            "why": "No error was recorded. The runner process was most "
                   "likely killed — a reboot, a manual stop, or a crash "
                   "that left no message.",
            "fix": "The work in the worktree is intact. Resuming is safe.",
        }

    # The model-backend classes belong to aihealth — call it, never copy
    # its vocabulary. Its "other" is the honest not-known answer, so it is
    # translated rather than presented as a diagnosis.
    a = aihealth.classify(err)
    if a["kind"] == "limit":
        return {"kind": "limit", "harness": True, "certain": True,
                "headline": "The model backend hit a usage limit",
                "why": "The plan's cap was reached mid-run. The login is "
                       "fine and nothing about the work was wrong.",
                "fix": "Pick it up once the limit clears — the session is "
                       "resumable and keeps its full context."}
    if a["kind"] in ("auth", "credit"):
        return {"kind": a["kind"], "harness": True, "certain": True,
                "headline": ("The model backend could not authenticate"
                             if a["kind"] == "auth"
                             else "The API account is out of credit"),
                "why": a["message"], "fix": a["message"]}

    if "permission" in low and "denied" in low:
        return {"kind": "refused", "harness": False, "certain": True,
                "headline": "A tool call was denied by the gate",
                "why": err[:400],
                "fix": "Read the denial — a write aimed at the live "
                       "checkout is denied by design, and the session "
                       "should have worked inside its worktree."}

    return {"kind": "unknown", "harness": False, "certain": False,
            "headline": "The session failed for a reason Vira cannot name",
            "why": err[:400],
            "fix": "Read the transcript before retrying — this is the case "
                   "where re-running unchanged is most likely to repeat."}


# ---------- the evidence a diagnosing session is handed ----------

def job_evidence(jid):
    """Everything on disk about one job's ending. Read-only; a missing job
    dir yields the ledger row alone rather than raising, because a pruned
    dir is ordinary (job dirs rotate at ~400)."""
    row = None
    for r in joblog.list_records():
        if r.get("id") == jid:
            row = r
            break
    if row is None:
        return None
    d = jobfiles.job_dir(jid)
    state, error = {}, ""
    try:
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        error = state.get("error") or ""
    except (OSError, ValueError):
        pass
    out_tail = _read_tail(d / "output.log")
    ev = {
        "id": jid,
        "title": row.get("title") or row.get("command") or "",
        "status": row.get("status") or state.get("status") or "",
        "started": row.get("started"), "finished": row.get("finished"),
        "model": row.get("model_used") or row.get("model") or "",
        "error": error,
        "output_tail": out_tail,
        "runner_tail": _read_tail(d / "runner.log", 1500),
        "tools": tool_calls(out_tail),
    }
    ev["diagnosis"] = classify(error, out_tail)
    return ev


def failures_for_branch(branch, limit=4):
    """Every FAILED session recorded against this branch, newest first.

    Plural on purpose. One failure is an incident; the same failure three
    times is the thing worth telling a diagnosing session, and it is the
    shape that was invisible before this module — each row read as a
    one-off because nothing ever put them side by side.
    """
    if not branch:
        return []
    rows = [r for r in joblog.list_records()
            if r.get("branch") == branch and r.get("status") == "error"]
    rows.sort(key=lambda r: r.get("finished") or r.get("started") or "",
              reverse=True)
    out = []
    for r in rows[:limit]:
        ev = job_evidence(r.get("id"))
        if ev:
            out.append(ev)
    return out


def repeated_kind(failures):
    """The failure kind shared by 2+ of these, when there is one. A
    repeat is the signal that retrying unchanged will not work."""
    kinds = [f["diagnosis"]["kind"] for f in failures
             if f.get("diagnosis", {}).get("certain")]
    for k in kinds:
        if kinds.count(k) >= 2:
            return k
    return None


def evidence_block(branch, limit=3):
    """The prompt-ready read of why this branch's sessions stopped.

    Empty string when there is nothing to report — a branch that simply
    went unfinished has no failure to diagnose, and inventing a section
    that says "no failures" would train the reader to skip it.
    """
    fails = failures_for_branch(branch, limit=limit)
    if not fails:
        return ""
    rep = repeated_kind(fails)
    lines = [f"PRIOR FAILURES ON THIS BRANCH ({len(fails)} recorded):", ""]
    if rep:
        lines += [
            f"!! {len(fails)} of these ended the SAME way ({rep}). Retrying "
            "the same step unchanged is expected to fail again — that is "
            "what this diagnosis is for.", ""]
    for i, f in enumerate(fails, 1):
        d = f["diagnosis"]
        lines.append(f"--- failure {i}: job {f['id']} "
                     f"({f.get('finished') or 'unknown time'}) ---")
        lines.append(f"Vira's read: {d['headline']}")
        lines.append(f"Why: {d['why']}")
        if d.get("fix"):
            lines.append(f"Known fix: {d['fix']}")
        if f.get("error"):
            lines.append(f"Recorded error: {f['error'][:300]}")
        if f.get("tools"):
            last = f["tools"][-1]
            lines.append(f"Last tool call before it stopped: "
                         f"{last['tool']} {last['arg'][:200]}")
        lines.append("")
    return "\n".join(lines)

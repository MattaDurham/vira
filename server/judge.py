"""Fresh-eyes judge — a clean session grades finished work.

The breadboard lab's verification methodology, productized: after a build
(or any job), a FRESH session with no shared context reads the original
ask, the produced output, and — when the job ran in a git repo — the
actual diff, then returns a structured verdict: a letter grade, per-axis
scores, findings, and a ship/fix/redo recommendation.

Judge sessions run read-only (spec read_only=True: write tools disallowed
at the SDK level AND gate-denied instantly, no hanging cards) in
manual mode, so the only reachable tools are the auto-allowed
read set + the native vira tools. The evidence (diff, transcript tail)
is computed server-side and embedded in the prompt — the judge never
needs Bash.

Verdicts are parsed from the judge's final JSON block and written back to
the judged job's ledger record (joblog.record_judge) and, when the job
was idea-linked, the idea's note. Circuits parse verdicts through the
same helpers for judge stages and grade gates.
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import jobfiles, joblog, modelbudget, settings

GRADES = ["F", "D-", "D", "D+", "C-", "C", "C+",
          "B-", "B", "B+", "A-", "A", "A+"]
POLL_S = 3.0
JUDGE_TIMEOUT = 1800

# ---- WHAT A JUDGE MAY SEE, asked of the backend that will answer ----
#
# Until 2026-08-28 this was four literals: DIFF_CAP = 30_000, OUTPUT_CAP =
# 20_000, plus an inline [:8000] on the original ask and [:4000] on the run
# context. 62_000 characters of evidence in total, roughly 18k tokens, each
# number typed once against no particular backend and never revisited - while
# the Anthropic path a judge session runs on reports a 1,000,000-token context
# window in its own response JSON.
#
# A judge is the surface where a small cap does the most damage, because it
# cannot detect its own truncation: handed the first 30k of a 400k diff it
# grades what it was shown and returns a confident letter for work it only
# partly read. That is the same signature as find.ASK_LIMIT = 8 and define's
# 9,000 characters (see server/modelbudget.py) - a cap that fails silently
# while looking correct.
#
# The class is DEEP: nobody waits on a judge, and thoroughness is the entire
# job. The four evidence channels split that budget evenly. They are CAPS,
# not allocations - a cap larger than the material costs nothing - and
# dividing by four is what keeps all four of them inside the window
# modelbudget measured, after its own template and output reserves.
#
# NOTE: the seam answers for the backend serving Vira's own model calls, which
# is not necessarily the one hosting this session (a judge stage may name its
# own model). That can only ever UNDER-spend against the session's real
# window, which is the safe direction, so it is deliberately left alone.
EVIDENCE_CHANNELS = 4

# A SESSION PROMPT IS NOT A BARE MODEL CALL, so the window is not its only
# ceiling. A judge prompt is handed to session.sessions.launch, and every
# launch writes the prompt VERBATIM into three places: data/jobs/<id>/job.json,
# the SDK's stdio transport, and - the binding one - joblog.record_launch's
# ledger row. `data/jobs-log.json` carries the full initial prompt of every job
# ever launched, is NEVER pruned (job DIRS prune at ~400; the ledger does not),
# and is re-read and re-serialized in full under a lock on every subsequent job
# write. So an unbounded prompt is a permanent, compounding store cost, not a
# per-call one.
#
# Measured on this machine while routing these caps: the learned window is
# 1,000,000 tokens, which makes the deep share 661,342 characters PER CHANNEL.
# TWO of the four channels can never reach a ceiling because they are bounded
# far below it upstream (runner.RESULT_KEEP truncates the report to 20,000; the
# context is one sentence plus circuits.EXTRA_CAP). The other two are real: the
# DIFF has no upstream cap at all, and the ASK is a composed dispatch prompt
# that genuinely gets large - measured across the live ledger's 546 rows (6.8MB
# on disk), the biggest is an applications.apply_prompt at 143,617 characters,
# with 13 rows over 64,000. So the diff is what would have written
# multi-hundred-kilobyte rows into that ledger forever, and the ask is what a
# channel cap still truncates today - eight times less harshly than the [:8000]
# it replaces.
#
# 256_000 characters of evidence is therefore the harness's ceiling rather than
# the model's, and both halves of it are checkable rather than chosen: it sits
# above the largest prompt this app has ever persisted (143,617) and ~4x under
# the SDK's own measured NDJSON line bound of 1,048,576 bytes - the bound that
# killed five sessions across three branches on 2026-08-28 - so even a full
# echo of the prompt cannot reach it. Against the old flat literals it is still
# ~4x more evidence than a judge has ever been given.
#
# THIS CEILING BELONGS IN modelbudget, not here: "a prompt bound for the session
# harness" is a class every session-dispatching caller needs, and stating it in
# one module means the next one restates it slightly differently. It is local
# only because modelbudget is owned elsewhere this pass.
SESSION_PROMPT_CHARS = 256_000


def evidence_cap():
    """Characters ONE evidence channel may carry - diff, report, ask, context.

    The window's answer, clamped by what the session harness will persist.
    Read per call, never at import: BOTH inputs are config the owner can change
    at any moment, and a value cached at import would describe whichever
    backend happened to be selected when the process started.
    """
    window = modelbudget.split("deep", parts=EVIDENCE_CHANNELS)[1]
    return max(min(window, SESSION_PROMPT_CHARS // EVIDENCE_CHANNELS), 2_000)


# How many untracked files to OPEN, and the size past which one is not read at
# all. Untracked files are the one piece of evidence `git diff` never shows, so
# a build that creates files is invisible to a judge without them. Neither
# number is a statement about the model's window - the first bounds how many
# files this function stats and reads, the second refuses to slurp a large one
# into memory - so both are left exactly as they were. Only the per-file
# TRUNCATION below was a context budget, and it was a flat [:4000]: a new
# 200-line source file was graded from its first half.
NEW_FILES = 12
NEW_FILE_BYTES = 40_000

VERDICT_CONTRACT = """Return your verdict as the FINAL thing in your reply,
as a single JSON object in a ```json code fence:

{"grade": "<A+|A|A-|B+|B|B-|C+|C|C-|D+|D|D-|F>",
 "score": <0-100>,
 "summary": "<two-sentence overall assessment>",
 "findings": [{"severity": "high|medium|low", "note": "<specific issue>"}],
 "recommendation": "ship|fix|redo"}

Grade honestly. An A means you would ship it untouched; a C means it works
but a careful reviewer would push back; an F means it does not do what was
asked. Findings must be specific and actionable, not generic advice."""


def grade_value(grade):
    """Letter grade -> ordinal (F=0 .. A+=12); None for unknown."""
    try:
        return GRADES.index((grade or "").strip().upper()
                            .replace("PLUS", "+").replace("MINUS", "-"))
    except ValueError:
        return None


def meets(grade, min_grade):
    gv, mv = grade_value(grade), grade_value(min_grade)
    if gv is None or mv is None:
        return False
    return gv >= mv


def parse_verdict(text):
    """The last JSON object containing a "grade" key anywhere in the text,
    fenced or bare. None when the judge failed to follow the contract."""
    if not text:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates += re.findall(r"(\{[^{}]*\"grade\"[^{}]*\})", text, re.S)
    for raw in reversed(candidates):
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "grade" in obj:
            obj["grade"] = str(obj["grade"]).strip().upper()
            if grade_value(obj["grade"]) is None:
                continue
            return obj
    return None


def _git_diff(cwd):
    """Working-tree diff + status for a repo cwd, capped. Empty string when
    cwd is not a git repo or git is unhappy — evidence, not a hard dep."""
    if not cwd or not (Path(cwd).expanduser() / ".git").exists():
        return ""
    cap = evidence_cap()
    try:
        status = subprocess.run(
            ["git", "status", "--short"], cwd=cwd, capture_output=True,
            text=True, timeout=20).stdout
        diff = subprocess.run(
            ["git", "diff"], cwd=cwd, capture_output=True,
            text=True, timeout=30).stdout
        untracked = [ln[3:] for ln in status.splitlines()
                     if ln.startswith("?? ")]
        extra = []
        root = Path(cwd).expanduser().resolve()
        for f in untracked[:NEW_FILES]:
            p = Path(cwd).expanduser() / f
            # Containment: never follow an untracked symlink, and never
            # read a path that resolves outside the repo root — this text
            # lands verbatim in a model prompt (audit P1-4).
            if p.is_symlink():
                continue
            try:
                if not p.resolve().is_relative_to(root):
                    continue
            except OSError:
                continue
            if p.is_file() and p.stat().st_size < NEW_FILE_BYTES:
                try:
                    # A quarter of the diff channel each, so a handful of new
                    # files can be read whole while the join's own cap below
                    # stays the thing that binds. Was a flat [:4000].
                    extra.append(f"--- new file: {f}\n"
                                 + p.read_text(errors="replace")[:max(cap // 4, 1_000)])
                except OSError:
                    pass
        return (f"git status --short:\n{status}\n\ngit diff:\n{diff}\n\n"
                + "\n\n".join(extra))[:cap]
    except Exception:  # noqa: BLE001 — evidence gathering is best-effort
        return ""


def build_prompt(ask, output, cwd=None, transcript_tail="", context=""):
    """The judge brief: original ask + evidence. Deterministic — the judge
    session itself needs no shell access."""
    diff = _git_diff(cwd)
    # One channel each: the ask, the context, the report, and the diff (or the
    # transcript tail, which is the diff's alternative and never both).
    cap = evidence_cap()
    parts = [
        "You are a JUDGE — a fresh, independent reviewer with no stake in "
        "the work. Another agent was given a task; your job is to grade "
        "what it produced. Be rigorous and specific: check the work "
        "against what was actually asked, not against effort.",
        f"THE ORIGINAL ASK:\n{(ask or '').strip()[:cap]}",
    ]
    if context:
        parts.append(f"CONTEXT:\n{context[:cap]}")
    if output:
        parts.append(f"THE WORKER'S FINAL REPORT:\n{output[:cap]}")
    if diff:
        parts.append("THE ACTUAL CHANGES ON DISK (git working tree at "
                     f"{cwd}):\n{diff}")
        parts.append("Judge the DIFF above as the primary evidence — the "
                     "report is the worker's claim, the diff is the truth. "
                     "You may Read files in the repo to verify claims.")
    elif transcript_tail:
        parts.append(f"SESSION TRANSCRIPT (tail):\n"
                     f"{transcript_tail[-cap:]}")
    parts.append(VERDICT_CONTRACT)
    return "\n\n".join(parts)


def prompt_for_job(jid):
    """Judge brief for a finished ledger job."""
    rec = joblog.get_record(jid)
    if not rec:
        raise KeyError(jid)
    output = rec.get("result") or ""
    # NOTE `output` here is the LEDGER's copy, which joblog truncates to its
    # own RESULT_CAP (4,000) on write - so this channel's cap has never been
    # what bound it on this path, and raising it alone changes nothing. The
    # tail read below is the one that binds.
    tail = jobfiles.tail_output(jobfiles.job_dir(jid), evidence_cap())
    return build_prompt(rec.get("prompt"), output, cwd=rec.get("cwd"),
                        transcript_tail=tail)


def judge_model():
    return settings.get("judge_model") or "opus"


def record_and_close(target_jid, verdict, judge_jid=None, idea_id=None):
    """The shared judge epilogue: stamp the verdict with the judge's job
    id, write it to the judged job's ledger record, and — when the judged
    job was idea-linked — append the outcome note to the idea
    (best-effort). Both judge paths end here: the ad-hoc /api/judge
    watcher below and circuits' judge stages (whose gate/retry/cascade
    logic stays in circuits.py). Returns the verdict as recorded."""
    from . import ideas
    v = dict(verdict)
    if judge_jid:
        v["judge_job"] = judge_jid
    joblog.record_judge(target_jid, v)
    if idea_id:
        try:
            ideas.stamp_note(idea_id,
                             f"judged {v['grade']} "
                             f"(job {(judge_jid or '')[:8]})",
                             append=True)
        except Exception:  # noqa: BLE001 — write-back is best-effort
            pass
    return v


def launch_judge(jid, model=None):
    """Spawn a fresh judge session over a finished job; returns the judge's
    job id. A watcher thread parses the verdict when it lands and writes it
    back to the judged job's ledger record (+ the linked idea's note)."""
    from . import session
    rec = joblog.get_record(jid)
    if not rec:
        raise KeyError(jid)
    if rec.get("status") == "running":
        raise ValueError("job is still running — judge it when it finishes")
    prompt = prompt_for_job(jid)
    judge_jid = session.sessions.launch(
        prompt, cwd=rec.get("cwd"), model=model or judge_model(),
        mode="manual", read_only=True,
        meta={"judge_of": jid})
    threading.Thread(target=_watch_judge, args=(jid, judge_jid),
                     daemon=True, name=f"vira-judge-{judge_jid}").start()
    return judge_jid


def _watch_judge(jid, judge_jid):
    from . import session
    deadline = time.time() + JUDGE_TIMEOUT
    while time.time() < deadline:
        snap = session.sessions.get(judge_jid) or joblog.get_record(judge_jid)
        status = (snap or {}).get("status", "running")
        if status != "running":
            break
        time.sleep(POLL_S)
    rec = joblog.get_record(judge_jid) or {}
    snap = session.sessions.get(judge_jid) or {}
    verdict = parse_verdict(snap.get("result_text") or rec.get("result"))
    if verdict is None:
        verdict = {"grade": "?", "score": None,
                   "summary": "judge finished without a parseable verdict",
                   "findings": [], "recommendation": ""}
    judged = joblog.get_record(jid) or {}
    record_and_close(jid, verdict, judge_jid=judge_jid,
                     idea_id=judged.get("idea_id"))

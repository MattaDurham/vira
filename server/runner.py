"""Detached durable job runner — a live agent session that outlives Vira.

Spawned by the session registry as its own process group
(.venv python -m server.runner data/jobs/<id>), so a Vira restart —
launchctl kickstart, crash, update-and-restart — no longer kills running
jobs. The runner owns the provider session end to end: it streams
the transcript to output.log, mirrors status / pending permission cards /
heartbeat into state.json, and tails control.jsonl for the owner's
steering, permission decisions, interrupts, and closes (appended by
whichever server process is up — including one booted AFTER this runner
started; the supervisor re-attaches through these same files).

Everything the in-process session had still applies here: the provider's
agent harness with the Vira preamble, the session-scoped "vira" native
tools (imported from viratools — they read Vira's data plane
directly from disk/Keychain, so they keep working even while the server
itself is down), the permission gate with timeout default-deny, plan
publishing, and closing out the launching idea. The runner finalizes its
own joblog record; the stores are cross-process safe (filelock).
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import agentbackend, jobfiles, joblog, settings, viratools, worktree
from .session import (EDIT_TOOLS, OUTPUT_CAP, READ_ONLY_EXCLUDE,
                      _extract_plan_md, _finalize_plan, _mark_idea,
                      _plan_ref, _scfg, _sdk_env, _tool_preview,
                      _tool_summary, norm_mode)

# The SDK is required only by the gated (Anthropic) path. A best-effort
# CLI-exec session must still run on a machine without it, so a failed
# import is recorded rather than fatal; the SDK path reports it loudly the
# moment a job actually needs it.
SDK_IMPORT_ERROR = None
try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    )
except Exception as e:  # noqa: BLE001 — tolerated for CLI-exec jobs
    SDK_IMPORT_ERROR = e
    AssistantMessage = ClaudeAgentOptions = ClaudeSDKClient = HookMatcher = None
    ResultMessage = None
    SystemMessage = TextBlock = ThinkingBlock = ToolUseBlock = None

    # The Vira gate is provider-neutral even when the Claude SDK is absent.
    # These two tiny stand-ins preserve its allow/deny contract for Codex;
    # the real SDK classes above are still used whenever Claude is installed.
    class PermissionResultAllow:  # noqa: D101 — compatibility value object
        pass

    class PermissionResultDeny:  # noqa: D101 — compatibility value object
        def __init__(self, message=""):
            self.message = message


# The floor is the SDK's own default, so a nonsense config value can only
# ever make the buffer BIGGER than shipping behaviour, never smaller — a
# misconfiguration must not be able to reintroduce the failure this exists
# to remove.
_SDK_DEFAULT_BUFFER = 1024 * 1024


def _max_buffer_bytes():
    """Bytes for ClaudeAgentOptions.max_buffer_size, never below the SDK
    default. Read per launch (not at import) so a config change reaches the
    next session without a restart."""
    try:
        mb = float(_scfg("session_max_buffer_mb"))
    except Exception:  # noqa: BLE001 — a bad value must not block a launch
        mb = 0
    return max(int(mb * 1024 * 1024), _SDK_DEFAULT_BUFFER)

# Who the mid-turn steering is from, in the words the model will read.
OWNER_LABEL = settings.get("owner_name") or "The owner"

HEARTBEAT = 2.0
CONTROL_POLL = 0.25
RESULT_KEEP = 20_000

# ---------- the landing card ----------
#
# A coding session that has finished its work on a branch used to sign off
# with the same prose question every time - "merge it, spin up a test
# instance, or discard?" - and a question that lives only in a transcript is
# exactly how a branch drifts into the orphan sweeper (owner, 2026-09-02: the
# whole point of the decision card is that it cannot be scrolled past). So
# the HARNESS raises the card, deterministically, the moment an owner-
# dispatched writer session parks with work on its branch. It never depends
# on the model remembering to ask, and the model is told NOT to ask.
#
# The test instance is not one of the answers any more - it is served BEFORE
# the card goes up (branch.sh serve --local: a personal snapshot is never
# auto-bridged to the tailnet), so the card carries a URL to look at rather
# than an offer to make one. The three verdicts that remain are the owner's:
LANDING_KIND = "landing"
LANDING_VERDICTS = ("merge", "keep", "discard")
LANDING_OPTIONS = [
    {"label": "Merge it",
     "description": "Land this branch on main: branch.sh merge (preflight, "
                    "the suite gate, the required PR), push, then tear the "
                    "branch and its test instance down."},
    {"label": "Keep playing",
     "description": "Leave the branch and its test instance up. Reply below "
                    "to keep working; the card comes back when the next turn "
                    "ends."},
    {"label": "Discard",
     "description": "Close the session and delete the branch, its worktree "
                    "and its test instance. The PR, if one was opened, "
                    "closes unmerged with its diff kept on GitHub."},
]
# What a session is steered with when the owner says Merge over an
# uncommitted tree. The Implement prompt tells sessions NOT to commit, so an
# uncommitted tree is the normal shape of delivered work - and branch.sh
# merge refuses a dirty worktree, so the commit has to happen first. The
# session that wrote the work writes the message; the harness never invents
# one.
COMMIT_STEER = (
    "The owner chose MERGE IT. Commit every change on this branch now with a "
    "real commit message that describes the work (git add -A && git commit "
    "in your worktree; ASCII only, no emoji). Do NOT push, do NOT merge, do "
    "NOT touch main or the live checkout. Then stop - the harness merges "
    "the moment your turn ends.")
SERVE_TIMEOUT = 600          # clone + provision + boot can take a minute+
PR_TIMEOUT = 120

# Sentinel on the steering inbox: the owner ended the reply window rather
# than answering. Distinct from a message so an empty Finish can't be
# mistaken for a blank steer.
_END = object()


class _EngineDone(Exception):
    """Control-flow only: the CLI-exec engine ran to completion inside
    run_session's try block, and the shared epilogue should proceed."""


class Runner:
    END = _END                       # exposed for agentbackend's inbox loop

    def _disarmed_guard(self):
        """The reason this session's branch-first guard cannot fire, or "".

        FAIL CLOSED. Placement and enforcement used to be independent: the
        session was moved into a worktree and the gate was armed by separate
        fields, so when the fields were dropped in transit (2026-07-25 ->
        2026-07-29) the move still happened, everything looked right, and
        the backstop was simply never there. Nothing anywhere said so.

        Binding them means a placed session that arrives without its guard
        is a hard error, not a silent fall-through. The test is the state on
        disk, not a flag someone remembered to set: if the cwd we were handed
        is a linked git worktree, placement happened, so live_root must be
        here too.
        """
        if self.spec.get("read_only"):
            return ""                       # read-only denial covers these
        cwd = self.spec.get("cwd") or ""
        if not cwd or not worktree.is_worktree(cwd):
            return ""                       # not a placed session
        if self.spec.get("worktree") and self.spec.get("live_root"):
            return ""                       # placed AND armed — the good case
        missing = [k for k in ("worktree", "live_root") if not self.spec.get(k)]
        return (f"cwd {cwd} is a linked worktree, so this session was placed "
                f"by branch-first — but the spec is missing {', '.join(missing)}, "
                f"so the guard that keeps writes out of the live checkout "
                f"cannot fire. Refusing to run rather than writing unguarded.")

    def __init__(self, jdir):
        self.dir = Path(jdir)
        self.spec = json.loads((self.dir / "job.json").read_text())
        self.disarmed = self._disarmed_guard()
        self.state = {
            "id": self.spec["id"], "status": "running",
            "started": self.spec.get("started") or time.time(),
            "finished": None, "session_id": "", "awaiting": None,
            "pending": [], "result_text": "", "heartbeat": time.time(),
            "pid": os.getpid(), "mode": self.spec["mode"], "live": True,
            "error": "",
        }
        self.out = open(self.dir / "output.log", "a", encoding="utf-8")
        self.output_tail = ""            # rolling copy (plan-URL search)
        self.inbox = asyncio.Queue()     # queued steering messages
        self.futures = {}                # req_id -> asyncio.Future
        self.session_allow = set()       # "approve for session" grants
        self.auto_allow = (set(self.spec.get("auto_allow") or [])
                           | set(viratools.TOOL_NAMES))
        self.client = None
        self.exec_proc = None            # the CLI-exec child, when that path runs
        self.closing = False
        # The owner deliberately ended it (Finish, or the close control) —
        # as opposed to a failure, a timeout, or a signal. This is the ONE
        # thing that takes the compose box away, so it must not be inferred
        # from status: a session that died on a usage limit and one the owner
        # shut both end "not running", and only one of them is finished.
        self.finished_by_owner = False
        self.interrupted = False
        self.reply_window = float(self.spec.get("reply_window") or 43200)
        self.awaiting_reply = False      # parked at a turn boundary
        # The landing verdict, once the owner has given one on the harness's
        # card: {"verdict": merge|discard}. Recorded here rather than acted
        # on, because the ACT (branch.sh merge/discard) belongs to the
        # server after this process has ended - a runner tearing down the
        # worktree it is running in would be sawing off its own branch.
        self.landing = None
        # A turn that ended on its own means the work is complete. Ending
        # the reply window after that is the owner saying "I'm done
        # talking" — NOT an abandoned run — so the epilogue (plan publish,
        # idea close-out) must still fire. Reset the moment a reply starts
        # a new turn, so a genuine mid-turn Stop still reads as aborted.
        self.finished_cleanly = False
        self._consumed = 0               # control.jsonl lines handled
        self.flush_state()

    # ----- files -----

    def flush_state(self):
        self.state["heartbeat"] = time.time()
        jobfiles.write_json_atomic(self.dir / "state.json", self.state)

    def append(self, piece):
        if not piece:
            return
        self.out.write(piece)
        self.out.flush()
        self.output_tail = (self.output_tail + piece)[-OUTPUT_CAP:]

    # ----- control stream -----

    async def steer_hook(self, _input, _tool_use_id, _ctx):
        """Deliver queued steering MID-TURN, alongside the next tool result.

        Before this, `say` only reached the model at the turn boundary in
        run_session — and a turn is however many tool calls the agent
        decides to make. The owner typed "Okay, wrap it up" and watched the
        session run twenty more calls without acknowledging it (2026-07-29);
        from the outside that is indistinguishable from steering being
        broken, and it is why "queued — delivers at the next turn boundary"
        was never a satisfying answer.

        A PostToolUse hook's `additionalContext` is the SDK's channel for
        exactly this: text handed to the model with the tool result it is
        already about to read. So the wait shrinks from "the rest of the
        turn" to "the current tool call".

        Deliberately non-blocking and total: it drains everything queued, it
        never touches the _END sentinel (that is Finish, and only
        await_reply may act on it), and any failure is swallowed — a hook
        that raises must never be able to kill a running session.
        """
        try:
            msgs, hold = [], []
            while True:
                try:
                    item = self.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                (hold if item is _END else msgs).append(item)
            for item in hold:            # put Finish back untouched
                self.inbox.put_nowait(item)
            if not msgs:
                return {}
            for m in msgs:
                self.append(f"[vira] steering delivered — {m}\n")
            body = "\n".join(str(m) for m in msgs)
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"{OWNER_LABEL} sent this mid-turn, just now. Treat it as "
                    f"a live instruction that outranks your current plan — "
                    f"act on it before continuing:\n\n{body}"),
            }}
        except Exception:  # noqa: BLE001 — a hook must never kill the session
            return {}

    async def control_loop(self):
        while True:
            self._consumed, cmds = jobfiles.read_control(
                self.dir, self._consumed)
            for cmd in cmds:
                try:
                    await self.handle(cmd)
                except Exception as e:  # noqa: BLE001 — never wedge the tail
                    self.append(f"[vira] control error: {e}\n")
            await asyncio.sleep(CONTROL_POLL)

    async def handle(self, cmd):
        op = cmd.get("op")
        if op == "say":
            text = (cmd.get("text") or "").strip()
            if text:
                # A reply typed under a landing card IS the "keep playing"
                # answer - the owner is continuing the work, so the card
                # comes down and returns when the next turn ends.
                if self.awaiting_reply:
                    self._drop_landing_card()
                self.inbox.put_nowait(text)
                self.append(f"[you] {text}\n")
                # Say what is actually about to happen. A parked session is
                # sitting on the inbox (immediate); mid-turn the steer_hook
                # hands it over at the next tool result. Neither is "the
                # next turn boundary", which is what this used to claim —
                # printing it immediately before "reply delivered" told the
                # owner two contradictory things in consecutive lines, and
                # claiming a turn-long wait when steering had genuinely
                # stopped being turn-bound would be worse (2026-07-29).
                if not self.awaiting_reply:
                    self.append("[vira] queued — delivers at the agent's "
                                "next step\n")
        elif op == "permission":
            fut = self.futures.get(cmd.get("req_id"))
            if fut is not None and not fut.done():
                fut.set_result((bool(cmd.get("allow")),
                                cmd.get("scope") or "once",
                                cmd.get("reason")))
        elif op == "answer":
            # the owner picked an option (or typed one) on a decision card
            fut = self.futures.get(cmd.get("req_id"))
            if fut is not None and not fut.done():
                fut.set_result(str(cmd.get("answer") or "").strip())
        elif op == "landing":
            await self.handle_landing(cmd)
        elif op == "interrupt":
            if self.awaiting_reply:
                # The turn is already over — Stop here is the Finish button,
                # "I have nothing to add", not an abandoned run.
                self.append("[vira] session finished by the owner\n")
                self.finished_by_owner = True
                self.inbox.put_nowait(_END)
                return
            self.interrupted = True
            self.deny_pending("interrupted by the owner")
            self.append("[vira] interrupt requested — stopping at the next "
                        "boundary…\n")
            await self.do_interrupt()
        elif op == "close":
            self.closing = True
            # Who closed it decides whether the conversation stays reachable.
            # A control-file close is the owner saying "done with this one";
            # the SIGTERM path routes through here too (system shutdown, a
            # manual kill) and must NOT read as a deliberate ending, or a
            # reboot would quietly make every open session unresumable.
            if cmd.get("why") != "signal":
                self.finished_by_owner = True
            while not self.inbox.empty():
                try:
                    self.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if self.awaiting_reply:
                self.append("[vira] session closed by the owner\n")
                self.inbox.put_nowait(_END)
                return
            self.interrupted = True
            self.deny_pending("session closed by the owner")
            self.append("[vira] session closed by the owner\n")
            await self.do_interrupt()

    async def do_interrupt(self):
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception as e:  # noqa: BLE001 — surface, don't crash
                self.append(f"[vira] interrupt failed: {e}\n")
        elif self.exec_proc is not None:
            # The CLI-exec path has no in-band interrupt; ending the child
            # ends the turn (the runner's loop then finalizes as aborted).
            try:
                self.exec_proc.terminate()
            except ProcessLookupError:
                pass

    def deny_pending(self, why):
        # Permission futures resolve to (allow, scope, reason); an ask future
        # resolves to the answer STRING. Resolving one with the other's shape
        # would hand the model a tuple as its answer, so the pending entry
        # carries the kind and this reads it.
        kinds = {p.get("req_id"): p.get("kind") for p in self.state["pending"]}
        for req_id, fut in list(self.futures.items()):
            if fut.done():
                continue
            if kinds.get(req_id) == "ask":
                fut.set_result(f"The owner ended the session ({why}). "
                               "Stop and report the open question.")
            else:
                fut.set_result((False, "once", why))

    async def heartbeat_loop(self):
        while True:
            self.flush_state()
            await asyncio.sleep(HEARTBEAT)

    # ----- the reply window -----

    def should_park(self, ok):
        """Whether THIS finished turn holds the session open.

        A method rather than an inline condition because the inline version
        was wrong and untestable: "Stop parks now" shipped on 2026-07-29
        with the fix inside await_reply and the bug in the line deciding
        whether to call it, so nothing the owner could see changed. The
        tests exercised the function and never the guard — both halves, not
        the join, exactly as with the branch guard.

        `ok or self.interrupted`: an interrupted turn is NOT a failed one,
        but the SDK reports them identically — a Stop makes the
        ResultMessage an error, so `ok` is False. A genuine failure still
        must NOT park (a dead session showing as alive for hours would hide
        the auth failures the AI-health watcher exists to catch), which is
        why this reads the interrupt explicitly instead of relaxing `ok`.
        """
        if not self.parks_at_turn_end():
            return False
        return bool(ok) or self.interrupted

    def parks_at_turn_end(self):
        """Whether a completed turn holds the session open for a reply.

        Only a session the OWNER dispatched parks — the window exists so
        the question a coding agent signs off with can be answered. A
        MACHINE-dispatched session has nobody in the terminal and its
        deliverable lands somewhere else entirely (a plan publishes in the
        epilogue, a muse's proposals sit in the Queue's approval bar, a
        judge's verdict is read off the finished run, a circuit stage's
        output feeds the next stage), so parking one only manufactures a
        fake-pending row in Live — and a parked circuit stage would stall
        its whole circuit at the barrier for the length of the window.
        Owner's ruling 2026-07-27: processes like Muse and Plan end
        cleanly with the delivered thing; the approval surfaces already
        exist.
        """
        if self.spec.get("publish_plan"):
            return False
        meta = self.spec.get("meta") or {}
        # `machine` is the generic marker for the same rule — any dispatch
        # no owner is watching (the jobboards auto-score) sets it rather
        # than growing this tuple a key at a time.
        return not (meta.get("machine") or meta.get("routine_id")
                    or meta.get("circuit_run") or meta.get("judge_of"))

    async def await_reply(self):
        """Park at a completed turn boundary with the session still open.

        Before this existed the runner ended the session the instant a turn
        finished with an empty inbox, so an agent that closed by asking the
        owner a question was already gone by the time the question was
        read — the compose bar vanishes with the session, and the answer
        had nowhere to go. Now the status stays `running` with awaiting
        "reply", which is exactly what keeps the bar live, and the run only
        finalizes when the owner says so (Finish) or the safety window
        expires. Returns the message to deliver, or None to finish.
        """
        # ONLY Finish/close ends the session here. A Stop does NOT — it ends
        # the current turn, which is a pause, not an abandonment. Stopping
        # something mid-work is precisely the moment the owner has something
        # to say to it, and until 2026-07-29 it was the one moment Vira took
        # the input box away: the agent received the queued steer, acted on
        # it, wrapped up cleanly, and the session closed anyway because
        # `interrupted` was set.
        if self.closing:
            return None
        # A cut-short turn is still NOT "finished cleanly" — that flag is
        # what decides whether the epilogue treats the run as complete
        # (publishing a plan, closing out the idea). Parking after a Stop
        # must not smuggle an interrupted run into that path; the owner can
        # reply to finish the work, and the reply's own clean turn is what
        # earns the flag.
        if not self.interrupted:
            self.finished_cleanly = True
        self.awaiting_reply = True
        # "reply" = the turn ended on its own, so the work is COMPLETE.
        # "paused" = a Stop cut it short. Both hold the box open; they are
        # separated because the surfaces must not call an interrupted run
        # complete — jobPhase reads this to decide what the LED and the
        # strip say (app.js).
        self.state["awaiting"] = "paused" if self.interrupted else "reply"
        # NOTHING IS APPENDED HERE (owner's call, 2026-07-29). This used to
        # print "turn complete — reply to keep going, or Finish to close the
        # session", which restated what the compose bar was already saying
        # in its own placeholder and its own button labels: the bar is live,
        # it reads "Reply — this session is holding open for you", and the
        # button says Finish. A line of chrome dressed as Vira speaking, at
        # the exact spot the owner reads for the session's CONCLUSION.
        # The close-out is the agent's job and is specified in the preamble
        # (viratools.preamble, "HOW TO END A TURN"); the harness must not
        # talk over it.
        self.flush_state()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self.inbox.get(),
                                                  self.reply_window)
                except asyncio.TimeoutError:
                    hrs = self.reply_window / 3600
                    self.append(f"[vira] no reply in {hrs:.0f}h — closing "
                                f"the session\n")
                    return None
                if item is _END:
                    return None
                text = (item or "").strip()
                if text:
                    return text
        finally:
            self.awaiting_reply = False
            self.state["awaiting"] = None
            self.flush_state()


    # ----- the landing card: the harness asks merge / keep / discard -----

    def landing_eligible(self):
        """Whether a parked turn of THIS session gets the harness's landing
        card. Owner-dispatched (it parks), placed on a branch by branch-first
        (there is something to land), not a plan run (its deliverable is the
        published plan), and not a Showroom candidate (the Showroom is that
        branch's own verdict surface). `landing_card` rides the spec off
        config `session_landing_card` so the owner can switch it off."""
        spec = self.spec
        if not (spec.get("worktree") and spec.get("branch")
                and spec.get("live_root")):
            return False
        if spec.get("landing_card") is False:
            return False
        if not self.parks_at_turn_end():
            return False
        meta = spec.get("meta") or {}
        if meta.get("showroom_idea"):
            return False
        return True

    def _landing_slug(self):
        return str(self.spec.get("branch") or "").split("/", 1)[-1]

    def _branch_work(self):
        """(uncommitted paths, commits ahead of main) for the session's
        branch - read off git, never off memory, because "is there anything
        to land" is the question that decides whether a card goes up at
        all. Either read failing reads as 0, which errs toward NO card: a
        card over nothing is the noise this must not add."""
        wt, root, branch = (self.spec.get("worktree"),
                            self.spec.get("live_root"), self.spec.get("branch"))
        dirty = ahead = 0
        try:
            out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                 capture_output=True, text=True, timeout=30,
                                 check=False)
            if out.returncode == 0:
                dirty = len((out.stdout or "").strip().splitlines())
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            out = subprocess.run(["git", "-C", str(root), "rev-list", "--count",
                                  f"main..{branch}"],
                                 capture_output=True, text=True, timeout=30,
                                 check=False)
            if out.returncode == 0:
                ahead = int((out.stdout or "0").strip() or 0)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return dirty, ahead

    def _branch_sh(self, argv, timeout):
        """The LIVE tree's branch.sh, run from the live root - the same call
        orphanwork and the Showroom make. Returns (ok, combined output)."""
        script = Path(self.spec["live_root"]) / "scripts" / "branch.sh"
        try:
            out = subprocess.run([str(script), *argv],
                                 cwd=self.spec["live_root"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"branch.sh {argv[0]} failed to run: {e}"
        return out.returncode == 0, ((out.stdout or "")
                                     + (out.stderr or "")).strip()

    def _serve_instance(self):
        """Serve the branch as a passive, LOCAL-ONLY test instance and return
        (url, note). --local is not optional: the snapshot behind a test
        instance is the owner's personal data, and bridging it to the tailnet
        needs his explicit approval per instance (the standing rule in
        branch.sh). A `serve` on a branch already serving prints "already
        running (pid N, port P)" and exits 0, so a second park reuses the
        instance instead of minting one per turn."""
        ok, text = self._branch_sh(["serve", self._landing_slug(), "--local"],
                                   SERVE_TIMEOUT)
        m = (re.search(r"localhost:(\d{4})", text)
             or re.search(r"port (\d{4})", text))
        if ok and m:
            return f"http://localhost:{m.group(1)}", ""
        tail = text.strip().splitlines()
        return "", (tail[-1] if tail else "no output")

    def _pr_url(self):
        """The open PR for this branch, if GitHub knows one - asked of gh,
        never inferred. Empty when there is none or gh cannot answer."""
        try:
            out = subprocess.run(
                ["gh", "pr", "view", self.spec["branch"], "--json", "url,state",
                 "--jq", '"\\(.state) \\(.url)"'],
                cwd=self.spec["live_root"], capture_output=True, text=True,
                timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return ""
        if out.returncode != 0:
            return ""
        parts = (out.stdout or "").split()
        return parts[1] if len(parts) == 2 and parts[0] == "OPEN" else ""

    def _ensure_pr(self):
        """The merge protocol REQUIRES an open PR (branch.sh pr_require), so
        a card offering Merge it over a branch with no PR would offer a
        button that fails. Open the draft here when the session did not.
        Returns (url, note); a dead gh is a NOTE on the card, never a reason
        to withhold the card - the owner can still keep playing or discard,
        and merge refuses with branch.sh's own message."""
        url = self._pr_url()
        if url:
            return url, ""
        ok, text = self._branch_sh(["pr", self._landing_slug()], PR_TIMEOUT)
        m = re.search(r"https://github\.com/\S+/pull/\d+", text)
        if ok and m:
            return m.group(0), ""
        tail = text.strip().splitlines()
        return "", ("no PR - " + (tail[-1] if tail else "branch.sh pr failed"))

    def _drop_landing_card(self):
        had = [p for p in self.state["pending"]
               if p.get("kind") == LANDING_KIND]
        if had:
            self.state["pending"] = [p for p in self.state["pending"]
                                     if p.get("kind") != LANDING_KIND]
            self.flush_state()
        return bool(had)

    async def offer_landing(self):
        """Called at a parking turn boundary, BEFORE await_reply. Returns
        True to park (with or without a card), False to finish the session
        now because a verdict is already on file - the commit turn a Merge
        steered has just ended, and the server is waiting on the ledger to
        run the merge.

        The card only goes up over WORK: a session that read and reported
        has nothing to land, and a card offering to merge nothing is what
        would teach the owner to dismiss these. The serve and the PR run in
        a thread because a data clone plus boot takes a minute; the card
        follows them so it carries a URL, not a promise."""
        if self.landing is not None:
            self.finished_cleanly = True
            self.finished_by_owner = True
            self.append(f"[vira] landing verdict on file: "
                        f"{self.landing.get('verdict')} - closing the "
                        "session so Vira can act on it\n")
            return False
        if not self.landing_eligible() or self.interrupted:
            return True
        dirty, ahead = await asyncio.to_thread(self._branch_work)
        if not dirty and not ahead:
            return True
        branch = self.spec["branch"]
        self.append(f"[vira] landing: {branch} carries "
                    f"{dirty} uncommitted path(s), {ahead} commit(s) ahead "
                    "of main - serving a test instance\u2026\n")
        test_url, serve_note = "", ""
        if self.spec.get("auto_serve", True) is not False:
            test_url, serve_note = await asyncio.to_thread(self._serve_instance)
        pr_url, pr_note = await asyncio.to_thread(self._ensure_pr)
        if test_url:
            self.append(f"[vira] test instance: {test_url}  (passive, "
                        f"local only)\n")
        elif serve_note:
            self.append(f"[vira] test instance could not start: "
                        f"{serve_note}\n")
        if pr_url:
            self.append(f"[vira] pull request: {pr_url}\n")
        elif pr_note:
            self.append(f"[vira] {pr_note}\n")
        req_id = uuid.uuid4().hex[:8]
        question = f"{branch} is ready - what happens to it?"
        self.state["pending"].append({
            "req_id": req_id, "kind": LANDING_KIND, "question": question,
            "summary": question, "options": list(LANDING_OPTIONS),
            "allow_text": False, "branch": branch,
            "worktree": self.spec.get("worktree"),
            "test_url": test_url, "serve_note": serve_note,
            "pr_url": pr_url, "pr_note": pr_note,
            "dirty": dirty, "ahead": ahead, "created": time.time(),
        })
        self.flush_state()
        return True

    async def handle_landing(self, cmd):
        """The owner's verdict on the landing card (control op `landing`).
        keep: the card comes down and the session stays parked. discard and
        merge: the session ends cleanly and finished_by_owner - the ACT is
        the server's (orphanwork.land_session waits for the ledger row to
        leave `running`, then runs branch.sh). merge over a DIRTY tree first
        steers the session to commit its own work; the verdict is held on
        `self.landing` so the turn that follows finishes instead of raising
        the card again."""
        verdict = str(cmd.get("verdict") or "").strip().lower()
        if verdict not in LANDING_VERDICTS:
            self.append(f"[vira] landing: unknown verdict {verdict!r} "
                        "ignored\n")
            return
        self._drop_landing_card()
        if verdict == "keep":
            self.append("[vira] keeping the branch and its test instance - "
                        "reply to keep working\n")
            return
        self.state["landing"] = verdict
        if verdict == "discard":
            self.landing = {"verdict": "discard"}
            self.finished_by_owner = True
            self.append("[vira] discard - closing the session; Vira tears "
                        "the branch down once it ends\n")
            self.flush_state()
            self.inbox.put_nowait(_END)
            return
        dirty, _ahead = await asyncio.to_thread(self._branch_work)
        self.landing = {"verdict": "merge"}
        if dirty and self.awaiting_reply:
            self.append(f"[vira] merge - {dirty} uncommitted path(s): asking "
                        "the session to commit its work first\n")
            self.flush_state()
            self.inbox.put_nowait(COMMIT_STEER)
            return
        self.finished_by_owner = True
        self.append("[vira] merge - closing the session; Vira merges the "
                    "branch once it ends\n")
        self.flush_state()
        self.inbox.put_nowait(_END)

    # ----- the owner channel: questions, not permissions -----

    async def ask_owner(self, question, options, allow_text=True):
        """Raise a DECISION card and block until the owner picks.

        This exists because of a real asymmetry the owner hit on 2026-07-25:
        a tool call the session could not make on its own produced a rich
        clickable Approve/Deny card, while a QUESTION the session needed
        answered produced only a line of prose above a free-text box. The
        first is impossible to miss; the second is a paragraph in a
        transcript on a phone. So the sessions that stopped to ask were the
        ones that quietly never finished.

        Same rails as the gate — a pending entry plus a future — so the card
        is delivered by the machinery that already proved it reaches the
        owner. Only `kind` differs, which is what the UI switches on.
        """
        q = (question or "").strip()
        if not q:
            return "No question was asked."
        # Options arrive as [{label, description}] (viratools.parse_options
        # normalizes both shapes). Keep the description — it is what makes
        # the choice answerable on a phone, and dropping it here would quietly
        # undo the whole point of the card.
        opts = []
        for o in (options or []):
            if isinstance(o, dict):
                label = str(o.get("label") or "").strip()
                desc = str(o.get("description") or "").strip()
            else:
                label, desc = str(o).strip(), ""
            if label:
                opts.append({"label": label, "description": desc})
        # Cap AFTER dropping blanks — a blank must not eat one of the six
        # slots the owner actually gets to choose from.
        opts = opts[:6]
        req_id = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.futures[req_id] = fut
        self.state["pending"].append({
            "req_id": req_id, "kind": "ask", "question": q,
            "options": opts, "allow_text": bool(allow_text),
            "summary": q, "created": time.time(),
        })
        self.state["awaiting"] = "ask"
        self.append(f"[vira] question for you — {q}\n")
        for i, o in enumerate(opts, 1):
            self.append(f"    {i} — {o['label']}\n")
            if o["description"]:
                self.append(f"        {o['description']}\n")
        self.flush_state()
        timeout = float(self.spec.get("ask_timeout") or 21600)
        try:
            answer = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            answer = None
        finally:
            self.futures.pop(req_id, None)
            self.state["pending"] = [p for p in self.state["pending"]
                                     if p["req_id"] != req_id]
            self.state["awaiting"] = ("permission" if self.state["pending"]
                                      else None)
            self.flush_state()
        if answer is None:
            self.append("[vira] no answer within the window — the session "
                        "should stop and report the question\n")
            # Deliberately NOT a default choice. Guessing is what produced
            # a half-applied feature in the first place; an unanswered
            # question must end the work, not silently pick a branch.
            return ("The owner did not answer in time. Do NOT guess or pick "
                    "an option yourself. Stop the work here, leave what you "
                    "have in a consistent state, and put this question at "
                    "the top of your final report.")
        self.append(f"[you] {answer}\n")
        return answer

    # ----- the permission gate -----

    async def gate(self, tool_name, tool_input, context):  # noqa: ARG002
        if self.spec.get("read_only"):
            # Read-only policy FIRST (audit P1-4): the denial outranks every
            # allow list — session grants never apply, and READ_ONLY_EXCLUDE
            # strips Task/WebSearch/the one native write from the read set.
            #
            # Keyed on read_only ALONE since 2026-08-04. publish_plan used to
            # imply it, which made "produce a plan" and "touch nothing" the
            # same switch — so a planning stage could not search the web or
            # spawn a subagent (READ_ONLY_EXCLUDE strips both) purely because
            # of what it was going to do with its output. A plan is a shape,
            # not a rung: a stage that wants both still sets read_only.
            if (tool_name in self.auto_allow
                    and tool_name not in READ_ONLY_EXCLUDE):
                return PermissionResultAllow()
            summary = _tool_summary({"name": tool_name, "input": tool_input})
            self.append(f"[vira] denied (read-only session) — {summary}\n")
            return PermissionResultDeny(
                message="This session is read-only. Do not modify anything "
                        "or retry this call — work from what the "
                        "auto-allowed read tools can see and describe any "
                        "needed change in your final report.")
        # Branch-first backstop. Placement alone does not hold: an agent can
        # still write an absolute path back into the live checkout, which is
        # exactly what happened on 2026-07-25. This denial outranks the
        # auto-allow set and session grants — like the read-only rule above,
        # because a rule that any allow-list can override is not a rule.
        wt, live_root = self.spec.get("worktree"), self.spec.get("live_root")
        if wt and live_root and tool_name in worktree.WRITE_TOOLS:
            targets = (worktree.bash_targets(tool_input) if tool_name == "Bash"
                       else worktree.target_paths(tool_input))
            for p in targets:
                if worktree.violates(p, live_root, wt):
                    self.append(f"[vira] denied (branch-first) — {tool_name} "
                                f"targets the live checkout: {p}\n")
                    return PermissionResultDeny(
                        message=worktree.deny_message(wt))
        if tool_name in self.auto_allow or tool_name in self.session_allow:
            return PermissionResultAllow()
        mode = norm_mode(self.spec.get("mode"), "manual")
        if mode == "bypassPermissions":
            # The top rung allows everything — but it is the GATE saying so,
            # not the gate being absent. Until 2026-07-29 this rung passed
            # can_use_tool=None, which removed the two denials above along
            # with the cards; the branch-first backstop the rung most needs
            # was the thing it switched off. Claude Code's own
            # bypassPermissions keeps a circuit breaker for the same reason;
            # the live-tree denial is ours.
            return PermissionResultAllow()
        if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
            # The middle rung: file edits land unasked, but commands and
            # everything else still raise a card. Deliberately below the
            # read-only denial above, which outranks every allow.
            return PermissionResultAllow()
        summary = _tool_summary({"name": tool_name, "input": tool_input})
        req_id = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.futures[req_id] = fut
        self.state["pending"].append({
            "req_id": req_id, "tool": tool_name, "summary": summary,
            "preview": _tool_preview(tool_name, tool_input),
            "created": time.time(),
        })
        self.state["awaiting"] = "permission"
        self.append(f"[vira] permission needed — {summary}\n")
        self.flush_state()
        timeout = float(self.spec.get("permission_timeout") or 600)
        try:
            allow, scope, reason = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            allow, scope, reason = False, "once", None
            self.append(f"[vira] permission timed out after {int(timeout)}s "
                        f"— denied: {summary}\n")
        finally:
            self.futures.pop(req_id, None)
            self.state["pending"] = [p for p in self.state["pending"]
                                     if p["req_id"] != req_id]
            self.state["awaiting"] = ("permission" if self.state["pending"]
                                      else None)
            self.flush_state()
        if allow:
            if scope == "session":
                self.session_allow.add(tool_name)
                self.append(f"[vira] approved for this session — "
                            f"{tool_name}\n")
            else:
                self.append(f"[vira] approved — {summary}\n")
            return PermissionResultAllow()
        note = f" ({reason})" if reason else ""
        self.append(f"[vira] denied{note} — {summary}\n")
        return PermissionResultDeny(
            message=(reason or "Denied by the owner.")
            + " Do not retry this call; adjust your approach or finish "
              "with what you have.")

    # ----- what a turn looked at -----

    # A chat turn's "sources" are the tool calls it made (virachat.py reads
    # them back as cards: the find query it ran, the note it opened, the
    # person it looked up). The transcript already prints each call as a
    # line, but a line is prose; this is the same fact as DATA, attributed
    # to the turn it belongs to. Bounded so a long session cannot grow
    # state.json without limit - the newest calls are the ones any reader
    # wants, since a chat reads the CURRENT turn's.
    TOOLS_KEEP = 120

    def record_tool(self, name, inp):
        inp = inp or {}
        keep = {}
        for k in ("query", "q", "path", "name", "person", "status", "days",
                  "start", "end", "limit"):
            v = inp.get(k) if isinstance(inp, dict) else None
            if v not in (None, ""):
                keep[k] = str(v)[:200]
        rows = list(self.state.get("tools") or [])
        rows.append({"turn": int(self.state.get("turn") or 0),
                     "name": str(name)[:80], "input": keep,
                     "t": time.time()})
        self.state["tools"] = rows[-self.TOOLS_KEEP:]
        self.flush_state()

    # ----- transcript rendering -----

    def render_message(self, msg):
        """Same shapes the in-process path produced, so renderTermLine keeps
        working. Returns (result_text, ok) on the terminal ResultMessage."""
        if isinstance(msg, AssistantMessage):
            out = ""
            for b in msg.content:
                if isinstance(b, TextBlock):
                    txt = (b.text or "").strip()
                    if txt:
                        out += txt + "\n"
                elif isinstance(b, ToolUseBlock):
                    out += "  → " + _tool_summary(
                        {"name": b.name, "input": b.input}) + "\n"
                    self.record_tool(b.name, b.input)
                elif isinstance(b, ThinkingBlock):
                    pass  # keep the log readable, as before
            self.append(out)
            return None
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            sid = msg.data.get("session_id") or ""
            if sid and not self.state["session_id"]:
                self.state["session_id"] = sid
                self.flush_state()
                joblog.record_session(self.spec["id"], sid,
                                      transport="claude-sdk")
            tail = f" (session {sid[:8]})" if sid else ""
            model = msg.data.get("model", "claude")
            # The RESOLVED generation, which is the only place it is ever
            # stated. A launch asks for a TIER ("opus") and the CLI picks the
            # id — and it does not always pick the newest: on CLI 2.1.207
            # `opus` resolves to claude-opus-4-8 while claude-opus-5 answers
            # fine. app.js has read `model_used` in three places since it was
            # written and nothing ever wrote it, so every surface fell back
            # to the requested alias and showed "Opus" over a 4.8 session.
            # Recording it is what makes a stale alias visible instead of
            # silent — the ledger keeps it after the job dir is pruned.
            if model and self.state.get("model_used") != model:
                self.state["model_used"] = model
                joblog.record_model_used(self.spec["id"], model)
            if sid:
                self.flush_state()
            self.append(f"[vira] {model} working…{tail}\n")
            return None
        if isinstance(msg, ResultMessage):
            return (msg.result or "", not msg.is_error)
        return None

    # ----- the session -----

    async def run_session(self):
        spec = self.spec
        result_text = ""
        ok = False
        try:
            self.append(spec.get("branch_note") or "")
            # A resumed run is a NEW job with its own transcript, deliberately:
            # output.log is the record of what THIS process did, and copying
            # the prior one forward would make "which job wrote this line"
            # unanswerable across the ledger. So the continuity is stated
            # instead — the owner reads which conversation this picks up, and
            # the earlier transcript stays reachable under its own job.
            if spec.get("resume_session"):
                prior = spec.get("resumed_from") or ""
                self.append(
                    f"[vira] continuing session {spec['resume_session'][:8]}"
                    + (f" (earlier transcript: job {prior})" if prior else "")
                    + " — the model has its full prior context\n")
            if self.disarmed:
                # Fail closed — see _disarmed_guard. This must raise BEFORE
                # any engine starts; a session that gets as far as its first
                # tool call has already been able to write.
                self.append(f"[vira] REFUSING TO RUN (branch-first guard "
                            f"disarmed) — {self.disarmed}\n")
                raise RuntimeError(f"branch-first guard disarmed: "
                                   f"{self.disarmed}")
            if agentbackend.uses_cli_exec(spec):
                # Provider adapter inside this same harness — inbox, reply
                # window, epilogue and policy all remain Runner-owned. Codex
                # uses App Server first and keeps exec only as compatibility.
                result_text, ok = await agentbackend.run_provider_session(self)
                raise _EngineDone
            if SDK_IMPORT_ERROR is not None:
                raise RuntimeError(
                    f"claude-agent-sdk unavailable: {SDK_IMPORT_ERROR}")
            # The owner channel: a tool the session can call when it needs a
            # DECISION, not a permission. Bound per-process because a runner
            # supervises exactly one session, so there is no ambiguity about
            # whose transcript the question belongs in.
            viratools.bind_ask(self.ask_owner)
            vira_srv = viratools.sdk_server()
            options = ClaudeAgentOptions(
                cwd=spec["cwd"],
                # See session.SESSION_DEFAULTS for why this is set at all:
                # the SDK bounds ONE NDJSON line at 1 MiB by default, and a
                # message carrying a large file's content exceeds it and
                # kills the session outright. Left unset, editing this
                # repo's own static/app.js is unsurvivable.
                max_buffer_size=_max_buffer_bytes(),
                model=spec.get("model_resolved") or spec.get("model"),
                env=_sdk_env(),
                # CONTINUE an earlier conversation rather than starting one.
                # This is what makes a session outlive its own process: the
                # reply window (await_reply) only holds a session open while
                # the runner is alive, so a usage limit, a crash, a reboot or
                # simply a week passing used to end the conversation for good
                # — the transcript survived on disk with nothing able to read
                # it back. `resume` hands the CLI the recorded session id and
                # the model arrives with its full prior context.
                #
                # Empty means a fresh session, which is every ordinary launch.
                resume=spec.get("resume_session") or None,
                # The SDK default is a near-empty system prompt; opt into the
                # full Claude Code harness prompt, with the Vira preamble
                # appended — the deep Vira connection.
                system_prompt={"type": "preset", "preset": "claude_code",
                               "append": viratools.preamble(
                                   worktree_path=spec.get("worktree") or "",
                                   branch=spec.get("branch") or "",
                                   live_root=spec.get("live_root") or "")},
                mcp_servers={"vira": vira_srv} if vira_srv else {},
                allowed_tools=list(viratools.TOOL_NAMES) if vira_srv else [],
                # ALWAYS "default" + ALWAYS our gate. Handing the SDK its own
                # bypassPermissions used to skip can_use_tool altogether,
                # which took the read-only and branch-first denials with it —
                # policy lived in the gate, so removing the gate removed the
                # policy. The bypass rung is now expressed INSIDE the gate
                # (it returns Allow), so there is exactly one place that
                # decides what a session may do, in every mode.
                permission_mode="default",
                can_use_tool=self.gate,
                # Steering reaches the model at the next TOOL RESULT, not at
                # the end of the turn — see steer_hook. matcher=None so it
                # fires after every tool, which is the point: the owner
                # should never have to wait out a fifty-call turn to be
                # heard. The turn-boundary drain in run_session stays as the
                # fallback for a turn that calls no tools at all.
                hooks={"PostToolUse": [HookMatcher(matcher=None,
                                                   hooks=[self.steer_hook])]},
                # Read-only sessions: write tools — and the excluded
                # non-reads (Task subagents, WebSearch egress) — leave the
                # model's context entirely; anything else risky is denied
                # by the gate.
                disallowed_tools=(["Write", "Edit", "NotebookEdit",
                                   "Task", "WebSearch"]
                                  if spec.get("read_only") else []),
            )
            async with ClaudeSDKClient(options) as client:
                self.client = client
                await client.query(spec["prompt"])
                done = False
                while not done:
                    async for msg in client.receive_response():
                        r = self.render_message(msg)
                        if r is not None:
                            result_text, ok = r
                    if self.closing:
                        break
                    # Turn boundary: deliver queued steering first.
                    steered = False
                    while not self.inbox.empty():
                        try:
                            item = self.inbox.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if item is _END:
                            continue
                        self.finished_cleanly = False
                        self.append("[vira] steering delivered\n")
                        await client.query(item)
                        steered = True
                    if steered:
                        continue
                    # Nothing queued. The agent has stopped talking — and it
                    # may have just ASKED the owner something (the merge /
                    # test / discard decision every branch session ends on).
                    # Hold the session open so that answer lands in this
                    # conversation instead of arriving after it died.
                    #
                    # Two runs finalize immediately instead. A FAILED turn
                    # must surface as an error now — parking it would show a
                    # dead session as alive for hours and hide exactly the
                    # auth failures the AI-health watcher exists to catch.
                    # And a PLAN session's whole deliverable is published in
                    # the epilogue, so lingering would withhold its own
                    # output; refine a plan by running Plan again. The same
                    # goes for every machine-dispatched run — see
                    # parks_at_turn_end for the full reasoning.
                    # A parked session has FINISHED its work — the turn
                    # ended on its own — so its answer exists now. Publish
                    # it before parking: state["result_text"] was only
                    # written in the epilogue, so a session waiting in its
                    # reply window reported no result at all, and anything
                    # reading the answer at the turn boundary (the reply
                    # channel texts it back) got an empty string. The
                    # epilogue's own assignment still wins at finalize.
                    self.state["result_text"] = (result_text or "")[:RESULT_KEEP]
                    park = self.should_park(ok)
                    if park:
                        park = await self.offer_landing()
                    reply = await self.await_reply() if park else None
                    if reply is None:
                        done = True
                    else:
                        self.finished_cleanly = False
                        # The reply answers the Stop, so the interrupt is
                        # served: this turn starts clean and is judged on
                        # its own ending. Without the reset, one Stop would
                        # mark every later turn of the session aborted.
                        self.interrupted = False
                        self.append("[vira] reply delivered\n")
                        # the reply opens a new TURN: tool calls from here
                        # belong to it, not to the answer just published
                        self.state["turn"] = int(self.state.get("turn") or 0) + 1
                        self.flush_state()
                        await client.query(reply)
        except _EngineDone:
            pass                     # CLI-exec engine finished; epilogue below
        except Exception as e:  # noqa: BLE001 — session surface, report all
            self.append(f"\n[vira] session failed: {e}\n")
            self.state["error"] = str(e)[:500]
            ok = False
        finally:
            self.client = None
            self.deny_pending("session ended")

        self.state["result_text"] = (result_text or "")[:RESULT_KEEP]
        # Abandoned, not merely ended: a Stop/Close that landed on a
        # COMPLETED turn (the reply window) is the owner closing the door
        # on finished work, so the plan still publishes and the idea still
        # closes out. Only a stop that cut a turn short counts as aborted.
        aborted = (self.interrupted or self.closing) and not self.finished_cleanly
        if aborted:
            self.append("[vira] session interrupted\n")
        # Plan sessions produce markdown read-only; the runner finalizes it
        # (deterministic, survives the server being down): saves it to the
        # vault as a reopenable note, and — on the owner's own machine — also
        # publishes the hosted lab page. Stay "running" until this finishes so
        # the UI streams through to the saved/published references.
        plan_res = None
        if ok and spec.get("publish_plan") and not aborted:
            md = _extract_plan_md(result_text or self.output_tail)
            self.append("\n[vira] saving the plan…\n")
            plan_res = await asyncio.to_thread(
                _finalize_plan, md, spec.get("idea_id"), spec["id"])
            self.append((
                f"[vira] plan saved: {_plan_ref(plan_res)}\n"
                if plan_res.get("plan_id") else
                "[vira] plan could not be saved — see runner.log\n"))
            if plan_res.get("url"):
                self.append(f"[vira] plan published: {plan_res['url']}\n")
        status = ("done" if ok or self.interrupted or self.closing
                  else "error")
        self.awaiting_reply = False
        self.state["status"] = status
        self.state["finished_by_owner"] = self.finished_by_owner
        self.state["awaiting"] = None
        self.state["pending"] = []
        self.state["finished"] = time.time()
        if spec.get("idea_id"):
            _mark_idea({"id": spec["id"], "idea_id": spec["idea_id"],
                        "publish_plan": spec.get("publish_plan"),
                        "plan": plan_res,
                        "output": self.output_tail},
                       ok and not aborted, interrupted=aborted)
        joblog.record_finish(spec["id"], status,
                             result_text or self.state["error"],
                             finished_by_owner=self.finished_by_owner)
        self.flush_state()

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # A polite kill (system shutdown, manual TERM) ends the turn and
            # finalizes state instead of leaving a running-forever record.
            loop.add_signal_handler(
                sig, lambda: asyncio.ensure_future(
                    self.handle({"op": "close", "why": "signal"})))
        hb = asyncio.ensure_future(self.heartbeat_loop())
        ctl = asyncio.ensure_future(self.control_loop())
        try:
            await self.run_session()
        finally:
            hb.cancel()
            ctl.cancel()
            self.out.close()


def main():
    if len(sys.argv) != 2:
        print("usage: python -m server.runner <job-dir>", flush=True)
        sys.exit(64)
    runner = Runner(sys.argv[1])
    try:
        asyncio.run(runner.run())
    except Exception as e:  # noqa: BLE001 — last-resort finalization
        runner.state["status"] = "error"
        runner.state["error"] = str(e)[:500]
        runner.state["finished"] = time.time()
        try:
            jobfiles.write_json_atomic(runner.dir / "state.json",
                                       runner.state)
            joblog.record_finish(runner.spec["id"], "error", str(e))
        finally:
            raise


if __name__ == "__main__":
    main()

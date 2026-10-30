"""A code card's code: what the file says it does, the code itself, and
changes made by typing what to change.

A Program card runs a file: the first argument of its command that is an
existing code file, read against the folder it runs in (``caffeinate -i
python3 -u scripts/step.py --jobs 3`` runs ``scripts/step.py``). A source
card of kind script or file points at one. For either, the Forge's right
column shows:

  - what the code does, in plain words: the description the file opens
    with (a Python module docstring, or the comment block at the top of a
    shell or other script). The description lives in the file, so it moves
    with the code and a change keeps it true;
  - the code;
  - a box to type what to change. ``change`` backs the file up, then starts
    a session told to edit that one file and to keep its description true.
    ``change_status`` reports the session and, once it ends, the diff from
    the backup; ``undo`` puts the backup back.

Records: data/card-code-changes.json, newest last. Backups:
data/card-code/<change id>/<file name>, kept with the record so any change
can be undone later.

Over the network (the tailnet, not this Mac's own keyboard) a card's code
is shown and changed only where a source card's preview may be shown
(flows._readable): the Claude library and the project's instruction and
settings files. At the keyboard, any file. Every call takes `local`, which
the route sets from where the request came from.
"""
import ast
import datetime as dt
import difflib
import re
import secrets
import shutil
from pathlib import Path

from . import flows, jsonstore, routines

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "card-code-changes.json"
BACKUPS = ROOT / "data" / "card-code"
CODE_SUFFIXES = {".py", ".sh", ".zsh", ".bash", ".js", ".mjs", ".ts", ".rb",
                 ".pl", ".swift", ".applescript", ".scpt"}
# The panel shows the whole file; a file past this is cut and says so. A
# step script is a few hundred lines; this keeps one stray minified bundle
# from filling the response.
MAX_CODE = 400_000
# What the owner types into the change box: a few sentences, not a spec.
MAX_INSTRUCTION = 4000
# History kept per store; the oldest records (and their backups) go first.
MAX_CHANGES = 400
LIVE = {"running", "queued", "waiting", "starting"}


class CodeError(ValueError):
    pass


class NotShown(CodeError):
    """The file is outside what Vira shows over the network."""


def _guard(path, local):
    if not flows._readable(Path(path), None, local):
        raise NotShown("That file is outside what Vira shows over the network; open the Forge on this Mac to see or "
                       "change it.")


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _blank():
    return {"changes": []}


def program_file(command_line, cwd=""):
    """The code file a command runs, or None. Reads the arguments left to
    right and takes the first one that names an existing file with a code
    suffix, against `cwd` when it is relative."""
    try:
        argv = routines._split_command(command_line)
    except ValueError:
        return None
    base = Path(cwd).expanduser() if cwd else None
    for arg in argv:
        if arg.startswith("-"):
            continue
        path = Path(arg).expanduser()
        if not path.is_absolute():
            if base is None:
                continue
            path = base / path
        if path.suffix.lower() in CODE_SUFFIXES and path.is_file():
            return path.resolve()
    return None


def _python_description(text):
    try:
        return ast.get_docstring(ast.parse(text), clean=True) or ""
    except (SyntaxError, ValueError):
        return ""


def _comment_description(text):
    """The comment block a script opens with, past its #! line."""
    lines = text.splitlines()
    out = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started and (stripped.startswith("#!") or not stripped):
            continue
        if stripped.startswith(("#", "//", "--")):
            started = True
            out.append(re.sub(r"^(#+|//+|--)\s?", "", stripped))
            continue
        break
    return "\n".join(out).strip()


def description(path, text):
    if Path(path).suffix.lower() == ".py":
        return _python_description(text)
    return _comment_description(text)


def resolve(path="", command="", cwd=""):
    if path:
        p = Path(path).expanduser()
        if not p.is_absolute() and cwd:
            p = Path(cwd).expanduser() / p
        return p.resolve() if p.exists() else p
    found = program_file(command, cwd)
    if not found:
        raise CodeError("its command names no code file that exists")
    return found


def view(path="", command="", cwd="", local=True):
    """The file, the description it opens with, and its recent changes."""
    p = resolve(path, command, cwd)
    _guard(p, local)
    if not p.is_file():
        return {"path": str(p), "exists": False, "changes": changes_for(str(p))}
    raw = p.read_bytes()
    text = raw[:MAX_CODE].decode("utf-8", errors="replace")
    stat = p.stat()
    return {
        "path": str(p), "exists": True, "name": p.name,
        "description": description(p, text),
        "text": text, "cut": len(raw) > MAX_CODE,
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "modified": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
        "changes": changes_for(str(p)),
    }


def _prompt(path, instruction, card="", flow=""):
    where = f" It is the code of the card \"{card}\"" if card else ""
    if where and flow:
        where += f" in the Flow \"{flow}\""
    return (
        f"Change one file: {path}.{where + '.' if where else ''}\n\n"
        f"What the owner wants changed:\n{instruction}\n\n"
        "How to do it:\n"
        "- Read the whole file first, and any file it imports from the same folder, so the change fits the code.\n"
        "- Edit only this file. If the change cannot be made in this file alone, change nothing and say what else "
        "would have to change.\n"
        "- The file opens with a description of what it does, in plain words. Keep it true: rewrite the parts the "
        "change makes wrong, and add a description if the file has none.\n"
        "- Do not run the program; it may touch real data.\n"
        "- End with two or three plain sentences: what you changed, and anything the owner should check."
    )


def _launch(prompt, cwd, subject, about):
    from . import session
    return session.sessions.launch(
        prompt, cwd=cwd, mode="acceptEdits",
        meta={"card_code": True}, subject=subject, about=about,
        kind_label="Code change")


def change(path, instruction, flow_id="", card_id="", card="", flow="", local=True):
    """Back the file up and start the session that changes it."""
    instruction = str(instruction or "").strip()
    if not instruction:
        raise CodeError("type what to change")
    if len(instruction) > MAX_INSTRUCTION:
        raise CodeError(f"keep the instruction under {MAX_INSTRUCTION} characters")
    p = Path(path).expanduser()
    if not p.is_absolute() or not p.is_file():
        raise CodeError(f"{path} is not a file on this Mac")
    p = p.resolve()     # the same spelling view() lists its changes under
    _guard(p, local)
    cid = "cc_" + secrets.token_hex(4)
    folder = BACKUPS / cid
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / p.name
    shutil.copy2(p, backup)
    rec = {
        "id": cid, "path": str(p), "flow_id": str(flow_id or "")[:100],
        "card_id": str(card_id or "")[:100], "card": str(card or "")[:200],
        "instruction": instruction, "backup": str(backup), "started": _now(),
        "status": "starting", "job_id": None,
    }
    jsonstore.mutate(STORE, lambda s: s["changes"].append(rec), _blank(), indent=1)
    try:
        jid = _launch(_prompt(p, instruction, card, flow), str(p.parent),
                      subject=f"{card or p.name}: change the code",
                      about=f"Changing {p} as the owner asked: {instruction[:400]}")
    except Exception as e:  # the record says why; nothing was changed
        _update(cid, status="failed", note=f"the session did not start: {e}")
        raise CodeError(f"the session did not start: {e}")
    return _update(cid, status="running", job_id=jid)


def _update(cid, **fields):
    out = {}

    def apply(state):
        for rec in state["changes"]:
            if rec["id"] == cid:
                rec.update(fields)
                out.update(rec)
        _prune(state)
    jsonstore.mutate(STORE, apply, _blank(), indent=1)
    return out


def _prune(state):
    extra = len(state["changes"]) - MAX_CHANGES
    if extra <= 0:
        return
    for rec in state["changes"][:extra]:
        shutil.rmtree(BACKUPS / rec["id"], ignore_errors=True)
    del state["changes"][:extra]


def get(cid):
    return next((dict(r) for r in jsonstore.read(STORE, _blank()).get("changes", []) if r.get("id") == cid), None)


def changes_for(path, limit=10):
    rows = [r for r in jsonstore.read(STORE, _blank()).get("changes", []) if r.get("path") == path]
    return [{k: r.get(k) for k in ("id", "instruction", "started", "status", "finished", "summary")}
            for r in rows[-limit:]][::-1]


def _job(jid):
    from . import joblog, session
    snap = session.sessions.get(jid) if jid else None
    return snap or (joblog.get_record(jid) if jid else None) or {}


def diff(rec):
    try:
        before = Path(rec["backup"]).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return None
    try:
        after = Path(rec["path"]).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        after = []
    name = Path(rec["path"]).name
    return "".join(difflib.unified_diff(before, after, f"{name} before", f"{name} now"))


def change_status(cid, local=True):
    """The change's record, its session's state and, once that has ended,
    the diff between the backup and the file as it is now."""
    rec = get(cid)
    if not rec:
        raise CodeError("unknown change")
    _guard(rec["path"], local)
    if rec.get("status") == "running":
        job = _job(rec.get("job_id"))
        status = str(job.get("status") or "running")
        # A Vira session stays open after its turn, waiting for a reply
        # (the owner can ask it for more); its turn ending is the change
        # being made.
        if status not in LIVE or job.get("awaiting") == "reply":
            text = diff(rec) or ""
            said = job.get("result_text") or job.get("result") or ""
            rec = _update(cid, status="done" if text else "unchanged", finished=_now(),
                          summary=str(said).strip()[:4000])
    out = dict(rec)
    if out.get("status") in ("done", "unchanged", "undone"):
        out["diff"] = diff(out) or ""
    return out


def undo(cid, local=True):
    """Put the backup back. The text being replaced is kept beside it."""
    rec = get(cid)
    if not rec:
        raise CodeError("unknown change")
    _guard(rec["path"], local)
    if rec.get("status") == "running":
        raise CodeError("the session is still changing it")
    backup = Path(rec["backup"])
    if not backup.is_file():
        raise CodeError("its backup is gone")
    target = Path(rec["path"])
    if target.is_file():
        shutil.copy2(target, backup.with_name(backup.name + ".before-undo"))
    shutil.copy2(backup, target)
    return _update(cid, status="undone", undone=_now())

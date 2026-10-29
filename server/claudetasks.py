"""Claude's scheduled task files on this Mac, for The Forge's one list.

The Claude app keeps each scheduled task as a folder under
~/.claude/scheduled-tasks/<name>/ holding one SKILL.md: frontmatter (name,
description) and the prompt the task runs. Its schedule lives in the
Claude app's own storage, which Vira cannot read, so a task row says that
plainly and offers what Vira can do: edit the file, archive it, restore
it, or give it a Vira schedule (server/adopt.py turns it into a Flow).

Archive moves the folder to ~/.claude/scheduled-tasks-archive/, which the
Claude app does not read, so an archived task cannot run there either.
Every save keeps the previous file in that archive's backups/.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# A task file is a prompt; past this it is not one a person wrote.
MAX_FILE = 200_000


def tasks_dir():
    return Path.home() / ".claude" / "scheduled-tasks"


def archive_dir():
    return Path.home() / ".claude" / "scheduled-tasks-archive"


def _front(text):
    """(frontmatter dict, body) of a SKILL.md; frontmatter keys are the
    plain `key: value` lines between the opening and closing ---."""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body


def _folder(name, archived=False):
    if not NAME.fullmatch(str(name or "")):
        raise ValueError("a task is named by its folder name")
    folder = (archive_dir() if archived else tasks_dir()) / name
    if not (folder / "SKILL.md").is_file():
        raise FileNotFoundError(str(folder / "SKILL.md"))
    return folder


def _row(folder, archived):
    path = folder / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE]
    except OSError as e:
        return {"name": folder.name, "path": str(path), "title": folder.name,
                "description": f"not readable: {e}", "archived": None}
    meta, _ = _front(text)
    stamp = None
    if archived:
        stamp = datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(
            timespec="seconds")
    return {"name": folder.name, "path": str(path),
            "title": meta.get("name") or folder.name,
            "description": meta.get("description") or "",
            "archived": stamp}


def list_tasks():
    out = []
    for archived, root in ((False, tasks_dir()), (True, archive_dir())):
        try:
            folders = sorted(p for p in root.iterdir() if p.is_dir() and p.name != "backups")
        except OSError:
            continue
        for folder in folders:
            if (folder / "SKILL.md").is_file():
                out.append(_row(folder, archived))
    return out


def read(name, archived=False):
    folder = _folder(name, archived)
    text = (folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    meta, body = _front(text)
    return {"name": name, "path": str(folder / "SKILL.md"), "text": text,
            "title": meta.get("name") or name, "description": meta.get("description") or "",
            "body": body, "archived": bool(archived)}


def save(name, text, archived=False):
    """Replace the task file, keeping the previous copy in backups/."""
    text = str(text or "")
    if not text.strip():
        raise ValueError("a task file cannot be empty")
    if len(text) > MAX_FILE:
        raise ValueError("a task file is at most 200,000 characters")
    folder = _folder(name, archived)
    path = folder / "SKILL.md"
    backups = archive_dir() / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backups / f"{name}.{datetime.now():%Y%m%d-%H%M%S}.md")
    tmp = path.with_name("SKILL.md.vira-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return read(name, archived)


def archive(name):
    folder = _folder(name)
    dest = archive_dir() / name
    if dest.exists():
        raise ValueError(f"the archive already holds a task named {name}")
    archive_dir().mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(dest))
    return read(name, archived=True)


def restore(name):
    folder = _folder(name, archived=True)
    dest = tasks_dir() / name
    if dest.exists():
        raise ValueError(f"a task named {name} is already in place")
    tasks_dir().mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(dest))
    return read(name)

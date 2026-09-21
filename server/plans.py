"""Saved plans — the durable vault home + registry for Plan-mode output.

A Plan-mode idea or action produces plan markdown. On the owner's own
machine Vira ALSO publishes it to a hosted lab page via a private
~/.claude/scripts/plan-html-deploy.py hook; on every other install that
hook is absent and the publish silently no-ops. This module is the
universal home every install shares: each plan is saved as a markdown
note in the vault — creating a Vira vault at ~/.vira/vault when none is
connected, so a first plan is what STARTS the vault — and recorded in a
small registry so it keeps a stable id + name, opens in an in-app viewer,
and stays reachable long after the job terminal is gone.

Registry entry shape (data/plans.json):
  { "id": "pl_<hex>", "title": str, "path": <absolute .md path>,
    "created": ISO8601, "idea_id": str|None, "job_id": str|None,
    "lab_url": str }   # lab_url set only where the private hook published

The registry is REGENERABLE in shape (the plan files under <vault>/plans
are the real content) but canonical in role, so writes are atomic
(tmp+rename) and serialized through the cross-process filelock — a
detached job runner calls save_plan from its own process.
"""
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import settings, vault, vaultwrite
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
REG_PATH = ROOT / "data" / "plans.json"
PLANS_SUBDIR = "plans"                       # <vault>/plans/<file>.md
DEFAULT_VAULT = Path.home() / ".vira" / "vault"

_lock = threading.Lock()

# ---------- what a plan IS ----------
#
# The one description of the plan format, because a plan is a SHAPE, not a
# permission mode. Three readers depend on this exact structure and none of
# them cares how the session that wrote it was gated:
#
#   * _extract_title below, and plans.save_plan's vault filename, read the
#     `# ` title on line 1;
#   * the in-app viewer renders the markdown;
#   * the private ~/.claude/scripts/plan-html-deploy.py renderer builds the
#     hosted dossier FROM this structure — the `# ` title becomes the hero
#     headline, `## Executive Summary` becomes the dek and the verdict
#     callout, every other `##` becomes a numbered section, ```mermaid
#     fences become dark-theme diagrams, and stated figures become the
#     count-up KPI band. Prose with no headings renders as a wall.
#
# So the instruction is what earns the dossier. It is stated once here and
# every surface that asks for a plan appends it: the Forge's Plan-dossier
# output (server/circuits.py _forge_brief) and the Queue's Plan button
# (ideaPlanPrompt in static/app.js — a JS copy, held to this text by
# tests/test_plan_shape.py, since a prompt must not depend on a round trip).
SHAPE = "\n".join([
    "Output ONLY the plan as markdown — no preamble, no closing remarks, no",
    "code fence around the whole thing. Vira saves it to the vault as an",
    "editable note. Rendering a hosted dossier is a separate opt-in. Follow this plan",
    "format exactly:",
    '- First line: "# Title" (a short noun phrase, max ~8 words).',
    '- Then "## Executive Summary" (2-3 sentences: what is built, the',
    "  approach, the key tradeoff or risk).",
    "- Then the full plan as markdown sections: context, architecture,",
    "  step-by-step changes, files touched, risks, open questions.",
    "- Use Mermaid code fences for any diagrams. No emojis.",
    "- Only state figures the work actually supports — the renderer counts",
    "  them up on the page, so an invented number becomes a headline.",
])


def _extract_title(md):
    """The plan's title: the first `# ` heading (the plan format mandates one
    on line 1), else the first non-empty line, else a fallback."""
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:120] or "Untitled plan"
    for line in (md or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:120]
    return "Untitled plan"


def _slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:60] or "plan"


def _load():
    """Fresh read every time — a detached runner and the server both touch
    this store, so an in-memory cache would clobber the other's writes."""
    try:
        s = json.loads(REG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # a registry an install already wrote in cp1252 (pre-42693d5 on
        # Windows) must degrade like any unreadable file, not raise
        s = {"plans": []}
    if not isinstance(s, dict) or "plans" not in s:
        s = {"plans": []}
    return s


def _save(s):
    REG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REG_PATH.with_name(REG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(REG_PATH)


def ensure_vault() -> Path:
    """The vault that plans live in. If the owner connected one, use it; else
    create a Vira vault at ~/.vira/vault (qocha-initialized) and connect it —
    a plan is what starts the vault. Falls back to a bare directory when the
    qocha CLI is unavailable (a vault is, at bottom, a folder of markdown)."""
    raw = str(settings.get("vault_root") or "").strip()
    if raw:
        root = Path(raw).expanduser()
        if not root.is_dir():
            raise ValueError("the configured vault is disconnected")
        return root
    from . import onboard
    try:
        onboard.vault_setup(str(DEFAULT_VAULT), init=True)
    except Exception:  # noqa: BLE001 — qocha missing / init failed
        DEFAULT_VAULT.mkdir(parents=True, exist_ok=True)
        try:
            onboard.config_set(vault_root=str(DEFAULT_VAULT))
        except Exception:  # noqa: BLE001 — best-effort connect
            pass
    return DEFAULT_VAULT


def destination_spec(destination=None, context=None):
    """Resolve once before work starts; only a first-ever plan creates a vault.

    An explicit unavailable destination, or a disconnected configured vault,
    never creates or redirects anything. Subsequent saves use the stable id.
    """
    if (not destination and not context
            and not settings.get("vault_root")
            and not settings.get("vault_sources")
            and not settings.get("vault_dirs")):
        ensure_vault()
    return vaultwrite.resolve_destination(destination, context, operation="plan")


def save_plan(md, idea_id=None, job_id=None, lab_url=None, destination=None,
              context=None):
    """Save a plan in its destination's allowed capture area and register it.

    Legacy primary vaults keep plans/. Explicit policies use capture_dir/plans
    so connecting an arbitrary folder does not impose the primary layout.
    The receipt preserves the source id even when two vaults use the same name.
    """
    md = (md or "").strip()
    if not md:
        raise ValueError("empty plan")
    spec = destination_spec(destination, context)
    if spec.get("primary") and not spec.get("policy_explicit"):
        folder = PLANS_SUBDIR
    else:
        folder = str(Path(spec["capture_dir"]) / PLANS_SUBDIR)
    title = _extract_title(md)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H%M")
    slug = _slugify(title)
    # The registry lock covers name allocation AND registration. The shared
    # write seam also locks the destination and atomically creates the file.
    with _lock, locked(REG_PATH):
        n = 1
        while True:
            suffix = "" if n == 1 else f"-{n}"
            relative = (Path(folder) / f"{stamp}-{slug}{suffix}.md").as_posix()
            try:
                receipt = vaultwrite.write_note(spec, relative, md + "\n")
                break
            except FileExistsError:
                n += 1
        entry = {
            "id": "pl_" + uuid.uuid4().hex[:10],
            "title": title,
            "path": receipt["absolute_path"],
            "source_id": receipt["source_id"],
            "source_name": receipt["source_name"],
            "relative_path": receipt["relative_path"],
            "citation": receipt["path"],
            "sha256": receipt["sha256"],
            "created": now.isoformat(timespec="seconds"),
            "idea_id": idea_id,
            "job_id": job_id,
            "lab_url": ((lab_url or "").strip() if spec.get("allow_publish")
                        else ""),
        }
        s = _load()
        s["plans"].insert(0, entry)
        _save(s)
    # A saved plan is something to read, so it joins the Reader's queue. Done
    # outside the lock above and best-effort on purpose: this runs inside the
    # detached job runner, and a queue that cannot be written is never a reason
    # to lose the plan itself. The backfill sweep picks up anything missed.
    try:
        from . import readinglist
        readinglist.register(title, "plan", entry["id"], "plan",
                             ref=entry["id"], source="plan-mode",
                             created=entry["created"])
    except Exception:  # noqa: BLE001 — the plan is saved; the pointer is a nicety
        pass
    return entry


def _entry_target(entry, operation="read"):
    """Resolve current policy, never trust an absolute path from the registry."""
    source_id = entry.get("source_id")
    relative = entry.get("relative_path")
    specs = vault.source_specs()
    if source_id:
        spec = next((s for s in specs if s["id"] == source_id), None)
        if spec is None or not relative:
            raise ValueError("the plan's vault is disconnected")
    else:
        # Pre-multivault registry rows carry only an absolute filename. They
        # remain readable only inside a currently connected source.
        saved = Path(entry.get("path") or "").expanduser().resolve()
        matches = []
        for candidate in specs:
            try:
                rel = saved.relative_to(Path(candidate["root"]).resolve())
            except ValueError:
                continue
            matches.append((candidate, rel.as_posix()))
        if len(matches) != 1:
            raise ValueError("the plan's vault is disconnected")
        spec, relative = matches[0]
    if operation != "read":
        spec = vaultwrite.resolve_destination(spec["id"], operation=operation)
    return vaultwrite.safe_path(spec, relative, operation=operation)


def set_publication(pid, url):
    """Attach an opt-in hook result only after the plan has been saved."""
    with _lock, locked(REG_PATH):
        s = _load()
        entry = next((p for p in s["plans"] if p["id"] == pid), None)
        if entry is None:
            raise KeyError(pid)
        spec = vaultwrite.resolve_destination(entry["source_id"], operation="plan")
        if not spec.get("allow_publish"):
            raise ValueError("this vault does not allow publication")
        entry["lab_url"] = (url or "").strip()
        _save(s)


def list_plans():
    """Saved plans with current availability, including disconnected sources."""
    out = []
    for p in _load()["plans"]:
        try:
            missing = not _entry_target(p).is_file()
            error = ""
        except (OSError, ValueError) as exc:
            missing, error = True, str(exc)
        out.append({**p, "missing": missing, "unavailable": error})
    return out


def get_plan(pid):
    """One plan and its body, read only through its current source policy."""
    for p in _load()["plans"]:
        if p["id"] == pid:
            error = ""
            try:
                md = _entry_target(p).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                md, error = "", str(exc)
            return {**p, "markdown": md, "missing": not md,
                    "unavailable": error}
    raise KeyError(pid)


def delete_plan(pid):
    """Remove a plan only while its connected source still permits that write."""
    with _lock, locked(REG_PATH):
        s = _load()
        gone = next((p for p in s["plans"] if p["id"] == pid), None)
        if gone is None:
            raise KeyError(pid)
        path = _entry_target(gone, operation="delete")
        # Legacy rows acquire their source id from the same confined path
        # resolution used for reading; no untrusted absolute path is unlinked.
        spec = next(s for s in vault.source_specs()
                    if path.is_relative_to(Path(s["root"]).resolve()))
        relative = path.relative_to(Path(spec["root"]).resolve()).as_posix()
        vaultwrite.delete_text(spec, relative)
        s["plans"] = [p for p in s["plans"] if p["id"] != pid]
        _save(s)
    return {"removed": pid}

"""The Reader's queue: everything worth reading, wherever it actually lives.

Vira generates a lot of documents — audit dossiers, plan-mode output, session
retros, morning briefs — and each one lands wherever its producing process puts
it. Before this they were scattered across as many surfaces as producers, and
nothing tied them together.

This module is the other half of the Reader: a registry of SOFT POINTERS. It
never copies a document and never becomes its home. An entry holds a title, a
kind, and a locator; resolving it is somebody else's job (a served URL for the
dossiers under static/, the vault for anything in the notes tree). If the
source file moves or is deleted, the entry reports `missing` and the queue
carries on — the registry is a view, not a store of record.

THERE IS NO READ STATE. Every registered document stays in the library for
good (owner's call, 2026-11-04: having read something does not mean you do not
want to read it again). The old `completed` stamp, the 14-day auto-filing of
older documents as read, and per-section progress marks were all removed; an
entry written before that may still carry a `completed` key in the store, and
every view drops it (see _decorate).

Same cross-process discipline as every other small JSON store: fresh read per
op, locked mutation, atomic write (server/jsonstore.py).
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, reading, settings

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "reading-list.json"

# Every directory the sweep READS, named here rather than reached for inline —
# a source that resolves its own path is a source a test cannot point
# somewhere else. See _sources().
EXPLAINER_DIR = ROOT / "static" / "explainer"
DOCS_DIR = ROOT / "static" / "docs"
# The session walkthroughs, served in place off lab_root (see walkthroughs.py).
# None means "ask walkthroughs.root()", which reads settings; a test roots it at
# its fixture like every other source here. Declared in THIS module for exactly
# the reason the docstring on _sources() gives.
WALKTHROUGH_DIR = None

# Folders OUTSIDE the checkout that the owner has connected to the Reader.
# Every other source below is a place Vira itself writes; this is the seam for
# documents produced somewhere else entirely — another project's output, a
# record repo, a scratch folder. It is config rather than a constant because
# the whole point is that it differs per install. Settings key `reader_sources`,
# a list of
#   {"path": "~/somewhere", "kind": "dossier", "glob": "*.html", "label": "..."}
# A connected folder is READ. Nothing is copied out of it and nothing is written
# back into it, which is the same soft-pointer contract as every other source
# and the reason connecting a folder is a safe thing to do to a live directory.
READER_SOURCE_KINDS = ("dossier", "plan", "retro", "brief", "walkthrough")
DEFAULT_SOURCE_GLOB = "*.html"
# A connected folder is somebody's working directory, not a curated shelf. The
# cap is what stops pointing at the wrong one from filling the queue with a
# thousand rows before the owner notices.
MAX_SOURCE_FILES = 500

# What a locator means, and therefore who resolves it.
#   url   -> a path this server already serves (the dossiers under static/)
#   vault -> a path inside the notes vault, rendered through /api/vault/note
#   plan  -> a plans.py registry id, rendered through /api/plans/{id}
#   file  -> an absolute path inside a connected folder, served by
#            /api/reading/file/{id} and re-checked for containment on every read
LOCATOR_KINDS = ("url", "vault", "plan", "file")

# The kinds of thing that can sit in the queue. `room` is legacy: reading
# rooms were briefly flattened into the queue (2026-07-27, first cut of this
# module) and the owner overruled it the same week — a room is a SHELF, a
# card beside My Documents, not a row inside it. Rows already registered with
# the kind stay valid (register() must not start refusing an old store), but
# the queue views exclude them and the backfill no longer sweeps them in.
KINDS = ("dossier", "plan", "retro", "brief", "room", "walkthrough")

MAX_TEXT = 300

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# Vault subdirectories that carry readable documents, and the kind each yields.
# Empty vault_root leaves all of this dormant, which is the correct state on an
# install with no notes tree.
VAULT_SOURCES = (("Sessions", "retro"), ("Briefs", "brief"))

_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean(s, limit=MAX_TEXT):
    return " ".join(str(s or "").split())[:limit]


def slugify(text, fallback="doc"):
    """A reading.NAME_RE-legal slug: the key _dedupe collapses twin entries by."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]
    s = re.sub(r"^[^a-z0-9]+", "", s)
    return s or fallback


def _blank():
    return {"items": []}


def _load():
    s = jsonstore.read(STORE, _blank())
    if not isinstance(s, dict):
        s = _blank()
    s.setdefault("items", [])
    if not isinstance(s["items"], list):
        s["items"] = []
    return s


# ---------------------------------------------------------------- registry ---

def register(title, kind, locator, locator_kind="url", *,
             ref=None, source="", created=None):
    """Add a document to the queue, or return the entry that already covers it.

    Idempotent on (locator_kind, locator): a producer may call this on every
    run, and the backfill sweep re-walks the same directories, so re-registering
    must never duplicate a row."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if locator_kind not in LOCATOR_KINDS:
        raise ValueError(f"unknown locator kind: {locator_kind}")
    locator = _clean(locator, 600)
    if not locator:
        raise ValueError("locator required")
    title = _clean(title) or locator

    def fn(s):
        for it in s["items"]:
            if it.get("locator") == locator and it.get("locator_kind") == locator_kind:
                # Refresh the title (a regenerated dossier may be renamed).
                it["title"] = title
                return s
        s["items"].append({
            "id": "rl_" + uuid.uuid4().hex[:10],
            "title": title,
            "kind": kind,
            "locator": locator,
            "locator_kind": locator_kind,
            "ref": _clean(ref, 64) or None,
            "source": _clean(source, 64),
            "created": _clean(created, 40) or _now(),
            "added": _now(),
            "slug": slugify(title, fallback=kind),
        })
        # Pointers are durable history: no view limit evicts an old entry.
        return s

    jsonstore.mutate(STORE, fn, _blank())
    return find_by_locator(locator, locator_kind)


def find_by_locator(locator, locator_kind):
    for it in _load()["items"]:
        if it.get("locator") == locator and it.get("locator_kind") == locator_kind:
            return it
    return None


def get(item_id):
    for it in _load()["items"]:
        if it.get("id") == item_id:
            return it
    return None


def forget(item_id):
    """Drop the pointer entirely. The document itself is never touched."""
    def fn(s):
        before = len(s["items"])
        s["items"] = [i for i in s["items"] if i.get("id") != item_id]
        if len(s["items"]) == before:
            raise KeyError(item_id)
        return s
    jsonstore.mutate(STORE, fn, _blank())
    return True


# ---------------------------------------------------------------- resolving --

def _vault_file(rel):
    root = settings.get("vault_root")
    if not root:
        return None
    p = (Path(root).expanduser() / rel)
    try:
        p = p.resolve()
        base = Path(root).expanduser().resolve()
        p.relative_to(base)            # containment, the media.py pattern
    except (OSError, ValueError):
        return None
    return p


def source_path(it):
    """The file behind an entry, when there is one we can stat. None means the
    locator is resolved by another module (a plan id) or is not a local file."""
    lk = it.get("locator_kind")
    if lk == "url":
        rel = it["locator"].lstrip("/")
        if rel.startswith("walkthroughs/"):
            # Films are served IN PLACE off lab_root (walkthroughs.py), never
            # from static/ — resolving them there read all 32 films as
            # missing, so every click refused with "no longer where it was
            # saved" while the row's own thumbnail rendered fine (found live
            # 2026-08-05, the day after the library shipped).
            from . import walkthroughs
            wroot = walkthroughs.root(WALKTHROUGH_DIR)
            if not wroot:
                return None
            p = wroot / rel[len("walkthroughs/"):]
            try:
                p = p.resolve()
                p.relative_to(wroot.resolve())  # containment, as _vault_file
            except (OSError, ValueError):
                return None
            return p / "index.html" if p.is_dir() else p
        p = ROOT / "static" / rel
        return p / "index.html" if p.is_dir() else p
    if lk == "vault":
        return _vault_file(it["locator"])
    if lk == "file":
        return _connected_file(it["locator"])
    return None


def _connected_file(locator):
    """Resolve a connected-folder locator, refusing anything no longer inside a
    configured root.

    Containment is checked at READ time rather than trusted from registration.
    That is deliberate: removing a folder from `reader_sources` has to make its
    rows report missing immediately, rather than leaving the server willing to
    serve files out of a directory the owner just disconnected. Same discipline
    as _vault_file, for the same reason."""
    try:
        p = Path(locator).expanduser().resolve()
    except OSError:
        return None
    for spec in _reader_specs():
        try:
            p.relative_to(spec["root"])
        except ValueError:
            continue
        return p
    return None


def _missing(it):
    if it.get("locator_kind") == "plan":
        return False               # plans.py owns that answer, via its own list
    p = source_path(it)
    return not (p and p.is_file())


# ------------------------------------------------------------------- views ---

def _decorate(it):
    out = dict(it)
    out.pop("completed", None)      # a pre-2026-11-04 read stamp, now meaningless
    out["missing"] = _missing(it)
    return out


def _dedupe(items):
    """Collapse entries that are the same document reached two ways.

    A plan is registered TWICE: once by `plans.save_plan` as `plan:pl_...`, and
    again by the sitedocs sweep as `url:/docs/plans/....html`, because
    register() is idempotent per (locator_kind, locator) and those are two
    different keys. Measured here: 11 of 551. It went unnoticed while the list
    was a flat date-ordered column and became obvious the moment documents were
    grouped by feature — one build reading as two.

    The key is the SLUG: both registrations derive it from the same title, so
    two entries sharing one are one document. The `plan` locator wins because plans.py resolves it (it serves
    the markdown and knows whether the file is still there); a `url` row for a
    deleted plan would render as present."""
    rank = {"plan": 0, "vault": 1, "url": 2}
    best = {}
    for it in items:
        key = it.get("slug") or it.get("id")
        cur = best.get(key)
        if cur is None or rank.get(it.get("locator_kind"), 9) < \
                rank.get(cur.get("locator_kind"), 9):
            best[key] = it
    return _keep(items, best)


def _keep(items, best):
    # Preserve the caller's ordering rather than dict order, so the sort a
    # caller applied before de-duplicating still means something.
    keep = {id(v) for v in best.values()}
    return [it for it in items if id(it) in keep]


def _newest_key(it):
    """Document date first, then the moment it first landed in the Reader.

    Producers encode dates at different precision: some know an exact time,
    others only YYYY-MM-DD. Comparing those raw strings lets the formatting
    decide their same-day order. The calendar day is the shared first rung;
    within it, the later Reader arrival wins.
    """
    created = str(it.get("created") or "")
    added = str(it.get("added") or "")
    return ((created or added)[:10], added or created, created)


def library():
    """Every registered document, newest first, deduped. Rooms are excluded:
    a room is its own tab in the Reader, not a row in Vira's Documents."""
    items = [i for i in _load()["items"] if i.get("kind") != "room"]
    items.sort(key=_newest_key, reverse=True)
    return [_decorate(i) for i in _dedupe(items)]


def counts():
    items = [i for i in _load()["items"] if i.get("kind") != "room"]
    return {"total": len(items)}


# ---------------------------------------------------------------- backfill ---

def _explainer_dossiers():
    """Generated HTML under static/explainer/ — already served, so url locators.

    The four hand-authored atlas pages at the top level are the system
    explainer, not reading; only subdirectories (one dossier each) are taken."""
    found = []
    try:
        dirs = sorted(d for d in EXPLAINER_DIR.iterdir() if d.is_dir())
    except OSError:
        return found
    for d in dirs:
        index = d / "index.html"
        if not index.is_file():
            continue
        try:
            head = index.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        m = _TITLE_RE.search(head)
        title = _clean(_TAG_RE.sub(" ", m.group(1))) if m else d.name
        found.append({"title": title, "kind": "dossier",
                      "locator": f"/explainer/{d.name}/", "locator_kind": "url",
                      "created": _file_created(index)})
    return found


def _file_created(p, name=""):
    """When a document was made. The filename's date prefix is the truth where
    there is one — the retros and briefs are named `YYYY-MM-DD ...` and their
    mtime is whenever the file was last rsynced, which is not the same thing."""
    m = _DATE_PREFIX_RE.match(name or (p.name if p else ""))
    if m:
        return m.group(1) + "T00:00:00"
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).astimezone() \
            .isoformat(timespec="seconds")
    except OSError:
        return _now()


def _vault_documents():
    root = settings.get("vault_root")
    if not root:
        return []
    base = Path(root).expanduser()
    found = []
    for sub, kind in VAULT_SOURCES:
        d = base / sub
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            continue
        for f in files:
            found.append({"title": f.stem, "kind": kind,
                          "locator": f"{sub}/{f.name}", "locator_kind": "vault",
                          "created": _file_created(f, f.stem)})
    return found


def _rooms():
    """RETIRED from the backfill (2026-07-27, owner's call): rooms are cards
    in the Reader's home, not queue rows. Kept as a named seam because a
    future 'register a room's page itself' need would land here."""
    out = []
    for p in reading.list_pages():
        out.append({"title": p["title"], "kind": "room",
                    "locator": p["url"], "locator_kind": "url"})
    return out


def _plans():
    from . import plans                       # lazy: plans imports settings too
    out = []
    try:
        rows = plans.list_plans()
    except Exception:                         # noqa: BLE001 — a dormant vault
        return out                            # is a state, not a failure
    for p in rows:
        out.append({"title": p.get("title") or "Untitled plan", "kind": "plan",
                    "locator": p["id"], "locator_kind": "plan",
                    "ref": p["id"], "created": p.get("created")})
    return out


def _sitedocs():
    """Documents migrated off thedurham.nyc plus every plan the repointed
    plan-mode hook has rendered since — static/docs/, served at /docs/.
    sitedocs owns the registry/manifest read; lazy import breaks the cycle
    (sitedocs imports this module for producer registration).

    DOCS_DIR is passed EXPLICITLY. registry_rows() falls back to sitedocs's own
    module-level path when it is not, and that fallback is invisible from here
    — which is exactly how this sweep spent a fortnight reading the owner's
    real documents out from under every test that thought it had isolated it."""
    try:
        from . import sitedocs
        return sitedocs.registry_rows(DOCS_DIR)
    except Exception:
        return []


def _walkthroughs():
    """The build films off lab_root, served in place at /walkthroughs/.
    Dormant (empty) when no lab_root is configured, which is every install but
    the owner's."""
    try:
        from . import walkthroughs
        return walkthroughs.rows(WALKTHROUGH_DIR)
    except Exception:
        return []


def _reader_specs():
    """The connected folders, normalized, or [] when none are configured.

    A malformed row is SKIPPED rather than raised on. This is owner-edited
    config read on every sweep and on every file resolution, so one bad entry
    must not be able to take the queue down with it. A folder that does not
    exist yet is dormant for the same reason `vault_root` is: absent is a
    state, not a failure."""
    raw = settings.get("reader_sources")
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        try:
            root = Path(path).expanduser().resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue
        kind = str(row.get("kind") or "dossier")
        if kind not in READER_SOURCE_KINDS:
            kind = "dossier"
        pattern = str(row.get("glob") or DEFAULT_SOURCE_GLOB)
        # A glob here names files inside the folder. Letting it carry separators
        # or a parent hop would turn one connected folder into a reach anywhere
        # on disk, which is the one thing the containment check exists to stop.
        if "/" in pattern or "\\" in pattern or ".." in pattern:
            pattern = DEFAULT_SOURCE_GLOB
        out.append({"root": root, "kind": kind, "glob": pattern,
                    "label": _clean(row.get("label") or root.name, 80)})
    return out


def _document_title(p):
    """A document's own title where it has one, its filename otherwise.

    A bundle takes its DIRECTORY name when its index carries no title, because
    "index" is not the name of anything."""
    fallback = p.parent.name if p.name.lower() == "index.html" else p.stem
    if p.suffix.lower() not in (".html", ".htm"):
        return _clean(fallback)
    try:
        head = p.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return _clean(fallback)
    m = _TITLE_RE.search(head)
    return _clean(_TAG_RE.sub(" ", m.group(1))) if m else _clean(fallback)


def _connected_documents():
    """Documents inside the folders the owner connected.

    Two shapes are taken, so connecting a folder works whether it holds files or
    bundles: a file matching the folder's glob is one document, and a
    SUBDIRECTORY holding index.html is one document too. The second is the same
    bundle shape static/explainer/ uses, so a generated page with its assets
    beside it connects without being flattened or copied."""
    found = []
    for spec in _reader_specs():
        root = spec["root"]
        try:
            files = sorted(p for p in root.glob(spec["glob"]) if p.is_file())
            for d in sorted(p for p in root.iterdir() if p.is_dir()):
                index = d / "index.html"
                if index.is_file():
                    files.append(index)
        except OSError:
            continue
        for p in files[:MAX_SOURCE_FILES]:
            found.append({"title": _document_title(p), "kind": spec["kind"],
                          "locator": p.as_posix(), "locator_kind": "file",
                          "created": _file_created(p, p.stem)})
    return found


def _sources():
    """Every producer the sweep reads, declared in one place.

    A source listed here must take its root from THIS module (DOCS_DIR,
    EXPLAINER_DIR) or from settings — never from a path its own module resolved
    at import time. Otherwise a test that patches everything it knows about
    still sweeps the live checkout, and the failure looks like a bad assertion
    rather than a missing seam. tests/test_readinglist.py roots all of these at
    one fixture directory and keeps a guard test that an empty root finds
    nothing; a new source that skips the seam fails that test on sight."""
    return (_explainer_dossiers() + _plans() + _vault_documents()
            + _sitedocs() + _walkthroughs() + _connected_documents())


def backfill(source="backfill"):
    """Register everything already on disk. Safe to re-run: register() is
    idempotent on the locator."""
    added = 0
    for row in _sources():
        before = find_by_locator(row["locator"], row["locator_kind"])
        try:
            register(row["title"], row["kind"], row["locator"],
                     row["locator_kind"], ref=row.get("ref"),
                     source=source, created=row.get("created"))
        except ValueError:
            continue
        if not before:
            added += 1
    return {"added": added, **counts()}

"""Skins — genre-compiled jumping-off points the Design Studio can wear.

A skin is a complete ``:root`` token set (the colour soul — editable
afterward by the Design Studio's token editor, which reads that same block)
plus an optional glass layer (geometry, type, the tube — things tokens
cannot carry). Applying a skin is a LOCAL, no-git action: it records the
skin's tokens and glass in this install's ``data/active-skin.json`` and the
app reloads wearing it. Nothing is committed or pushed — a skin is a personal
look on this machine, not a change to the shared repo. (This is why it works
the same on every download: a downloaded copy points at a public repo it
can't push to.) If the owner wants a skin to become the shipped default, that
is a deliberate git commit, not a side effect of a click.

The tracked ``static/style.css`` and ``static/skin-active.css`` are never
written by an apply. The server answers ``/style.css`` with the tracked sheet
whose ``:root`` values are rewritten in memory (values only, comments
preserved — the same conservative line rewrite the studio uses) and
``/skin-active.css`` with the skin's glass, so the browser receives exactly
the bytes the old in-place rewrite left on disk. Until 2026-10-11 the apply
wrote both tracked files, which left the live checkout dirty: every
``branch.sh merge`` refused, and the in-app updater would not fast-forward,
for as long as a skin was applied. ``migrate_legacy`` moves a skin applied
that way into the store on the first boot and restores the tracked files.

Skins ship as tracked data under ``static/skins/<id>.json`` (+ optional
``<id>.css``), so a new skin is added by dropping files in. Dark Mode is
the BASE: the shipped ``:root`` carries exactly its values, so applying it
clears the stored look and Vira serves the tracked files as committed. A
partial skin (Light Mode carries 64 of the 69 tokens) inherits the rest from
the shipped sheet, and every apply starts from that sheet, so a previously
applied skin can never bleed through. It is the one skin that cannot be
removed - ``apply_skin`` loads it unconditionally. (This was called the FLOOR
until 2026-08-28; the word read as a minimum value, which it never was, and
cost a real misdiagnosis.)

The picker lives at the top of the Design Studio module: pick a skin up top,
tweak it in the studio below.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from . import designstudio, jsonstore

router = APIRouter(prefix="/api/skins")
# /style.css and /skin-active.css, served with the applied skin laid over the
# tracked files. Included ahead of the static mount, which would otherwise
# answer both from disk.
assets = APIRouter()
log = logging.getLogger(__name__)

_SWATCH_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^(?:rgb|rgba|hsl|hsla)\([0-9 ,.%/]+\)$")

APP_ROOT = Path(__file__).resolve().parents[1]
SKINS_DIR = APP_ROOT / "static" / "skins"
STYLE_CSS = APP_ROOT / "static" / "style.css"           # tracked; read, never applied to
SKIN_ACTIVE = APP_ROOT / "static" / "skin-active.css"   # tracked; read, never applied to
# The applied skin: {"id", "tokens", "glass"}. Per-instance (data/ is ignored).
# A record with an id but no "tokens" was written before 2026-10-11, when the
# look itself lived in the tracked files; migrate_legacy upgrades it.
STATE = APP_ROOT / "data" / "active-skin.json"

BASE_ID = "darkmode"                             # the base every apply resets to
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EXTRAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\.css$")
MAX_MANIFEST_BYTES = 128 * 1024
MAX_EXTRAS_BYTES = designstudio.MAX_FILE_BYTES   # 512 KB, same ceiling as a saved stylesheet

# The shipped (glass-free) content of skin-active.css. migrate_legacy writes
# exactly this back over a glass layer the old apply left in the tracked file,
# so the restore leaves no git diff. Kept in sync with the shipped
# static/skin-active.css (ShippedStateInvariant). Comments only, no rules.
_ACTIVE_HEADER = (
    "/* skin-active.css — the glass layer of the currently applied skin.\n"
    "   Linked after style.css. This tracked file is the default, and\n"
    "   applying a skin never rewrites it: the server answers\n"
    "   /skin-active.css with that skin's glass from this install's data/\n"
    "   instead. The default skin (Dark Mode) carries no glass, so this file\n"
    "   has comments only, no rules. */\n"
)


# ---------------------------------------------------------------- manifests

def _manifest_path(skin_id: str) -> Path:
    if not ID_RE.match(skin_id or ""):
        raise HTTPException(400, "bad skin id")
    p = (SKINS_DIR / f"{skin_id}.json").resolve()
    # ID_RE already forbids separators, but resolve-and-contain is the belt.
    if SKINS_DIR.resolve() not in p.parents:
        raise HTTPException(400, "bad skin id")
    return p


def _check_token(key: str, value) -> None:
    name = key[2:] if key.startswith("--") else key
    if not designstudio.TOKEN_RE.match(name):
        raise ValueError(f"bad token name: {key!r}")
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError(f"bad value for {key}")
    if designstudio.VALUE_BAD.search(value):
        raise ValueError(f"illegal characters in value for {key}")
    # a value carrying a CSS comment sequence would comment out the following
    # :root declarations when written in place — reject it (this also hardens
    # the shared value brake the studio save path relies on).
    if "/*" in value or "*/" in value:
        raise ValueError(f"comment sequence in value for {key}")


def load_manifest(skin_id: str) -> dict:
    """Load + validate a skin manifest. Manifests are trusted (tracked) data;
    the validation catches authoring errors and is defence in depth for the
    values that reach the stylesheet."""
    p = _manifest_path(skin_id)
    if not p.is_file():
        raise HTTPException(404, f"no such skin: {skin_id}")
    if p.stat().st_size > MAX_MANIFEST_BYTES:
        raise HTTPException(500, f"skin manifest too large: {skin_id}")
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise HTTPException(500, f"unreadable skin manifest: {e}")
    if not isinstance(m, dict) or m.get("id") != skin_id:
        raise HTTPException(500, f"skin manifest id mismatch: {skin_id}")
    tokens = m.get("tokens") or {}
    if not isinstance(tokens, dict) or not tokens:
        raise HTTPException(500, f"skin {skin_id} has no tokens")
    try:
        for k, v in tokens.items():
            _check_token(k, v)
    except ValueError as e:
        raise HTTPException(500, str(e))
    extras = m.get("extras")
    if extras is not None and not EXTRAS_RE.match(str(extras)):
        raise HTTPException(500, f"skin {skin_id} has a bad extras name")
    background = m.get("background")
    if background is not None:
        if (not isinstance(background, dict)
                or set(background) != {"scene", "pond", "look", "dim", "paused"}
                or background["scene"] not in ("koi", "redwoods", "aurora", "constellation", "none")
                or background["pond"] not in ("garden", "courtyard")
                or background["look"] not in ("natural", "wireframe")
                or type(background["paused"]) is not bool
                or type(background["dim"]) not in (int, float)
                or not math.isfinite(background["dim"])
                or not 0 <= background["dim"] <= .65):
            raise HTTPException(500, f"skin {skin_id} has a bad background preset")
    return m


def _meta(m: dict) -> dict:
    """The listing shape — everything a card needs, minus the full token map."""
    return {
        "id": m["id"],
        "name": m.get("name", m["id"]),
        "genre": m.get("genre"),
        "register": m.get("register"),
        "medium": m.get("medium"),
        "tagline": m.get("tagline"),
        "description": m.get("description"),
        "swatch": [c for c in (m.get("swatch") or [])
                   if isinstance(c, str) and _SWATCH_RE.match(c)][:8],
        "source": m.get("source"),
        "covenants": m.get("covenants") or [],
        "default": bool(m.get("default")),
        "has_glass": bool(m.get("extras")),
        "background": m.get("background"),
    }


def _state() -> dict:
    s = jsonstore.read(STATE, {})
    return s if isinstance(s, dict) else {}


def _live_id(s: dict) -> str:
    got = s.get("id")
    if isinstance(got, str) and ID_RE.match(got) and _manifest_path(got).is_file():
        return got
    return BASE_ID


def active_id() -> str:
    """The applied skin, or the base. A stored id whose manifest is gone
    reads as the base: skins are renamed and retired (this store carried
    "taurid" before Dark Mode took its own name), and an id that survives
    its manifest would mark no card active and 404 every caller that loads
    it. A missing manifest is the honest signal that skin no longer exists."""
    return _live_id(_state())


def overlay() -> tuple[dict, str | None]:
    """(tokens, glass) the served stylesheets lay over the tracked files.

    Empty for the base, and for a stored id whose manifest is gone, so the
    look always agrees with the card the picker marks active. The store is a
    hand-editable file under data/ whose values reach CSS the browser runs,
    so each token is held to the same brake a manifest is; one that fails is
    skipped and named in the log, never served."""
    s = _state()
    if _live_id(s) == BASE_ID:
        return {}, None
    tokens = {}
    raw = s.get("tokens")
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        try:
            _check_token(k, v)
        except ValueError as e:
            log.warning("skins: not serving stored token %s: %s", k, e)
            continue
        tokens[k] = v
    glass = s.get("glass")
    if isinstance(glass, str) and len(glass.encode("utf-8")) > MAX_EXTRAS_BYTES:
        log.warning("skins: not serving stored glass over %d bytes", MAX_EXTRAS_BYTES)
        glass = None
    return tokens, (glass if isinstance(glass, str) else None)


def list_skins() -> dict:
    """Every skin on disk, base/default first, then the rest by name."""
    metas = []
    for p in sorted(SKINS_DIR.glob("*.json")):
        skin_id = p.stem
        if not ID_RE.match(skin_id):
            continue
        try:
            metas.append(_meta(load_manifest(skin_id)))
        except HTTPException:
            continue                              # skip a malformed file, list the rest
    metas.sort(key=lambda m: (not m["default"], m["name"].lower()))
    return {"active": active_id(), "skins": metas}


# ---------------------------------------------------------------- apply

def _rewrite_root(text: str, tokens: dict) -> str:
    """Replace the value of each named token inside the plain ``:root`` block,
    preserving the trailing ``;`` and any brand-book comment on the line —
    the same conservative rewrite the Design Studio's save path uses."""
    lines = text.split("\n")
    open_i, close_i = designstudio._root_block(lines)
    line_re = re.compile(r"^(\s*--)([a-z0-9-]+)(\s*:\s*)([^;]+)(;.*)$")
    for i in range(open_i, close_i + 1):
        m = line_re.match(lines[i])
        if not m:
            continue
        key = "--" + m.group(2)
        if key in tokens:
            lines[i] = m.group(1) + m.group(2) + m.group(3) + tokens[key].strip() + m.group(5)
    return "\n".join(lines)


def read_root_tokens(text: str) -> dict:
    """Every ``--token: value`` in the plain ``:root`` block. Same block and
    same line shape ``_rewrite_root`` writes, so the reader and the writer
    cannot disagree about which declarations are tokens."""
    lines = text.split("\n")
    try:
        open_i, close_i = designstudio._root_block(lines)
    except ValueError:
        return {}                                 # no :root block: nothing to read
    line_re = re.compile(r"^(\s*--)([a-z0-9-]+)(\s*:\s*)([^;]+)(;.*)$")
    out = {}
    for i in range(open_i, close_i + 1):
        m = line_re.match(lines[i])
        if m:
            out["--" + m.group(2)] = m.group(4).strip()
    return out


def sync_base_from_style(style_text: str) -> bool:
    """Fold the stylesheet's :root values into the base manifest. Returns
    True if the manifest changed on disk.

    THIS IS WHAT KEEPS THE DESIGN STUDIO FROM BREAKING ITS OWN INVARIANT.
    The studio writes static/style.css and nothing else, so before this every
    token the owner changed and saved left the shipped stylesheet disagreeing
    with the base - and ShippedStateInvariant, which exists to catch a skin
    left applied after visual verification, cannot tell that apart from a
    deliberate edit. It fired on three Design Studio commits on 2026-08-28
    and blocked every merge on the machine until the base was hand-edited.
    The manual "keep taurid.json in sync" rule is exactly the kind that fails.

    Only ADDITIVE and UPDATING: a base token absent from :root is left alone,
    because a skin may legitimately override a token the stock sheet does not
    declare. That is enough for both invariants - every :root token exists in
    the base (so the coverage test passes) and carries the same value (so
    applying the base is a no-op).
    """
    live = read_root_tokens(style_text)
    if not live:
        return False                              # no :root block found; write nothing
    m = load_manifest(BASE_ID)
    tokens = dict(m.get("tokens") or {})
    changed = {k: v for k, v in live.items() if tokens.get(k) != v}
    if not changed:
        return False
    for k, v in changed.items():
        _check_token(k, v)                        # never write a manifest we would refuse to read
    tokens.update(changed)
    m["tokens"] = tokens
    _write_text_atomic(_manifest_path(BASE_ID),
                       json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    return True


def _glass(m: dict) -> str | None:
    """The skin's glass layer (its extras CSS), or None when it carries none."""
    name = m.get("extras")
    if not name:
        return None
    p = (SKINS_DIR / name).resolve()
    if SKINS_DIR.resolve() not in p.parents or not p.is_file():
        raise HTTPException(500, f"skin extras missing: {name}")
    if p.stat().st_size > MAX_EXTRAS_BYTES:
        raise HTTPException(500, f"skin extras too large: {name}")
    return p.read_text(encoding="utf-8")


def _write_text_atomic(path: Path, text: str) -> None:
    """tmp + rename so a hard kill mid-write can never leave a truncated
    stylesheet or manifest behind."""
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def apply_skin(skin_id: str) -> dict:
    """Record this skin's tokens and glass as this install's look. A LOCAL
    action — no git, and no tracked file is written; the app reloads wearing
    it. Applying the base clears the look, so Vira serves the shipped files.
    ``changed`` names the served stylesheets whose content this moved."""
    migrate_legacy()                              # a pre-store look moves first
    skin = load_manifest(skin_id)
    load_manifest(BASE_ID)                        # the base must exist to reset to
    if skin_id == BASE_ID:
        tokens, glass = {}, None
    else:
        tokens = dict(skin.get("tokens") or {})
        glass = _glass(skin)
    for k, v in tokens.items():                   # revalidate what will be served
        try:
            _check_token(k, v)
        except ValueError as e:
            raise HTTPException(500, str(e))

    was_tokens, was_glass = overlay()
    jsonstore.write_atomic(STATE, {"id": skin_id, "tokens": tokens, "glass": glass})
    changed = [name for name, moved in (("style.css", was_tokens != tokens),
                                        ("skin-active.css", was_glass != glass))
               if moved]

    # No git: a skin is a personal, local look. It takes effect on the reload
    # the client triggers next. Persisting a skin as the shipped default is a
    # deliberate commit the owner makes, never a side effect of applying one.
    return {"ok": True, "active": skin_id, "changed": changed,
            "background": skin.get("background")}


def _has_rules(css: str) -> bool:
    return "{" in re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def migrate_legacy() -> dict | None:
    """Move a skin applied before the store held the look into the store, and
    put the tracked files back as shipped. Returns what it did, or None.

    Runs at startup and before every apply. It acts only on a record the old
    apply left: a non-base id with no "tokens". The look comes from the
    tracked files when they carry one (any :root value off the base, or
    glass with rules in skin-active.css), so values tuned after the apply
    survive the move; the files are then restored, :root to the base values
    and skin-active.css to its shipped header, exactly what applying the base
    used to do. When the files are already stock (set aside for a merge),
    the look is rebuilt from the recorded skin's manifest instead. The store
    is written before any tracked file, so an interrupted move leaves the
    look in both places rather than in neither."""
    s = _state()
    sid = s.get("id")
    if "tokens" in s or not isinstance(sid, str) or not ID_RE.match(sid) or sid == BASE_ID:
        return None
    base = load_manifest(BASE_ID)["tokens"]
    text = STYLE_CSS.read_text(encoding="utf-8") if STYLE_CSS.is_file() else ""
    off_base = {k: v for k, v in read_root_tokens(text).items()
                if k in base and base[k] != v}
    shipped = SKIN_ACTIVE.read_text(encoding="utf-8") if SKIN_ACTIVE.is_file() else ""
    glass = shipped if _has_rules(shipped) else None
    tokens = dict(off_base)
    if off_base or glass is not None:
        source = "tracked files"
    elif _manifest_path(sid).is_file():
        skin = load_manifest(sid)
        tokens, glass, source = dict(skin["tokens"]), _glass(skin), "manifest"
    else:
        return None                               # a retired skin, nothing on disk
    for k, v in list(tokens.items()):
        try:
            _check_token(k, v)
        except ValueError as e:
            log.warning("skins: dropped unservable token %s=%r: %s", k, v, e)
            del tokens[k]

    jsonstore.write_atomic(STATE, {"id": sid, "tokens": tokens, "glass": glass})
    restored = []
    if source == "tracked files":
        if off_base:
            _write_text_atomic(STYLE_CSS, _rewrite_root(text, base))
            restored.append("static/style.css")
        if glass is not None:
            _write_text_atomic(SKIN_ACTIVE, _ACTIVE_HEADER)
            restored.append("static/skin-active.css")
    out = {"id": sid, "source": source, "tokens": len(tokens),
           "glass": glass is not None, "restored": restored}
    log.warning("skins: moved the applied skin into %s: %s", STATE, out)
    return out


def served_style() -> str:
    """The tracked style.css with the applied skin's :root values in place."""
    text = STYLE_CSS.read_text(encoding="utf-8")
    tokens, _ = overlay()
    if not tokens:
        return text
    try:
        return _rewrite_root(text, tokens)
    except ValueError:                            # no :root block: nothing to lay over
        return text


def served_glass() -> str:
    """The applied skin's glass, or the tracked default."""
    _, glass = overlay()
    return glass if glass is not None else SKIN_ACTIVE.read_text(encoding="utf-8")


def _css(request: Request, text: str) -> Response:
    # These replace StaticFiles for two paths, so they keep its revalidation:
    # a content ETag answers an unchanged sheet with a 304. no-cache comes
    # from main's static middleware, which matches on the .css suffix.
    body = text.encode("utf-8")
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="text/css; charset=utf-8",
                    headers={"ETag": etag})


# ---------------------------------------------------------------- routes

@router.get("")
def get_skins():
    return list_skins()


@router.get("/{skin_id}")
def get_one(skin_id: str):
    m = load_manifest(skin_id)
    out = _meta(m)
    out["active"] = active_id() == m["id"]
    return out


@router.post("/{skin_id}/apply")
def post_apply(skin_id: str):
    return apply_skin(skin_id)


@assets.api_route("/style.css", methods=["GET", "HEAD"])
def get_style(request: Request):
    return _css(request, served_style())


@assets.api_route("/skin-active.css", methods=["GET", "HEAD"])
def get_glass(request: Request):
    return _css(request, served_glass())

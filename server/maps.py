"""Maps - the System Map's layered diagram, as a type any request can fill.

The System Map proved a shape: columns that read left to right (where
things come from, what holds them, what works on them, what you touch),
boxes coloured by one cross-cutting group, curved links that light up on
hover, and each box's full story one click away. This module makes that
shape reusable. Any subject - the owner's routines, a company's deal flow,
a household's paperwork - becomes one validated JSON spec, and the shared
renderer (static/maps/view.html) draws it live from GET /api/maps/<slug>.

The write boundary is the update_module_map discipline. A session
researches the subject and proposes the WHOLE spec through the native
`save_map` tool (server/viratools.py); `save()` validates it and stores it.
The session never writes the store or a page by hand, so a bad payload
comes back as a reason to fix, never as a broken page.

Store: data/maps.json - every saved map, the request it answers (the brief,
so Refresh can re-run it), and the one previous version, so a refresh that
goes wrong is one step from undone. The system map is NOT stored here:
`get("system")` derives it from the module registry (server/modulemap.py)
at read time, so it can never drift from the System Map window - and a
fresh clone, which has no private atlas page, still has a map to open.
"""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "maps.json"

BUILTIN = "system"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Past six columns a map stops reading left to right inside a window: the
# boxes shrink below a readable name. Past eight groups the colours stop
# being tellable apart on the dark ground (the palette below is eight).
MAX_COLUMNS = 6
MAX_GROUPS = 8
# A map is a picture of relationships; past 200 boxes it is a list, and
# the spec stops fitting comfortably in one tool call.
MAX_NODES = 200
# Per box. The hover highlight is only legible while a box's links can be
# followed by eye.
MAX_LINKS = 24
# How many maps one install keeps. A ceiling, not a target: it stops a
# looping session from filling the store.
MAX_MAPS = 100

# Field length caps. These bound what is stored and drawn (and so what a
# refresh prompt carries back to the model). A field over its cap is
# REFUSED with the number, never cut, so the session rewrites it shorter.
TEXT_CAPS = {"title": 80, "intro": 900, "column": 60, "group": 30,
             "name": 60, "kind": 60, "what": 1500, "how": 60,
             "brief": 1000}

# The System Map's group colours, in its order, then three more that stay
# distinct on the dark ground. Assigned by group position, so the renderer
# never has to choose and a session never has to.
PALETTE = ("#7fb3ff", "#d9b64a", "#5dd39e", "#c792ea", "#f0c674",
           "#f47272", "#6fd0e0", "#e39b6b")

# The system map's column titles - the same words the atlas page uses.
SYSTEM_COLUMNS = {
    "source": "Sources - outside Vira",
    "store": "Stores - state Vira builds",
    "engine": "Engines - the server",
    "surface": "Surfaces - what you touch",
}

_lock = threading.Lock()


class MapError(ValueError):
    """A spec, slug or request that cannot be saved; the message says why
    in words a session can act on."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty():
    return {"maps": {}}


def _read():
    s = jsonstore.read(STORE, _empty())
    if not isinstance(s, dict) or not isinstance(s.get("maps"), dict):
        return _empty()
    return s


# ---------------------------------------------------------------- the spec

def _text(value, field, cap_key, required=False):
    s = " ".join(str(value if value is not None else "").split())
    if required and not s:
        raise MapError(f"{field} is required")
    cap = TEXT_CAPS[cap_key]
    if len(s) > cap:
        raise MapError(f"{field} is {len(s)} characters; the cap is {cap} "
                       "- say it shorter")
    return s


def _ident(value, field):
    s = str(value or "").strip()
    if not ID_RE.match(s):
        raise MapError(f"{field} {s!r} must be lowercase kebab-case "
                       "(letters, digits, hyphens)")
    return s


def _list(value, field, lo, hi):
    if not isinstance(value, list):
        raise MapError(f"{field} must be a JSON array")
    if not lo <= len(value) <= hi:
        raise MapError(f"{field} holds {len(value)}; a map takes "
                       f"{lo} to {hi}")
    return value


def check_slug(slug):
    s = str(slug or "").strip()
    if not SLUG_RE.match(s):
        raise MapError(f"slug {s!r} must be short lowercase kebab-case, "
                       "e.g. 'routines-and-skills'")
    return s


def clean(spec):
    """Validate a candidate spec and return its normalized form: text
    whitespace-collapsed, groups coloured from the palette, links
    de-duplicated. Raises MapError naming the first problem, or every
    dangling link at once (the commonest mistake, and cheaper to fix in
    one pass)."""
    if not isinstance(spec, dict):
        raise MapError("the spec must be a JSON object with title, columns, "
                       "groups and nodes")
    out = {"title": _text(spec.get("title"), "title", "title", True),
           "intro": _text(spec.get("intro"), "intro", "intro")}

    cols, col_ids = [], set()
    for i, c in enumerate(_list(spec.get("columns"), "columns", 1,
                                MAX_COLUMNS)):
        if not isinstance(c, dict):
            raise MapError(f"columns[{i}] must be an object with id and title")
        cid = _ident(c.get("id"), "column id")
        if cid in col_ids:
            raise MapError(f"duplicate column id: {cid}")
        col_ids.add(cid)
        cols.append({"id": cid, "title": _text(
            c.get("title"), f"column {cid} title", "column", True)})
    out["columns"] = cols

    groups, group_ids = [], set()
    for i, g in enumerate(_list(spec.get("groups") or [], "groups", 0,
                                MAX_GROUPS)):
        if not isinstance(g, dict):
            raise MapError(f"groups[{i}] must be an object with id and name")
        gid = _ident(g.get("id"), "group id")
        if gid in group_ids:
            raise MapError(f"duplicate group id: {gid}")
        group_ids.add(gid)
        groups.append({"id": gid, "color": PALETTE[i],
                       "name": _text(g.get("name"), f"group {gid} name",
                                     "group", True)})
    out["groups"] = groups

    nodes, node_ids = [], set()
    for i, n in enumerate(_list(spec.get("nodes"), "nodes", 1, MAX_NODES)):
        if not isinstance(n, dict):
            raise MapError(f"nodes[{i}] must be an object")
        nid = _ident(n.get("id"), "node id")
        if nid in node_ids:
            raise MapError(f"duplicate node id: {nid}")
        node_ids.add(nid)
        col = str(n.get("column") or "").strip()
        if col not in col_ids:
            raise MapError(f"{nid}: column {col!r} is not one of the "
                           f"columns ({', '.join(sorted(col_ids))})")
        grp = str(n.get("group") or "").strip()
        if grp and grp not in group_ids:
            raise MapError(f"{nid}: group {grp!r} is not one of the groups"
                           + (f" ({', '.join(sorted(group_ids))})"
                              if group_ids else " (the map declares none)"))
        links = n.get("links") or []
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            raise MapError(f"{nid}: links must be an array of at most "
                           f"{MAX_LINKS}")
        nodes.append({
            "id": nid, "column": col, "group": grp,
            "name": _text(n.get("name"), f"{nid} name", "name", True),
            "kind": _text(n.get("kind"), f"{nid} kind", "kind"),
            "what": _text(n.get("what"), f"{nid} what", "what", True),
            "links": links})

    dangling = []
    for n in nodes:
        seen, kept = set(), []
        for j, link in enumerate(n["links"]):
            if not isinstance(link, dict):
                raise MapError(f"{n['id']}: links[{j}] must be an object "
                               "with to and how")
            to = str(link.get("to") or "").strip()
            if to == n["id"]:
                raise MapError(f"{n['id']}: a box cannot link to itself")
            if to not in node_ids:
                dangling.append(f"{n['id']} -> {to or '(empty)'}")
                continue
            if to in seen:
                continue
            seen.add(to)
            kept.append({"to": to, "how": _text(
                link.get("how"), f"{n['id']} link to {to}: how", "how",
                True)})
        n["links"] = kept
    if dangling:
        raise MapError("links point at boxes that do not exist: "
                       + "; ".join(dangling[:12])
                       + (f" (and {len(dangling) - 12} more)"
                          if len(dangling) > 12 else ""))

    used = {n["column"] for n in nodes}
    empty = [c["id"] for c in cols if c["id"] not in used]
    if empty:
        raise MapError(f"column(s) {', '.join(empty)} hold no boxes - "
                       "drop them or place boxes in them")
    out["nodes"] = nodes
    return out


# ---------------------------------------------------------------- the store

def save(slug, spec, brief=""):
    """Validated save (the native tool's write path). Replacing a map keeps
    its previous version and its brief unless a new brief is given.
    Returns a summary line for the session; raises MapError."""
    slug = check_slug(slug)
    if slug == BUILTIN:
        raise MapError("'system' is the built-in system map - it is drawn "
                       "from the module registry, which the System map "
                       "routine refreshes. Pick another slug.")
    norm = clean(spec)
    brief = _text(brief, "brief", "brief")
    now = _now_iso()
    before = {}

    def apply(s):
        maps = s["maps"]
        rec = maps.get(slug)
        if rec is None and len(maps) >= MAX_MAPS:
            raise MapError(f"this install already keeps {MAX_MAPS} maps - "
                           "the owner should delete some first")
        if rec:
            before["ids"] = {n["id"] for n in rec["spec"]["nodes"]}
            maps[slug] = {
                "spec": norm, "previous": rec["spec"],
                "meta": {**rec["meta"], "updated": now,
                         "revisions": int(rec["meta"].get("revisions", 1)) + 1,
                         "brief": brief or rec["meta"].get("brief", "")}}
        else:
            maps[slug] = {"spec": norm, "meta": {
                "created": now, "updated": now, "revisions": 1,
                "brief": brief}}

    with _lock:
        jsonstore.mutate(STORE, apply, _empty(), indent=1,
                         ensure_ascii=False)
    ids = {n["id"] for n in norm["nodes"]}
    links = sum(len(n["links"]) for n in norm["nodes"])
    line = (f"Saved map '{slug}': {len(ids)} boxes in "
            f"{len(norm['columns'])} columns, {links} links.")
    if "ids" in before:
        added = sorted(ids - before["ids"])
        removed = sorted(before["ids"] - ids)
        line += (f" Replaced the previous version (kept for undo)"
                 + (f"; added {', '.join(added[:10])}" if added else "")
                 + (f"; removed {', '.join(removed[:10])}" if removed else "")
                 + ".")
    return line + " The owner opens it in the Maps window."


def get(slug):
    """One map for the renderer: {slug, builtin, spec, meta}, or None."""
    if slug == BUILTIN:
        return system_map()
    rec = _read()["maps"].get(slug)
    if not rec:
        return None
    meta = {k: v for k, v in rec["meta"].items()}
    meta["has_previous"] = bool(rec.get("previous"))
    return {"slug": slug, "builtin": False, "spec": rec["spec"], "meta": meta}


def list_maps():
    """Every map for the picker: the built-in system map first, then the
    saved ones, most recently updated first."""
    rows = []
    for slug, rec in _read()["maps"].items():
        rows.append({"slug": slug, "builtin": False,
                     "title": rec["spec"]["title"],
                     "boxes": len(rec["spec"]["nodes"]),
                     "updated": rec["meta"].get("updated", ""),
                     "brief": rec["meta"].get("brief", ""),
                     "has_previous": bool(rec.get("previous"))})
    rows.sort(key=lambda r: r["updated"], reverse=True)
    sysmap = system_map()
    return [{"slug": BUILTIN, "builtin": True,
             "title": sysmap["spec"]["title"],
             "boxes": len(sysmap["spec"]["nodes"]),
             "updated": sysmap["meta"].get("updated", ""),
             "brief": "", "has_previous": False}] + rows


def delete(slug):
    """Remove a saved map (owner action only - no session tool reaches
    this). Returns True if it existed."""
    if slug == BUILTIN:
        raise MapError("the built-in system map cannot be deleted")
    gone = {}

    def apply(s):
        gone["hit"] = s["maps"].pop(slug, None) is not None

    with _lock:
        jsonstore.mutate(STORE, apply, _empty(), indent=1,
                         ensure_ascii=False)
    return gone["hit"]


def undo(slug):
    """Swap a map back to its previous version (one step). Returns True if
    there was one."""
    if slug == BUILTIN:
        raise MapError("the built-in system map has no saved versions")
    done = {}

    def apply(s):
        rec = s["maps"].get(slug)
        done["hit"] = bool(rec and rec.get("previous"))
        if done["hit"]:
            rec["spec"], rec["previous"] = rec["previous"], rec["spec"]
            rec["meta"]["updated"] = _now_iso()

    with _lock:
        jsonstore.mutate(STORE, apply, _empty(), indent=1,
                         ensure_ascii=False)
    return done["hit"]


# ---------------------------------------------------------- the system map

def system_map():
    """The module registry in map shape, derived at read time. Not run
    through clean(): the registry has its own validator and its prose has
    no length caps, and a dangling link there is skipped here rather than
    refusing the whole map."""
    from . import modulemap
    s = modulemap.registry()
    mods = s["modules"]
    ids = {m["id"] for m in mods}
    order = list(modulemap.GROUPS)
    groups = [{"id": g, "name": modulemap.GROUPS[g],
               "color": PALETTE[i % len(PALETTE)]}
              for i, g in enumerate(order)]
    nodes = [{"id": m["id"], "name": m.get("name", m["id"]),
              "column": m.get("layer", ""), "group": m.get("group", ""),
              "kind": m.get("kind", ""), "what": m.get("what", ""),
              "links": [{"to": l["to"], "how": l.get("how", "")}
                        for l in m.get("links") or []
                        if isinstance(l, dict) and l.get("to") in ids]}
             for m in mods]
    meta = s.get("meta") or {}
    return {"slug": BUILTIN, "builtin": True, "spec": {
        "title": "The system map",
        "intro": ("Left to right: the data that exists outside Vira, the "
                  "state Vira builds from it, the server engines that do "
                  "the work, and the surfaces you actually touch. Drawn "
                  "live from the module registry, which the System map "
                  "routine refreshes from the change log."),
        "columns": [{"id": l, "title": SYSTEM_COLUMNS.get(l, l)}
                    for l in modulemap.LAYERS],
        "groups": groups, "nodes": nodes},
        "meta": {"updated": meta.get("last_refresh") or meta.get("seeded")
                 or "", "brief": "", "has_previous": False}}


# ---------------------------------------------------------------- prompts

SPEC_SHAPE = (
    '{"title": str, "intro": str, '
    '"columns": [{"id": "kebab-id", "title": str}], '
    '"groups": [{"id": "kebab-id", "name": str}], '
    '"nodes": [{"id": "kebab-id", "name": str, "column": "<column id>", '
    '"group": "<group id>", "kind": str, "what": str, '
    '"links": [{"to": "<node id>", "how": str}]}]}')

LIMITS_LINE = (
    f"Limits: 1 to {MAX_COLUMNS} columns, up to {MAX_GROUPS} groups, "
    f"1 to {MAX_NODES} boxes, up to {MAX_LINKS} links per box. Ids are "
    "lowercase kebab-case and unique. Lengths: name "
    f"{TEXT_CAPS['name']}, kind {TEXT_CAPS['kind']}, what "
    f"{TEXT_CAPS['what']}, how {TEXT_CAPS['how']}, intro "
    f"{TEXT_CAPS['intro']} characters. Every column must hold a box.")

_DESIGN = (
    "A Vira map is a layered diagram, the same type as the System Map. "
    "COLUMNS read left to right as a flow: for a system, where things come "
    "from, what holds them, what works on them, what the owner touches; "
    "for a process, its stages in order. BOXES sit in those columns, each "
    "coloured by one GROUP - a cross-cutting question the owner would ask "
    "of the map (which area of life, which kind of work), never a "
    "restatement of the columns. LINKS say how one box feeds, triggers or "
    "uses another; hovering a box lights its links, clicking it shows its "
    "full story.\n\n"
    "METHOD\n"
    "1. Find out what actually exists before drawing anything. Use whatever "
    "reaches the subject: your native Vira tools, the files on this "
    "machine, this repository's code when the subject is Vira itself, the "
    "web when the subject is public. Never invent a box to fill a column.\n"
    "2. Choose 2 to 6 columns so that most links point left to right. "
    "Within a column, order boxes so linked boxes sit near each other.\n"
    "3. Every box gets a short `name`; a `kind` of a few words (what sort "
    "of thing it is, how often it runs, where it lives); and a `what` of "
    "two to four plain sentences: what it does for the owner, what it "
    "reads, what it produces. House voice: plain words, no jargon, no "
    "emojis.\n"
    "4. Link only where something real flows - data, a trigger, a "
    "dependency. `how` is a short verb phrase that reads "
    "'<this box> <how> <target box>': 'feeds', 'is run by', 'writes to'.\n"
    "5. `intro` is one or two sentences telling the owner how to read the "
    "map, left to right.\n\n")

_SAVE = (
    "Call mcp__vira__save_map ONCE with the whole map. It validates and "
    "stores the map, and the Maps window draws it - do NOT write HTML or "
    "any file yourself, and do not change anything else on this machine. "
    "If the tool returns an error, fix the payload and call it again.\n\n"
    "spec_json shape (one JSON string):\n" + SPEC_SHAPE + "\n" + LIMITS_LINE)


def ask_prompt(request):
    """The job that builds a new map from the owner's request."""
    request = _text(request, "request", "brief", True)
    return (
        "You are Vira's cartographer. The owner asked for a map:\n\n"
        f"  \"{request}\"\n\n" + _DESIGN +
        "THEN SAVE IT\n" + _SAVE + "\n"
        "Arguments: slug = a short kebab-case name for the subject (e.g. "
        "'routines-and-skills'; check it is not one the owner already "
        "has unless you mean to replace it); brief = the owner's request "
        "above, verbatim (Refresh re-runs it); spec_json = the map.\n\n"
        "When the tool reports success, say in two or three sentences what "
        "the map shows and anything you could not find.")


def refresh_prompt(slug):
    """The job that brings a saved map up to date with how things stand
    now. Raises MapError for an unknown or built-in slug."""
    if slug == BUILTIN:
        raise MapError("the system map refreshes through /api/map/refresh")
    m = get(slug)
    if not m:
        raise MapError(f"no map named {slug!r}")
    brief = m["meta"].get("brief") or m["spec"]["title"]
    return (
        "You are Vira's cartographer. The owner's map "
        f"'{m['spec']['title']}' (slug {slug}) answers this request:\n\n"
        f"  \"{brief}\"\n\n"
        "Bring it up to date with how things stand TODAY. Keep ids stable "
        "for everything that still exists; add what is new; remove what is "
        "gone; rewrite a `what` only where it no longer matches reality. "
        "If nothing has changed, say so and stop - do not save.\n\n"
        + _DESIGN + "THEN SAVE IT\n" + _SAVE + "\n"
        f"Arguments: slug = {slug} (the same map); brief = the request "
        "above, verbatim; spec_json = the full updated map, every box, not "
        "a diff.\n\n"
        "CURRENT MAP:\n"
        + json.dumps(m["spec"], indent=1, ensure_ascii=False))

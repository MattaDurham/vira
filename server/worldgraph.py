"""Vira's temporal World graph.

The old Contact Atlas is one valuable projection of the owner's world, but
people are not the only things in that world.  This module composes the
materialized CRM atlas with every connected Markdown vault and returns one
typed, temporal, provenance-carrying graph:

* CRM contacts remain person nodes, with their existing deterministic ties.
* Markdown notes become nodes; typed frontmatter promotes them to people,
  organizations, projects, places, events, sources, or concepts.
* Wikilinks and shared tags become relations whose receipt is the exact note
  and line that asserted them.
* ``valid_from`` / ``valid_to`` describe when an item held in the world;
  ``recorded_at`` describes when Vira learned it.  Both are optional and the
  API can replay either axis without inventing dates where none exist.

This is deliberately a derived read model.  The CRM and vault files stay the
sources of truth, no personal material is copied into the public repository,
and a request never writes back to either source.

The bitemporal vocabulary and receipt-first design were informed by the
Apache-2.0 Utopia project (https://github.com/deeplethe/utopia).  This is a
native Vira implementation over Vira's existing stores, not a vendored copy
or a second knowledge system.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

from . import atlas, data as crm, settings, vault, worldlayout

_WIKILINK_RE = re.compile(r"\[\[([^\]|#^]+)(?:\|[^\]]+)?\]\]")
_NAME_RE = re.compile(r"[^a-z0-9]+")
_SCALAR_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$")

KIND_ALIASES = {
    "people": "person", "contact": "person", "contacts": "person",
    "company": "organization", "org": "organization",
    "employer": "organization", "institution": "organization",
    "application": "project", "role": "project", "workstream": "project",
    "location": "place", "city": "place",
    "meeting": "event", "milestone": "event",
    "document": "source", "reference": "source", "article": "source",
    "entity": "concept", "definition": "concept", "topic": "concept",
    "session": "note", "brief": "note", "retro": "note",
}

KIND_LABELS = {
    "person": "People",
    "organization": "Organizations",
    "project": "Projects",
    "place": "Places",
    "event": "Events",
    "source": "Sources",
    "concept": "Concepts",
    "topic": "Topics",
    "note": "Notes",
}

DATE_KEYS = {
    # Transaction time: when this Vira learned or received the item.
    "recorded": ("recorded_at", "learned_at", "added_at", "ingested_at"),
    # Content time: when the note or the knowledge it records was created.
    "created": ("created_at", "created", "date", "published_at",
                "published"),
    "from": ("valid_from", "start_date", "starts", "start", "event_date",
             "met_on", "first_met", "date_met", "met"),
    "to": ("valid_to", "end_date", "ends", "end"),
}

_page_cache = {"fingerprint": None, "pages": [], "total": 0, "sources": 0}
_page_cache_lock = threading.Lock()
_graph_cache = {"key": None, "result": None}
_graph_cache_lock = threading.Lock()
_json_cache = {"graph": None, "payload": None}
_json_cache_lock = threading.Lock()


def _norm_name(value):
    return _NAME_RE.sub(" ", str(value or "").lower()).strip()


def _stable_id(prefix, value):
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _unquote(value):
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _list_value(value):
    raw = str(value or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [_unquote(bit.strip()) for bit in raw.split(",") if bit.strip()]


def _frontmatter(text):
    """Small, dependency-free reader for the scalar/list fields we own.

    It is intentionally not a general YAML parser.  Unknown structures stay
    untouched in their source file rather than being interpreted loosely.
    """
    if not text.startswith("---"):
        return {}, text, 1
    lines = text.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, text, 1
    out, current = {}, None
    for line in lines[1:end]:
        match = _SCALAR_RE.match(line)
        if match:
            current = match.group(1).lower().replace("-", "_")
            out[current] = _unquote(match.group(2))
            continue
        if current and line.lstrip().startswith("-"):
            existing = out.get(current)
            if not isinstance(existing, list):
                existing = _list_value(existing)
            existing.append(_unquote(line.lstrip()[1:].strip()))
            out[current] = existing
    return out, "\n".join(lines[end + 1:]), end + 2


def _kind(raw, rel=""):
    value = str(raw or "").strip().lower().replace("-", "_")
    value = KIND_ALIASES.get(value, value)
    if value in KIND_LABELS:
        return value
    folder = Path(rel).parts[0].lower() if Path(rel).parts else ""
    if folder in {"sessions", "retros", "brain_retros", "briefs"}:
        return "note"
    return "note"


def _iso_date(value):
    """Return ``(UTC ISO string, precision)`` or ``(None, None)``."""
    raw = _unquote(value).strip()
    if not raw:
        return None, None
    # A prose how-we-met field is not a date.  Only accept an ISO-looking
    # prefix, while preserving year/month precision for honest rendering.
    match = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", raw)
    if not match:
        return None, None
    year, month, day = match.groups()
    precision = "day" if day else "month" if month else "year"
    if len(raw) == len(match.group(0)):
        raw = match.group(0) + ("-01" if precision == "month" else
                               "-01-01" if precision == "year" else "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw[:10]),
                                      datetime.min.time())
        except ValueError:
            return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(), precision


def _first_date(mapping, keys):
    for key in keys:
        value, precision = _iso_date(mapping.get(key))
        if value:
            return value, precision, key
    return None, None, None


def _filename_date(path):
    """Best-effort content date from a dated filename, with provenance."""
    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)",
        Path(path).stem)
    if not match:
        return None, None, None
    value, precision = _iso_date("-".join(match.groups()))
    if not value:
        return None, None, None
    return value, precision, "filename_date"


def _public_ref(spec, rel):
    rel = str(rel).replace("\\", "/").lstrip("/")
    return rel if spec.get("primary") else f"@{spec['id']}/{rel}"


def _note_paths(spec):
    root = Path(spec["root"]).expanduser()
    if not root.is_dir():
        return []
    dirs = spec.get("dirs")
    bases = [root] if dirs is None else [root / str(item) for item in dirs]
    seen, rows = set(), []
    for base in bases:
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            try:
                resolved = path.resolve()
                rel = resolved.relative_to(root.resolve())
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            key = rel.as_posix().lower()
            if key in seen or any(part.startswith(".") for part in rel.parts):
                continue
            seen.add(key)
            learned = getattr(stat, "st_birthtime", None)
            learned_source = "file_birthtime"
            if learned is None:
                learned = getattr(stat, "st_ctime", stat.st_mtime)
                learned_source = "file_ctime"
            rows.append((stat.st_mtime, learned, learned_source, resolved, rel))
    # Stable newest-first ordering makes rebuilds deterministic.  World does
    # not truncate this list: the user asked for the actual connected vault,
    # and scale is the renderer's problem rather than a reason to hide data.
    rows.sort(key=lambda row: (-row[0], row[4].as_posix().lower()))
    return rows


def _read_note(spec, row):
    _mtime, learned_time, learned_source, path, relpath = row
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    meta, body, body_line = _frontmatter(text)
    title = _unquote(meta.get("title") or meta.get("name"))
    if not title:
        title = relpath.stem.replace("-", " ").replace("_", " ").strip()
    recorded, recorded_precision, recorded_key = _first_date(
        meta, DATE_KEYS["recorded"])
    if not recorded:
        recorded = datetime.fromtimestamp(
            learned_time, timezone.utc).isoformat()
        recorded_precision, recorded_key = "instant", learned_source
    valid_from, from_precision, from_key = _first_date(meta, DATE_KEYS["from"])
    if not valid_from:
        valid_from, from_precision, from_key = _first_date(
            meta, DATE_KEYS["created"])
    if not valid_from:
        valid_from, from_precision, from_key = _filename_date(relpath)
    if not valid_from:
        valid_from = datetime.fromtimestamp(
            learned_time, timezone.utc).isoformat()
        from_precision, from_key = "instant", learned_source
    valid_to, to_precision, to_key = _first_date(meta, DATE_KEYS["to"])
    kind = _kind(meta.get("type") or meta.get("kind"), relpath.as_posix())
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = _list_value(tags)
    ref = _public_ref(spec, relpath.as_posix())
    links = []
    for offset, line in enumerate(body.splitlines()):
        for match in _WIKILINK_RE.finditer(line):
            links.append({"target": match.group(1).strip(),
                          "line": body_line + offset})
    return {
        "id": _stable_id("note", f"{spec['id']}:{relpath.as_posix().lower()}"),
        "name": title[:160], "kind": kind, "ref": ref,
        "open_kind": "note", "source_id": spec["id"],
        "source_name": str(spec.get("name") or spec["id"]),
        "rel": relpath.as_posix(), "vault": True, "face": None,
        "company": "", "title": "", "qualifier": "",
        "degree": None, "cluster": None, "act": 0,
        "valid_from": valid_from, "valid_to": valid_to,
        "recorded_at": recorded,
        "time_precision": {"valid_from": from_precision,
                           "valid_to": to_precision,
                           "recorded_at": recorded_precision},
        "time_source": {"valid_from": from_key, "valid_to": to_key,
                        "recorded_at": recorded_key},
        "tags": [str(tag).strip()[:80] for tag in tags if str(tag).strip()],
        "links": links, "truncated": False,
        "receipts": [{"kind": "note", "ref": ref, "line": 1,
                      "label": str(spec.get("name") or spec["id"])}],
    }


def _vault_pages():
    """Cached Markdown projection, invalidated by count, names, or mtime.

    A node-detail click must not reread hundreds of notes.  We still stat the
    configured roots on every composition so edits land without a restart;
    only parsing and link extraction are cached.
    """
    specs = vault.source_specs()
    source_rows, fingerprints, total = [], [], 0
    for spec in specs:
        rows = _note_paths(spec)
        total += len(rows)
        names = "\n".join(row[4].as_posix() for row in rows)
        fingerprints.append((
            spec["id"], str(Path(spec["root"]).expanduser()), len(rows),
            max((row[0] for row in rows), default=0),
            hashlib.sha1(names.encode("utf-8")).hexdigest(),
        ))
        source_rows.append((spec, rows))
    fingerprint = tuple(fingerprints)
    with _page_cache_lock:
        if _page_cache["fingerprint"] == fingerprint:
            return (deepcopy(_page_cache["pages"]), _page_cache["total"],
                    _page_cache["sources"])
        pages = []
        for spec, rows in source_rows:
            for row in rows:
                note = _read_note(spec, row)
                if note:
                    pages.append(note)
        _page_cache.update(fingerprint=fingerprint, pages=pages,
                           total=total, sources=len(specs))
        return deepcopy(pages), total, len(specs)


def _crm_time(pid, cache):
    merged = {}
    merged.update(cache.get("by_id", {}).get(pid) or {})
    merged.update(cache.get("master", {}).get(pid) or {})
    merged.update(cache.get("profiles", {}).get(pid) or {})
    valid_from, from_precision, from_key = _first_date(merged, DATE_KEYS["from"])
    recorded, recorded_precision, recorded_key = _first_date(
        merged, DATE_KEYS["recorded"])
    return {
        "valid_from": valid_from, "valid_to": None,
        "recorded_at": recorded,
        "time_precision": {"valid_from": from_precision,
                           "valid_to": None,
                           "recorded_at": recorded_precision},
        "time_source": {"valid_from": from_key, "valid_to": None,
                        "recorded_at": recorded_key},
    }


def _crm_graph():
    base = atlas.compose(vault=False)
    if base.get("status") != "ok":
        return {"generated": None, "owner": {
                    "name": settings.get("owner_name") or "me", "pid": None},
                "nodes": [], "edges": [], "ego_edges": []}
    try:
        cache = crm._load()
    except Exception:
        cache = {}
    nodes = []
    for raw in base.get("nodes", []):
        node = deepcopy(raw)
        node.update(kind="person", open_kind="person", source_id="crm",
                    source_name="CRM", ref=None, note_ref=None, vault=False,
                    receipts=[{"kind": "crm", "label": "CRM record"}])
        node.update(_crm_time(node["id"], cache))
        nodes.append(node)
    # Contact Atlas intentionally materializes only its most active slice for
    # the old face graph. World is not that slice: every CRM person belongs in
    # the semantic space even when no deterministic relationship edge has
    # been built for them yet.
    present = {node["id"] for node in nodes}
    owner_pid = (base.get("owner") or {}).get("pid")
    for person in cache.get("people", []):
        pid = person.get("id")
        if not pid or pid == owner_pid or pid in present:
            continue
        master = cache.get("master", {}).get(pid) or {}
        profile = cache.get("profiles", {}).get(pid) or {}
        activity = person.get("activity") or {}
        node = {
            "id": pid, "name": person.get("name") or "Unnamed contact",
            "tier": person.get("profile_tier") or person.get("master_tier"),
            "company": str(master.get("company") or "")[:60],
            "title": str(master.get("title") or "")[:60],
            "relationship_class": profile.get("relationship_class"),
            "degree": None, "cluster": None, "face": None,
            "act": ((activity.get("imsg_n") or 0)
                    + (activity.get("email_n") or 0) * 2),
            "kind": "person", "open_kind": "person",
            "source_id": "crm", "source_name": "CRM", "ref": None,
            "note_ref": None, "vault": False,
            "receipts": [{"kind": "crm", "label": "CRM record"}],
        }
        node.update(_crm_time(pid, cache))
        nodes.append(node)
        present.add(pid)
    edges = []
    for raw in base.get("edges", []):
        edge = deepcopy(raw)
        signal = next(iter(edge.get("signals") or []), {})
        edge.update(
            id=_stable_id("edge", f"crm:{edge.get('a')}:{edge.get('b')}"),
            source=edge.get("a"), target=edge.get("b"),
            relation=signal.get("type") or "connected",
            label=signal.get("detail") or "connected",
            valid_from=None, valid_to=None,
            recorded_at=base.get("generated"),
            receipts=[{"kind": "crm-signal", "label": s.get("detail") or
                       s.get("type", "CRM signal")} for s in
                      edge.get("signals", [])],
        )
        edges.append(edge)
    ego_edges = deepcopy(base.get("ego_edges", []))
    return {"generated": base.get("generated"), "owner": base.get("owner"),
            "nodes": nodes, "edges": edges, "ego_edges": ego_edges}


def _indexes(pages):
    by_path, by_stem, by_title = {}, defaultdict(list), defaultdict(list)
    for page in pages:
        sid = page["source_id"]
        rel = page["rel"].lower()
        by_path[(sid, rel)] = page
        by_path[(sid, rel.removesuffix(".md"))] = page
        by_stem[(sid, Path(rel).stem)].append(page)
        by_title[(sid, _norm_name(page["name"]))].append(page)
    return by_path, by_stem, by_title


def _resolve_link(page, target, indexes):
    by_path, by_stem, by_title = indexes
    sid = page["source_id"]
    clean = target.strip().replace("\\", "/").lstrip("/")
    if clean.lower().endswith(".md"):
        clean = clean[:-3]
    parent = Path(page["rel"]).parent
    candidates = [(parent / clean).as_posix().lower(), clean.lower()]
    for candidate in candidates:
        hit = by_path.get((sid, candidate))
        if hit:
            return hit
    stem_hits = by_stem.get((sid, Path(clean).stem.lower()), [])
    if len(stem_hits) == 1:
        return stem_hits[0]
    title_hits = by_title.get((sid, _norm_name(Path(clean).name)), [])
    return title_hits[0] if len(title_hits) == 1 else None


def _active(item, at, axis):
    if not at:
        return True
    moment, _precision = _iso_date(at)
    if not moment:
        raise ValueError("invalid at date")
    if axis == "recorded":
        recorded = item.get("recorded_at")
        return not recorded or recorded <= moment
    start, end = item.get("valid_from"), item.get("valid_to")
    return (not start or start <= moment) and (not end or moment < end)


def _kind_lens(nodes):
    counts = Counter(node.get("kind") or "note" for node in nodes)
    ordered = [kind for kind in KIND_LABELS if counts.get(kind)]
    ordered += sorted(set(counts) - set(ordered))
    bands = [{"id": f"kind:{kind}", "label": KIND_LABELS.get(
                  kind, kind.replace("_", " ").title()),
              "kind": kind, "size": counts[kind]} for kind in ordered]
    node_band = {node["id"]: f"kind:{node.get('kind') or 'note'}"
                 for node in nodes}
    return [{"id": "kind", "label": "Kinds", "blurb":
             "People are one filter over the whole local knowledge world.",
             "total": len(nodes), "placed": len(nodes),
             "bands": bands, "node_band": node_band, "editable": False}]


def _timeline(nodes, edges):
    items = [*nodes, *edges]
    valid = sorted(value for item in items for value in
                   (item.get("valid_from"), item.get("valid_to")) if value)
    recorded = sorted(item.get("recorded_at") for item in items
                      if item.get("recorded_at"))
    values = sorted([*valid, *recorded])
    valid_nodes = sum(bool(n.get("valid_from") or n.get("valid_to"))
                      for n in nodes)
    recorded_nodes = sum(bool(n.get("recorded_at")) for n in nodes)
    return {"min": values[0] if values else None,
            "max": values[-1] if values else None,
            "valid": {"min": valid[0] if valid else None,
                      "max": valid[-1] if valid else None,
                      "dated_nodes": valid_nodes,
                      "undated_nodes": len(nodes) - valid_nodes},
            "recorded": {"min": recorded[0] if recorded else None,
                         "max": recorded[-1] if recorded else None,
                         "dated_nodes": recorded_nodes,
                         "undated_nodes": len(nodes) - recorded_nodes},
            "dated_nodes": sum(bool(n.get("valid_from") or
                                    n.get("recorded_at")) for n in nodes),
            "undated_nodes": sum(not (n.get("valid_from") or
                                       n.get("recorded_at")) for n in nodes)}


def compose(at=None, axis="valid", kinds=None):
    """Compose the current World graph, optionally replayed at one date."""
    if axis not in {"valid", "recorded"}:
        raise ValueError("axis must be valid or recorded")
    base = _crm_graph()
    nodes, edges = list(base["nodes"]), list(base["edges"])
    all_pages, total_notes, source_count = _vault_pages()
    kind_filter = {str(kind).strip().lower() for kind in (kinds or [])
                   if str(kind).strip()}
    cache_key = (base.get("generated"), _page_cache.get("fingerprint"),
                 worldlayout.source_fingerprint())
    if not at and not kind_filter:
        with _graph_cache_lock:
            if _graph_cache["key"] == cache_key:
                return _graph_cache["result"]

    # Fold a typed vault person onto an unambiguous CRM person.  The CRM id
    # remains canonical so every existing profile deep link keeps working.
    by_name = defaultdict(list)
    for node in nodes:
        name = _norm_name(node.get("name"))
        if len(name.split()) >= 2:
            by_name[name].append(node)
    page_to_node, kept_pages = {}, []
    for page in all_pages:
        match = by_name.get(_norm_name(page["name"]), []) \
            if page["kind"] == "person" else []
        if len(match) == 1:
            node = match[0]
            node["note_ref"] = page["ref"]
            node["receipts"] = [*(node.get("receipts") or []),
                                *(page.get("receipts") or [])]
            # A wiki page can supply the real-world date we met someone, but
            # its own file date must not become the CRM record's birth date.
            # Those are two different receipts on the recorded-time axis.
            for key in ("valid_from",):
                if not node.get(key) and page.get(key):
                    node[key] = page[key]
                    node["time_precision"][key] = page["time_precision"][key]
                    node["time_source"][key] = page["time_source"][key]
            page_to_node[page["id"]] = node["id"]
        else:
            kept_pages.append(page)
            page_to_node[page["id"]] = page["id"]
    nodes.extend(kept_pages)

    indexes = _indexes(all_pages)
    edge_by_key = {(min(e["a"], e["b"]), max(e["a"], e["b"]),
                    e.get("relation")): e for e in edges}
    for page in all_pages:
        source_id = page_to_node[page["id"]]
        for link in page["links"]:
            target_page = _resolve_link(page, link["target"], indexes)
            if not target_page:
                continue
            target_id = page_to_node[target_page["id"]]
            if source_id == target_id:
                continue
            pair = (min(source_id, target_id), max(source_id, target_id),
                    "wikilink")
            receipt = {"kind": "note", "ref": page["ref"],
                       "line": link["line"],
                       "label": f"{page['name']} line {link['line']}"}
            if pair in edge_by_key:
                edge_by_key[pair].setdefault("receipts", []).append(receipt)
                continue
            edge = {
                "id": _stable_id("edge", f"{page['id']}:{target_page['id']}"),
                "a": source_id, "b": target_id,
                "source": source_id, "target": target_id,
                "weight": 0.85, "relation": "wikilink",
                "label": "links to", "structural": True,
                "signals": [{"type": "wikilink",
                             "detail": f"linked from {page['name']}"}],
                "receipts": [receipt],
                "valid_from": page.get("valid_from"),
                "valid_to": page.get("valid_to"),
                "recorded_at": page.get("recorded_at"),
            }
            edges.append(edge)
            edge_by_key[pair] = edge

    # Tags used more than once become first-class topic nodes.  Single-use
    # tags remain metadata; turning every private filing label into a visible
    # object would make the map noisier without adding a relationship.
    tagged = defaultdict(list)
    node_by_id = {node["id"]: node for node in nodes}
    for page in all_pages:
        nid = page_to_node[page["id"]]
        for tag in page.get("tags", []):
            tagged[_norm_name(tag)].append((nid, page, tag))
    ranked_tags = sorted(((tag, rows) for tag, rows in tagged.items()
                          if tag and len({row[0] for row in rows}) >= 2),
                         key=lambda item: (-len(item[1]), item[0]))
    for tag, rows in ranked_tags:
        tid = _stable_id("tag", tag)
        if tid not in node_by_id:
            # A shared topic exists once its second distinct supporting note
            # is known.  Using the newest note would make old topics vanish
            # from recorded-time replay whenever a later note reused them.
            known = sorted({row[1].get("recorded_at") for row in rows
                            if row[1].get("recorded_at")})
            first_shared = known[1] if len(known) > 1 else (
                known[0] if known else None)
            valid_known = sorted({row[1].get("valid_from") for row in rows
                                  if row[1].get("valid_from")})
            valid_shared = valid_known[1] if len(valid_known) > 1 else (
                valid_known[0] if valid_known else None)
            topic = {"id": tid, "name": rows[0][2], "kind": "topic",
                     "open_kind": None, "source_id": "derived",
                     "source_name": "Vault tags", "ref": None,
                     "note_ref": None, "vault": False, "face": None,
                     "company": "", "title": "", "qualifier": "",
                     "degree": None, "cluster": None, "act": len(rows),
                     "valid_from": valid_shared, "valid_to": None,
                     "recorded_at": first_shared,
                     "time_precision": {"valid_from": "derived",
                                        "valid_to": None,
                                        "recorded_at": "derived"},
                     "time_source": {"valid_from": "tagged_notes",
                                     "valid_to": None,
                                     "recorded_at": "tagged_notes"},
                     "tags": [], "receipts": []}
            nodes.append(topic)
            node_by_id[tid] = topic
        for nid, page, raw_tag in rows:
            pair = (min(nid, tid), max(nid, tid), "tagged")
            if pair in edge_by_key:
                continue
            edge = {"id": _stable_id("edge", f"tag:{nid}:{tid}"),
                          "a": nid, "b": tid, "source": nid, "target": tid,
                          "weight": 0.45, "relation": "tagged",
                          "label": f"tagged {raw_tag}", "structural": False,
                          "signals": [{"type": "tagged",
                                       "detail": f"tagged {raw_tag}"}],
                          "receipts": page.get("receipts", []),
                          "valid_from": page.get("valid_from"),
                          "valid_to": page.get("valid_to"),
                          "recorded_at": page.get("recorded_at")}
            edges.append(edge)
            edge_by_key[pair] = edge

    positions, layout = worldlayout.positions(nodes, edges, page_to_node)
    for node in nodes:
        node["position"] = positions[node["id"]]

    visible = {node["id"] for node in nodes
               if (not kind_filter or node.get("kind") in kind_filter)
               and _active(node, at, axis)}
    nodes = [node for node in nodes if node["id"] in visible]
    edges = [edge for edge in edges
             if edge.get("a") in visible and edge.get("b") in visible
             and _active(edge, at, axis)]
    degree = Counter()
    for edge in edges:
        degree[edge["a"]] += 1
        degree[edge["b"]] += 1
    for node in nodes:
        node["graph_degree"] = degree[node["id"]]
        if node.get("degree") is None:
            node["degree"] = 1 if degree[node["id"]] else 3

    counts = Counter(node.get("kind") or "note" for node in nodes)
    result = {
        "schema": "vira.world.v1", "status": "ok" if nodes else "empty",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "owner": base["owner"], "nodes": nodes, "edges": edges,
        "ego_edges": [edge for edge in base["ego_edges"]
                      if edge.get("b") in visible],
        "lenses": _kind_lens(nodes), "timeline": _timeline(nodes, edges),
        "kinds": [{"id": kind, "label": KIND_LABELS.get(
                       kind, kind.replace("_", " ").title()), "count": count}
                  for kind, count in sorted(counts.items())],
        "scope": {"shown_notes": len(all_pages), "total_notes": total_notes,
                  "truncated": False,
                  "unreadable": max(0, total_notes - len(all_pages)),
                  "sources": source_count},
        "layout": layout,
        "replay": {"at": at, "axis": axis},
        "building": False,
    }
    if not at and not kind_filter:
        # Opening a SQLite sidecar can make its WAL visible between the first
        # fingerprint and the completed vector read.  Store the finished
        # graph under the post-read fingerprint so request two is a real
        # cache hit even on a completely cold process.
        cache_key = (base.get("generated"), _page_cache.get("fingerprint"),
                     worldlayout.source_fingerprint())
        with _graph_cache_lock:
            _graph_cache.update(key=cache_key, result=result)
    return result


def node_detail(node_id):
    with _graph_cache_lock:
        graph = _graph_cache.get("result")
    graph = graph or compose()
    node = next((item for item in graph["nodes"] if item["id"] == node_id),
                None)
    if not node:
        return None
    by_id = {item["id"]: item for item in graph["nodes"]}
    connected = []
    for edge in graph["edges"]:
        if node_id not in (edge.get("a"), edge.get("b")):
            continue
        other_id = edge["b"] if edge["a"] == node_id else edge["a"]
        other = by_id.get(other_id)
        if not other:
            continue
        connected.append({**deepcopy(edge), "pid": other_id,
                          "name": other["name"], "kind": other["kind"]})
    connected.sort(key=lambda row: (-float(row.get("weight") or 0),
                                    row["name"].lower()))
    ego = next((edge for edge in graph.get("ego_edges", [])
                if edge.get("b") == node_id), None)
    return {"node": node, "edges": connected, "ego": ego}


def encoded(at=None, axis="valid", kinds=None):
    """Serialized API payload, cached for the 75 MB full-vault response.

    Returning a dict makes FastAPI recursively convert the whole graph before
    JSON encoding it on every request.  The graph is already JSON-native and
    immutable after composition, so cache its compact bytes beside the graph.
    """
    graph = compose(at=at, axis=axis, kinds=kinds)
    cacheable = not at and not [kind for kind in (kinds or []) if kind]
    if cacheable:
        with _json_cache_lock:
            if _json_cache["graph"] is graph:
                return _json_cache["payload"]
    payload = json.dumps(graph, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if cacheable:
        with _json_cache_lock:
            # Retaining the graph object also prevents a future graph from
            # reusing its Python id and receiving stale serialized bytes.
            _json_cache.update(graph=graph, payload=payload)
    return payload

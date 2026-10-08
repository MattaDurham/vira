"""Semantic 3D coordinates for Vira's World graph.

The vault and CRM already keep local nomic-embed-text vectors for retrieval.
World reuses those sidecars: it averages chunk vectors into one vector per
note, combines a folded person page with that person's CRM vector, and makes a
deterministic three-component projection.  No source text or vector leaves the
machine, and the result is a derived in-memory read model.

Randomized PCA keeps the first request practical for a full vault.  It is not
an arbitrary random projection: the small random range finder discovers the
dominant subspace, then an exact SVD inside that subspace selects the three
axes.  A fixed seed and stable node ordering make the coordinates repeatable.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

from . import crmindex, vault

try:
    import numpy as np
except ImportError:  # the minimal install remains a deterministic graph
    np = None


PCA_OVERSAMPLE = 8
WORLD_RADIUS = 900.0

_vector_cache = {"fingerprint": None, "vectors": {}, "dimensions": 0}
_layout_cache = {"key": None, "positions": {}, "meta": {},
                 "page_to_node": {}}
_lock = threading.Lock()


def _stable_id(prefix, value):
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _fingerprint(specs):
    rows = []
    paths = [Path(spec["db"]) for spec in specs if spec.get("db")]
    for path in [*paths, crmindex.DB]:
        for candidate in (path, Path(str(path) + "-wal")):
            try:
                stat = candidate.stat()
                rows.append((str(candidate), stat.st_mtime_ns, stat.st_size))
            except OSError:
                rows.append((str(candidate), 0, 0))
    return tuple(rows)


def source_fingerprint():
    """Cheap public cache key for the vector sidecars World consumes."""
    return _fingerprint(vault.source_specs())


def _connect_readonly(path):
    return sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)


def _unit(vector):
    length = float(np.linalg.norm(vector))
    return vector / length if length > 1e-9 else None


def _vault_vectors(spec, out):
    if not spec.get("db"):
        return
    path = Path(spec["db"])
    if not path.is_file():
        return
    con = _connect_readonly(path)
    try:
        rows = con.execute(
            "SELECT c.path, v.vec FROM chunks c JOIN vecs v "
            "ON v.chunk_id=c.id ORDER BY c.path, c.seq")
        current, total, count = None, None, 0
        for rel, blob in rows:
            if rel != current:
                if current is not None and count:
                    vector = _unit(total)
                    if vector is not None:
                        out[_stable_id(
                            "note", f"{spec['id']}:{current.lower()}")] = vector
                current, total, count = rel, None, 0
            vector = np.frombuffer(blob, dtype="float16").astype("float32")
            if total is None:
                total = vector
            elif vector.shape == total.shape:
                total += vector
            else:
                continue
            count += 1
        if current is not None and count:
            vector = _unit(total)
            if vector is not None:
                out[_stable_id(
                    "note", f"{spec['id']}:{current.lower()}")] = vector
    except sqlite3.Error:
        return
    finally:
        con.close()


def _crm_vectors(out):
    if not crmindex.DB.is_file():
        return
    con = _connect_readonly(crmindex.DB)
    try:
        for pid, blob in con.execute(
                "SELECT p.pid, v.v FROM people p JOIN vecs v ON v.seq=p.seq"):
            vector = _unit(np.frombuffer(
                blob, dtype="float16").astype("float32"))
            if vector is not None:
                out[pid] = vector
    except sqlite3.Error:
        return
    finally:
        con.close()


def _raw_vectors(specs):
    if np is None:
        return {}, 0, ()
    fingerprint = _fingerprint(specs)
    with _lock:
        if _vector_cache["fingerprint"] == fingerprint:
            return (_vector_cache["vectors"],
                    _vector_cache["dimensions"], fingerprint)
    vectors = {}
    for spec in specs:
        _vault_vectors(spec, vectors)
    _crm_vectors(vectors)
    dimensions = len(next(iter(vectors.values()))) if vectors else 0
    vectors = {key: vector for key, vector in vectors.items()
               if len(vector) == dimensions}
    with _lock:
        _vector_cache.update(fingerprint=fingerprint, vectors=vectors,
                             dimensions=dimensions)
    return vectors, dimensions, fingerprint


def _hash_position(node_id, radius=WORLD_RADIUS * 1.35):
    raw = hashlib.sha256(str(node_id).encode("utf-8")).digest()
    values = [int.from_bytes(raw[i:i + 4], "big") / 0xffffffff * 2 - 1
              for i in (0, 4, 8)]
    length = sum(value * value for value in values) ** 0.5 or 1.0
    return [round(radius * value / length, 3) for value in values]


def _project(ids, matrix):
    """Deterministic randomized PCA from a normalized N x D matrix."""
    centered = matrix - matrix.mean(axis=0, keepdims=True)
    components = min(3, centered.shape[0], centered.shape[1])
    if components <= 0:
        return np.zeros((len(ids), 3), dtype="float32")
    width = min(centered.shape[1], components + PCA_OVERSAMPLE)
    rng = np.random.default_rng(20260902)
    omega = rng.standard_normal(
        (centered.shape[1], width), dtype="float32")
    q, _ = np.linalg.qr(centered @ omega, mode="reduced")
    _u, _s, vt = np.linalg.svd(q.T @ centered, full_matrices=False)
    coords = centered @ vt[:components].T
    if components < 3:
        coords = np.pad(coords, ((0, 0), (0, 3 - components)))
    # Equalized robust axes make all three semantic components navigable.
    low, high = np.percentile(coords, [2, 98], axis=0)
    middle = (low + high) / 2
    span = np.maximum((high - low) / 2, 1e-6)
    coords = np.clip((coords - middle) / span, -1.6, 1.6) * WORLD_RADIUS
    # Exact duplicate documents still need distinct pick targets.  The tiny
    # deterministic jitter is visual only and cannot move a node to another
    # semantic neighborhood.
    for index, node_id in enumerate(ids):
        jitter = _hash_position(node_id, radius=5.0)
        coords[index] += np.asarray(jitter, dtype="float32")
    return coords


def positions(nodes, edges, page_to_node):
    """Return ``(node_id -> [x,y,z], layout metadata)`` for one graph."""
    specs = vault.source_specs()
    raw, dimensions, fingerprint = _raw_vectors(specs)
    node_ids = {node["id"] for node in nodes}
    grouped = defaultdict(list)
    for raw_id, vector in raw.items():
        canonical = page_to_node.get(raw_id, raw_id)
        if canonical in node_ids:
            grouped[canonical].append(vector)
    digest = hashlib.sha1()
    for node_id in sorted(node_ids):
        digest.update(node_id.encode("utf-8"))
        digest.update(b"\0")
    for raw_id, canonical in sorted(page_to_node.items()):
        if raw_id != canonical:
            digest.update(f"{raw_id}>{canonical}".encode("utf-8"))
            digest.update(b"\0")
    key = (fingerprint, digest.hexdigest())
    with _lock:
        if _layout_cache["key"] == key:
            return (_layout_cache["positions"], _layout_cache["meta"])

    vectors = {}
    for node_id, rows in grouped.items():
        vector = _unit(np.mean(rows, axis=0)) if np is not None else None
        if vector is not None:
            vectors[node_id] = vector
    ids = sorted(vectors)
    out = {}
    if ids:
        matrix = np.vstack([vectors[node_id] for node_id in ids])
        coords = _project(ids, matrix)
        out.update({node_id: [round(float(value), 3) for value in coords[i]]
                    for i, node_id in enumerate(ids)})

    # Derived nodes such as shared tags inherit the centroid of their placed
    # neighbors.  A few passes also place any non-vector structural node
    # connected through another derived node.
    adjacency = defaultdict(list)
    for edge in edges:
        adjacency[edge.get("a")].append(edge.get("b"))
        adjacency[edge.get("b")].append(edge.get("a"))
    neighbor_placed = set()
    for _ in range(4):
        changed = False
        for node_id in sorted(node_ids - set(out)):
            rows = [out[other] for other in adjacency.get(node_id, [])
                    if other in out]
            if not rows:
                continue
            jitter = _hash_position(node_id, radius=12.0)
            out[node_id] = [round(sum(row[i] for row in rows) / len(rows)
                                  + jitter[i], 3) for i in range(3)]
            neighbor_placed.add(node_id)
            changed = True
        if not changed:
            break
    fallback = node_ids - set(out)
    for node_id in fallback:
        out[node_id] = _hash_position(node_id)

    semantic_nodes = len(ids) + len(neighbor_placed)
    meta = {
        "basis": "local-embedding-randomized-pca" if ids else
                 "deterministic-graph-fallback",
        "dimensions": dimensions,
        "vector_nodes": len(ids),
        "neighbor_nodes": len(neighbor_placed),
        "semantic_nodes": semantic_nodes,
        "fallback_nodes": len(fallback),
        "placed_nodes": len(out),
        "total_nodes": len(node_ids),
        "coverage": round(len(ids) / max(1, len(node_ids)), 4),
        "semantic_coverage": round(
            semantic_nodes / max(1, len(node_ids)), 4),
    }
    with _lock:
        # The page -> node folding is kept beside the layout so a subset can
        # find its members' vectors later without recomposing the graph.
        _layout_cache.update(key=key, positions=out, meta=meta,
                             page_to_node=dict(page_to_node))
    return out, meta


def _ball_position(node_id, radius, center=(0.0, 0.0, 0.0)):
    """A repeatable point INSIDE a ball, never on its surface.  The full
    layout's fallback puts every unplaced item on one shell, which is what
    makes an unplaced majority read as a giant hollow circle."""
    direction = _hash_position(node_id, radius=1.0)
    raw = hashlib.sha256(f"ball:{node_id}".encode("utf-8")).digest()
    depth = int.from_bytes(raw[:4], "big") / 0xffffffff
    reach = radius * (0.08 + 0.92 * depth) ** (1 / 3)
    return [round(center[i] + direction[i] * reach, 3) for i in range(3)]


def subset_vectors(ids):
    """One unit vector per subset member that has one, keyed by node id:
    the mean of its chunk vectors, with vault pages folded onto CRM people
    exactly as the last composed graph folded them."""
    wanted = {str(node_id) for node_id in ids}
    vectors = {}
    if np is None or not wanted:
        return vectors
    raw, _dimensions, _fingerprint = _raw_vectors(vault.source_specs())
    with _lock:
        page_to_node = dict(_layout_cache.get("page_to_node") or {})
    grouped = defaultdict(list)
    for raw_id, vector in raw.items():
        canonical = page_to_node.get(raw_id, raw_id)
        if canonical in wanted:
            grouped[canonical].append(vector)
    for node_id, rows in grouped.items():
        vector = _unit(np.mean(rows, axis=0))
        if vector is not None:
            vectors[node_id] = vector
    return vectors


def subset_positions(ids, edges, radius, vectors=None):
    """Coordinates for one subset of the composed World, fitted to it alone.

    The full layout's three axes are the directions that separate the whole
    vault, so a few hundred related notes share one small patch of it.  Here
    the same projection is fitted to the subset's own vectors, so its axes
    are the ones that separate these items.  Items without a vector take the
    centroid of their placed neighbours inside the subset; whatever is still
    unplaced (no vector, no path to a placed item) gathers in a small cloud
    of its own below the main one, so it is visibly "not placed by meaning"
    rather than mixed in with what was.

    `vectors` is subset_vectors(ids) when the caller already has it.
    Nothing is written.
    """
    node_ids = list(dict.fromkeys(str(node_id) for node_id in ids))
    wanted = set(node_ids)
    if vectors is None:
        vectors = subset_vectors(node_ids)
    vectors = {key: value for key, value in vectors.items() if key in wanted}

    out = {}
    placed = sorted(vectors)
    if placed:
        matrix = np.vstack([vectors[node_id] for node_id in placed])
        coords = _project(placed, matrix) * (radius / WORLD_RADIUS)
        out.update({node_id: [round(float(value), 3) for value in coords[i]]
                    for i, node_id in enumerate(placed)})

    adjacency = defaultdict(list)
    for edge in edges:
        a, b = edge.get("a"), edge.get("b")
        if a in wanted and b in wanted:
            adjacency[a].append(b)
            adjacency[b].append(a)
    neighbor_placed = set()
    # Four passes, as in the full layout: enough to reach an item two
    # derived hops from a vector without letting one vector place a chain.
    for _ in range(4):
        changed = False
        for node_id in sorted(wanted - set(out)):
            rows = [out[other] for other in adjacency.get(node_id, [])
                    if other in out]
            if not rows:
                continue
            jitter = _hash_position(node_id, radius=radius * 0.03)
            out[node_id] = [round(sum(row[i] for row in rows) / len(rows)
                                  + jitter[i], 3) for i in range(3)]
            neighbor_placed.add(node_id)
            changed = True
        if not changed:
            break

    loose = sorted(wanted - set(out))
    if loose:
        if out:
            # Its own small cloud at the main one's density, clear of its
            # lower edge (the projection clips outliers at 1.6 radii).
            loose_radius = max(radius * 0.18,
                               radius * (len(loose) / len(wanted)) ** (1 / 3))
            center = (0.0, -(radius * 1.7 + loose_radius), 0.0)
        else:
            loose_radius, center = radius, (0.0, 0.0, 0.0)
        for node_id in loose:
            out[node_id] = _ball_position(node_id, loose_radius, center)

    semantic = len(placed) + len(neighbor_placed)
    meta = {
        "basis": "subset-embedding-pca" if placed else "subset-links-only",
        "radius": round(float(radius), 3),
        "vector_nodes": len(placed),
        "neighbor_nodes": len(neighbor_placed),
        "loose_nodes": len(loose),
        "total_nodes": len(node_ids),
        "semantic_coverage": round(semantic / max(1, len(node_ids)), 4),
    }
    return {node_id: out[node_id] for node_id in node_ids}, meta

"""Subsets of the World: a slice of the galaxy, laid out on its own.

The full World holds every note, person, concept and tag the connected
sources carry.  Its one shared layout answers "where does this sit among
everything", which is the wrong question for forty notes about one subject:
they share a small patch of the ball, and the owner sees the ball.  A subset
answers the right one.  It is three things:

* A RECIPE, saved by name: narrowing steps that pick the members (search
  terms in the galaxy's own query syntax, kinds, seed items, and how many
  steps of connections to follow out from them).  The galaxy evaluates the
  recipe with the same filter code its search uses, so a saved subset means
  exactly what the panel showed when it was saved.  It stores no member
  list, so it stays current as the sources grow.
* A LAYOUT for those members alone (worldlayout.subset_positions): the
  semantic projection fitted to the subset's own vectors.
* CLUSTERS found in the subset and named from its members, served as a
  lens so the existing legend colours, counts and isolates them.  The graph
  they are found in is the subset's own links plus each member's nearest
  neighbours by meaning (the usual kNN-graph + Louvain recipe): links alone
  let one hub tag swallow everything it touches, and most notes carry only
  one or two links.

Nothing here reads or writes a source.  Store: data/world-subsets.json
(jsonstore discipline).  The owner makes these and they cannot be
regenerated, so the file is in backup.FILES.
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, worldgraph, worldlayout

STORE = Path(__file__).resolve().parent.parent / "data" / "world-subsets.json"

# The saved list is a menu the owner reads, not a database; past a couple
# of hundred entries it stops being findable, and every panel open reads it.
MAX_SUBSETS = 200
NAME_MAX = 80
QUERY_MAX = 500
# Each step narrows the previous one.  Eight is far past any recipe built by
# hand ("these notes, then these kinds, then one step out from these").
MAX_STEPS = 8
# A seed list is picked by hand or taken from one cluster.  Ten thousand ids
# keeps a saved entry under a few hundred kilobytes; a larger slice should be
# saved as a query, which also stays current.
MAX_SEEDS = 10000
# Three steps out from a seed already reaches most of a small-world graph;
# a fourth is "everything" with extra steps.
MAX_HOPS = 3
# Past this a "subset" is most of the World: its own layout tells the owner
# nothing the full one does not, and the request carries every id.
SUBSET_MAX = 25000
# Spacing so the subset's average neighbour distance sits inside the
# renderer's link rest length (110-210 world units) and repel range (180).
SPACING = 90.0
MIN_RADIUS = 220.0
# Clusters smaller than this are folded into "Other groups": a band of two
# is a pair, not a grouping worth a colour.
MIN_CLUSTER = 3
# The galaxy's palette has eleven distinct cluster colours; past that the
# legend repeats colours and stops telling clusters apart.
MAX_NAMED = 11
LABEL_MAX = 40
# Local-moving passes per Louvain level; it settles in a handful.
PASSES = 30
# Nearest neighbours by meaning per member.  Single-cell tools default to
# 15 on graphs of thousands; a few hundred notes split better on 10.
KNN = 10
# The neighbour search is an n x n similarity, done in row blocks.  Past
# 5,000 members it costs seconds of CPU per open, so a larger subset
# clusters on its links alone (still fine: it is big enough to have them).
KNN_MAX = 5000
KNN_BLOCK = 1000


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_store():
    return {"version": 1, "subsets": []}


# ---------- recipes ----------

def _clean_step(raw):
    if not isinstance(raw, dict):
        raise ValueError("each step must be an object")
    query = " ".join(str(raw.get("query") or "").split())
    if len(query) > QUERY_MAX:
        raise ValueError(f"a step's search is at most {QUERY_MAX} characters")
    kinds = _clean_kinds(raw.get("kinds"), "kinds")
    # start_kinds limits only where the step STARTS; following connections
    # out of it may reach other kinds (the galaxy's "isolate a band, then
    # + connected items").
    start_kinds = _clean_kinds(raw.get("start_kinds"), "start_kinds")
    seeds = raw.get("seeds") or []
    if not isinstance(seeds, list):
        raise ValueError("seeds must be a list")
    seeds = list(dict.fromkeys(str(seed).strip()[:160] for seed in seeds
                               if str(seed).strip()))
    if len(seeds) > MAX_SEEDS:
        raise ValueError(f"a step holds at most {MAX_SEEDS} seed items; "
                         "save a search instead")
    try:
        hops = int(raw.get("hops") or 0)
    except (TypeError, ValueError):
        raise ValueError("hops must be a whole number") from None
    if not 0 <= hops <= MAX_HOPS:
        raise ValueError(f"hops must be between 0 and {MAX_HOPS}")
    return {"query": query, "kinds": kinds, "start_kinds": start_kinds,
            "seeds": seeds, "hops": hops,
            "hide_orphans": bool(raw.get("hide_orphans"))}


def _clean_kinds(kinds, field):
    if kinds is None:
        return None
    if not isinstance(kinds, list):
        raise ValueError(f"{field} must be a list")
    kinds = sorted({str(kind).strip().lower()[:40] for kind in kinds
                    if str(kind).strip()})
    if not kinds:
        raise ValueError(f"a step that limits {field} must name at least one")
    return kinds


def _narrows(step):
    return bool(step["query"] or step["kinds"] is not None
                or step["start_kinds"] is not None or step["seeds"]
                or step["hide_orphans"])


def clean_recipe(recipe):
    """Validate and normalize a recipe; raise ValueError on anything off."""
    steps = recipe.get("steps") if isinstance(recipe, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ValueError("a subset needs at least one step")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"a subset has at most {MAX_STEPS} steps")
    clean = [_clean_step(step) for step in steps]
    if not any(_narrows(step) for step in clean):
        raise ValueError("a subset must narrow the World: add a search, "
                         "a kind, or a seed item")
    return {"steps": clean}


def _clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("a subset needs a name")
    return name[:NAME_MAX]


def _clean_stats(stats):
    if not isinstance(stats, dict):
        return None
    out = {}
    for key in ("items", "links", "clusters"):
        try:
            out[key] = max(0, int(stats.get(key) or 0))
        except (TypeError, ValueError):
            out[key] = 0
    out["at"] = _now()
    return out


# ---------- the store ----------

def list_all():
    state = jsonstore.read(STORE, _empty_store())
    rows = [row for row in state.get("subsets") or []
            if isinstance(row, dict) and row.get("id")]
    return sorted(rows, key=lambda row: str(row.get("name") or "").lower())


def create(name, recipe, stats=None):
    row = {"id": uuid.uuid4().hex[:12], "name": _clean_name(name),
           "recipe": clean_recipe(recipe), "stats": _clean_stats(stats),
           "created_at": _now(), "updated_at": _now()}

    def add(state):
        rows = state.setdefault("subsets", [])
        if len(rows) >= MAX_SUBSETS:
            raise ValueError(f"at most {MAX_SUBSETS} saved subsets; "
                             "delete one first")
        rows.append(row)

    jsonstore.mutate(STORE, add, _empty_store(), indent=2)
    return row


def update(subset_id, name=None, recipe=None, stats=None):
    changes = {}
    if name is not None:
        changes["name"] = _clean_name(name)
    if recipe is not None:
        changes["recipe"] = clean_recipe(recipe)
    if stats is not None:
        changes["stats"] = _clean_stats(stats)
    found = {}

    def edit(state):
        for row in state.get("subsets") or []:
            if row.get("id") == subset_id:
                row.update(changes)
                if "name" in changes or "recipe" in changes:
                    row["updated_at"] = _now()
                found["row"] = dict(row)
                return
        raise KeyError(subset_id)

    jsonstore.mutate(STORE, edit, _empty_store(), indent=2)
    return found["row"]


def delete(subset_id):
    def drop(state):
        rows = state.get("subsets") or []
        kept = [row for row in rows if row.get("id") != subset_id]
        if len(kept) == len(rows):
            raise KeyError(subset_id)
        state["subsets"] = kept

    jsonstore.mutate(STORE, drop, _empty_store(), indent=2)
    return list_all()


# ---------- clusters ----------

def _knn_edges(vectors):
    """Each member's KNN most similar members by cosine, as unit-weight
    edges (deterministic: ties broken by id)."""
    np = worldlayout.np
    ids = sorted(vectors)
    if np is None or not 3 <= len(ids) <= KNN_MAX:
        return []
    matrix = np.vstack([vectors[node_id] for node_id in ids]).astype("float32")
    k = min(KNN, len(ids) - 1)
    edges = set()
    for start in range(0, len(ids), KNN_BLOCK):
        sims = matrix[start:start + KNN_BLOCK] @ matrix.T
        for row, values in enumerate(sims):
            i = start + row
            values[i] = -np.inf
            nearest = np.argpartition(-values, k)[:k + 1]
            ranked = sorted((int(j) for j in nearest if int(j) != i),
                            key=lambda j: (-float(values[j]), ids[j]))[:k]
            for j in ranked:
                edges.add((min(ids[i], ids[j]), max(ids[i], ids[j])))
    return sorted(edges)


def _louvain(order, adjacency):
    """Louvain modularity communities, deterministic: nodes visit in sorted
    order and a move needs a strictly better gain, so equal gains keep the
    smaller community id.  `adjacency` is {u: {v: weight}}, symmetric, no
    self loops.  Returns {node: community representative}."""
    membership = {node: node for node in order}
    graph = {node: dict(adjacency.get(node, {})) for node in order}
    inside = {node: 0.0 for node in order}
    for _level in range(PASSES):
        nodes = sorted(graph)
        degree = {u: sum(graph[u].values()) + inside[u] for u in nodes}
        total = sum(degree.values())
        if total <= 0:
            break
        community = {u: u for u in nodes}
        tot = dict(degree)
        improved = False
        for _ in range(PASSES):
            moved = False
            for u in nodes:
                here = community[u]
                links = defaultdict(float)
                for v, weight in graph[u].items():
                    links[community[v]] += weight
                tot[here] -= degree[u]
                best = here
                best_gain = links.get(here, 0.0) - tot[here] * degree[u] / total
                for candidate in sorted(links):
                    gain = links[candidate] - tot[candidate] * degree[u] / total
                    if gain > best_gain + 1e-12:
                        best, best_gain = candidate, gain
                tot[best] += degree[u]
                if best != here:
                    community[u] = best
                    moved = improved = True
            if not moved:
                break
        if not improved:
            break
        merged = defaultdict(lambda: defaultdict(float))
        merged_inside = defaultdict(float)
        for u in nodes:
            cu = community[u]
            merged_inside[cu] += inside[u]
            for v, weight in graph[u].items():
                if community[v] == cu:
                    merged_inside[cu] += weight
                else:
                    merged[cu][community[v]] += weight
        membership = {node: community[rep] for node, rep in membership.items()}
        graph = {c: dict(merged.get(c, {})) for c in set(community.values())}
        inside = {c: merged_inside.get(c, 0.0) for c in graph}
    return membership


def _communities(ids, edges, vectors=None):
    """Communities over the subset's links plus its nearest-neighbour
    edges.  Returns (groups, linked, inner): groups of member ids, the set
    of members with any edge, and each member's edge count inside its own
    group (for naming a group after its best-connected member)."""
    order = sorted(set(ids))
    known = set(order)
    adjacency = defaultdict(lambda: defaultdict(float))
    for edge in edges:
        a, b = edge.get("a"), edge.get("b")
        if a == b or a not in known or b not in known:
            continue
        weight = min(3.0, max(0.1, float(edge.get("weight") or 1.0)))
        adjacency[a][b] += weight
        adjacency[b][a] += weight
    for a, b in _knn_edges({key: value for key, value in (vectors or {}).items()
                            if key in known}):
        adjacency[a][b] += 1.0
        adjacency[b][a] += 1.0
    membership = _louvain(order, adjacency)
    groups = defaultdict(list)
    for node_id in order:
        groups[membership[node_id]].append(node_id)
    linked = {node_id for node_id in order if adjacency.get(node_id)}
    inner = Counter()
    for node_id in order:
        for other in adjacency.get(node_id, {}):
            if membership[other] == membership[node_id]:
                inner[node_id] += 1
    return list(groups.values()), linked, inner


def _tags(node):
    tags = {str(tag).strip().lower() for tag in node.get("tags") or []
            if str(tag).strip()}
    if node.get("kind") == "topic" and node.get("name"):
        tags.add(str(node["name"]).strip().lower())
    return tags


# Page names that say where a note sits, not what it is about: a cluster
# named "Index" tells the owner nothing, so its next-best member names it.
_GENERIC_NAMES = {"index", "readme", "home", "overview", "contents", "toc",
                  "moc", "log", "notes", "untitled", "inbox"}


def _generic(name):
    return " ".join(str(name or "").lower().replace("_", " ")
                    .replace("-", " ").split()).removesuffix(".md") \
        in _GENERIC_NAMES


def _trim(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= LABEL_MAX else text[:LABEL_MAX - 1] + "…"


def _cluster_name(members, nodes, carriers, inner):
    """A tag most specific to the cluster, else its best-connected member.

    A tag scores (share of the cluster carrying it) x (share of the
    subset's carriers that sit in this cluster): high only when the tag is
    both common here and rare elsewhere, so the tag that defines the whole
    subset never names one cluster of it."""
    size = len(members)
    counts = Counter()
    for node_id in members:
        counts.update(_tags(nodes.get(node_id) or {}))
    scored = []
    for tag, count in counts.items():
        if count < 2 or count / size < 0.25:
            continue
        scored.append(((count / size) * (count / carriers[tag]), tag))
    scored.sort(key=lambda row: (-row[0], row[1]))
    if scored and scored[0][0] >= 0.2:
        tags = [scored[0][1]]
        if len(scored) > 1 and scored[1][0] >= 0.5 * scored[0][0]:
            tags.append(scored[1][1])
        return _trim(" · ".join(tags)), None
    named = [node_id for node_id in members
             if not _generic((nodes.get(node_id) or {}).get("name"))]
    hub = max(named or members, key=lambda node_id: (
        inner[node_id], float((nodes.get(node_id) or {}).get("act") or 0),
        -len(str((nodes.get(node_id) or {}).get("name") or node_id)),
        node_id))
    return _trim((nodes.get(hub) or {}).get("name") or hub), hub


def cluster_lens(ids, nodes, edges, vectors=None):
    """The Clusters lens for one subset, in the World lens shape."""
    groups, linked, inner = _communities(ids, edges, vectors)
    carriers = Counter()
    for node_id in ids:
        carriers.update(_tags(nodes.get(node_id) or {}))
    ranked = sorted((group for group in groups if len(group) >= MIN_CLUSTER),
                    key=lambda group: (-len(group), group[0]))
    bands, node_band = [], {}
    for i, group in enumerate(ranked[:MAX_NAMED], start=1):
        label, hub = _cluster_name(group, nodes, carriers, inner)
        band = {"id": f"cluster:{i}", "label": label, "size": len(group)}
        if hub:
            band["hub"] = hub
        bands.append(band)
        for node_id in group:
            node_band[node_id] = band["id"]
    other = [node_id for node_id in ids
             if node_id not in node_band and node_id in linked]
    loose = [node_id for node_id in ids
             if node_id not in node_band and node_id not in linked]
    if other:
        bands.append({"id": "cluster:other", "label": "Other groups",
                      "size": len(other), "muted": True})
        node_band.update({node_id: "cluster:other" for node_id in other})
    if loose:
        bands.append({"id": "cluster:loose", "label": "Unlinked",
                      "size": len(loose), "muted": True})
        node_band.update({node_id: "cluster:loose" for node_id in loose})
    return {"id": "subset-clusters", "label": "Clusters",
            "blurb": "Groups found in this subset by its links and by "
                     "meaning, named from their tags or best-connected item.",
            "total": len(ids), "placed": len(ids), "bands": bands,
            "node_band": node_band, "editable": False}


# ---------- layout ----------

def radius_for(count):
    return max(MIN_RADIUS, SPACING * max(1, count) ** (1 / 3))


def layout(ids):
    """Positions and clusters for one subset of the current World graph."""
    ids = list(dict.fromkeys(str(node_id) for node_id in ids or []
                             if str(node_id).strip()))
    if not ids:
        return {"status": "empty", "count": 0}
    if len(ids) > SUBSET_MAX:
        return {"status": "too_large", "count": len(ids),
                "limit": SUBSET_MAX}
    graph = worldgraph.current()
    wanted = set(ids)
    nodes = {node["id"]: node for node in graph.get("nodes") or []
             if node.get("id") in wanted}
    edges = [edge for edge in graph.get("edges") or []
             if edge.get("a") in wanted and edge.get("b") in wanted]
    vectors = worldlayout.subset_vectors(ids)
    positions, meta = worldlayout.subset_positions(
        ids, edges, radius_for(len(ids)), vectors)
    meta["unknown_nodes"] = len(wanted) - len(nodes)
    return {"status": "ok", "generated": graph.get("generated"),
            "count": len(ids), "links": len(edges), "positions": positions,
            "layout": meta, "lens": cluster_lens(ids, nodes, edges, vectors)}

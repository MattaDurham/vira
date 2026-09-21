"""Shared hybrid-retrieval primitives: FTS query building, bm25 and
cosine ranking, reciprocal-rank fusion, the float16 blob codec, and a
staleness-cached matrix loader.

One implementation for every retrieval stack on the vira side — the
media index (search.py + mediaindex.py) consumes these directly. The
qocha vault engine (qocha/vault.py) carries a line-for-line parallel of
each primitive: RRF_K, VaultIndex._fts_queries, _rank_fts, _rank_vec,
_vec_matrix, and the float16 pack/unpack in embed_pending. This module
is shaped so a later lift moves qocha onto the same core mechanically:

  fts_queries   <- qocha VaultIndex._fts_queries (identical)
  rank_fts      <- qocha VaultIndex._rank_fts (table + limit are
                   parameters here; qocha hardcodes chunks_fts/200)
  rank_vec      <- qocha VaultIndex._rank_vec (qocha has no candidate
                   filter or seen-dedupe; both are optional here)
  rrf           <- the fusion loop in qocha VaultIndex.search
  pack_vec /
  unpack_vec /
  stack_vecs    <- the float16 blob codec (qocha embed_pending /
                   _vec_matrix)
  MatrixCache   <- qocha VaultIndex._vec_state/_vec_matrix (generation
                   -based there; count+time staleness here)

Everything is deterministic and model-free: callers supply query
vectors and sqlite handles.
"""
import re
import concurrent.futures
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field, replace
import threading
import time

try:
    import numpy as np
except ImportError:  # minimal install: FTS-only retrieval still works
    np = None

RRF_K = 60



@dataclass(frozen=True)
class ReadPolicy:
    """A request may narrow configured access; it never grants access."""
    for_model: bool = False
    source_ids: tuple | None = None
    path_prefixes: tuple = ()
    corpus_ids: tuple | None = None
    message_sources: tuple | None = None

    def __post_init__(self):
        if self.source_ids is not None:
            object.__setattr__(self, "source_ids", tuple(self.source_ids))
        object.__setattr__(self, "path_prefixes", tuple(self.path_prefixes))
        for key in ("corpus_ids", "message_sources"):
            if getattr(self, key) is not None:
                object.__setattr__(self, key, tuple(getattr(self, key)))


def policy_from_sources(sources=(), *, for_model=True):
    """Translate the immutable chat scope into retrieval restrictions."""
    if isinstance(sources, dict):
        if "sources" not in sources or not isinstance(sources["sources"], (list, tuple)):
            raise ValueError("scope.sources must be a list")
        sources = sources["sources"]
    if sources is None or not isinstance(sources, (list, tuple)) or any(not isinstance(x, str) for x in sources):
        raise ValueError("sources must be a list of source IDs")
    sources = tuple(sources)
    if not sources:
        return ReadPolicy(for_model=for_model)
    vaults = tuple(x.split(":", 1)[1] for x in sources if x.startswith("vault:"))
    corpora = tuple(k for k, selected in (("notes", bool(vaults)),
        ("messages", "imessage" in sources or "mail" in sources),
        ("media", "media" in sources), ("people", "people" in sources)) if selected)
    messages = tuple(x for x, key in (("imessage", "imessage"), ("email", "mail")) if key in sources)
    return ReadPolicy(for_model=for_model, source_ids=vaults,
                      corpus_ids=corpora, message_sources=messages)


@contextmanager
def source_scope(sources=(), *, for_model=True, deadline=None, semantic=False):
    with request_scope(request_for(policy=policy_from_sources(sources, for_model=for_model),
                                  deadline=deadline, semantic=semantic)) as request:
        yield request


@dataclass(frozen=True)
class Request:
    policy: ReadPolicy = field(default_factory=ReadPolicy)
    deadline: float | None = None
    semantic: bool = False
    allow_cold_models: bool = False


_REQUEST = ContextVar("retrieval_request", default=None)
DEFAULT_BUDGET_S = 5.0
_MAX_WORKERS = 8
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS,
                                              thread_name_prefix="vira-retrieval")
_SLOTS = threading.BoundedSemaphore(_MAX_WORKERS)


def current_request():
    return _REQUEST.get()


def allows_corpus(name):
    request = current_request()
    return request is None or request.policy.corpus_ids is None or name in request.policy.corpus_ids


@contextmanager
def request_scope(request):
    token = _REQUEST.set(request)
    try:
        yield request
    finally:
        _REQUEST.reset(token)


def request_for(policy=None, deadline=None, semantic=None, for_model=False):
    previous = current_request()
    inherited = previous.policy if previous else ReadPolicy()
    if policy is None:
        policy = inherited
    elif previous:
        # Nested scopes can only narrow explicit source/path restrictions.
        ids = policy.source_ids
        if inherited.source_ids is not None:
            ids = tuple(x for x in inherited.source_ids if ids is None or x in ids)
        prefixes = policy.path_prefixes or inherited.path_prefixes
        if inherited.path_prefixes and policy.path_prefixes:
            prefixes = tuple(x for x in policy.path_prefixes
                             if any(x == p or x.startswith(p.rstrip("/") + "/")
                                    for p in inherited.path_prefixes))
            if not prefixes:
                prefixes = ("__no_access__",)
        narrow = {}
        for key in ("corpus_ids", "message_sources"):
            parent, chosen = getattr(inherited, key), getattr(policy, key)
            narrow[key] = tuple(x for x in parent if chosen is None or x in chosen) if parent is not None else chosen
        policy = replace(policy, source_ids=ids, path_prefixes=prefixes, **narrow)
    policy = replace(policy, for_model=bool(for_model or policy.for_model
                                           or inherited.for_model))
    if previous and previous.deadline is not None:
        deadline = min(deadline, previous.deadline) if deadline is not None else previous.deadline
    return Request(policy=policy, deadline=deadline,
                   semantic=previous.semantic if semantic is None and previous else bool(semantic),
                   allow_cold_models=previous.allow_cold_models if previous else False)


def remaining(deadline=None, maximum=None):
    req = current_request()
    if deadline is None and req:
        deadline = req.deadline
    left = max(0.0, deadline - time.monotonic()) if deadline is not None else None
    return min(left, maximum) if left is not None and maximum is not None else (left if left is not None else maximum)


def validate_date_window(since=None, until=None):
    """Date filters are day boundaries; malformed input must not widen scope."""
    from datetime import date
    values = []
    for label, value in (("since", since), ("until", until)):
        if value is None or value == "":
            values.append(None)
            continue
        try:
            values.append(date.fromisoformat(str(value)).isoformat())
        except ValueError as exc:
            raise ValueError(f"{label} requires an ISO date (YYYY-MM-DD)") from exc
    if all(values) and values[0] >= values[1]:
        raise ValueError("until must be after since (exclusive end date)")
    return tuple(values)


def check_deadline(deadline=None):
    if remaining(deadline) == 0:
        raise TimeoutError("retrieval deadline exceeded")


def configure_connection(con, deadline=None):
    """Bound SQLite lock waits and long queries by the request deadline."""
    left = remaining(deadline)
    if left is not None:
        check_deadline(deadline)
        end = time.monotonic() + left
        con.execute("PRAGMA busy_timeout=%d" % max(1, int(left * 1000)))
        con.set_progress_handler(lambda: int(time.monotonic() >= end), 1000)
    return con


def submit_bounded(fn, *args, **kwargs):
    """No unbounded queue or per-request threads; context follows the call.

    A timed-out operation retains its slot until it really exits. Repeated
    timeouts therefore cannot create unlimited surviving workers.
    """
    slots = _SLOTS
    if not slots.acquire(blocking=False):
        return None
    context = copy_context()
    try:
        future = _POOL.submit(context.run, fn, *args, **kwargs)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return future


class Hits(list):
    """List-compatible legacy results with honest coverage for new callers."""
    def __init__(self, rows=(), *, coverage=(), total=None, next_cursor=None):
        super().__init__(rows)
        self.coverage = list(coverage)
        self.total = total
        self.next_cursor = next_cursor


def candidate_clause(con, cand, column="rowid"):
    if cand is None:
        return ""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS retrieval_candidates(id INTEGER PRIMARY KEY)")
    con.execute("DELETE FROM retrieval_candidates")
    con.executemany("INSERT OR IGNORE INTO retrieval_candidates(id) VALUES(?)",
                    ((int(n),) for n in cand))
    return f" AND {column} IN (SELECT id FROM retrieval_candidates)"


def matching_fts(con, q, cand=None, table="fts", phrases=()):
    """All lexical matches, suitable for SQL ordering/counting afterwards."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("invalid FTS table")
    candidates = candidate_clause(con, cand)
    qs = fts_queries(q, phrases)
    if phrases:
        qs = [" AND ".join('"' + str(p).replace('"', '""') + '"' for p in phrases)]
    out = set()
    for fq in qs:
        check_deadline()
        out.update(r[0] for r in con.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH ?" + candidates,
            (fq,)))
    return out

def _require_np():
    if np is None:
        raise ImportError("numpy is required for vector retrieval")


# ---------- FTS ----------

def fts_queries(q, phrases=()):
    """User text -> ranked fts5 queries: quoted phrases first (a phrase
    the user typed in quotes is the one thing they are sure of), then
    all-terms (precise hits dominate), then any-term (recall for
    conversational phrasing)."""
    out = []
    for p in phrases:
        terms = re.findall(r"[A-Za-z0-9']+", p)
        if terms:                       # one quoted phrase = one fts5 phrase
            out.append('"' + " ".join(terms) + '"')
    terms = re.findall(r"[A-Za-z0-9']+", q)
    if not terms:
        return out
    all_q = " ".join(f'"{t}"' for t in terms)
    any_q = " OR ".join(f'"{t}"' for t in terms)
    out += [all_q, any_q] if len(terms) > 1 else [any_q]
    return out


def rank_fts(con, q, cand, limit, table="fts", phrases=()):
    """Apply candidates in SQL BEFORE relevance ranking and LIMIT."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("invalid FTS table")
    candidates = candidate_clause(con, cand)
    out, seen = [], set()
    queries = fts_queries(q, phrases)
    if phrases:
        queries = [" AND ".join('"' + str(p).replace('"', '""') + '"' for p in phrases)]
    for fq in queries:
        check_deadline()
        rows = con.execute(
            f"SELECT rowid, bm25({table}) AS r FROM {table} "
            f"WHERE {table} MATCH ?" + candidates + " ORDER BY r, rowid LIMIT ?",
            (fq, limit)).fetchall()
        for r in rows:
            rid = r[0]
            if rid not in seen:
                seen.add(rid)
                out.append(rid)
        if len(out) >= limit:
            break
    return out[:limit]


# ---------- vectors ----------

def pack_vec(v):
    """Vector -> the on-disk blob format (float16, half the bytes; the
    precision loss is far below embedding noise)."""
    _require_np()
    return v.astype("float16").tobytes()


def unpack_vec(blob):
    """On-disk blob -> float32 vector (float32 for fast matmul)."""
    _require_np()
    return np.frombuffer(blob, dtype="float16").astype("float32")


def stack_vecs(blobs):
    """Blobs -> one float32 matrix, row per vector."""
    _require_np()
    return np.stack([unpack_vec(b) for b in blobs])


def unit(v):
    """L2-normalized copy of a vector (epsilon-guarded)."""
    _require_np()
    return v / (np.linalg.norm(v) + 1e-9)


def unit_rows(M):
    """L2-normalized copy of a matrix, row-wise (epsilon-guarded)."""
    _require_np()
    return M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)


def rank_vec(space, qvec, cand, floor, limit=400):
    """Cosine top-k over an in-memory matrix: space is (ids, matrix) or
    None. Descending similarity, stopping at floor; duplicates (chunked
    ids) and rows outside cand (None = unfiltered) are dropped."""
    if np is None or space is None or qvec is None:
        return []
    ids, M = space
    sims = M @ qvec
    order = np.argsort(-sims)
    out, seen = [], set()
    for i in order:
        if sims[i] < floor:
            break
        s = int(ids[i])
        if s in seen or (cand is not None and s not in cand):
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def best_match(G, v):
    """Argmax cosine of v against gallery rows G -> (row_index, score).
    Callers normalize (unit / unit_rows) when cosine is intended."""
    _require_np()
    sims = G @ v
    i = int(sims.argmax())
    return i, float(sims[i])


# ---------- fusion ----------

def rrf(lists, k=RRF_K):
    """Reciprocal-rank fusion: ranked id lists -> {id: fused score}.
    Ties keep first-list-first insertion order under a stable sort."""
    ranks = {}
    for lst in lists:
        for r, item in enumerate(lst):
            ranks[item] = ranks.get(item, 0.0) + 1.0 / (k + r)
    return ranks


# ---------- matrix cache ----------

class MatrixCache:
    """(ids, float32 matrix) per vector space, loaded from sqlite blob
    tables and refreshed lazily: a load within max_age seconds of the
    last is free, and past it the matrices rebuild only when the row
    count actually changed — a stable index never pays the rebuild.

    spaces: {name: (count_sql, rows_sql)} where rows_sql yields rows
    with the id first and the blob last. The FIRST space is the
    sentinel: its presence marks the cache as loaded (mirroring the
    original search.py behavior where an empty primary table meant a
    reload every call).
    """

    def __init__(self, spaces, max_age=60.0):
        self.spaces = dict(spaces)
        self.max_age = max_age
        self._state = {name: None for name in self.spaces}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def get(self, name):
        """The (ids, matrix) tuple for a space, or None."""
        return self._state.get(name)

    def invalidate(self):
        self._loaded_at = 0.0

    def load(self, con_factory, force=False):
        if np is None:  # minimal install: no vector spaces
            return
        names = list(self.spaces)
        first = names[0]
        with self._lock:
            if not force and self._state[first] is not None and \
                    time.time() - self._loaded_at < self.max_age:
                return
            con = con_factory()
            if not force and self._state[first] is not None:
                counts = {n: con.execute(self.spaces[n][0]).fetchone()[0]
                          for n in names}
                if all(self._state[n] is not None
                       and counts[n] == len(self._state[n][0])
                       for n in names):
                    self._loaded_at = time.time()
                    con.close()
                    return
            for n in names:
                rows = con.execute(self.spaces[n][1]).fetchall()
                if rows:
                    ids = np.array([r[0] for r in rows], dtype=np.int64)
                    self._state[n] = (ids, stack_vecs([r[-1] for r in rows]))
            con.close()
            self._loaded_at = time.time()

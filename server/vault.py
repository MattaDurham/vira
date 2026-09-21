"""TC-IL vault index + grounded ask — now a thin adapter over qocha.

The engine that lived here was extracted 2026-07-20 into the standalone
qocha package (pip-installed editable from ~/workspace/qocha; see that
repo's README): heading-path chunking, FTS5 + local-embedding hybrid
search with RRF fusion, citation-validated ask, the sqlite sidecar
schema — all unchanged, so the existing data/vault-index.sqlite keeps
working with no re-index. This module keeps Vira's public surface and
seams exactly as they were:

  - config comes from settings (vault_root / vault_dirs), re-read on
    every access so a config.json edit takes effect without a restart
  - embeddings route through localmodels.ollama_embed (one Ollama
    client for the whole app, and the tests' mock seam)
  - ask() answers through suggest.complete (the backend ladder +
    aihealth accounting, and the tests' mock seam)
  - module-level DB_PATH / _vec_state / _connect / _init stay
    patchable — tests and atlas._vault_edges depend on them

Everything else delegates to a lazily (re)built qocha.Vault.
"""

from . import modulemodels
import hashlib
import base64
import json
import sqlite3
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

from qocha import Config as _QochaConfig, Vault as _QochaVault
from qocha.chunker import (CHUNK_MAX, CHUNK_TARGET,  # noqa: F401 — re-export
                           chunk_markdown)

from . import settings, retrieval

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "vault-index.sqlite"

VAULT_RESCAN_S = 300
DEFAULT_DIRS = ["wiki", "Briefs", "Sessions", "retros", "brain-retros"]
SOURCE_PREFIX = "@"
_MODEL_ACCESS = ContextVar("vault_model_access", default=False)


@contextmanager
def model_access():
    """Apply model exposure restrictions to an entire retrieval call chain."""
    token = _MODEL_ACCESS.set(True)
    try:
        yield
    finally:
        _MODEL_ACCESS.reset(token)


def model_path_allowed(path):
    """Path-level model permission, including aliases of excluded folders."""
    from . import vaultwrite
    raw = str(path or "")
    if raw.startswith("@"):
        sid, sep, rel = raw[1:].partition("/")
        if not sep:
            return False
    else:
        sid, rel = "primary", raw
    spec = next((s for s in source_specs() if s["id"] == sid), None)
    if not spec or not spec.get("read_enabled") or not spec.get("model_exposure"):
        return False
    if not spec["root"].is_dir():
        return False
    try:
        rel = vaultwrite.relative_path(rel)
        actual = (spec["root"] / rel).resolve().relative_to(spec["root"].resolve()).as_posix()
    except (OSError, ValueError):
        return False
    return _path_allowed(spec, rel, _read_policy(True))


def _model_hits(hits):
    return [h for h in hits if model_path_allowed(h.get("path"))]

SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# shared with the active qocha.Vault so tests can reset the cache in place
_vec_state = {"gen": -1, "ids": None, "mat": None}
_extra_vec_states = {}


def vault_root() -> Path:
    raw = str(settings.get("vault_root") or "").strip()
    # Unset must resolve to a path that never exists — Path("") is the cwd,
    # which would silently index the repo itself. Every consumer treats a
    # missing root as dormant, so a never-created sentinel keeps them all off.
    return (Path(raw).expanduser() if raw
            else Path.home() / ".vira" / "vault-unset")


def vault_dirs():
    return list(settings.get("vault_dirs") or DEFAULT_DIRS)


def exclude_names():
    """Directory names the owner never wants indexed, at any depth.

    A listed dir is walked whole (qocha rglobs it), so this is the only way
    to take a tree and skip one branch of it - `raw/` carries 1,007 loose
    full transcripts that exist nowhere else AND `raw/instagram`, whose
    clippings are already carried in full by their wiki notes.
    """
    return {str(n).strip() for n in (settings.get("vault_exclude_dirs") or [])
            if str(n).strip()}


def _source_id(value, root):
    """Stable, URL/path-safe source id for a configured vault."""
    raw = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    if SOURCE_ID_RE.fullmatch(raw or ""):
        return raw
    base = raw[:36].strip("-") or "vault"
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _relative_inside(path, root):
    """Return the confined relative path, recognizing filesystem aliases.

    resolve() follows links but does not canonicalize capitalization on a
    case-insensitive volume, and Windows can retain its extended-path prefix
    while resolving a concurrently created child. Compare ancestor identities
    after the lexical fast path so aliases retain the same confinement policy.
    Missing child folders remain valid when their existing ancestor is root.
    """
    path, root = Path(path).resolve(), Path(root).resolve()
    try:
        return path.relative_to(root)
    except ValueError:
        for ancestor in (path, *path.parents):
            try:
                if ancestor.samefile(root):
                    return path.relative_to(ancestor)
            except OSError:
                continue
        raise ValueError("path is outside the vault") from None


def _inside(path, root):
    try:
        _relative_inside(path, root)
        return True
    except (OSError, ValueError):
        return False


def _overlaps(a, b):
    return _inside(a, b) or _inside(b, a)


def source_specs():
    """Every connected markdown source, primary first.

    Each source carries independent local-read, write and model policies.
    `vault_root` retains its stable primary identity and unprefixed citations.
    An old `vault_dirs` entry that points outside the primary root is promoted
    in memory to an extra source; this migrates the pre-feature workaround
    without indexing the same files twice or requiring a config rewrite.
    """
    from .vaultwrite import policy
    primary_policy = settings.get("vault_primary") or {}
    primary_root = Path(vault_root()).expanduser()
    raw_dirs = vault_dirs()
    primary_dirs, legacy = [], []
    for item in raw_dirs:
        candidate = (primary_root / str(item)).expanduser()
        if _inside(candidate, primary_root):
            rel = _relative_inside(candidate, primary_root)
            primary_dirs.append(rel.as_posix())
        else:
            legacy.append(candidate.resolve())

    specs = [{
        "id": "primary", "name": primary_root.name or "Primary vault",
        "root": primary_root, "dirs": primary_dirs, "primary": True,
        "db": Path(DB_PATH),
        **policy(primary_policy, primary=True),
    }]
    specs[0]["name"] = str(primary_policy.get("name") or specs[0]["name"])

    configured = settings.get("vault_sources") or []
    rows = list(configured) if isinstance(configured, list) else []
    rows += [{"name": p.name, "root": str(p), "legacy": True}
             for p in legacy]
    connected_roots = [primary_root.resolve()]
    used_ids = {"primary"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_root = str(row.get("root") or "").strip()
        if not raw_root:
            continue
        root = Path(raw_root).expanduser().resolve()
        key = str(root)
        if any(_overlaps(root, connected) for connected in connected_roots):
            continue
        connected_roots.append(root)
        sid = _source_id(row.get("id") or row.get("name") or root.name, root)
        if sid in used_ids:
            sid = _source_id(f"{sid}-{hashlib.sha1(key.encode()).hexdigest()[:6]}",
                             root)
        used_ids.add(sid)
        configured_dirs = row.get("dirs")
        if isinstance(configured_dirs, list):
            dirs = []
            for item in configured_dirs:
                candidate = (root / str(item)).expanduser()
                if _inside(candidate, root):
                    rel = _relative_inside(candidate, root)
                    dirs.append(rel.as_posix())
        else:
            dirs = None
        specs.append({
            "id": sid, "name": str(row.get("name") or root.name or sid),
            "root": root, "dirs": dirs, "primary": False,
            "legacy": bool(row.get("legacy")),
            **policy(row),
            "db": Path(DB_PATH).parent / "vault-indexes" / f"{sid}.sqlite",
        })
    # A configured capture folder must be discoverable even when the older
    # index scope covered only the wiki. Never widen a read-only source.
    for spec in specs:
        if spec["write_enabled"] and spec["dirs"] is not None:
            capture_dirs = [spec["capture_dir"]]
            if spec["primary"] and not spec["policy_explicit"]:
                capture_dirs.append("plans")
            spec["dirs"] = list(dict.fromkeys(spec["dirs"] + capture_dirs))
    return specs


class _ViraEmbedder:
    """qocha embedder protocol over Vira's shared Ollama client."""

    def embed_documents(self, texts):
        from . import localmodels
        return localmodels.ollama_embed(
            [f"search_document: {t}"[:6000] for t in texts])

    def embed_query(self, text):
        from . import localmodels
        vecs = localmodels.ollama_embed([f"search_query: {text}"[:6000]])
        return vecs[0] if vecs else None


@modulemodels.scoped("find")
def _answer(prompt):
    from . import suggest
    return suggest.complete(prompt)


_active = {"key": None, "vault": None, "rows": []}
_build_lock = threading.Lock()


def _vault_rows():
    """[{spec, vault}], rebuilt when any connected source changes."""
    specs = [s for s in source_specs() if s.get("read_enabled")]
    key = (tuple((s["id"], str(s["root"]), tuple(s["dirs"] or ()),
                  str(s["db"]), s["name"], s["model_exposure"],
                  tuple(s["protected_dirs"]), tuple(s["model_exclude_dirs"])) for s in specs),
           str(settings.get("owner_name") or ""))
    with _build_lock:
        if _active["key"] != key:
            built = []
            for spec in specs:
                cfg = _QochaConfig(
                    root=spec["root"], dirs=spec["dirs"], db=spec["db"],
                    owner=settings.get("owner_name") or "the owner")
                # Owner exclusions are ADDED to qocha's own defaults, never
                # substituted for them: dropping .obsidian/.git/.venv is the
                # engine's business, and a config key that silently turned
                # those back on would be a footgun. Applies to every
                # connected source, primary and secondary alike.
                cfg.exclude_dirs = set(cfg.exclude_dirs) | exclude_names()
                v = _QochaVault(cfg.root, config=cfg,
                                embedder=_ViraEmbedder(), answerer=_answer)
                if spec["primary"]:
                    v._vec_state = _vec_state      # public test/atlas seam
                else:
                    v._vec_state = _extra_vec_states.setdefault(
                        spec["id"], {"gen": -1, "ids": None, "mat": None})
                built.append({"spec": spec, "vault": v})
            _active.update(key=key,
                           vault=built[0]["vault"] if built else None,
                           rows=built)
        return _active["rows"]


def _vault() -> _QochaVault:
    """The primary vault (or first connected source when primary is absent)."""
    rows = _vault_rows()
    if not rows:
        raise RuntimeError("no vault configured")
    return rows[0]["vault"]


def _public_path(spec, rel):
    rel = str(rel).replace("\\", "/").lstrip("/")
    return rel if spec["primary"] else f"{SOURCE_PREFIX}{spec['id']}/{rel}"


def _source_path(path):
    """(row, vault-relative path) for one public source-aware path."""
    raw = str(path or "").strip().replace("\\", "/")
    rows = _vault_rows()
    if raw.startswith(SOURCE_PREFIX):
        head, sep, rel = raw[1:].partition("/")
        if not sep or not rel:
            raise ValueError("invalid vault path")
        row = next((r for r in rows if r["spec"]["id"] == head), None)
        if row is None:
            raise ValueError("unknown vault source")
        return row, rel
    if not rows:
        raise ValueError("no vault configured")
    primary = next((r for r in rows if r["spec"]["primary"]), None)
    if primary is None:
        raise ValueError("primary vault reading is disabled")
    return primary, raw


def _hit(row, hit):
    out = dict(hit)
    spec = row["spec"]
    out["path"] = _public_path(spec, hit.get("path") or "")
    out["vault_id"] = spec["id"]
    out["vault_name"] = spec["name"]
    return out


# ---------- the public surface (unchanged) ----------

def scan_once():
    total = {"changed": 0, "removed": 0, "seen": 0, "vaults": []}
    for row in _vault_rows():
        spec = row["spec"]
        try:
            result = row["vault"].scan()
        except Exception as exc:  # one disconnected disk never blocks others
            result = {"error": str(exc)[:200]}
        total["vaults"].append({"id": spec["id"], "name": spec["name"],
                                **result})
        for key in ("changed", "removed", "seen"):
            total[key] += int(result.get(key) or 0)
    return total


def embed_pending(limit=2000):
    total = 0
    left = max(0, int(limit))
    for row in _vault_rows():
        if left <= 0:
            break
        done = int(row["vault"].embed_pending(limit=left) or 0)
        total += done
        left -= done
    return total


def _read_policy(for_model=False, policy=None):
    return retrieval.request_for(policy=policy,
        for_model=bool(for_model or _MODEL_ACCESS.get())).policy


def _path_allowed(spec, rel, policy):
    if policy.corpus_ids is not None and "notes" not in policy.corpus_ids:
        return False
    if not spec.get("read_enabled", True):
        return False
    if policy.source_ids is not None and spec["id"] not in policy.source_ids:
        return False
    if policy.for_model and not spec.get("model_exposure"):
        return False
    public = _public_path(spec, rel)
    if policy.path_prefixes and not any(public == p or public.startswith(p.rstrip("/") + "/")
                                       for p in policy.path_prefixes):
        return False
    root = spec["root"].resolve()
    try:
        actual = (root / rel).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return False
    if policy.for_model:
        from .vaultwrite import _under
        if any(_under(p, folder, protected=True)
               for p in (rel, actual) for folder in spec.get("model_exclude_dirs", ())):
            return False
    return True


def _query_cursor(query_key, generation, offset):
    return base64.urlsafe_b64encode(json.dumps(
        {"q": query_key, "g": generation, "o": offset}, separators=(",", ":")
    ).encode("utf-8")).decode("ascii")


def query_notes(query="", limit=20, cursor=None, mode="search", since=None,
                until=None, order="relevance", date_field="modified", policy=None,
                deadline=None, semantic=False, phrases=(), literal=False):
    """Policy-scoped notes with explicit count, pagination and coverage.

    Dates are file modification times, never claimed to be event dates.
    Literal enumeration/count is exhaustive over the indexed snapshot; semantic
    search is ranked and is never represented as an exhaustive event census.
    """
    since, until = retrieval.validate_date_window(since, until)
    if mode not in ("search", "enumerate", "count"):
        raise ValueError("mode must be search, enumerate, or count")
    if date_field != "modified":
        raise ValueError("notes support date_field='modified'; event dates require source evidence")
    if order not in ("relevance", "recent", "oldest"):
        raise ValueError("invalid order")
    request = retrieval.request_for(policy=_read_policy(policy=policy), deadline=deadline,
                                     semantic=semantic)
    if request.deadline is None:
        request = retrieval.Request(request.policy, time.monotonic() + retrieval.DEFAULT_BUDGET_S,
                                    request.semantic, request.allow_cold_models)
    limit = max(1, min(int(limit), 2000))
    q = str(query or "").strip()
    lo, hi = _epoch(since), _epoch(until)
    signature = hashlib.sha256(json.dumps([q, mode, since, until, order, date_field,
        request.policy.for_model, request.policy.source_ids, request.policy.path_prefixes,
        semantic, list(phrases), literal], sort_keys=True).encode("utf-8")).hexdigest()
    requested_offset = 0
    if cursor:
        try:
            requested_offset = int(json.loads(base64.urlsafe_b64decode(str(cursor)).decode("utf-8"))["o"])
            if requested_offset < 0:
                raise ValueError("negative cursor")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid cursor") from exc
    hits, coverage, generations = [], [], []
    with retrieval.request_scope(request):
        for row in _vault_rows():
            if request.policy.corpus_ids is not None and "notes" not in request.policy.corpus_ids:
                break
            spec = row["spec"]
            if request.policy.source_ids is not None and spec["id"] not in request.policy.source_ids:
                continue
            if request.policy.for_model and not spec.get("model_exposure"):
                continue
            started = time.monotonic()
            state = {"source_id": spec["id"], "status": "complete", "mode": "fts",
                     "date_field": "modified"}
            con = None
            try:
                retrieval.check_deadline()
                if not spec["root"].is_dir() or not Path(spec["db"]).exists():
                    state.update(status="unavailable", error="source or index unavailable")
                    coverage.append(state)
                    continue
                con = sqlite3.connect(Path(spec["db"]).resolve().as_uri() + "?mode=ro",
                                      uri=True, timeout=min(0.2, retrieval.remaining(maximum=0.2)))
                con.row_factory = sqlite3.Row
                retrieval.configure_connection(con)
                meta = dict(con.execute("SELECT k,v FROM meta"))
                state["last_scan"] = meta.get("last_scan")
                generations.append([spec["id"], meta.get("gen"), meta.get("last_scan"),
                                    spec.get("model_exposure"), spec.get("model_exclude_dirs", [])])
                where, params = [], []
                if lo is not None:
                    where.append("n.mtime>=?")
                    params.append(lo)
                if hi is not None:
                    where.append("n.mtime<?")
                    params.append(hi)
                notes = {r["path"]: dict(r) for r in con.execute(
                    "SELECT n.path,n.title,n.mtime,n.size FROM notes n" +
                    (" WHERE " + " AND ".join(where) if where else ""), params)}
                # Restrict allowed paths before chunk ranking/limit. This also
                # checks resolved paths so an excluded folder cannot be aliased.
                allowed = set()
                for i, path in enumerate(notes):
                    if i % 100 == 0:
                        retrieval.check_deadline()
                    if _path_allowed(spec, path, request.policy):
                        allowed.add(path)
                con.execute("CREATE TEMP TABLE allowed_notes(path TEXT PRIMARY KEY)")
                con.executemany("INSERT INTO allowed_notes VALUES(?)", ((p,) for p in allowed))
                chunks = {r["id"]: dict(r) for r in con.execute(
                    "SELECT c.id,c.path,c.seq,c.heading FROM chunks c JOIN allowed_notes a ON a.path=c.path")}
                cand = set(chunks)
                if literal and q:
                    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = "%" + escaped + "%"
                    ids = {r[0] for r in con.execute(
                        "SELECT c.id FROM chunks c JOIN allowed_notes a ON a.path=c.path "
                        "JOIN notes n ON n.path=c.path WHERE c.text LIKE ? ESCAPE '\\' "
                        "OR n.title LIKE ? ESCAPE '\\' OR c.path LIKE ? ESCAPE '\\'",
                        (pattern, pattern, pattern))}
                    ordered = sorted(ids)
                elif q:
                    ids = retrieval.matching_fts(con, q, cand, table="chunks_fts", phrases=phrases)
                    ordered = retrieval.rank_fts(con, q, ids, max(1, len(ids)),
                                                table="chunks_fts", phrases=phrases)
                else:
                    ordered = sorted(cand)
                    ids = cand
                scores = retrieval.rrf([ordered]) if q else {}
                # Semantic augmentation is opt-in and shares one bounded query
                # embedding. Candidate and access masks precede vector ranking.
                if semantic and q and mode == "search" and order == "relevance" and not literal:
                    from . import localmodels
                    vector = localmodels.query_embedding(q, deadline=request.deadline)
                    if vector is not None:
                        vectors = con.execute("SELECT chunk_id,vec FROM vecs").fetchall()
                        if vectors:
                            matrix = retrieval.stack_vecs([r["vec"] for r in vectors])
                            space = (retrieval.np.array([r["chunk_id"] for r in vectors]), matrix)
                            sem = retrieval.rank_vec(space, vector, cand, 0.35,
                                                     limit=max(limit * 4, 200))
                            scores = retrieval.rrf([ordered, sem])
                            ordered = sorted(scores, key=scores.get, reverse=True)
                            state["mode"] = "hybrid"
                    else:
                        state.update(status="partial", semantic_status="unavailable",
                                     error="semantic augmentation unavailable within deadline; lexical matches retained")
                seen = set()
                for cid in ordered:
                    c = chunks[cid]
                    if c["path"] in seen:
                        continue
                    seen.add(c["path"])
                    note = notes[c["path"]]
                    hits.append(_hit(row, {"path": c["path"], "title": note["title"],
                        "heading": c["heading"] or "", "text": "",
                        "mtime": note["mtime"], "date_field": "modified",
                        "size": note["size"], "score": scores.get(cid),
                        "chunk_id": cid, "chunk_seq": c["seq"], "literal": literal,
                        "_db": str(spec["db"]),
                        "index_generation": meta.get("gen"), "last_scan": meta.get("last_scan")}))
                state["matched_notes"] = len(seen)
            except (TimeoutError, sqlite3.OperationalError) as exc:
                status = "timed_out" if isinstance(exc, TimeoutError) or retrieval.remaining() == 0 else "unavailable"
                state.update(status=status, error=str(exc) or status)
            except (OSError, ValueError) as exc:
                state.update(status="unavailable", error=str(exc))
            finally:
                if con is not None:
                    con.close()
            state["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            coverage.append(state)
    if order in ("recent", "oldest") or not q:
        hits.sort(key=lambda h: (h.get("mtime") or 0, h["path"]), reverse=order != "oldest")
    else:
        hits.sort(key=lambda h: (-(h.get("score") or 0), h["path"]))
    generation = hashlib.sha256(json.dumps(generations, sort_keys=True).encode("utf-8")).hexdigest()
    offset = 0
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(str(cursor)).decode("utf-8"))
            if decoded["q"] != signature or decoded["g"] != generation:
                raise ValueError("query or index changed; restart pagination")
            offset = int(decoded["o"])
            if offset < 0:
                raise ValueError("invalid cursor")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid or stale cursor; restart pagination") from exc
    complete = all(c["status"] == "complete" for c in coverage)
    page = [] if mode == "count" else hits[offset:offset + limit]
    for hit in page:
        try:
            retrieval.check_deadline(request.deadline)
            con = sqlite3.connect(Path(hit["_db"]).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
            try:
                retrieval.configure_connection(con, request.deadline)
                found = con.execute("SELECT text FROM chunks WHERE id=?", (hit["chunk_id"],)).fetchone()
                hit["text"] = found[0] if found else ""
            finally:
                con.close()
        except (TimeoutError, sqlite3.Error) as exc:
            complete = False
            coverage.append({"source_id": hit.get("vault_id"), "status": "partial", "error": "source excerpt unavailable: " + str(exc)})
    for hit in hits:
        hit.pop("_db", None)
    next_cursor = _query_cursor(signature, generation, offset + limit) if complete and mode != "count" and offset + limit < len(hits) else None
    return {"rows": page, "hits": page, "total": len(hits), "total_exact": complete and not semantic,
            "next_cursor": next_cursor, "complete": complete, "coverage": coverage,
            "date_field": date_field, "match_mode": "literal" if literal else "lexical",
            "scope": "indexed notes", "mode": mode}


def search(q, limit=10, for_model=False, policy=None, deadline=None, semantic=None):
    request = retrieval.current_request()
    semantic = request.semantic if semantic is None and request else bool(semantic)
    out = query_notes(q, limit=limit, policy=_read_policy(for_model, policy),
                      deadline=deadline, semantic=semantic)
    return retrieval.Hits(out["hits"], coverage=out["coverage"], total=out["total"], next_cursor=out["next_cursor"])


def search_filtered(q, limit=10, since=None, until=None, order="relevance",
                    for_model=False, policy=None, deadline=None, phrases=()):
    request = retrieval.current_request()
    out = query_notes(q, limit=limit, since=since, until=until, order=order,
        policy=_read_policy(for_model, policy), deadline=deadline,
        semantic=request.semantic if request else False, phrases=phrases)
    return retrieval.Hits(out["hits"], coverage=out["coverage"], total=out["total"], next_cursor=out["next_cursor"])


def grep_notes(text, limit=None, since=None, until=None, order="recent",
               for_model=False, policy=None, deadline=None):
    # Legacy callers get a list; new agents use query_notes with a cursor.
    out = query_notes(text, limit=limit or 2000, since=since, until=until, order=order,
        mode="enumerate", literal=True, policy=_read_policy(for_model, policy), deadline=deadline)
    return retrieval.Hits(out["hits"], coverage=out["coverage"], total=out["total"], next_cursor=out["next_cursor"])


def _epoch(iso):
    """ISO date -> local-midnight unix seconds (mtime's own units)."""
    if not iso:
        return None
    try:
        return datetime.combine(date.fromisoformat(str(iso)[:10]),
                                dtime.min).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------ the ask budget
# What qocha's own ask() can actually CARRY, read out of its source rather
# than guessed: it renders each hit at up to 2,400 characters and truncates
# the joined block at 60,000. Those two are not ours to raise -- qocha is a
# separate package with its own release ritual -- but they ARE the ceiling
# this module has to respect, because a hit retrieved past them is searched
# for, paid for, and then dropped from the prompt with nothing said.
ENGINE_CHUNK_CHARS = 2_400
ENGINE_PROMPT_CHARS = 60_000

# A PASSAGE COSTS MORE THAN ITS TEXT.  qocha renders each hit under a header
# line (`--- CHUNK n | path | heading`) and joins the blocks with a blank
# line, so counting only the 2,400 characters of text over-retrieves and
# hands the engine material it truncates in silence -- the exact failure
# ask_hits() exists to prevent, one layer along.  Measured over the real
# 39,756-chunk index: median 141, p99 236, max 324.  The reserve is the
# MAX rather than the median on purpose, because the two errors are not
# symmetric: being conservative costs one passage, being generous costs a
# passage that was searched for, paid for, and then dropped with nothing
# said.  A vault with longer paths than this one would still overrun, by
# at most one passage.
ENGINE_BLOCK_OVERHEAD = 324


def ask_hits(kind="standard"):
    """How many passages to retrieve for one grounded answer.

    WAS a bare `k=10` default on ask() below -- typed once, never revisited,
    and the exact twin of find.ASK_LIMIT (8, which left the right note at
    rank 34 while the model answered confidently from the wrong ones). It
    was never measured against a window, and it could not fail loudly: a cap
    that is too small yields confident output from thin material.

    TWO CEILINGS, AND THE SMALLER WINS. modelbudget says what the answering
    backend can hold, so switching backends in Config re-sizes this instead
    of leaving a literal describing a machine nobody re-measured; the engine
    constants above say what the prompt downstream can carry. Retrieving
    past the second would spend the search and hand qocha material it
    silently truncates, which is the same defect one layer along.
    """
    from . import modelbudget
    room = min(modelbudget.context_chars(kind), ENGINE_PROMPT_CHARS)
    return max(1, int(room // (ENGINE_CHUNK_CHARS + ENGINE_BLOCK_OVERHEAD)))


@modulemodels.scoped("find")
def ask(question, k=None, hits=None):
    """Grounded answer over the vault.

    `k` is the retrieval width used when the caller has not already narrowed
    the corpus itself. None asks ask_hits() rather than carrying a number
    here; a caller that means a specific width still passes one.
    """
    if k is None:
        k = ask_hits()
    merged = search(question, limit=k, for_model=True) if hits is None else hits
    merged = [hit for hit in merged if model_path_allowed(hit.get("path"))]
    return _vault().ask(question, k=k, hits=merged)


def note_text(path, cap=None, for_model=False):
    """Uncapped by default -- the Reader serves a note whole.

    `cap` is for context-window callers and truncates HONESTLY (the
    engine appends an in-band marker). See qocha's note_text docstring.
    """
    row, rel = _source_path(path)
    if not _path_allowed(row["spec"], rel, _read_policy(for_model)):
        raise ValueError("model access is disabled for this vault or folder")
    return row["vault"].note_text(rel, cap=cap)


def asset_path(path):
    """Resolve a source-aware asset path without ever leaving its vault."""
    try:
        row, rel = _source_path(path)
    except ValueError:
        return None
    root = row["spec"]["root"].resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.is_file() and target.suffix.lower() != ".md" else None


def primary_path(path):
    """Whether a public path belongs to the primary/write vault."""
    try:
        row, _ = _source_path(path)
    except ValueError:
        return False
    return bool(row["spec"]["primary"])


# ------------------------------------------------------- wikilink resolution
# A `[[wikilink]]` is a FILENAME identifier, not a search query. Obsidian
# resolves it by exact stem across the whole vault; resolving it through the
# ranked hybrid search instead was measured wrong on 27% of the links in this
# vault that point at notes which genuinely exist — `[[claude]]` opened
# `types-of-claude-interfaces`, `[[supra]]` opened a consultation transcript.
# A wrong note presented as the right one is worse than an honest miss, so
# exact match answers first and search is only ever a labelled fallback.

_stem_cache = {"key": None, "map": None, "root": None, "at": 0.0}
_extra_stem_caches = {}

# The cache KEY is a filesystem walk, so computing it to decide whether to
# rebuild cost as much as rebuilding — measured on the real vault, 1.4s of
# rglob per call before assets were indexed and 5.3s after. `resolve_ref` is
# called once per link and an index page carries thousands, so the walk is
# gated behind a short clock: a burst of links pays for one walk, and an edit
# is still picked up within a few seconds without any explicit invalidation.
_STEM_TTL = 5.0

# Never resolvable, because Obsidian does not resolve them either: dotfolders
# (.git, .obsidian, .smart-env, and any agent worktree checked out INSIDE the
# vault), plus the soft-delete staging area, which the rest of the system
# already treats as gone. Leaving these in is not merely untidy — `sorted()`
# puts a dotfolder ahead of `wiki/`, so a stale worktree won every stem
# collision and `[[supra]]` opened a months-old copy of the real note.
SKIP_DIRS = ("pending-user-deletion",)
# On a genuine tie, the curated layer wins. Anything unlisted sorts last.
DIR_RANK = ("wiki", "", "Sessions", "Briefs", "retros", "brain-retros")


def _visible(root, pattern="*.md"):
    for p in root.rglob(pattern):
        if pattern != "*.md" and not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        yield rel, p


def _visible_notes(root):
    return _visible(root, "*.md")


def _visible_assets(root):
    """Every non-markdown file. `![[chart.png]]` is a wikilink too, and it was
    never in the stem map — measured 2026-08-11, all 15,143 asset embeds in
    the vault fell through to the search fallback, so an image ref answered
    with an unrelated NOTE at `exact: False`. Assets resolve by FULL filename
    (extension included), which is how Obsidian addresses them."""
    for rel, p in _visible(root, "*"):
        if rel.suffix.lower() != ".md":
            yield rel, p


def _rank(rel):
    top = rel.parts[0] if len(rel.parts) > 1 else ""
    try:
        return (DIR_RANK.index(top), len(rel.parts), rel.as_posix())
    except ValueError:
        return (len(DIR_RANK), len(rel.parts), rel.as_posix())


def _best(a, b):
    """The better of two candidates for the same key, or None-safe passthrough.

    Ranked comparison on (directory, depth) only. The third element of `_rank`
    is a lexical path tie-break, which must NOT decide a case collision — with
    it, a real `NASA.md` beside a real `nasa.md` resolves both refs to `NASA`
    because uppercase sorts first. Equal rank falls through to the caller's
    case-exact preference instead.
    """
    if a is None or b is None:
        return a or b
    return a if _rank(a)[:2] <= _rank(b)[:2] else b


def _stem_map(row=None):
    """{'exact': {stem: rel}, 'lower': {stem.lower(): rel}, 'assets': {...}}.

    Two maps, not one. The single map this replaced wrote both `p.stem` and
    `p.stem.lower()` with `setdefault` over rank-sorted notes, so a best-ranked
    file claimed only its own casing and a worst-ranked file could still claim
    the still-free case-exact key: `wiki/anthropic.md` took `anthropic`, then
    `raw/Anthropic.md` took `Anthropic`. `resolve_ref` asked for the case-exact
    key FIRST, so DIR_RANK was bypassed rather than outranked and 223 links
    opened a 0-byte stub. Keeping the two keyspaces apart lets the lookup
    compare ranks across them instead of racing them.
    """
    if row is None:
        rows = _vault_rows()
        row = rows[0] if rows else None
    root = row["spec"]["root"] if row else Path(vault_root())
    cache = (_stem_cache if not row or row["spec"]["primary"] else
             _extra_stem_caches.setdefault(
                 row["spec"]["id"],
                 {"key": None, "map": None, "root": None, "at": 0.0}))
    # Setting `key` to None is still the explicit invalidation, so a test or a
    # caller that knows the vault changed can force a rebuild.
    if (cache["key"] is not None
            and cache["root"] == str(root)
            and time.monotonic() - cache["at"] < _STEM_TTL):
        return cache["map"]
    if not root.exists():
        return {"exact": {}, "lower": {}, "names": {}, "assets": {},
                "assets_lower": {}}
    notes = list(_visible_notes(root))
    assets = list(_visible_assets(root))
    key = (str(root), len(notes), len(assets),
           max((p.stat().st_mtime_ns for _, p in notes), default=0),
           max((p.stat().st_mtime_ns for _, p in assets), default=0))
    cache["root"], cache["at"] = str(root), time.monotonic()
    if cache["key"] == key:
        return cache["map"]
    m = {"exact": {}, "lower": {}, "names": {}, "assets": {}, "assets_lower": {}}
    for rel, _ in notes:
        # Best-ranked writer wins per key, so resolution is stable and a
        # duplicate stem elsewhere can never silently re-point existing links.
        m["exact"][rel.stem] = _best(m["exact"].get(rel.stem), rel)
        low = rel.stem.lower()
        m["lower"][low] = _best(m["lower"].get(low), rel)
        # Full filename, case-sensitive. An author who typed the extension
        # said more than one who did not, and `_clean_ref` throws it away:
        # `[[CLAUDE.md]]` (101 links, meaning the vault's spec file at the
        # root) would otherwise rank-lose to `wiki/claude.md`, since the two
        # are structurally identical to the `raw/Anthropic.md` shadowing this
        # fix exists to kill. An exact filename hit is not a guess.
        m["names"][rel.name] = _best(m["names"].get(rel.name), rel)
    for rel, _ in assets:
        m["assets"][rel.name] = _best(m["assets"].get(rel.name), rel)
        low = rel.name.lower()
        m["assets_lower"][low] = _best(m["assets_lower"].get(low), rel)
    cache["key"], cache["map"] = key, m
    return m


def _clean_ref(ref, keep_ext=False):
    """Strip the parts of a wikilink that are not the note identity.

    `keep_ext` leaves a typed `.md` on, for the caller that wants to try the
    filename verbatim before falling back to stem matching.
    """
    r = (ref or "").strip()
    r = r.split("|", 1)[0].strip()          # [[note|Label]]
    r = re.split(r"[#^]", r, maxsplit=1)[0].strip()   # [[note#h]], [[note^b]]
    if not keep_ext and r.lower().endswith(".md"):
        r = r[:-3]
    return r.strip("/ ")


def _resolve_ref_one(row, ref, qualified=False):
    """{path, exact} for a wikilink, or None.

    `exact` False means this came from the search fallback and the caller
    should say so rather than present it as the linked note.
    """
    raw_ref = _clean_ref(ref, keep_ext=True)
    r = _clean_ref(ref)
    if not r:
        return None
    root = row["spec"]["root"]
    # Qualified file paths never need a whole-vault stem/asset walk.
    if qualified or "/" in raw_ref:
        for cand in ((root / raw_ref), Path(str(root / raw_ref) + ".md")):
            if not cand.is_file():
                continue
            try:
                direct = cand.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                return None
            if not _path_allowed(row["spec"], direct, _read_policy()):
                return None
            result = {"path": _public_path(row["spec"], direct), "exact": True}
            if not row["spec"]["primary"]:
                result.update(vault_id=row["spec"]["id"], vault_name=row["spec"]["name"])
            return result
        return None
    m = _stem_map(row)
    rel = None
    if "/" not in raw_ref and raw_ref != r:
        # An explicitly-typed `.md`, matched case-sensitively on the whole
        # filename. Only an exact hit counts — anything looser is the guess
        # the ranked path below is there to make.
        rel = m["names"].get(raw_ref)
    if rel is None and "/" in r:
        # Path-qualified. Try the literal path BEFORE forcing `.md` onto it —
        # `.with_suffix()` turns `wiki/assets/x.png` into `wiki/assets/x.md`,
        # so every path-qualified asset embed missed and fell to search.
        # `.md` is APPENDED, never `with_suffix`: pathlib reads the last
        # dotted run as the extension, so `Claude 3.5 Sonnet for sparking
        # creativity` becomes `Claude 3.md` and the note is unreachable.
        # 34 links in this vault have a dot mid-filename.
        for cand in ((root / r), Path(str(root / r) + ".md")):
            if not cand.is_file():
                continue
            try:                            # `..` must never escape the vault
                rel = cand.resolve().relative_to(root.resolve())
            except ValueError:
                return None
            break
    if rel is None:
        # Rank decides across the two keyspaces; case-exactness is only the
        # tie-break, so a real `NASA.md`/`nasa.md` pair still resolves by case
        # while a worst-ranked stub can no longer shadow the curated note.
        exact, lower = m["exact"].get(r), m["lower"].get(r.lower())
        if exact is not None and lower is not None:
            rel = exact if _rank(exact)[:2] <= _rank(lower)[:2] else lower
        else:
            rel = exact if exact is not None else lower
    if rel is None:
        rel = m["assets"].get(r) or m["assets_lower"].get(r.lower())
    if rel is not None:
        hit = {"path": _public_path(row["spec"], rel.as_posix()),
               "exact": True}
        if not row["spec"]["primary"]:
            hit.update(vault_id=row["spec"]["id"],
                       vault_name=row["spec"]["name"])
        return hit
    return None


def resolve_ref(ref, from_path=None):
    """Resolve within the current note's source first, then every other.

    A source context prevents `[[index]]` in a secondary vault from opening
    the primary vault's same-named note. Without context, the primary vault
    keeps the historical precedence.
    """
    if not _clean_ref(ref):
        return None
    policy = _read_policy()
    if policy.corpus_ids is not None and "notes" not in policy.corpus_ids:
        return None
    rows = [r for r in _vault_rows() if (not policy.for_model or r["spec"]["model_exposure"])
            and (policy.source_ids is None or r["spec"]["id"] in policy.source_ids)]
    if str(ref).startswith("@"):
        try:
            row, rel = _source_path(_clean_ref(ref, keep_ext=True))
        except ValueError:
            return None
        return _resolve_ref_one(row, rel, qualified=True) if row in rows else None
    if from_path:
        try:
            context, _ = _source_path(from_path)
            if context in rows:
                rows = [context] + [r for r in rows if r is not context]
        except ValueError:
            pass
    for row in rows:
        if not row["spec"]["root"].is_dir():
            continue
        hit = _resolve_ref_one(row, ref)
        if hit is not None and (not policy.for_model or model_path_allowed(hit["path"])):
            return hit
    found = search(_clean_ref(ref), limit=1) or []
    if found:
        return {"path": found[0]["path"], "exact": False,
                "vault_id": found[0].get("vault_id", "primary"),
                "vault_name": found[0].get("vault_name", "Primary vault")}
    return None


def known_stems():
    """Every resolvable name, so a client can dim unresolved links without a
    round-trip per link — an index page carries thousands.

    Names only, never paths: the client strips any directory off a
    path-qualified ref before checking, so `[[wiki/anthropic|Anthropic]]` is
    tested as `anthropic`. Sending full paths instead would multiply the
    payload for a set the client would still have to normalise.
    """
    # Read straight off the resolver's own index rather than re-walking, so
    # this list cannot drift from what `resolve_ref` will actually accept —
    # the client dims links with it, and a list that disagreed would dim
    # links the server resolves fine and light up links it will refuse.
    # Assets are in here for that reason too: `![[chart.png]]` resolves.
    names = set()
    for row in _vault_rows():
        if not row["spec"]["root"].exists():
            continue
        m = _stem_map(row)
        names.update(m["exact"])
        names.update(m["assets"])
    return sorted(names)


def status():
    vaults = []
    for row in _vault_rows():
        spec = row["spec"]
        try:
            state = row["vault"].status()
        except Exception as exc:
            state = {"root": str(spec["root"]), "db": str(spec["db"]),
                     "notes": 0, "chunks": 0, "vectors": 0,
                     "last_scan": None, "available": False,
                     "error": str(exc)[:200]}
        vaults.append({**state, "id": spec["id"], "name": spec["name"],
                       "primary": spec["primary"],
                       "legacy": bool(spec.get("legacy"))})
    first = vaults[0] if vaults else {}
    return {
        "root": first.get("root", str(vault_root())),
        "db": first.get("db", str(DB_PATH)),
        "notes": sum(int(v.get("notes") or 0) for v in vaults),
        "chunks": sum(int(v.get("chunks") or 0) for v in vaults),
        "vectors": sum(int(v.get("vectors") or 0) for v in vaults),
        "last_scan": max((str(v.get("last_scan") or "") for v in vaults),
                         default="") or None,
        "available": any(v.get("available") for v in vaults),
        "vaults": vaults,
    }


def person_notes(name, limit=6):
    """Vault notes that mention a person — the person-page seam."""
    name = (name or "").strip()
    if not name:
        return []
    hits = search(name, limit=24)
    by_path, order = {}, []
    for h in hits:
        if h["path"] not in by_path:
            by_path[h["path"]] = h
            order.append(h["path"])
    return [{"path": p, "title": by_path[p]["title"],
             "vault_id": by_path[p].get("vault_id", "primary"),
             "vault_name": by_path[p].get("vault_name"),
             "heading": by_path[p]["heading"],
             "snippet": by_path[p]["text"][:280]}
            for p in order[:limit]]


def _connect():
    """Primary raw index connection (atlas's co-mention signal is local)."""
    return _vault()._connect()


def _init(con):
    _QochaVault._init(con)


class VaultIndexer(threading.Thread):
    """Background maintainer: incremental rescan + vector fill. Dormant
    (cheap no-op ticks) when the vault root does not exist."""

    def __init__(self):
        super().__init__(daemon=True, name="vira-vault-indexer")
        self._stop = threading.Event()

    def run(self):
        time.sleep(5)                    # let the server finish booting
        while not self._stop.is_set():
            try:
                scan_once()
                embed_pending()
            except Exception:  # noqa: BLE001 — the indexer never dies
                pass
            self._stop.wait(VAULT_RESCAN_S)

    def stop(self):
        self._stop.set()

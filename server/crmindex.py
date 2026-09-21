"""The CRM as a searchable database, not just a lookup table.

`data.search_people()` is a lowercase substring scan over name, emails
and phones, so it answers an exactly-spelled name and nothing else:
1,006 people carrying relationship summaries, how-we-met stories,
topics, open loops and personal facts, all unreachable by any other
route. "who works in insurance" returned nothing, ever.

So the CRM gets the same treatment as every other corpus here: a sqlite
sidecar with FTS5 over the whole record plus one embedding per person,
fused with RRF through the shared primitives in retrieval.py. Nothing
about the shape is new — this is mediaindex's pattern at 1/13th the
size.

Two deliberate choices:

  - FTS is built synchronously and vectors fill in behind it. The blob
    for 1,006 people indexes in well under a second; embedding them
    through Ollama does not, and a search must never wait on a model.
    Rows carry `pending` until their vector lands (see embed_pending).
  - Freshness is a source fingerprint, not a watcher. The CRM files
    change when a background enrichment or a profile save rewrites
    them, so the cheap check is people.json + master.json + the
    profiles directory: mtimes and counts, compared on read.
"""
import json
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from . import data as crm
from . import retrieval, settings

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "crm-index.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  pid TEXT UNIQUE, name TEXT, text TEXT, pending INTEGER DEFAULT 1);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, text);
CREATE TABLE IF NOT EXISTS vecs(seq INTEGER PRIMARY KEY, v BLOB);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, val TEXT);
"""

_matrices = retrieval.MatrixCache({"text": ("SELECT COUNT(*) FROM vecs",
                                            "SELECT seq, v FROM vecs")})
_refreshed_at = 0.0
REFRESH_S = 120


def _con():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def available():
    return DB.exists()


def invalidate():
    global _refreshed_at
    _refreshed_at = 0.0
    _matrices.invalidate()


# ---------- the searchable blob ----------

def _stamp():
    """Source fingerprint: cheap to compute, changes whenever any CRM
    file a person's text is built from changes."""
    root = settings.crm_root()
    parts = []
    for name in ("people.json", "master.json"):
        f = root / name
        parts.append(f"{name}:{f.stat().st_mtime if f.exists() else 0}")
    pdir = root / "profiles"
    if pdir.exists():
        files = sorted(pdir.glob("*.json"))
        newest = max((f.stat().st_mtime for f in files), default=0)
        parts.append(f"profiles:{len(files)}:{newest}")
    return "|".join(parts)


def _person_text(p, master, profile):
    """Everything about one person that is worth matching on, as one
    blob: identity, handles, the master card, and the profile's prose."""
    bits = [p.get("name") or ""]
    bits += (p.get("refs", {}) or {}).get("card_names", []) or []
    h = p.get("handles", {}) or {}
    bits += h.get("emails", []) + h.get("phones10", []) + h.get("imessage", [])
    if master:
        bits += [master.get(k) or "" for k in
                 ("full_name", "company", "title", "relationship",
                  "evidence")]
    if profile:
        bits += [profile.get(k) or "" for k in
                 ("relationship_class", "relationship_summary", "how_we_met",
                  "comms_style")]
        for t in profile.get("topics") or []:
            bits += [t.get("topic") or "", t.get("quote") or ""]
        for loop in (profile.get("open_loops") or []) + \
                (profile.get("resolved_loops") or []):
            bits += [loop.get("what") or "", loop.get("how") or ""]
        for fact in profile.get("personal_facts") or []:
            bits.append(fact.get("fact") or "")
        for hook in profile.get("hooks") or []:
            bits += [hook.get("angle") or "", hook.get("detail") or ""]
    return " \n".join(b for b in bits if b)[:8000]


def refresh(force=False, log=lambda *a: None):
    """Rebuild the index when the CRM files moved underneath it. Full
    rebuild, not incremental: at this size it costs milliseconds and
    cannot drift."""
    global _refreshed_at
    if not force and time.time() - _refreshed_at < REFRESH_S:
        return {"rebuilt": False}
    stamp = _stamp()
    con = _con()
    try:
        row = con.execute("SELECT val FROM state WHERE key='stamp'").fetchone()
        if not force and row and row["val"] == stamp:
            _refreshed_at = time.time()
            return {"rebuilt": False}

        c = crm._load()
        profiles = c["profiles"]
        rows = []
        for p in c["people"]:
            pid = p["id"]
            rows.append((pid, p.get("name") or "",
                         _person_text(p, c["master"].get(pid),
                                      profiles.get(pid))))
        # keep the vectors of people whose text did not change: embedding
        # is the only expensive part of this whole module
        old = {r["pid"]: (r["seq"], r["text"]) for r in
               con.execute("SELECT seq, pid, text FROM people")}
        # `pending` means "still needs a vector", so it is answered by the
        # vecs table, NOT by whether the text changed. Deriving it from
        # sameness alone marked every unchanged-but-never-embedded row as
        # done, and a rebuild that landed mid-fill stranded the rest
        # lexical-only forever.
        embedded = {r[0] for r in con.execute("SELECT seq FROM vecs")}
        con.execute("DELETE FROM people")
        con.execute("DELETE FROM fts")
        keep = []
        for pid, name, text in rows:
            prev = old.get(pid)
            same = prev and prev[1] == text
            seq = prev[0] if same else None
            cur = con.execute(
                "INSERT INTO people(seq, pid, name, text, pending)"
                " VALUES(?,?,?,?,?)",
                (seq, pid, name, text,
                 0 if (same and seq in embedded) else 1))
            seq = seq or cur.lastrowid
            con.execute("INSERT INTO fts(rowid, name, text) VALUES(?,?,?)",
                        (seq, name, text))
            if same:
                keep.append(seq)
        if keep:
            con.execute("DELETE FROM vecs WHERE seq NOT IN (%s)"
                        % ",".join("?" * len(keep)), keep)
        else:                      # NOT IN (NULL) matches nothing in sql,
            con.execute("DELETE FROM vecs")     # so empty means empty here
        con.execute("INSERT OR REPLACE INTO state(key,val) VALUES('stamp',?)",
                    (stamp,))
        con.execute(
            "INSERT OR REPLACE INTO state(key,val) VALUES('built_at',?)",
            (str(time.time()),))
        con.commit()
        log(f"crm index: {len(rows)} people, {len(rows) - len(keep)} changed")
    finally:
        con.close()
    _refreshed_at = time.time()
    _matrices.invalidate()
    return {"rebuilt": True, "people": len(rows)}


def embed_pending(limit=128, log=lambda *a: None):
    """Fill vectors for rows FTS is already serving. Runs in the
    background tick; a search never waits for it."""
    from . import localmodels
    con = _con()
    try:
        rows = con.execute(
            "SELECT seq, text FROM people WHERE pending=1 LIMIT ?",
            (limit,)).fetchall()
        if not rows:
            return 0
        vecs = localmodels.ollama_embed(
            [f"search_document: {r['text'][:6000]}" for r in rows])
        if not vecs:
            return 0
        for r, v in zip(rows, vecs):
            con.execute("INSERT OR REPLACE INTO vecs(seq, v) VALUES(?,?)",
                        (r["seq"], retrieval.pack_vec(v)))
            con.execute("UPDATE people SET pending=0 WHERE seq=?", (r["seq"],))
        con.commit()
        _matrices.invalidate()
        log(f"crm index: embedded {len(rows)}")
        return len(rows)
    finally:
        con.close()


def _qvec(q):
    from . import localmodels
    return localmodels.query_embedding(q)


def _clear_qvec_cache():
    from .localmodels import clear_query_cache
    clear_query_cache()


_qvec.cache_clear = _clear_qvec_cache


# ---------- search ----------

def search(q=None, limit=20, exact=False, person=None, order="relevance",
           phrases=(), semantic=None, deadline=None):
    previous = retrieval.current_request()
    request = retrieval.request_for(deadline=deadline,
        semantic=(previous.semantic if previous else True) if semantic is None else semantic)
    if request.policy.corpus_ids is not None and "people" not in request.policy.corpus_ids:
        return retrieval.Hits()
    coverage = {"source_id": "people", "status": "complete", "mode": "fts", "date_field": "last_contact"}
    q = (q or "").strip()
    if not q:
        people = list(crm._load()["people"])
        if person:
            people = [p for p in people if p["id"] == person]
        people.sort(key=lambda p: (crm._last_contact(p), p["id"]), reverse=order != "oldest")
        return retrieval.Hits([_row(p, None) for p in people[:limit]], coverage=[coverage], total=len(people))
    if previous is None:
        refresh()
    if not DB.exists():
        return retrieval.Hits(coverage=[dict(coverage, status="unavailable", error="people index unavailable")])
    con = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    con.row_factory = sqlite3.Row
    with retrieval.request_scope(request):
        try:
            retrieval.configure_connection(con)
            cand = {r[0] for r in con.execute("SELECT seq FROM people WHERE pid=?", (person,))} if person else None
            ids = retrieval.matching_fts(con, q, cand, phrases=phrases)
            fts = retrieval.rank_fts(con, q, ids, max(1, len(ids)), phrases=phrases)
            lists = [fts]
            if request.semantic and not exact and order == "relevance":
                _matrices.load(_con)
                qv = _qvec(q)
                if qv is not None:
                    lists.append(retrieval.rank_vec(_matrices.get("text"), qv, cand, floor=0.45))
                    coverage["mode"] = "hybrid"
                else:
                    coverage.update(status="partial", error="semantic layer unavailable; lexical matches retained")
            ranks = retrieval.rrf(lists)
            top = sorted(ranks, key=ranks.get, reverse=True)
            clause = retrieval.candidate_clause(con, top, "seq")
            got = {r["seq"]: r for r in con.execute("SELECT seq,pid,text FROM people WHERE 1" + clause)}
            coverage["built_at"] = (con.execute("SELECT val FROM state WHERE key='built_at'").fetchone() or [None])[0]
        finally:
            con.close()
    c = crm._load()
    if order in ("recent", "oldest"):
        top.sort(key=lambda seq: (crm._last_contact(c["by_id"].get(got[seq]["pid"], {})), seq),
                 reverse=order == "recent")
    out = []
    for seq in top[:limit]:
        r = got.get(seq)
        p = c["by_id"].get(r["pid"]) if r else None
        if p:
            out.append(_row(p, _snippet(r["text"], q), ranks.get(seq)))
    return retrieval.Hits(out, coverage=[coverage], total=len(top) if not request.semantic else None)


def _row(p, snippet, score=None):
    row = crm.person_summary(p, crm._load()["profiles"])
    row["snippet"] = snippet
    row["score"] = round(score, 5) if score else None
    return row


def _snippet(text, q):
    """The first line that actually contains a query word — a match the
    reader can see beats a truncated preamble."""
    words = [w.lower() for w in q.split() if len(w) > 2]
    for line in (text or "").split("\n"):
        low = line.lower()
        if any(w in low for w in words) and len(line.strip()) > 12:
            return line.strip()[:240]
    return (text or "").split("\n")[0][:240]


def status():
    if not DB.exists():
        return {"available": False, "people": 0, "vectors": 0}
    con = _con()
    try:
        return {
            "available": True,
            "people": con.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "vectors": con.execute("SELECT COUNT(*) FROM vecs").fetchone()[0],
            "pending": con.execute(
                "SELECT COUNT(*) FROM people WHERE pending=1").fetchone()[0],
            "built_at": (con.execute(
                "SELECT val FROM state WHERE key='built_at'").fetchone()
                or [None])[0],
        }
    finally:
        con.close()


if __name__ == "__main__":       # python -m server.crmindex [embed]
    import sys
    print(json.dumps(refresh(force=True, log=print), indent=1))
    if "embed" in sys.argv:
        while embed_pending(log=print):
            pass
    print(json.dumps(status(), indent=1))

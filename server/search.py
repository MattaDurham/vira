"""Hybrid search over the media index: exact text (FTS5/bm25), scene
similarity (SigLIP), text similarity (nomic), face identity, and
deterministic filters (person, sender, kind, direction, date) — fused
with reciprocal-rank fusion so any layer can carry a query the others
miss.

Vector matrices live in memory (float32, ~40MB at full corpus) and
refresh lazily when the index grows. Query latency budget: <300ms
warm; the first scene query pays SigLIP model load (~5s).

ask(question) is the conversational wrapper: Claude (the existing CLI
path in suggest.py) parses the question into a structured plan, the
plan runs deterministically, and when the strict query comes back empty
the constraints relax one at a time — wrong-memory questions ("didn't
X send me…" when it was actually Y) get a near-miss answer
instead of a bare no.
"""

from . import modulemodels
import json
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from . import data as crm
from . import mediaindex
from . import retrieval

RRF_K = retrieval.RRF_K
FTS_LIMIT = 400

# vector spaces: (count_sql, rows_sql) — id first, blob last
_matrices = retrieval.MatrixCache({
    "scene": ("SELECT COUNT(*) FROM vec_scene",
              "SELECT seq, v FROM vec_scene"),
    "text": ("SELECT COUNT(*) FROM vec_text",
             "SELECT seq, chunk, v FROM vec_text"),
})


def _con():
    con = sqlite3.connect(mediaindex.DB, timeout=min(0.2, retrieval.remaining(maximum=0.2)))
    con.row_factory = sqlite3.Row
    return con


def invalidate():
    _matrices.invalidate()


def _scene_qvec(q):
    from .localmodels import query_scene_embedding
    return query_scene_embedding(q)


def _text_qvec(q):
    from .localmodels import query_embedding
    return query_embedding(q)


def _candidates(con, pid=None, sender_pid=None, kind=None, direction=None,
                face_pid=None, since=None, until=None, source=None):
    """Seq set passing the deterministic filters; None = unfiltered."""
    where, params = [], []
    if pid:
        where.append("(i.chat_pid=? OR i.sender_pid=?)")
        params += [pid, pid]
    if sender_pid:
        where.append("i.sender_pid=?")
        params.append(sender_pid)
    if kind:
        kinds = kind if isinstance(kind, (list, tuple)) else [kind]
        where.append(f"i.kind IN ({','.join('?' * len(kinds))})")
        params += list(kinds)
    if direction == "received":
        where.append("i.from_me=0")
    elif direction == "sent":
        where.append("i.from_me=1")
    if since:
        where.append("i.date_ns >= ?")
        params.append(since)
    if source:
        where.append("i.source=?")
        params.append(source)
    if until:
        where.append("i.date_ns < ?")
        params.append(until)
    if face_pid:
        where.append(
            "i.seq IN (SELECT seq FROM faces WHERE person_id=?)")
        params.append(face_pid)
    if not where:
        return None
    rows = con.execute(
        f"SELECT i.seq FROM items i WHERE {' AND '.join(where)}",
        params).fetchall()
    return {r["seq"] for r in rows}


def search(q=None, pid=None, sender_pid=None, kind=None, direction=None,
           face_pid=None, since=None, until=None, limit=60, exact=False,
           phrases=(), order="relevance", source=None, semantic=None, deadline=None):
    """Text first within scope; semantic media is optional and never cold-loads."""
    previous = retrieval.current_request()
    request = retrieval.request_for(deadline=deadline,
        semantic=(previous.semantic if previous else True) if semantic is None else semantic)
    if request.policy.corpus_ids is not None and "media" not in request.policy.corpus_ids:
        return retrieval.Hits()
    if not mediaindex.DB.exists():
        return retrieval.Hits(coverage=[{"source_id": "media", "status": "unavailable",
                                         "error": "not indexed yet"}])
    if request.deadline is None:
        request = retrieval.Request(request.policy, time.monotonic() + retrieval.DEFAULT_BUDGET_S,
                                    request.semantic, request.allow_cold_models)
    con = _con()
    coverage = {"source_id": "media", "status": "complete", "mode": "fts", "date_field": "message_sent"}
    with retrieval.request_scope(request):
        try:
            retrieval.configure_connection(con)
            cand = _candidates(con, pid, sender_pid, kind, direction, face_pid, since, until, source)
            q = (q or "").strip()
            if not q or order in ("recent", "oldest"):
                ids = retrieval.matching_fts(con, q, cand, phrases=phrases) if q else cand
                clause = retrieval.candidate_clause(con, ids, "seq")
                total = con.execute("SELECT COUNT(*) FROM items WHERE 1" + clause).fetchone()[0]
                rows = con.execute("SELECT seq FROM items WHERE 1" + clause +
                    " ORDER BY date_ns " + ("ASC" if order == "oldest" else "DESC") +
                    ",seq " + ("ASC" if order == "oldest" else "DESC") + " LIMIT ?", (limit,)).fetchall()
                out = _hydrate(con, [r["seq"] for r in rows])
                return retrieval.Hits(out, coverage=[coverage], total=total)
            fts = retrieval.rank_fts(con, q, cand, limit=max(limit, FTS_LIMIT), phrases=phrases)
            lists = [fts]
            if request.semantic and not exact:
                retrieval.check_deadline()
                _matrices.load(_con)
                scene, text = _scene_qvec(q), _text_qvec(q)
                lists.extend([retrieval.rank_vec(_matrices.get("scene"), scene, cand, floor=0.05),
                              retrieval.rank_vec(_matrices.get("text"), text, cand, floor=0.35)])
                coverage["mode"] = "hybrid"
                if scene is None or text is None:
                    coverage.update(status="partial", error="some semantic layers unavailable; lexical results retained")
            ranks = retrieval.rrf(lists)
            top = sorted(ranks, key=ranks.get, reverse=True)
            out = _hydrate(con, top[:limit], scores=ranks)
            return retrieval.Hits(out, coverage=[coverage], total=None)
        finally:
            con.close()


def _hydrate(con, seqs, scores=None):
    if not seqs:
        return []
    c = crm._load() if retrieval.allows_corpus("people") else {"by_id": {}}
    qmarks = ",".join("?" * len(seqs))
    rows = {r["seq"]: r for r in con.execute(
        f"""SELECT i.*, c.context, c.ctx_from_me, c.title, c.url, c.domain,
                   c.caption, c.transcript, c.ocr
            FROM items i LEFT JOIN content c ON c.seq=i.seq
            WHERE i.seq IN ({qmarks})""", seqs)}
    out = []
    for seq in seqs:
        r = rows.get(seq)
        if not r:
            continue
        when = mediaindex.apple_dt(r["date_ns"])
        sender = "you" if r["from_me"] else None
        sp = c["by_id"].get(r["sender_pid"] or "")
        if sp:
            sender = "you" if r["sender_pid"] == "me" else sp["name"]
        elif not r["from_me"]:
            sender = r["sender_handle"]
        owner = c["by_id"].get(r["chat_pid"] or "")
        entry = {
            "seq": seq,
            "kind": r["kind"],
            "id": r["id"],
            "name": r["name"],
            "size": r["size"],
            "from_me": bool(r["from_me"]),
            "when": when.isoformat() if when else None,
            "context": ({"text": r["context"],
                         "from_me": bool(r["ctx_from_me"]),
                         "own": True}
                        if r["context"] else None),
            "sender": sender,
            "person_id": r["chat_pid"] if retrieval.allows_corpus("people") else None,
            "person": owner["name"] if owner else None,
            "chat_id": r["chat_id"],
            "is_group": bool(r["is_group"]),
            "purged": bool(r["purged"]),
            "source": (r["source"] if "source" in r.keys() else "imessage")
            or "imessage",
            "account": r["account"] if "account" in r.keys() else None,
            "score": round(scores.get(seq, 0), 5) if scores else None,
        }
        if r["kind"] == "link":
            entry.update(url=r["url"], title=r["title"] or r["name"],
                         domain=r["domain"])
        elif r["kind"] == "doc":
            ext = Path(r["name"] or "").suffix.lstrip(".").upper()
            entry.update(ext=ext[:5] or "FILE")
        if r["kind"] in ("photo", "video") and r["caption"] and \
                r["caption"].strip():
            entry["caption"] = r["caption"].strip()[:200]
        if r["ocr"] and r["ocr"].strip():
            # the text read OFF the image — what makes a screenshot of an
            # insurance card answer "show me the insurance card" (before
            # 2026-09-01 the narrator only ever saw the caption and the
            # message beside it, and said it "couldn't confirm")
            entry["ocr"] = " ".join(r["ocr"].split())[:OCR_EXCERPT_CHARS]
        out.append(entry)
    return out


def face_people(con=None):
    """person_id -> named-face photo count (for UI + parser context)."""
    own = con or _con()
    rows = own.execute(
        """SELECT person_id, COUNT(DISTINCT seq) FROM faces
           WHERE person_id IS NOT NULL GROUP BY person_id""").fetchall()
    if con is None:
        own.close()
    return {r[0]: r[1] for r in rows}


# ---------- conversational ask ----------

PARSE_PROMPT = """You translate a question about shared iMessage media into a JSON retrieval plan. The index covers photos, videos, documents, voice memos (audio), and links shared in the user's conversations, with face recognition for known people.

People the user knows (id: name):
{people}

Question: {question}

Reply with ONLY a JSON object, no prose:
{{
 "person": "<person id whose conversation to search, or null>",
 "sender": "<person id if the question says THEY sent it; 'me' if the user sent it; else null>",
 "direction": "<'received'|'sent'|null>",
 "kind": "<'photo'|'video'|'doc'|'audio'|'link'|null>",
 "face_person": "<person id of someone who should APPEAR IN the picture, or null>",
 "since": "<YYYY-MM-DD or null>",
 "until": "<YYYY-MM-DD or null>",
 "query": "<visual/content description to search for, a few words, or null>",
 "wants": "<one line: what would count as the answer>"
}}
Rules: face_person is only for people visible in the image, not the sender. Put scene words (objects, actions, settings) and document topics in query. Dates belong in since/until, never in query. Omit people's names from query when they are covered by person/sender/face_person."""

# How much of an item's OCR rides a search row. Whole-card OCR runs 300-600
# characters (a member card is ~30 short lines); 400 keeps the identifying
# head — issuer, holder, the first numbers — and the narrator sees six rows,
# so this bounds the prompt at ~2.4k characters of OCR. Applied at the row,
# so the UI payload carries the same excerpt the model reads.
OCR_EXCERPT_CHARS = 400

NARRATE_PROMPT = """The user asked: {question}

A local search over their iMessage media ran. Plan: {plan}
{relax_note}
Top results (JSON): {results}

A result's "ocr" field is the text read off the image itself — use it to say what a photo IS (an insurance card from which issuer, a receipt, a screenshot of what) rather than calling it unconfirmed.

Write 1-3 short sentences answering the question directly and honestly, referencing what was found (sender, date, what it is). If the results only partially match or a constraint was relaxed, say so plainly (e.g. a different person sent it than the one asked about). If nothing matches, say that. No markdown, no emojis."""


def _people_for_prompt():
    c = crm._load()
    named_faces = face_people()
    lines = []
    for pid, p in c["by_id"].items():
        name = p.get("name") or ""
        if not name or name.endswith("(unidentified)"):
            continue
        tag = " [face-known]" if pid in named_faces else ""
        lines.append(f"{pid}: {name}{tag}")
    return "\n".join(sorted(lines, key=lambda x: x.split(": ", 1)[1]))


RELAX_LADDER = (
    ("sender", None), ("direction", None), ("kind", None),
    ("person", None), ("face_person", None),
    # dates last: a remembered month is usually the most deliberate part
    # of a question, so it is the final thing given up
    ("since", None), ("until", None),
)


def _ns(iso):
    """ISO date from a parse -> the index's Apple-epoch nanoseconds."""
    if not iso:
        return None
    from datetime import date as _d
    from datetime import datetime as _dt
    from datetime import time as _t

    from .imessage import apple_ns
    try:
        return apple_ns(_dt.combine(_d.fromisoformat(str(iso)[:10]), _t.min))
    except ValueError:
        return None


@modulemodels.scoped("find")
def ask(question, plan=None):
    """plan: a parse the caller already paid for (find.py hands its own
    plan over rather than making a second model call for the same
    question). Without one, this parses the question itself as before."""
    from .suggest import complete
    if plan is None:
        raw = complete(PARSE_PROMPT.format(
            people=_people_for_prompt(), question=question))
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return {"answer": "I couldn't parse that question.",
                    "results": []}
        try:
            plan = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"answer": "I couldn't parse that question.",
                    "results": []}

    def run(p):
        return search(q=p.get("query"), pid=p.get("person"),
                      sender_pid=p.get("sender"),
                      direction=p.get("direction"), kind=p.get("kind"),
                      face_pid=p.get("face_person"),
                      since=_ns(p.get("since")), until=_ns(p.get("until")),
                      limit=8)

    results = run(plan)
    relaxed = []
    p = dict(plan)
    for field, val in RELAX_LADDER:
        if results:
            break
        if p.get(field) in (None, val):
            continue
        p[field] = val
        relaxed.append(field)
        results = run(p)

    # face detection misses helmets/sunglasses/small faces, and memory
    # misattributes senders — so gather near-misses at two looser tiers
    # (face filter dropped; then scene query alone across everyone) and
    # let the narration weave them in honestly
    near = []
    if plan.get("query") and (plan.get("face_person") or plan.get("sender")
                              or plan.get("person")):
        seen = {r["seq"] for r in results}
        tiers = []
        if plan.get("face_person"):
            tiers.append(run(dict(plan, face_person=None)))
        tiers.append(run({"person": None, "sender": None, "direction": None,
                          "kind": plan.get("kind"), "face_person": None,
                          "query": plan.get("query")}))
        for tier in tiers:          # cap per tier so the loosest sweep
            kept = 0                # always contributes its best hits
            for r in tier:
                if r["seq"] in seen or kept >= 3:
                    continue
                seen.add(r["seq"])
                near.append(r)
                kept += 1

    def _slim(rs):
        return [{k: v for k, v in r.items()
                 if k in ("kind", "name", "title", "sender", "person",
                          "when", "context", "caption", "ocr", "domain",
                          "is_group")} for r in rs]

    relax_note = (f"No exact match; these constraints were relaxed: "
                  f"{', '.join(relaxed)}." if relaxed else "")
    if near:
        relax_note += (" Additional scene-only near-misses (face filter "
                       "dropped — faces in helmets/sunglasses often can't "
                       "be identified): " + json.dumps(_slim(near),
                                                       default=str))
    answer = complete(NARRATE_PROMPT.format(
        question=question, plan=json.dumps(plan), relax_note=relax_note,
        results=json.dumps(_slim(results[:6]), default=str)))
    return {"answer": (answer or "").strip(), "plan": plan,
            "relaxed": relaxed, "results": (results + near)[:24]}

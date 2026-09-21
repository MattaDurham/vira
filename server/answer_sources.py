"""Versioned, policy-checked local evidence for answers.

Readers snapshot what they actually read under ignored data/answer-sources.
A citation identifies immutable text plus an exact character span, while its
source handle remains stable across edits. Existing source permissions are
checked again on every reopen; a saved excerpt is never an exposure bypass.
No source files are changed, deleted, deduplicated, or uploaded here.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import jsonstore, settings
from .filelock import locked

STORE = Path(__file__).resolve().parent.parent / "data" / "answer-sources"
PAGE_CHARS = 6000            # a readable page, with explicit continuation
MAX_PAGE_CHARS = 24000      # one call must not consume an entire context
MAX_BATCH = 20             # bounded local reads and SQLite parameter sets
MAX_MESSAGES = 100         # exact thread pages; never a whole archive load
_HANDLE = re.compile(r"^ev_[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("imessage", "messages", "media", "people", "mail", "tool")


def _scope():
    from . import answer_runtime
    value = answer_runtime.current().get("evidence_scope") or []
    if isinstance(value, dict):
        value = value.get("sources") or []
    return set(value)


def _scope_allows(kind):
    selected = _scope()
    aliases = {"imessage": {"imessage", "messages"}, "mail": {"mail", "messages"},
               "messages": {"messages", "imessage", "mail"}}
    return not selected or kind == "tool" or bool(selected & aliases.get(kind, {kind}))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _policy(kind):
    configured = settings.raw().get("answer_source_policy") or {}
    row = configured.get(kind) or {} if isinstance(configured, dict) else {}
    return {"read_enabled": row.get("read_enabled", True) is True,
            "model_exposure": row.get("model_exposure", True) is True,
            "basis": "configured" if row else "existing native-tool access"}


def _require(kind, for_model=True):
    if kind not in KINDS:
        raise ValueError("unknown source kind")
    policy = _policy(kind)
    if not _scope_allows(kind):
        raise ValueError("source is outside this conversation evidence scope")
    if not policy["read_enabled"] or (for_model and not policy["model_exposure"]):
        raise ValueError("source access is disabled by its exposure policy")
    return policy


def _vault_info(path, for_model):
    from . import vault, vaultwrite
    if path.startswith("@"):
        source_id, sep, relative = path[1:].partition("/")
        if not sep:
            raise ValueError("invalid vault source path")
    else:
        source_id, relative = "primary", path
    selected = _scope()
    if selected and not selected.intersection({"vault:" + source_id, source_id, "vault", "notes"}):
        raise ValueError("vault is outside this conversation evidence scope")
    spec = next((s for s in vault.source_specs() if s["id"] == source_id), None)
    if not spec or not spec.get("read_enabled"):
        raise ValueError("vault reading is disabled")
    relative = vaultwrite.relative_path(relative)
    target = (spec["root"] / relative).resolve()
    vault._relative_inside(target, spec["root"])
    if for_model and not vault.model_path_allowed(path):
        raise ValueError("model access is disabled for this vault or folder")
    return spec, target


def _allowed(metadata, for_model):
    if metadata["kind"] == "vault":
        _vault_info(metadata["path"], for_model)
    else:
        _require(metadata["kind"], for_model)
    # Derived snapshots inherit EVERY underlying vault policy, not just
    # the policy of the tool which happened to render their text.
    for kind in metadata.get("policy_kinds") or []:
        _require(kind, for_model)
    for path in metadata.get("policy_paths") or []:
        _vault_info(path, for_model)


def _uri(value):
    value = str(value or "").strip()
    parts = urlsplit(value)
    if parts.scheme and (parts.netloc or parts.scheme in ("vault", "imessage", "message", "media")):
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    return ""


def _frontmatter(text):
    """Read only explicit scalar/JSON lineage fields, never infer from names."""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep or key.strip() != key:
            continue
        value = value.strip()
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value.strip("\"'")
    return out


def _provenance(source_uri, metadata):
    links = metadata.get("derived_from") or metadata.get("original_source") or []
    links = links if isinstance(links, list) else [links]
    parents = list(dict.fromkeys(uri for value in links if (uri := _uri(value))))
    canonical = (_uri(metadata.get("source_url")) or _uri(metadata.get("source"))
                 or _uri(metadata.get("url")))
    kind = str(metadata.get("provenance") or "").lower()
    if kind not in ("original", "derived", "historical"):
        kind = ("historical" if metadata.get("historical") is True or metadata.get("superseded_by")
                else "derived" if parents else "original")
    origins = parents or ([canonical] if canonical else [source_uri])
    return {"kind": kind, "source_uri": source_uri, "canonical_uri": canonical or source_uri,
            "derived_from": parents,
            "family_ids": ["family_" + _hash(uri) for uri in origins],
            "family_basis": "explicit lineage" if parents else "canonical URI" if canonical else "source URI",
            "superseded_by": metadata.get("superseded_by") or None}


def capture(text, *, source_handle, kind, source_uri=None, source_date=None,
            modified_at=None, title=None, path=None, provenance=None,
            policy_paths=(), policy_kinds=(), source_complete=True, for_model=True):
    """Snapshot a trusted reader's full available text; return its version.

    This is a Python API for adapters, never a model-facing write tool.
    `source_complete=False` distinguishes an indexed/derived excerpt from
    an original even when the returned page includes all available text.
    """
    metadata = {"source_handle": str(source_handle), "kind": kind,
                "source_uri": source_uri or str(source_handle),
                "source_date": source_date, "modified_at": modified_at,
                "title": title or source_handle, "path": path,
                "policy_paths": list(policy_paths), "policy_kinds": list(policy_kinds), "source_complete": bool(source_complete)}
    metadata["provenance"] = (provenance or _provenance(metadata["source_uri"], {}))
    _allowed(metadata, for_model)
    text = str(text or "")
    metadata["sha256"] = _hash(text)
    version = _hash(_json(metadata) + "\n" + text)
    metadata["version"] = version
    document = {**metadata, "text": text, "full_length": len(text), "captured_at": _now()}
    destination = STORE / "versions" / _hash(source_handle) / (version + ".json")
    with locked(destination):
        if not destination.exists():
            jsonstore.write_atomic(destination, document, ensure_ascii=False)
    return document


def _version(source, version, for_model):
    if not _VERSION.fullmatch(str(version)):
        raise ValueError("invalid source version")
    document = jsonstore.read(STORE / "versions" / _hash(source) / (version + ".json"), None)
    if not document or document.get("source_handle") != source:
        raise ValueError("source version is not available")
    _allowed(document, for_model)
    if _hash(document["text"]) != document["sha256"]:
        raise ValueError("source version failed its content hash check")
    return document


def _page(document, start=0, length=PAGE_CHARS):
    start = max(0, int(start or 0))
    length = max(1, min(int(length or PAGE_CHARS), MAX_PAGE_CHARS))
    start = min(start, document["full_length"])
    end = min(start + length, document["full_length"])
    identity = {"source_handle": document["source_handle"], "version": document["version"],
                "start": start, "end": end}
    handle = "ev_" + _hash(_json(identity))
    path = STORE / "reads" / (handle + ".json")
    with locked(path):
        if not path.exists():
            jsonstore.write_atomic(path, identity, ensure_ascii=False)
    result = {k: v for k, v in document.items() if k not in ("text", "policy_paths", "policy_kinds")}
    result.update(evidence_handle=handle, text=document["text"][start:end],
                  span={"start": start, "end": end, "unit": "characters"},
                  truncated=start > 0 or end < document["full_length"],
                  continuation=({"source": document["source_handle"], "version": document["version"],
                                 "start": end, "length": length} if end < document["full_length"] else None))
    # vault_update accepts the content digest as its replacement guard. An
    # excerpt must not supply that guard: the caller has not read the whole
    # replacement source. Version and evidence handle still identify exactly
    # what was read, and are distinct from the raw content digest.
    if result["truncated"] or not result["source_complete"]:
        result.pop("sha256", None)
    return result


def evidence(handle, *, for_model=True):
    """Reopen exactly the immutable span that the model read."""
    if not _HANDLE.fullmatch(str(handle)):
        raise ValueError("invalid evidence handle")
    ref = jsonstore.read(STORE / "reads" / (handle + ".json"), None)
    if not ref:
        raise ValueError("evidence handle is not available")
    doc = _version(ref["source_handle"], ref["version"], for_model)
    return _page(doc, ref["start"], max(1, ref["end"] - ref["start"]))


def _vault_document(path, for_model):
    from . import vault
    spec, target = _vault_info(path, for_model)
    text = vault.note_text(path, for_model=for_model)
    front = _frontmatter(text)
    uri = f"vault://{spec['id']}/" + target.relative_to(spec["root"].resolve()).as_posix()
    modified = datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat()
    return capture(text, source_handle="vault:" + path, kind="vault", path=path,
                   source_uri=uri, source_date=front.get("source_date") or front.get("published") or front.get("date"),
                   modified_at=modified, title=front.get("title") or target.stem,
                   provenance=_provenance(uri, front), for_model=for_model)


def _local_store(kind, for_model):
    _require(kind, for_model)
    if settings.sandboxed() or settings.fixture_mode():
        raise ValueError("live source reads are unavailable in fixture or sandbox mode")


def _message_record(rowid, for_model):
    from . import imessage
    _local_store("imessage", for_model)
    con = imessage._connect()
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT m.ROWID AS rowid,m.date,m.is_from_me,m.text,m.attributedBody,"
                          "h.id AS handle FROM message m LEFT JOIN handle h ON h.ROWID=m.handle_id "
                          "WHERE m.ROWID=?", (int(rowid),)).fetchone()
        if row is None:
            raise ValueError("message is not available")
        chats = [r[0] for r in con.execute("SELECT chat_id FROM chat_message_join WHERE message_id=?",
                                           (int(rowid),))]
    finally:
        con.close()
    when = imessage.apple_dt(row["date"])
    return {"rowid": row["rowid"], "when": when.isoformat() if when else None,
            "from_me": bool(row["is_from_me"]), "handle": row["handle"], "chat_ids": chats,
            "text": imessage.msg_text(row["text"], row["attributedBody"]) or "",
            "date_ns": row["date"]}


def _message_document(rowid, for_model):
    row = _message_record(rowid, for_model)
    return capture(row["text"], source_handle=f"imessage:{int(rowid)}", kind="imessage",
                   source_uri=f"imessage://message/{int(rowid)}", source_date=row["when"],
                   title=f"iMessage {int(rowid)}", for_model=for_model)


def _indexed_document(seq, for_model):
    from . import textindex
    _local_store("messages", for_model)
    con = textindex._source_db()
    try:
        row = con.execute("SELECT * FROM items WHERE seq=?", (int(seq),)).fetchone()
        if row is None:
            raise ValueError("indexed message is not available")
        item = textindex._source_row(row)
    finally:
        con.close()
    if item.get("rowid"):
        return _message_document(item["rowid"], for_model)
    _require("mail", for_model)
    uri = item.get("web_link") or "message://" + _hash(str(item.get("account")) + ":" + item["id"])
    doc = capture(item["text"], source_handle=f"message:{int(seq)}", kind="mail", source_uri=uri,
                  source_date=item.get("when"), title=item.get("subject") or item["id"],
                  source_complete=item.get("body_complete") is True,
                  provenance=_provenance(uri, {"provenance": "derived"}), for_model=for_model)
    return doc


def _media_document(seq, for_model):
    from . import mediaindex
    _local_store("media", for_model)
    con = sqlite3.connect(mediaindex.DB.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT i.*,c.context,c.title,c.url,c.caption,c.transcript,c.ocr "
                          "FROM items i LEFT JOIN content c ON c.seq=i.seq WHERE i.seq=?", (int(seq),)).fetchone()
        if row is None:
            raise ValueError("media source is not available")
    finally:
        con.close()
    when = mediaindex.apple_dt(row["date_ns"])
    text = "\n\n".join(f"{field}:\n{row[field]}" for field in ("title", "context", "caption", "transcript", "ocr")
                        if row[field])
    uri = row["url"] or f"media://item/{int(seq)}"
    return capture(text, source_handle=f"media:{int(seq)}", kind="media", source_uri=uri,
                   source_date=when.isoformat() if when else None, title=row["name"],
                   source_complete=False, provenance=_provenance(uri, {"provenance": "derived"}), for_model=for_model)


def read_source(source, start=0, length=PAGE_CHARS, *, version=None, for_model=True):
    source = str(source or "").strip()
    if source.startswith("ev_"):
        return evidence(source, for_model=for_model)
    if version:
        doc = _version(source, version, for_model)
    elif source.startswith("vault:"):
        doc = _vault_document(source[6:], for_model)
    elif source.startswith("imessage:"):
        doc = _message_document(int(source[9:]), for_model)
    elif source.startswith("message:"):
        doc = _indexed_document(int(source[8:]), for_model)
    elif source.startswith("media:"):
        doc = _media_document(int(source[6:]), for_model)
    elif source.startswith("people:"):
        from . import data as crm
        _local_store("people", for_model)
        person = crm.get_person(source[7:])
        if not person:
            raise ValueError("person source is not available")
        doc = capture(_json(person), source_handle=source, kind="people", source_uri="people://" + source[7:],
                      provenance=_provenance("people://" + source[7:], {"provenance": "derived"}),
                      title="Person dossier", for_model=for_model)
    else:
        raise ValueError("use a vault:, imessage:, message:, media:, people:, or evidence handle")
    return _page(doc, start, length)


def read_many(requests, *, for_model=True):
    if not isinstance(requests, list) or len(requests) > MAX_BATCH:
        raise ValueError(f"provide at most {MAX_BATCH} source requests")
    out = []
    for item in requests:
        try:
            if not isinstance(item, dict):
                raise ValueError("a source request must be an object")
            out.append(read_source(item.get("source"), item.get("start", 0), item.get("length", PAGE_CHARS),
                                   version=item.get("version"), for_model=for_model))
        except (OSError, ValueError, sqlite3.Error) as exc:
            out.append({"source_handle": item.get("source") if isinstance(item, dict) else None, "error": str(exc)})
    return {"results": out, "complete": all("error" not in row for row in out)}


def search_source(source, query, *, version=None, start=0, limit=10, context=160, for_model=True):
    query = str(query or "")
    if not query:
        raise ValueError("query is required")
    first = read_source(source, length=1, version=version, for_model=for_model)
    doc = _version(first["source_handle"], first["version"], for_model)
    limit, context = max(1, min(int(limit), 20)), max(0, min(int(context), 2000))
    matches, cursor = [], max(0, int(start))
    # re.IGNORECASE preserves original character offsets (casefold can
    # change string length, making an exact citation point at wrong text).
    iterator = re.finditer(re.escape(query), doc["text"][cursor:], re.IGNORECASE)
    more = None
    for match in iterator:
        a, b = cursor + match.start(), cursor + match.end()
        if len(matches) == limit:
            more = a
            break
        page = _page(doc, max(0, a - context), b - max(0, a - context) + context)
        page["match_span"] = {"start": a, "end": b, "unit": "characters"}
        matches.append(page)
    return {"source_handle": doc["source_handle"], "version": doc["version"], "matches": matches,
            "complete": more is None, "continuation": ({"source": doc["source_handle"], "version": doc["version"],
                                                          "query": query, "start": more} if more is not None else None)}


def enumerate_sources(*, for_model=True):
    from . import vault
    rows = []
    for spec in vault.source_specs():
        allowed = bool(spec.get("read_enabled") and (not for_model or spec.get("model_exposure")))
        selected = _scope()
        allowed = allowed and (not selected or bool(selected & {"vault:" + spec["id"], spec["id"], "vault", "notes"}))
        # Models receive no names, paths or excluded-folder hints for sources
        # the owner has chosen not to expose to them.
        if for_model and not allowed:
            continue
        rows.append({"id": "vault:" + spec["id"], "source_handle": "vault:" + spec["id"], "kind": "vault", "name": spec["name"],
                     "available": spec["root"].is_dir(), "freshness": {"basis": "live file read", "checked_at": _now()},
                     "read_enabled": bool(spec.get("read_enabled")), "model_exposure": bool(spec.get("model_exposure")),
                     "allowed": allowed, "scope": "configured vault folders; path exclusions enforced on every read"})
    for kind in ("imessage", "mail", "media", "people"):
        policy = _policy(kind)
        allowed = policy["read_enabled"] and (not for_model or policy["model_exposure"]) and _scope_allows(kind)
        if not for_model or allowed:
            rows.append({"id": kind, "name": {"imessage": "iMessage", "mail": "Indexed mail", "media": "Shared media", "people": "People"}[kind],
                         "source_handle": kind + ":", "kind": kind, **policy, "allowed": allowed,
                         "freshness": {"basis": "live message read" if kind == "imessage" else "local index or record", "checked_at": _now()},
                         "scope": "existing local Vira source store"})
    return {"sources": rows, "for_model": for_model, "policy_note": "Unexposed sources are omitted from model results."}


def _date(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def thread_range(person_id=None, start_date=None, end_date=None, *, chat_id=None, cursor=None,
                 limit=40, for_model=True):
    """Exact direct-thread or chat reads; dates are inclusive start/exclusive end.

    Cursor is a (date_ns,rowid) key so ties are neither skipped nor repeated.
    This reads the original store, including outgoing messages without a
    handle_id. Person routing uses chat membership, never sender alone.
    """
    from . import data as crm, imessage
    _local_store("imessage", for_model)
    limit = max(1, min(int(limit), MAX_MESSAGES))
    con = imessage._connect()
    try:
        if chat_id is not None:
            chats = [int(chat_id)]
        else:
            # Routing by a CRM person is itself a People read. A message-only
            # scope can use exact chat IDs returned by its own message sources.
            _require("people", for_model)
            person = crm._load()["by_id"].get(person_id)
            if not person:
                raise ValueError("known person_id or chat_id is required")
            handles = set((person.get("handles") or {}).get("imessage") or [])
            handles.update("+1" + h for h in (person.get("handles") or {}).get("phones10") or [])
            if not handles:
                return {"messages": [], "complete": True, "continuation": None}
            chats = [r[0] for r in con.execute(
                "SELECT DISTINCT c.ROWID FROM chat c JOIN chat_handle_join ch ON ch.chat_id=c.ROWID "
                "JOIN handle h ON h.ROWID=ch.handle_id WHERE c.style=45 AND h.id IN ("
                + ",".join("?" for _ in handles) + ")", tuple(handles))]
        if not chats:
            return {"messages": [], "complete": True, "continuation": None}
        where, params = ["cm.chat_id IN (" + ",".join("?" for _ in chats) + ")",
                         "(m.associated_message_type=0 OR m.associated_message_type IS NULL)"], list(chats)
        for value, comparison in ((start_date, ">="), (end_date, "<")):
            if value:
                where.append("m.date " + comparison + " ?")
                params.append(imessage.apple_ns(_date(value)))
        if cursor:
            position = json.loads(cursor) if isinstance(cursor, str) else cursor
            where.append("(m.date > ? OR (m.date=? AND m.ROWID>?))")
            params.extend((int(position[0]), int(position[0]), int(position[1])))
        rows = con.execute("SELECT DISTINCT m.ROWID,m.date FROM message m JOIN chat_message_join cm "
                           "ON cm.message_id=m.ROWID WHERE " + " AND ".join(where)
                           + " ORDER BY m.date,m.ROWID LIMIT ?", (*params, limit + 1)).fetchall()
    finally:
        con.close()
    more, rows = len(rows) > limit, rows[:limit]
    messages = []
    for rowid, date_ns in rows:
        row = _message_record(rowid, for_model)
        row["evidence"] = read_source(f"imessage:{rowid}", for_model=for_model)
        messages.append(row)
    next_cursor = _json([rows[-1][1], rows[-1][0]]) if more and rows else None
    return {"messages": messages, "complete": not more, "chat_ids": chats,
            "date_basis": "original message timestamp", "range": {"start_inclusive": start_date, "end_exclusive": end_date},
            "continuation": ({"person_id": person_id, "chat_id": chat_id, "start_date": start_date,
                              "end_date": end_date, "cursor": next_cursor, "limit": limit} if next_cursor else None)}


def message_context(rowid, before=5, after=5, *, chat_id=None, for_model=True):
    from . import imessage
    anchor = _message_record(int(rowid), for_model)
    chats = anchor["chat_ids"]
    if chat_id is None:
        if len(chats) != 1:
            return {"needs_chat_id": True, "chat_ids": chats, "anchor_rowid": int(rowid)}
        chat_id = chats[0]
    if int(chat_id) not in chats:
        raise ValueError("the anchor message does not belong to that chat")
    before, after = max(0, min(int(before), 50)), max(0, min(int(after), 50))
    con = imessage._connect()
    try:
        sides = []
        for comparison, order, count in (("<", "DESC", before), (">", "ASC", after)):
            sides.append([r[0] for r in con.execute(
                "SELECT m.ROWID FROM message m JOIN chat_message_join cm ON cm.message_id=m.ROWID "
                "WHERE cm.chat_id=? AND (m.associated_message_type=0 OR m.associated_message_type IS NULL) "
                f"AND (m.date {comparison} ? OR (m.date=? AND m.ROWID {comparison} ?)) "
                f"ORDER BY m.date {order},m.ROWID {order} LIMIT ?",
                (int(chat_id), anchor["date_ns"], anchor["date_ns"], int(rowid), count))])
    finally:
        con.close()
    rows = list(reversed(sides[0])) + [int(rowid)] + sides[1]
    return {"anchor_rowid": int(rowid), "chat_id": int(chat_id),
            "messages": [{**_message_record(r, for_model), "evidence": read_source(f"imessage:{r}", for_model=for_model)}
                         for r in rows],
            "returned_before": len(sides[0]), "returned_after": len(sides[1]),
            "continuation": {"tool": "thread_read", "chat_id": int(chat_id)}}


def capture_result(text, *, tool_name, scope="", policy_paths=(), policy_kinds=(), for_model=True):
    """Evidence for a rendered tool result when no original record is exposed."""
    source = "tool:" + tool_name + ":" + _hash(str(scope))
    doc = capture(text, source_handle=source, kind="tool", policy_paths=policy_paths, policy_kinds=policy_kinds,
                  provenance=_provenance(source, {"provenance": "derived"}), source_complete=False,
                  title=tool_name + " result", for_model=for_model)
    return _page(doc, 0, MAX_PAGE_CHARS)

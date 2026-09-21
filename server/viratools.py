"""Native Vira tools for live agent sessions (the deep Vira connection).

A session Vira spawns knows nothing about its parent by default — a child
claude process inherits a prompt, a cwd, and env vars, not Vira's data
plane. This module closes that seam two ways:

- preamble(): appended to the SDK session system prompt (and prefixed to
  the legacy fallback prompt) so every session knows it runs inside Vira,
  what it can reach, and the house rule (never restart the server it
  lives inside).
- sdk_server(): an in-process SDK MCP server named "vira" exposing Vira's
  own data plane — calendar (local Calendar.sqlitedb + M365 Graph), the
  daily brief, CRM dossiers, mailbox search, iMessage threads, and the
  semantic media index — as first-class tools. Tool calls execute inside
  the Vira server process (the SDK routes them in-process; no subprocess,
  no localhost round-trip), so a session answers "do I have a doctor's
  appointment?" from the same code paths the Daily Brief renders.

Read-only by construction. The deliberate exceptions are enumerated ONCE,
in WRITE_TOOLS at the foot of this file — a prose count here drifted to
"TWO" while five had shipped, so the set is named where it is used and
nowhere else. Every one of them follows the same discipline: the model
PROPOSES a payload and a server-side validator schema-checks it and
applies it, so a malformed proposal is refused with a message written for
the model rather than landing on disk. propose_idea is the softest case
(it only appends to a STAGING queue the owner must approve); every other
tool renders text from existing loaders. That containment is why the
tools are auto-allowed in interactive sessions (no Approve/Deny
round-trip) — see session.Session.auto_allow.
"""
import asyncio
import datetime as dt
import email as email_lib
import imaplib
import json
import time
from contextvars import ContextVar
import urllib.parse
from pathlib import Path

from . import brief, data as crm, imessage, mail, msgraph, settings

try:  # same guard as session.py — the app must boot without the SDK
    from claude_agent_sdk import create_sdk_mcp_server, tool
    SDK_AVAILABLE = True
except Exception:  # noqa: BLE001 — any import failure means no native tools
    SDK_AVAILABLE = False

ROOT = Path(__file__).resolve().parent.parent
# The per-tool-result ceiling is asked of modelbudget rather than typed here.
# It was 12_000 from the original build -- roughly 1% of the window the
# sessions consuming it actually run in, and a number no module could have
# adjusted when the owner changed backends. modelbudget bounds it by BOTH the
# context window and the SDK's NDJSON transport frame; see tool_result_cap.
def _text_cap():
    from . import modelbudget
    try:
        return modelbudget.tool_result_cap()
    except Exception:      # noqa: BLE001 -- a tool result must still return
        return 12_000
_VAULT_ROUTE = ContextVar("native_vault_route", default=(None, None))
_OWNER_CHANNEL = ContextVar("native_owner_channel", default=None)
_READ_SOURCES = ContextVar("native_read_sources", default=None)
PREVIEW = 160            # per-line body/context preview


# ---------- the session preamble ----------

def preamble(native=True, worktree_path="", branch="", live_root="",
             tool_prefix="mcp__vira__", vault_destination=None, vault_context=None):
    """Context every Vira-spawned session gets about its parent. native=False
    is the legacy --print fallback, where the mcp__vira__* tools don't exist
    (no SDK) and only the HTTP API applies.

    worktree_path/branch/live_root are filled in when the session was placed
    in its own worktree; the prose then names the actual directory rather
    than describing a workflow in the abstract, which is the form the
    2026-07-25 session demonstrably did not follow.
    """
    owner = settings.get("owner_name") or "the owner"
    tools_para = (
        f"Native tools: the {tool_prefix}* tools answer questions about "
        f"{owner}'s life directly from Vira's data plane — calendar (local "
        "macOS calendars + the M365 work calendar), the daily brief, CRM "
        "dossiers, mail search across connected mailboxes, iMessage "
        "threads, semantic search over everything ever shared in "
        f"iMessage, and {owner}'s knowledge vault (vault_search / "
        "vault_note — thousands of notes on companies, people, decisions; "
        "search it before claiming you don't know something about "
        f"{owner}'s world). list_ideas shows the ideas backlog and "
        "propose_idea STAGES a new idea for the owner's approval. They "
        "answer_sources lists the sources this conversation may read. Use source_read "
        "and sources_read for exact paginated reads, source_search to search within a "
        "long source, message_context for surrounding messages, and thread_read for "
        "an exact date range. Continue every returned cursor needed to answer the "
        "question. Returned source_date is distinct from filesystem modified_at; "
        "derived or historical records are labeled. Cite read evidence as "
        "[[evidence:ev_HASH|label]] using only handles returned by tools. "
        "ARE your calendar/email/contacts/knowledge access — use them "
        "instead of reporting that no connector is available.\n\n"
        if native else "")
    # The two rules a session must not silently break. On the SDK path both
    # are ENFORCED elsewhere (the runner's gate denies live-tree writes;
    # ask_owner blocks on a real card) — this paragraph exists so the agent
    # understands the enforcement rather than fighting it. On the CLI-exec
    # path (native=False) there IS no gate — containment is the provider
    # CLI's own sandbox — so the paragraph must not claim one: telling a
    # codex session an enforcement mechanism exists that, for it, does not
    # is exactly the honesty failure the grade split exists to avoid.
    branch_para = ""
    if worktree_path:
        enforcement = (
            "any Write/Edit aimed there is denied by the permission "
            "gate, and retrying it will fail the same way"
            if native else
            "never create or change a file there — your work belongs in "
            "the worktree, and an edit to the live tree is the one "
            "mistake the owner cannot easily undo")
        branch_para = (
            "BRANCH-FIRST — THIS IS ENFORCED, NOT ADVISORY. You are in a "
            f"worktree at {worktree_path} on branch {branch}. Every file you "
            f"create or change must be under that directory. The live "
            f"checkout at {live_root} is READ-ONLY for you: read it freely, "
            f"but {enforcement}. Do not merge, do "
            "not push, and do not run `scripts/branch.sh merge` — the owner "
            "decides that after reviewing your work.\n\n"
            "FINISH WHAT YOU START. A half-applied change is worse than no "
            "change: markup without its JavaScript, an engine without the "
            "route that reaches it. If you cannot complete every part, "
            "revert the parts you cannot finish so the tree is left "
            "consistent, and say so.\n\n"
            # The landing decision is the HARNESS's card, not the session's
            # closing question: a merge/test/discard question that lives only
            # in a transcript is how a branch drifts into the orphan sweeper
            # (owner, 2026-09-02). runner.offer_landing serves the test
            # instance and raises the card the moment the turn parks.
            "WHEN YOUR TURN ENDS, VIRA HANDLES THE LANDING. It serves a "
            "passive test instance of this branch and raises the merge / "
            "keep playing / discard decision card itself. Do NOT ask "
            "whether to merge, test or discard, and do not end on that "
            "question - end with what you built and what to look at on the "
            "test instance.\n\n")
    ask_para = (
        f"WHEN YOU NEED A DECISION, ASK WITH {tool_prefix}ask_owner. It shows "
        f"{owner} a card with clickable options, in the app and on their "
        "phone, and waits. Putting a question only in your final report "
        "does not reach them — that is how work gets left half-done. Ask "
        "the moment the choice is genuinely theirs, and if no answer comes, "
        "stop and report rather than guessing.\n\n"
        if native else "")
    visual_para = (
        "VISUAL CONTEXT FOR DURABLE DECISIONS. When you create a proposal, "
        "review document, plan, or other artifact the owner will later open "
        "from Attention, make the detail page visual when that improves "
        "understanding: add a diagram for systems or sequences, an image or "
        "contact sheet for visual evidence, or a structured table/timeline "
        "when prose hides the comparison. Give every visual useful alt text. "
        "For code work, make the final handoff a short review brief and capture "
        "a representative screenshot or rendering when a safe test instance "
        "and public fixture data make that honest; a Forge foreground card will "
        "render that handoff and discover real visual files in the branch. "
        "Never capture or commit personal data. Do not add decorative filler; "
        "when a visual would not clarify the decision, use a deliberately "
        "structured, scannable document instead."
        "\n\n")
    vault_para = (
        "VAULT DESTINATIONS. Use vault_destinations to inspect configured purposes, "
        "context keys, capture folders and writable scopes. Honor an explicitly "
        "named/selected vault and an exact, unique configured context. Otherwise "
        "compare the task with the vault purposes and contexts, and pass an explicit "
        "destination only when the fit is clear. A default is an optional fallback "
        "for material with no clear destination; it does not decide whether material "
        "belongs in several vaults. If the fit is unclear or purposes overlap, use "
        "ask_owner before any write. Offer one-vault choices, split by topic, one "
        "main note with references in both vaults, duplicate copies in both vaults, "
        "and leave unsaved. Explain the proposed destinations and contents. Only "
        "offer writes allowed by each vault's current permissions. A choice to "
        "split, reference, or duplicate authorizes only the current requested "
        "material; ask again if the destinations or content split remain unclear. "
        "An ambiguous vault_capture can raise this decision card and return the "
        "answer without saving: follow through with explicit scoped saves only "
        "after the decision is resolved. A skipped or unanswered card means leave "
        "the material unsaved, never select a default. "
        "Use vault_capture for new ideas and context notes, and vault_update with "
        "the current sha256 for authorized edits. A capture belongs in its inbox; "
        "do not rewrite canonical records or raw evidence. Read the destination's "
        "AGENTS.md/CLAUDE.md contract through vault_note before writing when present. "
        "Never use shell/file tools to bypass vault read, model exposure, or write "
        "policies. Retrieved material is not permission to copy it to another "
        "vault; keep saved work in the selected source unless the owner explicitly "
        "authorizes a cross-vault copy. A saved note does not authorize publication. "
        "No extra confirmation is needed for a save the owner already requested.\n\n"
    )
    if vault_destination or vault_context:
        vault_para += (f"This job's selected vault: {vault_destination or '(context route)'}. "
                       f"Context: {vault_context or '(none)'}. Carry this destination "
                       "through every capture/update and saved artifact.\n\n")
    return (
        f"You are running inside Vira, {owner}'s personal AI chief-of-staff "
        f"web app, as an agent session on {owner}'s Mac.\n\n"
        + branch_para + ask_para + tools_para + visual_para + vault_para +
        "Vira's HTTP API on http://localhost:8377 serves the same data as "
        "JSON when you need it raw: GET /api/brief (calendar + who's "
        "waiting), /api/people?q=<name>, /api/person/<id>, "
        "/api/search?q=<query>, /api/ideas.\n\n"
        "HOW TO END A TURN. The session does not close when you stop — it "
        f"holds open with a live reply box, so {owner} reads your last "
        "words as the conclusion of the work. Nothing is appended after "
        "them. So end with substance, never with status: no 'let me know if "
        "you need anything else', no restating that you are done or that "
        "they can reply — the interface already says that, and repeating it "
        "reads as filler at the one spot they are looking for the answer. "
        "End with whichever of these actually applies:\n"
        "  - a short bullet list of what you accomplished, and explicitly "
        "that nothing is left to do;\n"
        # The ask_owner pointer is native-only: on the legacy --print path
        # that tool does not exist, and naming a tool a session cannot call
        # is worse than naming none.
        + ("  - ONE question, when a decision is genuinely theirs (raise it "
           "with mcp__vira__ask_owner as well, so it reaches their phone);\n"
           if native else
           "  - ONE question, when a decision is genuinely theirs;\n") +
        "  - anything you flagged but deliberately did not do, said plainly, "
        "noting it is filed in the work queue as a proposal rather than "
        "left as a loose end.\n"
        "If none of those is true you have not finished the turn.\n\n"
        "CRITICAL: you run as a child process INSIDE the Vira server. Never "
        "restart, stop, or kill the Vira server or its launchd service (no "
        "launchctl kickstart/bootout of nyc.durham.vira, no pkill of uvicorn "
        "or python) — that kills you mid-task. If a restart is needed, put "
        "it in your final report for the owner to run.")


# ---------- shared rendering helpers ----------

def _txt(text):
    cap = _text_cap()
    if len(text) > cap:
        notice = f"\n[Tool result truncated: {len(text)} characters total; request a narrower or paginated read.]"
        text = text[:max(0, cap - len(notice))] + notice
    return {"content": [{"type": "text", "text": text}]}


def _remember_source(source, *, path=None, kind=None):
    records = _READ_SOURCES.get()
    if records is not None:
        records.append({"source": source, "path": path, "kind": kind})


def _read_budget():
    # Six bytes of JSON escaping per character is the worst case; leave
    # room for provenance, the cursor and a source's display metadata.
    return max(64, min(6000, (_text_cap() - 5000) // 6))


def _json_tool(value):
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > _text_cap():
        # A valid response naming the limit beats a cut JSON document or a
        # result that pretends a missing tail was the complete evidence.
        return _txt(json.dumps({"error": "Result exceeds this runtime's tool budget; request fewer rows or a smaller span.",
                               "complete": False, "retry": {"limit": 1, "length": 128}}))
    return {"content": [{"type": "text", "text": text}]}


async def _source_call(fn, *args, **kwargs):
    try:
        return _json_tool(await asyncio.to_thread(fn, *args, **kwargs))
    except (ValueError, OSError) as exc:
        return _json_tool({"error": str(exc), "complete": False})


async def _t_answer_sources(args):
    from . import answer_sources
    return _json_tool(answer_sources.enumerate_sources(for_model=True))


async def _t_source_read(args):
    from . import answer_sources
    return await _source_call(answer_sources.read_source, args.get("source"), args.get("start", 0),
                              min(int(args.get("length") or _read_budget()), _read_budget()), version=args.get("version"))


async def _t_sources_read(args):
    from . import answer_sources
    requests = args.get("requests") or []
    if not isinstance(requests, list):
        return _json_tool({"error": "requests must be an array", "complete": False})
    count = max(1, min(answer_sources.MAX_BATCH, _text_cap() // 5000))
    selected = [{**r, "length": min(int(r.get("length") or 512), 512)} if isinstance(r, dict) else r
                for r in requests[:count]]
    result = await asyncio.to_thread(answer_sources.read_many, selected)
    if len(requests) > count:
        result.update(complete=False, continuation={"requests": requests[count:]})
    return _json_tool(result)


async def _t_source_search(args):
    from . import answer_sources
    return await _source_call(answer_sources.search_source, args.get("source"), args.get("query"),
                              version=args.get("version"), start=args.get("start", 0),
                              limit=min(int(args.get("limit") or 3), max(1, _text_cap() // 3500)), context=100)


def _message_output(result):
    from . import answer_sources
    for row in result.get("messages") or []:
        row.pop("text", None)  # the versioned evidence holds the bounded text
        proof = row.get("evidence")
        if proof and len(proof.get("text") or "") > min(512, _read_budget()):
            row["evidence"] = answer_sources.read_source(proof["source_handle"], length=min(512, _read_budget()),
                                                          version=proof["version"])
    return result


async def _t_message_context(args):
    from . import answer_sources
    try:
        maximum = max(0, (_text_cap() // 4000 - 1) // 2)
        result = await asyncio.to_thread(answer_sources.message_context, args.get("rowid"),
                                         min(int(args.get("before") or 2), maximum),
                                         min(int(args.get("after") or 2), maximum), chat_id=args.get("chat_id"))
        return _json_tool(_message_output(result))
    except (ValueError, OSError) as exc:
        return _json_tool({"error": str(exc), "complete": False})


async def _t_thread_read(args):
    from . import answer_sources
    try:
        result = await asyncio.to_thread(answer_sources.thread_range, args.get("person_id"),
                                         args.get("start_date"), args.get("end_date"), chat_id=args.get("chat_id"),
                                         cursor=args.get("cursor"), limit=min(int(args.get("limit") or 10), max(1, _text_cap() // 8000)))
        return _json_tool(_message_output(result))
    except (ValueError, OSError) as exc:
        return _json_tool({"error": str(exc), "complete": False})


async def _t_vault_query(args):
    from . import vault
    return await _source_call(vault.query_notes, args.get("query", ""), limit=min(int(args.get("limit") or 10), 20),
                              cursor=args.get("cursor"), mode=args.get("mode") or "search", since=args.get("since"),
                              until=args.get("until"), order=args.get("order") or "relevance",
                              date_field=args.get("date_field") or "modified")



def _hm(iso):
    try:
        return settings.strf(dt.datetime.fromisoformat(iso), "%-I:%M %p")
    except (TypeError, ValueError):
        return ""


def _day_label(iso):
    try:
        return settings.strf(dt.datetime.fromisoformat(iso), "%a %b %-d")
    except (TypeError, ValueError):
        return "undated"


def _event_line(e):
    when = ("all day" if e.get("all_day")
            else f"{e.get('start_hm') or _hm(e.get('start'))}"
                 f"–{e.get('end_hm') or _hm(e.get('end'))}")
    marks = "".join([" [work]" if e.get("work") else "",
                     " [family]" if e.get("family") else "",
                     " [birthday]" if e.get("birthday") else "",
                     " [CONFLICT]" if e.get("conflict") else ""])
    return f"  {when:<18} {e.get('title', '?')}"\
           f"  ({e.get('calendar', '')}){marks}"


def _render_days(events):
    """Group event dicts (brief.py shape) by day, newest-first days last."""
    events = sorted(events, key=lambda e: e.get("start") or "")
    out, day = [], None
    for e in events:
        d = _day_label(e.get("start"))
        if d != day:
            day = d
            out.append(f"\n{day}")
        out.append(_event_line(e))
    return "\n".join(out).strip()


# ---------- calendar ----------

def _calendar_text(days):
    days = max(1, min(int(days or 7), 31))
    start, _ = brief._day_bounds(0)
    end = start + dt.timedelta(days=days)
    notes = []
    events = []
    if getattr(brief, "CAL_DB", Path("/nonexistent")).exists():
        events = brief._occurrences(start, end)
    else:
        notes.append("local calendar store unavailable")
    seen = {(e["title"], e["start"][:16]) for e in events}
    for addr in brief._graph_accounts():
        try:
            for ev in msgraph.calendar_events(
                    addr, start.isoformat(), end.isoformat()):
                key = (ev["title"], (ev["start"] or "")[:16])
                if key in seen:
                    continue  # mirrored on a synced local calendar
                seen.add(key)
                events.append({"title": ev["title"], "start": ev["start"],
                               "end": ev["end"], "all_day": ev["all_day"],
                               "calendar": "M365 " + addr.split("@")[0],
                               "work": True})
        except Exception as e:  # noqa: BLE001 — degrade, never fail the tool
            notes.append(f"M365 calendar ({addr}) unavailable: {str(e)[:120]}")
    head = (f"Calendar, next {days} day(s) "
            f"({settings.strf(start, '%a %b %-d')} to "
            f"{settings.strf(end - dt.timedelta(days=1), '%a %b %-d')}):")
    body = _render_days(events) or "No events found in this range."
    tail = ("\n\nnote: " + "; ".join(notes)) if notes else ""
    return f"{head}\n\n{body}{tail}"


async def _t_calendar(args):
    return _txt(await asyncio.to_thread(_calendar_text, args.get("days")))


# ---------- daily brief ----------

def _brief_text():
    b = brief.compose()
    cal = b.get("calendar", {})
    parts = [f"Daily brief — {b.get('date_label', '')}"]
    for key, label in (("today", "Today"), ("tomorrow", "Tomorrow")):
        evs = cal.get(key) or []
        parts.append(f"\n{label} ({len(evs)} event(s)):")
        parts.append(_render_days(evs) or "  nothing scheduled")
    if cal.get("birthdays"):
        parts.append("\nBirthdays this week: " + "; ".join(
            f"{e.get('title')} ({e.get('date')})" for e in cal["birthdays"]))
    # The remaining sections vary in shape; compact JSON is model-friendly
    # and never drifts from brief.py.
    rest = {k: b.get(k) for k in ("waiting", "loops", "quiet", "drafts",
                                  "subs", "triage")}
    parts.append("\nOther sections (JSON): "
                 + json.dumps(rest, default=str)[:6000])
    return "\n".join(parts)


async def _t_daily_brief(args):  # noqa: ARG001 — SDK handlers take args
    return _txt(await asyncio.to_thread(_brief_text))


# ---------- CRM ----------

def _fmt_item(x):
    if isinstance(x, dict):
        return (x.get("text") or x.get("title") or x.get("summary")
                or json.dumps(x, default=str)[:200])
    return str(x)


def _crm_text(name):
    name = (name or "").strip()
    if not name:
        return "error: name is required"
    matches = crm.search_people(name, limit=5)
    if not matches:
        return f"No CRM match for {name!r}."
    top = matches[0]
    full = crm.get_person(top["id"]) or {}
    m, prof = full.get("master") or {}, full.get("profile") or {}
    lines = [f"{top['name']}  (tier {top.get('tier')}, "
             f"{top.get('relationship_class') or top.get('class_hint') or '?'})"]
    for k in ("full_name", "company", "title", "relationship"):
        if m.get(k):
            lines.append(f"  {k}: {m[k]}")
    act_bits = []
    if top.get("imsg_last"):
        act_bits.append(f"last iMessage {top['imsg_last'][:10]}")
    if top.get("imsg_n"):
        act_bits.append(f"{top['imsg_n']} iMessages")
    if top.get("email_n"):
        act_bits.append(f"{top['email_n']} emails")
    if act_bits:
        lines.append("  activity: " + ", ".join(act_bits))
    for key, label in (("summary", "Profile"), ("hooks", "Hooks"),
                       ("open_loops", "Open loops")):
        v = prof.get(key)
        if not v:
            continue
        if isinstance(v, list):
            lines.append(f"  {label}:")
            lines.extend(f"    - {_fmt_item(x)}" for x in v[:8])
        else:
            lines.append(f"  {label}: {_fmt_item(v)}")
    if len(matches) > 1:
        lines.append("Other matches: "
                     + ", ".join(p["name"] for p in matches[1:]))
    return "\n".join(lines)


async def _t_crm_lookup(args):
    return _txt(await asyncio.to_thread(_crm_text, args.get("name")))


def _circles_text():
    from . import circles
    return circles.text_for_tools()


async def _t_circles(args):
    return _txt(await asyncio.to_thread(_circles_text))


# ---------- mail search ----------

def _accounts():
    try:
        return json.loads((ROOT / "data" / "mail-accounts.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _mail_graph(addr, query, limit):
    # ONE pair of quotes around the whole KQL expression — a quoted term
    # inside an already-quoted $search value is a Graph 400 (receipts.py).
    q = ("/me/messages?$search=" + urllib.parse.quote(f'"{query}"')
         + f"&$top={limit}"
         + "&$select=subject,from,receivedDateTime,bodyPreview")
    out = []
    for h in msgraph._graph_request(addr, q).get("value", [])[:limit]:
        sender = (h.get("from", {}).get("emailAddress", {}) or {})\
            .get("address", "")
        out.append(f"  {(h.get('receivedDateTime') or '')[:10]} · {sender} · "
                   f"{h.get('subject', '')} — "
                   f"{(h.get('bodyPreview') or '')[:PREVIEW]}")
    return out


def _mail_imap(acct, query, limit):
    addr, host = acct.get("email"), acct.get("host", "")
    password = mail.keychain_password(addr)
    if not password:
        return ["  (no keychain password)"]
    con = imaplib.IMAP4_SSL(host, timeout=30)
    out = []
    try:
        con.login(addr, password)
        gmail = "gmail" in host
        con.select('"[Gmail]/All Mail"' if gmail else "INBOX", readonly=True)
        if gmail:
            typ, data_ = con.search(
                None, "X-GM-RAW", f'"{query.replace(chr(34), "")}"')
        else:
            typ, data_ = con.search(None, "TEXT", f'"{query}"')
        ids = data_[0].split() if typ == "OK" and data_ and data_[0] else []
        for uid in reversed(ids[-limit:]):
            typ, msg_data = con.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])
            when = email_lib.utils.parsedate_to_datetime(msg.get("Date"))
            out.append(f"  {when.date().isoformat() if when else '?'} · "
                       f"{msg.get('From', '')} · "
                       f"{mail._decode_header(msg.get('Subject'))} — "
                       f"{mail._body_preview(msg, limit=PREVIEW)}")
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def _mail_text(query, limit):
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 6), 20))
    accounts = _accounts()
    if not accounts:
        return "No mail accounts are connected to Vira."
    parts = []
    for acct in accounts:
        addr = acct.get("email", "?")
        try:
            hits = (_mail_graph(addr, query, limit)
                    if acct.get("type") == "graph"
                    else _mail_imap(acct, query, limit))
            parts.append(f"{addr}:\n"
                         + ("\n".join(hits) if hits else "  no matches"))
        except Exception as e:  # noqa: BLE001 — one account never kills all
            parts.append(f"{addr}: unavailable ({str(e)[:120]})")
    return f"Mail search {query!r}:\n\n" + "\n\n".join(parts)


async def _t_mail_search(args):
    return _txt(await asyncio.to_thread(
        _mail_text, args.get("query"), args.get("limit")))


# ---------- iMessage thread ----------

def _thread_text(name, limit):
    matches = crm.search_people((name or "").strip(), limit=3)
    if not matches:
        return f"No CRM match for {name!r}."
    top = matches[0]
    limit = max(1, min(int(limit or 25), 60))
    msgs = imessage.thread_for_person(top["id"], limit)
    if not msgs:
        return f"No direct iMessage thread with {top['name']}."
    lines = [f"iMessage thread with {top['name']} "
             f"(last {len(msgs)} messages; text previews up to 300 characters):",
             "This is a recent slice, not the complete thread. Use thread_read with "
             f"person_id={top['id']} and follow its cursor for a date range; source_read "
             "opens each original message without the preview limit."]
    for msg in msgs:
        when = msg.get("when")
        stamp = (settings.strf(dt.datetime.fromisoformat(when), "%b %-d %-I:%M %p")
                 if when else "?")
        who = "Me" if msg.get("from_me") else top["name"]
        source = f"imessage:{msg['rowid']}" if msg.get("rowid") else ""
        if source:
            _remember_source(source, kind="imessage")
        lines.append(f"  [{stamp}] {who}: {msg.get('text', '')[:300]}" + (f" [source={source}]" if source else ""))
    return "\n".join(lines)


async def _t_imessage_thread(args):
    return _txt(await asyncio.to_thread(
        _thread_text, args.get("name"), args.get("limit")))


# ---------- semantic media search ----------

def _media_text(query, person, limit):
    from . import search as msearch  # deferred: first call loads models
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 10), 30))
    pid = None
    if person:
        matches = crm.search_people(person.strip(), limit=1)
        if not matches:
            return f"No CRM match for {person!r} to scope the search."
        pid = matches[0]["id"]
    results = msearch.search(q=query, pid=pid, limit=limit)
    if not results:
        return f"No matches for {query!r}."
    lines = [f"Media search {query!r} ({len(results)} hit(s)):"]
    for r in results:
        _remember_source(f"media:{r['seq']}", kind="media")
        ctx = r.get("context") or {}
        ctx_txt = f' — "{ctx.get("text", "")[:PREVIEW]}"' if ctx else ""
        lines.append(f"  [{r.get('kind')}] {r.get('name') or r.get('title')}"
                     f" · from {r.get('sender') or '?'}"
                     f" · thread: {r.get('person') or '?'}"
                     f" · {(r.get('when') or '')[:10]}{ctx_txt} [source=media:{r['seq']}]")
    return "\n".join(lines)


async def _t_media_search(args):
    return _txt(await asyncio.to_thread(
        _media_text, args.get("query"), args.get("person"),
        args.get("limit")))


# ---------- find: one query over all four databases ----------

def _find_text(query, limit):
    """The agent-facing twin of the Find window. An agent picking between
    four retrieval tools has the same problem the owner had with two
    search boxes — this is the one that sorts for itself."""
    from . import find
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 8), 25))
    from . import vault
    with vault.model_access():
        out = find.find(query, limit=limit)
    plan = out["plan"]
    head = [f"Find {query!r} — plan: {plan['why'] or 'no filters'}"
            f" (terms: {plan['text'] or '-'})"]
    head.append(json.dumps({"complete": out.get("complete", False),
                           "retrieval_mode": out.get("retrieval_mode", "text"),
                           "coverage": {db: {k: g.get(k) for k in (
                               "status", "complete", "error", "total", "total_exact", "next_cursor", "coverage")
                               if k in g} for db, g in out.get("groups", {}).items()}}, ensure_ascii=False))
    hit_count = 0
    for db in plan["databases"]:
        g = out["groups"].get(db) or {}
        rows = g.get("rows") or []
        if not rows:
            continue
        hit_count += len(rows)
        head.append(f"{db} ({g.get('count', len(rows))}):")
        for r in rows:
            when = (r.get("when") or "")[:10]
            if db == "notes":
                _remember_source("vault:" + r["path"], path=r["path"])
                head.append(f"  {r['path']} · {r.get('heading') or ''}"
                            f" · {when} — {(r.get('snippet') or '')[:PREVIEW]} [source=vault:{r['path']}]")
            elif db == "people":
                _remember_source("people:" + r["id"], kind="people")
                head.append(f"  {r['name']} ({r['id']})"
                            f" — {(r.get('snippet') or '')[:PREVIEW]}")
            elif db == "messages":
                _remember_source(f"message:{r['seq']}", kind="imessage" if r.get("source") == "imessage" else "mail")
                head.append(f"  [{r.get('source')}] {r.get('sender') or '?'}"
                            f" · {when} — {(r.get('text') or '')[:PREVIEW]} [source=message:{r['seq']}]")
            else:
                _remember_source(f"media:{r['seq']}", kind="media")
                head.append(f"  [{r.get('kind')}] "
                            f"{r.get('name') or r.get('title')}"
                            f" · from {r.get('sender') or '?'} · {when} [source=media:{r['seq']}]")
    if not hit_count:
        head.append(f"No matches for {query!r} in the completed searches. "
                    + ("Some sources could not be searched; this is not evidence of absence."
                       if not out.get("complete", True) else ""))
    return "\n".join(head)


async def _t_find(args):
    return _txt(await asyncio.to_thread(
        _find_text, args.get("query"), args.get("limit")))


# ---------- the knowledge vault ----------

def _vault_search_text(query, limit):
    from . import vault
    query = (query or "").strip()
    if not query:
        return "error: query is required"
    limit = max(1, min(int(limit or 8), 20))
    hits = vault.search(query, limit=limit, for_model=True)
    if not hits:
        st = vault.status()
        if not st.get("available"):
            return "The knowledge vault is not available on this machine."
        return f"No vault matches for {query!r}."
    lines = [f"Vault search {query!r} ({len(hits)} hit(s)):"]
    for h in hits:
        _remember_source("vault:" + h["path"], path=h["path"])
        lines.append(f"\n[{h['path']}] {h['heading']} [source=vault:{h['path']}]")
        lines.append("  " + h["text"][:500].replace("\n", "\n  "))
    lines.append("\nUse vault_note with a path above for the full note.")
    return "\n".join(lines)


async def _t_vault_search(args):
    return _txt(await asyncio.to_thread(
        _vault_search_text, args.get("query"), args.get("limit")))


def _vault_note_text(path):
    # A session reads notes into a context window, so this caller CAPS --
    # but honestly: qocha appends an in-band marker naming the real length,
    # because a model handed 41% of a transcript with no signal will
    # summarize the fragment as if it were the whole note.
    from . import vault
    from qocha.vault import NOTE_CAP
    try:
        from . import vaultwrite
        _remember_source("vault:" + str(path), path=path)
        full = vault.note_text((path or "").strip(), for_model=True)
        # A replacement hash is useful only when this tool can show the whole
        # note. Never let a capped read become an apparently safe full rewrite.
        header = f"[{path}]\nsha256: {vaultwrite.digest(full)}\n\n"
        if len(full) > NOTE_CAP or len(header) + len(full) > _text_cap():
            notice = (f"[{path}]\nRead-only excerpt: this note exceeds the tool's "
                      "complete-read budget. No update hash is supplied; use the "
                      f"source_read with source=vault:{path} to page through the full source; source_search finds exact passages.\n\n")
            return notice + full[:max(0, min(NOTE_CAP, _text_cap() - len(notice)))]
        return header + full
    except (ValueError, OSError) as e:
        return f"error: {e}"


async def _t_vault_note(args):
    return await _t_source_read({"source": "vault:" + str(args.get("path") or ""),
                                 "start": args.get("start", 0), "length": args.get("length"),
                                 "version": args.get("version")})


async def _t_vault_destinations(args):
    from . import vaultwrite
    return _txt(json.dumps(vaultwrite.destinations(for_model=True), ensure_ascii=False))


async def _vault_routing_decision(args, reason):
    """Surface an unresolved capture through the existing decision channel.

    The answer is a plan, never a multi-vault write operation. Returning it to
    the agent keeps all subsequent writes on the normal policy-checked path.
    """
    from . import vaultwrite
    visible = vaultwrite.destinations(for_model=True)
    connected = [s for s in visible if s.get("connected")]
    candidates = [s for s in connected if s.get("write_enabled")]
    options = []
    if len(candidates) <= 2:
        for spec in candidates:
            options.append({
                "label": f"Save only in {spec['name']}",
                "description": f"Keep this material in {spec['name']} ({spec['id']}) only.",
            })
    else:
        options.append({
            "label": "Choose one vault",
            "description": "Keep one copy. Use Other to name a vault from the list, "
                           "or choose this option to review that choice before saving.",
        })
    if len(candidates) > 1:
        options.extend([
            {"label": "Split by topic", "description":
             "Put each part in its appropriate vault; confirm the content split and destinations before saving."},
            {"label": "Keep references in both", "description":
             "Keep one main note and references in the other vault; confirm where the main note and references belong."},
            {"label": "Duplicate in both", "description":
             "Save copies of this requested material in both chosen vaults; confirm the pair if it is not already clear."},
        ])
    options.append({"label": "Leave unsaved", "description":
                    "Do not save this material or change any vault settings."})
    inventory = []
    for spec in connected:
        scope = "writable" if spec.get("write_enabled") else "read only"
        purpose = str(spec.get("purpose") or "").strip()
        inventory.append(f"- {spec['name']} ({spec['id']}; {scope})"
                         + (f": {purpose}" if purpose else ""))
    question = (f"Where should I save {str(args.get('title') or 'this note').strip()!r}? "
                f"{reason}. Nothing has been saved.\n\n"
                + ("Connected vaults available to this assistant:\n" + "\n".join(inventory)
                   if inventory else "No connected vault is available to this assistant.")
                + "\n\nChoose how to file this material. Use Other to name destinations "
                  "and explain the split or references when needed.")
    result = {
        "status": "needs_vault_choice", "saved": False,
        "question": question, "options": options,
        "destinations": candidates,
        "instructions": "Do not write after cancellation, a skipped question, or an unanswered question. "
                        "A split, reference, or duplicate choice authorizes only the current requested material. "
                        "If destinations or contents remain unclear, show the proposed filing plan with ask_owner "
                        "before writing. Otherwise use explicit destinations on the normal capture/update tools, "
                        "respect every vault's permissions, and report the actual saved paths. "
                        "This response itself has not saved any files.",
    }
    channel = _OWNER_CHANNEL.get() or _ASK
    if channel is not None:
        result["owner_decision"] = await channel(question, options, True)
        result["status"] = "vault_choice_received"
    else:
        result["instructions"] = ("No owner question channel is available. Ask the owner "
                                  "to resolve the filing choice and leave this material unsaved. "
                                  + result["instructions"])
    return _txt(json.dumps(result, ensure_ascii=False))


async def _t_vault_capture(args):
    from . import vaultwrite
    route, context = _VAULT_ROUTE.get()
    try:
        receipt = await asyncio.to_thread(
            vaultwrite.capture, args.get("title"), args.get("text"),
            args.get("destination") or (None if args.get("context") else route),
            args.get("context") or context, for_model=True)
        return _txt(json.dumps(receipt, ensure_ascii=False))
    except vaultwrite.VaultRoutingRequired as exc:
        return await _vault_routing_decision(args, str(exc))
    except (ValueError, OSError) as exc:
        return _txt(f"error: {exc}")


async def _t_vault_update(args):
    from . import vault, vaultwrite
    from qocha.vault import NOTE_CAP
    route, _context = _VAULT_ROUTE.get()
    try:
        public = str(args.get("path") or "")
        requested = args.get("destination") or (None if public.startswith("@") else route)
        if requested and not public.startswith("@"):
            spec = vaultwrite.resolve_destination(requested, operation="update", for_model=True)
            public = vault._public_path(spec, public)
        full = vault.note_text(public, for_model=True)
        header = f"[{public}]\nsha256: {vaultwrite.digest(full)}\n\n"
        if len(full) > NOTE_CAP or len(header) + len(full) > _text_cap():
            raise ValueError("note exceeds the complete-read budget; use the local note editor")
        receipt = await asyncio.to_thread(
            vaultwrite.update, args.get("path"), args.get("text"),
            args.get("expected_hash"), args.get("destination") or (
                None if str(args.get("path") or "").startswith("@") else route),
            for_model=True)
        return _txt(json.dumps(receipt, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        return _txt(f"error: {exc}")


# ---------- the ideas backlog ----------

def _list_ideas_text(status):
    from . import ideas
    items = ideas.list_items()
    status = (status or "").strip().lower()
    if status:
        items = [i for i in items if i["status"] == status]
    if not items:
        return "No ideas match."
    lines = [f"Ideas backlog ({len(items)} item(s)):"]
    for i in items[:60]:
        lines.append(f"  [{i['status']}] ({i.get('project', '?')}) "
                     f"{i['text'][:180]}")
    return "\n".join(lines)


async def _t_list_ideas(args):
    return _txt(await asyncio.to_thread(_list_ideas_text,
                                        args.get("status")))


def _near_duplicate(text, project, items):
    """The strongest near-duplicate already on the backlog, or None.

    NEVER raises: a similarity layer that is down (no Ollama, no numpy)
    must not be able to block a legitimate proposal, so every failure here
    falls through to staging. Missing a repeat costs one card in a queue
    the owner reviews anyway; swallowing a good idea is invisible."""
    from . import ideatags
    try:
        hits = ideatags.check_candidate(text, project, items=items,
                                        limit=1)["matches"]
    except Exception:                    # noqa: BLE001 — degrade, never block
        return None
    return hits[0] if hits else None


def _propose_idea_text(text, project, why):
    from . import ideas
    text = (text or "").strip()
    if not text:
        return "error: idea text is required"
    items = ideas.list_items()
    # "deferred" counts here: the owner saw that proposal and set it aside,
    # so re-staging it is exactly what Defer exists to prevent.
    dupes = [i for i in items
             if i["status"] in ("proposed", "open", "on-hold", "deferred")
             and i["text"].strip().lower() == text.lower()]
    if dupes:
        return ("Not staged — an identical idea is already on the backlog"
                + (" (deferred by the owner)."
                   if dupes[0]["status"] == "deferred" else "."))
    # Same wording is the easy case; the muse repeats itself by REPHRASING.
    # The refusal names the match, because a refusal that only says no
    # invites a blind retry of the same idea in different words.
    near = _near_duplicate(text, project, items)
    if near:
        why_ = "; ".join(near.get("reasons") or []) or "same subject"
        return ("Not staged — this reads as a near-duplicate of an idea "
                f"already on the backlog: [{near['id']}] "
                f"({near.get('project') or '?'}) {near['text'][:160]} "
                f"({why_}). Propose something genuinely different rather "
                "than rewording this one.")
    item = ideas.add(text, status="proposed", source="muse",
                     note=(why or "").strip()[:400], project=project)
    return (f"Staged for the owner's approval: [{item['id']}] "
            f"({item['project']}) {item['text'][:160]}")


async def _t_propose_idea(args):
    return _txt(await asyncio.to_thread(
        _propose_idea_text, args.get("text"), args.get("project"),
        args.get("why")))


# ---------- the owner channel ----------
# A session that needs a DECISION had no way to raise one. Permission
# requests got a clickable card; a question got a line of prose above a
# free-text box, which on a phone is easy to miss entirely — so the runs
# that stopped to ask were the runs that quietly never finished. The runner
# binds its own handler here (one runner supervises one session, so there
# is no ambiguity about whose transcript the question belongs in); unbound —
# the legacy in-process path, or a bare import — the tool says so plainly
# instead of pretending to have asked.
_ASK = None


def _update_person_profile_text(person, relationship_summary, how_we_met):
    """The explore session's write-back: a refreshed dossier description,
    through the same quarantined writer the refresh button uses."""
    from . import data as crm
    pid = (person or "").strip()
    if pid not in crm._load()["by_id"]:
        hits = crm.search_people(q=pid, limit=2)
        if len(hits) != 1:
            return (f"No unique person for {person!r} — "
                    f"{len(hits)} matches. Pass the person id.")
        pid = hits[0]["id"]
    try:
        prof = crm.save_profile_refresh(
            pid, relationship_summary or "",
            how_met=how_we_met or "", reason="vira-refresh-explore")
    except ValueError as e:
        return f"Refused: {e}"
    except crm.ProfileCorruptError as e:
        return f"Refused: {e}"
    return (f"Profile refreshed for {prof.get('name') or pid} ({pid}): "
            f"description updated"
            + (", how_we_met set" if (how_we_met or "").strip() else "")
            + f"; refresh #{prof.get('refresh_count')}.")


def bind_ask(fn):
    """Called by the runner with an async (question, options, allow_text)."""
    global _ASK
    _ASK = fn


def parse_options(raw):
    """Options as [{label, description}].

    An option needs the sentence that says what CHOOSING it means — a bare
    label asks the owner to decide from three words, which on a phone (where
    the transcript has already scrolled away) is not a decision they can
    actually make. So JSON is the documented shape. A plain '|' list still
    parses, because a model that reaches for the simple form should get a
    usable card rather than an error.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    out = []
    if raw.startswith("["):
        try:
            for o in json.loads(raw):
                if isinstance(o, str) and o.strip():
                    out.append({"label": o.strip(), "description": ""})
                elif isinstance(o, dict) and str(o.get("label", "")).strip():
                    out.append({
                        "label": str(o["label"]).strip(),
                        "description": str(o.get("description") or "").strip(),
                    })
            return out[:6]
        except (json.JSONDecodeError, TypeError):
            pass          # fall through to the plain list
    for part in raw.split("|"):
        # tolerate "Label :: description" too — the shape a model reaches for
        # when it wants a description without composing JSON
        label, _, desc = part.partition("::")
        if label.strip():
            out.append({"label": label.strip(), "description": desc.strip()})
    return out[:6]


async def _t_ask_owner(args):
    channel = _OWNER_CHANNEL.get() or _ASK
    if channel is None:
        return _txt("No owner channel is available in this session. Do not "
                    "guess: stop and put the question in your final report.")
    return _txt(await channel(args.get("question"),
                             parse_options(args.get("options")),
                             str(args.get("allow_text", "true")).lower()
                             != "false"))


def _update_module_map_text(modules_json):
    from . import modulemap
    try:
        mods = json.loads(modules_json or "")
    except json.JSONDecodeError as e:
        return f"error: modules_json is not valid JSON ({e})"
    try:
        return modulemap.replace_modules(mods)
    except ValueError as e:
        return f"error: {e}"


async def _t_update_module_map(args):
    return _txt(await asyncio.to_thread(
        _update_module_map_text, args.get("modules_json")))


# ---------- first-run setup writes (server/frontdoor.py) ----------
# Both are dispatched only by a module's front door, and both exist so the
# setup session never touches config or the served page tree by hand.

def _create_reading_room_text(slug, title, subtitle, items_json, destination=None):
    from . import readingroom
    try:
        items = json.loads(items_json or "")
    except json.JSONDecodeError as e:
        return (f"error: items_json is not valid JSON ({e}). Pass the whole "
                "item array as a single JSON string.")
    try:
        res = readingroom.build(slug, title, subtitle or "", items)
    except readingroom.BuildError as e:
        return f"error: {e}"
    except OSError as e:
        return f"error: could not write the room ({e})"
    # Project into the vault HERE, not inside build(). build() is a pure
    # store write; hanging a cross-boundary write off it means any caller
    # that never heard of the vault — a test, a fixture, a future
    # importer — writes to the owner's real Obsidian vault. That is not
    # hypothetical: it put 11 fixture rooms in the live vault on
    # 2026-07-29. The sync belongs to the real entry points.
    from . import fullingest, roomvault
    destination = destination or _VAULT_ROUTE.get()[0]
    if destination:
        try:
            fullingest.set_destination(slug, destination)
        except (ValueError, OSError) as exc:
            return f"Room saved locally; vault ingestion refused: {exc}"
    synced = roomvault.sync(slug)
    line = readingroom.summary_line(res)
    return line + (f" {roomvault.summary_line(synced)}" if synced else "")


async def _t_create_reading_room(args):
    return _txt(await asyncio.to_thread(
        _create_reading_room_text, args.get("slug"), args.get("title"),
        args.get("subtitle"), args.get("items_json"), args.get("destination")))


def _add_reading_room_items_text(slug, items_json, destination=None):
    from . import readingroom
    try:
        items = json.loads(items_json or "")
    except json.JSONDecodeError as e:
        return (f"error: items_json is not valid JSON ({e}). Pass ONLY the "
                "new items as a JSON array.")
    try:
        res = readingroom.merge_items(slug, items)
    except KeyError:
        return f"error: no room named {slug!r} — create_reading_room builds one"
    except readingroom.BuildError as e:
        return f"error: {e}"
    except OSError as e:
        return f"error: could not write the room ({e})"
    # Same cross-boundary rule as create: the vault projection hangs off the
    # real entry point, never off the store write itself.
    from . import fullingest, roomvault
    destination = destination or _VAULT_ROUTE.get()[0]
    if destination:
        try:
            fullingest.set_destination(slug, destination)
        except (ValueError, OSError) as exc:
            return f"Room saved locally; vault ingestion refused: {exc}"
    synced = roomvault.sync(slug)
    line = (f"merged into {slug}: {res['added']} added, "
            f"{res['items']} items total."
            + (" Added: " + "; ".join(res["titles"][:12]) if res["titles"]
               else " Nothing new — every item was already in the room."))
    return line + (f" {roomvault.summary_line(synced)}" if synced else "")


async def _t_add_reading_room_items(args):
    return _txt(await asyncio.to_thread(
        _add_reading_room_items_text, args.get("slug"),
        args.get("items_json"), args.get("destination")))


def _configure_applications_text(config_json):
    from . import frontdoor
    try:
        res = frontdoor.configure_applications(config_json)
    except frontdoor.ConfigError as e:
        return f"error: {e}"
    return frontdoor.configure_summary(res)


async def _t_configure_applications(args):
    return _txt(await asyncio.to_thread(
        _configure_applications_text, args.get("config_json")))


def _record_role_scores_text(scores_json):
    """The write path for job-role scores.

    A BAD ENTRY LOSES ITSELF, NEVER THE BATCH. A scoring session deep-reads
    up to forty postings before it files anything, so refusing the whole
    array over one malformed tier would throw away the expensive part of the
    run. Each entry is validated on its own; the reply names every refusal
    with its reason so the session can fix those and re-file just them.
    """
    from . import jobscores
    try:
        rows = json.loads(scores_json or "")
    except json.JSONDecodeError as e:
        return (f"error: scores_json is not valid JSON ({e}). Pass a JSON "
                "array of score objects.")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        return ("error: scores_json must be a non-empty JSON array of score "
                "objects.")

    try:
        jobscores._refuse_if_passive()
    except PermissionError as e:
        return f"error: {e}."

    known = jobscores.known_uids()
    wrote, failed = [], []
    for row in rows:
        uid = str((row or {}).get("uid") or "?") if isinstance(row, dict) \
            else "?"
        try:
            rec = jobscores.write(row, known=known or None)
        except jobscores.ScoreError as e:
            failed.append(f"{uid}: {e}")
        except OSError as e:
            failed.append(f"{uid}: could not be written ({e})")
        else:
            wrote.append(rec["uid"])

    line = (f"recorded {len(wrote)} score(s): {', '.join(wrote[:12])}"
            + (f" (+{len(wrote) - 12} more)" if len(wrote) > 12 else "")
            if wrote else "recorded nothing")
    if failed:
        line += (f". {len(failed)} refused — fix and re-file only these: "
                 + " | ".join(failed[:8]))
    return line


async def _t_record_role_scores(args):
    return _txt(await asyncio.to_thread(
        _record_role_scores_text, args.get("scores_json")))


# ---------- the SDK server ----------

# (name, description, input schema, handler). Schemas use the SDK's simple
# name->type form; handlers tolerate missing optional keys.
TOOL_SPECS = [
    ("answer_sources", "List this conversation's approved sources, current exposure policies and freshness basis.", {}, _t_answer_sources),
    ("source_read", "Read an exact source page with an immutable evidence handle, original date, provenance, full length and continuation. Pass version on every continued read.",
     {"source": str, "start": int, "length": int, "version": str}, _t_source_read),
    ("sources_read", "Batch exact source reads. Each request has source, start, length and optional version; follow per-source and batch continuations.",
     {"requests": list}, _t_sources_read),
    ("source_search", "Find literal text inside one full versioned source. Returns exact match spans, surrounding context and continuation.",
     {"source": str, "query": str, "version": str, "start": int, "limit": int}, _t_source_search),
    ("message_context", "Read exact messages before and after an iMessage rowid. If the message belongs to multiple chats, select an explicitly returned chat_id.",
     {"rowid": int, "before": int, "after": int, "chat_id": int}, _t_message_context),
    ("thread_read", "Read an exact chat or person's direct thread over a date range, start inclusive and end exclusive. Results include original timestamps and evidence; follow cursor to exhaust the range.",
     {"person_id": str, "chat_id": int, "start_date": str, "end_date": str, "cursor": str, "limit": int}, _t_thread_read),
    ("vault_query", "Search, enumerate or count approved vault notes with complete/cursor/coverage receipts. Set mode to search, enumerate or count; dates use date_field explicitly.",
     {"query": str, "mode": str, "cursor": str, "limit": int, "since": str, "until": str, "order": str, "date_field": str}, _t_vault_query),
    ("calendar",
     "The owner's calendar for the next N days: local macOS calendars "
     "(personal + family + birthdays) merged with the M365 work calendar. "
     "Use for any appointment/schedule/availability question.",
     {"days": int}, _t_calendar),
    ("daily_brief",
     "The owner's full daily brief: today/tomorrow calendar, who is "
     "waiting on a reply, open relationship loops, contacts going quiet, "
     "subscription renewals, queued drafts, triage count.",
     {}, _t_daily_brief),
    ("crm_lookup",
     "CRM dossier for a person by name: role, company, relationship, "
     "conversation hooks, open loops, contact activity.",
     {"name": str}, _t_crm_lookup),
    ("circles",
     "The owner's social circles as the Visual Network reads them: each "
     "circle's name, how the owner is connected to it, how its members "
     "connect to each other, who anchors it, its members, and what "
     "changed recently (people joining, new shared group chats).",
     {}, _t_circles),
    ("mail_search",
     "Search the owner's connected mailboxes (M365 work + personal Gmail) "
     "for messages matching a query. Returns date, sender, subject, "
     "preview.",
     {"query": str, "limit": int}, _t_mail_search),
    ("imessage_thread",
     "Recent direct iMessage conversation with a person by name, both "
     "directions, newest last.",
     {"name": str, "limit": int}, _t_imessage_thread),
    ("find",
     "ONE search over all four of the owner's databases at once: vault "
     "notes, shared media (photos/videos/docs/links), CRM people, and the "
     "text of iMessage and mail. Reads dates, names, 'most recent', "
     "filenames and quoted phrases out of the query and applies them as "
     "filters. Prefer this over the single-corpus tools unless you know "
     "exactly which database holds the answer.",
     {"query": str, "limit": int}, _t_find),
    ("media_search",
     "Text search over everything ever shared with the owner in "
     "iMessage (photos, videos, documents, links, voice memos) — by "
     "content, OCR text, captions. Optionally scoped to one person. "
     "Interactive text search does not cold-load an image model.",
     {"query": str, "person": str, "limit": int}, _t_media_search),
    ("vault_destinations",
     "Inspect connected vault IDs, purpose/context routes, capture folders and writable scopes. "
     "Use before saving; match the requested material to these purposes and choose an explicit "
     "destination only when clear. For unclear or overlapping purposes use ask_owner before writing, "
     "with options for one vault, splitting by topic, references in both, duplicates in both, and leaving unsaved.",
     {}, _t_vault_destinations),
    ("vault_capture",
     "Save an authorized new idea/context note in a vault's configured capture inbox. "
     "Pass an explicit destination ID/name or configured context key; otherwise the configured "
     "fallback applies. An unresolved implicit route asks the owner when the session supports "
     "questions and returns a filing decision without writing. Resolve that decision before "
     "explicit scoped saves. Successful saves return the actual source-aware note path and sha256. No publication.",
     {"title": str, "text": str, "destination": str, "context": str}, _t_vault_capture),
    ("vault_update",
     "Update an authorized existing Markdown note within configured writable folders. "
     "First read it with vault_note; pass the full replacement text and current sha256 as "
     "expected_hash. Preserve existing content/conventions. Protected folders and stale hashes fail.",
     {"path": str, "text": str, "expected_hash": str, "destination": str}, _t_vault_update),
    ("vault_search",
     "Search the owner's knowledge vault (thousands of Obsidian notes on "
     "companies, deals, people, decisions, sessions). Returns excerpt "
     "chunks with note paths — follow up with vault_note for a full note.",
     {"query": str, "limit": int}, _t_vault_search),
    ("vault_note",
     "Read one versioned page of a note from the owner's knowledge vault by its path "
     "(as returned by vault_search). Follow continuation for the complete text.",
     {"path": str, "start": int, "length": int, "version": str}, _t_vault_note),
    ("list_ideas",
     "The owner's ideas backlog (cross-project). Optional status filter: "
     "proposed | open | on-hold | deferred | done | dropped.",
     {"status": str}, _t_list_ideas),
    ("propose_idea",
     "STAGE a new idea on the owner's backlog as status 'proposed' — it "
     "runs only if the owner approves it. Use for genuinely new, concrete, "
     "buildable ideas; include the project it belongs to and a short "
     "'why now' rationale. Refused, with the match named, when the backlog "
     "already carries the same idea — including a reworded one.",
     {"text": str, "project": str, "why": str}, _t_propose_idea),
    ("update_module_map",
     "Replace Vira's system-map registry (the Modules atlas page's data) "
     "with an updated FULL module list. Pass the complete JSON array as "
     "modules_json — every module, not a diff. Validated server-side: "
     "stable kebab-case ids, layer in source/store/engine/surface, "
     "name+what required; a payload that drops too many existing modules "
     "is refused. Use only when refreshing the system map.",
     {"modules_json": str}, _t_update_module_map),
    ("create_reading_room",
     "Build a reading room — a researched consumption queue — live in the "
     "owner's Reader. Pass the COMPLETE item array as items_json (a JSON "
     "string). Each item: title (required), url, date (YYYY, YYYY-MM or "
     "YYYY-MM-DD), type, mode watch|listen|read, prio P1|P2|P3, people [], "
     "venue, note, why, status MISSING|PARTIAL, vault, pay. The server "
     "validates, dedupes on a stable id and writes the room's data store — "
     "never write reading-room files yourself. Rebuilding an existing slug "
     "is a repass: the owner's done-marks are preserved and they are "
     "notified of any items the rebuild added.",
     {"slug": str, "title": str, "subtitle": str, "items_json": str, "destination": str},
     _t_create_reading_room),
    ("add_reading_room_items",
     "Add NEW items to an existing reading room without re-emitting it — "
     "the refresh write path. Pass the room's slug and ONLY the new items "
     "as items_json (a JSON array, same item shape as create_reading_room). "
     "The server validates, merges by stable URL-derived id (a duplicate "
     "of an existing item is dropped, so over-including is safe), keeps "
     "every existing item and the owner's done-marks untouched, and "
     "notifies the owner of what arrived. Never rebuild a whole room just "
     "to add to it.",
     {"slug": str, "items_json": str, "destination": str}, _t_add_reading_room_items),
    ("configure_applications",
     "Apply first-run setup for the Applications module. Pass config_json "
     "as a JSON string: {record_dir, locations: [str], "
     "remote_regions: [str], remote_ok: bool, "
     "boards: [{company, ats, slug, query, location, note}]}. ats is "
     "greenhouse|ashby|lever|microsoft|google|manual. The server creates "
     "the record and universe directories, writes the config keys, "
     "registers every board, and starts the first poll — never edit "
     "data/config.json or the boards registry by hand. An EMPTY locations "
     "list means unfiltered; never guess a city. remote_regions separately "
     "lists accepted employer-written remote territories; never infer it "
     "from a city.",
     {"config_json": str}, _t_configure_applications),
    ("record_role_scores",
     "File job-role scores into the candidate universe. Pass scores_json "
     "as a JSON ARRAY of objects: uid (the role's board uid, required), "
     "fit 0-100 (narrative resonance), screen 0-100 (screening "
     "probability — the two-score discipline, kept separate), tier and "
     "final_tier one of 1|2|3|pass|cut, lane, why_fit (required, under "
     "1200 chars), lead_with, caveat, comp_note, verdict "
     "confirm|demote|flag. The server validates each entry, stamps when it "
     "was scored and against which canon, and writes one file per role — "
     "NEVER write a *-raw-scores.json file yourself. Re-filing a uid "
     "REPLACES its score and keeps the previous one recoverable, so this "
     "is also how a rescore lands. A refused entry is named with its "
     "reason and loses only itself.",
     {"scores_json": str}, _t_record_role_scores),
    ("update_person_profile",
     "REPLACE a CRM person's dossier description with a refreshed one you "
     "researched. person is the person id (preferred) or an unambiguous "
     "name; relationship_summary is 3-6 grounded sentences with evidence "
     "dates in brackets like [2019-04-02]; how_we_met is one sentence or "
     "'' to leave it unchanged. The previous description is kept and the "
     "refresh is stamped. Call it once, at the end, with your final text "
     "— never with a draft.",
     {"person": str, "relationship_summary": str, "how_we_met": str},
     _update_person_profile_text),
    ("ask_owner",
     "Ask the owner a question and WAIT for the answer. Use this the "
     "moment a decision is genuinely theirs — which of two approaches to "
     "take, whether to keep going down a path, anything you would "
     "otherwise guess at or leave half-done. It raises a card with "
     "numbered options in the app and on their phone, so it reaches them; "
     "writing the question into your final report does NOT. options is a "
     "JSON array (up to 6) of {\"label\", \"description\"}, e.g. "
     "[{\"label\":\"Fold it in\",\"description\":\"One window, less to "
     "scan; the old deep links keep working.\"}]. ALWAYS write the "
     "description: the owner is often reading this on a phone with the "
     "transcript scrolled away, and a bare label asks them to decide from "
     "three words. Say what choosing it actually means and what it costs. "
     "Never call this for permission to use a tool (that already has its "
     "own card), and never ask what you can determine by reading the code.",
     {"question": str, "options": str, "allow_text": str}, _t_ask_owner),
]

TOOL_NAMES = [f"mcp__vira__{name}" for name, *_ in TOOL_SPECS]

# The tools on this server that MUTATE. Every other spec renders text from
# an existing loader, which is what makes the whole server auto-allowed in
# interactive sessions (runner.Runner.auto_allow). Read-only sessions —
# judges, circuit read stages — must be denied these, so the list lives
# here beside the tools rather than as a hand-maintained copy in
# session.py that the next write tool would quietly fall out of.
# propose_idea is deliberately absent: it STAGES to a queue the owner must
# approve, which is why it was safe to ship as a read-adjacent tool.
WRITE_TOOLS = {
    "mcp__vira__vault_capture",
    "mcp__vira__vault_update",
    "mcp__vira__update_module_map",
    "mcp__vira__create_reading_room",
    "mcp__vira__add_reading_room_items",
    "mcp__vira__configure_applications",
    "mcp__vira__record_role_scores",
    "mcp__vira__update_person_profile",
}

_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def json_input_schema(simple):
    """Translate the SDK's compact name->type schema to portable JSON Schema.

    TOOL_SPECS remains the single registry. Claude's adapter consumes the
    compact form it always has; Codex and future function/MCP adapters consume
    this standards-shaped view. Fields stay optional because the established
    handlers deliberately tolerate missing optional keys.
    """
    props = {}
    for name, pytype in (simple or {}).items():
        props[name] = {"type": _JSON_TYPES.get(pytype, "string")}
        if name == "requests" and pytype is list:
            props[name]["items"] = {"type": "object", "properties": {
                "source": {"type": "string"}, "start": {"type": "integer"},
                "length": {"type": "integer"}, "version": {"type": "string"}},
                "required": ["source"], "additionalProperties": False}
    return {"type": "object", "properties": props,
            "additionalProperties": False}


def dynamic_tool_specs(read_only=False):
    """Codex App Server dynamic-tool namespace derived from TOOL_SPECS."""
    tools = []
    for name, description, schema, _handler in TOOL_SPECS:
        fqname = f"mcp__vira__{name}"
        if read_only and fqname in WRITE_TOOLS:
            continue
        tools.append({"type": "function", "name": name,
                      "description": description,
                      "inputSchema": json_input_schema(schema),
                      "deferLoading": False})
    return [{"type": "namespace", "name": "vira",
             "description": "Vira's governed local data and action tools",
             "tools": tools}]


def has_tool(name):
    plain = str(name or "").removeprefix("mcp__vira__")
    return any(tool_name == plain for tool_name, *_ in TOOL_SPECS)


async def invoke(name, arguments=None, read_only=False, ask_owner=None,
                 vault_destination=None, vault_context=None, runtime=None, receipt=None):
    """Call one registered Vira tool through a provider-neutral adapter."""
    plain = str(name or "").removeprefix("mcp__vira__")
    for tool_name, _description, _schema, handler in TOOL_SPECS:
        if tool_name != plain:
            continue
        fqname = f"mcp__vira__{tool_name}"
        if read_only and fqname in WRITE_TOOLS:
            return _txt(f"error: {fqname} is unavailable in a read-only session")
        args = arguments if isinstance(arguments, dict) else {}
        token = _VAULT_ROUTE.set((vault_destination, vault_context))
        owner_token = _OWNER_CHANNEL.set(ask_owner)
        source_token = _READ_SOURCES.set([])
        try:
            from . import answer_runtime, answer_sources, retrieval, vault
            with answer_runtime.scope(runtime, observer=receipt), answer_runtime.tool_call(fqname, args) as record:
                approved = [s["id"] for s in answer_sources.enumerate_sources(for_model=True)["sources"] if s["allowed"]]
                with vault.model_access(), retrieval.source_scope(approved or ["__no_sources__"], for_model=True,
                                                                  deadline=time.monotonic() + retrieval.DEFAULT_BUDGET_S):
                    if answer_sources._scope() and plain in {"calendar", "daily_brief", "list_ideas"}:
                        raise ValueError("This cross-source tool is unavailable in a narrowed evidence scope. "
                                         "Use the selected source readers or start a chat with all enabled sources.")
                    kind = {"imessage_thread": "imessage", "mail_search": "mail", "media_search": "media",
                            "crm_lookup": "people", "circles": "people"}.get(plain)
                    if kind:
                        answer_sources._require(kind)
                    if plain == "imessage_thread" or (plain == "media_search" and args.get("person")):
                        answer_sources._require("people")
                    result = await handler(args)
                    # Existing text tools become citable too. This snapshot is
                    # explicitly a derived tool rendering, never a full source.
                    if plain in {"find", "vault_search", "imessage_thread", "media_search", "mail_search", "crm_lookup", "circles"}:
                        sources = _READ_SOURCES.get() or []
                        body = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
                        proof = await asyncio.to_thread(answer_sources.capture_result, body, tool_name=plain,
                                                       scope=json.dumps(args, sort_keys=True),
                                                       policy_paths=[s["path"] for s in sources if s.get("path")],
                                                       policy_kinds=list({s["kind"] for s in sources if s.get("kind")} | ({kind} if kind else set())))
                        result["evidence"] = {k: v for k, v in proof.items() if k != "text"}
                        result["content"].insert(0, {"type": "text", "text": json.dumps({
                            "evidence_handle": proof["evidence_handle"], "provenance": "derived tool rendering",
                            "sources": list(dict.fromkeys(s["source"] for s in sources)),
                            "full_length": proof["full_length"], "span": proof["span"], "truncated": proof["truncated"],
                            "continuation": proof["continuation"]}, ensure_ascii=False)})
                    record["result"] = result
                    return result
        finally:
            _READ_SOURCES.reset(source_token)
            _OWNER_CHANNEL.reset(owner_token)
            _VAULT_ROUTE.reset(token)
    return _txt(f"error: unknown Vira tool {plain or '(blank)'}")


def function_tool_specs(read_only=False):
    """Portable function definitions for HTTP model adapters."""
    out = []
    for name, description, schema, _handler in TOOL_SPECS:
        fqname = f"mcp__vira__{name}"
        if read_only and fqname in WRITE_TOOLS:
            continue
        out.append({"type": "function", "name": name,
                    "description": description,
                    "parameters": json_input_schema(schema)})
    return out

_server = None


def sdk_server(vault_destination=None, vault_context=None, read_only=False, runtime=None, receipt=None):
    """The in-process MCP server config for ClaudeAgentOptions.mcp_servers,
    or None when the SDK is unavailable (legacy fallback path)."""
    global _server
    if not SDK_AVAILABLE:
        return None
    def wrapped(name):
        async def handler(args):
            return await invoke(name, args, read_only=read_only,
                                vault_destination=vault_destination,
                                vault_context=vault_context, runtime=runtime, receipt=receipt)
        return handler

    if vault_destination or vault_context or read_only or runtime is not None or receipt is not None:
        return create_sdk_mcp_server(
            name="vira", tools=[tool(n, d, json_input_schema(s))(wrapped(n))
                                for n, d, s, _h in TOOL_SPECS
                                if not read_only or f"mcp__vira__{n}" not in WRITE_TOOLS])
    if _server is None:
        _server = create_sdk_mcp_server(
            name="vira", tools=[tool(n, d, json_input_schema(s))(wrapped(n))
                                for n, d, s, _h in TOOL_SPECS])
    return _server

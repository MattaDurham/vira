"""Person-profile refresh: the dossier description, on demand.

The CRM's relationship summaries were synthesized once (most in mid-2026)
and have been aging since — and none of them ever saw email, so anything
that happened before the iMessage archive starts (late 2015) reads as
unknown. Two modes behind one button on the person page:

  current — one model pass over what Vira already holds: the profile,
      the master evidence, the recent direct thread, and the person's
      indexed email bodies (newest AND oldest — the oldest mail is where
      a pre-2016 history finally becomes visible once the mail backlog
      is indexed). Rewrites relationship_summary, and how_we_met only
      when the material actually answers it. Seconds, no session.
  explore — a live agent session with the vira tools (live mailbox
      search included), told to actually dig for what the one-pass can't
      see, and to file what it finds through the guarded
      update_person_profile write tool. Minutes, runs in Work.

Both write through data.save_profile_refresh: quarantined read-modify-
write, previous summary kept one deep, refresh provenance stamped.
"""
import json

from . import data as crm
from . import imessage, settings, suggest, textindex

# WHAT ONE REFRESH MAY CARRY IS ASKED, NOT TYPED.
#
# The two block caps below were 6,000 and 2,500 characters, typed with this
# module in 2026-07-28 with no reason written down, against a backend that
# reports a 1,000,000-token context window in its own response JSON. A
# dossier truncated at 6,000 characters does not fail: it produces a
# confident summary written from half the material, which is exactly why it
# survived. modelbudget converts a budget CLASS into characters against
# whatever backend is actually answering.
#
# "interactive": the owner pressed a button on the person page and is
# watching it spin, so latency is the binding constraint here rather than
# capacity — modelbudget's class ceiling is what keeps that true when the
# window is enormous.
BUDGET_CLASS = "interactive"
# Five blocks of material reach the model and they share the budget evenly:
# the profile Vira already holds, the master evidence, the recent thread,
# the newest mail, and the oldest mail. Even shares because WHICH block
# carries the answer changes per person — a contact with no email is all
# thread, and a pre-2016 friendship is the other way round.
BUDGET_PARTS = 5

# HOW MANY items to gather stays this module's own decision — the contract
# stated in modelbudget.split, where a module says how many passages it
# wants and is told how large each may be. These are not capacity numbers:
# they name the SHAPE of the evidence (the recent thread, plus mail from
# BOTH ends, because the oldest mail is the only place a pre-2016 history
# exists). The budget below is what actually bounds the prompt.
THREAD_N = 60          # recent direct messages in the current-mode context
MAIL_RECENT_N = 25     # newest indexed mail bodies with this person
MAIL_OLDEST_N = 15     # oldest — where the pre-archive history lives
# Per-line previews, deliberately left literal (the viratools.PREVIEW
# precedent): these bound ONE message or ONE mail body, and past a few
# hundred characters a mail line is quoted history rather than more signal.
# What the budget decides is how many of them survive into the prompt.
SNIPPET = 280      # one mail body
MSG_CHARS = 200    # one iMessage — the same preview rule

# The pre-seam size, used ONLY when the seam itself cannot answer. A refresh
# must never fail over its own sizing.
_FALLBACK_BLOCK = 2500


def _block_chars():
    """Characters each material block may carry, asked of the backend that
    will answer this pass."""
    from . import modelbudget
    try:
        return modelbudget.split(BUDGET_CLASS, BUDGET_PARTS)[1]
    except Exception:  # noqa: BLE001 — sizing must never break a refresh
        return _FALLBACK_BLOCK


def _fit(lines, cap, tail=False):
    """As many WHOLE lines as the budget carries.

    Whole lines, because truncating the joined block cuts a message in half
    and reads as corrupted evidence. From the END when the lines are
    chronological (`tail`): a thread is oldest-first, so what has to survive
    a squeeze is the most recent exchange, not the first one.
    """
    seq = list(reversed(lines)) if tail else list(lines)
    out, used = [], 0
    for ln in seq:
        if out and used + len(ln) + 1 > cap:
            break
        out.append(ln)
        used += len(ln) + 1
    return list(reversed(out)) if tail else out


def _mail_lines(pid, order, limit):
    try:
        rows = textindex.search("", person=pid, source="email",
                                order=order, limit=limit)
    except Exception:  # noqa: BLE001 — no mail index is a thinner context,
        return []      # never a failed refresh
    out = []
    for r in rows:
        who = "me" if r.get("from_me") else "them"
        when = (r.get("when") or "")[:10]
        subj = (r.get("subject") or "").strip()
        text = (r.get("text") or "").strip()[:SNIPPET]
        out.append(f"[{when}] [{who}] {subj + ' — ' if subj else ''}{text}")
    return out


def _context(pid):
    detail = crm.get_person(pid)
    if not detail:
        raise KeyError(pid)
    p = detail["person"]
    prof = detail.get("profile") or {}
    master = detail.get("master") or {}
    prof_slim = {k: prof.get(k) for k in (
        "relationship_class", "relationship_summary", "how_we_met",
        "topics", "personal_facts", "comms_style", "cadence", "stats",
        "hooks", "open_loops") if prof.get(k)}
    thread = imessage.thread_for_person(pid, THREAD_N)
    return {
        "person": p,
        "prof": prof,
        "prof_slim": prof_slim,
        "evidence": (master.get("evidence") or "")[:_block_chars()],
        "thread": thread,
        "mail_recent": _mail_lines(pid, "recent", MAIL_RECENT_N),
        "mail_oldest": _mail_lines(pid, "oldest", MAIL_OLDEST_N),
    }


def _prompt(ctx):
    owner = settings.get("owner_name") or "the owner"
    name = ctx["person"]["name"]
    cap = _block_chars()
    lines = [
        f"You maintain the private CRM dossier {owner} keeps on "
        f"{name}. Rewrite the relationship description so it reads "
        "current — today's state of the relationship, grounded in the "
        "material below.",
        "",
        f"CURRENT PROFILE (model-synthesized earlier):",
        json.dumps(ctx["prof_slim"], ensure_ascii=False,
                   default=str)[:cap],
        "",
    ]
    if ctx["evidence"]:
        lines += ["MASTER EVIDENCE:", ctx["evidence"], ""]
    if ctx["thread"]:
        msgs = []
        for m in ctx["thread"]:
            who = "me" if m["from_me"] else name.split(" ")[0]
            msgs.append(f"[{(m.get('when') or '')[:10]}] [{who}] "
                        + m["text"][:MSG_CHARS])
        lines.append(f"RECENT DIRECT THREAD (oldest first; 'me' = {owner}):")
        lines += _fit(msgs, cap, tail=True) + [""]
    if ctx["mail_recent"]:
        lines += (["RECENT EMAIL (indexed bodies):"]
                  + _fit(ctx["mail_recent"], cap) + [""])
    if ctx["mail_oldest"]:
        lines += (["OLDEST EMAIL ON RECORD (the early history):"]
                  + _fit(ctx["mail_oldest"], cap) + [""])
    lines.append(
        "Return STRICT JSON only, no prose around it:\n"
        "{\n"
        '  "relationship_summary": "3-6 sentences",\n'
        '  "how_we_met": "one sentence, or \\"\\" to leave it unchanged"\n'
        "}\n"
        "Rules: ground EVERYTHING in the material above — never invent "
        "facts, names, dates or plans. Keep the existing summary's "
        "durable facts unless the material contradicts them; update "
        "what's stale; fold in what's new. Cite evidence dates in "
        "brackets like [2026-06-11] the way the current summary does. "
        "Fill how_we_met ONLY if the material actually shows it. "
        "No emojis.")
    return "\n".join(lines)


def _clean(raw, prof):
    if not isinstance(raw, dict):
        raise ValueError("refresh is not an object")
    summary = str(raw.get("relationship_summary") or "").strip()[:2400]
    if len(summary) < 40:
        raise ValueError("refreshed summary is empty or trivial")
    how = str(raw.get("how_we_met") or "").strip()[:500]
    if how and how == (prof.get("how_we_met") or "").strip():
        how = ""
    return summary, how


def refresh_current(pid):
    """The one-pass mode. Returns the updated profile fields."""
    if settings.fixture_mode():
        return {"status": "empty", "note": "fixture mode"}
    ctx = _context(pid)
    text = suggest.complete(_prompt(ctx))
    summary, how = _clean(suggest._extract_json(text), ctx["prof"])
    prof = crm.save_profile_refresh(pid, summary, how_met=how,
                                    reason="vira-refresh-current")
    return {"status": "ok",
            "relationship_summary": prof["relationship_summary"],
            "how_we_met": prof.get("how_we_met"),
            "refresh_count": prof.get("refresh_count")}


def explore_prompt(pid):
    detail = crm.get_person(pid)
    if not detail:
        raise KeyError(pid)
    p = detail["person"]
    prof = detail.get("profile") or {}
    name = p["name"]
    owner = settings.get("owner_name") or "the owner"
    handles = p.get("handles", {})
    # The same block budget: this prompt only ORIENTS a session that then
    # goes and reads the rest itself, and the two blocks it carries (the
    # current description, the handle list) are smaller material than the
    # one-pass refresh gets. What the budget ends is the silent truncation —
    # 2,200 characters cut a long description mid-sentence, and 600 cut the
    # handle list of a contact carrying nine addresses.
    cap = _block_chars()
    known = json.dumps({k: prof.get(k) for k in (
        "relationship_class", "relationship_summary", "how_we_met")
        if prof.get(k)}, ensure_ascii=False)[:cap]
    stats = prof.get("stats") or {}
    first = stats.get("imsg_first") or "unknown"
    return (
        f"You are Vira, {owner}'s AI chief of staff. Research {name} "
        f"(person id {pid}) across {owner}'s own data and REFRESH their "
        "dossier description with what you find. The owner pressed "
        "'Explore and refresh' on this profile — they want you to "
        "actually dig, not summarize what the dossier already says.\n\n"
        f"KNOWN TODAY: {known or '(no profile yet)'}\n"
        f"Their handles: {json.dumps(handles)[:cap]}\n"
        f"The iMessage archive with them starts {first} — anything "
        "earlier only exists in email, notes, and photos.\n\n"
        "How to work:\n"
        "1. mcp__vira__find and mcp__vira__mail_search are your main "
        f"shovels: search {name.split(' ')[0]}'s name, their handles, and "
        "the topics the dossier hints at. Chase the EARLY history "
        "(oldest email) and the recent state (last few months) — both "
        "ends are where a stale dossier is most wrong.\n"
        "2. Cross-check with mcp__vira__imessage_thread, "
        "mcp__vira__vault_search / mcp__vira__vault_note, and "
        "mcp__vira__media_search (photos and docs they appear in).\n"
        "3. Then call mcp__vira__update_person_profile ONCE with the "
        "refreshed description: person = the person id above, a "
        "relationship_summary of 3-6 sentences grounded in what you "
        "actually found (cite dates in brackets like [2019-04-02]), and "
        "how_we_met if the material shows it. Durable facts from the "
        "current description stay unless contradicted. Never invent.\n"
        "4. Finish with a short note: what you searched, what you found "
        "that was new, what stayed unknown.\n\n"
        "No emojis anywhere.")


def explore(pid):
    """Dispatch the explore session. Returns {job_id}."""
    if settings.fixture_mode():
        return {"status": "empty", "note": "fixture mode"}
    from . import session
    prompt = explore_prompt(pid)
    jid = session.sessions.launch(
        prompt, meta={"kind": "profile-explore", "person_id": pid})
    return {"status": "ok", "job_id": jid}

"""The omni router - rung 2 of the dictation door (2026-09-01).

Rung 1 (static/app.js OMNI_PREFIXES) is the deterministic label grammar:
a spoken "this is a tell..." routes with no model call and no waiting.
Unprefixed prose used to get every intent row with tell pre-selected - a
guess presented as a default. This module is the ONE model call that
reads the sentence and, grounded in what each Vira surface actually
answers, returns a validated route the palette renders as its top row:
voice in, the right window opens holding the answer.

Grounded-or-held, the resolver.py discipline: an off-vocabulary intent,
an empty reply, an unparseable payload all return None, and the palette
keeps its deterministic rows. The router may only ever ADD a sharper
first row - it can never take the deterministic path away, so a dead
backend degrades to exactly what shipped before it existed.

Read-only: it proposes a route; every act still runs through the same
client machinery the deterministic rows use (OMNI_ROUTES in app.js).
Model-call class is reply drafting, so passive instances answer too.
"""

import json
import re

# What each intent MEANS - the routing table the prompt is composed
# from, so the vocabulary and the instruction cannot drift (the
# ideatags AXES pattern). The descriptions carry what each surface can
# actually reach, because that is the fact the router needs: a question
# about "what did X text me" is answerable (Find spans messages, mail,
# photos-with-OCR, notes, contacts), and routing it to tell would file
# a question as a fact.
INTENTS = {
    "ask": ("a QUESTION to answer from the owner's own data - texts and "
            "iMessages, email, shared photos and their OCR'd contents, "
            "notes, contacts, calendar. Anything shaped 'find/what/when/"
            "did someone send...' about the owner's life or records."),
    "tell": ("a STATEMENT of fact the owner wants recorded - about a "
             "person, plans, a life update. It updates the database; it "
             "is never a question."),
    "idea": ("a task, feature wish or to-do to file in the owner's work "
             "queue for later."),
    "open": ("open a module window or a person's page - 'open X', 'show "
             "me X', 'pull up X'."),
    "session": ("dispatch an agent session to go DO multi-step work "
                "right now - research, build, fix something."),
}

ROUTE_PROMPT = """You route one spoken command inside Vira, the owner's personal assistant app. Pick the ONE intent that fits best:

{intents}

Command: {text}

Reply with ONLY a JSON object, no prose:
{{"intent": "<one of {names}>", "text": "<the command cleaned of filler, ready to carry - keep the owner's meaning exactly, never add facts>", "target": "<for open only: the window or person to open; else null>", "why": "<under 8 words, why this intent>"}}"""

# A dictated palette command is a sentence or two; anything longer is
# not a command and would only pad the prompt. This caps what WE send,
# never what the owner typed - the client carries the full text and the
# route's text falls back to it on the client side too.
TEXT_CAP = 600

WHY_CAP = 80
TARGET_CAP = 60


def compose_prompt(text):
    intents = "\n".join(f"- {k}: {v}" for k, v in INTENTS.items())
    return ROUTE_PROMPT.format(intents=intents,
                               names="|".join(INTENTS),
                               text=text[:TEXT_CAP])


def route(text):
    """One model call -> a validated route, or None (held). Never
    raises: a router that can fail the palette is worse than none."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from .suggest import complete
        raw = complete(compose_prompt(text))
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return None
        got = json.loads(m.group(0))
    except Exception:      # noqa: BLE001 — held, never broken
        return None
    intent = got.get("intent")
    if intent not in INTENTS:
        return None
    routed_text = str(got.get("text") or "").strip() or text
    target = got.get("target")
    target = str(target).strip()[:TARGET_CAP] if target else None
    why = str(got.get("why") or "").strip()[:WHY_CAP]
    return {"intent": intent, "text": routed_text,
            "target": target, "why": why}

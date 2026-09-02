"""How much a prompt may carry, asked of the backend that will answer it.

THE PROBLEM THIS EXISTS TO END.  Every prompt in Vira used to be sized by a
literal typed once and never revisited, and nothing in the app knew what the
current backend could actually hold.  `define` fed a model 9,000 characters
(5 passages x 1800) while the Anthropic CLI path it was calling reported a
1,000,000-token context window in its own response JSON.  That is not a
rounding error, it is two orders of magnitude, and it produced no error at
all -- a cap that is too small yields confident output from thin material,
which is why it survived from 2026-08-04 with nobody noticing.

It had already happened once before this module was written.  `find.ASK_LIMIT`
was 8 and is now 24, with the reason recorded in that file: "small enough that
the right note routinely sat outside it while the model answered confidently
from the wrong ones".  `define` repeated it ten days later.  A third instance
of the same signature -- a value that exists but is never passed or never
revisited, failing silently while looking correct -- killed live sessions
through 2026-08-28, when `runner.py` never passed `max_buffer_size`.

THE RULE THIS MODULE ENFORCES.  A module states WHAT IT WANTS (a budget class
and its share of it); this module answers HOW MUCH THAT IS on the backend
that will actually answer.  No module should carry a number describing a
model's capacity, because that number is a fact about a backend the owner can
change in Config at any moment.

WHY THE LADDER DEGRADES DOWNWARD.  Guessing high is not symmetric with
guessing low: an over-large prompt is rejected or silently truncated by the
provider, and a truncation we did not perform is one we cannot report.  So
every rung of `capability()` falls back to a SMALLER number, and the floor is
a budget that works on any model worth calling.  Being wrong costs efficiency;
it never costs correctness.

WHAT MAY BE SHIPPED AS A LITERAL, AND WHAT MAY NOT.  models.py already holds
the line that a shipped model ID is the thing that goes stale (see its MODEL
SOURCES heading), and the same reasoning applies here with one carve-out.  A
CONTEXT WINDOW claimed for a named model generation is a rotting literal and
is not allowed.  A conservative per-provider FLOOR is a different kind of
statement: it is not a claim about any model, it is the smallest budget we are
willing to assume, and it is only ever consulted when nothing better could be
learned or probed.  Floors here are deliberately below every current model.

THE THREE SOURCES, best first:

    learned   what a real call REPORTED about itself, cached per (provider,
              backend, model).  The Anthropic CLI hands back
              modelUsage.<model>.contextWindow and maxOutputTokens in every
              --output-format json response, so the app can simply read its
              own receipts instead of being told.  This rung cannot rot: a
              model upgrade re-learns on the next call.
    probed    a provider endpoint that states its own limits (Gemini's models
              endpoint carries inputTokenLimit / outputTokenLimit).
    floor     the conservative per-provider minimum below.

TRANSPORT IS A SEPARATE CEILING FROM CONTEXT.  A tool result inside a live
agent session crosses the SDK's NDJSON framing, which bounds ONE line -- so
`transport_cap()` exists beside the context budget and is the binding limit
for anything a tool hands back.  Before runner.py passed max_buffer_size
(2026-08-28) that ceiling was 1 MiB and exceeding it killed the session
outright, which means viratools.TEXT_CAP was accidentally protecting Vira
from a bug nobody had diagnosed.  Never raise a tool-output cap without
asking this function what the transport can carry.
"""
from pathlib import Path

from . import jsonstore

STORE = Path(__file__).resolve().parent.parent / "data" / "model-limits.json"

# Characters per token, deliberately LOW.  English prose runs ~4; code, JSON
# and dense identifiers run lower, and this number converts a token budget
# into a character budget we then fill with exactly that material.  Under-
# estimating spends less of the window than we could; over-estimating builds
# a prompt the provider rejects.
CHARS_PER_TOKEN = 3.5

# What the window must hold BESIDES the retrieved material: the prompt
# template and instructions, plus room for the answer.  Both are subtracted
# before any module gets a share.
TEMPLATE_RESERVE_TOKENS = 4_000
OUTPUT_RESERVE_TOKENS = 8_000
# A margin against our own char-to-token estimate being wrong on unusual
# material (CJK, base64, minified JS all tokenize far worse than prose).
SAFETY = 0.85

# The budget when nothing at all is known about the backend.  Chosen to be
# safe on any model worth calling while still being ~3x what the literals it
# replaces allowed.  This is the honest-unknown rung, not a target.
SAFE_MIN_CONTEXT_TOKENS = 8_000

# Output ceiling when the backend has not told us its own. Every current
# model comfortably exceeds this; it is a floor, not a target.
API_OUTPUT_FLOOR = 8_000

# Conservative per-provider floors.  NOT a claim about any specific model --
# see the module docstring.  Each is below every current model from that
# provider, so it can only ever under-spend.
FLOORS = {
    "anthropic": 200_000,
    "openai": 128_000,
    "google": 128_000,
    "xai": 128_000,
}

# A budget CLASS is a statement about the surface, not about the model.  The
# share is of the usable window after reserves.
#
#   interactive  a card or popup the owner is waiting on.  Latency is the
#                binding constraint, not capacity: filling a 1M window to
#                define one word would make the gesture feel broken.
#   standard     the default for a composed answer.
#   deep         a background pass nobody is watching, where being thorough
#                beats being quick.
CLASSES = {
    "interactive": 0.15,
    "standard": 0.45,
    "deep": 0.90,
}
DEFAULT_CLASS = "standard"

# A SHARE OF THE WINDOW IS NOT A LATENCY BUDGET, and on a 1M-token backend
# the difference stops being academic: 15% of it is ~126k tokens, which is
# an enormous prompt to build while someone watches a definition card open.
# Time-to-answer tracks input size, so a surface whose binding constraint is
# WAITING gets an absolute ceiling as well as a share, and the smaller wins.
#
# These are sized from what the material actually is rather than from a round
# number: a long article runs 5-15k characters and eight vault passages
# another ~16k, so ~24k tokens leaves generous headroom over the real case
# while keeping an interactive call in the few-seconds range it has today.
# `deep` has none - nobody is waiting on it, which is what makes it deep.
CLASS_CEILING_TOKENS = {
    "interactive": 24_000,
    "standard": 120_000,
}


def _cfg():
    from . import suggest
    return suggest.config()


def effective():
    """(provider, backend) that WILL answer the next call.

    Delegates to suggest so the budget and the caller cannot disagree about
    which backend is in play -- the fallback ladder (no key, dead login) is
    real and lives there.
    """
    from . import suggest
    try:
        return suggest.effective_backend(_cfg())
    except Exception:      # noqa: BLE001 -- a budget must never block a call
        return "anthropic", "cli"


def _key(provider, backend, model=""):
    return f"{provider}:{backend}:{model or '*'}"


def _store():
    return jsonstore.read(STORE, {}) or {}


def learn(provider, backend, model="", context_tokens=0, max_output_tokens=0):
    """Record what a real response said about its own limits.

    This is the rung that cannot go stale: the app reads its own receipts.
    Called from suggest after a CLI response that carries them.  Never raises
    -- a failed write costs one cache entry, and losing a model call over
    bookkeeping would be the worse trade.
    """
    ctx = int(context_tokens or 0)
    out = int(max_output_tokens or 0)
    if ctx <= 0 and out <= 0:
        return
    row = {"context_tokens": ctx, "max_output_tokens": out}
    def _put(st):
        st.setdefault("learned", {})[_key(provider, backend, model)] = row
        return st

    try:
        jsonstore.mutate(STORE, _put, {"learned": {}}, indent=2)
    except Exception:      # noqa: BLE001
        pass


def _learned(provider, backend, model=""):
    learned = _store().get("learned") or {}
    return learned.get(_key(provider, backend, model)) or \
        learned.get(_key(provider, backend))


def capability(provider=None, backend=None, model=""):
    """What the answering backend can hold, and where that number came from.

    `source` is part of the answer on purpose: a caller that wants to say
    "this card was composed against a 1M window" must be able to tell a
    learned fact from a floor we assumed.
    """
    if provider is None or backend is None:
        provider, backend = effective()
    cfg = _cfg()
    if not model:
        model = (cfg.get("cli_model") if backend == "cli"
                 else cfg.get("api_model")) or ""

    hit = _learned(provider, backend, model)
    if hit and hit.get("context_tokens"):
        return {"provider": provider, "backend": backend, "model": model,
                "context_tokens": hit["context_tokens"],
                "max_output_tokens": hit.get("max_output_tokens") or 0,
                "tools": has_tools(provider, backend),
                "source": "learned"}

    floor = FLOORS.get(provider)
    if floor:
        return {"provider": provider, "backend": backend, "model": model,
                "context_tokens": floor, "max_output_tokens": 0,
                "tools": has_tools(provider, backend), "source": "floor"}

    return {"provider": provider, "backend": backend, "model": model,
            "context_tokens": SAFE_MIN_CONTEXT_TOKENS, "max_output_tokens": 0,
            "tools": has_tools(provider, backend), "source": "unknown"}


def has_tools(provider=None, backend=None):
    """Whether the answering path can call tools.

    This describes the drafting path, not a detached live agent session.
    Anthropic's CLI draft path is tool-capable; direct API paths are plain
    completions. A module may use this to ask for MORE, never to require it:
    the any-model seam means the same feature must still work when the answer
    is False.
    """
    if provider is None or backend is None:
        provider, backend = effective()
    return provider == "anthropic" and backend == "cli"


def context_chars(kind=DEFAULT_CLASS, provider=None, backend=None):
    """Characters of retrieved material this surface may put in a prompt."""
    cap = capability(provider, backend)
    share = CLASSES.get(kind, CLASSES[DEFAULT_CLASS])
    usable = cap["context_tokens"] - TEMPLATE_RESERVE_TOKENS \
        - OUTPUT_RESERVE_TOKENS
    if usable <= 0:
        usable = max(int(cap["context_tokens"] * 0.5), 1_000)
    tokens = usable * share
    ceiling = CLASS_CEILING_TOKENS.get(kind)
    if ceiling:
        tokens = min(tokens, ceiling)
    return max(int(tokens * CHARS_PER_TOKEN * SAFETY), 2_000)


def split(kind=DEFAULT_CLASS, parts=1, provider=None, backend=None):
    """(total_chars, per_part_chars) for a surface that gathers N passages.

    The replacement for the `N items x M chars` pair every module carried.
    A module says how many passages it wants and gets a per-passage size,
    instead of two literals that were each chosen once and never compared to
    the window they had to fit inside.
    """
    total = context_chars(kind, provider, backend)
    return total, max(int(total / max(parts, 1)), 400)


def api_output_tokens(provider=None, backend=None):
    """max_tokens to send on a direct API call.

    The Anthropic API path shipped a hardcoded 1500 against models that
    report 128_000 -- sized in 2026-07-07 for reply drafts and never revisited
    while 22 modules adopted the function. Prefers what the backend said about
    itself, falls back to a floor that is generous for a card and still modest
    for a model, and never returns 0 (the API requires the field).
    """
    cap = capability(provider, backend)
    known = cap.get("max_output_tokens") or 0
    return known or API_OUTPUT_FLOOR


def transport_cap():
    """Bytes ONE tool result may carry back through the session transport.

    Bounded by the SDK's NDJSON framing, not by the context window, and it
    binds first for anything a tool returns.  Read from the runner so this
    and the transport cannot drift; the 0.5 leaves room for the JSON envelope
    and escaping around the payload.
    """
    try:
        from . import runner
        return int(runner._max_buffer_bytes() * 0.5)
    except Exception:      # noqa: BLE001 -- the SDK default, halved
        return 512 * 1024


# How many tool results a single session turn should be able to hold at once.
# A tool-result cap is not a whole-window budget: a session makes several
# calls and every result stays in context for the rest of the turn, so the
# ceiling for ONE is the deep budget divided by the number that must coexist.
TOOL_RESULTS_COEXIST = 8


def tool_result_cap(provider=None, backend=None):
    """Characters ONE native tool may hand back to a session.

    TWO CEILINGS BIND HERE AND THEY ARE UNRELATED. The window decides how much
    the model can hold; the SDK's NDJSON framing decides how much can physically
    cross the transport in one message, and that one is measured in bytes and
    kills the session outright when exceeded rather than degrading.

    This replaces viratools.TEXT_CAP = 12_000, which was set in the original
    build and throttled EVERY native tool result to every agent session -- about
    1% of the window the session actually had. It was also, unknowingly, the
    only thing protecting Vira from the 1 MiB framing bug fixed on 2026-08-28:
    before runner.py passed max_buffer_size, a larger result would have killed
    the session whole. Never raise a tool-output cap without asking here.

    NOTE the provider caveat: this reads the DRAFTING backend's capability,
    while a tool result is consumed by the SESSION's model. Those are the same
    on the ordinary install (both Anthropic) and can differ if the owner runs
    a codex session while drafting elsewhere. The transport ceiling is exact
    either way, and the context term degrades downward, so a mismatch costs
    headroom rather than correctness.
    """
    # "standard", not "deep": a tool result is not a one-shot prompt whose
    # material IS the whole ask. It stays in context for the rest of the
    # session and sits alongside every other result, so the share it draws
    # on must leave room for the turns after it.
    by_window = int(context_chars("standard", provider, backend)
                    / TOOL_RESULTS_COEXIST)
    return max(min(by_window, transport_cap()), 4_000)


def status():
    provider, backend = effective()
    cap = capability(provider, backend)
    return {
        "provider": provider, "backend": backend,
        "context_tokens": cap["context_tokens"], "source": cap["source"],
        "tools": cap["tools"],
        "max_output_tokens": cap["max_output_tokens"],
        "transport_cap": transport_cap(),
        "tool_result_cap": tool_result_cap(),
        "classes": {k: context_chars(k, provider, backend) for k in CLASSES},
    }

"""What a posting's own body says about where the work happens.

A board's location field and its remote flag are metadata an employer
fills in loosely. The description is where the policy is actually
written down, in a sentence somebody wrote on purpose:

    This role is based in San Francisco, CA. We use a hybrid work model
    of 3 days in the office per week and offer relocation assistance.

Measured on OpenAI's live Ashby board 2026-08-12: 735 listed roles, of
which 422 carry `isRemote: True` while naming real cities in their
location strings -- and 221 of those postings carry the sentence above.
`fetch_ashby` believed the flag and appended a "Remote" location that
the employer never wrote, so 179 roles in the measured snapshot read as
eligible even though their binding office did not match the configured
places. The body was right there in `jd` the whole time.

So: THE BODY OUTRANKS THE FLAG, and nothing else here outranks the
body. This module only reports what a sentence actually says --
`places` are lifted out of the sentence text, never inferred from the
board's own location strings, and a posting that says nothing confident
returns None rather than a guess. That is the same grounded-or-held
discipline `resolver.py` and `evidence.py` hold: an honest silence
beats a confident wrong reading, because a wrong reading here HIDES a
job from the owner.

The reader is deterministic and costs no model call, which is what lets
it run on every role of every sweep.

WINDOWS, NOT SENTENCES. The policy routinely spans a sentence boundary
("...San Francisco, CA. We use a hybrid work model of 3 days...") and
the corpus is full of abbreviations that defeat a sentence splitter
("anywhere in the U.S.", "Washington, D.C."). So each trigger claims a
bounded window of following text. The bound matters in the other
direction too: an unbounded window would eventually reach some
unrelated paragraph mentioning remote work and flip the reading.
"""

from __future__ import annotations

import re

# How much text after a trigger counts as the same policy statement.
# The longest real policy sentence measured on this corpus is 156 chars
# ("...based in San Francisco or NYC, with a hybrid schedule of 3 days
# per week in the office, or can be performed remotely from anywhere in
# the U.S."), so this holds the whole statement plus its follow-on
# clause without reaching the next topic.
WINDOW = 260

# The residency trigger. The adverb slot is what carries the corpus's
# real variety -- "exclusively", "primarily", "ideally", "preferred to
# be", "either fully remote or" -- so it is a bounded wildcard rather
# than a list nobody would keep current.
TRIGGER = re.compile(
    r"(?i)\b(?:this|the)\s+(?:role|position)\s+(?:is|will\s+be)\s+"
    r"(?:\w+\s+){0,4}?based\b")

# A schedule stated with no residency sentence at all still binds a role
# to whatever offices the board named: you cannot do 3 days a week in an
# office from another city.
SCHEDULE = re.compile(
    r"(?i)\bhybrid\s+(?:work\s+model|(?:work\s+)?schedule)\b|"
    r"\bhybrid\s+role\b(?=[^.]{0,100}\b(?:days?|offices?|on-?site|in-?person|based)\b)|"
    r"\bthis\s+role\s+is\s+hybrid\b|"
    r"\blocation[- ]based\s+hybrid\s+policy\b|"
    r"\bthis\s+role\s+is\s+hybrid\s+and\s+has\s+a\s+requirement\b|"
    r"\brequires?\s+in-?person\s+presence\b|"
    r"\b(?:in|at)\s+the\s+office\s+(?:\d+|one|two|three|four|five)\s+days?\b|"
    r"\b(?:sit|work(?:ing)?)\s+(?:in-?person|on-?site)\b[^.]{0,80}?"
    r"\b(?:\d+|one|two|three|four|five)\s+days?\b|"
    r"\b(?:this\s+)?(?:role|position)\s+will\s+be\s+"
    r"(?:\d+|one|two|three|four|five)\s+days?\s+on-?site\b|"
    r"\bexpect\s+all\s+staff\s+to\s+be\s+in\s+one\s+of\s+our\s+offices\b")

# Policy forms that do not use the corpus's dominant "this role is
# based" sentence. Each expression captures only the employer-written
# place clause; the generic place reader is intentionally not used on
# arbitrary prose around these forms.
HEADING_POLICY = re.compile(
    r"(?i)\blocation\s*(?:/\s*work\s+model)?\s*:\s*"
    r"(?P<place>[^.;()]{2,90}?)\s*(?:[;(]\s*hybrid\b)")
HYBRID_PLACE = re.compile(
    r"(?i)\b(?:this\s+(?:is\s+)?(?:a\s+)?|the\s+)?"
    r"(?:available\s+as\s+a\s+)?"
    r"(?:hybrid\s+(?:role|position|work)|role\s+is\s+hybrid)\b"
    r"(?:\s*\([^)]*\))?\s*(?:,\s*)?"
    r"(?:that\s+can\s+be\s+)?(?:and\s+(?:will\s+be|is)\s+)?"
    r"(?:based\s+(?:in|out\s+of)|from|in)\s+"
    r"(?P<place>[^.;]{2,110}?)\s+"
    r"(?:offices?|hubs?|hq|headquarters)\b")
HYBRID_POSITION_IN = re.compile(
    r"(?i)\bhybrid\s+position\s*(?:\([^)]*\))?\s+in\s+"
    r"(?P<place>[^.;]{2,70}?)(?=\.|\s+(?:why|about|in\s+this\s+role)\b)")
HYBRID_BASED = re.compile(
    r"(?i)\b(?:this\s+is\s+a\s+)?hybrid\s+(?:role|position)\b"
    r"(?:\s*\([^)]*\))?\s+(?:and\s+(?:will\s+be|is)\s+)?"
    r"based\s+(?:in|out\s+of)\s+(?P<place>[^.;,]{2,70}?)"
    r"(?=,\s+(?:with|and\s+in\s+office|monday|tuesday|wednesday|thursday|friday)\b|\.)")
HYBRID_WORK_OFFICE = re.compile(
    r"(?i)\bhybrid\s+(?:role|position)\b[^.]{0,80}?"
    r"(?:work(?:ing)?|sit)\s+(?:out\s+of|from|in|at)\s+"
    r"(?:(?:one\s+of\s+)?(?:our|the))?\s*"
    r"(?P<place>[^.;]{2,70}?)\s+(?:offices?|hubs?|hq|headquarters)\b")
ATTEND_OFFICE = re.compile(
    r"(?i)\b(?:ability\s+to|comfortable\s+with|required\s+to|require\s+you\s+to|"
    r"must\s+be\s+willing\s+to)\s+(?:be\s+comfortable\s+with\s+)?"
    r"(?:work(?:ing)?|sit)\s+(?:in-?person\s+)?(?:from|in|at)\s+"
    r"(?:(?:one\s+of\s+)?(?:our|the))\s+(?P<place>[^.;]{2,90}?)\s+"
    r"(?:offices?|hubs?|hq|headquarters|datacenters?)\b")
DIRECT_LOCATED = re.compile(
    r"(?i)\bthis\s+(?:role|position)\s+is\s+located\s+in\s+"
    r"(?P<place>[^.;]{2,70}?)(?=\.|\s+(?:why|about|in\s+this\s+role)\b)")
DIRECT_BASED = re.compile(
    r"(?i)\b(?:and\s+is|opportunity\s+to\s+be)\s+based\s+"
    r"(?:in|out\s+of)\s+(?:(?:one\s+of\s+)?(?:our|the))?\s*"
    r"(?P<place>[^.;]{2,90}?)\s+(?:offices?|hubs?|hq|headquarters)\b")
TRANSITION_OFFICE = re.compile(
    r"\bThis\s+(?P<place>[A-Z][A-Za-z .'-]{1,45})-based\s+"
    r"(?i:(?:role|position)\s+is\s+currently\s+remote\s+and\s+is\s+"
    r"expected\s+to\s+transition\s+to\s+an\s+in-?office\s+arrangement)\b")

# Remote work can still be territorially bound. These are hard modal
# forms only: preferences and "remote-first anywhere in ..." remain
# permissive. The captured region is kept as employer evidence, while
# callers retain the Remote facet and apply the owner's place rule.
REMOTE_REGION = re.compile(
    r"(?i)(?:\b(?:successful\s+)?candidates?\s+|\bthis\s+team\s+member\s+|"
    r"\bthis\s+role\s+|\byou\s+|\(\s*)"
    r"(?:must|should|is\s+required\s+to)\s+(?:be\s+)?"
    r"(?:based|located)\s+(?:in\s+)?|"
    r"\b(?:successful\s+)?candidates?\s+must\s+reside\s+in\s+|"
    r"\bthis\s+team\s+member\s+must\s+reside\s+in\s+|"
    r"\brequires?\s+candidates?\s+to\s+be\s+located\s+in\s+")

REGION_END = re.compile(
    r"(?i)\)\s*(?:compensation|about|mission|why)\b|"
    r"\.\s+(?:compensation|about|mission|why|in\s+this\s+role)\b|"
    r"\b(?:compensation|about\s+the\s+role|mission|why\s+work)\b")

CAPTURED_POLICY = (
    HEADING_POLICY,
    HYBRID_PLACE,
    HYBRID_POSITION_IN,
    HYBRID_BASED,
    HYBRID_WORK_OFFICE,
    ATTEND_OFFICE,
    DIRECT_LOCATED,
    DIRECT_BASED,
    TRANSITION_OFFICE,
)

# Read in this order: a refusal is worded with almost the same words as
# an offer -- "we aren't considering remote applications" against "we
# are open to considering remote" -- so permissiveness can only be
# judged after the refusals have had their say. Getting that order or
# these patterns wrong does not merely miss a refusal, it reads one as
# an offer, which is the worst answer available. `_clean` folds curly
# apostrophes to straight ones before this runs: the corpus writes
# "aren't" with U+2019 and a missed contraction here inverted the
# reading of every posting that refused remote in that spelling.
REFUSAL = re.compile(
    r"(?i)(?:are\s*n'?t|is\s*n'?t|are\s+not|is\s+not|not\s+currently|"
    r"no\s+longer)\s+(?:considering|accepting|open\s+to)[^.]{0,40}remote|"
    r"\bremote\s+(?:work|applications?|candidates?)[^.]{0,24}?\bnot\s+"
    r"(?:be\s+)?(?:considered|accepted|available|an\s+option)|"
    r"\bno\s+remote\b|\bnot\s+a\s+remote\s+role\b")

PERMISSIVE = re.compile(
    r"(?i)\bfully\s+remote\b|\bperformed\s+remotely\b|"
    r"\bremotely\s+from\s+anywhere\b|\bwork\s+from\s+anywhere\b|"
    r"\bremote[- ]first\b|"
    r"\b(?:welcome|welcomes|considering|consider|open\s+to)\b[^.]{0,40}?"
    r"\bremote\b|"
    r"\bremote\s+(?:candidates?|applicants?|work)\b[^.]{0,24}?"
    r"\b(?:welcome|considered|possible|available|fine)\b|"
    r"\bmay\s+(?:also\s+)?(?:be|consider)\b[^.]{0,24}?\bremote\b|"
    r"\bor\s+remote\b")

# "3 days a week" and "3 days in the office per week" are the same
# statement; the words between the count and "week" are why a tight
# pattern silently reported no schedule on the corpus's most common
# sentence.
DAYS = re.compile(
    r"(?i)\b(\d+|one|two|three|four|five|six)\s*days?\b"
    r"(?:[^.]{0,24}?\b(?:a|per)\s*week\b|\s+(?:a|per)\s*week\b|"
    r"\s+weekly\b)|"
    r"\bin\s+the\s+office\s+(\d+|one|two|three|four|five|six)\s*days?")
WORD_DAYS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}

# Where the place clause ends. Everything here begins a different
# thought: the schedule, the relocation offer, a caveat, a new sentence.
CLAUSE_END = re.compile(
    r"(?i)\.\s|;|\s+with\s+|\s+and\s+(?:we|requires?|offers?|you)\b|"
    r"\s+and\s+is\s+in-?person\b|"
    r"\s+(?:however|but|although|while)\b|\s+We\s+use\b|\s+and\s+follows?\b|"
    r"\s+at\s+this\s+time\b|\s+on\s+a\s+case\b|\s*[-–—]\s+|"
    r"\s+for\s+at\s+least\b|\s+\d+\s*days?\b")

# Office nouns that qualify a place rather than naming one, and the
# determiners a policy sentence opens with.
OFFICE_NOUN = re.compile(
    r"(?i)\s*\b(?:hq|headquarters|offices?|hubs?|locations?|site)\b\s*$")
LEAD_JUNK = re.compile(
    r"(?i)^(?:in|at|out\s+of|within|from)\s+|^(?:one\s+of\s+)?(?:our|the|its)\s+"
    r"|^(?:either|any\s+of)\s+"
    # "based on-site at our Palo Alto office" -- the arrangement word
    # leads a perfectly real place, so strip it rather than rejecting
    # the whole clause.
    r"|^(?:on-?site|onsite|in-?person|remotely)\s+")

# A trailing comma segment that QUALIFIES the place before it rather
# than naming a new one, so "San Francisco, CA" and "Paris, France"
# survive whole while "San Francisco, Seattle or New York" splits into
# three. Misjudging one only changes the label shown to the owner --
# eligibility matches by substring -- so a modest list is the right
# size for this.
QUALIFIER = re.compile(
    r"(?i)^(?:[A-Z]{2}|U\.?S\.?A?|USA|United\s+States|UK|U\.K\.|"
    r"England|Ireland|France|Germany|Japan|Singapore|Australia|India|"
    r"Canada|Poland|Switzerland|Netherlands|Spain|Italy|Sweden|Brazil|"
    r"Mexico|Israel|South\s+Korea|Korea|China|Taiwan|UAE|Portugal|"
    r"Denmark|Norway|Finland|Belgium|Austria|Greece|New\s+Zealand)$")

# A place clause is a handful of proper nouns. Anything long or carrying
# a verb is prose that happened to follow the word "based". The word cap
# is what rejects "an OpenAI self-build data center campus" (six words,
# 39 characters -- comfortably inside the character cap, and nothing a
# city ever looks like); the longest real names here are four, like
# "San Francisco Bay Area" and "New York, New York".
MAX_PLACE = 44
MAX_PLACE_WORDS = 5
NOT_A_PLACE = re.compile(
    r"(?i)\b(?:you|we|your|our\s+team|experience|candidate|role|work|"
    r"team|company|please|apply|position|salary|which|that|this)\b|\d")

# Words that describe the ARRANGEMENT, not the place. "This role is
# based on-site, five days a week" names no city at all, and reading
# "on-" and "five days a week" as offices would bind the role to
# nowhere real.
NOT_A_PLACE_WORD = re.compile(
    r"(?i)^(?:on|on-?site|onsite|in-?person|remote(?:ly)?|hybrid|"
    r"anywhere|home|flexible)$|\bdays?\b|\bweek\b|"
    r"\b(?:campus|centre|center|facility|building|premises)\b")

# Some policies lead with the schedule and name the binding office after
# it: "a minimum of 3 days weekly in our San Francisco office." This is
# the schedule-form counterpart to `_places`' usual "based in" clause.
# Requiring a determiner keeps a bare "in the office" from becoming a
# place, while `_split_places` applies the same bounded place validation.
SCHEDULE_OFFICE = re.compile(
    r"(?i)\b(?:in|at)\s+(?:either\s+)?(?:(?:one\s+of\s+)?(?:our|the|its))\s+"
    r"([^.;]{2,80}?)\s+offices?\b")


def _clean(text):
    """Markdown out, whitespace flattened. The corpus is full of
    `**This role is based in...**` and `## Workplace & Location`."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"[*_`#>]+", " ", text)
    # Curly punctuation to straight, so the contraction patterns above
    # match the spelling employers actually publish.
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def _days(window):
    m = DAYS.search(window)
    if not m:
        return None
    raw = m.group(1) or m.group(2) or ""
    raw = raw.strip().lower()
    if raw.isdigit():
        n = int(raw)
    else:
        n = WORD_DAYS.get(raw)
    return n if n and 1 <= n <= 7 else None


def _split_places(clause):
    """A place clause -> the individual places it names."""
    # Parenthesised content is either the real list ("our European
    # offices (Paris, France and London, UK)") or a schedule ("our New
    # York City office (5 days per week)"). Digits tell them apart.
    paren = re.search(r"\(([^)]{2,80})\)", clause)
    if paren and not re.search(r"\d", paren.group(1)):
        clause = paren.group(1)
    else:
        clause = re.sub(r"\([^)]*\)", " ", clause)

    parts = [p for p in re.split(r"\s+or\s+|\s+and\s+|/|,", clause) if p.strip()]
    out = []
    for part in parts:
        p = part.strip().strip(".,;:–—- ")
        if not p:
            continue
        if QUALIFIER.match(p) and out \
                and not (p.upper() == "NY" and out[-1].upper() == "SF"):
            out[-1] = f"{out[-1]}, {p}"     # attach a state or country
            continue
        # "in our San Francisco HQ" stacks a preposition on a
        # determiner, so one pass leaves "our San Francisco" behind.
        while True:
            stripped = LEAD_JUNK.sub("", p).strip()
            if stripped == p:
                break
            p = stripped
        # "our European offices" names a region, not a place; drop the
        # noun and keep whatever remains only if it is a real name.
        # Trailing hyphen matters: stripping "site" off "on-site" leaves
        # "on-", which no reject pattern would recognise as the fragment
        # it is.
        p = OFFICE_NOUN.sub("", p).strip().strip(".,;:-– ")
        if not p or len(p) > MAX_PLACE or NOT_A_PLACE.search(p):
            continue
        if len(p.split()) > MAX_PLACE_WORDS or NOT_A_PLACE_WORD.search(p):
            continue
        if not re.search(r"[A-Za-z]", p):
            continue
        if p.lower() in ("european", "us", "u.s.", "american", "global",
                         "remote", "one", "either", "regional"):
            continue
        if p not in out:
            out.append(p)
    return out


def _places(window, kind):
    """The offices a residency window names, in the order it names
    them. A schedule window may name its office after the schedule; it
    must use that bounded clause rather than generic ``based`` prose,
    which also appears in follow-on phrases such as ``If based
    in-office`` and can make weekdays look like cities."""
    if kind == "schedule":
        office = SCHEDULE_OFFICE.search(window)
        return _split_places(office.group(1)) if office else []

    m = re.search(r"(?i)\bbased\b\s*", window)
    if not m:
        return []
    tail = window[m.end():]
    cut = CLAUSE_END.search(tail)
    clause = tail[:cut.start()] if cut else tail[:120]
    return _split_places(clause)


def _quote(text, start, end=None):
    """A short source excerpt beginning at a grounded policy match."""
    quote = text[start:(end or start + WINDOW)].strip()
    cut = re.search(r"(?<=[.!?])\s+(?=[A-Z])", quote[80:])
    if cut:
        quote = quote[:80 + cut.start()]
    return quote[:240]


def _captured_policy(text):
    """Earliest policy whose office is captured by a bounded expression."""
    matches = []
    for pattern in CAPTURED_POLICY:
        match = pattern.search(text)
        if match:
            matches.append(match)
    if not matches:
        return None
    match = min(matches, key=lambda item: (
        item.start(), len(item.group("place") or "")))
    places = _split_places(match.group("place"))
    if not places:
        return None
    window = text[match.start():match.start() + WINDOW]
    days = _days(window)
    mode = "hybrid" if days or re.search(r"(?i)\bhybrid\b", window) else "onsite"
    return {
        "mode": mode,
        "days": days,
        "places": places,
        "remote_ok": False,
        "binds": True,
        "quote": _quote(text, match.start()),
    }


def _region_clause(text, match):
    """The bounded territory following a hard residence requirement."""
    tail = text[match.end():match.end() + 180]
    stops = [len(tail)]
    end = REGION_END.search(tail)
    if end:
        stops.append(end.start())
    sentence = re.search(r"[;.!?](?:\s|$)", tail)
    if sentence:
        stops.append(sentence.start())
    clause = tail[:min(stops)].strip(" ()[],:;-–—")
    clause = re.sub(r"(?i)\s+for\s+this\s+role\b.*$", "", clause)
    clause = re.sub(r"(?i)\s+or\s+surrounding\s+area\b.*$", "", clause)
    return clause[:140].strip()


def _remote_region(text):
    """A hard remote residence/territory rule, without changing its mode."""
    match = REMOTE_REGION.search(text)
    if not match:
        return None
    region = _region_clause(text, match)
    if not region or NOT_A_PLACE.search(region):
        return None
    return {
        "mode": "remote",
        "days": None,
        "places": [region],
        "remote_ok": True,
        "binds": True,
        "remote_limited": True,
        "quote": _quote(text, max(0, match.start() - 80)),
    }


def read(jd):
    """The workplace policy a description states about itself.

    Returns None when the body says nothing confident -- which is the
    common case and must stay cheap to act on. Otherwise:

        mode      "remote" | "hybrid" | "onsite"
        days      in-office days per week, when the body states a number
        places    the offices the body names (may be empty: a schedule
                  can bind a role without naming a city)
        remote_ok whether the body leaves a remote path open
        binds     True when this reading narrows board eligibility. For an
                  office policy it rules remote out; for remote_limited it
                  preserves remote work but binds it to a territory.
        remote_limited
                  True only for the latter case.
        quote     the sentence, so a surface can show its own evidence
    """
    text = _clean(jd)
    if not text:
        return None

    limited = _remote_region(text)
    if limited:
        return limited

    captured = _captured_policy(text)
    if captured:
        return captured

    m = TRIGGER.search(text)
    kind = "residency"
    if not m:
        m = SCHEDULE.search(text)
        kind = "schedule"
    if not m:
        return None

    # Schedule-first language often follows a genuine remote alternative
    # ("fully remote ... or ... hybrid schedule"). Include only a small
    # preceding context for that decision; place extraction remains bounded
    # to the schedule's own forward window.
    context_start = max(0, m.start() - 160) if kind == "schedule" else m.start()
    window = text[m.start():m.start() + WINDOW]
    context = text[context_start:m.start() + WINDOW]
    refused = bool(REFUSAL.search(context))
    permissive = (not refused) and bool(PERMISSIVE.search(context))
    days = _days(window)
    places = _places(window, kind)

    if permissive:
        mode, remote_ok = "remote", True
    elif days or re.search(r"(?i)hybrid", window):
        mode, remote_ok = "hybrid", False
    else:
        mode, remote_ok = "onsite", False

    # A residency sentence that names nowhere, states no schedule and
    # refuses nothing is not evidence of anything -- "this role is based
    # on the West Coast team" and similar prose land here.
    if kind == "residency" and not places and not days and not refused \
            and mode != "remote":
        return None

    return {
        "mode": mode,
        "days": days,
        "places": places,
        "remote_ok": remote_ok,
        "binds": not remote_ok,
        "quote": _quote(text, m.start()),
    }


def label(wp):
    """A short human line for a row: 'San Francisco, CA - hybrid, 3
    days/week'. Empty when there is nothing worth saying."""
    if not wp:
        return ""
    where = " / ".join(wp.get("places") or [])
    if wp.get("mode") == "remote":
        qualifier = "remote limited" if wp.get("remote_limited") else "remote ok"
        return f"{qualifier}{' - ' + where if where else ''}"
    bits = []
    if where:
        bits.append(where)
    if wp.get("days"):
        bits.append(f"{'hybrid' if wp['mode'] == 'hybrid' else 'onsite'}, "
                    f"{wp['days']} days/week")
    elif wp.get("mode") == "hybrid":
        bits.append("hybrid")
    elif where:
        bits.append("office-based")
    return " - ".join(bits)


# Deliberately a local copy of jobboards.REMOTE_RE: jobboards imports
# this module, so reaching back for it would be a cycle.
_REMOTE_LOC = re.compile(r"(?i)\bremote\b")

# Tokens that never distinguish one city from another, so matching on
# them alone would call New York and New Orleans the same place.
_WEAK = {"new", "the", "of", "our", "us", "usa", "united", "states",
         "city", "area", "metro", "greater", "downtown", "north",
         "south", "east", "west", "saint", "st"}


def _tokens(place):
    words = re.sub(r"[^a-z0-9]+", " ", str(place).lower()).split()
    return {w for w in words if len(w) > 1 and w not in _WEAK}


def _same_place(a, b):
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb and ta & tb)


def allows(wp, places_rx, locations=None, remote_regions_rx=None):
    """Whether a bound policy can be worked from the owner's places.

    True when the body does not bind, when it names no office (the
    board's own location strings stay the authority then), or when one
    of the offices it names matches the rule. `places_rx` is the
    compiled office-place rule from jobboards.location_rule().
    `remote_regions_rx` is the separately configured set of territories
    from which the owner can accept region-limited remote work. Neither
    rule has a built-in city, country, or region.

    WHEN A POSTING EXPLICITLY NAMES A CONFIGURED CITY, that claim stands
    unless the body corroborates a DIFFERENT published city. Three
    measured cases decide the shape, and no simpler rule gets all three
    right:

    - Location "US - Remote", body "based in San Francisco, CA, hybrid
      3 days a week". Nothing published is a city at all, so the role's
      eligibility rested entirely on a remote tag the body contradicts.
      REFUSE -- this is the case the module exists for.
    - Locations "San Francisco / New York City / Seattle", body
      "exclusively based in our San Francisco HQ". A published city
      matches the owner, but the body corroborates one of the others and
      says it is the only real one. REFUSE -- that is what narrowing is.
    - Configured location "New York", published location "NYC", body
      "based in our SoHo office" (Hebbia's "AI
      Strategist, Corporate Law"). SoHo matches no New York rule and
      corroborates nothing published -- because it is the SAME office at
      a finer granularity, not a different one. ALLOW; vetoing here
      would hide a real New York job.

    The asymmetry is deliberate: showing an unsuitable job costs a
    glance, while hiding a suitable job costs the opportunity.
    """
    if not wp or not wp.get("binds"):
        return True
    named = wp.get("places") or []
    locations = locations or []
    if wp.get("remote_limited"):
        region = " | ".join(str(p) for p in named)
        if places_rx is None and remote_regions_rx is None:
            return True
        return bool((places_rx is not None and places_rx.search(region))
                    or (remote_regions_rx is not None
                        and remote_regions_rx.search(region)))

    if places_rx is None:
        # A configured remote territory is not an office-location rule.
        # With no office places configured, a binding office requirement
        # cannot be declared reachable merely because the board says Remote.
        return remote_regions_rx is None

    if not named:
        # A schedule with no office named ("we use a hybrid work model of
        # 3 days in the office per week") still binds the role to
        # whatever the posting itself lists -- you cannot be in an office
        # three days a week from another city. The remote tag is exactly
        # what that contradicts, so the on-site locations are what count.
        if any(places_rx.search(str(loc)) for loc in locations):
            return True
        onsite = [part.strip()
                  for loc in locations
                  for part in re.split(r"[|;•]", str(loc))
                  if part.strip() and not _REMOTE_LOC.search(part)]
        if not onsite:
            return False         # recurring office work needs a confirmed base
        return any(places_rx.search(str(l)) for l in onsite)

    if any(places_rx.search(p) for p in named):
        return True
    if not any(places_rx.search(str(loc)) for loc in locations):
        return False        # eligibility rested on a remote tag alone
    return not any(_same_place(p, loc) for p in named for loc in locations)

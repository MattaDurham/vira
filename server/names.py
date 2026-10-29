"""Person-name comparison for the contact cleanup tools.

Every duplicate finder in Vira asks one question of two names: could they
be the same person? This module answers it the same way everywhere, so
the CRM merge, the phone fold and the import check agree. A name is split
into first and last the way the Apple spoke does (`applecontacts.split_name`
drops a comma suffix, this one also drops a parenthetical and a generation
or degree suffix), the first name is reduced to its first token, and a
small nickname table says that "Matt" and "Matthew" are one first name.
Two names are compatible when the surnames agree and the first names are
the same or nickname-equivalent; a single initial never matches, since
"J. Example" could be anyone in the family.

The nickname table is the one the June 2026 phone dedup used
(crm/scripts/dedup_extract.py), kept as data here. It is deliberately
short: a table that maps every spelling to every other merges people it
should not, and the owner decides every pair from the tab anyway.
Deterministic, stdlib only.
"""
import re

# formal name -> the nicknames it is commonly shortened to. Every spelling
# in a row is equivalent to every other spelling in that row.
NICKNAMES = {
    "robert": "rob bob bobby", "william": "will bill billy liam",
    "michael": "mike mick micky", "richard": "rick dick rich richie",
    "james": "jim jimmy jamie", "john": "jack johnny jon",
    "elizabeth": "liz beth lizzie betsy eliza libby",
    "katherine": "kate katie kathy kat catherine cathy",
    "margaret": "meg maggie peggy marge", "jennifer": "jen jenny",
    "matthew": "matt", "joseph": "joe joey", "charles": "charlie chuck",
    "thomas": "tom tommy", "daniel": "dan danny", "christopher": "chris",
    "samuel": "sam sammy", "benjamin": "ben benny",
    "alexander": "alex al xander", "alexandra": "alex allie sandra",
    "nicholas": "nick nicky", "anthony": "tony", "andrew": "andy drew",
    "edward": "ed eddie ted ned", "steven": "steve", "stephen": "steve",
    "david": "dave davey", "jonathan": "jon jonny",
    "patricia": "pat patty trish", "deborah": "deb debbie",
    "susan": "sue susie", "kimberly": "kim", "rebecca": "becca becky",
    "jessica": "jess", "gregory": "greg", "timothy": "tim", "ronald": "ron",
    "kenneth": "ken kenny", "joshua": "josh", "zachary": "zach zack",
    "nathaniel": "nate nat", "theodore": "ted teddy theo",
    "frederick": "fred freddy", "raymond": "ray", "lawrence": "larry",
    "vincent": "vince", "gerald": "jerry", "francis": "frank frankie",
    "peter": "pete",
}

_GROUPS = {}
for _formal, _nicks in NICKNAMES.items():
    _group = frozenset([_formal, *_nicks.split()])
    for _n in _group:
        _GROUPS.setdefault(_n, set()).update(_group)

_PAREN = re.compile(r"\([^)]*\)")
_SPACES = re.compile(r"\s+")
_LETTERS = re.compile(r"[^a-z]")
# trailing tokens that are not a surname
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd", "cfp", "esq",
             "cpa", "dds", "jd", "mba", "do", "rn", "pe"}


def clean(name):
    """The name without a parenthetical, a comma suffix or doubled spaces:
    "Pat Example, CFP (role account)" -> "Pat Example"."""
    base = _PAREN.sub(" ", str(name or ""))
    base = base.split(",")[0]
    return _SPACES.sub(" ", base).strip()


def split(name):
    """(first, last). "Pat Q. Example" -> ("Pat Q.", "Example"); a lone word
    is a first name with no surname; a generation or degree suffix ("Jr.",
    "III", "MD") is dropped from the end."""
    parts = clean(name).split()
    while len(parts) > 1 and _LETTERS.sub("", parts[-1].lower()) in _SUFFIXES:
        parts.pop()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def first_key(first):
    """The first token of a first name, letters only, lower case. "J." is
    "j": one letter, an initial, which never counts as a match."""
    toks = str(first or "").split()
    return _LETTERS.sub("", toks[0].lower()) if toks else ""


def surname_key(last):
    return _LETTERS.sub("", str(last or "").lower())


def nick_set(first):
    """Every spelling the table calls equivalent to this first name,
    including itself; {} for an empty or initial-only first name."""
    k = first_key(first)
    if len(k) < 2:
        return set()
    return set(_GROUPS.get(k, {k})) | {k}


def same_first(a, b):
    """Same first name, or nickname-equivalent ("Matt" / "Matthew").
    Initials and empty names never match."""
    ka, kb = first_key(a), first_key(b)
    if len(ka) < 2 or len(kb) < 2:
        return False
    return ka == kb or kb in nick_set(a)


def compatible(name_a, name_b):
    """Could these be one person? Both carry a first name and a surname,
    the surnames agree, and the first names are the same or nickname-
    equivalent."""
    return bool(why(name_a, name_b))


def why(name_a, name_b):
    """"same name", "nickname" or "" - the reason compatible() says yes."""
    fa, la = split(name_a)
    fb, lb = split(name_b)
    if not (fa and la and fb and lb):
        return ""
    if surname_key(la) != surname_key(lb):
        return ""
    ka, kb = first_key(fa), first_key(fb)
    if len(ka) < 2 or len(kb) < 2:
        return ""
    if ka == kb:
        return "same name"
    if kb in nick_set(fa):
        return "nickname"
    return ""

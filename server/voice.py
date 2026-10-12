"""The owner's writing voice, learned from what they actually sent.

Every drafting prompt used to say "match the owner's voice as evidenced in
the thread". That had two holes. An email reply was drafted against the
iMessage thread, so the only evidence of voice was the wrong register. And
a thread of five messages is not evidence of anything: the model filled the
gap with its own defaults, which is exactly the generic-assistant prose the
owner then had to rewrite.

This module measures the voice instead of hoping the model infers it. From
the owner's own sent messages in the text index (iMessage and every
connected mailbox) it computes, deterministically and with no model call:

  - how they open (greeting forms with their share) and how they close
    (sign-off forms and the name they sign with),
  - message and sentence length,
  - mechanics: lowercase starts, final punctuation, exclamations, emoji,
    the shorthand they really use,
  - a formality register,

per channel, per relationship class from the CRM, and per person where
there is enough history. `block()` renders the most specific bucket that
has enough samples into a short prompt section, with a few of the owner's
real messages as examples, and every drafting site carries it.

The store (data/voice.json) is derived: it is rebuilt from the text index
whenever it is older than MAX_AGE_S, so it never joins backup.FILES. It
never raises into a drafting path: with no index or no sent history the
block is empty and the prompt reads exactly as it did before.
"""
import re
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import jsonstore, settings

STORE = settings.ROOT / "data" / "voice.json"
VERSION = 1

# A rebuild reads the index once and takes a second or two. Three days keeps
# the voice current with how the owner writes now without doing that work on
# every draft.
MAX_AGE_S = 3 * 86_400
# Just after the mail sweep is switched on the store has no email voice,
# and the sweep fills the index a batch at a time; re-learning hourly until
# email has enough samples means email drafts pick it up the same day.
EMPTY_MAIL_AGE_S = 3600

# The most recent sent messages read per source. 6000 is years of texting
# and well past the point where the shares stop moving; reading the whole
# index would weight a voice the owner had a decade ago as heavily as today's.
SAMPLE_CAP = 6000

# Only the head of each body is read. The owner's own words sit at the top
# of a sent email; what follows is quoted history and signature blocks.
HEAD_CHARS = 4000

# Minimum samples before a bucket speaks for itself. Below these a share is
# noise ("100% sign off with Cheers" from two emails), so the block falls
# back to the next broader bucket instead.
MIN_PERSON = 8
MIN_CLASS = 12
MIN_CHANNEL = 12

# People kept in the store, most samples first. 400 covers everyone the
# owner writes to often enough to have a per-person voice; the long tail
# falls back to their relationship class.
MAX_PEOPLE = 400

# Example messages quoted into the prompt per bucket, and the length window
# they are picked from. Short enough to stay a small share of any drafting
# prompt (well under 2,000 characters in all), long enough to show rhythm.
EXAMPLES = 3
EXAMPLE_WORDS = (4, 80)
EXAMPLE_CHARS = 400

# Each voice channel is one slice of the index: (source, is_group). Group
# chats are their own register (shorter, more banter, nobody greeted), so a
# group reply is voiced from the owner's group messages, not their 1:1 texts.
CHANNELS = {"imessage": ("imessage", 0), "email": ("email", 0),
            "group": ("imessage", 1)}
CHANNEL_LABEL = {"imessage": "texts", "email": "email",
                 "group": "group-chat messages"}

_TAPBACK = re.compile(
    r"^(Liked|Loved|Laughed at|Emphasized|Disliked|Questioned|Reacted .{1,8} to)"
    r" [“\"]")
_AUTO_SUBJECT = re.compile(
    r"^(Accepted|Declined|Tentative|Tentatively Accepted|Invitation|"
    r"Updated invitation|Canceled|Cancelled|Automatic reply)\b", re.I)
_URL_ONLY = re.compile(r"^\s*https?://\S+\s*$")
# Where the owner's words end in a sent email. The reply header is matched
# case-sensitively and must start like a date ("On Mon, ..." / "On 5 Oct
# ..."), or "... more on item 3 than Raj wrote:" would cut the owner's own
# sentence in half.
_QUOTE_CUT = re.compile(
    r"(\bOn (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
    r"Sep|Oct|Nov|Dec|\d)[^\n]{4,140}? wrote:|"
    r"(?i:-{2,}\s*Original Message\s*-{2,}|-{5,} ?Forwarded message|_{10,}|"
    r"\bFrom: [^\n]{1,200}?\bSent: |\bSent from my \w+|"
    r"\bGet Outlook for \w+))")

# Longest first, so "hi all" wins over "hi" and "good morning" over "morning".
GREETINGS = (
    "good morning", "good afternoon", "good evening", "hope you're well",
    "hope you are well", "hi all", "hi team", "hi everyone", "hey all",
    "hey there", "hi there", "hello", "dear", "hiya", "howdy", "greetings",
    "morning", "hey", "heya", "hi", "yo",
)
SIGNOFFS = (
    "best regards", "kind regards", "warm regards", "warmest regards",
    "all the best", "thanks so much", "thank you", "many thanks",
    "thanks again", "talk soon", "speak soon", "sincerely", "regards",
    "warmly", "cheers", "thanks", "best", "thx", "love", "yours", "xo",
)
FORMAL_GREETINGS = {"dear", "hello", "good morning", "good afternoon",
                    "good evening", "greetings", "hope you are well"}
FORMAL_SIGNOFFS = {"best regards", "kind regards", "warm regards",
                   "warmest regards", "regards", "sincerely"}
# Shorthand that marks a casual register (unlike "ok" or "yeah", which
# careful writers use too).
CASUAL_SHORTHAND = {"lol", "haha", "lmao", "omg", "tbh", "btw", "lmk", "thx",
                    "ty", "omw", "idk", "nvm", "gonna", "wanna", "u", "ur",
                    "pls", "plz", "rn", "yep", "yup", "ya"}
SHORTHAND = ("lol", "haha", "hah", "lmao", "omg", "tbh", "btw", "imo", "fwiw",
             "lmk", "np", "thx", "ty", "omw", "idk", "nvm", "ok", "k", "yep",
             "yup", "nope", "ya", "yeah", "gonna", "wanna", "u", "ur", "pls",
             "plz", "def", "prob", "rn", "fyi", "asap")

# The greeting and sign-off words match in any case; the name after them
# must be capitalized, so that part is case-sensitive on purpose.
_GREET_RE = re.compile(
    r"^((?i:" + "|".join(re.escape(g) for g in GREETINGS) + r"))\b"
    r"(?:[ ,]+([A-Z][\w'-]{0,20}|(?i:team|all|there|everyone|folks|guys|both)\b))?\s*([,!.:\-—]?)")
# A sign-off follows a sentence end or starts with a capital ("... know
# Best, M"); "this is the best" at the end of a line is not one.
_SIGN_RE = re.compile(
    r"(?:(?<=[.!?])\s+|\s+(?=[A-Z])|^)"
    r"((?i:" + "|".join(re.escape(s) for s in SIGNOFFS) + r"))\b"
    r"\s*([,!.\-—]?)\s*([A-Z][\w'.-]{0,15})?\s*$")
# Words a greeting can be followed by that are an audience, not a name.
_AUDIENCE = {"team", "all", "there", "everyone", "folks", "guys", "both"}
# Capitalized words that start the message body rather than name anyone.
_NOT_NAME = {"thanks", "thank", "just", "so", "i", "quick", "hope", "sorry",
             "great", "good", "yes", "no", "ok", "okay", "sure"}
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF]")
_CONTRACTION = re.compile(r"\b\w+['’](s|re|ve|ll|d|t|m)\b", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z']+")

_build_lock = threading.Lock()


# ---------- reading the owner's sent messages ----------

def _sent_rows(con, source, is_group):
    return con.execute(
        "SELECT chat_pid, chat_id, subject, substr(text, 1, ?) AS text FROM items"
        " WHERE from_me=1 AND is_group=? AND source=?"
        " ORDER BY date_ns DESC LIMIT ?",
        (HEAD_CHARS, is_group, source, SAMPLE_CAP)).fetchall()


def _chat_people():
    """chat_id -> person for 1:1 iMessage chats, resolved against today's
    CRM. The index stamps chat_pid when a row is first indexed, so a person
    added to the CRM later has years of rows with no pid; the voice would
    otherwise never learn how the owner writes to them. {} without chat.db."""
    from . import imessage, mediaindex
    try:
        src = imessage._connect()
    except Exception:  # noqa: BLE001 - no Full Disk Access or no Messages
        return {}
    try:
        return mediaindex._chat_maps(src)[1]
    except Exception:  # noqa: BLE001
        return {}
    finally:
        src.close()


def _own_words(text, source):
    """The part of a sent body the owner actually wrote: quoted history,
    forwarded blocks and mobile footers cut away. Empty means skip it."""
    text = (text or "").strip()
    if not text or _TAPBACK.match(text) or _URL_ONLY.match(text):
        return ""
    if source == "email":
        m = _QUOTE_CUT.search(text)
        if m:
            text = text[:m.start()]
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _greeting(text, first_name=None):
    m = _GREET_RE.match(text)
    if not m and first_name:
        # "Sean, can you ..." - the name alone is the greeting. Only the
        # recipient's own first name counts, so "Awesome, thanks" never does.
        n = re.match(r"^(" + re.escape(first_name) + r")\b\s*([,!:\-\u2014]?)",
                     text, re.I)
        if n:
            return "<name>" + n.group(2), "name"
    if not m:
        return None, None
    word, name, punct = m.group(1), m.group(2), m.group(3) or ""
    form = re.sub(r"\s+", " ", word)
    if name and name.lower() in _AUDIENCE:
        form += " " + name.lower() + punct
    elif name and name.lower() in _NOT_NAME:
        pass  # "Hi Thanks for this": the body started, no name, no punctuation
    elif name:
        form += " <name>" + punct
    else:
        form += punct
    return form, word.lower()


def _signoff(text):
    m = _SIGN_RE.search(text[-120:])
    if not m:
        return None, None, None, text
    phrase = m.group(1)
    sign_name = m.group(3)
    # A body that is nothing but "Thanks!" is a message, not a sign-off.
    start = len(text) - len(text[-120:]) + m.start(1)
    if start == 0:
        return None, None, None, text
    form = re.sub(r"\s+", " ", phrase) + (m.group(2) or "")
    if sign_name:
        form += " <signature>"
    return form, phrase.lower(), sign_name, text[:start].rstrip(" ,-")


def features(text, source, first_name=None):
    """Deterministic traits of one sent message, or None to skip it.
    `first_name` is the recipient's, when known, to spot a name-only
    greeting."""
    text = _own_words(text, source)
    if len(text) < 2:
        return None
    greet_form, greet_word = _greeting(text, first_name)
    sign_form, sign_word, sign_name, body = (
        _signoff(text) if source == "email" else (None, None, None, text))
    words = _WORD.findall(body)
    if not words:
        return None
    sentences = [s for s in _SENT_SPLIT.split(body) if _WORD.search(s)]
    lowered = [w.lower() for w in words]
    first_alpha = next((c for c in body if c.isalpha()), "")
    last = body.rstrip()[-1:]
    formal = sum((greet_word in FORMAL_GREETINGS, sign_word in FORMAL_SIGNOFFS))
    casual = sum((first_alpha.islower(), bool(_EMOJI.search(body)),
                  any(w in CASUAL_SHORTHAND for w in lowered),
                  bool(re.search(r"\bi\b", body))))
    return {
        "words": len(words),
        "sentence_words": len(words) / max(1, len(sentences)),
        "greet": greet_form, "sign": sign_form, "sign_name": sign_name,
        "lower_start": first_alpha.islower(),
        "end_period": last == ".",
        "end_bare": last.isalnum(),
        "exclaim": "!" in body,
        "question": "?" in body,
        "emoji": bool(_EMOJI.search(body)),
        "ellipsis": "..." in body or "…" in body,
        "contractions": bool(_CONTRACTION.search(body)),
        "shorthand": sorted({w for w in lowered if w in SHORTHAND}),
        "formality": formal - casual,
        "text": text,
    }


# ---------- aggregating ----------

def _pct(n, total):
    return round(100 * n / total) if total else 0


def _top(counter, total, k=3, floor=8):
    """Top forms with their share; anything under `floor` percent is noise."""
    out = []
    for form, n in counter.most_common(k):
        p = _pct(n, total)
        if p >= floor:
            out.append({"form": form, "pct": p})
    return out


def _examples(feats, k):
    lo, hi = EXAMPLE_WORDS
    seen, out = set(), []
    for f in feats:  # newest first
        if not (lo <= f["words"] <= hi):
            continue
        key = f["text"][:40].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(f["text"][:EXAMPLE_CHARS])
        if len(out) >= k:
            break
    return out


def summarize(feats, examples=EXAMPLES):
    """One bucket's statistics from a newest-first list of features."""
    n = len(feats)
    if not n:
        return {"n": 0}
    words = sorted(f["words"] for f in feats)
    q = statistics.quantiles(words, n=4) if n >= 2 else [words[0]] * 3
    greets = Counter(f["greet"] or "" for f in feats)
    signs = Counter(f["sign"] or "" for f in feats)
    names = Counter(f["sign_name"] for f in feats if f["sign_name"])
    shorthand = Counter(w for f in feats for w in f["shorthand"])
    formality = statistics.fmean(f["formality"] for f in feats)
    register = ("formal" if formality >= 0.4 else
                "casual" if formality <= -0.5 else "neutral")
    return {
        "n": n,
        "words_median": round(statistics.median(words)),
        "words_p25": round(q[0]), "words_p75": round(q[2]),
        "sentence_words": round(statistics.median(
            f["sentence_words"] for f in feats), 1),
        "greetings": _top(Counter({k: v for k, v in greets.items() if k}), n),
        "no_greeting_pct": _pct(greets.get("", 0), n),
        "signoffs": _top(Counter({k: v for k, v in signs.items() if k}), n),
        "no_signoff_pct": _pct(signs.get("", 0), n),
        "sign_name": names.most_common(1)[0][0] if names else None,
        "lower_start_pct": _pct(sum(f["lower_start"] for f in feats), n),
        "end_period_pct": _pct(sum(f["end_period"] for f in feats), n),
        "end_bare_pct": _pct(sum(f["end_bare"] for f in feats), n),
        "exclaim_pct": _pct(sum(f["exclaim"] for f in feats), n),
        "question_pct": _pct(sum(f["question"] for f in feats), n),
        "emoji_pct": _pct(sum(f["emoji"] for f in feats), n),
        "ellipsis_pct": _pct(sum(f["ellipsis"] for f in feats), n),
        "contraction_pct": _pct(sum(f["contractions"] for f in feats), n),
        "shorthand": _top(shorthand, n, k=5, floor=4),
        "register": register,
        "examples": _examples(feats, examples),
    }


def _empty_reason(src):
    """Why a channel has no samples, in words the owner can act on."""
    if src == "email" and not settings.get("mail_body_index"):
        return ("mail bodies are not indexed (mail_body_index is off), so "
                "there is no sent mail to learn from")
    return f"no sent {CHANNEL_LABEL[src]} in the text index yet"


def build(con=None, profiles=None, chat_people=None):
    """Read the owner's sent messages and write the voice store.

    `con` is an open text-index connection, `profiles` the CRM profile map
    and `chat_people` the chat_id -> person map; all default to the real
    ones and exist so tests pass synthetic sources. Returns the payload."""
    from . import textindex
    names = {}
    if profiles is None:
        from . import data as crm
        profiles = crm.profiles_map()
        # everyone the CRM knows has a name, not only those with a profile
        names = {pid: p.get("name") for pid, p in crm._load()["by_id"].items()}
    if chat_people is None:
        chat_people = _chat_people()
    own = con is None
    if own:
        if not textindex.DB.exists():
            return {"status": "unavailable", "reason": "text index not built"}
        con = sqlite3.connect(textindex.DB, timeout=60)
        con.row_factory = sqlite3.Row
    try:
        rows = {ch: _sent_rows(con, *CHANNELS[ch]) for ch in CHANNELS}
    finally:
        if own:
            con.close()

    channels, people, samples = {}, defaultdict(dict), {}
    for ch, src_rows in rows.items():
        src, is_group = CHANNELS[ch]
        overall, by_class, by_person = [], defaultdict(list), defaultdict(list)
        for r in src_rows:
            if src == "email" and _AUTO_SUBJECT.match(r["subject"] or ""):
                continue
            pid = None if is_group else (
                r["chat_pid"] or (chat_people.get(r["chat_id"])
                                  if src == "imessage" else None))
            first = ((profiles.get(pid) or {}).get("name")
                     or names.get(pid) or "").split()[:1]
            f = features(r["text"], src, first[0] if first else None)
            if not f:
                continue
            overall.append(f)
            if pid:  # a group has no one person or class to learn
                by_person[pid].append(f)
                cls = ((profiles.get(pid) or {}).get("relationship_class")
                       or "").strip().lower()
                if cls:
                    by_class[cls].append(f)
        samples[ch] = len(overall)
        channels[ch] = {
            "overall": summarize(overall) if overall else
                       {"n": 0, "reason": _empty_reason(ch)},
            "by_class": {c: summarize(fs) for c, fs in by_class.items()
                         if len(fs) >= MIN_CLASS},
        }
        ranked = sorted(by_person.items(), key=lambda kv: -len(kv[1]))
        for pid, fs in ranked[:MAX_PEOPLE]:
            if len(fs) >= MIN_PERSON:
                people[pid][ch] = summarize(fs, examples=2)

    payload = {"version": VERSION, "status": "ok",
               "built_at": datetime.now(timezone.utc).isoformat(),
               "samples": samples, "channels": channels,
               "people": dict(people)}
    jsonstore.write_atomic(STORE, payload, ensure_ascii=False)
    return payload


def _stale(store):
    if store.get("version") != VERSION or store.get("status") != "ok":
        return True
    try:
        built = datetime.fromisoformat(store["built_at"])
    except (KeyError, TypeError, ValueError):
        return True
    age = (datetime.now(timezone.utc) - built).total_seconds()
    if (age > EMPTY_MAIL_AGE_S and settings.get("mail_body_index")
            and (store.get("samples") or {}).get("email", 0) < MIN_CHANNEL):
        return True
    return age > MAX_AGE_S


def load(refresh=True):
    """The voice store, rebuilt first when missing or older than MAX_AGE_S.

    A failed rebuild returns the old store if there is one (marked stale)
    and otherwise a named status; it is never cached as a fact."""
    store = jsonstore.read(STORE, {})
    if not refresh or not _stale(store):
        return store or {"status": "unavailable", "reason": "not built yet"}
    with _build_lock:
        store = jsonstore.read(STORE, {})
        if not _stale(store):
            return store
        try:
            from . import admission
            if admission.is_worker():
                with admission.cpu("voice.build"):
                    return build()
            return build()
        except Exception as e:  # noqa: BLE001 - enrichment, never a gate
            if store:
                return dict(store, stale=True, error=str(e)[:200])
            return {"status": "unavailable", "reason": str(e)[:200]}


# ---------- rendering for a prompt ----------

def channel_of(channel):
    c = (channel or "").lower()
    if c in ("email", "mail"):
        return "email"
    return "group" if c == "group" else "imessage"


def _forms(items):
    return ", ".join(f'"{x["form"]}" ({x["pct"]}%)' for x in items)


def describe(s):
    """A bucket's statistics as plain prompt lines."""
    lines = [f"- Length: usually about {s['words_median']} words "
             f"(most between {s['words_p25']} and {s['words_p75']}); "
             f"sentences run about {s['sentence_words']} words."]
    if s.get("greetings"):
        lines.append(f"- Opens with {_forms(s['greetings'])}; "
                     f"no greeting at all {s['no_greeting_pct']}% of the time.")
    else:
        lines.append("- Rarely opens with a greeting; starts straight in.")
    if s.get("signoffs"):
        name = f' (signs as "{s["sign_name"]}")' if s.get("sign_name") else ""
        lines.append(f"- Closes with {_forms(s['signoffs'])}{name}; "
                     f"no sign-off {s['no_signoff_pct']}% of the time.")
    mech = [f"starts lowercase {s['lower_start_pct']}%",
            f"ends with no final punctuation {s['end_bare_pct']}%",
            f"ends with a period {s['end_period_pct']}%",
            f"uses an exclamation mark {s['exclaim_pct']}%",
            f"uses emoji {s['emoji_pct']}%",
            f"uses contractions {s['contraction_pct']}%"]
    lines.append("- Mechanics (share of messages): " + "; ".join(mech) + ".")
    if s.get("shorthand"):
        lines.append("- Shorthand they really use: " + _forms(s["shorthand"]) + ".")
    lines.append(f"- Register: {s['register']}.")
    return lines


def _class_line(ch):
    classes = ch.get("by_class") or {}
    if len(classes) < 2:
        return ""
    parts = [f"{c} {s['register']}, ~{s['words_median']} words"
             for c, s in sorted(classes.items(), key=lambda kv: -kv[1]["n"])[:6]]
    return "- By relationship: " + "; ".join(parts) + "."


def pick(store, person_id, channel):
    """(stats, scope) for the most specific bucket with enough samples."""
    from . import data as crm
    ch = (store.get("channels") or {}).get(channel) or {}
    if person_id:
        s = ((store.get("people") or {}).get(person_id) or {}).get(channel)
        if s and s.get("n", 0) >= MIN_PERSON:
            return s, "person"
        cls = ((crm.profiles_map().get(person_id) or {})
               .get("relationship_class") or "").strip().lower()
        s = (ch.get("by_class") or {}).get(cls)
        if s and s.get("n", 0) >= MIN_CLASS:
            return s, f"class:{cls}"
    s = ch.get("overall")
    if s and s.get("n", 0) >= MIN_CHANNEL:
        return s, "overall"
    return None, None


def block(person_id=None, channel="imessage", owner="the owner", name=None):
    """The voice section for a drafting prompt. Empty when nothing is known,
    so a prompt without it reads exactly as it did before. Never raises."""
    try:
        store = load()
        channel = channel_of(channel)
        s, scope = pick(store, person_id, channel)
        if not s:
            return ""
        label = CHANNEL_LABEL[channel]
        if scope == "person":
            whom = f"to {name or 'this person'}"
        elif scope and scope.startswith("class:"):
            whom = f"to people {owner} counts as {scope[6:]}"
        elif channel == "group":
            whom = "in group chats"
        else:
            whom = "to everyone"
        lines = [f"{owner}'s writing voice in {label} {whom}, measured from "
                 f"{s['n']} messages {owner} actually sent. Write the draft "
                 f"the way {owner} writes, not the way an assistant writes: "
                 f"match these habits, including the informal ones, and do "
                 f"not polish them away."]
        lines += describe(s)
        if scope == "overall":
            extra = _class_line((store.get("channels") or {}).get(channel) or {})
            if extra:
                lines.append(extra)
        if s.get("examples"):
            lines.append(f"Real {label} {owner} sent {whom} (for rhythm and "
                         f"habits only; never reuse their content):")
            lines += [f"  > {x}" for x in s["examples"]]
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - enrichment, never a gate
        return ""


def overview():
    """The learned voice for the API: per channel, overall and by class,
    with how many people have their own bucket. Examples are left out."""
    store = load()
    if store.get("status") != "ok":
        return {"status": store.get("status", "unavailable"),
                "reason": store.get("reason")}

    def strip(s):
        return {k: v for k, v in s.items() if k != "examples"}
    out = {"status": "ok", "built_at": store.get("built_at"),
           "mail_body_index": bool(settings.get("mail_body_index")),
           "stale": bool(store.get("stale")), "samples": store.get("samples"),
           "channels": {}}
    for ch, data in (store.get("channels") or {}).items():
        out["channels"][ch] = {
            "overall": strip(data.get("overall") or {"n": 0}),
            "by_class": {c: strip(s) for c, s in
                         (data.get("by_class") or {}).items()},
            "people": sum(1 for p in (store.get("people") or {}).values()
                          if ch in p),
        }
    return out


def _main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m server.voice")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="rebuild data/voice.json from the text index")
    sub.add_parser("show", help="print the learned voice")
    b = sub.add_parser("block", help="print the prompt block for a person")
    b.add_argument("--person")
    b.add_argument("--channel", default="imessage")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        t = time.monotonic()
        out = build()
        print(json.dumps({"status": out.get("status"),
                          "samples": out.get("samples"),
                          "people": len(out.get("people") or {}),
                          "seconds": round(time.monotonic() - t, 2)}))
    elif args.cmd == "show":
        print(json.dumps(overview(), indent=1, ensure_ascii=False))
    else:
        print(block(args.person, args.channel) or "(no voice learned)")


if __name__ == "__main__":
    _main()

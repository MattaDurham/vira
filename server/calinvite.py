"""RSVP to a calendar invitation that arrived as email.

Vira could not answer an invite before this module: Graph is consented for
`Calendars.Read` only (msgraph.SCOPE_CAL), the local Calendar store is read
at `~/Library/Group Containers/.../Calendar.sqlitedb` and never written, and
a Vira session is given only the in-process `vira` MCP server (runner.py) —
so it does not inherit this machine's Google Calendar tooling either. The
one surface that CAN answer is the invitation email itself, which is where
both of the mechanisms below live.

TWO RUNGS, most specific first, and the ladder reports which one ran:

  1. THE ORGANIZER'S OWN RSVP LINK. A Google invitation carries three
     `calendar.google.com/calendar/event?action=RESPOND...&rst=N` links —
     rst 1/2/3 for yes/no/maybe — each with a `tok` that authenticates the
     response. This is the mechanism Google puts in the mail FOR the
     recipient to use, so it is the likeliest to be honoured, and it needs
     no scope, no credential and no send.

  2. AN iCALENDAR REPLY BY EMAIL (RFC 5546). Every invite worth answering
     carries a `text/calendar; method=REQUEST` part; the standard response
     is a `METHOD:REPLY` carrying the same UID and one ATTENDEE line whose
     PARTSTAT is the answer, mailed to the organizer. This is how any
     non-Google client RSVPs, so it is the rung that generalizes — Google,
     Exchange and Apple all consume it — and it rides the SMTP path
     mailread already owns.

  3. Neither available -> an honest refusal naming what was missing. A
     surface may only claim what it can see.

Measured against a real Google invitation on the live mailbox
2026-08-28: it carried BOTH — three RESPOND links and a
`TEXT/CALENDAR (METHOD REQUEST)` part beside an `invite.ics`
attachment — so both rungs have a real foundation here.

RSVPing is an OUTWARD action: it tells another person you are coming. It is
therefore never taken on a bare texted instruction; server/inbound.py holds
it and confirms first. VIRA_PASSIVE refuses outright — a branch instance
must never answer the owner's real invitations (the send.py precedent).
"""
import email
import email.utils
import html
import imaplib
import os
import re
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

from . import mail as mailmod
from . import mailread, settings

IMAP_TIMEOUT = 30
HTTP_TIMEOUT = 20
SMTP_TIMEOUT = 30

# yes/no/maybe -> (Google rst, iCalendar PARTSTAT, human word)
ANSWERS = {
    "yes":   ("1", "ACCEPTED",  "yes"),
    "no":    ("2", "DECLINED",  "no"),
    "maybe": ("3", "TENTATIVE", "maybe"),
}

_RESPOND_RE = re.compile(
    r'https://calendar\.google\.com/calendar/event\?[^"\'<>\s)]*action=RESPOND'
    r'[^"\'<>\s)]*', re.I)


class RsvpError(RuntimeError):
    """Raised with a sentence meant for the owner, not a stack trace."""


def norm_answer(text):
    """Map what a person actually types to one of the three answers, or None.

    Deliberately narrow: this decides an outward action, so an unrecognised
    word must read as "I don't know what you meant" rather than being
    coerced to the nearest option.
    """
    t = (text or "").strip().lower().strip(".!,")
    if t in ("yes", "y", "yep", "yeah", "yes please", "accept", "accepted",
             "going", "i'm in", "im in", "ok", "okay", "sure", "rsvp yes",
             "reply yes"):
        return "yes"
    if t in ("no", "n", "nope", "decline", "declined", "can't", "cant",
             "can't make it", "cant make it", "rsvp no", "reply no"):
        return "no"
    if t in ("maybe", "tentative", "perhaps", "possibly", "rsvp maybe",
             "reply maybe"):
        return "maybe"
    return None


# ---------- reading the invitation ----------

def _raw_message(acct, rowid):
    """The full RFC822 message, which mailread.get_message does not return —
    rung 2 needs the `text/calendar` part, not the rendered body."""
    addr, host = acct["email"], acct.get("host", "")
    pw = mailmod.keychain_password(addr)
    if not pw:
        raise RsvpError(f"no password in the keychain for {addr}")
    uid = mailread._imap_uid(rowid, addr)
    if not uid:
        raise RsvpError("that notification carries no mailbox id to fetch")
    con = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)
    try:
        con.login(addr, pw)
        con.select("INBOX", readonly=True)
        _, md = con.uid("fetch", str(uid), "(RFC822)")
        if not md or md[0] is None:
            raise RsvpError("that message is no longer in the inbox")
        return email.message_from_bytes(md[0][1])
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001 — best-effort close
            pass


def calendar_part(msg):
    """The invitation's iCalendar text, from the inline part or the
    attachment. Returns "" when this is not an invitation at all."""
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        name = (part.get_filename() or "").lower()
        if ctype == "text/calendar" or name.endswith(".ics"):
            try:
                raw = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001 — a malformed part is not an invite
                continue
            for enc in ("utf-8", "latin-1"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
    return ""


def respond_link(body_html, answer):
    """The organizer's own RSVP link for this answer, or "".

    Ampersands arrive HTML-escaped in the markup, so the URL is unescaped
    before use — fetching the escaped form would send `&amp;rst=1` and the
    answer would be dropped.
    """
    rst = ANSWERS[answer][0]
    for raw in _RESPOND_RE.findall(body_html or ""):
        url = html.unescape(raw)
        if re.search(rf"[?&]rst={rst}(?:&|$)", url):
            return url
    return ""


def _ical_fold(line):
    """iCalendar lines are folded at 75 octets; an unfolded long ORGANIZER
    or UID is a spec violation some servers reject outright."""
    out, cur = [], line
    while len(cur.encode("utf-8")) > 75:
        cut = 74
        while cut > 1 and len((cur[:cut]).encode("utf-8")) > 75:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def vevent(ics):
    """Just the VEVENT block.

    Load-bearing, not tidiness: a Google invitation opens with a VTIMEZONE
    whose DAYLIGHT/STANDARD subcomponents carry their OWN DTSTART (the DST
    transition, e.g. 19700308T020000). A whole-file property scan returns
    that one — it appears first — so a reply built from it would carry a
    1970 start date. Measured on the real invitation before this existed.
    """
    m = re.search(r"^BEGIN:VEVENT\r?$(.*?)^END:VEVENT\r?$",
                  ics or "", re.I | re.M | re.S)
    return m.group(1) if m else (ics or "")


def _prop(ics, name):
    """The first value of a property, unfolded. Returns (params, value).

    Always give this a VEVENT (see above), never the whole calendar.
    """
    text = re.sub(r"\r?\n[ \t]", "", ics or "")
    m = re.search(rf"^{name}([^:\r\n]*):(.*)$", text, re.I | re.M)
    if not m:
        return "", ""
    return m.group(1), m.group(2).strip()


def build_reply(ics, attendee, answer, name=""):
    """An RFC 5546 METHOD:REPLY for this invitation.

    Carries the REQUEST's own UID and SEQUENCE — those are what tie the
    reply to the event; a reply with a fresh UID creates a new event on the
    organizer's calendar instead of answering theirs.
    """
    partstat = ANSWERS[answer][1]
    ev = vevent(ics)
    uid = _prop(ev, "UID")[1]
    if not uid:
        raise RsvpError("the invitation carries no event id to answer")
    seq = _prop(ev, "SEQUENCE")[1] or "0"
    org_params, org_val = _prop(ev, "ORGANIZER")
    if not org_val:
        raise RsvpError("the invitation names no organizer to reply to")
    summary = _prop(ev, "SUMMARY")[1]
    dtstart_p, dtstart = _prop(ev, "DTSTART")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cn = f';CN="{name}"' if name else ""
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Vira//Reply//EN",
        "VERSION:2.0",
        "METHOD:REPLY",
        "BEGIN:VEVENT",
        _ical_fold(f"UID:{uid}"),
        f"SEQUENCE:{seq}",
        f"DTSTAMP:{stamp}",
        _ical_fold(f"ORGANIZER{org_params}:{org_val}"),
        _ical_fold(f"ATTENDEE{cn};PARTSTAT={partstat}:mailto:{attendee}"),
    ]
    if dtstart:
        lines.append(_ical_fold(f"DTSTART{dtstart_p}:{dtstart}"))
    if summary:
        lines.append(_ical_fold(f"SUMMARY:{summary}"))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n", org_val, summary


# ---------- taking the action ----------

def _fetch_link(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Vira"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.status, (r.read(4096) or b"").decode("utf-8", "replace")


def _send_ical_reply(acct, ics, organizer, subject, answer, summary):
    addr = acct["email"]
    pw = mailmod.keychain_password(addr)
    if not pw:
        raise RsvpError(f"no password in the keychain for {addr}")
    to = re.sub(r"^mailto:", "", organizer, flags=re.I).strip()
    word = ANSWERS[answer][2]
    msg = EmailMessage()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = f"{word.capitalize()}: {summary or subject}"
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(f"{word.capitalize()} — {summary or subject}")
    msg.add_alternative(ics, subtype="calendar",
                        params={"method": "REPLY", "charset": "UTF-8"})
    host = mailread.smtp_host_for(acct)
    with smtplib.SMTP_SSL(host, 465, timeout=SMTP_TIMEOUT) as s:
        s.login(addr, pw)
        s.send_message(msg)
    return to


def rsvp(account, rowid, answer, *, dry_run=False):
    """Answer the invitation that arrived as this email.

    `dry_run` walks the whole ladder and reports which rung WOULD run
    without taking the action — what the confirmation text is composed
    from, and the only way to exercise this against a real invitation
    without answering it.
    """
    if answer not in ANSWERS:
        raise RsvpError(f"answer must be one of {', '.join(ANSWERS)}")
    if os.environ.get("VIRA_PASSIVE") and not dry_run:
        raise RsvpError("passive test instance — RSVP is blocked here so a "
                        "branch copy can never answer a real invitation")
    acct = mailread._account(account)
    if not acct:
        raise RsvpError(f"no mail account configured for {account}")
    if acct.get("type") == "graph":
        raise RsvpError(
            "that invite is in the M365 mailbox, which Vira can only read "
            "(Calendars.Read); answering it needs Calendars.ReadWrite "
            "granted in Entra")

    msg = _raw_message(acct, rowid)
    ics = calendar_part(msg)
    body_html = ""
    for part in msg.walk():
        if (part.get_content_type() or "").lower() == "text/html":
            try:
                body_html = (part.get_payload(decode=True) or b"").decode(
                    part.get_content_charset() or "utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            break
    subject = mailread._decode_header(msg.get("Subject") or "") \
        if hasattr(mailread, "_decode_header") else (msg.get("Subject") or "")
    summary = _prop(vevent(ics), "SUMMARY")[1] if ics else ""

    if not ics and "action=RESPOND" not in (body_html or ""):
        raise RsvpError("that email is not a calendar invitation")

    # Rung 1 — the organizer's own link.
    link = respond_link(body_html, answer)
    if link:
        if dry_run:
            return {"ok": True, "via": "respond-link", "dry_run": True,
                    "event": summary or subject, "detail": "would open the "
                    "organizer's own RSVP link"}
        try:
            status, _ = _fetch_link(link)
            return {"ok": True, "via": "respond-link", "status": status,
                    "event": summary or subject,
                    "detail": f"answered {ANSWERS[answer][2]} through the "
                              "organizer's RSVP link"}
        except (urllib.error.URLError, OSError) as e:
            last = f"the RSVP link failed ({e})"
    else:
        last = "the invitation carries no RSVP link"

    # Rung 2 — the standards path.
    if ics:
        reply, organizer, summary = build_reply(
            ics, acct["email"], answer, settings.get("owner_name") or "")
        if dry_run:
            return {"ok": True, "via": "ical-reply", "dry_run": True,
                    "event": summary or subject, "to": organizer,
                    "detail": "would email an iCalendar reply to the organizer"}
        to = _send_ical_reply(acct, reply, organizer, subject, answer, summary)
        return {"ok": True, "via": "ical-reply", "to": to,
                "event": summary or subject,
                "detail": f"emailed {ANSWERS[answer][2]} to {to}"}

    raise RsvpError(f"could not answer that invitation — {last}, and it "
                    "carries no iCalendar part to reply to")

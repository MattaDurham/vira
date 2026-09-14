"""Source-backed action links. Pure parsing: no requests, redirects, or state.

Labels describe links present in a message, not verified merchant identities.
Keep the actual destination domain visible and preserve signed URLs unchanged.
"""
from html.parser import HTMLParser
import ipaddress
import re
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

BODY_MAX = 400_000
LINK_MAX = 20
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_JUNK = re.compile(
    r"unsubscribe|opt[\s_-]?out|manage[\s_-]?(?:email[\s_-]?)?preferences|"
    r"email[\s_-]preferences|view[\s_-](?:in[\s_-](?:a[\s_-])?browser|online)|"
    r"privacy[\s_-]policy|terms[\s_-](?:of[\s_-])?(?:use|service)", re.I)
_TRACKING = re.compile(r"(?:^|[/_.-])(?:track(?:ing)?|pixel|beacon|open|click)(?:[/_.-]|$)", re.I)
_TRACK_PARAMS = {"gclid", "fbclid", "mc_cid", "mc_eid"}
_KINDS = (
    ("payment", r"\b(?:pay(?:ment)?s?|billing|bill|checkout)\b", "Open payment page"),
    ("document", r"\b(?:statements?|documents?|invoice|receipt|download|pdf)\b", "Open document"),
    ("scheduling", r"\b(?:schedule|scheduling|appointment|booking|calendar|calendly|rsvp)\b", "Open scheduling page"),
    ("account", r"\b(?:account|login|log[ -]?in|sign[ -]?in|portal)\b", "Open account page"),
    ("help", r"\b(?:help|support|contact[ -]?us)\b", "Open help page"),
)


def safe_http_url(value):
    """Return an unchanged navigable URL, or None for unsafe/malformed input."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (not value or len(value) > 4096 or any(c in value for c in '\\<>"')
            or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I)):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme.lower() not in {"http", "https"} or not host
                or parts.username is not None or parts.password is not None):
            return None
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii")
            if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in host.rstrip(".").split(".")):
                return None
    except (ValueError, UnicodeError):
        return None
    return value


def _domain(url):
    return urlsplit(url).hostname.encode("idna").decode("ascii").lower()


def _action(kind, label, url):
    return {"kind": kind, "label": label, "url": url, "domain": _domain(url)}


def _label(value):
    return " ".join("".join(c for c in value if not unicodedata.category(c).startswith("C")
                            or c in "\n\r\t").split())[:100]


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "head"}:
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag == "a":
            self._finish()
            hidden = "hidden" in attrs or re.search(r"display\s*:\s*none", attrs.get("style") or "", re.I)
            if not hidden:
                self.current = [attrs.get("href") or "", [], attrs.get("aria-label") or attrs.get("title") or ""]
        elif tag == "img" and self.current:
            self.current[1].append(attrs.get("alt") or "")

    def handle_endtag(self, tag):
        if tag in self.ignored:
            self.ignored.remove(tag)
        elif tag == "a":
            self._finish()

    def handle_data(self, data):
        if self.current and not self.ignored:
            self.current[1].append(data)

    def _finish(self):
        if self.current and len(self.links) < 200:
            url, text, fallback = self.current
            self.links.append((url, _label(" ".join(text)) or _label(fallback)))
        self.current = None


def _dedupe_key(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in _TRACK_PARAMS]
    return (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
            tuple(query), parts.fragment)


def extract_links(text, html=""):
    """Useful HTML anchors and plain-text HTTP(S) URLs, with no URL inference.

    Discard footer and tracking-only links. A tracked payment/document link
    remains useful, so keep its original URL rather than guessing a redirect.
    """
    candidates = []
    if isinstance(html, str) and html:
        parser = _Anchors()
        parser.feed(html[:BODY_MAX])
        parser.close()
        parser._finish()
        candidates.extend((url, label, label) for url, label in parser.links)
    if isinstance(text, str):
        body = text[:BODY_MAX]
        for match in _URL_RE.finditer(body):
            url = match[0].rstrip(".,;:!?")
            for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
                while url.endswith(closing) and url.count(closing) > url.count(opening):
                    url = url[:-1]
            # Nearby prose helps classify a bare URL, but is never presented
            # as a button label or applied to another paragraph's links.
            start = max(body.rfind("\n", 0, match.start()) + 1, match.start() - 80)
            context = body[start:match.start()]
            candidates.append((url, "", context))
            if len(candidates) >= 400:
                break
    found = {}
    for raw, label, context in candidates:
        url = safe_http_url(raw)
        if not url:
            continue
        parts = urlsplit(url)
        path = unquote(parts.path)
        if _JUNK.search(label + " " + context + " " + path + " " + unquote(parts.query)):
            continue
        kind, fallback = "link", "Open link"
        # The visible anchor is stronger evidence than the hostname: a
        # statement on billing.example.com is still a document link.
        for meaning in (label, _URL_RE.sub("", context), path, _domain(url)):
            for candidate_kind, pattern, candidate_label in _KINDS:
                if re.search(pattern, meaning, re.I):
                    kind, fallback = candidate_kind, candidate_label
                    break
            if kind != "link":
                break
        if (re.search(r"(?:pixel|beacon|tracking)\.(?:gif|png|jpg)$", path, re.I)
                or kind == "link" and _TRACKING.search(_domain(url) + path)):
            continue
        if not label or re.fullmatch(r"(?:click here|here|learn more|https?://.*)", label, re.I):
            label = fallback
        item = _action(kind, label, url)
        key = _dedupe_key(url)
        if key not in found or found[key]["kind"] == "link" and kind != "link":
            found[key] = item
    return sorted(found.values(), key=lambda item: item["kind"] == "link")[:LINK_MAX]


def email_actions(locator, accounts):
    """Provider actions derived only from an exact configured mailbox match.

    Graph webLink is the provider's original-message URL. Gmail's documented
    rfc822msgid operator supplies a search, honestly labeled as such:
    https://support.google.com/mail/answer/7190
    https://learn.microsoft.com/en-us/graph/api/resources/message
    """
    if not isinstance(locator, dict) or not isinstance(accounts, list):
        return []
    address = str(locator.get("account") or "").strip().lower()
    matched = [a for a in accounts if isinstance(a, dict)
               and str(a.get("email") or "").strip().lower() == address and address]
    if len(matched) != 1:
        return []
    account = matched[0]
    host = str(account.get("host") or "").strip().lower()
    provider = str(account.get("type") or "").lower()
    mailbox = safe_http_url(account.get("webmail_url"))
    label, actions = "mailbox", []
    if provider == "graph":
        label, mailbox = "Outlook", mailbox or "https://outlook.office.com/mail/"
        link = safe_http_url(locator.get("web_link") or locator.get("webLink"))
        if link:
            actions.append(_action("email", "Open email in Outlook", link))
    elif host in {"imap.gmail.com", "imap.googlemail.com"} or provider == "gmail":
        label = "Gmail"
        mailbox = mailbox or "https://mail.google.com/mail/?" + urlencode({"authuser": address})
        message_id = str(locator.get("message_id") or "").strip().strip("<>")
        if (message_id and len(message_id) <= 998 and "@" in message_id
                and not re.search(r"[\s<>\"\\\x00-\x1f]", message_id)):
            # It is a search, not a fabricated Gmail thread identifier.
            search = "https://mail.google.com/mail/?" + urlencode({"authuser": address})
            search += "#search/" + quote("in:anywhere rfc822msgid:" + message_id, safe="")
            actions.append(_action("email", "Find email in Gmail", search))
    elif host == "imap.mail.me.com":
        label, mailbox = "iCloud Mail", mailbox or "https://www.icloud.com/mail/"
    elif host == "outlook.office365.com":
        label, mailbox = "Outlook", mailbox or "https://outlook.office.com/mail/"
    if mailbox:
        actions.append(_action("mailbox", "Open " + label, mailbox))
    return actions

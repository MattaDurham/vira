"""Local navigation evidence for commitments; rendering never opens a source.

New evidence keeps its locator. Older evidence can recover it through an
exact local index match or a current feed item. Only an explicit owner click
uses the existing email reader; no website or mailbox is fetched here.
"""
from urllib.parse import urlsplit

from . import channels, commitmentresources, settings, textindex

LOCATOR_FIELDS = ("account", "message_id", "graph_id", "rowid", "web_link", "subject")
NAVIGATION_FIELDS = frozenset({"account", "message_id", "graph_id", "rowid", "web_link",
                               "links", "imap_uid", "mailbox"})


def metadata(item):
    """Retain source-owned navigation data, never a model's proposed URL."""
    out = {}
    for key in LOCATOR_FIELDS:
        value = item.get(key)
        if key == "rowid" and isinstance(value, int):
            value = str(value)
        if isinstance(value, str) and value.strip() and len(value) <= 8192:
            if not any(ord(c) < 32 for c in value):
                out[key] = value.strip()
    links = item.get("links")
    if isinstance(links, list):
        # Preserve source-extracted labels while validating stored URLs again;
        # malformed or legacy records cannot supply active markup.
        cleaned = []
        seen = set()
        for link in links[:40]:
            if not isinstance(link, dict) or not isinstance(link.get("url"), str):
                continue
            url = commitmentresources.safe_http_url(link["url"])
            if url and url not in seen:
                seen.add(url)
                # Keep a classified HTML button even if its signed URL is a
                # tracking redirect: reparsing that URL alone loses its label.
                label = link.get("label")
                kind = link.get("kind")
                cleaned.append({"url": url, "domain": urlsplit(url).hostname.encode("idna").decode("ascii"),
                                "label": label.strip()[:160] if isinstance(label, str) and label.strip() else "Open link",
                                "kind": kind if isinstance(kind, str) and kind in {
                                    "payment", "document", "scheduling", "account", "help", "link"} else "link"})
        if cleaned:
            out["links"] = cleaned[:24]
    return out


def model_view(value):
    """Keep routing metadata and hidden signed button URLs out of extraction prompts."""
    if isinstance(value, dict):
        return {key: model_view(child) for key, child in value.items()
                if key not in NAVIGATION_FIELDS}
    if isinstance(value, list):
        return [model_view(child) for child in value]
    return value


def _source_id(item):
    if item.get("id"):
        return str(item["id"])
    if item.get("channel") == "email" and item.get("message_id"):
        return "mail:" + str(item["message_id"]).strip()
    if item.get("rowid") is not None:
        prefix = "imsg" if item.get("channel") == "imessage" else item.get("channel", "")
        return str(prefix) + ":" + str(item["rowid"])
    return ""


def enrich(rows, source_items=()):
    """Add actionable resources in one bounded batch of local source lookups."""
    ids = list(dict.fromkeys(str(ref["id"]) for row in rows
                            for ref in row.get("evidence", [])
                            if isinstance(ref, dict) and ref.get("id")))
    isolated = bool(settings.fixture_mode() or settings.sandboxed())
    sources, feed_sources, accounts, unavailable = {}, {}, [], False
    if not isolated:
        try:
            accounts = channels.mail_accounts()
        except (OSError, ValueError, TypeError):
            unavailable = True
        try:
            # The index accessor caps each exact-match query at 100 IDs.
            for start in range(0, len(ids), 100):
                sources.update(textindex.lookup_sources(ids[start:start + 100]))
        except Exception:  # noqa: BLE001 - navigation must not suppress a commitment
            # Source navigation cannot suppress a reminder if the index is
            # unavailable, corrupt, or busy. Existing evidence still works.
            unavailable = True
        wanted = set(ids)
        for item in source_items:
            if not isinstance(item, dict):
                continue
            key = _source_id(item)
            if key in wanted:
                feed_sources.setdefault(key, []).append(item)
    connected = {str(a.get("email") or "").strip().lower() for a in accounts if isinstance(a, dict)}
    for row in rows:
        actions, seen, missing_email = [], set(), False
        row.pop("resources_note", None)
        retained_accounts = {}
        for ref in row.get("evidence", []):
            if isinstance(ref, dict) and ref.get("id"):
                retained = metadata(ref)
                account = retained.get("account", "").lower()
                if account:
                    copies = retained_accounts.setdefault(str(ref["id"]), {})
                    copies[account] = {**copies.get(account, {}), **retained}

        def add(action):
            key = ("url", action["url"]) if action.get("url") else (
                "email", action.get("account"), action.get("message_id"),
                action.get("graph_id"), action.get("rowid"))
            if key not in seen:
                seen.add(key)
                actions.append(action)

        for ref in row.get("evidence", []):
            if not isinstance(ref, dict):
                continue
            ident = str(ref.get("id") or "")
            found = sources.get(ident, {})
            retained = metadata(ref)
            copies = retained_accounts.get(ident, {})
            # Older deadlines saved a second, bare evidence reference. Use
            # its sibling's known mailbox when that choice is unambiguous.
            if not retained.get("account") and len(copies) == 1:
                retained = next(iter(copies.values()))
            wanted_account = str(retained.get("account") or found.get("account") or "").strip().lower()
            # A Message-ID can occur in multiple connected mailboxes. Keep
            # each account's body, signed links, and opaque provider IDs
            # together instead of relabeling another mailbox's copy.
            if wanted_account and str(found.get("account") or "").strip().lower() != wanted_account:
                found = {}
            for item in feed_sources.get(ident, []):
                if wanted_account and str(item.get("account") or "").strip().lower() != wanted_account:
                    continue
                # The matching index's full body outranks a feed preview;
                # a current locator fills gaps before indexing catches up.
                found = {**item, **found, **metadata(item)}
                break
            source = {**found, **retained}
            channel = ref.get("channel") or source.get("channel")
            if channel == "email":
                account = str(source.get("account") or "").strip()
                # RFC IDs can safely be recovered from legacy evidence. An
                # opaque Graph ID or All Mail UID must not become an INBOX UID.
                if not source.get("message_id") and ident.startswith("mail:<") and ident.endswith(">"):
                    source["message_id"] = ident[5:]
                known = account.lower() in connected or isolated and bool(account)
                locator = metadata(source)
                inbox_prefix = "mail-" + account + "-"
                rowid = locator.get("rowid", "")
                inbox_uid = rowid.startswith(inbox_prefix) and rowid[len(inbox_prefix):].isdigit()
                if not inbox_uid:
                    locator.pop("rowid", None)
                if known and (locator.get("message_id") or locator.get("graph_id") or inbox_uid):
                    add({"kind": "email", "label": "Open email", "source_id": ident,
                         **{k: v for k, v in locator.items() if k != "links"}})
                else:
                    missing_email = True
                for action in commitmentresources.email_actions(source, accounts):
                    add(action)
            links = metadata(source).get("links", [])
            body = str(found.get("text") or "")
            quote = str(ref.get("quote") or "")
            links += commitmentresources.extract_links(body + "\n" + quote)
            for link in links:
                add({**link, "source_id": ident})
        row["resources"] = actions
        if unavailable:
            row["resources_note"] = "Some source links are temporarily unavailable. Your commitment is still saved."
        elif missing_email:
            row["resources_note"] = "The exact email could not be linked from its saved source. Available account and website links are shown."
        elif not actions:
            row["resources_note"] = "No action links were included in the available source."
    return rows

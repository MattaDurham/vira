"""Vira's own copy of every email.

Everything here is rooted at ONE tmp archive, ONE fake IMAP mailbox and
ONE fake Graph mailbox. The module reads four things outside its own
store (the config, the archive root, the account list, the providers), so
`test_an_empty_fixture_archives_nothing` is the isolation guard: a source
added later that reaches the real machine instead of the fixture fails
it on sight.
"""
import imaplib
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import mailarchive, msgraph  # noqa: E402

IMAP_ACCOUNT = "owner@example.test"
GRAPH_ACCOUNT = "owner@work.example.test"
MID_A = "<a@example.test>"
MID_B = "<b@example.test>"


def raw_message(mid=MID_A, date="Thu, 10 Sep 2026 14:00:00 +0000", body="hello"):
    msg = EmailMessage()
    msg["From"] = "billing@example.test"
    msg["To"] = IMAP_ACCOUNT
    msg["Subject"] = "Invoice ready"
    msg["Date"] = date
    if mid:
        msg["Message-ID"] = mid
    msg.set_content(body)
    msg.add_attachment(b"%PDF-1.4 synthetic", maintype="application",
                       subtype="pdf", filename="invoice.pdf")
    return msg.as_bytes()


class FakeIMAP:
    """Just enough imaplib for _imap_sweep: a uid-keyed mailbox with a
    UIDVALIDITY, poison uids that fail a set number of times, and the real
    server's quirk that "N:*" past the last uid answers with the last uid."""

    def __init__(self, mailbox, validity="1", poison=None):
        self.mailbox = mailbox
        self.validity = validity
        self.poison = poison if poison is not None else {}   # shared: a poison
                                                              # message fails on any connection
        self.fetched = []
        self.commands = []     # uids asked for by each FETCH command

    def login(self, *a):
        return "OK", []

    def select(self, *a, **kw):
        return "OK", [b"1"]

    def response(self, code):
        return code, [str(self.validity).encode()]

    def status(self, mailbox, what):
        nxt = (max(self.mailbox) if self.mailbox else 0) + 1
        return "OK", [f'{mailbox} (UIDNEXT {nxt})'.encode()]

    def uid(self, cmd, *args):
        if cmd == "search":
            lo, _, hi = args[-1].partition(":")
            lo = int(lo)
            top = max(self.mailbox) if self.mailbox else 0
            hi = top if hi in ("*", "") else int(hi)
            if hi < lo:               # "N:*" past the last uid answers the last uid
                lo, hi = hi, lo
            hits = sorted(u for u in self.mailbox if lo <= u <= hi)
            return "OK", [" ".join(map(str, hits)).encode()]
        if "," in str(args[0]):               # one FETCH for a batch of uids
            uids = [int(x) for x in str(args[0]).split(",")]
            self.commands.append(len(uids))
            if any(self.poison.get(x) for x in uids):
                # The command fails whole; the walker then fetches the
                # chunk one message at a time, where the poison counts.
                raise imaplib.IMAP4.error("fetch failed")
            out = []
            for x in uids:
                self.fetched.append(x)
                if x in self.mailbox:
                    out.append((f"{x} (UID {x} BODY[] {{{len(self.mailbox[x])}}}".encode(),
                                self.mailbox[x]))
                    out.append(b")")
            return "OK", out or [None]
        u = int(args[0])
        self.commands.append(1)
        self.fetched.append(u)
        if self.poison.get(u):
            self.poison[u] -= 1
            raise imaplib.IMAP4.error("fetch failed")
        if u not in self.mailbox:
            return "OK", [None]
        return "OK", [(b"h", self.mailbox[u]), b")"]

    def logout(self):
        return "OK", []


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "archive"
        self.cap = 0
        self.accounts = []
        self.imap = FakeIMAP({})
        self.graph_pages = {}      # request path -> response dict
        self.graph_mime = {}       # graph id -> raw bytes
        self.graph_poison = {}     # graph id -> failures left
        self.graph_calls = []
        self.mime_calls = []
        patches = [
            mock.patch.object(mailarchive, "root", lambda: self.root),
            mock.patch.object(mailarchive, "enabled", lambda: True),
            mock.patch.object(mailarchive, "max_bytes", lambda: self.cap),
            mock.patch.object(mailarchive, "_default_workers", lambda: 1),
            mock.patch.object(mailarchive, "_accounts", lambda: self.accounts),
            mock.patch.object(mailarchive.imaplib, "IMAP4_SSL", lambda *a, **k: self.imap),
            mock.patch.object(mailarchive.channels, "imap_special_folder",
                              lambda *a: "[Example]/All Mail"),
            mock.patch.object(mailarchive.mailmod, "keychain_password",
                              lambda addr: "synthetic-password"),
            mock.patch.object(msgraph, "_graph_request", self._graph_request),
            mock.patch.object(msgraph, "get_bytes", self._get_bytes),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # fake Graph
    def _graph_request(self, account, path):
        self.graph_calls.append(path)
        return self.graph_pages[path]

    def _get_bytes(self, account, path):
        gid = urllib.parse.unquote(path[len("/me/messages/"):-len("/$value")])
        self.mime_calls.append(gid)
        if self.graph_poison.get(gid):
            self.graph_poison[gid] -= 1
            raise OSError("graph fetch failed")
        return self.graph_mime[gid]

    def use_imap(self, mailbox, **kw):
        self.accounts = [{"email": IMAP_ACCOUNT, "host": "imap.example.test"}]
        self.imap = FakeIMAP(mailbox, **kw)

    def use_graph(self, messages, page_size=50):
        """messages: list of (graph id, internetMessageId, receivedDateTime,
        raw). Paged into the fixture with real-looking nextLinks."""
        self.accounts = [{"email": GRAPH_ACCOUNT, "type": "graph"}]
        first = ("/me/messages?$top=50&$select=id,internetMessageId,receivedDateTime")
        self.graph_mime = {gid: raw for gid, _, _, raw in messages}
        rows = [{"id": gid, "internetMessageId": mid, "receivedDateTime": rd}
                for gid, mid, rd, _ in messages]
        pages = [rows[i:i + page_size] for i in range(0, len(rows), page_size)] or [[]]
        path = first
        for i, page in enumerate(pages):
            body = {"value": page}
            if i + 1 < len(pages):
                nxt = f"/me/messages?$skip={(i + 1) * page_size}"
                body["@odata.nextLink"] = msgraph.GRAPH + nxt
            self.graph_pages[path] = body
            path = body.get("@odata.nextLink", "")[len(msgraph.GRAPH):]
        self.first_path = first

    def state(self, key):
        con = sqlite3.connect(self.root / "index.sqlite")
        try:
            row = con.execute("SELECT val FROM state WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def blobs(self):
        return sorted((self.root / "blobs").rglob("*.eml")) if (self.root / "blobs").exists() else []


class IsolationTests(_Base):
    def test_an_empty_fixture_archives_nothing(self):
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 0)
        self.assertEqual(mailarchive.stats()["messages"], 0)
        self.assertEqual(self.blobs(), [])

    def test_a_disabled_archive_touches_nothing(self):
        self.use_imap({1: raw_message()})
        with mock.patch.object(mailarchive, "enabled", lambda: False):
            self.assertEqual(mailarchive.sweep(log=lambda *a: None), 0)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.imap.fetched, [])


class ImapTests(_Base):
    def test_messages_land_as_eml_blobs_with_a_watermark(self):
        a, b = raw_message(MID_A), raw_message(MID_B, date="Mon, 1 Oct 2012 09:00:00 +0000")
        self.use_imap({41: a, 42: b})
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 2)
        blobs = self.blobs()
        self.assertEqual(len(blobs), 2)
        self.assertEqual({p.read_bytes() for p in blobs}, {a, b})
        self.assertEqual(self.imap.fetched, [42, 41])     # newest first
        self.assertEqual(self.state(f"wm:{IMAP_ACCOUNT}"), "42")
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")
        self.assertEqual(self.state(f"uidvalidity:{IMAP_ACCOUNT}"), "1")
        acct = mailarchive.stats()["accounts"][IMAP_ACCOUNT]
        self.assertEqual(acct["messages"], 2)
        self.assertTrue(acct["history_complete"])
        self.assertEqual(acct["oldest"], "2012-10-01T09:00:00+00:00")
        self.assertEqual(acct["newest"], "2026-09-10T14:00:00+00:00")
        # The second pass asks for 43:* and gets 42 back; nothing is fetched.
        fetched = list(self.imap.fetched)
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 0)
        self.assertEqual(self.imap.fetched, fetched)
        # New mail lands above the high mark without touching the history.
        self.imap.mailbox[50] = raw_message("<c@example.test>")
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 1)
        self.assertEqual(self.imap.fetched, fetched + [50])
        self.assertEqual(self.state(f"wm:{IMAP_ACCOUNT}"), "50")

    def test_identical_bytes_share_one_blob_and_one_message_id_is_one_row(self):
        same, a = raw_message(mid=None), raw_message(MID_A)
        self.use_imap({1: same, 2: same, 3: a, 4: raw_message(MID_A, body="other")})
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 3)
        s = mailarchive.stats()
        self.assertEqual(s["messages"], 3)       # uid:1, uid:2, MID_A
        self.assertEqual(s["blobs"], 2)          # same bytes once, MID_A once
        self.assertEqual(len(self.blobs()), 2)   # the second MID_A never hit disk
        self.assertEqual(s["bytes"], len(same) + len(a))

    def test_a_lone_failure_is_retried_next_sweep(self):
        self.use_imap({u: raw_message(f"<{u}@example.test>") for u in range(1, 7)},
                      poison={2: 1})
        said = []
        self.assertEqual(mailarchive.sweep(log=said.append), 5)
        self.assertEqual(self.state(f"wm:{IMAP_ACCOUNT}"), "6")
        self.assertEqual(mailarchive.stats()["failed"], 1)
        self.assertTrue(any("uid 2" in s and "retry" in s for s in said))
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 1)
        self.assertEqual(mailarchive.stats()["failed"], 0)
        self.assertEqual(mailarchive.stats()["messages"], 6)

    def test_a_dying_connection_ends_the_pass_with_the_watermark_held(self):
        self.use_imap({u: raw_message(f"<{u}@example.test>") for u in range(1, 7)},
                      poison={3: 9, 4: 9, 5: 9, 6: 9})
        said = []
        self.assertEqual(mailarchive.sweep(log=said.append), 0)
        self.assertEqual(self.state(f"wm:{IMAP_ACCOUNT}"), "6")
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "4")   # held above uid 3
        self.assertEqual(mailarchive.stats()["failed"], 3)         # 6, 5, 4 recorded
        self.assertTrue(any("repeated failures" in s for s in said))

    def test_uidvalidity_change_rewalks_without_duplicating(self):
        self.use_imap({41: raw_message(MID_A), 42: raw_message(MID_B)})
        mailarchive.sweep(log=lambda *a: None)
        self.imap.validity = "2"
        self.imap.fetched.clear()
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 0)
        self.assertEqual(self.imap.fetched, [42, 41])
        self.assertEqual(mailarchive.stats()["messages"], 2)
        self.assertEqual(len(self.blobs()), 2)
        self.assertEqual(self.state(f"wm:{IMAP_ACCOUNT}"), "42")
        self.assertEqual(self.state(f"uidvalidity:{IMAP_ACCOUNT}"), "2")

    def test_limit_bounds_a_pass_and_the_next_pass_continues(self):
        self.use_imap({u: raw_message(f"<{u}@example.test>") for u in range(1, 6)})
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, limit=2), 2)
        self.assertEqual(self.imap.fetched, [5, 4])
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "4")
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 3)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")
        self.assertEqual(self.state(f"wm:{IMAP_ACCOUNT}"), "5")


class ParallelHistoryTests(_Base):
    """The CLI backfill walks the history on several connections; the
    background sweep (a limit) stays on one."""

    def connections(self, mailbox, **kw):
        """A fresh fake connection per IMAP4_SSL call, all over one mailbox."""
        made = []

        def factory(*a, **k):
            c = FakeIMAP(mailbox, **kw)
            made.append(c)
            return c
        p = mock.patch.object(mailarchive.imaplib, "IMAP4_SSL", factory)
        p.start()
        self.addCleanup(p.stop)
        self.accounts = [{"email": IMAP_ACCOUNT, "host": "imap.example.test"}]
        return made

    def test_the_history_is_walked_by_several_connections(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 1201)}
        made = self.connections(mailbox)
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, workers=3), 1200)
        self.assertEqual(len(made), 1 + 3)       # the sweep's own, plus the walkers
        fetched = sorted(u for c in made for u in c.fetched)
        self.assertEqual(fetched, list(range(1, 1201)))
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")
        self.assertEqual(mailarchive.stats()["accounts"][IMAP_ACCOUNT]["messages"], 1200)
        self.assertTrue(mailarchive.stats()["accounts"][IMAP_ACCOUNT]["history_complete"])

    def test_the_history_is_fetched_a_hundred_messages_a_command(self):
        # Gmail paces the account by the command (2026-10-16), so the walk
        # asks for FETCH_BATCH uids at once: 1200 messages in three spans
        # cost 5 + 5 + 2 FETCH commands, not 1200.
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 1201)}
        made = self.connections(mailbox)
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, workers=3), 1200)
        commands = [n for c in made[1:] for n in c.commands]
        self.assertEqual(sorted(commands), [100] * 12)
        self.assertEqual(sorted(u for c in made for u in c.fetched), list(range(1, 1201)))

    def test_a_batch_that_dies_with_its_connection_is_refetched_one_at_a_time(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 301)}
        stalls = [1]

        class Dying(FakeIMAP):
            def uid(self, cmd, *args):
                if cmd == "fetch" and "," in str(args[0]) and stalls[0]:
                    stalls[0] -= 1
                    raise OSError("read timed out")
                return super().uid(cmd, *args)
        made = []

        def factory(*a, **k):
            c = Dying(mailbox)
            made.append(c)
            return c
        p = mock.patch.object(mailarchive.imaplib, "IMAP4_SSL", factory)
        p.start()
        self.addCleanup(p.stop)
        self.accounts = [{"email": IMAP_ACCOUNT, "host": "imap.example.test"}]
        said = []
        self.assertEqual(mailarchive.sweep(log=said.append, workers=1 + 1), 300)
        self.assertTrue(any("reconnecting and fetching them one at a time" in m for m in said), said)
        self.assertEqual(len(made), 1 + 2 + 1)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")
        self.assertEqual(mailarchive.stats()["failed"], 0)

    def test_gmails_quota_ends_the_pass_and_the_backfill_rests(self):
        # [OVERQUOTA] is the account's budget, not a dead connection or a
        # poison message: no reconnect, no one-at-a-time retry, no failure
        # recorded. The pass ends on it and the backfill waits QUOTA_WAIT
        # before walking on from the marks.
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 301)}
        refusals = [2]
        alert = "[OVERQUOTA] Account exceeded command or bandwidth limits."

        class Quota(FakeIMAP):
            def uid(self, cmd, *args):
                if cmd == "fetch" and refusals[0]:
                    refusals[0] -= 1
                    raise imaplib.IMAP4.error(alert)
                return super().uid(cmd, *args)
        made = []

        def factory(*a, **k):
            c = Quota(mailbox)
            made.append(c)
            return c
        p = mock.patch.object(mailarchive.imaplib, "IMAP4_SSL", factory)
        p.start()
        self.addCleanup(p.stop)
        self.accounts = [{"email": IMAP_ACCOUNT, "host": "imap.example.test"}]
        said = []
        naps = []
        with mock.patch.object(mailarchive.time, "sleep", naps.append):
            self.assertEqual(mailarchive.backfill(log=said.append, workers=2), 300)
        self.assertIn(mailarchive.QUOTA_WAIT, naps)
        self.assertTrue(any("Gmail's quota; resting" in m for m in said), said)
        self.assertFalse(any("one at a time" in m or "reconnecting" in m for m in said), said)
        self.assertEqual(mailarchive.stats()["failed"], 0)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")

    def test_a_bounded_sweep_stays_on_one_connection(self):
        made = self.connections({u: raw_message(f"<{u}@example.test>") for u in range(1, 30)})
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, limit=10, workers=4), 10)
        self.assertEqual(len(made), 1)

    def test_seen_uids_make_a_walk_met_again_cost_no_fetch(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 701)}
        made = self.connections(mailbox)
        mailarchive.sweep(log=lambda *a: None, workers=2)
        # A crash before the mark was saved: put it back at the top.
        con = mailarchive._db()
        mailarchive.set_state(con, f"low:{IMAP_ACCOUNT}", 701)
        con.commit()
        con.close()
        before = sum(len(c.fetched) for c in made)
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, workers=2), 0)
        self.assertEqual(sum(len(c.fetched) for c in made), before)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")

    def test_a_stalled_connection_is_replaced_and_the_walk_completes(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 1001)}
        poison = {999: 9, 998: 9, 997: 9, 996: 9}
        made = self.connections(mailbox, poison=poison)
        said = []
        # Three failures are recorded as messages; the fourth in a row reads
        # as the connection, which is replaced, and uid 996 is tried once
        # more on the new one (still poison: recorded). The span completes.
        self.assertEqual(mailarchive.sweep(log=said.append, workers=2), 996)
        self.assertEqual(len(made), 1 + 2 + 1)            # one reconnect
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")
        self.assertEqual(mailarchive.stats()["failed"], 4)
        self.assertTrue(any("reconnecting (1 of" in m for m in said), said)
        # The chunk holding the poison failed as one command and was then
        # walked one message at a time.
        self.assertTrue(any("failed: fetch failed; fetching them one at a time" in m for m in said), said)
        poison.clear()
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, workers=2), 4)
        self.assertEqual(mailarchive.stats()["messages"], 1000)

    def test_past_the_reconnect_budget_a_walker_ends_and_holds_the_mark(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 601)}
        poison = {u: 9 for u in range(576, 601)}      # 25 uids: five reconnects, then one more
        self.connections(mailbox, poison=poison)
        said = []
        # The top span (101..600) burns its five reconnects; the bottom span
        # completes on the other connection but the mark cannot pass the
        # broken span above it.
        self.assertEqual(mailarchive.sweep(log=said.append, workers=2), 100)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "601")
        self.assertEqual(mailarchive.stats()["failed"], 23)
        self.assertTrue(any("and 5 reconnects" in m for m in said), said)

    def test_a_stalled_search_reconnects_and_the_span_is_still_walked(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 701)}
        stalls = {"201:700": 1}

        class StallingSearch(FakeIMAP):
            def uid(self, cmd, *args):
                if cmd == "search" and stalls.get(args[-1]):
                    stalls[args[-1]] -= 1
                    raise OSError("read timed out")
                return super().uid(cmd, *args)
        made = []

        def factory(*a, **k):
            c = StallingSearch(mailbox)
            made.append(c)
            return c
        p = mock.patch.object(mailarchive.imaplib, "IMAP4_SSL", factory)
        p.start()
        self.addCleanup(p.stop)
        self.accounts = [{"email": IMAP_ACCOUNT, "host": "imap.example.test"}]
        said = []
        self.assertEqual(mailarchive.sweep(log=said.append, workers=2), 700)
        self.assertTrue(any("stalled at uid 201:700; reconnecting" in m for m in said), said)
        self.assertEqual(len(made), 1 + 2 + 1)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")

    def limited_connections(self, mailbox, allow):
        """Gmail refuses connections past its limit: the factory raises its
        alert once `allow` connections are open."""
        made = []
        open_count = [0]
        outer = self

        class Limited(FakeIMAP):
            def logout(self):
                open_count[0] -= 1
                return super().logout()

        def factory(*a, **k):
            if open_count[0] >= allow:
                raise imaplib.IMAP4.error("[ALERT] Too many simultaneous connections. (Failure)")
            open_count[0] += 1
            c = Limited(mailbox)
            made.append(c)
            return c
        p = mock.patch.object(mailarchive.imaplib, "IMAP4_SSL", factory)
        p.start()
        self.addCleanup(p.stop)
        self.accounts = [{"email": IMAP_ACCOUNT, "host": "imap.example.test"}]
        return made

    def test_a_walker_refused_at_the_connection_limit_steps_aside(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 1201)}
        made = self.limited_connections(mailbox, allow=2)      # the sweep's own, plus one
        said = []
        self.assertEqual(mailarchive.sweep(log=said.append, workers=3), 1200)
        self.assertEqual(len(made), 2)
        self.assertEqual(sum("connection limit; walking with fewer" in m for m in said), 2, said)
        self.assertTrue(any("2 of 3 walkers refused" in m for m in said), said)
        self.assertFalse(any("walk ended early" in m for m in said), said)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")

    def test_no_walker_connecting_makes_the_backfill_retry_with_fewer(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 301)}
        self.limited_connections(mailbox, allow=1)              # only the sweep's own
        said = []
        with mock.patch.object(mailarchive.time, "sleep", lambda s: None):
            self.assertEqual(mailarchive.backfill(log=said.append, workers=4), 300)
        retries = [m for m in said if "retrying with" in m]
        self.assertEqual(retries, ["mail archive backlog: Gmail's connection limit; retrying with 2 connection(s)",
                                   "mail archive backlog: Gmail's connection limit; retrying with 1 connection(s)"])
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")

    def test_a_busy_index_in_a_walker_ends_the_pass_as_an_error(self):
        mailbox = {u: raw_message(f"<{u}@example.test>") for u in range(1, 11)}
        self.connections(mailbox)
        real = mailarchive._Store.put
        calls = []

        def put(store, **kw):
            calls.append(1)
            if len(calls) == 3:
                raise sqlite3.OperationalError("database is locked")
            return real(store, **kw)
        said = []
        with mock.patch.object(mailarchive._Store, "put", put):
            archived, errored, _ = mailarchive._sweep(said.append, None, None, 2)
        self.assertTrue(errored)
        self.assertTrue(any("walk ended early" in m for m in said), said)
        self.assertEqual(archived, 2)            # the walker stopped at the lock
        # Every walker says why it left: the one that hit the lock names
        # it, the other saw the pass ending.
        exits = [m for m in said if "walker done" in m]
        self.assertEqual(len(exits), 2, said)
        self.assertTrue(any("OperationalError" in m for m in exits), exits)
        self.assertTrue(any("pass is ending" in m or "no spans left" in m for m in exits), exits)
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, workers=2), 8)
        self.assertEqual(self.state(f"low:{IMAP_ACCOUNT}"), "1")


class GraphTests(_Base):
    def test_walk_fetches_raw_mime_and_stamps_the_watermark_when_complete(self):
        a, b = raw_message(MID_A), raw_message(MID_B)
        self.use_graph([("g1", MID_A, "2026-09-10T14:00:00Z", a),
                        ("g2", MID_B, "2026-09-09T08:00:00Z", b)], page_size=1)
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 2)
        self.assertEqual({p.read_bytes() for p in self.blobs()}, {a, b})
        self.assertEqual(self.mime_calls, ["g1", "g2"])
        self.assertEqual(self.state(f"wm:{GRAPH_ACCOUNT}"), "2026-09-10T14:00:00Z")
        # The next pass asks only for mail since the watermark, and a known
        # key costs a page row, not a fetch.
        since = "/me/messages?$filter=" + urllib.parse.quote(
            "receivedDateTime ge 2026-09-10T14:00:00Z") + \
            "&$top=50&$select=id,internetMessageId,receivedDateTime"
        self.graph_pages[since] = {"value": [
            {"id": "g1", "internetMessageId": MID_A, "receivedDateTime": "2026-09-10T14:00:00Z"}]}
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 0)
        self.assertEqual(self.graph_calls[-1], since)
        self.assertEqual(self.mime_calls, ["g1", "g2"])

    def test_a_limited_pass_leaves_the_watermark_unset(self):
        self.use_graph([("g1", MID_A, "2026-09-10T14:00:00Z", raw_message(MID_A)),
                        ("g2", MID_B, "2026-09-09T08:00:00Z", raw_message(MID_B))])
        self.assertEqual(mailarchive.sweep(log=lambda *a: None, limit=1), 1)
        self.assertIsNone(self.state(f"wm:{GRAPH_ACCOUNT}"))
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 1)
        self.assertEqual(self.state(f"wm:{GRAPH_ACCOUNT}"), "2026-09-10T14:00:00Z")

    def test_a_failed_fetch_is_recorded_and_retried(self):
        self.use_graph([("g1", MID_A, "2026-09-10T14:00:00Z", raw_message(MID_A)),
                        ("g2", MID_B, "2026-09-09T08:00:00Z", raw_message(MID_B))])
        self.graph_poison = {"g2": 1}
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 1)
        self.assertEqual(mailarchive.stats()["failed"], 1)
        self.assertEqual(self.state(f"wm:{GRAPH_ACCOUNT}"), "2026-09-10T14:00:00Z")
        since = "/me/messages?$filter=" + urllib.parse.quote(
            "receivedDateTime ge 2026-09-10T14:00:00Z") + \
            "&$top=50&$select=id,internetMessageId,receivedDateTime"
        self.graph_pages[since] = {"value": []}
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 1)
        self.assertEqual(mailarchive.stats()["failed"], 0)
        self.assertEqual(mailarchive.stats()["messages"], 2)

    def test_a_message_without_a_message_id_is_keyed_by_graph_id(self):
        self.use_graph([("g9", "", "2026-09-10T14:00:00Z", raw_message(mid=None))])
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 1)
        con = sqlite3.connect(self.root / "index.sqlite")
        try:
            key, mid, date = con.execute(
                "SELECT key, message_id, date FROM messages").fetchone()
        finally:
            con.close()
        self.assertEqual(key, f"{GRAPH_ACCOUNT}|graph:g9")
        self.assertIsNone(mid)
        self.assertEqual(date, "2026-09-10T14:00:00+00:00")


class StoreTests(_Base):
    def test_cap_is_reported_never_silent(self):
        a, b = raw_message(MID_A), raw_message(MID_B)
        self.use_imap({1: a, 2: b})
        self.cap = len(a)
        said = []
        self.assertEqual(mailarchive.sweep(log=said.append), 1)
        self.assertTrue(any("at cap" in s for s in said))
        self.assertEqual(mailarchive.stats()["messages"], 1)
        self.assertEqual(mailarchive.stats()["bytes"], len(b))   # newest first

    def test_lookup_and_have_many_serve_only_archived_mail(self):
        a = raw_message(MID_A)
        self.use_imap({1: a, 2: raw_message(MID_B)})
        mailarchive.sweep(log=lambda *a: None)
        path = mailarchive.lookup(IMAP_ACCOUNT, MID_A)
        self.assertEqual(path.suffix, ".eml")
        self.assertEqual(path.read_bytes(), a)
        self.assertIsNone(mailarchive.lookup(IMAP_ACCOUNT, "<nope@example.test>"))
        self.assertIsNone(mailarchive.lookup("other@example.test", MID_A))
        self.assertEqual(mailarchive.have_many(IMAP_ACCOUNT, [MID_A, MID_B, "<x>", ""]),
                         {MID_A, MID_B})
        self.assertEqual(mailarchive.have_many(IMAP_ACCOUNT, []), set())

    def test_blob_path_rejects_anything_but_a_hash(self):
        for bad in ("", "../x", "a" * 63, "A" * 64, "zz" * 32, None, 7):
            self.assertIsNone(mailarchive.blob_path(bad))
        sha = "ab" * 32
        p = mailarchive.blob_path(sha)
        self.assertEqual(p, self.root / "blobs" / "ab" / (sha + ".eml"))

    def test_unreachable_root_degrades_to_no_copy(self):
        self.use_imap({1: raw_message(MID_A)})
        # A root under a regular file cannot be created on any platform;
        # a Unix-only path such as /dev/null/nowhere is creatable on Windows.
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with mock.patch.object(mailarchive, "root", lambda: blocker / "nowhere"):
            self.assertIsNone(mailarchive.lookup(IMAP_ACCOUNT, MID_A))
            self.assertFalse(mailarchive.stats()["available"])
            said = []
            self.assertEqual(mailarchive.sweep(log=said.append), 0)
        self.assertTrue(any("unreachable" in s for s in said))

    def test_the_index_runs_in_write_ahead_mode(self):
        con = mailarchive._db()
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            con.close()

    def test_each_message_is_committed_on_its_own(self):
        # A sweep spends its time on network fetches; a transaction held
        # across them locked the index against the other writer. A second
        # connection reading mid-walk sees every earlier message.
        seen = {}
        db = self.root / "index.sqlite"

        class PeekingIMAP(FakeIMAP):
            def uid(self, cmd, *args):
                if cmd == "fetch" and int(args[0]) == 2:      # third fetched
                    other = sqlite3.connect(db)
                    try:
                        seen["rows"] = other.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                    finally:
                        other.close()
                return super().uid(cmd, *args)
        self.accounts = [{"email": IMAP_ACCOUNT, "host": "imap.example.test"}]
        self.imap = PeekingIMAP({u: raw_message(f"<{u}@example.test>") for u in range(1, 5)})
        self.assertEqual(mailarchive.sweep(log=lambda *a: None), 4)
        self.assertEqual(seen, {"rows": 2})

    def test_a_busy_index_ends_the_pass_and_the_backfill_retries(self):
        # The background thread and the CLI write the same index. A lock
        # that outlasts the wait ends the walk, keeps what landed, and the
        # backfill tries again after a pause instead of reading an empty
        # pass as "done" (the CLI's first real pass, 2026-10-13).
        self.use_imap({u: raw_message(f"<{u}@example.test>") for u in range(1, 4)})
        real = mailarchive._Store.put
        calls = []

        def put(store, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return real(store, **kw)
        said = []
        with mock.patch.object(mailarchive._Store, "put", put), \
             mock.patch.object(mailarchive.time, "sleep", lambda s: None):
            self.assertEqual(mailarchive.backfill(log=said.append), 3)
        self.assertEqual(mailarchive.stats()["messages"], 3)
        self.assertEqual(mailarchive.stats()["failed"], 0)
        self.assertTrue(any("walk ended early" in m for m in said), said)
        self.assertTrue(any("retrying (1 of" in m for m in said), said)
        self.assertEqual([m for m in said if m.startswith("mail archive backlog: +")],
                         ["mail archive backlog: +1 (total 1)",
                          "mail archive backlog: +2 (total 3)",
                          "mail archive backlog: +0 (total 3)"])

    def test_the_backfill_gives_up_after_repeated_error_passes(self):
        self.use_imap({1: raw_message(MID_A)})
        with mock.patch.object(mailarchive._Store, "put",
                               side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(mailarchive.time, "sleep", lambda s: None):
            said = []
            self.assertEqual(mailarchive.backfill(log=said.append), 0)
        self.assertTrue(any("giving up after 3" in m for m in said), said)

    def test_backfill_loops_until_a_pass_is_quiet(self):
        self.use_imap({u: raw_message(f"<{u}@example.test>") for u in range(1, 4)})
        said = []
        self.assertEqual(mailarchive.backfill(log=said.append), 3)
        self.assertEqual([s for s in said if s.startswith("mail archive backlog")],
                         ["mail archive backlog: +3 (total 3)",
                          "mail archive backlog: +0 (total 3)"])


class Contracts(unittest.TestCase):
    def test_every_config_key_it_reads_has_a_default(self):
        """settings.get raises KeyError on a key with no DEFAULTS entry (the
        mail_body_index incident)."""
        from server import settings
        for key in ("mail_archive_enabled", "mail_archive_root",
                    "mail_archive_max_gb", "mail_archive_interval_min"):
            self.assertIn(key, settings.DEFAULTS)

    def test_the_archiver_is_built_and_started(self):
        """main.py starts the thread, and the startup test patches it by
        name so a suite run never sweeps a checkout: the first full run
        of this branch did exactly that after the thread's 90-second
        settle, and tests/test_zz_data_untouched.py caught it."""
        root = Path(__file__).resolve().parent.parent
        src = (root / "server" / "main.py").read_text(encoding="utf-8")
        self.assertIn("mailarchive.Archiver(", src)
        self.assertIn("mail_archiver.start()", src)
        startup_test = (root / "tests" / "test_instance_runtime.py").read_text(encoding="utf-8")
        self.assertIn('"mail_archiver"', startup_test)


if __name__ == "__main__":
    unittest.main()

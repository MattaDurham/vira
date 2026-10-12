"""The brief's waiting-on-reply list: a thread the other person closed (a
tapback, "thanks, big relief") leaves the list, one that asks nothing is kept
but muted as optional, and only real asks read as owed.

Runs the real _unreplied_imessages SQL against a synthetic chat.db built in a
temp directory, so the join from chat rows to reply_state is what is tested,
not a hand-built row. No real chat.db, CRM or store is read."""
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from server import brief, threadread

APPLE_EPOCH = 978307200

PEOPLE = {
    "p_closed": {"id": "p_closed", "name": "Cleo Shut", "handle": "+15555550101",
                 "last": "Thank you, big relief"},
    "p_ask": {"id": "p_ask", "name": "Asa Query", "handle": "+15555550102",
              "last": "Are you free Thursday?"},
    "p_link": {"id": "p_link", "name": "Lin Share", "handle": "+15555550103",
               "last": "https://example.com/story"},
}


def _ns(minutes_ago):
    return int((time.time() - minutes_ago * 60 - APPLE_EPOCH) * 1e9)


def _chat_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, style INTEGER);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER,
            is_from_me INTEGER, text TEXT, attributedBody BLOB,
            item_type INTEGER, handle_id INTEGER);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    """)
    for i, p in enumerate(PEOPLE.values(), start=1):
        con.execute("INSERT INTO chat VALUES (?, 45)", (i,))
        con.execute("INSERT INTO handle VALUES (?, ?)", (i, p["handle"]))
        for j, (mins, me, text) in enumerate(
                [(90, 1, "Sent the plan over"), (30, 0, p["last"])]):
            rid = i * 10 + j
            con.execute("INSERT INTO message VALUES (?,?,?,?,NULL,0,?)",
                        (rid, _ns(mins), me, text, i))
            con.execute("INSERT INTO chat_message_join VALUES (?, ?)", (i, rid))
    con.commit()
    con.close()


def _hist(pid):
    base = datetime.now().astimezone() - timedelta(minutes=90)
    row = lambda k, me, text: {"rowid": k, "when": base + timedelta(minutes=k),
                               "from_me": me, "text": text, "reaction": False,
                               "attachments": 0}
    return [row(0, True, "Sent the plan over"),
            row(60, False, PEOPLE[pid]["last"])]


class WaitingListTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "chat.db"
        _chat_db(db)
        by_handle = {p["handle"]: pid for pid, p in PEOPLE.items()}
        crm = {"by_id": PEOPLE, "profiles": {}, "master": {}}
        patches = [
            mock.patch.object(brief.imessage, "_connect",
                              side_effect=lambda: sqlite3.connect(db)),
            mock.patch.object(brief.crm, "resolve_handle",
                              side_effect=by_handle.get),
            mock.patch.object(brief.crm, "_load", return_value=crm),
            mock.patch.object(threadread.crm, "_load", return_value=crm),
            mock.patch.object(threadread, "messages",
                              side_effect=lambda pid, days=None: _hist(pid)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_rows_carry_the_reply_state(self):
        rows = {r["person_id"]: r for r in brief._unreplied_imessages()}
        self.assertEqual(rows["p_closed"]["reply"], "ended")
        self.assertEqual(rows["p_ask"]["reply"], "owed")
        self.assertEqual(rows["p_link"]["reply"], "optional")
        self.assertEqual(rows["p_link"]["reply_why"], "shared a link")

    def test_split_drops_ended_threads_but_keeps_them_countable(self):
        keep, ended = brief._split_waiting(brief._unreplied_imessages())
        self.assertEqual([r["person_id"] for r in keep], ["p_ask", "p_link"])
        self.assertEqual([r["person_id"] for r in ended], ["p_closed"])
        self.assertEqual(ended[0]["reply_why"], "signed off")

    def test_an_unreadable_thread_stays_owed(self):
        with mock.patch.object(threadread, "messages",
                               side_effect=OSError("chat.db locked")):
            rows = brief._unreplied_imessages()
        self.assertEqual({r["reply"] for r in rows}, {"owed"})
        keep, ended = brief._split_waiting(rows)
        self.assertEqual(len(keep), 3)
        self.assertEqual(ended, [])


if __name__ == "__main__":
    unittest.main()

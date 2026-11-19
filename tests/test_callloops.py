"""After-call cards, end to end on synthetic data.

The call history is a temp SQLite file with the macOS ZCALLRECORD shape,
the CRM is an in-memory registry whose loop writes mutate it the way the
real profile store does, the clock is pinned and the iMessage ping is
replaced. Nothing reads the machine's call log, CRM or config. Handles are
synthetic (555-01xx block).
"""
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import callhistory, callloops, executive, notify, reviewqueue
from server import data as crm

NOW = dt.datetime(2030, 9, 10, 16, 0, tzinfo=dt.timezone.utc)
DAVID = "+15555550101"
OFFICE = "+15555550150"


def _apple(instant):
    return (instant - callhistory.APPLE_EPOCH).total_seconds()


class CallLoopsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.db = root / "CallHistory.storedata"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, ZUNIQUE_ID TEXT, "
                    "ZADDRESS TEXT, ZDATE REAL, ZDURATION REAL, ZANSWERED INTEGER, "
                    "ZORIGINATED INTEGER, ZCALLTYPE INTEGER, ZSERVICE_PROVIDER TEXT)")
        con.commit()
        con.close()
        self.cfg = {"call_loops_enabled": True, "call_loops_min_minutes": 3}
        self.profiles = {"p-david": {"open_loops": [
            {"what": "Hotel fund question", "owed_by": "them", "since": "2030-08-01"},
            {"what": "Pick a time for the wine-cellar chat", "owed_by": "me", "since": "2030-09-01"},
            {"what": "Old thing", "owed_by": "me", "status": "closed"},
        ]}, "p-quiet": {"open_loops": []}}
        self.registry = {"profiles": self.profiles,
                         "by_id": {"p-david": {"name": "David Example"},
                                   "p-quiet": {"name": "Quinn Example"}}}
        handles = {"5555550101": "p-david", "5555550102": "p-quiet", "5555550150": "p-david"}
        self.pings = []
        self.quiet = False
        for patcher in (
                mock.patch.object(callhistory, "DB", self.db),
                mock.patch.object(callloops, "STORE", root / "call-loops.json"),
                mock.patch.object(callloops, "_now", return_value=NOW),
                mock.patch.object(callloops.settings, "get", side_effect=lambda k: self.cfg[k]),
                mock.patch.object(callloops.settings, "fixture_mode", return_value=False),
                mock.patch.dict(callloops.os.environ, {"VIRA_SANDBOX": ""}),
                mock.patch.object(crm, "_load", return_value=self.registry),
                mock.patch.object(crm, "lines", return_value={"5555550150"}),
                mock.patch.object(crm, "resolve_handle",
                                  side_effect=lambda h: handles.get(crm.norm_digits(h))),
                mock.patch.object(crm, "update_loop", side_effect=self._close),
                mock.patch.object(crm, "add_loop", side_effect=self._add),
                mock.patch.object(executive, "quiet", side_effect=lambda now=None, cfg=None: self.quiet),
                mock.patch.object(notify, "agent_ping",
                                  side_effect=lambda text, key=None: self.pings.append((key, text)) or True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    # The CRM writes, done to the in-memory profiles the way data.py does.
    def _close(self, pid, what, action):
        for lp in self.profiles[pid]["open_loops"]:
            if lp["what"] == what and lp.get("status") != "closed":
                lp["status"] = "closed"
                return lp
        raise LookupError("loop not found")

    def _add(self, pid, what, owed_by="me"):
        entry = {"what": what, "owed_by": owed_by, "status": "open", "since": "2030-09-10"}
        self.profiles[pid]["open_loops"].append(entry)
        return entry

    def call(self, uid, handle, minutes, ended_ago_min=1, answered=1, call_type=1):
        end = NOW - dt.timedelta(minutes=ended_ago_min)
        start = end - dt.timedelta(minutes=minutes)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO ZCALLRECORD (ZUNIQUE_ID, ZADDRESS, ZDATE, ZDURATION, ZANSWERED, "
                    "ZORIGINATED, ZCALLTYPE, ZSERVICE_PROVIDER) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, handle, _apple(start), minutes * 60, answered, 0, call_type,
                     "com.apple.FaceTime" if call_type != 1 else "com.apple.Telephony"))
        con.commit()
        con.close()

    def test_a_long_call_raises_one_card_and_one_text(self):
        self.call("c1", DAVID, 14)
        result = callloops.tick()
        self.assertEqual(len(result["raised"]), 1)
        [ask] = callloops.pending()
        self.assertEqual(ask["question"], "You just talked to David for 14 min. Did you cover any of these?")
        # Owed-by-me first; the closed loop is not offered.
        self.assertEqual([lp["what"] for lp in ask["loops"]],
                         ["Pick a time for the wine-cellar chat", "Hotel fund question"])
        self.assertEqual(len(self.pings), 1)
        self.assertIn("1) Pick a time for the wine-cellar chat", self.pings[0][1])
        self.assertLessEqual(len(self.pings[0][1]), 300)
        # The next pass sees the same call and asks nothing again.
        self.assertEqual(callloops.tick()["raised"], [])
        self.assertEqual(len(self.pings), 1)
        self.assertEqual(len(callloops.pending()), 1)

    def test_calls_that_do_not_qualify_raise_nothing(self):
        self.call("short", DAVID, 2)
        self.call("missed", DAVID, 10, answered=0)
        self.call("noloops", "+15555550102", 20)
        self.call("office", OFFICE, 20)
        self.call("unknown", "+15555550199", 20)
        self.call("old", DAVID, 20, ended_ago_min=180)
        self.assertEqual(callloops.tick()["raised"], [])
        self.assertEqual(self.pings, [])

    def test_still_on_the_call_waits_until_it_ends(self):
        self.call("live", DAVID, 10, ended_ago_min=-5)
        self.assertEqual(callloops.tick()["raised"], [])
        with mock.patch.object(callloops, "_now", return_value=NOW + dt.timedelta(minutes=6)):
            self.assertEqual(len(callloops.tick(NOW + dt.timedelta(minutes=6))["raised"]), 1)

    def test_a_call_back_joins_the_open_card(self):
        self.call("c1", DAVID, 5, ended_ago_min=20)
        self.call("c2", DAVID, 9, ended_ago_min=2, call_type=16)
        callloops.tick()
        [ask] = callloops.pending()
        self.assertEqual(ask["minutes"], 14)
        self.assertEqual(ask["call_ids"], ["c1", "c2"])
        self.assertEqual(len(self.pings), 1)

    def test_tapping_closes_the_loop_on_the_person(self):
        self.call("c1", DAVID, 14)
        callloops.tick()
        [ask] = callloops.pending()
        after = callloops.close_loop(ask["id"], 0)
        self.assertEqual(after["loops"][0]["status"], "closed")
        self.assertEqual(after["status"], "open")
        # The CRM no longer offers it: a later call asks only about the rest.
        self.assertEqual([lp["what"] for lp in callloops.open_loops("p-david")[0]],
                         ["Hotel fund question"])
        done = callloops.close_loop(ask["id"], 1)
        self.assertEqual(done["status"], "answered")
        self.assertEqual(callloops.pending(), [])

    def test_a_loop_closed_elsewhere_is_marked_gone(self):
        self.call("c1", DAVID, 14)
        callloops.tick()
        [ask] = callloops.pending()
        self._close("p-david", ask["loops"][0]["what"], "close")
        self.assertEqual(callloops.close_loop(ask["id"], 0)["loops"][0]["status"], "gone")

    def test_something_new_becomes_an_open_loop(self):
        self.call("c1", DAVID, 14)
        callloops.tick()
        [ask] = callloops.pending()
        after = callloops.add_loop(ask["id"], "Send the term sheet by Friday", "me")
        self.assertEqual(after["added"], [{"what": "Send the term sheet by Friday", "owed_by": "me"}])
        self.assertIn("Send the term sheet by Friday",
                      [lp["what"] for lp in self.profiles["p-david"]["open_loops"]])
        with self.assertRaises(ValueError):
            callloops.add_loop(ask["id"], "   ")

    def test_the_card_is_in_attention_and_finishes_there(self):
        self.call("c1", DAVID, 14)
        callloops.tick()
        rows = reviewqueue.SOURCES["calls"].read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["call"]["person_name"], "David Example")
        self.assertEqual(rows[0]["actions"], ["done", "none"])
        reviewqueue.act(rows[0]["id"], "none")
        self.assertEqual(callloops.pending(), [])
        # The loops not tapped stay open on the person.
        self.assertEqual(len(callloops.open_loops("p-david")[0]), 2)
        with self.assertRaises(ValueError):
            callloops.finish(rows[0]["raw_id"], "done")

    def test_quiet_hours_hold_the_text_not_the_card(self):
        self.quiet = True
        self.call("c1", DAVID, 14)
        result = callloops.tick()
        self.assertEqual(result["texted"], [])
        self.assertEqual(self.pings, [])
        self.assertEqual(len(callloops.pending()), 1)

    def test_an_unanswered_card_expires(self):
        self.call("c1", DAVID, 14)
        callloops.tick()
        later = NOW + callloops.ASK_KEEP + dt.timedelta(minutes=1)
        callloops.tick(later)
        self.assertEqual(callloops.pending(), [])

    def test_a_missing_call_log_is_a_named_status(self):
        self.db.unlink()
        result = callloops.tick()
        self.assertEqual(result["source"], "missing")
        self.assertEqual(callloops.status()["source"]["status"], "missing")

    def test_off_reads_nothing(self):
        self.cfg["call_loops_enabled"] = False
        self.call("c1", DAVID, 14)
        self.assertEqual(callloops.tick(), {"enabled": False})
        self.assertEqual(self.pings, [])


if __name__ == "__main__":
    unittest.main()

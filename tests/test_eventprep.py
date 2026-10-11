"""Event prep: anchors from calendar and messages, loop clustering, the
countdown and its clearing, and the owner's checklist state.

Every source is patched: the calendar occurrences, the CRM corpus, the
assistant commitment store and the checklist store. `today` is passed in, so
no case depends on the run date.

Run: .venv/bin/python -m unittest discover tests
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import brief, commitments, data as crm, eventprep

TODAY = dt.date(2030, 3, 4)   # a Monday


def _d(days):
    return (TODAY + dt.timedelta(days=days)).isoformat()


def _cal(title, start_days, end_days=None, *, all_day=False, birthday=False,
         hm="7:00 PM"):
    start = dt.datetime.combine(TODAY + dt.timedelta(days=start_days),
                                dt.time(0 if all_day else 19))
    end = (dt.datetime.combine(TODAY + dt.timedelta(days=end_days), dt.time(0))
           if end_days is not None else start + dt.timedelta(hours=2))
    return {"title": title, "calendar": "Birthdays" if birthday else "Home",
            "family": False, "birthday": birthday, "all_day": all_day,
            "start": start.isoformat(), "end": end.isoformat(),
            "start_hm": "" if all_day else hm, "end_hm": "",
            "conflict": False, "remote": False}


def _loop(what, due=None, owed_by="me", status="open"):
    lp = {"what": what, "owed_by": owed_by, "since": _d(-5),
          "channel": "imessage", "status": status}
    if due is not None:
        lp["due"] = due
    return lp


class EventPrepBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "event-prep.json"
        self.events = []
        self.profiles = {}
        self.people = {}
        self.subjects = {}
        for p in (
            mock.patch.object(eventprep, "STORE", self.store),
            mock.patch.object(eventprep, "_calendar_events",
                              lambda lo, hi: list(self.events)),
            mock.patch.object(crm, "_load", lambda: {
                "profiles": self.profiles, "by_id": self.people}),
            mock.patch.object(commitments, "all_subjects",
                              lambda: self.subjects),
        ):
            p.start()
            self.addCleanup(p.stop)

    def person(self, pid, name, *loops):
        self.people[pid] = {"id": pid, "name": name}
        self.profiles[pid] = {"name": name, "open_loops": list(loops)}

    def build(self, today=TODAY, **kw):
        return eventprep.build(today=today, **kw)


class AnchorsAndClustering(EventPrepBase):
    def test_trip_on_calendar_gathers_its_loops_across_people_and_inbox(self):
        self.events = [_cal("Lake Placid trip", 5, 8, all_day=True),
                       _cal("Weekly sync", 1)]
        self.person("p_test00000001", "Casey Example",
                    _loop("Book the Lake Placid cabin", _d(2)),
                    _loop("Return Casey's drill"))
        self.subjects = {"sender:rentals": {"person_name": "Inbox", "open_loops": [
            {**_loop("Pick up the rental car for the trip", _d(5)),
             "assistant_key": "k1"}]}}
        events = self.build()["events"]
        self.assertEqual([e["title"] for e in events], ["Lake Placid trip"])
        ev = events[0]
        self.assertEqual(ev["days"], 5)
        self.assertEqual(ev["end"], _d(7))   # all-day end is exclusive
        self.assertEqual(ev["urgency"], "soon")
        texts = {i["text"]: i["kind"] for i in ev["items"]}
        self.assertEqual(texts, {"Book the Lake Placid cabin": "logistics",
                                 "Pick up the rental car for the trip": "logistics"})
        self.assertEqual(ev["open"], 2)

    def test_routine_meeting_never_anchors_and_shared_name_alone_does_not_join(self):
        self.events = [_cal("Casey 1:1", 1), _cal("Dinner with Casey", 3)]
        self.person("p_test00000001", "Casey Example",
                    _loop("Reply to Casey about the drill"),
                    _loop("Order flowers for Casey"))
        events = self.build()["events"]
        self.assertEqual([e["title"] for e in events], ["Dinner with Casey"])
        self.assertEqual([i["text"] for i in events[0]["items"]],
                         ["Order flowers for Casey"])
        self.assertEqual(events[0]["items"][0]["kind"], "gift")

    def test_birthday_calendar_entry_joins_gift_loop_by_person_name(self):
        self.events = [_cal("Drew Sample's Birthday", 2, 3, all_day=True,
                            birthday=True)]
        self.person("p_test00000002", "Drew Sample", _loop("Get a present"))
        ev = self.build()["events"][0]
        self.assertEqual(ev["urgency"], "now")
        self.assertEqual([i["text"] for i in ev["items"]], ["Get a present"])

    def test_dated_loops_naming_an_occasion_become_one_message_event(self):
        self.person("p_test00000001", "Casey Example",
                    _loop("Book hotel for Sam's birthday weekend", _d(10)),
                    _loop("Wrap the gift for the birthday weekend", _d(10)))
        events = self.build(include_calendar=False)["events"]
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["source"], "messages")
        self.assertEqual(ev["title"], "Sam's birthday weekend")
        self.assertEqual(ev["urgency"], "ahead")
        self.assertEqual(sorted(i["kind"] for i in ev["items"]),
                         ["gift", "logistics"])

    def test_message_loop_matching_calendar_joins_it_instead_of_duplicating(self):
        self.events = [_cal("Jazz festival", 6, 7, all_day=True)]
        self.person("p_test00000001", "Casey Example",
                    _loop("Buy tickets for the jazz festival", _d(3)))
        events = self.build()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "calendar")
        self.assertEqual(len(events[0]["items"]), 1)

    def test_closed_and_owed_by_them_loops_are_ignored(self):
        self.events = [_cal("Beach weekend", 4, 6, all_day=True)]
        self.person("p_test00000001", "Casey Example",
                    _loop("Pack beach towels", status="closed"),
                    _loop("Casey sends the beach address", owed_by="them"))
        self.assertEqual(self.build()["events"][0]["items"], [])


class CountdownAndClearing(EventPrepBase):
    def test_moves_up_as_date_nears_and_clears_after_it_ends(self):
        self.events = [_cal("Ski trip", 9, 12, all_day=True)]
        self.assertEqual(self.build()["events"][0]["urgency"], "ahead")
        self.assertEqual(self.build(TODAY + dt.timedelta(days=4))["events"][0]["urgency"], "soon")
        near = self.build(TODAY + dt.timedelta(days=8))["events"][0]
        self.assertEqual((near["urgency"], near["days"]), ("now", 1))
        during = self.build(TODAY + dt.timedelta(days=10))["events"][0]
        self.assertTrue(during["started"])
        # the last day is day 11 (all-day end is exclusive); day 12 is clear
        self.assertEqual(self.build(TODAY + dt.timedelta(days=11))["events"][0]["title"], "Ski trip")
        self.assertEqual(self.build(TODAY + dt.timedelta(days=12))["events"], [])

    def test_beyond_horizon_is_not_shown_yet(self):
        self.events = []
        self.person("p_test00000001", "Casey Example",
                    _loop("Book the wedding hotel", _d(eventprep.HORIZON_DAYS + 5)))
        self.assertEqual(self.build()["events"], [])

    def test_recurring_occurrences_collapse_to_the_next_instance(self):
        self.events = [_cal("Family dinner", 2), _cal("Family dinner", 9)]
        events = self.build()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["start"], _d(2))


class OwnerChecklist(EventPrepBase):
    def test_tick_add_remove_and_dismiss_persist_without_touching_loops(self):
        self.events = [_cal("Lake trip", 3, 5, all_day=True)]
        self.person("p_test00000001", "Casey Example",
                    _loop("Book the lake cabin", _d(1)))
        ev = self.build()["events"][0]
        item = ev["items"][0]
        eventprep.set_checked(ev["key"], ev["end"], item["id"], True)
        added = eventprep.add_item(ev["key"], ev["end"], "  Pack   sunscreen ")
        self.assertEqual((added["text"], added["kind"]), ("Pack sunscreen", "packing"))

        ev = self.build()["events"][0]
        self.assertEqual((ev["total"], ev["open"]), (2, 1))
        self.assertEqual(ev["items"][-1]["id"], item["id"])   # done sinks
        self.assertTrue(ev["items"][-1]["done"])
        # the CRM loop itself is untouched
        self.assertEqual(self.profiles["p_test00000001"]["open_loops"][0]["status"], "open")

        eventprep.remove_item(ev["key"], ev["end"], added["id"])
        with self.assertRaises(KeyError):
            eventprep.remove_item(ev["key"], ev["end"], added["id"])
        eventprep.dismiss(ev["key"], ev["end"])
        self.assertEqual(self.build()["events"], [])
        eventprep.dismiss(ev["key"], ev["end"], restore=True)
        self.assertEqual(len(self.build()["events"]), 1)

    def test_bad_keys_are_refused(self):
        with self.assertRaises(ValueError):
            eventprep.set_checked("../../etc", _d(3), "x")
        with self.assertRaises(ValueError):
            eventprep.add_item("0" * 20, "not-a-date", "Pack")
        with self.assertRaises(ValueError):
            eventprep.add_item("0" * 20, _d(3), "   ")

    def test_state_for_long_finished_events_is_pruned(self):
        eventprep._mutate("a" * 20, (TODAY - dt.timedelta(days=60)).isoformat(),
                          lambda ev: None, today=TODAY - dt.timedelta(days=60))
        eventprep._mutate("b" * 20, _d(3), lambda ev: None, today=TODAY)
        import json
        stored = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(sorted(stored["events"]), ["b" * 20])


class BriefJoin(EventPrepBase):
    def test_fixture_brief_reads_crm_only(self):
        called = []
        self.events = [_cal("Lake trip", 3, 5, all_day=True)]
        with mock.patch.object(eventprep, "_calendar_events",
                               lambda lo, hi: called.append(1) or []), \
             mock.patch.object(commitments, "all_subjects",
                               lambda: called.append(2) or {}):
            out = brief._event_prep(fixture=True)
        self.assertEqual(called, [])
        self.assertEqual(out["events"], [])

    def test_section_failure_degrades_with_a_named_error(self):
        with mock.patch.object(eventprep, "build", side_effect=RuntimeError("boom")):
            out = brief._event_prep()
        self.assertEqual((out["events"], out["error"]), ([], "boom"))


if __name__ == "__main__":
    unittest.main()

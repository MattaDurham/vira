"""Event radar: the deterministic layer. No chat.db, no model, no
osascript, no sends — every gated side effect is patched or asserted
against the gate itself."""
import unittest
from unittest import mock

from server import events, reviewqueue


class GateTests(unittest.TestCase):
    def test_eventish_matches_real_invites(self):
        for t in ("BBQ at ours Saturday, you guys free?",
                  "save the date for the twins' birthday",
                  "we're hosting a game night friday",
                  "Can you come to the recital?"):
            self.assertTrue(events.EVENTISH.search(t), t)

    def test_eventish_ignores_plain_chat(self):
        for t in ("running late, be there in 10",
                  "loved that photo!!",
                  "can you get more gatorade"):
            self.assertFalse(events.EVENTISH.search(t), t)


class CleanTests(unittest.TestCase):
    def test_bad_dates_are_dropped_good_fields_clamped(self):
        raw = {"events": [
            {"title": "BBQ", "date": "2026-09-05", "confidence": 3,
             "status": "confirmed", "needs_reply": True},
            {"title": "no date", "date": "sometime"},
            {"title": "", "date": "2026-09-06"}]}
        out = events._clean_events(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["confidence"], 1.0)
        self.assertEqual(out[0]["status"], "confirmed")

    def test_unknown_status_becomes_proposed(self):
        out = events._clean_events({"events": [
            {"title": "x", "date": "2026-09-05", "status": "maybe"}]})
        self.assertEqual(out[0]["status"], "proposed")

    def test_not_a_dict_raises(self):
        with self.assertRaises(ValueError):
            events._clean_events([1, 2])


class KeyTests(unittest.TestCase):
    def test_stable_across_title_punctuation(self):
        a = events.event_key("t1", "Alex's BBQ!", "2026-09-05")
        b = events.event_key("t1", "alex s bbq", "2026-09-05")
        self.assertEqual(a, b)

    def test_thread_and_date_separate_events(self):
        a = events.event_key("t1", "BBQ", "2026-09-05")
        self.assertNotEqual(a, events.event_key("t2", "BBQ", "2026-09-05"))
        self.assertNotEqual(a, events.event_key("t1", "BBQ", "2026-09-12"))


class ScriptTests(unittest.TestCase):
    def test_dates_are_component_built_day_first(self):
        s = events.build_event_script("Family", "Tentative · BBQ",
                                      "2026-08-31", "15:00", "", "", "")
        i_day1 = s.index("set day of d1 to 1")
        i_month = s.index("set month of d1 to August")
        self.assertLess(i_day1, i_month)      # day-31 rollover guard
        self.assertIn("set day of d1 to 31", s)
        self.assertIn(f"set time of d1 to {15 * 3600}", s)
        self.assertNotIn('date "', s)          # never locale literals

    def test_late_start_ends_next_day_not_this_morning(self):
        s = events.build_event_script("Fam", "T", "2026-09-05", "23:00",
                                      "", "", "")
        self.assertIn(f"set time of d2 to {1 * 3600}", s)
        self.assertIn("set day of d2 to 6", s)   # crossed midnight -> Sep 6

    def test_stated_end_before_start_crosses_midnight(self):
        s = events.build_event_script("Fam", "T", "2026-09-05", "19:00",
                                      "01:00", "", "")
        self.assertIn("set day of d2 to 6", s)

    def test_quotes_in_titles_are_escaped(self):
        s = events.build_event_script("Fam", 'The "big" one', "2026-09-05",
                                      "", "", 'Bob\'s "yard"', "")
        self.assertIn('\\"big\\"', s)
        self.assertIn('\\"yard\\"', s)

    def test_hold_requires_a_configured_calendar(self):
        with mock.patch.object(events.settings, "get",
                               side_effect=lambda k: ""):
            with self.assertRaises(RuntimeError):
                events.create_hold({"title": "x", "date": "2026-09-05"})


class MergeTests(unittest.TestCase):
    def unit(self):
        return {"key": "t1", "chat_ids": [7], "label": "Fam",
                "participants": ["Alex"], "send": None}

    def test_new_event_enters_once_and_state_survives_rescan(self):
        store, changes = {}, {}
        found = [{"title": "BBQ", "date": "2099-09-05", "time": "",
                  "end_time": "", "location": "", "organizer": "Alex",
                  "status": "proposed", "needs_reply": False,
                  "confidence": 0.9, "quote": "bbq sat?"}]
        with mock.patch.object(events.settings, "get", return_value=""):
            n = events._merge(store, changes, [dict(found[0])],
                              self.unit(), [], 100)
        self.assertEqual(n, 1)
        k = next(iter(store))
        store[k]["state"] = "dismissed"
        with mock.patch.object(events.settings, "get", return_value=""):
            n2 = events._merge(store, changes, [dict(found[0])],
                               self.unit(), [], 101)
        self.assertEqual(n2, 0)
        self.assertEqual(store[k]["state"], "dismissed")

    def test_drifted_title_never_mints_a_second_event(self):
        store, changes = {}, {}
        base = {"date": "2099-09-05", "time": "", "end_time": "",
                "location": "", "organizer": "Alex", "status": "proposed",
                "needs_reply": False, "confidence": 0.9, "quote": ""}
        with mock.patch.object(events.settings, "get", return_value=""):
            events._merge(store, changes, [dict(base, title="Alex's BBQ")],
                          self.unit(), [], 100)
            n = events._merge(store, changes,
                              [dict(base, title="BBQ at Alex's place")],
                              self.unit(), [], 101)
        self.assertEqual(n, 0)          # fuzzy match: same thread+date+words
        self.assertEqual(len(store), 1)

    def test_past_events_never_enter(self):
        store = {}
        found = [{"title": "Old", "date": "2020-01-01", "time": "",
                  "end_time": "", "location": "", "organizer": "",
                  "status": "proposed", "needs_reply": False,
                  "confidence": 0.9, "quote": ""}]
        with mock.patch.object(events.settings, "get", return_value=""):
            self.assertEqual(
                events._merge(store, {}, found, self.unit(), [], 100), 0)


class ReviewSourceTests(unittest.TestCase):
    EV = {"key": "abc123", "title": "Alex's BBQ", "date": "2099-09-05",
          "time": "15:00", "organizer": "Alex", "organizer_pid": "p_1",
          "state": "calendared", "detected": "2026-08-27T18:00:00",
          "thread_label": "Fam", "quote": "bbq saturday?",
          "location": "the yard",
          "drafts": {"reply": "We're in for Saturday!",
                     "partner_fyi": "Alex is hosting a BBQ Sat 3pm"}}

    def test_row_shape_carries_draft_and_actions(self):
        with mock.patch.object(events, "pending", return_value=[self.EV]):
            rows = reviewqueue._events_read()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["id"], "events:abc123")
        self.assertIn("Alex's BBQ", r["title"])
        self.assertIn("We're in for Saturday!", r["note"])  # visible pre-tap
        self.assertEqual(list(r["actions"]),
                         ["reply", "tell partner", "drop"])  # calendared: no
        self.assertEqual(r["ref"], "the yard")

    def test_uncalendared_event_offers_the_hold(self):
        ev = dict(self.EV, state="new")
        with mock.patch.object(events, "pending", return_value=[ev]):
            rows = reviewqueue._events_read()
        self.assertIn("calendar", rows[0]["actions"])

    def test_source_is_registered_with_the_queue(self):
        src = reviewqueue.SOURCES.get("events")
        self.assertIsNotNone(src)
        self.assertEqual(tuple(src.actions),
                         ("reply", "tell partner", "calendar", "drop"))


class ActTests(unittest.TestCase):
    def test_unknown_event_and_action_raise(self):
        with mock.patch.object(events.jsonstore, "read",
                               return_value={"events": {}}):
            with self.assertRaises(ValueError):
                events.act("nope", "drop")
        with mock.patch.object(events.jsonstore, "read", return_value={
                "events": {"k": {"key": "k", "date": "2099-01-01"}}}):
            with self.assertRaises(ValueError):
                events.act("k", "explode")

    def test_reply_without_draft_refuses(self):
        with mock.patch.object(events.jsonstore, "read", return_value={
                "events": {"k": {"key": "k", "drafts": None}}}):
            with self.assertRaises(ValueError):
                events.act("k", "reply")

    def test_second_tap_answers_already_never_resends(self):
        ev = {"key": "k", "reply_sent": "2026-08-27T18:00:00",
              "fyi_sent": "2026-08-27T18:01:00", "state": "calendared",
              "drafts": {"reply": "x", "partner_fyi": "y"}}
        with mock.patch.object(events.jsonstore, "read",
                               return_value={"events": {"k": ev}}), \
             mock.patch.object(events, "send", create=True) as snd:
            self.assertTrue(events.act("k", "reply")["already"])
            self.assertTrue(events.act("k", "tell partner")["already"])
            self.assertTrue(events.act("k", "calendar")["already"])
        snd.send_message.assert_not_called()

    def test_passive_instance_refuses_every_action(self):
        with mock.patch.dict(events.os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(RuntimeError):
                events.act("k", "drop")

    def test_exhausted_event_leaves_the_queue(self):
        done = {"state": "calendared", "reply_sent": "t", "fyi_sent": "t",
                "drafts": {"reply": "x", "partner_fyi": "y"}}
        open_ = dict(done, fyi_sent=None)
        self.assertTrue(events._exhausted(done))
        self.assertFalse(events._exhausted(open_))


class FyiTests(unittest.TestCase):
    def test_config_wins_then_family_spouse_only(self):
        with mock.patch.object(events.settings, "get",
                               side_effect=lambda k: "p_cfg"):
            self.assertEqual(events.fyi_person(), "p_cfg")
        # a business partner and an ex must never receive the family FYI —
        # only a FAMILY-classed contact recorded as wife/husband/spouse
        c = {"master": {"p_1": {"relationship": "business partner at the firm"},
                        "p_2": {"relationship": "ex-wife"},
                        "p_9": {"relationship": "the owner's wife"}},
             "profiles": {"p_2": {"relationship_class": "family"},
                          "p_9": {"relationship_class": "family"}},
             "by_id": {}}
        with mock.patch.object(events.settings, "get",
                               side_effect=lambda k: ""), \
             mock.patch.object(events.crm, "_load", return_value=c):
            self.assertEqual(events.fyi_person(), "p_9")

    def test_no_qualifying_contact_means_none_not_a_guess(self):
        c = {"master": {"p_1": {"relationship": "business partner"}},
             "profiles": {}, "by_id": {}}
        with mock.patch.object(events.settings, "get",
                               side_effect=lambda k: ""), \
             mock.patch.object(events.crm, "_load", return_value=c):
            self.assertIsNone(events.fyi_person())


class ScanGateTests(unittest.TestCase):
    def test_fixture_mode_never_scans(self):
        with mock.patch.object(events.settings, "fixture_mode",
                               return_value=True):
            self.assertEqual(events.scan()["status"], "skipped")

    def test_passive_instances_never_scan(self):
        with mock.patch.dict(events.os.environ, {"VIRA_PASSIVE": "1"}):
            self.assertEqual(events.scan()["status"], "skipped")


if __name__ == "__main__":
    unittest.main()

"""The sorter: rung-1 heuristics as a table, the rung-2 degrade path,
and the fan-out envelope with stubbed corpora.

Every case pins a FIXED today, so "last March" means the same thing in
2027 as it does the day this was written.

Run: .venv/bin/python -m unittest tests.test_find
"""
import json
import unittest
from datetime import date
from unittest import mock

from server import data as crm
from server import find

TODAY = date(2026, 7, 22)


def _crm_cache():
    return {
        "loaded_at": 1.0,
        "by_id": {
            "p_kim": {"id": "p_kim", "name": "Kim Vantongeren",
                      "activity": {"imsg_n": 5200, "email_n": 40}},
            "p_kip": {"id": "p_kip", "name": "Kim Baxter",
                      "activity": {"imsg_n": 4, "email_n": 0}},
            "p_dana": {"id": "p_dana", "name": "Dana Okonkwo",
                       "activity": {"imsg_n": 90, "email_n": 3}},
            "p_dane": {"id": "p_dane", "name": "Dana Whitfield",
                       "activity": {"imsg_n": 60, "email_n": 0}},
        },
    }


class PlanFixture(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(crm, "_load", _crm_cache)
        p.start()
        self.addCleanup(p.stop)
        find._name_maps.cache = None
        self.addCleanup(lambda: setattr(find._name_maps, "cache", None))

    def plan(self, q):
        return find.plan(q, today=TODAY)


class DateHeuristics(PlanFixture):
    def test_iso_day_is_a_one_day_window(self):
        f = self.plan("standup 2026-03-14")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2026-03-14",
                                                    "2026-03-15"))

    def test_iso_month(self):
        f = self.plan("invoices 2025-11")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2025-11-01",
                                                    "2025-12-01"))

    def test_last_month_name_is_this_year_when_it_has_happened(self):
        f = self.plan("photos from last March")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2026-03-01",
                                                    "2026-04-01"))

    def test_month_name_not_yet_reached_walks_back_a_year(self):
        f = self.plan("the October offsite")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2025-10-01",
                                                    "2025-11-01"))

    def test_since_month_is_open_ended(self):
        f = self.plan("lease since June")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2026-06-01", None))

    def test_before_year(self):
        f = self.plan("anything before 2024")["filters"]
        self.assertEqual((f["since"], f["until"]), (None, "2024-01-01"))

    def test_relative_window(self):
        f = self.plan("receipts past 2 weeks")["filters"]
        self.assertEqual(f["since"], "2026-07-08")

    def test_last_week(self):
        f = self.plan("what shipped last week")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2026-07-13",
                                                    "2026-07-20"))

    def test_bare_may_is_not_a_date(self):
        # "may" is an ordinary verb; only a preposition or a year makes
        # it a month
        self.assertIsNone(self.plan("we may need a new plan")
                          ["filters"]["since"])
        self.assertEqual(self.plan("in may")["filters"]["since"],
                         "2026-05-01")

    def test_phone_digits_are_not_a_year(self):
        # a phone ends in four digits that read exactly like a year, in
        # whatever punctuation style it was typed
        f = self.plan("(917) 555 2014")["filters"]
        self.assertIsNone(f["since"])
        self.assertTrue(f["exact"])

    def test_date_is_consumed_not_left_in_the_terms(self):
        self.assertNotIn("march", self.plan("beach last March")["text"].lower())


class OrderAndTotality(PlanFixture):
    def test_most_recent_sets_recency(self):
        self.assertEqual(self.plan("most recent retro")["filters"]["order"],
                         "recent")

    def test_earliest_sets_oldest(self):
        self.assertEqual(self.plan("earliest mention of Qocha")
                         ["filters"]["order"], "oldest")

    def test_last_march_is_a_date_not_a_superlative(self):
        p = self.plan("last March")
        self.assertEqual(p["filters"]["order"], "relevance")
        self.assertEqual(p["filters"]["since"], "2026-03-01")

    def test_totality(self):
        self.assertTrue(self.plan("all the links")["filters"]["limit_all"])
        self.assertTrue(self.plan("how many photos")["filters"]["limit_all"])


class Exactness(PlanFixture):
    def test_quoted_phrase_is_kept_whole(self):
        f = self.plan('the "second brain thesis" note')["filters"]
        self.assertEqual(f["phrases"], ["second brain thesis"])
        self.assertTrue(f["exact"])

    def test_filename(self):
        self.assertTrue(self.plan("quarterly-deck.pptx")["filters"]["exact"])

    def test_email_address(self):
        self.assertTrue(self.plan("owner@example.test")
                        ["filters"]["exact"])

    def test_plain_words_are_not_exact(self):
        self.assertFalse(self.plan("beach sunset")["filters"]["exact"])


class Operators(PlanFixture):
    def test_db_narrows_to_one_corpus(self):
        p = self.plan("db:notes agent orchestration")
        self.assertEqual(p["databases"], ["notes"])
        self.assertEqual(p["text"], "agent orchestration")

    def test_operators_are_stripped_left_to_right(self):
        # regression: cutting one span used to invalidate the next
        p = self.plan("from:me kind:photo beach")
        self.assertEqual(p["filters"]["sender"], "me")
        self.assertEqual(p["filters"]["kind"], ["photo"])
        self.assertEqual(p["text"], "beach")

    def test_is_operator(self):
        self.assertEqual(self.plan("is:sent contract")
                         ["filters"]["direction"], "sent")


class Names(PlanFixture):
    def test_full_name(self):
        p = self.plan("Kim Vantongeren dinner plans")
        self.assertEqual(p["filters"]["person"], "p_kim")
        self.assertNotIn("vantongeren", p["text"].lower())

    def test_dominant_first_name_wins(self):
        # two Kims, one with 1000x the traffic
        self.assertEqual(self.plan("kim beach photos")
                         ["filters"]["person"], "p_kim")

    def test_ambiguous_first_name_stays_unresolved(self):
        # two Danas within 2x of each other: guessing would be worse
        self.assertIsNone(self.plan("dana beach photos")
                          ["filters"]["person"])

    def test_ordinary_words_are_never_names(self):
        self.assertIsNone(self.plan("management delivery client")
                          ["filters"]["person"])


class DatabaseRanking(PlanFixture):
    def test_media_words_lead_with_media(self):
        self.assertEqual(self.plan("photos of the boat")["primary"], "media")

    def test_note_words_lead_with_notes(self):
        self.assertEqual(self.plan("session retro decisions")["primary"],
                         "notes")

    def test_speech_words_lead_with_messages(self):
        self.assertEqual(self.plan("what did they say about the lease")
                         ["primary"], "messages")

    def test_bare_name_leads_with_people(self):
        self.assertEqual(self.plan("Kim Vantongeren")["primary"],
                         "people")

    def test_every_corpus_is_still_queried(self):
        # signals reorder, they never exclude — that is the whole merge
        self.assertEqual(sorted(self.plan("photos of the boat")["databases"]),
                         sorted(find.DATABASES))

    def test_no_signal_falls_back_on_shape(self):
        self.assertEqual(self.plan("blue umbrella")["primary"], "media")
        self.assertEqual(self.plan("why did that happen?")["primary"],
                         "notes")


class ShapeAndTerms(PlanFixture):
    def test_question_mark(self):
        self.assertEqual(self.plan("blue umbrella?")["shape"], "answer")

    def test_leading_interrogative(self):
        self.assertEqual(self.plan("who sent the deck")["shape"], "answer")

    def test_short_phrase_is_a_list(self):
        self.assertEqual(self.plan("blue umbrella")["shape"], "list")

    def test_long_sentence_with_a_verb_is_a_question(self):
        self.assertEqual(
            self.plan("the session where we decided to build the sorter "
                      "thing")["shape"], "answer")

    def test_filler_is_dropped_from_the_terms(self):
        self.assertEqual(self.plan("what did they say about the boat")
                         ["text"], "say boat")

    def test_all_filler_query_keeps_its_words(self):
        self.assertTrue(self.plan("what is this about")["text"])

    def test_why_names_what_the_sorter_did(self):
        why = self.plan("most recent photos from last March")["why"]
        self.assertIn("newest first", why)
        self.assertIn("2026-03-01", why)


class RungTwo(PlanFixture):
    def test_model_plan_overrides_heuristics(self):
        payload = ('{"databases": ["media"], "person": "p_dana",'
                   ' "order": "recent", "query": "snowmobile",'
                   ' "kind": "photo", "wants": "a photo"}')
        with mock.patch("server.suggest.complete", return_value=payload), \
             mock.patch("server.search._people_for_prompt", return_value=""):
            p = find.plan_llm("did someone send a snowmobile picture",
                              today=TODAY)
        self.assertEqual(p["rung"], 2)
        # the model's list REORDERS the corpora, it never excludes them
        # (the module rule; only an explicit db: narrows) - the rest of
        # the databases ride behind its preference
        self.assertEqual(p["primary"], "media")
        self.assertEqual(p["databases"][0], "media")
        self.assertEqual(set(p["databases"]), set(find.DATABASES))
        self.assertEqual(p["filters"]["person"], "p_dana")
        self.assertEqual(p["text"], "snowmobile")

    def test_dead_backend_degrades_to_rung_one(self):
        with mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("no model")), \
             mock.patch("server.search._people_for_prompt", return_value=""):
            p = find.plan_llm("most recent photos", today=TODAY)
        self.assertEqual(p["rung"], 1)
        self.assertEqual(p["filters"]["order"], "recent")

    def test_unparseable_reply_degrades_to_rung_one(self):
        with mock.patch("server.suggest.complete", return_value="sorry!"), \
             mock.patch("server.search._people_for_prompt", return_value=""):
            p = find.plan_llm("most recent photos", today=TODAY)
        self.assertEqual(p["rung"], 1)


class FanOut(PlanFixture):
    def _adapters(self, **over):
        seen = {}

        def maker(name, rows):
            def adapter(p, limit):
                seen[name] = p["filters"]
                return {"rows": rows[:limit], "count": len(rows)}
            return adapter

        adapters = {"notes": maker("notes", [{"path": "a.md"}]),
                    "media": maker("media", [{"seq": 1}, {"seq": 2}]),
                    "people": maker("people", []),
                    "messages": maker("messages", [{"seq": 9}])}
        adapters.update(over)
        return adapters, seen

    def test_groups_stay_separate_with_their_own_counts(self):
        adapters, _ = self._adapters()
        with mock.patch.object(find, "ADAPTERS", adapters):
            out = find.run(self.plan("beach"), limit=10)
        self.assertEqual(set(out["groups"]), set(find.DATABASES))
        self.assertEqual(out["counts"]["media"], 2)
        self.assertEqual(out["counts"]["people"], 0)

    def test_filters_reach_every_adapter(self):
        adapters, seen = self._adapters()
        with mock.patch.object(find, "ADAPTERS", adapters):
            find.run(self.plan("photos from last March"), limit=10)
        for name in find.DATABASES:
            self.assertEqual(seen[name]["since"], "2026-03-01")

    def test_one_dead_corpus_never_kills_the_search(self):
        def broken(p, limit):
            raise RuntimeError("index missing")
        adapters, _ = self._adapters(notes=broken)
        with mock.patch.object(find, "ADAPTERS", adapters):
            out = find.run(self.plan("beach"), limit=10)
        self.assertIn("index missing", out["groups"]["notes"]["error"])
        self.assertEqual(out["counts"]["media"], 2)

    def test_explicit_db_queries_only_that_one(self):
        adapters, seen = self._adapters()
        with mock.patch.object(find, "ADAPTERS", adapters):
            out = find.run(self.plan("db:media beach"), limit=10)
        self.assertEqual(list(out["groups"]), ["media"])
        self.assertNotIn("notes", seen)

    def test_limit_is_honoured(self):
        adapters, _ = self._adapters()
        with mock.patch.object(find, "ADAPTERS", adapters):
            out = find.run(self.plan("beach"), limit=1)
        self.assertEqual(len(out["groups"]["media"]["rows"]), 1)


class AskRouting(PlanFixture):
    """The answer runs over the SAME filtered hits the list shows — the
    whole point of the merge is that the prose cannot contradict the
    rows beside it."""

    def test_notes_answer_uses_the_filtered_hits(self):
        hits = [{"path": "retro.md", "title": "Retro", "text": "we decided"}]
        adapters = {
            "notes": lambda p, n: {"rows": [], "count": 1, "hits": hits},
            "media": lambda p, n: {"rows": [], "count": 0},
            "people": lambda p, n: {"rows": [], "count": 0},
            "messages": lambda p, n: {"rows": [], "count": 0}}
        seen = {}

        def fake_ask(question, k=10, hits=None):
            seen["hits"] = hits
            return {"answer": "grounded", "citations": [{"path": "retro.md"}]}

        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("no model")), \
             mock.patch("server.search._people_for_prompt", return_value=""), \
             mock.patch("server.vault.ask", fake_ask):
            out = find.ask("the session where we decided about the sorter",
                           today=TODAY)
        self.assertEqual(out["answer"], "grounded")
        self.assertEqual(seen["hits"], hits)
        self.assertEqual(out["citations"], [{"path": "retro.md"}])

    def test_media_answer_reuses_our_plan(self):
        adapters = {"media": lambda p, n: {"rows": [], "count": 0},
                    "notes": lambda p, n: {"rows": [], "count": 0},
                    "people": lambda p, n: {"rows": [], "count": 0},
                    "messages": lambda p, n: {"rows": [], "count": 0}}
        seen = {}

        def fake_ask(question, plan=None):
            seen["plan"] = plan
            return {"answer": "narrated", "relaxed": ["kind"], "results": []}

        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("no model")), \
             mock.patch("server.search._people_for_prompt", return_value=""), \
             mock.patch("server.search.ask", fake_ask):
            out = find.ask("did someone send me a photo of the boat",
                           today=TODAY)
        self.assertEqual(out["answer"], "narrated")
        self.assertEqual(seen["plan"]["kind"], "photo")     # not a re-parse

    def test_a_failing_answer_leaves_the_lists_standing(self):
        adapters = {"notes": lambda p, n: {"rows": [{"path": "a.md"}],
                                           "count": 1, "hits": []},
                    "media": lambda p, n: {"rows": [], "count": 0},
                    "people": lambda p, n: {"rows": [], "count": 0},
                    "messages": lambda p, n: {"rows": [], "count": 0}}
        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete",
                        side_effect=RuntimeError("no model")), \
             mock.patch("server.search._people_for_prompt", return_value=""), \
             mock.patch("server.vault.ask",
                        side_effect=RuntimeError("backend down")):
            out = find.ask("what did we decide about the sorter thing",
                           today=TODAY)
        self.assertIsNone(out["answer"])
        self.assertIn("backend down", out["answer_error"])
        self.assertEqual(out["counts"]["notes"], 1)


class TrailingWindows(PlanFixture):
    """"this month" is a RECENCY qualifier, never the calendar bucket:
    read month-to-date it is a near-empty window on the 1st (the
    owner-reported insurance miss, 2026-09-01)."""

    def test_this_month_is_a_trailing_window(self):
        f = self.plan("insurance this month")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2026-06-21", None))

    def test_this_month_on_the_first_still_reaches_back(self):
        f = find.plan("insurance this month",
                      today=date(2026, 9, 1))["filters"]
        self.assertEqual((f["since"], f["until"]), ("2026-08-01", None))

    def test_this_week_trails_seven_days(self):
        f = self.plan("what shipped this week")["filters"]
        self.assertEqual((f["since"], f["until"]), ("2026-07-15", None))

    def test_last_month_stays_the_calendar_month(self):
        # "last X" names a COMPLETED unit; only "this X" trails
        f = self.plan("invoices last month")["filters"]
        self.assertEqual((f["since"], f["until"]),
                         ("2026-06-01", "2026-07-01"))


class AskRelaxAndFallback(PlanFixture):
    """The corpora that never had an answer contract (messages, people)
    answer over their filtered rows now, relax an empty query one
    constraint at a time, and fall back to a corpus that has something
    to say - the insurance dead-end of 2026-09-01, ended."""

    ZERO = staticmethod(lambda p, n: {"rows": [], "count": 0})

    def _plan_payload(self, **extra):
        got = {"databases": ["messages"], "sender": "p_kim",
               "query": "insurance", "wants": "the text"}
        got.update(extra)
        return json.dumps(got)

    def test_messages_relax_drops_the_date_window_and_answers(self):
        rows = [{"seq": 1, "when": "2026-07-01T10:00", "person_id": "p_kim",
                 "text": "new insurance member ids attached",
                 "source": "imessage", "from_me": False}]

        def messages(p, n):
            if p["filters"]["since"]:
                return {"rows": [], "count": 0}
            return {"rows": rows, "count": 1}

        adapters = {"messages": messages, "notes": self.ZERO,
                    "media": self.ZERO, "people": self.ZERO}
        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete",
                        side_effect=[self._plan_payload(since="2026-07-22"),
                                     "answered from the rows"]), \
             mock.patch("server.search._people_for_prompt",
                        return_value=""):
            out = find.ask("did Kim text me the insurance recently",
                           today=TODAY)
        self.assertEqual(out["answer"], "answered from the rows")
        self.assertIn("the date window", out["relaxed"])
        # the list beside the answer shows the SAME relaxed hit set
        self.assertEqual(out["counts"]["messages"], 1)
        self.assertNotIn("fallback", out)

    def test_relaxation_widens_sender_to_the_conversation(self):
        def messages(p, n):
            f = p["filters"]
            if f["sender"] is None and f["person"] == "p_kim":
                return {"rows": [{"seq": 2, "text": "here", "from_me": True,
                                  "when": "2026-07-02T09:00"}], "count": 1}
            return {"rows": [], "count": 0}

        adapters = {"messages": messages, "notes": self.ZERO,
                    "media": self.ZERO, "people": self.ZERO}
        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete",
                        side_effect=[self._plan_payload(), "got it"]), \
             mock.patch("server.search._people_for_prompt",
                        return_value=""):
            out = find.ask("did Kim send the thing", today=TODAY)
        self.assertEqual(out["answer"], "got it")
        self.assertIn("either direction with them", out["relaxed"])

    def test_empty_primary_falls_back_to_a_corpus_with_hits(self):
        hits = [{"path": "n.md"}]
        adapters = {"messages": self.ZERO, "media": self.ZERO,
                    "people": self.ZERO,
                    "notes": lambda p, n: {"rows": [{"path": "n.md"}],
                                           "count": 1, "hits": hits}}

        def fake_vault_ask(question, k=10, hits=None):
            return {"answer": "it is in your notes", "citations": []}

        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete",
                        side_effect=[self._plan_payload(
                            databases=["messages", "notes"])]), \
             mock.patch("server.search._people_for_prompt",
                        return_value=""), \
             mock.patch("server.vault.ask", fake_vault_ask):
            out = find.ask("where is the insurance info", today=TODAY)
        self.assertEqual(out["fallback"],
                         {"from": "messages", "to": "notes"})
        self.assertEqual(out["answer"], "it is in your notes")

    def test_the_answer_sees_sibling_corpora_rows_labelled(self):
        # the insurance-question shape: the primary (messages) has rows,
        # but the thing itself is a PHOTO - the answer prompt carries
        # the media rows under a labelled header so it can point there
        msg_rows = [{"seq": 1, "when": "2026-06-01T10:00",
                     "person_id": "p_kim", "text": "insurance chat",
                     "from_me": False}]
        media_rows = [{"seq": 7, "kind": "photo", "name": "card.png",
                       "when": "2026-06-02T11:00", "person_id": "p_kim",
                       "caption": "an insurance card", "from_me": False}]
        adapters = {"messages": lambda p, n: {"rows": msg_rows,
                                              "count": 1},
                    "media": lambda p, n: {"rows": media_rows,
                                           "count": 1},
                    "notes": self.ZERO, "people": self.ZERO}
        prompts = []

        def fake_complete(prompt):
            if not prompts:          # rung 2's plan call
                prompts.append(prompt)
                return self._plan_payload()
            prompts.append(prompt)
            return "the card is the photo"

        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete", fake_complete), \
             mock.patch("server.search._people_for_prompt",
                        return_value=""):
            out = find.ask("where are the insurance account numbers",
                           today=TODAY)
        self.assertEqual(out["answer"], "the card is the photo")
        answer_prompt = prompts[-1]
        self.assertIn("Also retrieved from media", answer_prompt)
        self.assertIn("card.png", answer_prompt)
        self.assertIn("insurance chat", answer_prompt)

    def test_nothing_anywhere_is_an_honest_sentence_never_a_blank(self):
        adapters = {db: self.ZERO for db in find.DATABASES}

        def fake_media_ask(question, plan=None):
            return {"answer": None, "relaxed": [], "results": []}

        with mock.patch.object(find, "ADAPTERS", adapters), \
             mock.patch("server.suggest.complete",
                        side_effect=[self._plan_payload()]), \
             mock.patch("server.search._people_for_prompt",
                        return_value=""), \
             mock.patch("server.search.ask", fake_media_ask):
            out = find.ask("did Kim send the zorp file", today=TODAY)
        self.assertTrue(out["answer"])
        self.assertIn("Nothing matched", out["answer"])
        # the sentence names the person, not a pid
        self.assertIn("Kim Vantongeren", out["answer"])


if __name__ == "__main__":
    unittest.main()

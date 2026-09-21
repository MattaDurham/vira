"""Chat with Vira - a session rendered as a conversation (2026-09-01).

The engine is the session harness, untouched; this pins the layer that
turns it into a chat: the store, the launch-then-say rule, the follower
that files an answer at the turn boundary, what a turn looked at (data
off the runner, never a guess), exact citation resolution, and the
concept pass that no longer needs a vault path. The runner's own tool
record is pinned with the real method over a stand-in state.

Every store is rooted at a tmp path and every session call is stubbed:
a test here must never launch a real runner.

Run: .venv/bin/python -m unittest tests.test_virachat
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from server import jobfiles, runner, virachat


REAL_THREAD = threading.Thread


def snap(status="running", awaiting="reply", result="", tools=(), error=None):
    return {"status": status, "awaiting": awaiting, "result_text": result,
            "tools": list(tools), "error": error}


def tool(tname, turn=0, t=10.0, **inp):
    return {"turn": turn, "name": "mcp__vira__" + tname, "input": inp, "t": t}


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(virachat, "_ENRICHMENT_SLOTS", threading.BoundedSemaphore(2)),
                  mock.patch.object(virachat, "STORE",
                                    Path(self.tmp.name) / "vira-chat.json"),
                  mock.patch.object(jobfiles, "JOBS_DIR", Path(self.tmp.name) / "jobs"),
                  mock.patch.object(virachat.threading, "Thread"),
                  mock.patch.object(virachat.threading, "Timer")):
            p.start()
            self.addCleanup(p.stop)
        self.launched, self.said = [], []
        launch = mock.Mock(side_effect=lambda *a, **k: (self.launched.append((a, k)) or "job000000001"))
        say = mock.Mock(side_effect=lambda jid, text: (self.said.append((jid, text)) or {"job": jid, "resumed": False}))
        self.sessions = mock.Mock(launch=launch, say=say, get=mock.Mock(return_value=None))
        for p in (mock.patch("server.session.sessions", self.sessions),
                  mock.patch("server.models.connected",
                             return_value=[{"id": "anthropic", "connected": True}])):
            p.start(); self.addCleanup(p.stop)


class TheStore(ChatBase):
    def test_a_fresh_store_has_no_chat(self):
        self.assertIsNone(virachat.current())
        self.assertEqual(virachat.summary_rows(), [])

    def test_new_and_switch(self):
        a = virachat.new(); b = virachat.new()
        self.assertEqual(virachat.current()["id"], b["id"])
        virachat.switch(a["id"])
        self.assertEqual(virachat.current()["id"], a["id"])
        with self.assertRaises(KeyError):
            virachat.switch("chat_nope")
        self.assertEqual([r["id"] for r in virachat.summary_rows()][:1], [b["id"]] if b["updated"] > a["updated"] else [a["id"]])


class SendingATurn(ChatBase):
    def test_http_context_stays_on_the_owning_instance(self):
        with mock.patch("server.instance.api_url", return_value="http://localhost:8392"):
            for native in (True, False):
                prompt = virachat._launch_prompt("hello", native=native)
                self.assertIn("http://localhost:8392", prompt)
                self.assertNotIn("localhost:8377", prompt)

    def test_the_first_turn_launches_a_session_on_the_tools_at_the_default_rung(self):
        s = virachat.send("What is on my calendar tomorrow?")
        self.assertEqual(len(self.launched), 1)
        args, kw = self.launched[0]
        # NOT read-only and NO rung named here: a chat is an owner session
        # and runs on session_default_mode like every other dispatch. The
        # read-only first cut could see /api/subs and could not call it.
        self.assertFalse(kw.get("read_only", False))
        self.assertNotIn("mode", kw)
        self.assertNotIn("permission_mode", kw)
        self.assertIn("narrowest useful tool", args[0])
        self.assertEqual(kw["meta"], {"kind": "chat"})
        self.assertEqual(kw["provider"], "anthropic")   # the engine with the tools
        self.assertIn("mcp__vira__find", args[0])
        self.assertIn("What is on my calendar tomorrow?", args[0])
        self.assertEqual(s["job_id"], "job000000001")
        self.assertEqual(s["turns"][0]["status"], "pending")
        virachat.threading.Thread.assert_called_once()

    def test_openai_chat_uses_the_same_native_vira_tool_contract(self):
        with mock.patch("server.models.connected", return_value=[{"id": "openai", "connected": True}]):
            virachat.send("hello")
        args, kw = self.launched[0]
        self.assertEqual(kw["provider"], "openai")
        self.assertIn("vira.find", args[0])
        self.assertNotIn("mcp__vira__find", args[0])

    def test_a_later_turn_talks_to_the_same_session(self):
        virachat.send("first")
        virachat._finish_turn(virachat.current()["id"], 0, "answer one", "", [], [], [], [])
        virachat.send("second")
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.said, [("job000000001", "second")])

    def test_a_resumed_session_moves_the_chat_to_its_new_job(self):
        virachat.send("first")
        virachat._finish_turn(virachat.current()["id"], 0, "a", "", [], [], [], [])
        self.sessions.say.side_effect = lambda jid, text: {"job": "job000000002", "resumed": True}
        s = virachat.send("second")
        self.assertEqual(s["job_id"], "job000000002")

    def test_a_pending_turn_refuses_a_second(self):
        virachat.send("first")
        with self.assertRaises(virachat.Busy):
            virachat.send("second")

    def test_a_launch_that_cannot_start_is_the_turns_answer(self):
        self.sessions.launch.side_effect = ValueError("no supervisor here")
        s = virachat.send("hello")
        t = s["turns"][0]
        self.assertEqual(t["status"], "failed")
        self.assertIn("no supervisor here", t["answer"])
        virachat.threading.Thread.assert_not_called()

    def test_empty_is_refused(self):
        with self.assertRaises(ValueError):
            virachat.send("   ")


class FollowingATurn(ChatBase):
    """_follow driven with a scripted sequence of snapshots and a fake clock."""

    def drive(self, snaps, prior="", max_s=60, sent_t=0.0):
        virachat._mutate(lambda st: st["sessions"][self.sid]["turns"][0].update(sent_t=sent_t))
        seq = iter(snaps)
        last = {"s": None}

        def get(_jid):
            try:
                last["s"] = next(seq)
            except StopIteration:
                pass
            return last["s"]
        clock = {"t": 0.0}
        with mock.patch.object(virachat, "_session_snapshot", side_effect=get), \
             mock.patch.object(virachat, "_concepts", return_value=([], [])):
            virachat._follow(self.sid, 0, "job000000001", prior, sent_t, max_s=max_s,
                             clock=lambda: clock["t"],
                             sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
            for call in virachat.threading.Thread.call_args_list:
                if call.kwargs.get("target") is virachat._enrich:
                    virachat._enrich(*call.kwargs["args"])
        return virachat.current()["turns"][0]

    def setUp(self):
        super().setUp()
        self.sid = virachat.send("Show me the card")["id"]

    def test_the_answer_lands_when_the_session_parks_again(self):
        t = self.drive([snap(awaiting=None),
                        snap(awaiting=None, tools=[tool("find", query="insurance card")]),
                        snap(awaiting="reply", result="It is the BlueCross card.",
                             tools=[tool("find", query="insurance card")])], sent_t=0.0)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["answer"], "It is the BlueCross card.")
        self.assertEqual(t["looked_at"][0]["kind"], "find")
        self.assertEqual(t["looked_at"][0]["query"], "insurance card")

    def test_a_stale_parked_answer_is_not_mistaken_for_the_new_one(self):
        # the session is still parked on the PRIOR answer when polling starts
        t = self.drive([snap(awaiting="reply", result="old answer"),
                        snap(awaiting=None, result="old answer"),
                        snap(awaiting="reply", result="new answer")], prior="old answer")
        self.assertEqual(t["answer"], "new answer")

    def test_a_dead_session_fails_the_turn_by_name(self):
        t = self.drive([snap(status="error", awaiting=None, error="You've hit your monthly spend limit")])
        self.assertEqual(t["status"], "failed")
        self.assertIn("spend limit", t["answer"])

    def test_a_turn_that_never_settles_times_out(self):
        t = self.drive([snap(awaiting=None)] * 5, max_s=3)
        self.assertEqual(t["status"], "failed")
        self.assertIn("no answer after", t["answer"])

    def test_a_vanished_session_fails_the_turn(self):
        t = self.drive([None])
        self.assertEqual(t["status"], "failed")

    def test_a_settled_turn_is_never_overwritten(self):
        virachat._finish_turn(self.sid, 0, "first", "", [], [], [], [])
        virachat._finish_turn(self.sid, 0, "second", "", [], [], [], [])
        self.assertEqual(virachat.current()["turns"][0]["answer"], "first")


class AnswerDelivery(ChatBase):
    def setUp(self):
        super().setUp()
        self.chat = virachat.send("original question")
        self.sid = self.chat["id"]
        self.key = self.chat["turns"][0]["id"]

    def follow(self, result="the answer"):
        with mock.patch.object(virachat, "_session_snapshot", return_value=snap(result=result)):
            virachat._follow(self.sid, 0, "job000000001", "", turn_key=self.key)

    def worker(self):
        calls = [c for c in virachat.threading.Thread.call_args_list
                 if c.kwargs.get("target") is virachat._enrich]
        self.assertEqual(len(calls), 1)
        thread = REAL_THREAD(target=virachat._enrich, args=calls[0].kwargs["args"])
        thread.start()
        return thread

    def test_every_slow_enrichment_stage_runs_after_the_answer_is_readable(self):
        for stage in ("looked_at", "citations", "_concepts"):
            with self.subTest(stage=stage):
                # Each phase can stall independently. No phase may delay
                # answer delivery, nor may reading the chat retry it.
                if stage != "looked_at":
                    self.chat = virachat.new()
                    self.chat = virachat.send("original question")
                    self.sid = self.chat["id"]
                    self.key = self.chat["turns"][0]["id"]
                    virachat.threading.Thread.reset_mock()
                entered, release = threading.Event(), threading.Event()
                result = ([], []) if stage == "_concepts" else []

                def blocked(*args, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(2))
                    return result
                with mock.patch.object(virachat, "looked_at", return_value=[]), \
                     mock.patch.object(virachat, "citations", return_value=[]), \
                     mock.patch.object(virachat, "_concepts", return_value=([], [])), \
                     mock.patch.object(virachat, stage, side_effect=blocked):
                    self.follow()
                    thread = self.worker()
                    try:
                        self.assertTrue(entered.wait(2))
                        for _ in range(2):
                            t = virachat.current()["turns"][0]
                            self.assertEqual((t["status"], t["answer"]), ("done", "the answer"))
                            self.assertEqual(t["enrichment"]["status"], "running")
                        self.assertEqual(len([c for c in virachat.threading.Thread.call_args_list
                                              if c.kwargs.get("target") is virachat._enrich]), 1)
                    finally:
                        release.set()
                        thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(virachat.current()["turns"][0]["enrichment"]["status"], "done")

    def test_timeout_releases_enrichment_and_discards_its_late_result(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return [{"term": "late", "weight": .5}], ["Late suggestion?"]
        with mock.patch.object(virachat, "_concepts", side_effect=blocked):
            self.follow()
            thread = self.worker()
            try:
                self.assertTrue(entered.wait(2))
                t = virachat.current()["turns"][0]
                timer_call = virachat.threading.Timer.call_args
                with mock.patch.object(virachat.time, "time", return_value=t["enrichment"]["deadline_t"] + 1):
                    timer_call.args[1](*timer_call.kwargs["args"], **timer_call.kwargs["kwargs"])
                    self.assertEqual(virachat.current()["turns"][0]["enrichment"]["status"], "failed")
                next_chat = virachat.send("next question")
                self.assertEqual(next_chat["turns"][-1]["status"], "pending")
            finally:
                release.set()
                thread.join(2)
        old = virachat.current()["turns"][0]
        self.assertEqual((old["status"], old["answer"]), ("done", "the answer"))
        self.assertEqual(old["enrichment"]["status"], "failed")
        self.assertEqual(virachat.current()["concepts"], [])

    def test_new_turn_during_enrichment_keeps_the_original_question_and_current_summary(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(_answer):
            entered.set()
            self.assertTrue(release.wait(2))
            return []
        with mock.patch.object(virachat, "citations", side_effect=blocked), \
             mock.patch.object(virachat, "_concepts", return_value=([
                 {"term": "old", "weight": .5}], ["Old suggestion?"])) as concepts:
            self.follow()
            thread = self.worker()
            try:
                self.assertTrue(entered.wait(2))
                virachat.send("new question")
                virachat._finish_turn(self.sid, 1, "new answer", "", [], [],
                                      [{"term": "new", "weight": .6}], ["New suggestion?"])
            finally:
                release.set()
                thread.join(2)
        self.assertEqual(concepts.call_args.args[0], "original question")
        s = virachat.current()
        self.assertEqual(s["follow_up_questions"], ["New suggestion?"])
        self.assertEqual([c["term"] for c in s["concepts"]], ["new"])
        self.assertEqual(s["turns"][0]["enrichment"]["status"], "done")

    def test_old_callbacks_cannot_replace_a_later_turn_and_history_is_preserved(self):
        self.follow()
        virachat.send("replacement question")
        virachat._finish_turn(self.sid, 0, "stale answer", "", [], [], [], [], turn_key=self.key)
        virachat._finish_enrichment(self.sid, self.key, [{"kind": "find"}], [], [], ["Stale?"])
        s = virachat.current()
        self.assertEqual(s["turns"][1]["question"], "replacement question")
        self.assertEqual(s["turns"][1]["status"], "pending")
        self.assertEqual(s["turns"][1]["looked_at"], [])
        self.assertNotEqual(s["turns"][0]["answer"], "stale answer")
        self.assertEqual(s["follow_up_questions"], [])

    def test_send_preserves_decoration_that_finishes_while_the_next_session_opens(self):
        self.follow()

        def opening(*args, **kwargs):
            virachat._finish_enrichment(self.sid, self.key, [{"kind": "find", "query": "old"}], [], [], [])
            return "job000000001", False, False
        with mock.patch.object(virachat, "_open_session", side_effect=opening):
            s = virachat.send("next question")
        self.assertEqual(s["turns"][0]["looked_at"], [{"kind": "find", "query": "old"}])
        self.assertEqual(s["turns"][0]["enrichment"]["status"], "done")

    def test_enrichment_exception_is_named_without_changing_the_answer(self):
        with mock.patch.object(virachat, "citations", side_effect=RuntimeError("lookup failed")):
            self.follow()
            thread = self.worker()
            thread.join(2)
        t = virachat.current()["turns"][0]
        self.assertEqual((t["status"], t["answer"]), ("done", "the answer"))
        self.assertEqual(t["enrichment"], {"status": "failed", "deadline_t": t["enrichment"]["deadline_t"],
                                          "error": "lookup failed"})


    def test_sources_survive_concept_failure_and_are_counted_once(self):
        card = {"kind": "find", "query": "original"}
        cite = {"path": "wiki/source.md", "title": "source"}
        with mock.patch.object(virachat, "looked_at", return_value=[card]), \
             mock.patch.object(virachat, "citations", return_value=[cite]), \
             mock.patch.object(virachat, "_concepts", side_effect=RuntimeError("concept failed")):
            self.follow()
            thread = self.worker()
            thread.join(2)
        s = virachat.current()
        self.assertEqual(s["turns"][0]["looked_at"], [card])
        self.assertEqual(s["turns"][0]["citations"], [cite])
        self.assertEqual(s["cited"][0]["count"], 1)
        self.assertEqual(s["turns"][0]["enrichment"]["status"], "failed")

    def test_sources_are_visible_during_a_blocked_concept_pass_and_survive_timeout(self):
        entered, release = threading.Event(), threading.Event()
        card = {"kind": "find", "query": "original"}
        cite = {"path": "wiki/source.md", "title": "source"}

        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return [], []
        with mock.patch.object(virachat, "looked_at", return_value=[card]), \
             mock.patch.object(virachat, "citations", return_value=[cite]), \
             mock.patch.object(virachat, "_concepts", side_effect=blocked):
            self.follow()
            thread = self.worker()
            try:
                self.assertTrue(entered.wait(2))
                t = virachat.current()["turns"][0]
                self.assertEqual(t["looked_at"], [card])
                self.assertEqual(t["citations"], [cite])
                with mock.patch.object(virachat.time, "time", return_value=t["enrichment"]["deadline_t"] + 1):
                    expired = virachat.current()["turns"][0]
                    self.assertEqual(expired["enrichment"]["status"], "failed")
            finally:
                release.set()
                thread.join(2)
        t = virachat.current()["turns"][0]
        self.assertEqual(t["looked_at"], [card])
        self.assertEqual(t["citations"], [cite])


class Recovery(ChatBase):
    def write_snapshot(self, result, **extra):
        path = jobfiles.job_dir("job000000001") / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(snap(status="done", awaiting=None, result=result), **extra)),
                        encoding="utf-8")

    def make_legacy(self, sid, idx):
        def up(st):
            t = st["sessions"][sid]["turns"][idx]
            for key in ("id", "prior_result", "job_id"):
                t.pop(key, None)
        virachat._mutate(up)

    def test_current_recovers_a_legacy_answer_from_a_finished_durable_job(self):
        s = virachat.send("question")
        self.make_legacy(s["id"], 0)
        self.write_snapshot("recovered answer")
        recovered = virachat.current()
        self.assertEqual(recovered["turns"][0]["answer"], "recovered answer")
        self.assertEqual(recovered["turns"][0]["status"], "done")
        self.assertEqual(recovered["turns"][0]["enrichment"]["status"], "running")
        virachat.current()
        calls = [c for c in virachat.threading.Thread.call_args_list
                 if c.kwargs.get("target") is virachat._enrich]
        self.assertEqual(len(calls), 1)

    def test_current_does_not_recover_the_previous_answer_for_a_legacy_new_question(self):
        s = virachat.send("first question")
        virachat._finish_turn(s["id"], 0, "previous answer", "", [], [], [], [])
        virachat.send("new question")
        self.make_legacy(s["id"], 1)
        self.write_snapshot("previous answer")
        t = virachat.current()["turns"][1]
        self.assertEqual((t["status"], t["answer"]), ("pending", ""))

    def test_session_baseline_prevents_recovering_an_answer_not_yet_in_the_chat(self):
        s = virachat.send("first question")
        virachat._finish_turn(s["id"], 0, "chat answer", "", [], [], [], [])
        self.sessions.get.return_value = snap(result="answer from session terminal")
        virachat.send("new question")
        t = virachat.current()["turns"][1]
        self.assertEqual((t["status"], t["answer"]), ("pending", ""))

    def test_current_expires_enrichment_whose_timer_was_lost_on_restart(self):
        s = virachat.send("question")
        virachat._finish_turn(s["id"], 0, "answer", "", [], [], [], [], enrich=True)
        t = virachat.current()["turns"][0]
        with mock.patch.object(virachat.time, "time", return_value=t["enrichment"]["deadline_t"] + 1):
            t = virachat.current()["turns"][0]
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["enrichment"]["status"], "failed")


    def test_current_expires_a_launch_reservation_abandoned_by_a_crash(self):
        s = virachat.send("question")
        virachat._mutate(lambda st: st["sessions"][s["id"]]["turns"][0].update(
            launching=True, sent_t=time.time() - virachat.TURN_MAX_S - 1))
        t = virachat.current()["turns"][0]
        self.assertEqual(t["status"], "failed")
        self.assertIn("did not finish starting", t["answer"])
        self.assertEqual(virachat.send("next question")["turns"][-1]["status"], "pending")

    def test_a_late_launch_cannot_replace_the_new_turns_job_after_its_lease_expires(self):
        s = virachat.new()
        opened = []

        def opening(job_id, question, **kwargs):
            opened.append(question)
            if question == "slow launch":
                virachat._mutate(lambda st: st["sessions"][s["id"]]["turns"][0].update(
                    sent_t=time.time() - virachat.TURN_MAX_S - 1))
                self.assertEqual(virachat.current()["turns"][0]["status"], "failed")
                virachat.send("new launch")
                return "job_old_late", False, False
            return "job_new", False, False
        with mock.patch.object(virachat, "_open_session", side_effect=opening):
            result = virachat.send("slow launch")
        self.assertEqual(opened, ["slow launch", "new launch"])
        self.assertEqual(result["job_id"], "job_new")
        self.assertEqual(result["turns"][-1]["job_id"], "job_new")
        followers = [c for c in virachat.threading.Thread.call_args_list
                     if c.kwargs.get("target") is virachat._follow]
        self.assertEqual(len(followers), 1)
        self.assertEqual(followers[0].kwargs["args"][2], "job_new")

    def test_current_expires_a_pending_turn_whose_follower_was_lost(self):
        s = virachat.send("question")
        virachat._mutate(lambda st: st["sessions"][s["id"]]["turns"][0].update(
            sent_t=time.time() - virachat.TURN_MAX_S - 1))
        t = virachat.current()["turns"][0]
        self.assertEqual(t["status"], "failed")
        self.assertIn("no answer after", t["answer"])


class ConceptCompletion(unittest.TestCase):
    def test_concept_prompt_uses_the_captured_question_with_no_tools(self):
        with mock.patch("server.suggest.complete", return_value='{"concepts": []}') as complete:
            virachat._concepts("the original question", "the original answer", [], [])
        self.assertIn("QUESTION:\nthe original question", complete.call_args.args[0])
        self.assertEqual(complete.call_args.kwargs["tools"], [])
        self.assertEqual(complete.call_args.kwargs["work_class"], "auxiliary")
        self.assertLessEqual(complete.call_args.kwargs["timeout"], virachat.ENRICHMENT_MAX_S)


class WhatItLookedAt(unittest.TestCase):
    def test_cards_are_the_calls_made_since_the_message_was_sent(self):
        rows = [tool("find", 0, t=1.0, query="old"), tool("vault_note", 1, t=5.0, path="wiki/boat.md"),
                tool("find", 1, t=6.0, query="boat"), tool("find", 1, t=7.0, query="boat")]
        cards = virachat.looked_at(snap(tools=rows), since_t=4.0)
        self.assertEqual([c["kind"] for c in cards], ["note", "find"])
        self.assertEqual(cards[0]["label"], "boat")
        self.assertEqual(cards[0]["path"], "wiki/boat.md")

    def test_a_person_lookup_resolves_to_a_pid_when_the_name_is_exact(self):
        with mock.patch("server.data.search_people",
                        return_value=[{"id": "p_1", "name": "Casey Example"}]):
            cards = virachat.looked_at(snap(tools=[tool("imessage_thread", 0, name="Casey Example")]))
        self.assertEqual(cards[0], {"kind": "person", "label": "Casey Example",
                                    "pid": "p_1", "detail": "messages"})

    def test_an_ambiguous_name_keeps_no_pid(self):
        with mock.patch("server.data.search_people",
                        return_value=[{"id": "p_1", "name": "Casey A"}, {"id": "p_2", "name": "Casey B"}]):
            cards = virachat.looked_at(snap(tools=[tool("crm_lookup", 0, name="Casey")]))
        self.assertIsNone(cards[0]["pid"])

    def test_the_brief_and_the_backlog_have_doors(self):
        cards = virachat.looked_at(snap(tools=[tool("calendar", 0, days="2"), tool("list_ideas", 0)]))
        self.assertEqual([c["kind"] for c in cards], ["brief", "queue"])

    def test_harness_reads_are_not_sources(self):
        rows = [{"turn": 0, "name": "Read", "input": {"path": "/x"}, "t": 1},
                {"turn": 0, "name": "ToolSearch", "input": {"query": "select:x"}, "t": 2},
                {"turn": 0, "name": "Bash", "input": {"query": "ls"}, "t": 3}]
        self.assertEqual(virachat.looked_at(snap(tools=rows)), [])

    def test_a_turn_with_no_calls_of_its_own_inherits_nothing(self):
        # the second live turn (2026-09-01) showed the first turn's seven
        # cards because attribution keyed on the runner's turn counter
        rows = [tool("crm_lookup", 0, t=1.0, name="Casey Example")]
        with mock.patch("server.data.search_people", return_value=[]):
            self.assertEqual(virachat.looked_at(snap(tools=rows), since_t=50.0), [])
            self.assertEqual(len(virachat.looked_at(snap(tools=rows), since_t=0.0)), 1)


class Citations(unittest.TestCase):
    def test_exact_and_inexact_resolutions_are_told_apart(self):
        def resolve(ref):
            return {"wiki/boat": {"path": "wiki/boat.md", "exact": True},
                    "boaty": {"path": "wiki/boat.md", "exact": False}}.get(ref)
        with mock.patch("server.vault.resolve_ref", side_effect=resolve):
            out = virachat.citations("See [[wiki/boat]] and [[boaty|the boat]] and [[nothing]] and [[wiki/boat#x]].")
        self.assertEqual([c["ref"] for c in out], ["wiki/boat", "boaty", "nothing"])
        self.assertTrue(out[0]["exact"]); self.assertFalse(out[1]["exact"])
        self.assertIsNone(out[2]["path"])


class Concepts(unittest.TestCase):
    def test_a_concept_needs_no_note_and_an_invented_note_is_dropped(self):
        raw = {"concepts": [{"term": "the boat", "weight": 0.9, "note": "wiki/boat.md"},
                            {"term": "cap rates", "weight": 0.4, "note": "invented.md"},
                            {"term": "", "weight": 0.5}],
               "follow_up_questions": ["When is the survey?", 7, "Who is the broker?"]}
        concepts, follow = virachat._validate(raw, ["wiki/boat.md"])
        self.assertEqual([(c["term"], c["primary_path"]) for c in concepts],
                         [("the boat", "wiki/boat.md"), ("cap rates", None)])
        self.assertEqual(follow, ["When is the survey?", "Who is the broker?"])

    def test_merge_counts_turns_and_keeps_the_first_note(self):
        prior = [{"term": "The Boat", "weight": 0.5, "turns": 1, "primary_path": None}]
        out = virachat._merge_concepts(prior, [{"term": "the boat", "weight": 0.7, "primary_path": "wiki/boat.md"}])
        self.assertEqual(out[0]["turns"], 2)
        self.assertEqual(out[0]["primary_path"], "wiki/boat.md")
        self.assertGreaterEqual(out[0]["weight"], 0.7)


class TheRunnerRecordsWhatATurnLookedAt(unittest.TestCase):
    def test_record_tool_keeps_the_turn_and_a_bounded_input(self):
        class Stand:
            TOOLS_KEEP = runner.Runner.TOOLS_KEEP
            state = {"turn": 2}
            flushed = 0
            def flush_state(self): self.flushed += 1
        st = Stand()
        runner.Runner.record_tool(st, "mcp__vira__find", {"query": "x" * 500, "limit": 8, "junk": {"a": 1}})
        row = st.state["tools"][0]
        self.assertEqual(row["turn"], 2)
        self.assertEqual(len(row["input"]["query"]), 200)
        self.assertNotIn("junk", row["input"])
        self.assertEqual(st.flushed, 1)

    def test_the_record_is_bounded(self):
        class Stand:
            TOOLS_KEEP = 3
            state = {}
            def flush_state(self): pass
        st = Stand()
        for i in range(5):
            runner.Runner.record_tool(st, "t", {"query": str(i)})
        self.assertEqual([r["input"]["query"] for r in st.state["tools"]], ["2", "3", "4"])

    def test_render_and_reply_are_wired(self):
        src = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn("self.native_tool(b.id, b.name, b.input)", src)
        self.assertIn("self.record_tool(name, arguments)", src)
        i = src.index("await client.query(reply)")
        self.assertIn('self.state["turn"]', src[i - 400:i])


class TheSurfaceSpeaksToTheNewEngine(unittest.TestCase):
    SRC = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_buttons_read_search_and_chat(self):
        # The commit button is SEARCH: this pane is the search, and Chat is
        # the conversation. "Ask" implied a conversation the pane never had.
        i = self.SRC.index("function initFindView(")
        bar = self.SRC[i:self.SRC.index("\n}\n", i)]
        self.assertIn('el("button", "btn primary", "Search")', bar)
        self.assertIn('el("button", "btn", "Chat")', bar)
        self.assertNotIn('"Ask"', bar)
        self.assertNotIn("Chat with my vault", self.SRC)

    def test_the_chat_surface_never_calls_the_vault_only_engine(self):
        for fn in ("loadFindChat", "sendFindChat", "startNewFindChat", "findChatWatch"):
            i = self.SRC.index("function " + fn + "(")
            body = self.SRC[i:self.SRC.index("\n}\n", i)]
            self.assertNotIn("/api/find/chat", body, fn)
            self.assertIn("/api/vira/chat", body, fn)

    def test_a_pending_turn_is_followed_and_a_concept_without_a_note_finds(self):
        self.assertIn('t.status === "pending"', self.SRC)
        self.assertIn("openFindQuery(c.term)", self.SRC)

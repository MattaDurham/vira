"""User-visible chat guarantees; fixtures never launch a provider or read owner data."""
import time
from unittest import mock

from server import virachat
from tests.test_virachat import ChatBase


class ChatContract(ChatBase):
    def test_all_history_survives_and_pages_are_stable(self):
        chats = [virachat.new() for _ in range(25)]
        sid = chats[0]["id"]
        turns = [{"id": f"turn_{i}", "question": str(i), "status": "done", "answer": str(i)}
                 for i in range(75)]
        virachat._mutate(lambda st: st["sessions"][sid].update(turns=turns))
        self.assertEqual(len(virachat.summary_rows()), 25)
        recent = virachat.current(sid)
        self.assertEqual([t["question"] for t in recent["turns"]], [str(i) for i in range(45, 75)])
        older = virachat.current(sid, before=recent["history"]["before"])
        self.assertEqual([t["question"] for t in older["turns"]], [str(i) for i in range(15, 45)])
        self.assertEqual(len(virachat._load()["sessions"][sid]["turns"]), 75)

    def test_run_pins_mode_scope_and_prompt_version(self):
        s = virachat.send("Compare the records", mode="synthesis", sources=["vault:primary"])
        kw = self.launched[0][1]
        self.assertEqual(kw["runtime"]["evidence_scope"], {"sources": ["vault:primary"]})
        self.assertEqual(kw["runtime"]["answer_mode"], "synthesis")
        self.assertEqual(s["turns"][0]["prompt_version"], virachat.PROMPT_VERSION)
        virachat._finish_turn(s["id"], 0, "answer", "", [], [], [], [])
        with self.assertRaisesRegex(ValueError, "evidence scope"):
            virachat.send("Now broaden it", sources=["mail"])

    def test_streams_only_current_public_messages_and_records_visibility(self):
        s = virachat.send("question")
        t = s["turns"][0]
        now = time.time()
        snapshot = {"execution": {"answer_ready_t": now, "message_items": [
            {"phase": "commentary", "text": "Earlier answer", "updated_t": t["sent_t"] - 1},
            {"phase": "reasoning", "text": "Private scratchpad", "updated_t": now},
            {"phase": "unknown", "text": "Unknown phase", "updated_t": now},
            {"phase": "thinking", "text": "Check the parser first.", "updated_t": now},
            {"phase": "commentary", "text": "Two independent records agree", "updated_t": now},
        ]}}
        virachat._observe(s["id"], t["id"], snapshot)
        observed = virachat.current()["turns"][0]
        self.assertEqual([m["text"] for m in observed["messages"]],
                         ["Check the parser first.", "Two independent records agree"])
        self.assertGreaterEqual(observed["metrics"]["time_to_first_useful_s"], 0)
        virachat._finish_turn(s["id"], 0, "answer", "", [], [], [], [])
        virachat.visible(s["id"], t["id"])
        metrics = virachat.current()["turns"][0]["metrics"]
        self.assertGreaterEqual(metrics["visible_answer_lag_s"], 0)
        first = metrics["answer_visible_t"]
        virachat.visible(s["id"], t["id"])
        self.assertEqual(virachat.current()["turns"][0]["metrics"]["answer_visible_t"], first)

    def test_a_reasoning_line_streams_but_is_not_the_first_useful_message(self):
        """The model's summarized thinking (runner phase "thinking") reaches
        the turn as it arrives; the first-useful clock still waits for the
        model's words to the owner."""
        s = virachat.send("question")
        t = s["turns"][0]
        now = time.time()
        thought = {"phase": "thinking", "text": "Check the parser first.", "updated_t": now}
        virachat._observe(s["id"], t["id"], {"execution": {"message_items": [thought]}})
        observed = virachat.current()["turns"][0]
        self.assertEqual([m["phase"] for m in observed["messages"]], ["thinking"])
        self.assertNotIn("first_useful_t", observed.get("metrics") or {})
        virachat._observe(s["id"], t["id"], {"execution": {"message_items": [
            thought, {"phase": "commentary", "text": "Two records agree.", "updated_t": now + 1}]}})
        observed = virachat.current()["turns"][0]
        self.assertEqual([m["phase"] for m in observed["messages"]], ["thinking", "commentary"])
        self.assertIn("first_useful_t", observed["metrics"])

    def test_the_brief_no_longer_asks_the_model_to_hide_its_reasoning(self):
        """The reasoning line shows the model's thinking on purpose; a brief
        that still said to keep it private would be arguing with the UI."""
        for native in (True, False):
            prompt = virachat._launch_prompt("question", native=native)
            self.assertNotIn("private reasoning", prompt)
        self.assertIn("Keep raw tool output out of the answer.",
                      virachat._launch_prompt("question", native=True))

    def test_stop_preserves_partial_and_blocks_late_answer(self):
        s = virachat.send("question")
        t = s["turns"][0]
        self.sessions.get.return_value = {"status": "running", "awaiting": None}
        virachat._mutate(lambda st: st["sessions"][s["id"]]["turns"][0].update(
            messages=[{"phase": "commentary", "text": "An initial finding"}]))
        stopped = virachat.control(s["id"], "stop")
        self.sessions.interrupt.assert_called_once_with(s["job_id"])
        self.assertEqual(stopped["turns"][0]["status"], "pending")
        with self.assertRaises(virachat.Busy):
            virachat.send("too soon")
        self.sessions.get.return_value = {"status": "running", "awaiting": "paused"}
        stopped = virachat.current()
        self.assertEqual(stopped["turns"][0]["status"], "stopped")
        self.assertEqual(stopped["turns"][0]["messages"][0]["text"], "An initial finding")
        self.assertFalse(virachat._finish_turn(s["id"], 0, "late", "", [], [], [], [], turn_key=t["id"]))
        self.assertEqual(virachat.send("follow up")["turns"][-1]["status"], "pending")

    def test_stop_settles_when_runner_is_orphaned(self):
        s = virachat.send("question")
        self.sessions.get.return_value = {"status": "running", "awaiting": None}
        self.assertEqual(virachat.control(s["id"], "stop")["turns"][0]["status"], "pending")
        self.sessions.get.return_value = {"status": "orphaned", "awaiting": None}
        stopped = virachat.current(s["id"])["turns"][0]
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["outcome"], "interrupted")
        self.assertIn("finished_t", stopped)
        self.assertEqual(virachat.send("follow up", s["id"])["turns"][-1]["status"], "pending")

    def test_stop_recovers_when_registry_and_durable_snapshot_are_missing(self):
        s = virachat.send("question")
        self.sessions.get.return_value = {"status": "running", "awaiting": None}
        self.assertEqual(virachat.control(s["id"], "stop")["turns"][0]["status"], "pending")
        # ChatBase uses an empty, temporary job directory. With no registry
        # entry either, recovery must terminate this saved stop request.
        self.sessions.get.return_value = None
        self.assertIsNone(virachat._session_snapshot(s["job_id"]))
        stopped = virachat.current(s["id"])["turns"][0]
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["outcome"], "interrupted")
        self.assertIn("finished_t", stopped)
        self.assertEqual(virachat.send("follow up", s["id"])["turns"][-1]["status"], "pending")

    def test_steering_is_refused_while_stop_is_pending(self):
        s = virachat.send("question")
        self.sessions.get.return_value = {"status": "running", "awaiting": None}
        virachat.control(s["id"], "stop")
        with self.assertRaises(virachat.Busy):
            virachat.control(s["id"], "steer", "Use the recent record")
        self.sessions.say.assert_not_called()
        pending = virachat.current(s["id"])["turns"][0]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending.get("steering", []), [])

    def test_complete_latest_typed_final_answer_survives_result_text_cap(self):
        answer = "Complete answer\n" + "Detailed evidence paragraph. " * 1200 + "\nThe complete ending."
        self.assertGreater(len(answer), 20000)
        for delivery in ("follower", "read_recovery"):
            with self.subTest(delivery=delivery):
                s = virachat.send("A detailed synthesis", virachat.new()["id"])
                t = s["turns"][0]
                self.sessions.get.return_value = {
                    "status": "running", "awaiting": "reply", "result_text": answer[:20000],
                    "execution": {"message_items": [
                        {"phase": "final_answer", "text": "A previous turn's answer",
                         "updated_t": t["sent_t"] - 1, "completed": True},
                        {"phase": "final_answer", "text": "An earlier final message",
                         "updated_t": t["sent_t"] + 1, "completed": True},
                        {"phase": "final_answer", "text": answer,
                         "updated_t": t["sent_t"] + 2, "completed": True},
                        {"phase": "reasoning", "text": "Never public",
                         "updated_t": t["sent_t"] + 3, "completed": True},
                    ]},
                }
                if delivery == "follower":
                    virachat._follow(s["id"], 0, s["job_id"], "", t["sent_t"], turn_key=t["id"])
                completed = virachat.current(s["id"])["turns"][0]
                self.assertEqual(completed["status"], "done")
                self.assertEqual(completed["answer"], answer)

    def test_guidance_is_recorded_in_the_running_turn(self):
        s = virachat.send("question")
        updated = virachat.control(s["id"], "steer", "Use the recent record")
        self.assertEqual(self.said[-1], (s["job_id"], "Use the recent record"))
        self.assertEqual(updated["turns"][0]["steering"][0]["text"], "Use the recent record")
        self.assertEqual(len(updated["turns"]), 1)

    def test_evidence_citations_do_not_resolve_to_current_mutable_note(self):
        handle = "ev_" + "a" * 64
        with mock.patch("server.answer_sources.evidence", return_value={
                "title": "Original", "version": "b" * 64, "span": {"start": 20, "end": 60}}), \
                mock.patch("server.vault.resolve_ref") as resolve:
            rows = virachat.citations("Fact [[evidence:" + handle + "|Original]]")
        resolve.assert_not_called()
        self.assertTrue(rows[0]["exact"])
        self.assertEqual(rows[0]["evidence_handle"], handle)

    def test_adaptive_prompt_has_no_forced_broad_search_or_report(self):
        prompt = virachat._launch_prompt("What changed?", mode="synthesis", sources=["mail"])
        self.assertIn("narrowest useful tool", prompt)
        self.assertIn("counterevidence", prompt)
        self.assertIn("not a measured top three", prompt)
        self.assertIn("Evidence scope: mail", prompt)
        self.assertNotIn("find first", prompt)
        self.assertNotIn("GET /api/subs", prompt)

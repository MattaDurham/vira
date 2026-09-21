"""Optional chat work remains bounded even when a dependency ignores its deadline."""
import threading
import time
from unittest import mock

from server import virachat
from tests.test_virachat import ChatBase, REAL_THREAD


class EnrichmentCapacity(ChatBase):
    def _completed_answer(self, number):
        session = virachat.new()
        turn = {"id": f"turn_{number}", "question": f"Question {number}",
                "status": "pending", "answer": "", "sent_t": time.time()}
        virachat._mutate(lambda state: state["sessions"][session["id"]]["turns"].append(turn))
        self.assertTrue(virachat._finish_turn(
            session["id"], 0, f"Answer {number}", "", [], [], [], [],
            turn_key=turn["id"], enrich=True))
        return session["id"], turn["id"]

    def _assert_capacity_survives_timeout(self, blocking_stage):
        # Reserve all answers before starting real workers. No session/provider
        # is launched, and every possible source/model operation is replaced.
        answers = [self._completed_answer(i) for i in range(5)]
        entered, release = threading.Event(), threading.Event()
        count_lock = threading.Lock()
        calls, workers, timers = [], [], []

        def blocked(*args, **kwargs):
            with count_lock:
                calls.append(True)
                if len(calls) == 2:
                    entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release optional work")
            return ([], []) if blocking_stage == "_concepts" else []

        def worker(*args, **kwargs):
            thread = REAL_THREAD(*args, **kwargs)
            workers.append(thread)
            return thread

        def timer(delay, callback, args=(), kwargs=None):
            # Fire the real expiry callback deterministically while source or
            # concept calls remain blocked on real threading events.
            handle = mock.Mock()
            timers.append((callback, args, kwargs or {}, handle))
            return handle

        with mock.patch.object(virachat.threading, "Thread", side_effect=worker), \
                mock.patch.object(virachat.threading, "Timer", side_effect=timer), \
                mock.patch.object(virachat, "looked_at", return_value=[]) as looked, \
                mock.patch.object(virachat, "citations", return_value=[]), \
                mock.patch.object(virachat, "_concepts", return_value=([], [])) as concepts:
            (concepts if blocking_stage == "_concepts" else looked).side_effect = blocked
            try:
                for sid, key in answers[:2]:
                    virachat._start_enrichment(sid, key, {})
                self.assertTrue(entered.wait(2), "both optional workers must reach the barrier")
                self.assertEqual(len(workers), 2)
                self.assertTrue(all(thread.is_alive() for thread in workers))

                # Expiry makes metadata terminal, but cannot cancel the actual
                # dependency call and must not release its concurrency slot.
                def expire(state):
                    for sid, key in answers[:2]:
                        virachat._find_turn(state, sid, key)[1]["enrichment"]["deadline_t"] = time.time() - 1
                virachat._mutate(expire)
                for callback, args, kwargs, _handle in timers:
                    callback(*args, **kwargs)
                for sid, _key in answers[:2]:
                    turn = virachat.current(sid)["turns"][0]
                    self.assertEqual(turn["status"], "done")
                    self.assertEqual(turn["enrichment"]["error"], "enrichment timed out")
                self.assertTrue(all(thread.is_alive() for thread in workers))

                for index, (sid, key) in enumerate(answers[2:4], start=2):
                    virachat._start_enrichment(sid, key, {})
                    turn = virachat.current(sid)["turns"][0]
                    self.assertEqual(turn["status"], "done")
                    self.assertEqual(turn["answer"], f"Answer {index}")
                    self.assertEqual(turn["enrichment"]["status"], "failed")
                    self.assertIn("workers are busy", turn["enrichment"]["error"])
                self.assertEqual(len(workers), 2, "a timeout must not admit another blocked worker")
                self.assertEqual(len(timers), 2)

                release.set()
                for thread in workers:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                # The late completions cannot turn expired metadata back into
                # success. Once they return, the next answer can use a slot.
                for sid, _key in answers[:2]:
                    self.assertEqual(virachat.current(sid)["turns"][0]["enrichment"]["status"], "failed")
                sid, key = answers[4]
                virachat._start_enrichment(sid, key, {})
                workers[-1].join(2)
                self.assertFalse(workers[-1].is_alive())
                self.assertEqual(len(workers), 3)
                self.assertEqual(len(calls), 3)
                turn = virachat.current(sid)["turns"][0]
                self.assertEqual(turn["status"], "done")
                self.assertEqual(turn["answer"], "Answer 4")
                self.assertEqual(turn["enrichment"]["status"], "done")
            finally:
                release.set()
                for thread in workers:
                    thread.join(2)

    def test_blocked_source_lookups_keep_slots_until_they_return(self):
        self._assert_capacity_survives_timeout("looked_at")

    def test_blocked_concept_completion_keeps_slots_until_it_returns(self):
        self._assert_capacity_survives_timeout("_concepts")

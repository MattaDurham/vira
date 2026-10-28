"""The source-health sentinel: a zero against a busy baseline is labelled
broken, everywhere an answer from that source surfaces, and one fix-it item
is filed per breakage.

Every probe is synthetic and the store, the ideas backlog and the clock are
pinned, so no case reads the real calendar, chat.db, mail archive or vault.
"""
import asyncio
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import ideas, settings, sourcehealth, viratools


class _Clock:
    """A settable clock for sourcehealth._now."""

    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def days(self, n):
        self.t = self.t + dt.timedelta(days=n)


def _probe(key, kind, mode, value=None, error=None, absent=None):
    label = {"imessage": "iMessage"}.get(key, key.title())
    return lambda: [sourcehealth._reading(key, kind, label, mode,
                                          value=value, error=error,
                                          absent=absent)]


class SentinelCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        # Noon local time, relative to now, so the weekday arithmetic is
        # whatever today is and the dates never straddle midnight.
        start = dt.datetime.now().astimezone().replace(
            hour=12, minute=0, second=0, microsecond=0) - dt.timedelta(days=70)
        self.clock = _Clock(start)
        for p in (mock.patch.object(sourcehealth, "STORE", root / "sh.json"),
                  mock.patch.object(sourcehealth, "_now", self.clock),
                  mock.patch.object(ideas, "STORE", root / "ideas.json")):
            p.start()
            self.addCleanup(p.stop)

    def run_days(self, n, probe):
        for _ in range(n):
            out = sourcehealth.check(probes=[probe])
            self.clock.days(1)
        return out

    def entry(self, key):
        return next(r for r in sourcehealth.status()["sources"]
                    if r["key"] == key)


class DailyBaseline(SentinelCase):
    def test_learning_until_enough_days_then_ok(self):
        self.run_days(3, _probe("imessage", "imessage", "daily", value=40))
        self.assertEqual(self.entry("imessage")["state"], "learning")
        self.run_days(5, _probe("imessage", "imessage", "daily", value=40))
        self.assertEqual(self.entry("imessage")["state"], "ok")

    def test_zero_against_busy_baseline_is_broken_with_since(self):
        self.run_days(7, _probe("imessage", "imessage", "daily", value=40))
        broke_at = self.clock.t
        out = sourcehealth.check(probes=[_probe("imessage", "imessage",
                                                "daily", value=0)])
        e = self.entry("imessage")
        self.assertEqual(e["state"], "broken")
        self.assertEqual(e["since"], sourcehealth._iso(broke_at))
        self.assertIn("looks broken since", e["label_text"])
        self.assertIn("usually 40", e["detail"])
        self.assertEqual([r["key"] for r in out["broken"]], ["imessage"])

    def test_since_holds_across_broken_days_and_baseline_does_not_learn_zero(self):
        self.run_days(7, _probe("imessage", "imessage", "daily", value=40))
        first = self.clock.t
        # Long enough that zeros would outvote the 40s if they were learned.
        self.run_days(10, _probe("imessage", "imessage", "daily", value=0))
        e = self.entry("imessage")
        self.assertEqual(e["state"], "broken")
        self.assertEqual(e["since"], sourcehealth._iso(first))
        self.assertEqual(e["baseline"], 40)

    def test_quiet_source_is_never_broken(self):
        self.run_days(10, _probe("imessage", "imessage", "daily", value=1))
        sourcehealth.check(probes=[_probe("imessage", "imessage", "daily",
                                          value=0)])
        self.assertNotEqual(self.entry("imessage")["state"], "broken")

    def test_recovery_clears_the_label(self):
        self.run_days(7, _probe("imessage", "imessage", "daily", value=40))
        self.run_days(2, _probe("imessage", "imessage", "daily", value=0))
        sourcehealth.check(probes=[_probe("imessage", "imessage", "daily",
                                          value=12)])
        e = self.entry("imessage")
        self.assertEqual(e["state"], "ok")
        self.assertIsNone(e["since"])
        self.assertTrue(e["recovered_at"])
        self.assertEqual(e["label_text"], "")


class WeekdayBaseline(SentinelCase):
    def test_calendar_compares_like_weekdays_only(self):
        # Busy on one weekday, empty on the other six, for four weeks.
        busy_wd = (self.clock.t + dt.timedelta(days=28)).weekday()

        def cal():
            n = 6 if self.clock.t.weekday() == busy_wd else 0
            return [sourcehealth._reading("calendar:local", "calendar",
                                          "Local calendar", "weekday", value=n)]
        self.run_days(28, cal)
        # An ordinary empty weekend-style day stays healthy.
        self.assertNotEqual(self.entry("calendar:local")["state"], "broken")
        self.assertEqual(self.clock.t.weekday(), busy_wd)
        sourcehealth.check(probes=[_probe("calendar:local", "calendar",
                                          "weekday", value=0)])
        e = self.entry("calendar:local")
        self.assertEqual(e["state"], "broken")
        self.assertIn(self.clock.t.strftime("%A"), e["detail"])


class UnreadableSources(SentinelCase):
    def test_absent_on_a_fresh_machine_is_unavailable_not_broken(self):
        sourcehealth.check(probes=[_probe("imessage", "imessage", "daily",
                                          absent="no Messages database")])
        self.assertEqual(self.entry("imessage")["state"], "unavailable")
        self.assertEqual(sourcehealth.status()["broken"], [])

    def test_unreadable_after_working_is_broken(self):
        self.run_days(2, _probe("imessage", "imessage", "daily", value=40))
        sourcehealth.check(probes=[_probe("imessage", "imessage", "daily",
                                          error="chat.db unreadable")])
        e = self.entry("imessage")
        self.assertEqual(e["state"], "broken")
        self.assertIn("chat.db unreadable", e["detail"])

    def test_a_crashing_probe_is_one_error_not_a_dead_check(self):
        def boom():
            raise RuntimeError("store moved")
        out = sourcehealth.check(probes=[boom, _probe(
            "imessage", "imessage", "daily", value=3)])
        keys = {r["key"] for r in out["sources"]}
        self.assertEqual(keys, {"boom", "imessage"})


class VaultHits(SentinelCase):
    def test_every_search_empty_against_a_hitting_baseline(self):
        probe = _probe("vault", "vault", "daily", value=900)
        for _ in range(6):
            for found in (True, True, False, True, True):
                sourcehealth.note_vault_search(found)
            sourcehealth.check(probes=[probe])
            self.clock.days(1)
        for _ in range(5):
            sourcehealth.note_vault_search(False)
        sourcehealth.check(probes=[probe])
        e = self.entry("vault")
        self.assertEqual(e["state"], "broken")
        self.assertIn("vault searches today found nothing", e["detail"])

    def test_few_empty_searches_are_not_a_verdict(self):
        probe = _probe("vault", "vault", "daily", value=900)
        for _ in range(6):
            for _i in range(5):
                sourcehealth.note_vault_search(True)
            sourcehealth.check(probes=[probe])
            self.clock.days(1)
        sourcehealth.note_vault_search(False)
        sourcehealth.check(probes=[probe])
        self.assertEqual(self.entry("vault")["state"], "ok")


class FixItItems(SentinelCase):
    def test_one_item_per_breakage_even_when_it_flaps(self):
        self.run_days(7, _probe("imessage", "imessage", "daily", value=40))
        self.run_days(1, _probe("imessage", "imessage", "daily", value=0))
        self.run_days(1, _probe("imessage", "imessage", "daily", value=30))
        self.run_days(1, _probe("imessage", "imessage", "daily", value=0))
        filed = [i for i in ideas.list_items() if i["source"] == "source-health"]
        self.assertEqual(len(filed), 1)
        self.assertEqual(filed[0]["status"], "proposed")
        self.assertIn("looks broken since", filed[0]["text"])
        self.assertEqual(self.entry("imessage")["idea_id"], filed[0]["id"])

    def test_a_closed_item_lets_the_next_breakage_file_again(self):
        self.run_days(7, _probe("imessage", "imessage", "daily", value=40))
        self.run_days(1, _probe("imessage", "imessage", "daily", value=0))
        first = self.entry("imessage")["idea_id"]
        ideas.update(first, status="done")
        self.run_days(1, _probe("imessage", "imessage", "daily", value=30))
        self.run_days(1, _probe("imessage", "imessage", "daily", value=0))
        filed = [i for i in ideas.list_items() if i["source"] == "source-health"]
        self.assertEqual(len(filed), 2)


class Surfaces(SentinelCase):
    def break_imessage(self):
        self.run_days(7, _probe("imessage", "imessage", "daily", value=40))
        sourcehealth.check(probes=[_probe("imessage", "imessage", "daily",
                                          value=0)])

    def test_tool_notice_only_for_tools_drawn_from_the_source(self):
        self.break_imessage()
        for tool in ("imessage_thread", "mcp__vira__thread_read", "find",
                     "daily_brief"):
            self.assertIn("iMessage source looks broken since",
                          sourcehealth.tool_notice(tool), tool)
        for tool in ("calendar", "vault_search", "crm_lookup", "list_ideas"):
            self.assertEqual(sourcehealth.tool_notice(tool), "", tool)

    def test_invoke_leads_the_answer_with_the_label(self):
        self.break_imessage()

        async def fake(args):
            return {"content": [{"type": "text", "text": "No matches."}]}
        specs = [(n, d, s, fake if n == "message_context" else h)
                 for n, d, s, h in viratools.TOOL_SPECS]
        with mock.patch.object(viratools, "TOOL_SPECS", specs), \
                mock.patch("server.answer_sources._require"):
            out = asyncio.run(viratools.invoke("message_context", {"rowid": 1}))
        self.assertIn("SOURCE HEALTH WARNING", out["content"][0]["text"])
        self.assertEqual(out["content"][-1]["text"], "No matches.")

    def test_healthy_sources_leave_answers_untouched(self):
        self.run_days(7, _probe("imessage", "imessage", "daily", value=40))

        async def fake(args):
            return {"content": [{"type": "text", "text": "No matches."}]}
        specs = [(n, d, s, fake if n == "message_context" else h)
                 for n, d, s, h in viratools.TOOL_SPECS]
        with mock.patch.object(viratools, "TOOL_SPECS", specs), \
                mock.patch("server.answer_sources._require"):
            out = asyncio.run(viratools.invoke("message_context", {"rowid": 1}))
        self.assertEqual([c["text"] for c in out["content"]], ["No matches."])

    def test_brief_section_and_attention_row(self):
        from server import attention
        self.break_imessage()
        sec = sourcehealth.brief_section()
        self.assertEqual([b["key"] for b in sec["broken"]], ["imessage"])
        rows = sourcehealth.attention_rows(attention._row)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "health:source:imessage")
        self.assertTrue(rows[0]["needs_you"])
        self.assertIn("looks broken", rows[0]["title"])

    def test_brief_compose_carries_the_section(self):
        from server import brief
        self.break_imessage()
        with mock.patch.object(settings, "fixture_mode", return_value=False):
            sec = brief._source_health()
        self.assertEqual(sec["broken"][0]["kind"], "imessage")


if __name__ == "__main__":
    unittest.main()

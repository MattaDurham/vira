"""The two daily budgets in notify.py.

Agent pings (job-board finds, routine outcomes, circuit finishes) and the
human-facing notifications (a contact emailed, a renewal is due) used to
share ONE daily count. Measured on the live log over 2026-09-03..10: the
agent pings filled all 20 slots on two of eight days, so `maybe_notify`
silently dropped every email from an active contact on exactly the days
Vira was busiest. These cases pin the split - each side has its own
budget and neither can spend the other's.

The clock is frozen (the FrozenClockCase rule) and the log is rooted at a
tmp file, so nothing here reads or writes the real data/ directory.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from server import notify

TODAY = datetime(2026, 9, 10, 12, 0, 0)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(TODAY.year, TODAY.month, TODAY.day, 12, 0, 0, tzinfo=tz)


def _entry(person_id, channel, minutes_ago, ok=True):
    at = (TODAY - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    return {"at": at, "person_id": person_id, "person_name": "x",
            "channel": channel, "text": "t", "ref": None, "ok": ok}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "notify-log.json"
        for p in (mock.patch.object(notify, "LOG", self.log),
                  mock.patch.object(notify, "datetime", _FrozenDateTime),
                  mock.patch.object(notify.time, "time",
                                    lambda: TODAY.timestamp())):
            p.start()
            self.addCleanup(p.stop)

    def write(self, entries):
        self.log.write_text(json.dumps({"sent": entries}), encoding="utf-8")


class TwoBudgets(Base):
    def test_a_day_of_agent_pings_never_blocks_a_human_notification(self):
        # The incident shape: the agent budget is spent, minutes apart.
        self.write([_entry(f"agent:jobboards:{i}", "agent", 300 - i * 10)
                    for i in range(notify.AGENT_DAILY_CAP)])
        self.assertEqual(notify._throttled("agent:jobboards:new"),
                         "daily cap reached")
        self.assertIsNone(notify._throttled("p_someone"))

    def test_a_day_of_human_notifications_never_blocks_an_agent_ping(self):
        self.write([_entry(f"p_{i}", "email", 300 - i * 10)
                    for i in range(notify.DAILY_CAP)])
        self.assertEqual(notify._throttled("p_new"), "daily cap reached")
        self.assertIsNone(notify._throttled("agent:routine:muse"))

    def test_the_agent_budget_is_read_off_channel_or_key(self):
        # Legacy rows may carry the key prefix without the channel, or the
        # channel without the prefix; both count against the agent side.
        rows = [_entry(f"agent:k{i}", "notify", 200 - i) for i in range(10)]
        rows += [_entry(f"k{i}", "agent", 100 - i) for i in range(10)]
        self.write(rows)
        self.assertEqual(notify._throttled("agent:another"),
                         "daily cap reached")
        self.assertIsNone(notify._throttled("p_human"))

    def test_yesterday_never_counts(self):
        day_ago = 24 * 60 + 30
        self.write([_entry(f"agent:k{i}", "agent", day_ago + i)
                    for i in range(notify.AGENT_DAILY_CAP)])
        self.assertIsNone(notify._throttled("agent:fresh"))

    def test_failed_sends_never_count(self):
        self.write([_entry(f"agent:k{i}", "agent", 60 + i, ok=False)
                    for i in range(notify.AGENT_DAILY_CAP)])
        self.assertIsNone(notify._throttled("agent:fresh"))

    def test_the_sender_cooldown_is_unchanged(self):
        self.write([_entry("p_katie", "email", 30)])
        self.assertEqual(notify._throttled("p_katie"), "sender cooldown")
        self.assertIsNone(notify._throttled("p_other"))
        self.write([_entry("p_katie", "email", 7 * 60)])
        self.assertIsNone(notify._throttled("p_katie"))


class Isolation(Base):
    def test_the_fixture_isolates_the_real_log(self):
        real = Path(__file__).resolve().parent.parent / "data" / "notify-log.json"
        before = real.stat().st_mtime_ns if real.exists() else None
        self.write([_entry("agent:k", "agent", 5)])
        notify._throttled("agent:k")
        notify._throttled("p_x")
        after = real.stat().st_mtime_ns if real.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

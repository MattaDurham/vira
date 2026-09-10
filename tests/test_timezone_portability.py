"""Exercise real timezone rules without the host's IANA database or cache."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class TimezonePortability(unittest.TestCase):
    def test_assistant_dates_work_without_a_system_timezone_database(self):
        # A fresh process with an empty TZPATH reproduces stock Windows on
        # macOS/Linux too. It must use the declared tzdata runtime dependency.
        script = textwrap.dedent("""\
            import datetime as dt
            from unittest import mock
            import zoneinfo
            from server import calendarplan, contactintel

            assert zoneinfo.TZPATH == (), zoneinfo.TZPATH
            assert zoneinfo.ZoneInfo("UTC").utcoffset(None) == dt.timedelta(0)
            zone = zoneinfo.ZoneInfo("America/New_York")
            assert dt.datetime(2030, 1, 15, tzinfo=zone).utcoffset() == dt.timedelta(hours=-5)
            assert dt.datetime(2030, 7, 15, tzinfo=zone).utcoffset() == dt.timedelta(hours=-4)

            draft = {
                "time_quote": "2030-09-11 from 10:00 to 11:00",
                "source": {"when": "2030-09-11T01:00:00+00:00"},
                "start": "2030-09-11T10:00:00-04:00",
                "end": "2030-09-11T11:00:00-04:00",
            }
            with mock.patch.object(calendarplan.settings, "raw", return_value={
                    "assistant_timezone": "America/New_York"}):
                assert calendarplan._grounded(draft) == ""
                draft["start"] = "2030-09-11T10:00:00-05:00"
                draft["end"] = "2030-09-11T11:00:00-05:00"
                assert "UTC offsets" in calendarplan._grounded(draft)

            source = {"id": "synthetic-mail", "text": "Send the report tomorrow.",
                      "when": "2030-09-11T01:00:00+00:00", "channel": "email"}
            with mock.patch.object(contactintel, "_cfg", return_value="America/New_York"):
                due, evidence = contactintel._deadline(
                    "2030-09-11", "tomorrow", [{"id": source["id"]}],
                    {source["id"]: source})
            assert due == "2030-09-11"
            assert evidence["id"] == source["id"]
            print("timezone portability verified")
            """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONTZPATH": ""},
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "timezone portability verified")


if __name__ == "__main__":
    unittest.main()

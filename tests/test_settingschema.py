"""The one settings table: every key declared once, every writer through
settings.save, the public template generated from the table."""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from server import (correspondence, executive, notify, onboard, settings,
                    settingschema, suggest, workers)

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"


class TheTable(unittest.TestCase):
    def test_every_key_has_a_group_a_type_and_words(self):
        for s in settingschema.SCHEMA:
            with self.subTest(key=s.key):
                self.assertIn(s.group, settingschema.GROUP_TITLES)
                self.assertIn(s.type, settingschema.TYPES)
                self.assertTrue(s.label and not s.label.endswith("."), "label is a short name")
                if s.type == "enum":
                    self.assertIn(s.default, s.choices)
                if s.range is not None:
                    self.assertEqual(len(s.range), 2)
                if s.default is None:
                    self.assertTrue(s.nullable, "a None default must be nullable")
                elif s.type in ("int",):
                    self.assertIs(type(s.default), int)
                elif s.type == "bool":
                    self.assertIs(type(s.default), bool)

    def test_derived_default_tables_agree(self):
        self.assertIs(settings.DEFAULTS, settingschema.DEFAULTS)
        for key, value in executive.DEFAULT_CONFIG.items():
            self.assertEqual(settingschema.DEFAULTS[key], value, key)
        for key, value in suggest.DEFAULTS.items():
            self.assertEqual(settingschema.DEFAULTS[key], value, key)
        # The template once said "interactive" while the code shipped
        # bypassPermissions; one table, one answer.
        from server import session
        for key, value in session.SESSION_DEFAULTS.items():
            self.assertEqual(settingschema.DEFAULTS[key], value, key)

    def test_every_key_read_from_config_is_declared(self):
        """A literal key in settings.get / settings.raw().get / a module's
        _cfg wrapper must be in the table, or a fresh install KeyErrors
        (this has broken startup twice)."""
        pattern = re.compile(
            r"(?:settings\.get|settings\.raw\(\)\.get|raw\(\)\.get|_cfg|_cfg_bool|_cfg_num)"
            r"\(\s*\"([a-z0-9_]+)\"")
        found = set()
        for path in SERVER.glob("*.py"):
            if path.name == "settingschema.py":
                continue
            found |= set(pattern.findall(path.read_text(encoding="utf-8")))
        undeclared = sorted(found - set(settingschema.BY_KEY))
        self.assertEqual(undeclared, [], "declare these in server/settingschema.py")

    def test_template_is_generated_from_the_table(self):
        self.assertTrue(settingschema.example_in_sync(),
                        "run: python -m server.settingschema example --write")
        doc = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        for s in settingschema.SCHEMA:
            self.assertIn(s.key, doc)
            self.assertEqual(doc[s.key], s.default)
            if s.help:
                self.assertEqual(doc["_" + s.key], s.help)

    def test_describe_reports_stored_default_and_advanced(self):
        rows = {r["key"]: r for r in settingschema.describe(
            {"owner_name": "Sam", "assistant_timezone": ""})}
        self.assertEqual(rows["owner_name"]["value"], "Sam")
        self.assertTrue(rows["owner_name"]["set"])
        self.assertEqual(rows["assistant_timezone"]["value"], "")
        self.assertFalse(rows["assistant_debounce_s"]["set"])
        self.assertTrue(rows["assistant_debounce_s"]["advanced"])
        self.assertEqual(settingschema.keys("assistant", advanced=False),
                         ["assistant_enabled", "assistant_catchup_days",
                          "assistant_stale_days", "assistant_due_soon_hours",
                          "coverage_enabled", "coverage_calendar_blocks",
                          "coverage_lookahead_days"])
        self.assertNotIn("assistant_calendar_id",
                         settingschema.keys("calendar", editable=True))
        groups = settingschema.payload({})["groups"]
        self.assertEqual([g["id"] for g in groups][:3], ["you", "assistant", "notifications"])


class Validation(unittest.TestCase):
    def test_types_ranges_choices_and_zones(self):
        v = settingschema.validate
        self.assertIs(v("assistant_enabled", True), True)
        for key, bad in (("assistant_enabled", "yes"), ("assistant_enabled", 1),
                         ("assistant_quiet_start", 24), ("assistant_quiet_start", True),
                         ("assistant_quiet_start", "9"), ("subs_notify_threshold_usd", "100"),
                         ("ai_backend", "cloud"), ("assistant_timezone", "Missing/Zone"),
                         ("family_calendars", "Family"), ("family_calendars", [1]),
                         ("vault_primary", "x"), ("owner_name", None),
                         ("owner_name", "x" * 1001), ("no_such_key", 1)):
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                v(key, bad)
        self.assertEqual(v("assistant_timezone", "UTC"), "UTC")
        self.assertEqual(v("assistant_timezone", ""), "")
        self.assertEqual(v("family_calendars", [" Family ", "", "Kids"]), ["Family", "Kids"])
        self.assertEqual(v("subs_notify_threshold_usd", 25), 25)
        self.assertIsNone(v("fixture_mode", None))
        self.assertEqual(v("crm_root", "  ~/x "), "~/x")

    def test_validate_many_rejects_the_whole_batch(self):
        with self.assertRaises(ValueError):
            settingschema.validate_many({"owner_name": "ok", "assistant_quiet_end": 99})
        with self.assertRaises(ValueError):
            settingschema.validate_many(["owner_name"])


class TheOneWriter(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.path.write_text(json.dumps({"unrelated": "keep", "owner_name": "Sam"}),
                             encoding="utf-8")
        for target in (settings, suggest):
            p = mock.patch.object(target, "CONFIG_PATH", self.path)
            p.start()
            self.addCleanup(p.stop)

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_save_validates_before_writing_and_keeps_other_keys(self):
        with self.assertRaises(ValueError):
            settings.save({"owner_name": "Kim", "assistant_quiet_end": 99})
        self.assertEqual(self.stored()["owner_name"], "Sam", "nothing lands on a bad batch")
        settings.save({"owner_name": "Kim", "family_calendars": ["Family"]})
        self.assertEqual(self.stored(), {"unrelated": "keep", "owner_name": "Kim",
                                         "family_calendars": ["Family"]})

    def test_validate_hook_runs_on_the_stored_document(self):
        seen = {}
        settings.save({"owner_name": "Kim"}, validate=lambda cfg: seen.update(cfg))
        self.assertEqual(seen["owner_name"], "Sam", "the hook sees what is on disk")
        with self.assertRaises(KeyError):
            settings.save({}, validate=lambda cfg: (_ for _ in ()).throw(KeyError("x")))
        self.assertEqual(self.stored()["owner_name"], "Kim")

    def test_every_feature_writer_lands_in_the_same_file(self):
        notify.save_config({"enabled": False, "handle": " +15550100 "})
        executive.save_config({"assistant_notify": True, "assistant_timezone": "UTC"})
        suggest.save_config({"timeout": 99, "not_an_ai_key": 1})
        onboard.config_set(owner_name="Lee")
        with mock.patch.object(correspondence, "_guard"):
            correspondence.save_config({"enabled": True})
        stored = self.stored()
        self.assertEqual(stored["notify_enabled"], False)
        self.assertEqual(stored["notify_handle"], "+15550100")
        self.assertTrue(stored["assistant_notify"])
        self.assertEqual(stored["timeout"], 99)
        self.assertNotIn("not_an_ai_key", stored)
        self.assertEqual(stored["owner_name"], "Lee")
        self.assertTrue(stored["correspondence"]["enabled"])
        self.assertEqual(stored["unrelated"], "keep")

    def test_concurrent_saves_never_lose_each_other(self):
        """notify.save_config once rewrote the file outside the shared lock;
        two forms saving in the same second could drop a key."""
        errors = []

        def hammer(i):
            try:
                for n in range(20):
                    if i % 2:
                        notify.save_config({"handle": f"+1555010{n:04d}"})
                    else:
                        settings.save({"assistant_stale_days": 1 + n % 5})
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        stored = self.stored()
        self.assertEqual(stored["unrelated"], "keep")
        self.assertIn("notify_handle", stored)
        self.assertIn("assistant_stale_days", stored)


class Workers(unittest.TestCase):
    def test_every_worker_reports_even_when_a_read_fails(self):
        with mock.patch("server.jobboards.health", side_effect=OSError("boom")), \
                mock.patch("server.textindex.status", return_value={"available": False}), \
                mock.patch("server.mediaarchive.stats", return_value={"enabled": True, "available": False}), \
                mock.patch("server.mailarchive.stats", return_value={"enabled": True, "available": False}), \
                mock.patch("server.aihealth.summary", return_value={"state": "ok", "checked_at": "2026-10-13T09:00:00"}), \
                mock.patch("server.contactintel.status", return_value={"pending_contacts": 2}), \
                mock.patch.object(executive, "config", return_value=dict(executive.DEFAULT_CONFIG)), \
                mock.patch.object(correspondence, "config", return_value=dict(correspondence.DEFAULTS)), \
                mock.patch.object(correspondence, "_state", return_value={"items": {}, "last_run": "x"}), \
                mock.patch.object(settings, "get", side_effect=lambda k: settingschema.DEFAULTS[k]):
            out = workers.status("idle - last sweep 2026-10-01T00:00")
        rows = {w["id"]: w for w in out["workers"]}
        self.assertEqual(list(rows), ["assistant", "intake", "textindex", "media_archive", "mail_archive",
                                      "ai_health", "receipts", "boards"])
        self.assertIn("status unavailable", rows["boards"]["last_error"])
        self.assertEqual(rows["receipts"]["last_run"], "2026-10-01T00:00")
        self.assertEqual(rows["assistant"]["detail"], "2 contact(s) queued")
        self.assertEqual(rows["ai_health"]["last_run"], "2026-10-13T09:00:00")
        for row in rows.values():
            self.assertIn("enabled_key", row)


if __name__ == "__main__":
    unittest.main()

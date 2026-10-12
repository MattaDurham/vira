"""The owner's writing voice: what is measured from one sent message, how a
synthetic text index becomes per-channel / per-class / per-person buckets,
which bucket a draft gets, and that the drafting prompts actually carry it.

Everything is synthetic: an in-memory index with the real `items` columns,
a two-person CRM map, a temp store. Nothing reads the real machine.

Run: .venv/bin/python -m unittest tests.test_voice
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import groupchat, settings, suggest, voice

PROFILES = {
    "p_ann": {"name": "Ann Reyes", "relationship_class": "Family"},
    "p_raj": {"name": "Raj Patel", "relationship_class": "business"},
    "p_lee": {"name": "Lee Park", "relationship_class": "business"},
}


def _index(rows):
    """An in-memory text index holding `rows` (newest last)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE items(seq INTEGER PRIMARY KEY, uid TEXT,
        source TEXT, account TEXT, chat_id INTEGER, is_group INTEGER,
        from_me INTEGER, sender_pid TEXT, chat_pid TEXT, sender_handle TEXT,
        date_ns INTEGER, subject TEXT, text TEXT, source_meta TEXT,
        pending INTEGER DEFAULT 1)""")
    for i, r in enumerate(rows):
        con.execute(
            "INSERT INTO items(source, chat_id, is_group, from_me, chat_pid,"
            " date_ns, subject, text) VALUES(?,?,?,?,?,?,?,?)",
            (r.get("source", "imessage"), r.get("chat_id"),
             r.get("is_group", 0), r.get("from_me", 1), r.get("chat_pid"),
             i, r.get("subject", ""), r["text"]))
    return con


def _corpus():
    rows = []
    # Ann: casual family texts, chat_pid missing (indexed before she was in
    # the CRM), so only the chat.db map can attribute them.
    for i in range(10):
        rows.append({"chat_id": 7, "text": f"ok see u at {i} lol"})
    # Raj: business email with a greeting, a sign-off and quoted history.
    for i in range(14):
        rows.append({"source": "email", "chat_pid": "p_raj",
                     "subject": "Q3 numbers",
                     "text": f"Hi Raj, Thanks for the update on item {i}. "
                             "I will review the deck tonight. Best regards, M "
                             "On Mon, Oct 5, 2026 Raj Patel wrote: old text"})
    rows.append({"source": "email", "chat_pid": "p_raj",
                 "subject": "Accepted: Q3 review", "text": "Accepted"})
    # Group chat banter, its own register.
    for i in range(13):
        rows.append({"is_group": 1, "chat_id": 9, "text": "haha yes"})
    # Messages the owner did not send never count.
    rows.append({"from_me": 0, "chat_id": 7, "text": "Dear sir, regards"})
    return rows


class Features(unittest.TestCase):
    def test_email_greeting_signoff_and_quote_cut(self):
        f = voice.features(
            "Hi Dana, Thanks for this. Will send it Friday. Best, M "
            "On Tue, Oct 6, 2026 Dana wrote: the old thread", "email")
        self.assertEqual(f["greet"], "Hi <name>,")
        self.assertEqual(f["sign"], "Best, <signature>")
        self.assertEqual(f["sign_name"], "M")
        self.assertNotIn("old thread", f["text"])

    def test_a_trailing_word_is_not_a_signoff(self):
        f = voice.features("Hello team. Please read it. This is the best",
                           "email")
        self.assertEqual(f["greet"], "Hello team.")
        self.assertIsNone(f["sign"])

    def test_body_word_after_greeting_is_not_a_name(self):
        self.assertEqual(voice.features("Hey Thanks so much", "imessage")
                         ["greet"], "Hey")

    def test_recipient_name_alone_is_a_greeting(self):
        self.assertEqual(voice.features("Raj, can you send it", "email",
                                        "Raj")["greet"], "<name>,")
        self.assertIsNone(voice.features("Awesome, thanks", "email",
                                         "Raj")["greet"])

    def test_tapbacks_and_bare_links_are_skipped(self):
        self.assertIsNone(voice.features("Liked “nice”", "imessage"))
        self.assertIsNone(voice.features("https://example.com/x", "imessage"))

    def test_casual_mechanics(self):
        f = voice.features("lol ok i'm in", "imessage")
        self.assertTrue(f["lower_start"])
        self.assertTrue(f["end_bare"])
        self.assertTrue(f["contractions"])
        self.assertLess(f["formality"], 0)


class Build(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "voice.json"
        mock.patch.object(voice, "STORE", self.store).start()
        self.addCleanup(mock.patch.stopall)

    def _build(self, rows=None, mail_on=True):
        real_get = settings.get
        with mock.patch.object(settings, "get", side_effect=lambda k, *a:
                               mail_on if k == "mail_body_index"
                               else real_get(k, *a)):
            return voice.build(con=_index(_corpus() if rows is None else rows),
                               profiles=PROFILES, chat_people={7: "p_ann"})

    def test_buckets_per_channel_class_and_person(self):
        out = self._build()
        self.assertEqual(out["samples"], {"imessage": 10, "email": 14,
                                          "group": 13})
        # chat.db attributed Ann's rows even though the index had no pid
        self.assertEqual(out["people"]["p_ann"]["imessage"]["n"], 10)
        self.assertEqual(out["people"]["p_raj"]["email"]["n"], 14)
        email = out["channels"]["email"]["overall"]
        self.assertEqual(email["greetings"][0]["form"], "Hi <name>,")
        self.assertEqual(email["signoffs"][0]["form"],
                         "Best regards, <signature>")
        self.assertEqual(email["sign_name"], "M")
        self.assertEqual(email["register"], "formal")
        self.assertEqual(out["channels"]["imessage"]["overall"]["register"],
                         "casual")
        # classes need MIN_CLASS samples: business email (14) yes, family (10) no
        self.assertIn("business", out["channels"]["email"]["by_class"])
        self.assertNotIn("family", out["channels"]["imessage"]["by_class"])
        # the group register is learned apart from the 1:1 texts
        self.assertEqual(out["channels"]["group"]["overall"]["shorthand"][0]
                         ["form"], "haha")
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8"))
                         ["samples"]["group"], 13)

    def test_empty_mail_names_the_setting(self):
        out = self._build(rows=[{"text": "ok"}], mail_on=False)
        self.assertIn("mail_body_index",
                      out["channels"]["email"]["overall"]["reason"])


class Pick(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mock.patch.object(voice, "STORE", Path(tmp.name) / "v.json").start()
        mock.patch("server.data.profiles_map", return_value=PROFILES).start()
        self.addCleanup(mock.patch.stopall)
        self.store = voice.build(con=_index(_corpus()), profiles=PROFILES,
                                 chat_people={7: "p_ann"})
        mock.patch.object(voice, "load", return_value=self.store).start()

    def test_person_then_class_then_overall(self):
        self.assertEqual(voice.pick(self.store, "p_raj", "email")[1], "person")
        self.assertEqual(voice.pick(self.store, "p_lee", "email")[1],
                         "class:business")
        self.assertEqual(voice.pick(self.store, "p_unknown", "group")[1],
                         "overall")
        # Ann has only 10 texts and family has no class bucket; 1:1 overall
        # is below MIN_CHANNEL too, so there is no voice to claim
        self.assertEqual(voice.pick(self.store, "p_none", "imessage"),
                         (None, None))

    def test_block_carries_habits_and_examples(self):
        txt = voice.block("p_raj", "email", owner="Sam", name="Raj Patel")
        self.assertIn("email to Raj Patel", txt)
        self.assertIn('"Best regards, <signature>"', txt)
        self.assertIn('signs as "M"', txt)
        self.assertIn("  > Hi Raj, Thanks for the update", txt)

    def test_block_is_empty_without_a_voice(self):
        with mock.patch.object(voice, "load",
                               return_value={"status": "unavailable"}):
            self.assertEqual(voice.block("p_raj", "email"), "")


class Load(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "v.json"
        mock.patch.object(voice, "STORE", self.store).start()
        self.addCleanup(mock.patch.stopall)

    def _write(self, age_s):
        built = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        self.store.write_text(json.dumps(
            {"version": voice.VERSION, "status": "ok",
             "built_at": built.isoformat(), "channels": {}}),
            encoding="utf-8")

    def test_fresh_store_is_not_rebuilt(self):
        self._write(60)
        with mock.patch.object(voice, "build") as b:
            voice.load()
        b.assert_not_called()

    def test_stale_store_rebuilds(self):
        self._write(voice.MAX_AGE_S + 60)
        with mock.patch.object(voice, "build",
                               return_value={"status": "ok"}) as b:
            self.assertEqual(voice.load(), {"status": "ok"})
        b.assert_called_once()

    def test_empty_email_relearns_sooner_once_mail_is_indexed(self):
        self._write(voice.EMPTY_MAIL_AGE_S + 60)
        real_get = settings.get
        for on, expect in ((False, 0), (True, 1)):
            with mock.patch.object(settings, "get", side_effect=lambda k, *a:
                                   on if k == "mail_body_index"
                                   else real_get(k, *a)), \
                 mock.patch.object(voice, "build",
                                   return_value={"status": "ok"}) as b:
                voice.load()
            self.assertEqual(b.call_count, expect)

    def test_failed_rebuild_keeps_old_store_marked_stale(self):
        self._write(voice.MAX_AGE_S + 60)
        with mock.patch.object(voice, "build", side_effect=OSError("locked")):
            out = voice.load()
        self.assertTrue(out["stale"])
        self.assertEqual(out["status"], "ok")
        self.assertFalse(json.loads(self.store.read_text(encoding="utf-8"))
                         .get("stale"))


class PromptsCarryTheVoice(unittest.TestCase):
    """The join: the voice reaches the prompt a draft is written from."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.calls = []
        mock.patch.object(voice, "block", side_effect=lambda pid, ch, **kw:
                          self.calls.append((pid, ch)) or
                          f"VOICE[{voice.channel_of(ch)}]").start()

    def _suggest(self, channel, mode="replies"):
        captured = {}
        detail = {"person": {"id": "p_raj", "name": "Raj Patel"},
                  "profile": PROFILES["p_raj"]}

        def run(prompt, cfg, tools=None):
            captured["prompt"] = prompt
            return '{"suggestions": [{"text": "ok", "tone": "direct"}]}', "t"
        with mock.patch("server.data.get_person", return_value=detail), \
             mock.patch("server.imessage.thread_for_person", return_value=[]), \
             mock.patch("server.threadread.facts_block", return_value=""), \
             mock.patch.object(suggest, "config",
                               return_value={"owner_name": "Sam"}), \
             mock.patch.object(suggest, "_run", side_effect=run):
            out = suggest.suggest.__wrapped__("p_raj", channel, mode=mode) \
                if hasattr(suggest.suggest, "__wrapped__") else \
                suggest.suggest("p_raj", channel, mode=mode)
        return captured["prompt"], out

    def test_email_reply_is_voiced_from_email(self):
        prompt, out = self._suggest("email")
        self.assertIn("VOICE[email]", prompt)
        self.assertTrue(out["voice"])
        self.assertEqual(self.calls[-1], ("p_raj", "email"))

    def test_hook_opener_is_voiced_from_texts(self):
        prompt, _ = self._suggest("imessage", mode="hook")
        self.assertIn("VOICE[imessage]", prompt)

    def test_group_brief_is_voiced_from_group_chats(self):
        prof = {"label": "Ski trip",
                "group": {"participants": [{"name": "Ann Reyes"}]},
                "activity": [], "connections": {"edges": []}, "related": []}
        prompt = groupchat._brief_prompt(prof, [])
        self.assertIn("VOICE[group]", prompt)


if __name__ == "__main__":
    unittest.main()

"""Family moments, end to end on synthetic data.

The store is a temp file and every source the module reads is replaced: the
calendar, the media index, the text index, the CRM, the attachment files and
both sends. Nothing reads the machine's calendars, messages, photos or
config. Names and handles are synthetic (555-01xx block, example.com).
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import moments, reviewqueue

TODAY = dt.date(2030, 10, 31)
SOURCE = {"name": "Pat Example", "person_id": "p_pat", "handle": ""}
GRAN = {"name": "Robin Example", "person_id": "p_robin", "handle": "",
        "relation": "Grandparent", "occasions": []}
AUNT = {"name": "Sam Example", "person_id": "", "handle": "+15555550102",
        "relation": "Aunt", "occasions": ["birthday"]}
PEOPLE = {"p_pat": {"name": "Pat Example"}, "p_robin": {"name": "Robin Example"},
          "p_lee": {"name": "Lee Example"}}


def shot(att, faces=0, known=0, ocr="", kind="photo", vec=None, mime="image/jpeg"):
    return {"seq": att, "id": att, "kind": kind, "mime": mime, "name": f"IMG_{att}.jpg",
            "date_ns": att, "from_me": 0, "sender_pid": "p_pat", "chat_pid": "p_pat",
            "ocr": ocr, "caption": "", "context": "", "faces": faces, "known": known,
            "vec": vec}


def vec(*values):
    import numpy as np
    return np.array(values, dtype=np.float32).tobytes()


class MomentsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.events = [{"title": "Halloween parade", "family": False, "birthday": False}]
        self.items = [shot(1, faces=2, known=1), shot(2, faces=0, ocr="x" * 80),
                      shot(3, faces=1), shot(4, faces=3, known=2), shot(5)]
        self.msgs = []
        self.texts, self.files = [], []
        (self.root / "orig").mkdir()
        for i in range(1, 6):
            (self.root / "orig" / f"IMG_{i}.jpg").write_bytes(b"jpeg")
        moments._pending_cache.clear()
        for patcher in (
                mock.patch.object(moments, "STORE", self.root / "moments.json"),
                mock.patch.object(moments, "STAGE_DIR", self.root / "stage"),
                mock.patch.object(moments, "_today", return_value=TODAY),
                mock.patch.object(moments, "_calendar_events", side_effect=lambda day: (
                    self.events if day == TODAY else [])),
                mock.patch.object(moments, "_media_items", side_effect=lambda day, pids: (
                    list(self.items) if day == TODAY and pids else [])),
                mock.patch.object(moments, "_messages", side_effect=lambda *a, **k: list(self.msgs)),
                mock.patch.object(moments, "_people", return_value=PEOPLE),
                mock.patch.object(moments, "_index_ready", return_value=True),
                mock.patch.object(moments, "_file_for", side_effect=self.file_for),
                mock.patch.object(moments, "_send_text", side_effect=self.send_text),
                mock.patch.object(moments, "_send_file", side_effect=self.send_file)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.missing = set()

    def file_for(self, att):
        if att in self.missing:
            return None, f"IMG_{att}.HEIC"
        return self.root / "orig" / f"IMG_{att}.jpg", f"IMG_{att}.jpg"

    def send_text(self, text, pid, handle):
        self.texts.append((pid, handle, text))
        return {"ok": True, "handle": handle or "+15555550101", "channel": "imessage", "note": None}

    def send_file(self, path, pid, handle, channel):
        self.files.append((pid, Path(path).name))
        return {"handle": handle, "channel": channel}

    def setup_family(self, relay=(GRAN, AUNT)):
        moments.save_setup({"sources": [SOURCE], "relay": list(relay)})

    def moment(self):
        [m] = moments.day_view(TODAY)
        return m


class DetectTests(MomentsCase):
    def test_calendar_occasion_makes_a_moment_with_ranked_shots(self):
        self.setup_family()
        m = self.moment()
        self.assertEqual(m["title"], "Halloween parade")
        self.assertEqual(m["origin"], "calendar")
        # Most faces first; the screenshot (OCR text) last.
        self.assertEqual([s["id"] for s in m["shots"]], [4, 1, 3, 5, 2])
        self.assertEqual(m["best"], [4, 1, 3])

    def test_routine_meeting_is_not_a_moment_but_a_burst_is(self):
        self.setup_family()
        self.events = [{"title": "Team sync", "family": False, "birthday": False}]
        self.assertEqual(moments.day_view(TODAY), [])
        self.items = [shot(i) for i in range(1, moments.BURST_MIN + 1)]
        self.items[0]["context"] = "the parade was amazing"
        [m] = moments.day_view(TODAY)
        self.assertEqual((m["origin"], m["title"]), ("photos", "Parade"))

    def test_weak_word_needs_a_family_calendar(self):
        self.setup_family()
        self.events = [{"title": "School board", "family": False, "birthday": False}]
        self.assertEqual(moments.day_view(TODAY), [])
        self.events = [{"title": "School board", "family": True, "birthday": False}]
        self.assertEqual(len(moments.day_view(TODAY)), 1)

    def test_a_task_about_an_occasion_is_not_the_occasion(self):
        self.setup_family()
        self.events = [{"title": "Work on: send the Halloween parade costume", "family": True,
                        "birthday": False},
                       {"title": "Buy recital flowers", "family": False, "birthday": False}]
        self.assertEqual(moments.day_view(TODAY), [])

    def test_contact_birthday_is_not_a_family_moment(self):
        self.setup_family()
        self.events = [{"title": "Lee Example's Birthday", "family": False, "birthday": True}]
        self.assertEqual(moments.day_view(TODAY), [])

    def test_near_identical_shots_fold_into_one(self):
        self.setup_family()
        self.items = [shot(1, faces=2, vec=vec(1, 0, 0)), shot(2, faces=1, vec=vec(0.999, 0.01, 0)),
                      shot(3, vec=vec(0, 1, 0))]
        self.assertEqual([s["id"] for s in self.moment()["shots"]], [1, 3])

    def test_added_moment_on_a_quiet_day(self):
        self.setup_family()
        day = TODAY - dt.timedelta(days=1)
        moments.add_moment("Spring recital", day.isoformat())
        [m] = moments.day_view(day)
        self.assertEqual((m["origin"], m["title"], m["shots"]), ("added", "Spring recital", []))
        with self.assertRaises(ValueError):
            moments.add_moment("Later", (TODAY + dt.timedelta(days=1)).isoformat())


class RecipientTests(MomentsCase):
    def test_relay_list_filtered_by_occasion_words(self):
        self.setup_family()
        names = [r["name"] for r in self.moment()["recipients"]]
        # The aunt only wants birthdays; the source is never a recipient.
        self.assertEqual(names, ["Robin Example"])

    def test_gift_sender_suggested_with_the_message_and_a_thank_you(self):
        self.setup_family()
        self.msgs = [
            {"text": "Sent a little something for the Halloween parade!", "from_me": False,
             "person_id": "p_lee", "person": "Lee Example", "when": "2030-10-20T10:00:00"},
            {"text": "A gift for your birthday", "from_me": False,  # wrong occasion
             "person_id": "p_other", "person": "Other", "when": "2030-10-21T10:00:00"},
            {"text": "Thank you for the costume for halloween!", "from_me": True,
             "person_id": "p_robin", "person": "Robin Example", "when": "2030-10-22T10:00:00"},
            {"text": "Here's a present for the parade", "from_me": False,
             "person_id": "p_pat", "person": "Pat Example", "when": "2030-10-22T10:00:00"},
        ]
        rows = {r["name"]: r for r in self.moment()["recipients"]}
        self.assertEqual(set(rows), {"Robin Example", "Lee Example"})
        self.assertTrue(rows["Lee Example"]["gift"])
        self.assertIn("little something", rows["Lee Example"]["quote"])
        self.assertTrue(rows["Lee Example"]["text"].startswith("Lee, thank you"))
        # A relay person the owner thanked keeps one row, with the thank-you.
        self.assertTrue(rows["Robin Example"]["gift"])
        self.assertIn("Grandparent", rows["Robin Example"]["why"])

    def test_draft_names_the_moment_and_picks_the_best(self):
        self.setup_family()
        [r] = self.moment()["recipients"]
        self.assertEqual(r["text"], "Hi Robin! A few from the Halloween parade - thought you'd love these.")
        self.assertEqual(r["picks"], [4, 1, 3])

    def test_setup_validation(self):
        with self.assertRaises(ValueError):
            moments.save_setup({"sources": [{"name": "Nobody"}], "relay": []})
        clean = moments.save_setup({"sources": [SOURCE, SOURCE],
                                    "relay": [dict(GRAN, occasions="Birthday, recital")]})
        self.assertEqual(len(clean["sources"]), 1)
        self.assertEqual(clean["relay"][0]["occasions"], ["birthday", "recital"])


class SendTests(MomentsCase):
    def test_one_tap_send_delivers_text_then_picks_and_never_twice(self):
        self.setup_family()
        m = self.moment()
        moments.update_recipient(m["id"], m["date"], "p_robin", text="Look at these!", picks=[4, 5])
        res = moments.send(m["id"], m["date"], "p_robin")
        self.assertEqual(self.texts, [("p_robin", "", "Look at these!")])
        self.assertEqual([f[1] for f in self.files], [f"{m['id'][:8]}-IMG_4.jpg", f"{m['id'][:8]}-IMG_5.jpg"])
        self.assertTrue((self.root / "stage" / f"{m['id'][:8]}-IMG_4.jpg").exists())
        self.assertEqual(res["files"], 2)
        [r] = self.moment()["recipients"]
        self.assertTrue(r["sent_at"])
        with self.assertRaises(ValueError):
            moments.send(m["id"], m["date"], "p_robin")
        self.assertEqual(len(self.texts), 1)

    def test_offloaded_file_is_named_not_dropped(self):
        self.setup_family()
        m = self.moment()
        self.missing = {1}
        res = moments.send(m["id"], m["date"], "p_robin")
        self.assertEqual((res["files"], res["missing"]), (2, 1))
        self.assertIn("IMG_1.HEIC", res["note"])

    def test_undelivered_text_is_not_recorded_as_sent(self):
        self.setup_family()
        m = self.moment()
        with mock.patch.object(moments, "_send_text",
                               return_value={"ok": False, "note": "not delivered"}):
            with self.assertRaises(RuntimeError):
                moments.send(m["id"], m["date"], "p_robin")
        self.assertFalse(self.moment()["recipients"][0]["sent_at"])
        self.assertEqual(self.files, [])

    def test_unknown_picks_are_ignored(self):
        self.setup_family()
        m = self.moment()
        moments.send(m["id"], m["date"], "p_robin", picks=[999, 3])
        self.assertEqual([f[1][-9:] for f in self.files], ["IMG_3.jpg"])


class MediaIndexJoinTests(unittest.TestCase):
    """The real query against a temp index built from the real schema: the
    day window, the source filter, and what each row carries."""

    def test_day_and_source_filter(self):
        from server import imessage, mediaindex
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.object(mediaindex, "_DATA", root), \
                mock.patch.object(mediaindex, "DB", root / "media-index.sqlite"):
            con = mediaindex._db()
            noon = dt.datetime.combine(TODAY, dt.time(12)).astimezone()
            rows = [  # (seq, kind, chat_pid, sender_pid, when)
                (1, "photo", "p_pat", "p_pat", noon),
                (2, "video", None, "p_pat", noon),                 # a group post
                (3, "photo", "p_pat", "me", noon),                 # owner's own, same thread
                (4, "photo", "p_other", "p_other", noon),          # not a source
                (5, "photo", "p_pat", "p_pat", noon - dt.timedelta(days=1)),
                (6, "doc", "p_pat", "p_pat", noon),
            ]
            for seq, kind, chat, sender, when in rows:
                con.execute("INSERT INTO items(seq, kind, id, chat_pid, sender_pid, date_ns, mime, name)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (seq, kind, 100 + seq, chat, sender, imessage.apple_ns(when),
                             "image/jpeg", f"IMG_{seq}.jpg"))
            con.execute("INSERT INTO faces(seq, person_id) VALUES (1, 'p_kid'), (1, NULL)")
            con.execute("INSERT INTO content(seq, ocr) VALUES (3, 'menu')")
            con.commit()
            con.close()
            got = moments._media_items(TODAY, ["p_pat"])
        self.assertEqual(sorted(r["id"] for r in got), [101, 102, 103])
        one = next(r for r in got if r["id"] == 101)
        self.assertEqual((one["faces"], one["known"]), (2, 1))
        self.assertEqual(next(r for r in got if r["id"] == 103)["ocr"], "menu")


class QueueTests(MomentsCase):
    def test_decide_queue_points_at_the_window_until_everyone_is_handled(self):
        self.setup_family()
        [row] = reviewqueue.SOURCES["moments"].read()
        self.assertEqual(row["open"], "#moments")
        self.assertIn("Robin Example", row["why"])
        m = self.moment()
        moments.send(m["id"], m["date"], "p_robin")
        self.assertEqual(reviewqueue.SOURCES["moments"].read(), [])

    def test_dismiss_from_the_queue(self):
        self.setup_family()
        [row] = reviewqueue.SOURCES["moments"].read()
        reviewqueue.SOURCES["moments"].act(row["raw_id"], "dismiss")
        self.assertEqual(reviewqueue.SOURCES["moments"].read(), [])
        self.assertTrue(self.moment()["dismissed"])

    def test_no_setup_no_rows(self):
        self.assertEqual(moments.pending(), [])


if __name__ == "__main__":
    unittest.main()

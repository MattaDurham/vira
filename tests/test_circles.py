"""Circle intelligence (server/circles.py): stable identities across
rebuilds, deterministic evidence off a synthetic chat.db, grounded-or-held
reads with the model stubbed, the re-read triggers and their budget, and
the read-time overlay.

Everything is rooted at ONE tmp fixture — the module reads the graph cache,
the circles store, the CRM cache, chat.db and the AddressBook index — and
`test_an_empty_fixture_syncs_nothing` is the isolation guard.

Run: .venv/bin/python -m unittest tests.test_circles
"""
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import atlas, atlaslens, circles, data as crm, imessage, settings
from server import suggest

ANN, RAJ, LEE, MAX, ZOE = ("+12125550101", "+12125550102", "+12125550103",
                           "+12125550104", "+12125550105")
DAY = 86_400 * 10 ** 9


def _person(pid, name, handle, cls="friend", summary="", met=""):
    return ({"id": pid, "name": name, "profile_tier": "A",
             "handles": {"emails": [], "imessage": [handle],
                         "phones10": [handle[2:]]},
             "activity": {"imsg_n": 50, "email_n": 0}},
            {"relationship_class": cls, "relationship_summary": summary,
             "how_we_met": met})


def _cache():
    people, profiles = [], {}
    rows = [
        _person("p_ann", "Ann Larkspur", ANN,
                summary="Ann is one of the owner's closest Brooklyn friends, "
                        "a regular on the 'Ski trip' chat since 2016.",
                met="Met skiing at Hunter Mountain in 2016."),
        _person("p_raj", "Raj Finch", RAJ,
                summary="Raj skis with the Brooklyn crew every winter."),
        _person("p_lee", "Lee Heron", LEE,
                summary="Lee organises the Hunter Mountain weekends."),
        _person("p_max", "Max Plover", MAX, cls="business",
                summary="Max is a colleague from the Gotham office."),
        _person("p_zoe", "Zoe Swift", ZOE),
    ]
    for p, prof in rows:
        people.append(p)
        profiles[p["id"]] = prof
    by_id = {p["id"]: p for p in people}
    by_handle = {}
    for p in people:
        for h in p["handles"]["imessage"]:
            by_handle[crm.norm_digits(h)] = p["id"]
    return {"people": people, "master": {}, "profiles": profiles,
            "by_id": by_id, "by_handle": by_handle,
            "chats_by_person": {}, "loaded_at": 0}


def _chat_db(path, msgs_in_ski=3, extra_chat=False):
    """'Ski trip' (ann, raj, lee — named), an unnamed ann+raj chat, a
    1:1, and optionally a new named 'Hunter 2027' chat."""
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, style INT,
                        display_name TEXT);
      CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
      CREATE TABLE chat_handle_join(chat_id INT, handle_id INT);
      CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INT,
                           is_from_me INT, handle_id INT, text TEXT);
      CREATE TABLE chat_message_join(chat_id INT, message_id INT);
    """)
    con.executemany("INSERT INTO handle VALUES(?,?)",
                    [(1, ANN), (2, RAJ), (3, LEE), (4, MAX), (5, ZOE)])
    con.executemany("INSERT INTO chat VALUES(?,?,?)", [
        (1, 45, ""), (2, 43, "Ski trip"), (3, 43, ""),
        (4, 43, "Hunter 2027")])
    joins = [(1, 1), (2, 1), (2, 2), (2, 3), (3, 1), (3, 2)]
    if extra_chat:
        joins += [(4, 1), (4, 2), (4, 3)]
    con.executemany("INSERT INTO chat_handle_join VALUES(?,?)", joins)
    base = int((datetime(2026, 1, 10, 12, tzinfo=timezone.utc).timestamp()
                - imessage.APPLE_EPOCH) * 1e9)
    rows, cmj, rid = [], [], 0
    for i in range(msgs_in_ski):
        rid += 1
        rows.append((rid, base + i * DAY, 0, 1, f"ski {i}"))
        cmj.append((2, rid))
    rid += 1
    rows.append((rid, base, 0, 2, "just us"))
    cmj.append((3, rid))
    if extra_chat:
        for i in range(2):
            rid += 1
            rows.append((rid, base + 40 * DAY + i * DAY, 0, 3, f"h{i}"))
            cmj.append((4, rid))
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chat_message_join VALUES(?,?)", cmj)
    con.commit()
    con.close()


def _graph(members=("p_ann", "p_raj", "p_lee"), cid="c3", extra=()):
    nodes = [{"id": pid, "name": n, "cluster": None, "company": "",
              "title": "", "degree": 1, "act": 10}
             for pid, n in (("p_ann", "Ann Larkspur"), ("p_raj", "Raj Finch"),
                            ("p_lee", "Lee Heron"), ("p_max", "Max Plover"),
                            ("p_zoe", "Zoe Swift"))]
    node_cluster = {}
    for n in nodes:
        if n["id"] in members:
            n["cluster"] = cid
            node_cluster[n["id"]] = cid
    clusters = [{"id": cid, "label": "circle 4", "size": len(members),
                 "kind": "circle"}]
    for ecid, ems in extra:
        for n in nodes:
            if n["id"] in ems:
                n["cluster"] = ecid
                node_cluster[n["id"]] = ecid
        clusters.append({"id": ecid, "label": "circle 9", "size": len(ems),
                         "kind": "circle"})
    edges = [
        {"a": "p_ann", "b": "p_raj", "weight": 1.5,
         "signals": [{"type": "group_cochat", "detail": "2 shared"},
                     {"type": "photo_cooccur", "detail": "3 photos"}]},
        {"a": "p_ann", "b": "p_lee", "weight": 0.9,
         "signals": [{"type": "group_cochat", "detail": "1 shared"}]},
    ]
    return {"generated": "2026-01-01T00:00:00+00:00",
            "owner": {"name": "Owner", "pid": None},
            "nodes": nodes, "edges": edges, "ego_edges": [],
            "clusters": clusters, "node_cluster": node_cluster}


class CirclesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.db = root / "chat.db"
        _chat_db(self.db)
        self.cache = _cache()
        self.graph = _graph()
        (root / "atlas-graph.json").write_text(json.dumps(self.graph),
                                               encoding="utf-8")
        self.cfg = {"owner_name": "Owner", "fixture_mode": False,
                    "circle_refresh_min": 60}
        patches = [
            mock.patch.object(crm, "_load", lambda: self.cache),
            mock.patch.object(atlas, "GRAPH", root / "atlas-graph.json"),
            mock.patch.object(atlas, "GROUPS", root / "atlas-groups.json"),
            mock.patch.object(circles, "STORE", root / "atlas-circles.json"),
            mock.patch.object(imessage, "CHAT_DB", self.db),
            mock.patch.object(settings, "fixture_mode", lambda: False),
            mock.patch.object(atlaslens, "_ab_index", lambda: {}),
            mock.patch.object(settings, "get",
                              lambda k, d=None: self.cfg.get(k, d)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.reads = []

    def stub_model(self, reply):
        """One canned read; records every prompt the engine composes."""
        def fake(prompt, tools=None):
            self.reads.append(prompt)
            return json.dumps(reply)
        p = mock.patch.object(suggest, "complete", fake)
        p.start()
        self.addCleanup(p.stop)

    def model_down(self):
        def fake(prompt, tools=None):
            self.reads.append(prompt)
            raise RuntimeError("no backend")
        p = mock.patch.object(suggest, "complete", fake)
        p.start()
        self.addCleanup(p.stop)

    def store(self):
        return json.loads((self.root / "atlas-circles.json")
                          .read_text(encoding="utf-8"))

    def only_sid(self):
        s = self.store()
        self.assertEqual(len(s["map"]), 1, s["map"])
        return next(iter(s["map"].values()))


GOOD_READ = {"label": "Ski trip crew", "why": "All three share the 'Ski "
             "trip' chat and Hunter Mountain weekends.",
             "you": "Owner skis with them every winter since 2016.",
             "them": "Lee organises the weekends; Ann and Raj ride along.",
             "hub": "p_lee", "since": "2016", "whats_new": ""}


class Identity(CirclesBase):
    def test_a_circle_keeps_its_id_across_a_rebuild_that_renumbers_it(self):
        self.model_down()
        circles.sync()
        sid = self.only_sid()
        # the next build calls the same people c7 and adds Zoe
        g2 = _graph(members=("p_ann", "p_raj", "p_lee", "p_zoe"), cid="c7")
        circles.sync(graph=g2)
        s = self.store()
        self.assertEqual(s["map"], {"c7": sid})
        rec = s["circles"][sid]
        self.assertIn("p_zoe", rec["members"])
        kinds = [h["kind"] for h in rec["history"]]
        self.assertEqual(kinds[0], "formed")
        self.assertIn("joined", kinds)
        joined = next(h for h in rec["history"] if h["kind"] == "joined")
        self.assertIn("Zoe", joined["what"])

    def test_a_different_set_of_people_mints_a_new_circle(self):
        self.model_down()
        circles.sync()
        sid = self.only_sid()
        g2 = _graph(members=("p_max", "p_zoe"), cid="c3")   # same cid!
        circles.sync(graph=g2)
        s = self.store()
        self.assertNotEqual(s["map"]["c3"], sid)
        self.assertTrue(s["circles"][sid]["dissolved"])
        self.assertEqual(s["circles"][sid]["history"][-1]["kind"],
                         "dissolved")

    def test_a_dissolved_circle_that_comes_back_is_revived_with_its_story(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        sid = self.only_sid()
        circles.sync(graph=_graph(members=("p_max", "p_zoe")))
        circles.sync(graph=self.graph)
        s = self.store()
        self.assertEqual(s["map"]["c3"], sid)
        rec = s["circles"][sid]
        self.assertFalse(rec["dissolved"])
        self.assertEqual(rec["label"], "Ski trip crew")
        self.assertEqual(rec["history"][-1]["kind"], "revived")

    def test_two_circles_match_one_to_one_by_best_overlap(self):
        self.model_down()
        g = _graph(members=("p_ann", "p_raj", "p_lee"),
                   extra=[("c9", ("p_max", "p_zoe"))])
        circles.sync(graph=g)
        s = self.store()
        self.assertEqual(len(s["map"]), 2)
        sids = dict(s["map"])
        # swap the positional ids; identities must follow the people
        g2 = _graph(members=("p_ann", "p_raj", "p_lee"), cid="c9",
                    extra=[("c3", ("p_max", "p_zoe"))])
        circles.sync(graph=g2)
        s2 = self.store()
        self.assertEqual(s2["map"]["c9"], sids["c3"])
        self.assertEqual(s2["map"]["c3"], sids["c9"])


class Evidence(CirclesBase):
    def test_live_groups_come_off_chat_db_in_one_pass(self):
        groups, source = circles.groups_for(
            self.cache, {"p_ann", "p_raj", "p_lee"})
        self.assertEqual(source, "chat.db")
        by_label = {g["label"]: g for g in groups}
        self.assertIn("Ski trip", by_label)
        ski = by_label["Ski trip"]
        self.assertTrue(ski["named"])
        self.assertEqual(sorted(ski["members"]), ["p_ann", "p_lee", "p_raj"])
        self.assertEqual(ski["messages"], 3)
        self.assertEqual(ski["first"], "2026-01-10")
        # the unnamed two-person chat gets a synthetic label and named=False
        unnamed = [g for g in groups if not g["named"]]
        self.assertEqual(len(unnamed), 1)
        self.assertTrue(unnamed[0]["label"].startswith("group: "))
        # the 1:1 (style 45) never appears
        self.assertEqual(len(groups), 2)

    def test_an_unreadable_chat_db_falls_back_to_the_archive(self):
        imessage.CHAT_DB = self.root / "missing.db"
        self.cache["chats_by_person"] = {"p_ann": [{
            "file": "g.md", "type": "group", "title": "Old crew",
            "chat_id": 9, "messages": 12, "date_first": "2019-01-01",
            "date_last": "2020-01-01",
            "participants": [{"person_id": "p_ann"},
                             {"person_id": "p_raj"}]}]}
        groups, source = circles.groups_for(self.cache, {"p_ann", "p_raj"})
        self.assertEqual(source, "archive")
        self.assertEqual(groups[0]["label"], "Old crew")
        self.assertTrue(groups[0]["named"])

    def test_evidence_names_the_hub_and_grounding_text(self):
        ev = circles.evidence(self.cache, self.graph,
                              ["p_ann", "p_raj", "p_lee"])
        self.assertEqual(ev["hub"], "p_ann")        # most in-circle weight
        self.assertEqual(ev["since"], "2026")
        self.assertIn("Ski trip", ev["text"])
        self.assertIn("Hunter Mountain", ev["text"])
        self.assertEqual(ev["ties"]["group_cochat"], 2)
        self.assertEqual(ev["photos"], 1)
        self.assertEqual(ev["chats"][0]["label"], "Ski trip")

    def test_topics_are_rare_across_the_graph_and_never_the_owner(self):
        # every member's profile says "owner" and "brooklyn"; only the
        # second is a topic, and only while the graph as a whole keeps it
        # rare
        for pid in ("p_ann", "p_raj", "p_lee"):
            self.cache["profiles"][pid]["relationship_summary"] += \
                " Owner and the Brooklyn skiers."
        members = ["p_ann", "p_raj", "p_lee"]
        ev = circles.evidence(self.cache, self.graph, members)
        self.assertIn("brooklyn", ev["topics"])
        self.assertNotIn("owner", ev["topics"])
        common = Counter({"brooklyn": 150, "skiers": 3, "hunter": 2})
        ev2 = circles.evidence(self.cache, self.graph, members,
                               df_all=common)
        self.assertNotIn("brooklyn", ev2["topics"])   # everyone has it
        self.assertIn("skiers", ev2["topics"])

    def test_a_synthetic_chat_label_never_carries_a_raw_handle(self):
        # a fourth participant nobody in the CRM resolves
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO handle VALUES(9, '+12125550199')")
        con.execute("INSERT INTO chat_handle_join VALUES(3, 9)")
        con.commit()
        con.close()
        groups, _src = circles.groups_for(self.cache, {"p_ann", "p_raj"})
        unnamed = next(g for g in groups if not g["named"])
        self.assertNotIn("5550", unnamed["label"])
        self.assertTrue(unnamed["label"].endswith("+1"), unnamed["label"])

    def test_the_prompt_carries_members_chats_and_the_prior_read(self):
        ev = circles.evidence(self.cache, self.graph,
                              ["p_ann", "p_raj", "p_lee"], "Ski trip")
        rec = {"read_at": "2026-01-01T00:00:00+00:00", "label": "Old name",
               "why": "w", "story": {"you": "Y", "them": "T"}}
        prompt = circles.compose_prompt(ev, rec, ["joined: Zoe"])
        self.assertIn("Ann Larkspur [p_ann]", prompt)
        self.assertIn("'Ski trip': 3 of 3 members", prompt)
        self.assertIn("PREVIOUS READ", prompt)
        self.assertIn("WHAT CHANGED SINCE: joined: Zoe", prompt)
        self.assertIn("CURRENT NAME: Ski trip", prompt)


class Grounding(CirclesBase):
    def ev(self):
        return circles.evidence(self.cache, self.graph,
                                ["p_ann", "p_raj", "p_lee"])

    def test_a_label_made_of_the_evidences_own_words_is_applied(self):
        r = circles.clean_read(GOOD_READ, self.ev())
        self.assertEqual(r["label"], "Ski trip crew")
        self.assertIsNone(r["held"])
        self.assertEqual(r["story"]["hub"], "p_lee")
        self.assertEqual(r["story"]["since"], "2016")

    def test_a_label_the_evidence_cannot_support_is_held(self):
        read = dict(GOOD_READ, label="Aspen powder hounds")
        r = circles.clean_read(read, self.ev())
        self.assertEqual(r["label"], "")
        self.assertEqual(r["held"]["label"], "Aspen powder hounds")
        self.assertIn("evidence", r["held"]["reason"])

    def test_generic_descriptors_need_no_grounding(self):
        self.assertTrue(circles.grounded("Old friends", "nothing here"))
        self.assertTrue(circles.grounded("Brooklyn crew",
                                         "she lives in brooklyn"))
        self.assertFalse(circles.grounded("Brooklyn crew", "queens"))

    def test_a_number_is_not_a_name(self):
        r = circles.clean_read(dict(GOOD_READ, label="Circle 4"), self.ev())
        self.assertEqual(r["held"]["reason"], "a number is not a name")

    def test_a_hub_outside_the_circle_and_a_year_it_never_saw_are_dropped(self):
        read = dict(GOOD_READ, hub="p_max", since="1999")
        r = circles.clean_read(read, self.ev())
        self.assertEqual(r["story"]["hub"], "p_ann")     # the evidence's
        self.assertEqual(r["story"]["since"], "2026")

    def test_a_read_missing_its_story_is_refused(self):
        with self.assertRaises(ValueError):
            circles.clean_read({"label": "Ski trip crew", "why": "w"},
                               self.ev())

    def test_the_fallback_name_is_the_covering_named_chat(self):
        self.assertEqual(circles.fallback_label(self.ev()), "Ski trip")

    def test_the_fallback_name_is_the_hub_when_no_chat_covers_half(self):
        ev = circles.evidence(self.cache, self.graph,
                              ["p_ann", "p_raj", "p_lee", "p_max", "p_zoe",
                               "p_x1", "p_x2"])
        self.assertEqual(circles.fallback_label(ev), "Ann's circle")


class Sync(CirclesBase):
    def test_a_first_pass_names_and_reads_every_circle(self):
        self.stub_model(GOOD_READ)
        rep = circles.sync()
        self.assertEqual(rep["circles"], 1)
        self.assertEqual(len(rep["read"]), 1)
        rec = self.store()["circles"][self.only_sid()]
        self.assertEqual(rec["label"], "Ski trip crew")
        self.assertEqual(rec["read_reason"], "never read")
        self.assertEqual(rec["story"]["you"], GOOD_READ["you"])
        self.assertEqual(rec["ev"]["chats"][0]["label"], "Ski trip")

    def test_with_no_model_the_fallback_name_stands_and_the_error_is_kept(self):
        self.model_down()
        rep = circles.sync()
        self.assertEqual(len(rep["errors"]), 1)
        rec = self.store()["circles"][self.only_sid()]
        self.assertEqual(rec["label"], "Ski trip")
        self.assertIsNone(rec["read_at"])
        self.assertIn("no backend", rec["read_error"])

    def test_an_unchanged_circle_is_not_read_twice(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        rep = circles.sync()
        self.assertEqual(rep["read"], [])
        self.assertEqual(rep["skipped"][0]["why"], "current")
        self.assertEqual(len(self.reads), 1)

    def test_enough_new_messages_earn_a_reread(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        _chat_db(self.db.with_name("busy.db"),
                 msgs_in_ski=3 + circles.REREAD_MSGS)
        imessage.CHAT_DB = self.db.with_name("busy.db")
        rep = circles.sync()
        self.assertEqual(len(rep["read"]), 1)
        rec = self.store()["circles"][self.only_sid()]
        self.assertIn("new messages", rec["read_reason"])

    def test_too_few_new_messages_do_not(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        _chat_db(self.db.with_name("quiet.db"), msgs_in_ski=6)
        imessage.CHAT_DB = self.db.with_name("quiet.db")
        self.assertEqual(circles.sync()["read"], [])

    def test_a_new_shared_chat_earns_a_reread_and_a_history_line(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        _chat_db(self.db.with_name("new.db"), extra_chat=True)
        imessage.CHAT_DB = self.db.with_name("new.db")
        rep = circles.sync()
        self.assertEqual(len(rep["read"]), 1)
        rec = self.store()["circles"][self.only_sid()]
        self.assertIn("Hunter 2027", rec["read_reason"])
        self.assertTrue(any(h["kind"] == "chat" and "Hunter 2027" in h["what"]
                            for h in rec["history"]))

    def test_a_member_change_earns_a_reread(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        g2 = _graph(members=("p_ann", "p_raj", "p_lee", "p_zoe"))
        rep = circles.sync(graph=g2)
        self.assertEqual(len(rep["read"]), 1)
        self.assertIn("joined: Zoe", self.store()["circles"][self.only_sid()]
                      ["read_reason"])

    def test_a_stale_read_is_read_again(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        sid = self.only_sid()
        old = (datetime.now(timezone.utc)
               - timedelta(days=circles.STALE_DAYS + 1)).isoformat(
                   timespec="seconds")
        circles._mutate(lambda s: s["circles"][sid].__setitem__(
            "read_at", old))
        rep = circles.sync()
        self.assertEqual(len(rep["read"]), 1)
        self.assertIn("days ago", self.store()["circles"][sid]["read_reason"])

    def test_a_pass_spends_at_most_its_budget(self):
        self.stub_model(GOOD_READ)
        g = _graph(members=("p_ann", "p_raj", "p_lee"),
                   extra=[("c9", ("p_max", "p_zoe"))])
        rep = circles.sync(graph=g, limit=1)
        self.assertEqual(len(rep["read"]), 1)
        self.assertEqual([s["why"] for s in rep["skipped"]], ["budget"])
        # the skipped one still wears a name
        labels = [r["label"] for r in self.store()["circles"].values()]
        self.assertTrue(all(labels), labels)
        # the next pass picks it up
        self.assertEqual(len(circles.sync(graph=g, limit=1)["read"]), 1)

    def test_force_and_sids_read_exactly_one_circle(self):
        self.stub_model(GOOD_READ)
        g = _graph(members=("p_ann", "p_raj", "p_lee"),
                   extra=[("c9", ("p_max", "p_zoe"))])
        circles.sync(graph=g)
        target = self.store()["map"]["c9"]
        rep = circles.sync(graph=g, force=True, limit=1, sids=[target])
        self.assertEqual([r["id"] for r in rep["read"]], [target])

    def test_a_name_another_circle_wears_is_held_and_named_in_the_prompt(self):
        # a generic label is grounded for BOTH circles, so only the
        # collision guard can hold the second one
        self.stub_model(dict(GOOD_READ, label="Old friends"))
        g = _graph(members=("p_ann", "p_raj", "p_lee"),
                   extra=[("c9", ("p_max", "p_zoe"))])
        circles.sync(graph=g)
        s = self.store()
        labels = sorted(r["label"] for r in s["circles"].values())
        # one wears the read, the other was held and fell back
        self.assertIn("Old friends", labels)
        self.assertEqual(labels.count("Old friends"), 1)
        held = [r for r in s["circles"].values() if r.get("held")]
        self.assertEqual(len(held), 1)
        self.assertIn("another circle", held[0]["held"]["reason"])
        # the second read was told what was taken
        self.assertIn("ALREADY CARRY (pick something distinct): Old friends",
                      self.reads[1])

    def test_a_held_label_keeps_the_fallback_and_records_the_proposal(self):
        self.stub_model(dict(GOOD_READ, label="Aspen powder hounds"))
        circles.sync()
        rec = self.store()["circles"][self.only_sid()]
        self.assertEqual(rec["label"], "Ski trip")
        self.assertEqual(rec["held"]["label"], "Aspen powder hounds")
        self.assertTrue(rec["read_at"])           # the story still landed

    def test_an_owner_rename_outranks_the_read(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        sid = self.only_sid()
        out = circles.rename(sid, "The mountain people")
        self.assertEqual(out["display_label"], "The mountain people")
        g = atlas.compose()
        band = next(l for l in g["lenses"] if l["id"] == "circles")["bands"]
        self.assertEqual(band[0]["label"], "The mountain people")
        circles.rename(sid, "")
        self.assertEqual(circles.circle(sid)["display_label"],
                         "Ski trip crew")

    def test_the_circles_lens_shows_the_name_and_carries_the_identity(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        sid = self.only_sid()
        g = atlas.compose()
        lens = next(l for l in g["lenses"] if l["id"] == "circles")
        self.assertEqual(lens["bands"][0]["label"], "Ski trip crew")
        self.assertEqual(lens["bands"][0]["circle"], sid)
        self.assertTrue(lens["bands"][0]["story"])
        cl = next(c for c in g["clusters"] if c["id"] == "c3")
        self.assertEqual(cl["raw_label"], "circle 4")

    def test_the_overlay_refuses_a_stale_map(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        # the graph on disk now holds different people under c3 and no
        # sync has run — the store's name must not land on strangers
        (self.root / "atlas-graph.json").write_text(
            json.dumps(_graph(members=("p_max", "p_zoe"))), encoding="utf-8")
        g = atlas.compose()
        lens = next(l for l in g["lenses"] if l["id"] == "circles")
        self.assertEqual(lens["bands"][0]["label"], "circle 4")
        self.assertIsNone(lens["bands"][0]["circle"])

    def test_dissolving_a_renamed_circle_still_removes_it(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        atlas.group_dissolve("c3")          # keyed on the shown label
        g = atlas.compose()
        self.assertEqual([c["id"] for c in g["clusters"]], [])

    def test_the_person_page_line_and_the_tool_text(self):
        self.stub_model(GOOD_READ)
        circles.sync()
        pg = atlas.person_groups("p_ann")
        self.assertEqual(pg["story"]["label"], "Ski trip crew")
        self.assertIn("Hunter", pg["story"]["why"])
        text = circles.text_for_tools()
        self.assertIn("## Ski trip crew (3 people", text)
        self.assertIn("Ann Larkspur", text)

    def test_an_empty_fixture_syncs_nothing(self):
        (self.root / "atlas-graph.json").unlink()
        self.model_down()
        rep = circles.sync()
        self.assertEqual(rep["circles"], 0)
        self.assertIn("atlas not built yet", rep["errors"])
        self.assertFalse((self.root / "atlas-circles.json").exists())
        self.assertEqual(self.reads, [])
        self.assertEqual(circles.status()["circles"], 0)


class Wiring(unittest.TestCase):
    def test_a_build_kicks_the_sync_and_compose_applies_the_store(self):
        src = (Path(__file__).resolve().parent.parent / "server"
               / "atlas.py").read_text(encoding="utf-8")
        self.assertIn("_after_build(graph)", src)
        self.assertIn("circles.sync_async(graph)", src)
        self.assertIn("circles.apply(graph)", src)
        # names before overrides — the dissolve list keys on the label
        self.assertLess(src.index("circles.apply(graph)"),
                        src.index("apply_overrides(graph)\n"))

    def test_the_watcher_is_built_and_started(self):
        src = (Path(__file__).resolve().parent.parent / "server"
               / "main.py").read_text(encoding="utf-8")
        self.assertIn("circle_watcher = circles.Watcher(", src)
        self.assertIn("circle_watcher.start()", src)


if __name__ == "__main__":
    unittest.main()

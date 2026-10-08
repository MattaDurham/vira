"""Maps - the System Map's layered diagram as a type any request can fill.

The contracts pinned here: the validator refuses a spec with a reason a
session can act on (and names every dangling link at once); a save keeps
the previous version and the brief, so Refresh can re-run and Undo can
step back; the built-in system map is derived from the module registry and
never stored; the native save_map tool is the write path and read-only
sessions cannot reach it; and the routes dispatch a real job shape.

Run: .venv/bin/python -m unittest tests.test_maps
"""
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import maps, modulemap, viratools

REAL_STORE = maps.STORE


def spec(**over):
    s = {
        "title": "Routines and skills",
        "intro": "Left to right: what starts it, what runs, what it feeds.",
        "columns": [{"id": "trigger", "title": "Triggers"},
                    {"id": "work", "title": "The work"},
                    {"id": "output", "title": "Where it lands"}],
        "groups": [{"id": "know", "name": "Know"},
                   {"id": "operate", "name": "Operate"}],
        "nodes": [
            {"id": "weekly", "name": "Weekly tick", "column": "trigger",
             "group": "operate", "kind": "scheduler",
             "what": "Fires every   seven days.",
             "links": [{"to": "map-routine", "how": "starts"},
                       {"to": "map-routine", "how": "starts again"}]},
            {"id": "map-routine", "name": "System map routine",
             "column": "work", "group": "know", "kind": "weekly",
             "what": "Rewrites the module registry from the change log.",
             "links": [{"to": "registry", "how": "writes"}]},
            {"id": "registry", "name": "Module registry", "column": "output",
             "group": "know", "what": "One record per module."},
        ],
    }
    s.update(over)
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.real_before = (REAL_STORE.exists(),
                            REAL_STORE.stat().st_mtime if REAL_STORE.exists()
                            else None)
        for target, attr, val in ((maps, "STORE", base / "maps.json"),
                                  (modulemap, "STORE", base / "modules.json")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        clock = (f"2026-10-08T12:00:{i:02d}+00:00" for i in itertools.count())
        p = mock.patch.object(maps, "_now_iso", side_effect=lambda: next(clock))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        after = (REAL_STORE.exists(),
                 REAL_STORE.stat().st_mtime if REAL_STORE.exists() else None)
        self.assertEqual(after, self.real_before,
                         "a test touched the real data/maps.json")


class TheValidator(Base):
    def test_a_good_spec_is_normalized(self):
        out = maps.clean(spec())
        self.assertEqual([g["color"] for g in out["groups"]],
                         list(maps.PALETTE[:2]))     # coloured by position
        weekly = out["nodes"][0]
        self.assertEqual(weekly["what"], "Fires every seven days.")
        self.assertEqual(weekly["links"], [{"to": "map-routine",
                                            "how": "starts"}])  # de-duplicated
        self.assertEqual(out["nodes"][2]["kind"], "")

    def test_every_dangling_link_is_named_at_once(self):
        s = spec()
        s["nodes"][1]["links"] = [{"to": "nowhere", "how": "x"},
                                  {"to": "elsewhere", "how": "y"}]
        with self.assertRaises(maps.MapError) as cm:
            maps.clean(s)
        self.assertIn("map-routine -> nowhere", str(cm.exception))
        self.assertIn("map-routine -> elsewhere", str(cm.exception))

    def test_refusals_name_the_problem(self):
        cases = [
            (dict(columns=[{"id": f"c{i}", "title": "C"} for i in range(7)]),
             "columns holds 7"),
            (dict(nodes=spec()["nodes"] + [dict(spec()["nodes"][2])]),
             "duplicate node id: registry"),
            (dict(columns=spec()["columns"] + [{"id": "empty", "title": "E"}]),
             "empty hold no boxes"),
            (dict(groups=[{"id": "Bad Id", "name": "x"}]), "kebab-case"),
            (dict(title=" "), "title is required"),
        ]
        for over, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(maps.MapError) as cm:
                    maps.clean(spec(**over))
                self.assertIn(needle, str(cm.exception))

    def test_a_node_in_an_unknown_column_or_group_is_refused(self):
        s = spec()
        s["nodes"][2]["column"] = "attic"
        with self.assertRaisesRegex(maps.MapError, "column 'attic'"):
            maps.clean(s)
        s = spec()
        s["nodes"][2]["group"] = "money"
        with self.assertRaisesRegex(maps.MapError, "group 'money'"):
            maps.clean(s)

    def test_a_self_link_is_refused(self):
        s = spec()
        s["nodes"][2]["links"] = [{"to": "registry", "how": "loops"}]
        with self.assertRaisesRegex(maps.MapError, "itself"):
            maps.clean(s)

    def test_overlong_text_is_refused_never_cut(self):
        s = spec()
        s["nodes"][2]["what"] = "x" * (maps.TEXT_CAPS["what"] + 1)
        with self.assertRaisesRegex(maps.MapError, "the cap is 1500"):
            maps.clean(s)


class TheStore(Base):
    def test_save_then_get_round_trips_with_the_brief(self):
        line = maps.save("routines", spec(), "map my routines")
        self.assertIn("3 boxes in 3 columns, 2 links", line)
        m = maps.get("routines")
        self.assertFalse(m["builtin"])
        self.assertEqual(m["meta"]["brief"], "map my routines")
        self.assertFalse(m["meta"]["has_previous"])
        self.assertEqual(m["spec"]["title"], "Routines and skills")

    def test_a_replacement_keeps_the_previous_version_and_brief(self):
        maps.save("routines", spec(), "map my routines")
        s = spec()
        s["nodes"] = s["nodes"][1:]
        s["columns"] = s["columns"][1:]
        line = maps.save("routines", s)          # a refresh with no brief
        self.assertIn("removed weekly", line)
        m = maps.get("routines")
        self.assertEqual(m["meta"]["brief"], "map my routines")
        self.assertEqual(m["meta"]["revisions"], 2)
        self.assertTrue(m["meta"]["has_previous"])
        self.assertTrue(maps.undo("routines"))
        self.assertEqual(len(maps.get("routines")["spec"]["nodes"]), 3)

    def test_the_system_slug_and_bad_slugs_are_refused(self):
        with self.assertRaisesRegex(maps.MapError, "built-in system map"):
            maps.save("system", spec())
        with self.assertRaisesRegex(maps.MapError, "kebab-case"):
            maps.save("My Map", spec())

    def test_the_ceiling_refuses_a_new_map_but_not_a_replacement(self):
        with mock.patch.object(maps, "MAX_MAPS", 1):
            maps.save("one", spec())
            maps.save("one", spec())              # replacing is fine
            with self.assertRaisesRegex(maps.MapError, "already keeps 1"):
                maps.save("two", spec())

    def test_list_puts_the_system_map_first_then_newest(self):
        maps.save("older", spec())
        maps.save("newer", spec())
        slugs = [r["slug"] for r in maps.list_maps()]
        self.assertEqual(slugs, ["system", "newer", "older"])
        self.assertTrue(maps.list_maps()[0]["builtin"])

    def test_delete_removes_and_the_system_map_cannot_be_deleted(self):
        maps.save("gone", spec())
        self.assertTrue(maps.delete("gone"))
        self.assertIsNone(maps.get("gone"))
        self.assertFalse(maps.delete("gone"))
        with self.assertRaises(maps.MapError):
            maps.delete("system")


class TheSystemMap(Base):
    def test_it_is_derived_from_the_registry_and_never_stored(self):
        reg = {"modules": [
            {"id": "a", "name": "A", "layer": "source", "group": "know",
             "what": "x", "links": [{"to": "b", "how": "feeds"},
                                    {"to": "ghost", "how": "haunts"}]},
            {"id": "b", "name": "B", "layer": "surface", "group": "money",
             "what": "y", "links": []}],
            "meta": {"last_refresh": "2026-10-02T22:56:29+00:00"}}
        modulemap.STORE.write_text(json.dumps(reg), encoding="utf-8")
        m = maps.get("system")
        self.assertTrue(m["builtin"])
        self.assertEqual([c["id"] for c in m["spec"]["columns"]],
                         list(modulemap.LAYERS))
        self.assertEqual(m["spec"]["nodes"][0]["links"],
                         [{"to": "b", "how": "feeds"}])   # ghost skipped
        self.assertEqual(m["meta"]["updated"], "2026-10-02T22:56:29+00:00")
        self.assertFalse(maps.STORE.exists())


class TheToolIsTheWritePath(Base):
    def test_bad_json_and_bad_specs_come_back_as_errors(self):
        self.assertTrue(viratools._save_map_text("x", "{not json").startswith(
            "error: spec_json is not valid JSON"))
        s = spec()
        s["nodes"][0]["links"] = [{"to": "nope", "how": "x"}]
        self.assertIn("weekly -> nope",
                      viratools._save_map_text("x", json.dumps(s)))
        self.assertIsNone(maps.get("x"))

    def test_a_good_call_saves(self):
        out = viratools._save_map_text("routines", json.dumps(spec()),
                                       "map my routines")
        self.assertTrue(out.startswith("Saved map 'routines'"), out)
        self.assertEqual(maps.get("routines")["meta"]["brief"],
                         "map my routines")

    def test_read_only_sessions_cannot_reach_it(self):
        self.assertIn("mcp__vira__save_map", viratools.WRITE_TOOLS)
        names = {t["name"] for t in viratools.dynamic_tool_specs(read_only=True)}
        self.assertFalse(any(n.endswith("save_map") for n in names), names)


class ThePrompts(Base):
    def test_the_ask_prompt_carries_the_request_and_the_write_path(self):
        p = maps.ask_prompt("every routine and skill, and what each feeds")
        self.assertIn('"every routine and skill, and what each feeds"', p)
        self.assertIn("mcp__vira__save_map", p)
        self.assertIn(maps.SPEC_SHAPE, p)
        with self.assertRaises(maps.MapError):
            maps.ask_prompt("   ")

    def test_the_refresh_prompt_carries_the_map_and_its_brief(self):
        maps.save("routines", spec(), "map my routines")
        p = maps.refresh_prompt("routines")
        self.assertIn('"map my routines"', p)
        self.assertIn("slug = routines", p)
        self.assertIn('"id": "map-routine"', p)
        with self.assertRaises(maps.MapError):
            maps.refresh_prompt("nope")
        with self.assertRaises(maps.MapError):
            maps.refresh_prompt("system")


class TheRoutes(Base):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server import main
        self.main = main
        self.client = TestClient(main.app)

    def test_list_get_and_404(self):
        maps.save("routines", spec())
        r = self.client.get("/api/maps")
        self.assertEqual([m["slug"] for m in r.json()["maps"]],
                         ["system", "routines"])
        self.assertEqual(self.client.get("/api/maps/routines").json()
                         ["spec"]["title"], "Routines and skills")
        self.assertEqual(self.client.get("/api/maps/nope").status_code, 404)

    def test_ask_dispatches_a_map_job(self):
        with mock.patch.object(self.main.jobs, "launch",
                               return_value="job123") as launch:
            r = self.client.post("/api/maps/ask",
                                 json={"request": "my routines and skills"})
        self.assertEqual(r.json(), {"job_id": "job123"})
        prompt = launch.call_args.args[0]
        self.assertIn('"my routines and skills"', prompt)
        self.assertEqual(launch.call_args.kwargs["meta"], {"kind": "map-build"})
        self.assertEqual(self.client.post("/api/maps/ask",
                                          json={"request": " "}).status_code,
                         400)

    def test_refresh_undo_and_delete(self):
        maps.save("routines", spec(), "brief")
        with mock.patch.object(self.main.jobs, "launch",
                               return_value="job9") as launch:
            r = self.client.post("/api/maps/routines/refresh")
        self.assertEqual(r.json(), {"job_id": "job9"})
        self.assertEqual(launch.call_args.kwargs["meta"],
                         {"kind": "map-build", "map": "routines"})
        self.assertEqual(self.client.post("/api/maps/nope/refresh").status_code,
                         404)
        self.assertEqual(self.client.post("/api/maps/routines/undo").status_code,
                         404)                     # nothing to undo yet
        self.assertEqual(self.client.delete("/api/maps/system").status_code, 400)
        self.assertEqual(self.client.delete("/api/maps/routines").status_code,
                         200)
        self.assertIsNone(maps.get("routines"))


if __name__ == "__main__":
    unittest.main()

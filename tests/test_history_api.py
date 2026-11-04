"""Every /api/history route, on the synthetic machine of
test_history_ingest.Sandbox. The routes that act (Merge, Discard, Resume,
Rerun, Judge) are checked to reach the existing code paths, which are
replaced here by recorders: nothing merges, deletes or launches."""
import asyncio
import json
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import circuits, judge, orphanwork, suggest
from server import history as H
from server import historyroutes as R
from tests.test_history_ingest import Sandbox


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(R.router)
        cls.app = app

    def setUp(self):
        self.sb = Sandbox(self)
        self.sb.build_all()
        self.sb.ingest()
        self.c = TestClient(self.app)

    def get(self, path, code=200):
        r = self.c.get(path)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    def post(self, path, body, code=200):
        r = self.c.post(path, json=body)
        self.assertEqual(r.status_code, code, r.text)
        return r.json()

    # ------------------------------------------------------------ reads
    def test_list(self):
        out = self.get("/api/history")
        self.assertEqual(out["group"], "day")
        self.assertEqual(set(out["statuses"]), set(H.STATUSES))
        entry = out["groups"][0]["entries"][0]
        for k in ("id", "status", "title", "at", "project", "module", "lines", "cost_usd",
                  "active_s", "actions", "sessions"):
            self.assertIn(k, entry)
        for group in ("request", "module", "project", "automation", "session", "none"):
            self.assertEqual(self.get(f"/api/history?group={group}")["group"], group)
        self.get("/api/history?group=weekday", 400)
        self.get("/api/history?status=Done", 400)
        self.get("/api/history?unit=thing", 400)
        events = self.get("/api/history?group=none&unit=event&limit=500")
        self.assertEqual(events["total"], len(H.load()))
        page = self.get("/api/history?group=none&unit=event&limit=2&offset=2")
        self.assertEqual(page["shown"], 2)
        self.assertTrue(page["more"])

    def test_list_filters(self):
        out = self.get("/api/history?group=request&status=Merged&q=widget")
        titles = [e["title"] for g in out["groups"] for e in g["entries"]]
        self.assertEqual(titles, ["Widget: draw in half the time"])
        out = self.get("/api/history?group=request&kind=run")
        self.assertTrue(any(e["kind"] == "automation" for g in out["groups"] for e in g["entries"]))
        out = self.get("/api/history?group=session&app=codex")
        self.assertEqual([e["title"] for g in out["groups"] for e in g["entries"]],
                         ["Count the tomatoes."])

    def test_entry(self):
        out = self.get("/api/history/entry?id=idea:idea_a")
        for k in ("entry", "outcome", "trail", "touched", "cost_time", "actions", "open"):
            self.assertIn(k, out)
        self.assertEqual(out["outcome"]["status"], "Merged")
        tid = out["entry"]["id"]
        self.assertEqual(self.get("/api/history/entry?id=" + tid)["entry"]["id"], tid)
        focus = self.get("/api/history/entry?id=session:vira:j1")
        self.assertEqual(focus["focus"]["id"], "session:vira:j1")
        self.assertTrue(any(o["what"] == "session" and o.get("job_id") == "j1" for o in focus["open"]))
        self.get("/api/history/entry?id=nothing", 404)

    def test_for(self):
        out = self.get("/api/history/for?idea=idea_a")
        self.assertEqual(out["for"], "idea:idea_a")
        self.assertEqual(out["entry"]["title"], "Widget: draw in half the time")
        mod = self.get("/api/history/for?module=work")
        self.assertGreater(mod["list"]["total"], 0)
        self.get("/api/history/for?branch=vira:claude/stale")
        self.get("/api/history/for?flow_run=run_a")
        self.get("/api/history/for", 400)
        self.get("/api/history/for?job=nope", 404)

    def test_grid_timemap_facets_rev(self):
        g = self.get("/api/history/grid?span=hour")
        self.assertTrue(any(r["missed"] for r in g["rows"]))
        self.get("/api/history/grid?span=week", 400)
        tm = self.get("/api/history/timemap?lane=module&since=2026-01-01")
        self.assertTrue(tm["lanes"])
        self.get("/api/history/timemap?lane=person", 400)
        f = self.get("/api/history/facets")
        self.assertEqual([s["key"] for s in f["statuses"]], list(H.STATUSES))
        self.assertIn("vira", [p["key"] for p in f["projects"]])
        rev = self.get("/api/history/rev")
        self.assertEqual(rev["revision"], H.revision())

    def test_stream_sends_the_revision_first(self):
        class Req:
            async def is_disconnected(self):
                return False

        async def first():
            resp = await R.api_history_stream(Req())
            gen = resp.body_iterator
            chunk = await gen.__anext__()
            await gen.aclose()
            return chunk
        chunk = asyncio.run(first())
        chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
        self.assertTrue(chunk.startswith("event: rev\ndata: "))
        self.assertEqual(json.loads(chunk.split("data: ", 1)[1])["revision"], H.revision())

    def test_ingest_route_starts_one_pass(self):
        with mock.patch.object(R.ticker, "run_once", return_value=True) as run:
            out = self.post("/api/history/ingest", {})
        self.assertTrue(out["started"])
        run.assert_called_once()

    # ------------------------------------------------------------ ask
    def test_ask_returns_a_checked_spec(self):
        answer = json.dumps({"view": "list", "group": "project", "filters": {
            "since": "2026-11-02", "project": "vira", "module": "made-up",
            "status": ["Merged", "Finished"], "kind": ["code"], "q": "widget"}})
        with mock.patch.object(suggest, "complete", return_value="Here: " + answer) as call:
            out = self.post("/api/history/ask", {"q": "what merged into vira this week?"})
        self.assertEqual(call.call_args.kwargs.get("tools"), [], "no tools for the model")
        self.assertEqual(out["status"], "ok")
        spec = out["spec"]
        self.assertEqual(spec["group"], "project")
        self.assertEqual(spec["filters"]["project"], "vira")
        self.assertEqual(spec["filters"]["module"], "", "an unknown module is dropped")
        self.assertEqual(spec["filters"]["status"], ["Merged"], "only the eight words")
        self.assertEqual(spec["filters"]["since"], "2026-11-02")

    def test_ask_without_a_model_says_so(self):
        with mock.patch.object(suggest, "complete", side_effect=RuntimeError("no login")):
            out = self.post("/api/history/ask", {"q": "did notes capture run yesterday?"})
        self.assertEqual(out["status"], "model_unavailable")
        self.assertIsNone(out["spec"])
        self.assertEqual(out["fallback_source"], "keywords")
        self.assertEqual(out["fallback"]["view"], "grid")
        with mock.patch.object(suggest, "complete", return_value="no json here"):
            self.assertEqual(self.post("/api/history/ask", {"q": "x"})["status"], "invalid_answer")
        self.post("/api/history/ask", {"q": "  "}, 400)

    # ------------------------------------------------------------ actions
    def test_merge_needs_confirm_then_reaches_branch_sh(self):
        body = {"id": "branch:vira:claude/stale", "action": "merge", "branch": "claude/stale"}
        r = self.c.post("/api/history/action", json=body)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["confirm"], "Merge claude/stale into live main?")
        with mock.patch.object(orphanwork, "merge", return_value=(True, "started")) as merge:
            out = self.post("/api/history/action", dict(body, confirm=True))
        merge.assert_called_once_with("stale")
        self.assertEqual(out["via"], "branch.sh")

    def test_discard_reaches_branch_sh_with_force(self):
        body = {"id": "branch:vira:claude/stale", "action": "discard", "confirm": True,
                "force": True}
        with mock.patch.object(orphanwork, "discard", return_value=(True, "started")) as d:
            self.post("/api/history/action", body)
        d.assert_called_once_with("stale", force=True)

    def test_an_action_the_entry_does_not_offer_is_refused(self):
        self.post("/api/history/action", {"id": "idea:idea_a", "action": "merge",
                                          "confirm": True}, 409)
        self.post("/api/history/action", {"id": "nothing", "action": "merge"}, 404)

    def test_resume_asks_for_a_prompt_first(self):
        body = {"id": "branch:vira:claude/stale", "action": "resume"}
        with mock.patch.object(orphanwork, "compose", return_value={"items": []}), \
                mock.patch.object(orphanwork, "resume_prompt", return_value="Carry on.") as rp:
            out = self.post("/api/history/action", body)
        self.assertTrue(out["needs_prompt"])
        self.assertEqual(out["prompt"], "Carry on.")
        self.assertEqual(rp.call_args.args[0]["branch"], "claude/stale")
        with mock.patch.object(orphanwork, "compose", return_value={"items": []}), \
                mock.patch.object(orphanwork, "resume", return_value="j9") as res:
            out = self.post("/api/history/action", dict(body, prompt="Carry on."))
        self.assertEqual(out["job_id"], "j9")
        self.assertEqual(res.call_args.kwargs["prompt"], "Carry on.")

    def test_rerun_starts_the_same_flow_with_its_entry(self):
        with mock.patch.object(circuits, "start_run", return_value={"id": "run_new"}) as sr:
            out = self.post("/api/history/action", {"id": "flow_run:run_a", "action": "rerun"})
        self.assertEqual(out["run_id"], "run_new")
        args, kw = sr.call_args
        self.assertEqual(args[:2], ("cir_n", "go"))
        self.assertEqual(kw["entry"], ["export"])
        self.assertEqual(kw["source"], "history-rerun")

    def test_judge_reaches_launch_judge(self):
        with mock.patch.object(judge, "launch_judge", return_value="jj") as lj:
            out = self.post("/api/history/action", {"id": "idea:idea_a", "action": "judge"})
        self.assertEqual(out["judge_job_id"], "jj")
        lj.assert_called_once_with("j1", model=None)


class TickerCase(unittest.TestCase):
    def test_one_pass_at_a_time_and_its_report_is_kept(self):
        t = R.Ticker()
        script = "import json; print(json.dumps({'written': 3}))"
        with mock.patch.object(t, "spawn_args", lambda mode: [R.sys.executable, "-c", script]):
            self.assertTrue(t.run_once("tick"))
            self.assertFalse(t.run_once("tick"), "a second pass waits for the first")
            for _ in range(500):
                if not t.status()["running"]:
                    break
                R.time.sleep(0.01)
        self.assertEqual(t.status()["last"]["written"], 3)
        self.assertEqual(t.status()["last"]["mode"], "tick")


if __name__ == "__main__":
    unittest.main()

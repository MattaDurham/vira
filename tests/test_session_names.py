"""The three-part session name: PR #n - subject - what this is.

Before 2026-09-03 a session was named by cutting the first line of its
prompt, so every Land dispatch read "Finishing stalled work in a
branch-first repository so it can LAND" and every text reply "The owner
just sent you this by text message". These pin the composition, its
inputs at every seam that supplies one, and the surfaces that render it.
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import joblog, orphanwork, prindex

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _rec(**kw):
    r = {"id": "j1", "prompt": "", "cwd": "/tmp", "meta": {}, "title": ""}
    r.update(kw)
    return r


class _Store(unittest.TestCase):
    """A throwaway ledger and PR index - joblog and prindex are both
    cross-process stores that would otherwise land in data/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        for mod, name, val in ((joblog, "STORE", d / "jobs-log.json"),
                               (prindex, "STORE", d / "pr-index.json")):
            p = mock.patch.object(mod, name, val)
            p.start()
            self.addCleanup(p.stop)
        prindex._cache["mtime"] = None


class KindLabel(unittest.TestCase):
    def test_every_dispatch_shape_has_a_kind(self):
        cases = [
            (_rec(meta={"kind": "orphan-land", "land_mode": "diagnose"}),
             "Diagnose and land"),
            (_rec(meta={"kind": "orphan-land", "land_mode": "finish"}), "Land"),
            (_rec(meta={"kind": "orphan-resume"}), "Resume"),
            (_rec(meta={"kind": "text-reply"}), "Text reply"),
            (_rec(meta={"kind": "chat"}), "Chat"),
            (_rec(meta={"judge_of": "j0"}), "Judge"),
            (_rec(meta={"routine_id": "muse", "kind": "muse"}), "Routine"),
            (_rec(meta={"routine_id": "system-map"}), "System map"),
            (_rec(meta={"circuit_run": "r", "stage": "build"}), "Flow step"),
            (_rec(idea_id="i1", publish_plan=True), "Plan"),
            (_rec(idea_id="i1"), "Implement"),
            (_rec(prompt='You are Vira.\n"""\nwhat is due?\n"""\n'), "Ask"),
            (_rec(prompt="do a thing"), "Session"),
            (_rec(kind_label="Write application"), "Write application"),
        ]
        for rec, want in cases:
            self.assertEqual(joblog.kind_label(rec), want, rec)


class Subject(_Store):
    def test_explicit_subject_wins(self):
        r = _rec(subject="Qocha vault onboarding", idea_id="i1",
                 pr={"number": 3, "title": "PR title"})
        self.assertEqual(joblog.subject(r, "the idea"), "Qocha vault onboarding")

    def test_pr_title_outranks_the_prompt_preamble(self):
        prindex.note("claude/qocha-vault-onboarding", 19, "u",
                     "Vault commissioning: attach a folder of files")
        r = _rec(prompt="You are finishing stalled work in a branch-first "
                        "repository so it can LAND.\n",
                 meta={"kind": "orphan-land", "branch":
                       "claude/qocha-vault-onboarding"})
        self.assertEqual(joblog.subject(r),
                         "Vault commissioning: attach a folder of files")

    def test_a_legacy_land_row_reads_its_branch_not_the_preamble(self):
        r = _rec(prompt="You are finishing stalled work in a branch-first "
                        "repository so it can LAND.\n",
                 meta={"kind": "orphan-land", "land_mode": "diagnose",
                       "branch": "claude/qocha-vault-onboarding"})
        self.assertEqual(joblog.subject(r), "qocha vault onboarding")
        self.assertEqual(joblog.name(r),
                         "qocha vault onboarding · Diagnose and land")

    def test_the_job_id_suffix_never_names_a_branch(self):
        self.assertEqual(joblog._humanize_branch("claude/undo-button-ab12cd"),
                         "undo button")

    def test_idea_and_question_stay_the_subject(self):
        r = _rec(idea_id="i1")
        self.assertEqual(joblog.subject(r, "Add an undo button"),
                         "Add an undo button")
        q = _rec(prompt='You are Vira.\n"""\nwhat is due?\n"""\n')
        self.assertEqual(joblog.subject(q), "what is due?")


class Composition(_Store):
    def test_pr_and_kind_survive_and_the_subject_gives_way(self):
        long = "a subject " * 20
        t = joblog.compose_name({"number": 19}, long, "Diagnose and land")
        self.assertTrue(t.startswith("PR #19" + joblog.SEP))
        self.assertTrue(t.endswith(joblog.SEP + "Diagnose and land"))
        self.assertLessEqual(len(t), joblog.TITLE_CAP)
        self.assertIn("…", t)

    def test_no_pr_no_head(self):
        self.assertEqual(joblog.compose_name(None, "x", "Land"), "x · Land")

    def test_pr_number_joins_from_the_index_at_read_time(self):
        r = _rec(subject="Undo in Flows", idea_id="i1",
                 branch="claude/undo-button-ab12cd")
        self.assertEqual(joblog.name(r), "Undo in Flows · Implement")
        prindex.note("claude/undo-button-ab12cd", 12, "https://x/pull/12")
        self.assertEqual(joblog.name(r), "PR #12 · Undo in Flows · Implement")

    def test_a_stored_default_title_is_not_an_edit(self):
        # record_launch always stored the derived default in `title`, so a
        # row written before the composition must still recompose
        r = _rec(prompt='You are Vira.\n"""\nBuild it\n"""\n', idea_id="i1")
        r["title"] = joblog._legacy_default(r, "Build it")
        self.assertEqual(joblog.name(r, "Build it"), "Build it · Implement")

    def test_an_owner_rename_outranks_everything(self):
        r = _rec(subject="x", pr={"number": 1}, title="My name",
                 title_edited=True)
        self.assertEqual(joblog.name(r), "My name")
        legacy = _rec(subject="x", title="Old hand-typed name")
        self.assertEqual(joblog.name(legacy), "Old hand-typed name")

    def test_command_names_the_first_command_line_by_the_subject(self):
        r = _rec(subject="Qocha vault onboarding",
                 meta={"kind": "orphan-land", "land_mode": "finish"},
                 prompt="You are finishing stalled work ...")
        self.assertEqual(joblog.command(r), "Land — Qocha vault onboarding")


class Ledger(_Store):
    def test_record_launch_stamps_the_inputs(self):
        joblog.record_launch({"id": "a1", "prompt": "p", "cwd": "/tmp",
                              "subject": "S", "about": "long form",
                              "kind_label": "Write application",
                              "pr": {"number": 4, "url": "u"}})
        r = joblog.get_record("a1")
        self.assertEqual((r["subject"], r["about"], r["kind_label"]),
                         ("S", "long form", "Write application"))
        self.assertEqual(r["pr"]["number"], 4)
        self.assertEqual(joblog.name(r), "PR #4 · S · Write application")

    def test_a_resumed_conversation_inherits_its_subject(self):
        joblog.record_launch({"id": "a1", "prompt": "p", "cwd": "/tmp",
                              "subject": "S", "about": "A",
                              "pr": {"number": 4, "url": "u"}})
        joblog.record_launch({"id": "a2", "prompt": "continue", "cwd": "/tmp",
                              "resumed_from": "a1",
                              "meta": {"kind": "resume"}})
        r = joblog.get_record("a2")
        self.assertEqual((r["subject"], r["about"], r["pr"]["number"]),
                         ("S", "A", 4))
        self.assertEqual(joblog.name(r), "PR #4 · S · Resume")

    def test_record_pr_and_the_rename_round_trip(self):
        joblog.record_launch({"id": "a1", "prompt": "p", "cwd": "/tmp",
                              "subject": "S"})
        joblog.record_pr("a1", 7, "https://x/pull/7")
        self.assertEqual(joblog.name(joblog.get_record("a1")),
                         "PR #7 · S · Session")
        joblog.set_title("a1", "Mine")
        self.assertTrue(joblog.get_record("a1")["title_edited"])
        self.assertEqual(joblog.name(joblog.get_record("a1")), "Mine")
        joblog.set_title("a1", "")
        self.assertEqual(joblog.name(joblog.get_record("a1")),
                         "PR #7 · S · Session")

    def test_describe_carries_every_field_a_surface_renders(self):
        joblog.record_launch({"id": "a1", "prompt": "p", "cwd": "/tmp",
                              "subject": "S", "about": "A"})
        joblog.record_finish("a1", "done", "I did the thing.")
        d = joblog.describe(joblog.get_record("a1"))
        self.assertEqual(set(d), {"title", "kind_label", "subject", "pr",
                                  "about", "outcome"})
        self.assertEqual(d["outcome"], "I did the thing.")


class PrIndex(_Store):
    def test_refresh_shapes_gh_rows_and_a_dead_gh_keeps_the_old_index(self):
        rows = [{"number": 44, "url": "u44", "title": "Landing card",
                 "headRefName": "claude/landing-card", "state": "MERGED",
                 "isDraft": False},
                {"number": 45, "url": "u45", "title": "Names",
                 "headRefName": "claude/session-names", "state": "OPEN",
                 "isDraft": True}]
        with mock.patch.object(prindex, "_gh_list", return_value=rows):
            by = prindex.refresh(force=True)
        self.assertEqual(by["claude/session-names"]["number"], 45)
        self.assertTrue(prindex.lookup("claude/session-names")["draft"])
        self.assertEqual(prindex.lookup("claude/landing-card")["state"],
                         "MERGED")
        with mock.patch.object(prindex, "_gh_list", return_value=None):
            by2 = prindex.refresh(force=True)
        self.assertEqual(by2, by)
        self.assertIsNone(prindex.lookup("claude/nope"))
        self.assertIsNone(prindex.lookup(""))

    def test_note_adds_without_making_a_stale_index_read_fresh(self):
        prindex.note("claude/x", 9, "u9", "Title")
        self.assertEqual(prindex.lookup("claude/x")["title"], "Title")
        self.assertTrue(prindex.stale())

    def test_refresh_async_is_off_under_the_env_switch(self):
        with mock.patch.dict(os.environ, {"VIRA_PR_INDEX_OFF": "1"}):
            self.assertFalse(prindex.refresh_async(force=True))


class LandDispatch(_Store):
    """The Land / Resume dispatches name the session for the WORK."""

    def _item(self, **kw):
        it = {"branch": "claude/qocha-vault-onboarding", "worktree": "/wt",
              "ahead": 1, "dirty": 0,
              "commits": ["Vault commissioning: attach a folder of files"],
              "job": {"subject": "", "about": "",
                      "prompt_head": "You are finishing stalled work ..."},
              "pr": {"number": 19, "url": "u", "title": "Vault commissioning"},
              "failure": {"headline": "The harness refused to carry the "
                                      "message"}}
        it.update(kw)
        return it

    def test_subject_ladder(self):
        self.assertEqual(orphanwork.branch_subject(self._item()),
                         "Vault commissioning")
        self.assertEqual(orphanwork.branch_subject(
            self._item(job={"subject": "From the dispatch"})),
            "From the dispatch")
        # no dispatch, no PR: the first unmerged commit names the work
        self.assertEqual(orphanwork.branch_subject(self._item(pr=None, job=None)),
                         "Vault commissioning: attach a folder of files")
        # nothing at all: the slug, humanized
        self.assertEqual(orphanwork.branch_subject(
            self._item(pr=None, job=None, commits=[])), "qocha vault onboarding")
        # a subject the ledger derived from the Land prompt is the ACT and
        # never outranks the commit
        self.assertEqual(orphanwork.branch_subject(self._item(
            pr=None, job={"subject": "Finishing stalled work in a branch-first"})),
            "Vault commissioning: attach a folder of files")

    def test_a_preamble_slug_never_names_a_branch(self):
        # the 2026-07-29 incident's branches: the slug is the prompt's
        # first line and describes nothing
        it = self._item(branch="claude/you-are-vira-s-coding-agent-work-a80ec5",
                        pr=None, job={"subject": "you are vira s coding agent work"},
                        commits=["Undeclared config keys fail loudly now"])
        self.assertEqual(orphanwork.branch_subject(it),
                         "Undeclared config keys fail loudly now")
        self.assertEqual(joblog._humanize_branch(it["branch"]), "")
        bare = self._item(branch=it["branch"], pr=None, job=None, commits=[])
        # the raw slug is the honest last resort - never an empty title
        self.assertEqual(orphanwork.branch_subject(bare),
                         "you-are-vira-s-coding-agent-work-a80ec5")

    def test_the_sweep_row_carries_the_subject(self):
        with mock.patch.object(orphanwork, "_ahead_behind",
                               return_value=(1, 0)), \
                mock.patch.object(orphanwork, "_tip_sha", return_value="abc"), \
                mock.patch.object(orphanwork, "_commit_time",
                                  return_value=1.0), \
                mock.patch.object(orphanwork, "_branch_commits",
                                  return_value=["Fix the thing"]), \
                mock.patch.object(orphanwork, "_failure_summary",
                                  return_value=None):
            item = orphanwork._make_item(
                "claude/you-are-vira-s-coding-agent-work-a80ec5", None, [], {})
        self.assertEqual(item["subject"], "Fix the thing")
        self.assertIn("Fix the thing", item["about"])

    def test_subject_hint_reads_the_cached_sweep(self):
        with mock.patch.object(orphanwork, "_read", return_value={
                "items": [{"branch": "claude/x", "subject": "The work"}]}):
            self.assertEqual(orphanwork.subject_hint("claude/x"), "The work")
            self.assertEqual(orphanwork.subject_hint("claude/none"), "")
            self.assertEqual(orphanwork.subject_hint(""), "")

    def test_about_states_the_branch_and_why_it_stopped(self):
        a = orphanwork.branch_about(self._item(), "Finish and land")
        self.assertIn("Finish and land the branch claude/qocha-vault-onboarding",
                      a)
        self.assertIn("1 commit not on main", a)
        self.assertIn("Originally asked:", a)
        self.assertIn("Why it stopped: The harness refused", a)

    def test_land_and_resume_pass_the_name_inputs(self):
        calls = []

        def launch(prompt, **kw):
            calls.append(kw)
            return "jid"
        with mock.patch("server.session.sessions") as ss, \
                mock.patch.object(orphanwork, "_refuse_if_busy"), \
                mock.patch.object(orphanwork, "land_diagnose_prompt",
                                  return_value="P"), \
                mock.patch.object(orphanwork, "resume_prompt",
                                  return_value="P"):
            ss.launch.side_effect = launch
            orphanwork._launch_land_session(self._item(), "diagnose")
            orphanwork.resume(self._item())
        land, res = calls
        self.assertEqual(land["subject"], "Vault commissioning")
        self.assertEqual(land["pr"]["number"], 19)
        self.assertIn("Diagnose why the earlier session stopped", land["about"])
        self.assertEqual(res["subject"], "Vault commissioning")
        self.assertIn("Resume the work on the branch", res["about"])

    def test_the_sweep_row_carries_the_pr(self):
        prindex.note("claude/b", 3, "u3", "T")
        with mock.patch.object(orphanwork, "_ahead_behind",
                               return_value=(1, 0)), \
                mock.patch.object(orphanwork, "_tip_sha", return_value="abc"), \
                mock.patch.object(orphanwork, "_commit_time",
                                  return_value=1.0), \
                mock.patch.object(orphanwork, "_branch_commits",
                                  return_value=["c"]), \
                mock.patch.object(orphanwork, "_failure_summary",
                                  return_value=None):
            item = orphanwork._make_item("claude/b", None, [], {})
        self.assertEqual(item["pr"]["number"], 3)


class Retroactive(_Store):
    """Rows written before the name existed - and branches whose
    originating dispatch is gone - still get named for the work."""

    def _land(self, branch, jid="j2"):
        return _rec(id=jid, prompt="You are finishing stalled work in a "
                    "branch-first repository so it can LAND.\n",
                    meta={"kind": "orphan-land", "land_mode": "finish",
                          "branch": branch}, branch=branch)

    def test_a_land_row_is_named_for_the_job_that_started_the_branch(self):
        impl = _rec(id="j1", prompt='You are Vira.\n"""\nAdd undo in Flows\n"""\n',
                    idea_id="i1", branch="claude/undo-ab12cd")
        land = self._land("claude/undo-ab12cd")
        bb = joblog.by_branch_index([impl, land])
        self.assertEqual(joblog.name(land, None, bb), "Add undo in Flows · Land")
        self.assertEqual(joblog.about(land, bb), "Add undo in Flows")
        # and without the index the ledger is read once
        joblog.record_launch({"id": "k1", "prompt": impl["prompt"], "cwd": "/tmp",
                              "idea_id": "i1", "branch": "claude/undo-ab12cd"})
        joblog.record_launch({"id": "k2", "prompt": land["prompt"], "cwd": "/tmp",
                              "meta": land["meta"], "branch": "claude/undo-ab12cd"})
        self.assertEqual(joblog.name(joblog.get_record("k2")),
                         "Add undo in Flows · Land")

    def test_a_resumed_row_is_named_for_the_conversation_it_continues(self):
        first = _rec(id="c1", prompt='You are Vira.\n"""\nwhat is due?\n"""\n')
        resumed = _rec(id="c2", prompt="Pick this up where you left off.",
                       meta={"kind": "resume", "resumed_from": "c1"})
        idx = joblog.by_branch_index([first, resumed])
        self.assertEqual(joblog.name(resumed, None, idx), "what is due? · Resume")
        joblog.record_launch({"id": "c1", "prompt": first["prompt"], "cwd": "/tmp"})
        joblog.record_launch({"id": "c2", "prompt": resumed["prompt"], "cwd": "/tmp",
                              "meta": resumed["meta"]})
        self.assertEqual(joblog.name(joblog.get_record("c2")), "what is due? · Resume")

    def test_a_one_word_stage_subject_yields_to_the_commit(self):
        it = {"branch": "claude/circuit-step-build-8c5f67", "pr": None,
              "job": {"subject": "build"},
              "commits": ["Triage resolver: the grounded-or-held verify"]}
        self.assertEqual(orphanwork.branch_subject(it),
                         "Triage resolver: the grounded-or-held verify")
        it["commits"] = []
        self.assertEqual(orphanwork.branch_subject(it), "build")

    def test_a_legacy_stage_row_reads_the_flows_original_ask(self):
        r = _rec(prompt="You are the BUILD stage of a pipeline. Implement "
                        "the plan below.\nOriginal ask: Build a triage "
                        "resolver for unknown senders\n\nPlan: ...",
                 meta={"circuit_run": "r1", "stage": "build"})
        self.assertEqual(joblog.subject(r),
                         "Build a triage resolver for unknown senders")
        self.assertEqual(joblog.name(r),
                         "Build a triage resolver for unknown senders · Flow step")

    def test_a_legacy_row_falls_back_to_what_it_was_asked(self):
        r = _rec(prompt='You are Vira.\n"""\nwhat is due?\n"""\n')
        self.assertEqual(joblog.about(r), "what is due?")

    def test_a_continuation_never_reads_its_own_preamble_as_the_ask(self):
        self.assertEqual(joblog.about(self._land("claude/x")), "")

    def test_the_route_overlays_the_sweeps_name_when_the_origin_is_gone(self):
        from fastapi.testclient import TestClient
        from server import main
        joblog.record_launch({"id": "h2", "prompt": "You are finishing stalled "
                              "work in a branch-first repository so it can LAND.\n",
                              "cwd": "/tmp",
                              "meta": {"kind": "orphan-land", "land_mode": "finish",
                                       "branch": "claude/you-are-vira-s-coding-agent-work-a80ec5"},
                              "branch": "claude/you-are-vira-s-coding-agent-work-a80ec5"})
        with mock.patch.object(orphanwork, "subject_hint",
                               return_value="Undeclared config keys fail loudly"):
            c = TestClient(main.app)
            rows = c.get("/api/jobs/history?limit=500").json()["jobs"]
        row = next(r for r in rows if r["id"] == "h2")
        self.assertEqual(row["title"],
                         "Undeclared config keys fail loudly · Land")
        self.assertEqual(row["subject"], "Undeclared config keys fail loudly")


class LandingCardStampsThePr(_Store):
    def test_note_pr_writes_the_ledger_and_the_index(self):
        from server import runner
        joblog.record_launch({"id": "r1", "prompt": "p", "cwd": "/tmp",
                              "subject": "S", "branch": "claude/s"})
        r = runner.Runner.__new__(runner.Runner)
        r.spec = {"id": "r1", "branch": "claude/s", "subject": "S"}
        r._note_pr("https://github.com/x/vira/pull/45")
        self.assertEqual(joblog.get_record("r1")["pr"]["number"], 45)
        self.assertEqual(prindex.lookup("claude/s")["number"], 45)
        self.assertEqual(joblog.name(joblog.get_record("r1")),
                         "PR #45 · S · Session")


class RouteOverlay(_Store):
    def test_history_rows_carry_the_name_parts(self):
        from fastapi.testclient import TestClient
        from server import main
        joblog.record_launch({"id": "h1", "prompt": "p", "cwd": "/tmp",
                              "subject": "S", "about": "A",
                              "pr": {"number": 2, "url": "u"}})
        # No context manager: entering one runs the app's startup and
        # starts every background worker inside the test process.
        c = TestClient(main.app)
        rows = c.get("/api/jobs/history?limit=500").json()["jobs"]
        row = next(r for r in rows if r["id"] == "h1")
        self.assertEqual(row["title"], "PR #2 · S · Session")
        self.assertEqual(row["about"], "A")
        self.assertEqual(row["pr"]["number"], 2)
        self.assertEqual(row["kind_label"], "Session")


def _fn(name):
    m = re.search(r"function " + name + r"\(.*?\n\}\n", APP_JS, re.S)
    assert m, name
    return m.group(0)


class Surfaces(unittest.TestCase):
    """Source contracts over app.js: the chip and the block reach every
    run surface, and the client says what its dispatch is about."""

    def test_launch_passes_the_name_inputs(self):
        f = _fn("launchJob")
        for k in ("subject", "about", "kind_label"):
            self.assertIn(k + ": opts." + k, f)

    def test_the_idea_sheet_names_its_dispatch(self):
        self.assertIn("subject: it.text, about: ideaAbout(it, mode, extra, fold)",
                      APP_JS)
        self.assertIn("function ideaAbout(", APP_JS)

    def test_every_run_surface_renders_the_pr_and_the_about(self):
        for fn in ("runCard", "jobHistRow", "renderFirstCmd"):
            self.assertIn("prChip(", _fn(fn), fn)
        for fn in ("runCard", "jobHistRow"):
            self.assertIn("aboutBlock(", _fn(fn), fn)
        self.assertIn("cc-fc-about", _fn("renderFirstCmd"))

    def test_the_block_never_opens_the_session(self):
        self.assertIn('box.addEventListener("click", (e) => e.stopPropagation())',
                      _fn("aboutBlock"))
        self.assertIn('e.target.closest("details, a")', _fn("jobHistRow"))

    def test_unlanded_cards_are_titled_by_the_subject(self):
        f = _fn("runItems")
        self.assertIn("title: o.subject || (o.branch", f)

    def test_the_styles_exist(self):
        for sel in (".run-pr", ".run-pr.merged", ".run-about", ".cc-fc-about"):
            self.assertIn(sel + " ", STYLE + " ")


if __name__ == "__main__":
    unittest.main()

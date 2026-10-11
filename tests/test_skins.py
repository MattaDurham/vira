"""Skins: the :root rewrite (value-only, comment-preserving), manifest
validation, the apply/reset/idempotency semantics against a throwaway tree,
path safety, the HTTP routes, the served stylesheets that wear the applied
skin while the tracked files stay as committed, the move of a skin applied
the old way (into the tracked files) into the store, and the shipped-state
invariant that guards against ever committing a skinned style.css.

Run: .venv/bin/python -m unittest tests.test_skins
"""
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import jsonstore, skins

REPO = Path(__file__).resolve().parents[1]

SMALL_CSS = """/* header */
:root {
  --bg: #0d0d0d;            /* Graphite */
  --text: #cfcbc2;
  --radius: 2px;            /* engineered, not designed */
  --pad: 16px;
}

body { background: var(--bg); }
"""


class RewriteRootTests(unittest.TestCase):
    def test_value_swaps_and_comment_survives(self):
        out = skins._rewrite_root(SMALL_CSS, {"--bg": "#030100", "--radius": "0"})
        self.assertIn("--bg: #030100;            /* Graphite */", out)
        self.assertIn("--radius: 0;            /* engineered, not designed */", out)
        self.assertNotIn("#0d0d0d", out)

    def test_unmentioned_tokens_and_outside_block_untouched(self):
        out = skins._rewrite_root(SMALL_CSS, {"--bg": "#030100"})
        self.assertIn("--text: #cfcbc2;", out)          # not in the change set
        self.assertIn("body { background: var(--bg); }", out)   # outside :root
        self.assertEqual(out.count("\n"), SMALL_CSS.count("\n"))  # no lines added

    def test_token_only_in_map_is_ignored(self):
        # a token the css does not declare must not be injected
        out = skins._rewrite_root(SMALL_CSS, {"--nonexistent": "#fff"})
        self.assertNotIn("--nonexistent", out)


class ManifestTests(unittest.TestCase):
    def test_real_base_loads_and_is_default(self):
        m = skins.load_manifest("darkmode")
        self.assertTrue(m.get("default"))
        self.assertEqual(m["id"], "darkmode")
        self.assertEqual(m["name"], "Dark Mode")
        self.assertEqual(m["genre"], "Taurid Capital")
        self.assertGreaterEqual(len(m["tokens"]), 60)

    def test_real_light_mode_loads_with_structure_and_valid_tokens(self):
        m = skins.load_manifest("light")
        self.assertEqual(m["name"], "Light Mode")
        self.assertEqual(m["genre"], "Card Grid Interface")
        self.assertEqual(m["register"], "A002")
        self.assertEqual(m["extras"], "light.css")
        self.assertEqual(m["tokens"]["--radius"], "14px")
        self.assertIn("rgba", m["tokens"]["--bg-card"])
        for k, v in m["tokens"].items():
            skins._check_token(k, v)

    def test_real_phosphor_loads_with_glass_and_valid_tokens(self):
        m = skins.load_manifest("phosphor-console")
        self.assertEqual(m["extras"], "phosphor-console.css")
        for k, v in m["tokens"].items():
            skins._check_token(k, v)                     # raises on anything bad

    def test_phosphor_overrides_all_exist_in_the_base(self):
        base = set(skins.load_manifest("darkmode")["tokens"])
        over = set(skins.load_manifest("phosphor-console")["tokens"])
        self.assertTrue(over <= base, over - base)

    def test_light_overrides_all_exist_in_the_base(self):
        base = set(skins.load_manifest("darkmode")["tokens"])
        over = set(skins.load_manifest("light")["tokens"])
        self.assertTrue(over <= base, over - base)

    def test_bad_id_and_unknown_id(self):
        with self.assertRaises(HTTPException) as e:
            skins.load_manifest("../secrets")
        self.assertEqual(e.exception.status_code, 400)
        with self.assertRaises(HTTPException) as e:
            skins.load_manifest("nope")
        self.assertEqual(e.exception.status_code, 404)

    def test_manifest_path_cannot_escape(self):
        for bad in ("..", "a/b", "a.b", "A", "", "with/slash"):
            with self.assertRaises(HTTPException):
                skins._manifest_path(bad)

    def test_check_token_rejects_comment_sequence(self):
        # a value carrying /* or */ would comment out following :root lines
        for bad in ("#fff /* x", "#fff */ }", "/* wipe"):
            with self.assertRaises(ValueError):
                skins._check_token("--bg", bad)
        skins._check_token("--bg", "#030100")            # a clean value is fine


class _Tree:
    """A throwaway static/skins world with the real manifests + a small
    style.css, wired into the module globals."""
    def __init__(self, tmp: Path):
        self.tmp = tmp
        (tmp / "static" / "skins").mkdir(parents=True)
        (tmp / "data").mkdir()
        real = REPO / "static" / "skins"
        for name in ("darkmode.json", "light.json", "light.css",
                     "phosphor-console.json", "phosphor-console.css",
                     "living-garden.json", "neon-pond.json"):
            shutil.copy(real / name, tmp / "static" / "skins" / name)
        # a style.css whose :root carries the base values for the tokens we assert on
        base = json.loads((real / "darkmode.json").read_text(encoding="utf-8"))["tokens"]
        lines = [":root {"]
        for k in ("--bg", "--text", "--radius", "--accent"):
            lines.append(f"  {k}: {base[k]};")
        lines += ["}", "", "body { color: var(--text); }", ""]
        # utf-8 explicitly, matching what _write_text_atomic writes: the fixture
        # stands in for the tracked stylesheets, and _ACTIVE_HEADER carries an
        # em dash. Left bare, Windows encodes it cp1252 (0x97) and apply_skin's
        # utf-8 read then fails on a file the test itself wrote.
        (tmp / "static" / "style.css").write_text("\n".join(lines),
                                                  encoding="utf-8")
        (tmp / "static" / "skin-active.css").write_text(skins._ACTIVE_HEADER,
                                                        encoding="utf-8")

    def patches(self):
        s = self.tmp / "static"
        return mock.patch.multiple(
            skins,
            SKINS_DIR=s / "skins",
            STYLE_CSS=s / "style.css",
            SKIN_ACTIVE=s / "skin-active.css",
            STATE=self.tmp / "data" / "active-skin.json",
        )


class _TreeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.tree = _Tree(self.tmp)
        self.enterContext(self.tree.patches())
        # bytes, and their text with line endings as on disk: a Windows run
        # writes the fixture with CRLF, and the served sheets keep it
        self.stock_style = skins.STYLE_CSS.read_bytes()
        self.stock_glass = skins.SKIN_ACTIVE.read_bytes()
        self.stock_text = self.stock_style.decode("utf-8")
        self.stock_glass_text = self.stock_glass.decode("utf-8")

    def _style(self):
        """What the browser receives for /style.css."""
        return skins.served_style()

    def assertTrackedUntouched(self):
        self.assertEqual(skins.STYLE_CSS.read_bytes(), self.stock_style)
        self.assertEqual(skins.SKIN_ACTIVE.read_bytes(), self.stock_glass)


class ApplyTests(_TreeCase):
    def test_living_skin_returns_preset_with_the_applied_tokens(self):
        for skin_id, look in (("living-garden", "natural"), ("neon-pond", "wireframe")):
            out = skins.apply_skin(skin_id)
            self.assertEqual(out["background"]["look"], look)
            self.assertEqual(out["background"]["scene"], "koi")
            self.assertEqual(skins.active_id(), skin_id)
            card = next(s for s in skins.list_skins()["skins"] if s["id"] == skin_id)
            self.assertEqual(card["background"], out["background"])
            self.assertIn(skins.load_manifest(skin_id)["tokens"]["--accent"], self._style())
        self.assertIsNone(skins.apply_skin("darkmode")["background"])

    def test_invalid_background_presets_fail_before_any_skin_write(self):
        path = skins.SKINS_DIR / "neon-pond.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        before = self._style()
        for key, value in (("look", "unknown"), ("dim", float("nan")),
                           ("paused", "false"), ("pond", "../other")):
            manifest = dict(original)
            manifest["background"] = {**original["background"], key: value}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(HTTPException):
                skins.apply_skin("neon-pond")
            self.assertEqual(self._style(), before)
            self.assertFalse(skins.STATE.exists())

    def test_background_preset_can_select_each_desktop_scene(self):
        path = skins.SKINS_DIR / "living-garden.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for scene in ("koi", "redwoods", "aurora", "constellation", "none"):
            manifest["background"]["scene"] = scene
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(skins.apply_skin("living-garden")["background"]["scene"], scene)

    def test_apply_phosphor_serves_root_and_glass_records_active(self):
        out = skins.apply_skin("phosphor-console")
        self.assertTrue(out["ok"])
        self.assertEqual(out["active"], "phosphor-console")
        self.assertNotIn("committed", out)              # apply is local, no git
        self.assertEqual(out["changed"], ["style.css", "skin-active.css"])
        self.assertIn("--bg: #030100;", self._style())
        self.assertIn("--radius: 0;", self._style())
        glass = skins.served_glass()
        self.assertIn("body::after", glass)              # the tube overlay
        self.assertIn("pc-flicker", glass)               # the glass animation
        self.assertEqual(skins.active_id(), "phosphor-console")

    def test_apply_darkmode_resets_to_stock(self):
        skins.apply_skin("phosphor-console")
        out = skins.apply_skin("darkmode")
        self.assertEqual(out["active"], "darkmode")
        self.assertEqual(self._style(), self.stock_text)
        self.assertIn("--bg: #0d0d0d;", self._style())   # base value served
        self.assertIn("--radius: 13px;", self._style())
        self.assertEqual(skins.served_glass(), self.stock_glass_text)
        self.assertEqual(skins.overlay(), ({}, None))
        self.assertEqual(skins.active_id(), "darkmode")

    def test_apply_light_serves_paper_tokens_and_structure_layer(self):
        out = skins.apply_skin("light")
        self.assertEqual(out["active"], "light")
        self.assertIn("--bg: #f2eee3;", self._style())
        self.assertIn("--accent: #a53b36;", self._style())
        self.assertIn("--radius: 14px;", self._style())
        structure = skins.served_glass()
        self.assertIn("counter-reset: light-window", structure)
        self.assertIn(".fwin-title::before", structure)
        self.assertIn("backdrop-filter: blur(18px)", structure)
        self.assertEqual(skins.active_id(), "light")

    def test_the_served_sheet_is_what_the_old_in_place_rewrite_wrote(self):
        # the move must not change one byte the browser receives: the old
        # apply wrote base-plus-skin into :root, the new one serves the skin
        # over the shipped (base) sheet
        base = skins.load_manifest("darkmode")["tokens"]
        for skin_id in ("light", "phosphor-console", "neon-pond"):
            merged = {**base, **skins.load_manifest(skin_id)["tokens"]}
            skins.apply_skin(skin_id)
            self.assertEqual(self._style(), skins._rewrite_root(self.stock_text, merged))

    def test_applying_never_writes_a_tracked_file(self):
        for skin_id in ("light", "phosphor-console", "living-garden", "darkmode"):
            skins.apply_skin(skin_id)
            self.assertTrackedUntouched()

    def test_switching_skins_leaves_no_trace_of_the_last(self):
        # phosphor has glass, living-garden has none: its glass must go
        skins.apply_skin("phosphor-console")
        out = skins.apply_skin("living-garden")
        self.assertIn("skin-active.css", out["changed"])
        self.assertEqual(skins.served_glass(), self.stock_glass_text)
        self.assertNotIn("#030100", self._style())

    def test_apply_is_idempotent(self):
        skins.apply_skin("phosphor-console")
        again = skins.apply_skin("phosphor-console")
        self.assertEqual(again["changed"], [])           # nothing left to change
        self.assertEqual(skins.active_id(), "phosphor-console")

    def test_list_reflects_active(self):
        skins.apply_skin("phosphor-console")
        listing = skins.list_skins()
        self.assertEqual(listing["active"], "phosphor-console")
        ids = [s["id"] for s in listing["skins"]]
        self.assertEqual(ids[0], "darkmode")               # base/default first
        self.assertIn("light", ids)
        self.assertIn("phosphor-console", ids)

    def test_a_stored_id_whose_manifest_is_gone_reads_as_the_base(self):
        # skins get renamed and retired - this store held "taurid" before Dark
        # Mode took its own name. "taurid" still matches ID_RE, so without the
        # manifest check active_id would hand back an id that marks no card
        # active and 404s every caller that loads it.
        jsonstore.write_atomic(skins.STATE, {"id": "taurid"})
        self.assertEqual(skins.active_id(), skins.BASE_ID)
        listing = skins.list_skins()
        self.assertEqual(listing["active"], skins.BASE_ID)
        self.assertIn(listing["active"], [sk["id"] for sk in listing["skins"]])

    def test_a_retired_skin_stops_being_served(self):
        # the look agrees with the card the picker marks active
        jsonstore.write_atomic(skins.STATE, {"id": "taurid",
                                             "tokens": {"--bg": "#010203"},
                                             "glass": "body { color: red; }"})
        self.assertEqual(skins.overlay(), ({}, None))
        self.assertEqual(self._style(), self.stock_text)

    def test_a_stored_id_whose_manifest_exists_is_kept(self):
        # the guard above must not swallow a real applied skin
        jsonstore.write_atomic(skins.STATE, {"id": "phosphor-console"})
        self.assertEqual(skins.active_id(), "phosphor-console")

    def test_a_hand_edited_token_that_could_break_out_is_not_served(self):
        # the store is a plain file under data/; its values reach the CSS
        jsonstore.write_atomic(skins.STATE, {
            "id": "light", "glass": None,
            "tokens": {"--bg": "#fff; } body { display: none",
                       "--text": "#123456"}})
        with self.assertLogs("server.skins", level="WARNING") as logs:
            tokens, _ = skins.overlay()
            served = self._style()
        self.assertEqual(tokens, {"--text": "#123456"})
        self.assertNotIn("display: none", served)
        self.assertIn("--text: #123456;", served)
        self.assertIn("--bg", logs.output[0])              # named, not dropped silently

    def test_an_oversized_stored_glass_is_not_served(self):
        jsonstore.write_atomic(skins.STATE, {
            "id": "light", "tokens": {},
            "glass": "a" * (skins.MAX_EXTRAS_BYTES + 1)})
        with self.assertLogs("server.skins", level="WARNING"):
            self.assertEqual(skins.served_glass(), self.stock_glass_text)


class LegacyMoveTests(_TreeCase):
    """Before 2026-10-11 an apply wrote the look into the tracked files and
    recorded only the id. The first boot of the new code moves that look into
    the store and restores the files, without changing what is served."""

    def _apply_the_old_way(self, skin_id, tweak=None):
        """What the pre-store apply did: a universal-newline read, a
        text-mode write (CRLF on Windows). Returns the files as on disk."""
        base = skins.load_manifest("darkmode")["tokens"]
        skin = skins.load_manifest(skin_id)
        merged = {**base, **skin["tokens"], **(tweak or {})}
        stock = skins.STYLE_CSS.read_text(encoding="utf-8")
        skins.STYLE_CSS.write_text(skins._rewrite_root(stock, merged), encoding="utf-8")
        skins.SKIN_ACTIVE.write_text(skins._glass(skin) or skins._ACTIVE_HEADER,
                                     encoding="utf-8")
        jsonstore.write_atomic(skins.STATE, {"id": skin_id})
        return (skins.STYLE_CSS.read_bytes().decode("utf-8"),
                skins.SKIN_ACTIVE.read_bytes().decode("utf-8"))

    def _quiet(self):
        return self.assertLogs("server.skins", level="WARNING")

    def test_the_look_moves_and_the_tracked_files_come_back_clean(self):
        old_style, old_glass = self._apply_the_old_way("light")
        with self._quiet():
            out = skins.migrate_legacy()
        self.assertEqual(out["source"], "tracked files")
        self.assertEqual(sorted(out["restored"]),
                         ["static/skin-active.css", "static/style.css"])
        self.assertTrackedUntouched()                    # back to the shipped bytes
        self.assertEqual(self._style(), old_style)       # and the look is unchanged
        self.assertEqual(skins.served_glass(), old_glass)
        self.assertEqual(skins.active_id(), "light")

    def test_values_tuned_after_the_apply_survive_the_move(self):
        old_style, _ = self._apply_the_old_way("light", tweak={"--accent": "#00ff88"})
        with self._quiet():
            skins.migrate_legacy()
        self.assertIn("--accent: #00ff88;", self._style())
        self.assertEqual(self._style(), old_style)
        self.assertTrackedUntouched()

    def test_a_skin_without_glass_moves_only_its_tokens(self):
        old_style, _ = self._apply_the_old_way("living-garden")
        with self._quiet():
            out = skins.migrate_legacy()
        self.assertEqual(out["restored"], ["static/style.css"])
        self.assertFalse(out["glass"])
        self.assertEqual(self._style(), old_style)
        self.assertTrackedUntouched()

    def test_files_set_aside_for_a_merge_rebuild_from_the_manifest(self):
        # the owner's landing: the dirty files were reset so the merge could
        # run, leaving only the recorded id
        jsonstore.write_atomic(skins.STATE, {"id": "light"})
        with self._quiet():
            out = skins.migrate_legacy()
        self.assertEqual(out["source"], "manifest")
        self.assertEqual(out["restored"], [])
        self.assertEqual(skins.served_glass(), skins._glass(skins.load_manifest("light")))
        self.assertIn("--bg: #f2eee3;", self._style())
        self.assertTrackedUntouched()

    def test_it_runs_once(self):
        self._apply_the_old_way("phosphor-console")
        with self._quiet():
            skins.migrate_legacy()
        state = skins.STATE.read_bytes()
        self.assertIsNone(skins.migrate_legacy())
        self.assertEqual(skins.STATE.read_bytes(), state)

    def test_a_dirty_sheet_without_an_old_apply_record_is_left_alone(self):
        # an uncommitted :root edit on a branch is someone's work, not a skin
        edited = skins.STYLE_CSS.read_text(encoding="utf-8").replace(
            "--radius: 13px;", "--radius: 21px;")
        skins.STYLE_CSS.write_text(edited, encoding="utf-8")
        for state in (None, {"id": "darkmode"},
                      {"id": "light", "tokens": {}, "glass": None}):
            with self.subTest(state=state):
                skins.STATE.unlink(missing_ok=True)
                if state is not None:
                    jsonstore.write_atomic(skins.STATE, state)
                self.assertIsNone(skins.migrate_legacy())
                self.assertEqual(skins.STYLE_CSS.read_text(encoding="utf-8"), edited)
                self.assertEqual(skins.SKIN_ACTIVE.read_bytes(), self.stock_glass)

    def test_a_retired_skin_with_stock_files_has_nothing_to_move(self):
        jsonstore.write_atomic(skins.STATE, {"id": "taurid"})
        self.assertIsNone(skins.migrate_legacy())
        self.assertEqual(skins.STATE.read_text(encoding="utf-8"), '{"id": "taurid"}')

    def test_a_retired_skin_left_in_the_files_is_kept_but_not_worn(self):
        # its values move into the store (nothing discarded) and the files
        # come back clean; like every retired id it then reads as the base
        old_style, _ = self._apply_the_old_way("phosphor-console")
        jsonstore.write_atomic(skins.STATE, {"id": "taurid"})
        with self._quiet():
            out = skins.migrate_legacy()
        self.assertEqual(out["source"], "tracked files")
        self.assertTrackedUntouched()
        kept = json.loads(skins.STATE.read_text(encoding="utf-8"))
        self.assertEqual(kept["tokens"]["--bg"], "#030100")
        self.assertIn("pc-flicker", kept["glass"])
        self.assertEqual(skins.active_id(), skins.BASE_ID)
        self.assertEqual(self._style(), self.stock_text)

    def test_applying_the_base_on_an_unmoved_install_still_resets_to_stock(self):
        # the reset the updater hint used to send people to must keep working
        self._apply_the_old_way("phosphor-console")
        with self._quiet():
            skins.apply_skin("darkmode")
        self.assertTrackedUntouched()
        self.assertEqual(self._style(), self.stock_text)
        self.assertEqual(skins.served_glass(), self.stock_glass_text)

    def test_an_interrupted_move_still_serves_the_look(self):
        # the store is written first: if restoring the files fails, the look
        # is in both places, never in neither
        old_style, old_glass = self._apply_the_old_way("light")
        real = skins._write_text_atomic
        calls = []

        def second_write_fails(path, text, **kw):
            calls.append(path)
            if len(calls) > 1:
                raise OSError("read-only")
            real(path, text, **kw)

        with mock.patch.object(skins, "_write_text_atomic", second_write_fails):
            with self.assertRaises(OSError):
                skins.migrate_legacy()
        self.assertEqual(len(calls), 2)                  # one file restored, one not
        self.assertEqual(self._style(), old_style)
        self.assertEqual(skins.served_glass(), old_glass)


class SyncBaseTests(unittest.TestCase):
    """The Design Studio writes style.css; this is what keeps the base skin
    manifest from drifting away from it. Before it existed, every token the
    owner changed and saved broke ShippedStateInvariant."""
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.tree = _Tree(self.tmp)
        self.enterContext(self.tree.patches())

    def _base(self):
        return json.loads((self.tmp / "static" / "skins" /
                           f"{skins.BASE_ID}.json").read_text(encoding="utf-8"))["tokens"]

    def _style(self):
        return (self.tmp / "static" / "style.css").read_text(encoding="utf-8")

    def test_a_changed_value_lands_in_the_base(self):
        new = self._style().replace("--radius: 13px;", "--radius: 21px;")
        self.assertTrue(skins.sync_base_from_style(new))
        self.assertEqual(self._base()["--radius"], "21px")

    def test_the_sync_makes_the_invariant_hold_again(self):
        # the whole point: applying the base to the new sheet is a no-op
        new = self._style().replace("--radius: 13px;", "--radius: 21px;")
        self.assertNotEqual(skins._rewrite_root(new, self._base()), new)   # broken first
        skins.sync_base_from_style(new)
        self.assertEqual(skins._rewrite_root(new, self._base()), new)      # and repaired

    def test_a_token_new_to_root_is_added(self):
        new = self._style().replace(":root {", ":root {\n  --brand-new: #123456;")
        self.assertTrue(skins.sync_base_from_style(new))
        self.assertEqual(self._base()["--brand-new"], "#123456")

    def test_a_base_token_absent_from_root_is_left_alone(self):
        # the fixture sheet declares 4 of the 69; a skin may override tokens
        # the stock sheet does not carry, so absence must never mean delete
        before = self._base()
        skins.sync_base_from_style(self._style().replace("--bg: #0d0d0d;", "--bg: #010203;"))
        after = self._base()
        self.assertGreater(len(after), 60)
        for k in before:
            if k not in ("--bg",):
                self.assertEqual(after[k], before[k])

    def test_no_change_writes_nothing(self):
        path = self.tmp / "static" / "skins" / f"{skins.BASE_ID}.json"
        before = path.read_bytes()
        self.assertFalse(skins.sync_base_from_style(self._style()))
        self.assertEqual(path.read_bytes(), before)

    def test_a_sheet_with_no_root_block_writes_nothing(self):
        path = self.tmp / "static" / "skins" / f"{skins.BASE_ID}.json"
        before = path.read_bytes()
        self.assertFalse(skins.sync_base_from_style("body { color: red; }"))
        self.assertEqual(path.read_bytes(), before)

    def test_a_value_we_would_refuse_to_read_is_never_written(self):
        path = self.tmp / "static" / "skins" / f"{skins.BASE_ID}.json"
        before = path.read_bytes()
        bad = self._style().replace("--bg: #0d0d0d;", "--bg: red /* wipe;")
        with self.assertRaises(ValueError):
            skins.sync_base_from_style(bad)
        self.assertEqual(path.read_bytes(), before)   # nothing half-written


class NoGitTests(_TreeCase):
    """Applying a skin is local only — it must never invoke git, so a
    downloaded copy (whose origin is a repo it can't push to) is never asked
    to commit or push."""

    def test_apply_never_touches_git(self):
        with mock.patch.object(skins.designstudio, "commit_and_push") as spy:
            out = skins.apply_skin("phosphor-console")
        spy.assert_not_called()
        self.assertNotIn("committed", out)
        self.assertNotIn("pushed", out)
        self.assertIn("style.css", out["changed"])       # the local change happened
        self.assertIn("--bg: #030100;", self._style())


class RouteTests(_TreeCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(skins.router)
        app.include_router(skins.assets)
        self.client = TestClient(app)

    def test_get_lists(self):
        r = self.client.get("/api/skins")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active"], "darkmode")
        self.assertGreaterEqual(len(r.json()["skins"]), 3)

    def test_post_apply_and_unknown(self):
        r = self.client.post("/api/skins/phosphor-console/apply")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["active"], "phosphor-console")
        self.assertEqual(self.client.post("/api/skins/nope/apply").status_code, 404)

    def test_the_stylesheets_are_served_wearing_the_skin(self):
        self.client.post("/api/skins/phosphor-console/apply")
        r = self.client.get("/style.css")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/css"))
        self.assertIn("--bg: #030100;", r.text)
        self.assertIn("body::after", self.client.get("/skin-active.css").text)
        self.assertTrackedUntouched()

    def test_with_no_skin_the_tracked_files_are_served(self):
        self.assertEqual(self.client.get("/style.css").content, self.stock_style)
        self.assertEqual(self.client.get("/skin-active.css").content, self.stock_glass)

    def test_an_unchanged_sheet_revalidates_to_a_304(self):
        first = self.client.get("/style.css")
        etag = first.headers["etag"]
        again = self.client.get("/style.css", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.client.post("/api/skins/light/apply")       # a new look is a new tag
        after = self.client.get("/style.css", headers={"If-None-Match": etag})
        self.assertEqual(after.status_code, 200)
        self.assertNotEqual(after.headers["etag"], etag)

    def test_head_is_answered(self):
        r = self.client.head("/skin-active.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn("etag", r.headers)


class MainAppRoutingTests(_TreeCase):
    """The join: in the real app the skin routes must win over the static
    mount, which would otherwise answer both paths from disk."""

    def test_main_serves_the_skinned_stylesheets(self):
        from server import main
        client = TestClient(main.app)
        skins.apply_skin("light")
        r = client.get("/style.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn("--bg: #f2eee3;", r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-cache")
        self.assertIn("counter-reset: light-window",
                      client.get("/skin-active.css").text)


class ShippedStateInvariant(unittest.TestCase):
    """The repo must never ship a skinned style.css: applying the base to the
    committed stylesheet is a no-op, and the committed skin-active.css carries
    no rules. This fails loudly if a visual-verification apply was not reset."""
    def test_shipped_style_is_the_base(self):
        style = (REPO / "static" / "style.css").read_text(encoding="utf-8")
        base = skins.load_manifest("darkmode")["tokens"]
        self.assertEqual(skins._rewrite_root(style, base), style)

    def test_base_covers_every_shipped_root_token(self):
        # the reset/idempotency guarantee rests on the base carrying every
        # token line in the shipped :root — enforce it, don't assume it.
        import re
        style = (REPO / "static" / "style.css").read_text(encoding="utf-8")
        open_i = next(i for i, l in enumerate(style.split("\n"))
                      if re.match(r"^:root\s*{", l))
        lines = style.split("\n")
        depth = 0
        names = set()
        for j in range(open_i, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            m = re.match(r"^\s*--([a-z0-9-]+)\s*:", lines[j])
            if m:
                names.add("--" + m.group(1))
            if depth == 0 and j > open_i:
                break
        base = set(skins.load_manifest("darkmode")["tokens"])
        self.assertTrue(names <= base, names - base)

    def test_shipped_skin_active_has_no_rules(self):
        active = (REPO / "static" / "skin-active.css").read_text(encoding="utf-8")
        self.assertEqual(active, skins._ACTIVE_HEADER)   # exactly the reset content
        import re
        stripped = re.sub(r"/\*.*?\*/", "", active, flags=re.S)
        self.assertNotIn("{", stripped)                  # no CSS rules, only comments


if __name__ == "__main__":
    unittest.main()

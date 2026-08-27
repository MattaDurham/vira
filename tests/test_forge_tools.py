"""Contract: the Forge's edit toolbar, and the join that makes undo record.

The Forge grew an edit toolbar (undo / redo / copy / paste / duplicate /
delete) beside Save and Save as. Three things about it are load-bearing and
fail SILENTLY -- the button still renders, the shortcut still fires, and
nothing throws -- so each one gets a case here rather than a paragraph.

(a) A button in the markup with no listener in forge.js is a dead control.
    That is the reader-with-no-writer shape this repo keeps hitting (the
    branch guard's dropped spec fields, `model_used`, the doctags Indexer
    that shipped built and never started). The scan is DERIVED from the
    markup, so a seventh tool button is covered without anyone remembering
    this file exists.

(b) `setDirty()` is the Forge's one mutation choke point -- forty-odd edit
    sites already call it -- so the undo stack hangs off it. Move the hook
    out and undo keeps its buttons, its shortcuts and its stack, and simply
    records nothing. Nothing else in the app would notice.

(c) A snapshot is the DOCUMENT. Server-owned identity (id, revision,
    builtin, created, updated) must never make the round trip: `saveFlow`
    replaces `state.current` with the server's response, so a snapshot
    carrying `id` could restore content under the wrong record, and one
    carrying `revision` would silently rewind the version the next PUT
    claims to be editing.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
FORGE = (STATIC / "forge.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "forge.css").read_text(encoding="utf-8")

# Fields the server owns. A restore that writes any of these re-points the
# open Flow at a record the owner did not ask for.
SERVER_OWNED = ("id", "revision", "builtin", "created", "updated")


def _block(src: str, marker: str, opener: str, closer: str) -> str:
    """`src` from `marker` through the matching `closer`."""
    start = src.index(marker)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced %r from %r" % (closer, marker))


def _fn(name: str) -> str:
    """The body of a named function declaration in forge.js."""
    return _block(FORGE, "function %s(" % name, "{", "}")


def _toolbar_ids() -> list:
    """Every button id inside the toolbar's edit-tool group, from the markup."""
    start = HTML.index('<div class="forge-tools"')
    end = HTML.index("</div>", HTML.index('id="forge-delete"'))
    return re.findall(r'<button[^>]*\bid="([^"]+)"', HTML[start:end])


class ToolbarIsWired(unittest.TestCase):
    """(a) Every tool button exists, is styled, and has a listener."""

    def test_the_scan_reaches_the_toolbar(self):
        # Guards the three cases below: a scan that matches nothing would
        # pass every assertion in this class vacuously.
        ids = _toolbar_ids()
        self.assertEqual(
            ids,
            ["forge-undo", "forge-redo", "forge-copy", "forge-paste",
             "forge-duplicate", "forge-delete"],
            "the edit-tool group in index.html is not what this file scans",
        )

    def test_every_tool_button_has_a_click_listener(self):
        bind = _fn("bind")
        for button in _toolbar_ids():
            with self.subTest(button=button):
                self.assertRegex(
                    bind,
                    r'q\("#%s"\)\.addEventListener\("click"' % re.escape(button),
                    "%s renders but nothing binds it -- a dead control" % button,
                )

    def test_every_tool_button_is_reachable_without_a_mouse(self):
        start = HTML.index('<div class="forge-tools"')
        end = HTML.index("</div>", HTML.index('id="forge-delete"'))
        for tag in re.findall(r"<button[^>]*>", HTML[start:end]):
            with self.subTest(tag=tag[:60]):
                # The face is an icon, so the accessible name is the label.
                self.assertIn("aria-label=", tag)
                self.assertIn("title=", tag)

    def test_the_group_is_styled(self):
        # Unstyled, the icons fall back to browser chrome -- the documented
        # `.seg` / `.linkish` trap.
        self.assertIn(".forge-tool {", CSS)
        self.assertIn(".forge-tool:disabled", CSS)


class SetDirtyRecordsHistory(unittest.TestCase):
    """(b) The join between the mutation choke point and the undo stack."""

    def test_set_dirty_marks_history(self):
        body = _fn("setDirty")
        self.assertIn("historyMark()", body,
                      "setDirty no longer records undo entries; every edit "
                      "site still calls it, so undo would silently do nothing")
        self.assertIn("historyReset()", body,
                      "setDirty(false) must reset the stack -- a load or save "
                      "replaces the document")

    def test_set_dirty_keeps_the_buttons_honest(self):
        # Undo/redo enablement is derived from stack depth, and setDirty is
        # the only thing every mutation calls.
        self.assertIn("syncTools()", _fn("setDirty"))

    def test_undo_and_redo_read_opposite_stacks(self):
        body = _fn("stepHistory")
        self.assertIn("back ? history.past : history.future", body)
        self.assertIn("back ? history.future : history.past", body)

    def test_typing_coalesces(self):
        # Text controls fire change() per keystroke, so without this one
        # sentence is sixty undo entries and evicts every structural edit
        # behind it (HISTORY_MAX).
        body = _fn("historyMark")
        self.assertIn("COALESCE_MS", body)
        self.assertIn("editKey()", body)

    def test_the_stack_is_bounded(self):
        self.assertIn("HISTORY_MAX", _fn("historyMark"))


class SnapshotsCarryContentOnly(unittest.TestCase):
    """(c) Identity is the server's; a snapshot must not round-trip it."""

    def test_snapshot_omits_server_owned_fields(self):
        body = _fn("docSnapshot")
        for field in SERVER_OWNED:
            with self.subTest(field=field):
                self.assertNotRegex(
                    body, r"\b%s:\s*flow\." % re.escape(field),
                    "docSnapshot captures %r, which the server owns" % field,
                )

    def test_restore_writes_content_only(self):
        body = _fn("restoreDoc")
        written = set(re.findall(r"flow\.(\w+)\s*=", body))
        self.assertTrue(written, "restoreDoc assigns nothing -- undo is inert")
        leaked = written & set(SERVER_OWNED)
        self.assertFalse(
            leaked,
            "restoreDoc writes server-owned %s: an undo would re-point the "
            "open Flow at another record" % sorted(leaked),
        )

    def test_restore_covers_everything_the_save_sends(self):
        # If a field is persisted but never restored, undo would appear to
        # work and then the next save would write the un-undone value.
        sent = set(re.findall(r"(\w+):\s*(?:copy\()?flow\??\.", _fn("flowPayload")))
        restored = set(re.findall(r"flow\.(\w+)\s*=", _fn("restoreDoc")))
        missing = (sent - restored) - set(SERVER_OWNED)
        self.assertFalse(
            missing,
            "saveFlow persists %s but restoreDoc never restores it" % sorted(missing),
        )


class ShortcutsYieldToTextFields(unittest.TestCase):
    """A document-level handler must not hijack typing."""

    def test_the_handler_bails_inside_editable_controls(self):
        body = _fn("forgeKeys")
        self.assertIn("input, textarea, select, [contenteditable='true']", body)
        # Cmd+Z inside a field belongs to the field's own undo.
        self.assertRegex(body, r'key === "z".*\n?.*if \(typing\) return;')

    def test_copy_never_steals_a_text_selection(self):
        self.assertIn("isCollapsed", _fn("forgeKeys"))

    def test_the_handler_bails_when_the_forge_is_not_showing(self):
        self.assertIn("forgeShowing()", _fn("forgeKeys"))
        self.assertIn("getClientRects()", _fn("forgeShowing"))


if __name__ == "__main__":
    unittest.main()

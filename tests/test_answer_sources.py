"""Versioned source reads over synthetic files and SQLite stores only."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from server import answer_runtime, answer_sources as sources, imessage, settings, vault, viratools


class SourceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = {"answer_source_policy": {}}
        self.specs = [{"id": "primary", "root": self.vault, "name": "Test vault", "primary": True,
                       "read_enabled": True, "model_exposure": True, "model_exclude_dirs": []}]
        patches = [mock.patch.object(sources, "STORE", self.root / "evidence"),
                   mock.patch.object(settings, "raw", side_effect=lambda: self.config),
                   mock.patch.object(settings, "fixture_mode", return_value=False),
                   mock.patch.object(settings, "sandboxed", return_value=False),
                   mock.patch.object(vault, "source_specs", side_effect=lambda: self.specs),
                   mock.patch.object(vault, "note_text", side_effect=self.note_text)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def note_text(self, path, **kwargs):
        spec, target = sources._vault_info(path, kwargs.get("for_model", False))
        return target.read_text(encoding="utf-8")

    def note(self, path, text):
        p = self.vault / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return "vault:" + path


class VersionedReads(SourceBase):
    def test_only_complete_original_reads_expose_the_replacement_digest(self):
        source = self.note("editable.md", "complete original text")
        whole = sources.read_source(source)
        self.assertEqual(whole["sha256"], sources._hash(whole["text"]))
        for page in (sources.read_source(source, length=8),
                     sources.read_source(source, start=9),
                     sources.read_source(source, start=1000)):
            self.assertTrue(page["truncated"])
            self.assertNotIn("sha256", page)
            self.assertEqual(page["version"], whole["version"])
            reopened = sources.evidence(page["evidence_handle"])
            self.assertEqual(reopened["text"], page["text"])
            self.assertNotIn("sha256", reopened)
        derived = sources.capture_result("available excerpt", tool_name="find")
        self.assertFalse(derived["truncated"])
        self.assertFalse(derived["source_complete"])
        self.assertNotIn("sha256", derived)
        self.assertNotIn("sha256", sources.evidence(derived["evidence_handle"]))

    def test_pagination_reconstructs_full_long_source_without_silent_tail_loss(self):
        full = "alpha\n" * 5000 + "final evidence"
        source = self.note("long.md", full)
        first = sources.read_source(source, length=317)
        pages, page = [first["text"]], first
        while page["continuation"]:
            page = sources.read_source(**page["continuation"])
            pages.append(page["text"])
        self.assertEqual("".join(pages), full)
        self.assertEqual(first["full_length"], len(full))
        self.assertEqual(first["span"], {"start": 0, "end": 317, "unit": "characters"})
        self.assertTrue(first["truncated"])
        self.assertIsNone(page["continuation"])

    def test_an_edited_or_removed_source_does_not_change_a_cited_version(self):
        source = self.note("record.md", "original statement")
        first = sources.read_source(source)
        self.note("record.md", "corrected statement")
        second = sources.read_source(source)
        self.assertNotEqual(first["version"], second["version"])
        self.assertEqual(sources.evidence(first["evidence_handle"])["text"], "original statement")
        (self.vault / "record.md").unlink()
        self.assertEqual(sources.evidence(first["evidence_handle"])["text"], "original statement")
        self.assertTrue((sources.STORE / "versions").is_dir())

    def test_citation_reopens_only_the_read_span(self):
        source = self.note("record.md", "before TARGET after")
        page = sources.read_source(source, start=7, length=6)
        exact = sources.evidence(page["evidence_handle"])
        self.assertEqual(exact["text"], "TARGET")
        self.assertEqual(exact["evidence_handle"], page["evidence_handle"])

    def test_source_date_is_not_invented_from_file_mtime(self):
        dated = sources.read_source(self.note("dated.md", "---\npublished: 2020-01-02\n---\nA source"))
        undated = sources.read_source(self.note("undated.md", "A source"))
        self.assertEqual(dated["source_date"], "2020-01-02")
        self.assertNotEqual(dated["source_date"], dated["modified_at"])
        self.assertIsNone(undated["source_date"])
        self.assertIsNotNone(undated["modified_at"])

    def test_batch_preserves_order_and_reports_missing_sources_without_dropping_good_ones(self):
        a, b = self.note("a.md", "A"), self.note("b.md", "B")
        got = sources.read_many([{"source": a}, {"source": "vault:missing.md"}, {"source": b}])
        self.assertEqual([r.get("text") for r in got["results"]], ["A", None, "B"])
        self.assertIn("error", got["results"][1])
        self.assertFalse(got["complete"])

    def test_within_source_search_returns_exact_offsets_and_exhaustible_matches(self):
        source = self.note("search.md", "lead Straße TARGET one target two TARGET end")
        first = sources.search_source(source, "target", limit=1, context=2)
        matches = first["matches"][:]
        page = first
        while page["continuation"]:
            page = sources.search_source(**page["continuation"], limit=1, context=2)
            matches.extend(page["matches"])
        self.assertEqual(len(matches), 3)
        full = (self.vault / "search.md").read_text(encoding="utf-8")
        for match in matches:
            span = match["match_span"]
            self.assertEqual(full[span["start"]:span["end"]].lower(), "target")
            self.assertEqual(sources.evidence(match["evidence_handle"])["text"], match["text"])

    def test_source_families_follow_explicit_urls_not_shared_filenames(self):
        a = sources.read_source(self.note("one/same.md", "---\nsource: https://example.test/one\n---\nA"))
        b = sources.read_source(self.note("two/same.md", "---\nsource: https://example.test/two\n---\nB"))
        c = sources.read_source(self.note("different.md", '---\nderived_from: ["https://example.test/one"]\n---\nSummary'))
        self.assertNotEqual(a["provenance"]["family_ids"], b["provenance"]["family_ids"])
        self.assertEqual(a["provenance"]["family_ids"], c["provenance"]["family_ids"])
        self.assertEqual(c["provenance"]["kind"], "derived")

    def test_historical_is_an_explicit_provenance_fact_not_a_filename_guess(self):
        a = sources.read_source(self.note("old-report.md", "Still current"))
        b = sources.read_source(self.note("fresh.md", "---\nhistorical: true\n---\nOld version"))
        self.assertEqual(a["provenance"]["kind"], "original")
        self.assertEqual(b["provenance"]["kind"], "historical")


class Exposure(SourceBase):
    def test_reopened_versions_obey_new_model_policy(self):
        page = sources.read_source(self.note("note.md", "restricted later"))
        self.specs[0]["model_exposure"] = False
        with self.assertRaisesRegex(ValueError, "model access"):
            sources.evidence(page["evidence_handle"])
        self.assertEqual(sources.evidence(page["evidence_handle"], for_model=False)["text"], "restricted later")

    def test_derived_tool_snapshot_inherits_all_source_policies(self):
        self.note("note.md", "source")
        page = sources.capture_result("result", tool_name="find", policy_paths=["note.md"])
        self.specs[0]["model_exposure"] = False
        with self.assertRaisesRegex(ValueError, "model access"):
            sources.evidence(page["evidence_handle"])

    def test_hidden_folder_and_outside_symlink_cannot_be_read_or_reopened(self):
        self.note("private/secret.md", "secret")
        self.specs[0]["model_exclude_dirs"] = ["private"]
        with self.assertRaises(ValueError):
            sources.read_source("vault:private/secret.md")
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        try:
            (self.vault / "alias.md").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            sources.read_source("vault:alias.md")
        with self.assertRaises(ValueError):
            sources.read_source("vault:../outside.md")

    def test_conversation_scope_restricts_reads_reopens_and_enumeration(self):
        page = sources.read_source(self.note("note.md", "vault source"))
        with answer_runtime.scope({"evidence_scope": {"sources": ["imessage"]}}):
            with self.assertRaisesRegex(ValueError, "scope"):
                sources.evidence(page["evidence_handle"])
            ids = [r["id"] for r in sources.enumerate_sources()["sources"]]
            self.assertEqual(ids, ["imessage"])
        with answer_runtime.scope({"evidence_scope": ["vault:primary"]}):
            self.assertEqual(sources.evidence(page["evidence_handle"])["text"], "vault source")
            with self.assertRaises(ValueError):
                sources._require("mail")

    def test_inventory_omits_hidden_source_names_for_models(self):
        self.specs[0]["model_exposure"] = False
        model = sources.enumerate_sources()
        self.assertNotIn("Test vault", json.dumps(model))
        owner = sources.enumerate_sources(for_model=False)
        self.assertIn("Test vault", json.dumps(owner))
        self.assertFalse(owner["sources"][0]["model_exposure"])

    def test_invalid_handles_cannot_escape_the_evidence_store(self):
        for value in ("../../secret", "ev_../secret", "ev_" + "0" * 64):
            with self.assertRaises(ValueError):
                sources.evidence(value)


class MessageRanges(SourceBase):
    def setUp(self):
        super().setUp()
        self.db = self.root / "chat.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY,date INTEGER,is_from_me INTEGER,text TEXT,
                             attributedBody BLOB,handle_id INTEGER,associated_message_type INTEGER);
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY,id TEXT);
        CREATE TABLE chat(ROWID INTEGER PRIMARY KEY,style INTEGER);
        CREATE TABLE chat_handle_join(chat_id INTEGER,handle_id INTEGER);
        CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);
        INSERT INTO handle VALUES(1,'synthetic@example.test');
        INSERT INTO chat VALUES(1,45),(2,45);
        INSERT INTO chat_handle_join VALUES(1,1);
        """)
        base = imessage.apple_ns(datetime(2026, 1, 1, tzinfo=timezone.utc))
        rows = [(1, base, 0, "one", None, 1, 0), (2, base, 1, "two outgoing", None, None, 0),
                (3, base + 1_000_000_000, 0, "three", None, 1, 0),
                (4, base + 86_400_000_000_000, 0, "next day", None, 1, 0),
                (5, base, 0, "other chat", None, 1, 0), (6, base, 0, "tapback", None, 1, 2000)]
        con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO chat_message_join VALUES(?,?)", [(1, i) for i in (1,2,3,4,6)] + [(2,5)])
        con.commit(); con.close()
        patch = mock.patch.object(imessage, "CHAT_DB", self.db)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch("server.data._load", return_value={"by_id": {"person": {"handles": {"imessage": ["synthetic@example.test"]}}}})
        patch.start(); self.addCleanup(patch.stop)

    def test_exact_date_range_pages_ties_without_losing_outgoing_messages(self):
        page = sources.thread_range("person", "2026-01-01", "2026-01-02", limit=1)
        ids = [m["rowid"] for m in page["messages"]]
        while page["continuation"]:
            page = sources.thread_range(**page["continuation"])
            ids.extend(m["rowid"] for m in page["messages"])
        self.assertEqual(ids, [1, 2, 3])
        self.assertTrue(page["complete"])

    def test_exact_surroundings_stay_in_the_selected_chat(self):
        result = sources.message_context(2, before=1, after=1)
        self.assertEqual([m["rowid"] for m in result["messages"]], [1, 2, 3])
        self.assertEqual(result["anchor_rowid"], 2)
        for msg in result["messages"]:
            self.assertEqual(sources.evidence(msg["evidence"]["evidence_handle"])["text"], msg["text"])

    def test_ambiguous_chat_membership_requires_selection(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO chat_message_join VALUES(2,2)")
        con.commit(); con.close()
        result = sources.message_context(2)
        self.assertTrue(result["needs_chat_id"])
        with self.assertRaises(ValueError):
            sources.message_context(2, chat_id=3)
        result = sources.message_context(2, chat_id=1)
        self.assertEqual(result["chat_id"], 1)

    def test_fixture_and_disabled_sources_do_not_open_live_message_store(self):
        with mock.patch.object(settings, "fixture_mode", return_value=True), mock.patch.object(imessage, "_connect") as connect:
            with self.assertRaises(ValueError):
                sources.read_source("imessage:1")
            connect.assert_not_called()
        self.config["answer_source_policy"]["imessage"] = {"model_exposure": False}
        with self.assertRaises(ValueError):
            sources.thread_range(chat_id=1)


class NativeEvidence(SourceBase):
    def test_native_vault_read_returns_reopenable_handle_and_continuation(self):
        self.note("note.md", "full text " * 1000)
        result = asyncio.run(viratools.invoke("vault_note", {"path": "note.md", "length": 100}))
        page = json.loads(result["content"][0]["text"])
        self.assertEqual(page["span"]["end"], 100)
        self.assertEqual(sources.evidence(page["evidence_handle"])["text"], "full text " * 10)
        self.assertIsNotNone(page["continuation"])

    def test_existing_find_returns_handles_and_inherits_source_restrictions(self):
        self.note("note.md", "original source")
        found = {"plan": {"why": "test", "text": "source", "databases": ["notes"]},
                 "groups": {"notes": {"rows": [{"path": "note.md", "heading": "", "snippet": "original source"}]}}}
        with mock.patch("server.find.find", return_value=found):
            result = asyncio.run(viratools.invoke("find", {"query": "source"}))
        notice = json.loads(result["content"][0]["text"])
        self.assertEqual(notice["sources"], ["vault:note.md"])
        self.assertIn("original source", sources.evidence(notice["evidence_handle"])["text"])
        self.specs[0]["model_exposure"] = False
        with self.assertRaises(ValueError):
            sources.evidence(notice["evidence_handle"])

    def test_native_batch_schema_is_an_array_of_typed_source_requests(self):
        tools = viratools.function_tool_specs()
        spec = next(t for t in tools if t["name"] == "sources_read")
        items = spec["parameters"]["properties"]["requests"]["items"]
        self.assertEqual(items["properties"]["start"], {"type": "integer"})
        self.assertEqual(items["required"], ["source"])

    def test_original_tool_scope_denial_precedes_the_reader(self):
        with mock.patch.object(viratools, "_thread_text") as read:
            with self.assertRaises(ValueError):
                asyncio.run(viratools.invoke("imessage_thread", {"name": "synthetic"}, runtime={"evidence_scope": ["vault:primary"]}))
        read.assert_not_called()

    def test_find_receives_corpus_exposure_restrictions_before_reading(self):
        from server import retrieval
        self.config["answer_source_policy"]["mail"] = {"model_exposure": False}
        seen = []

        def find(*args, **kwargs):
            policy = retrieval.current_request().policy
            seen.append(policy)
            return {"plan": {"why": "test", "text": "test", "databases": []}, "groups": {}}
        with mock.patch("server.find.find", side_effect=find):
            asyncio.run(viratools.invoke("find", {"query": "test"}))
        self.assertEqual(seen[0].message_sources, ("imessage",))
        self.assertIn("notes", seen[0].corpus_ids)

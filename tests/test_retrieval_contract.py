"""Cross-layer regressions: scope, wall clocks, exact filters and paging."""
import concurrent.futures
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from server import find, localmodels, retrieval, vault, textindex
from tests.test_multivault import MultiVaultIndexTests
from tests.test_textindex import TextIndexTests


class RequestTests(unittest.TestCase):
    def test_malformed_scope_rejected_and_unknown_scope_denies(self):
        for value in (None, 'imessage', {}, {'sources': None}, [None]):
            with self.assertRaises(ValueError):
                retrieval.policy_from_sources(value)
        policy = retrieval.policy_from_sources(['unknown'])
        self.assertEqual(policy.corpus_ids, ())
        self.assertEqual(policy.source_ids, ())
        self.assertEqual(retrieval.policy_from_sources([]).corpus_ids, None)

    def test_nested_scope_only_narrows(self):
        with retrieval.source_scope(['vault:one', 'imessage']):
            with retrieval.source_scope(['vault:two', 'mail']):
                p = retrieval.current_request().policy
                self.assertEqual(p.source_ids, ())
                self.assertEqual(p.message_sources, ())
                self.assertTrue(p.for_model)

    def test_fanout_returns_before_stalled_worker_and_preserves_context(self):
        gate = threading.Event()
        def slow(p, limit):
            gate.wait(1)
            return {'rows': [], 'count': 0}
        def fast(p, limit):
            self.assertTrue(retrieval.current_request().policy.for_model)
            self.assertTrue(vault._MODEL_ACCESS.get())
            return {'rows': [{'ok': True}], 'count': 1}
        try:
            with mock.patch.dict(find.ADAPTERS, {'notes': slow, 'messages': fast}, clear=True), vault.model_access():
                started = time.monotonic()
                out = find.run({'databases': ['notes', 'messages'], 'filters': {}},
                               deadline=started + .04)
                self.assertLess(time.monotonic() - started, .25)
                self.assertEqual(out['groups']['messages']['count'], 1)
                self.assertEqual(out['groups']['notes']['status'], 'timed_out')
                self.assertTrue(out['groups']['notes']['error'])
                self.assertFalse(out['complete'])
        finally:
            gate.set()

    def test_worker_admission_has_no_unbounded_queue(self):
        gate = threading.Event()
        slots = threading.BoundedSemaphore(1)
        with mock.patch.object(retrieval, '_SLOTS', slots):
            first = retrieval.submit_bounded(gate.wait, .5)
            self.assertIsNotNone(first)
            self.assertIsNone(retrieval.submit_bounded(lambda: None))
            gate.set()
            first.result(timeout=1)

    def test_sql_candidate_filter_precedes_fts_limit(self):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.execute('CREATE VIRTUAL TABLE fts USING fts5(text)')
        for i in range(1, 601):
            con.execute('INSERT INTO fts(rowid,text) VALUES(?,?)', (i, 'insurance'))
        con.execute('INSERT INTO fts VALUES(?)', ('insurance ' + 'other ' * 150,))
        self.assertEqual(retrieval.rank_fts(con, 'insurance', {601}, 3), [601])

    def test_embedding_single_flight_bounded_and_failures_not_cached(self):
        localmodels.clear_query_cache()
        entered, gate = threading.Event(), threading.Event()
        def embed(texts, timeout=None):
            self.assertLessEqual(timeout, 1.5)
            entered.set()
            gate.wait(.2)
            return [[1.0, 0.0]]
        with mock.patch.object(localmodels, 'ollama_embed', side_effect=embed) as call:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                one = pool.submit(localmodels.query_embedding, 'fixture-shared')
                self.assertTrue(entered.wait(.2))
                two = pool.submit(localmodels.query_embedding, 'fixture-shared')
                gate.set()
                self.assertEqual(one.result(), [1.0, 0.0])
                self.assertEqual(two.result(), [1.0, 0.0])
            self.assertEqual(call.call_count, 1)
        self.addCleanup(localmodels.clear_query_cache)


class VaultContractTests(MultiVaultIndexTests):
    def test_model_scope_survives_real_find_worker(self):
        self.extra.joinpath('restricted').mkdir()
        self.extra.joinpath('restricted/secret.md').write_text('# Secret\nUnique fixture orchard.', encoding='utf-8')
        vault.scan_once()
        specs = vault.source_specs()
        next(s for s in specs if s['id'] == 'research')['model_exposure'] = False
        with mock.patch.object(vault, 'source_specs', return_value=specs), vault.model_access():
            plan = {'databases': ['notes'], 'text': 'frontier research',
                    'filters': {'since': None, 'until': None, 'order': 'relevance'}}
            out = find.run(plan)
            self.assertTrue(all(not r['path'].startswith('@research/') for r in out['groups']['notes']['rows']))

    def test_count_paging_scope_and_modified_dates(self):
        vault.scan_once()
        with retrieval.source_scope(['vault:research']):
            one = vault.query_notes('', limit=1, mode='enumerate')
            self.assertEqual(one['total'], 2)
            self.assertTrue(one['total_exact'])
            self.assertEqual(one['date_field'], 'modified')
            two = vault.query_notes('', limit=1, mode='enumerate', cursor=one['next_cursor'])
            self.assertNotEqual(one['rows'][0]['path'], two['rows'][0]['path'])
            with self.assertRaises(ValueError):
                vault.note_text('wiki/shared.md')
            with self.assertRaises(ValueError):
                vault.query_notes('', date_field='event')

    def test_exclusion_and_cached_query_do_not_expand_scope(self):
        vault.scan_once()
        with mock.patch.object(localmodels, 'query_embedding', return_value=None):
            with retrieval.source_scope(['vault:research'], semantic=True):
                vault.search('shared', semantic=True)
            with retrieval.source_scope(['vault:primary'], semantic=True):
                hits = vault.search('shared', semantic=True)
                self.assertTrue(all(not h['path'].startswith('@research/') for h in hits))
        specs = vault.source_specs()
        next(s for s in specs if s['id'] == 'research')['model_exclude_dirs'] = ['shared.md']
        with mock.patch.object(vault, 'source_specs', return_value=specs), retrieval.source_scope(['vault:research']):
            out = vault.query_notes('', mode='count')
            self.assertEqual(out['total'], 1)

    def test_explicit_path_skips_global_resolution_walk(self):
        vault.scan_once()
        with mock.patch.object(vault, '_stem_map', side_effect=AssertionError('must not walk')):
            self.assertEqual(vault.resolve_ref('@research/extra-only.md')['path'], '@research/extra-only.md')

    def test_no_embedding_for_fast_text_find(self):
        vault.scan_once()
        p = {'databases': ['notes'], 'text': 'frontier',
             'filters': {'since': None, 'until': None, 'order': 'relevance'}}
        with mock.patch.object(localmodels, 'query_embedding', side_effect=AssertionError('cold encoder')):
            self.assertEqual(find.run(p)['groups']['notes']['count'], 1)


class MessageContractTests(TextIndexTests):
    def test_exact_count_pages_and_scope(self):
        self.index()
        with retrieval.source_scope(['imessage']):
            out = textindex.query_messages('', limit=2, mode='enumerate', order='recent')
            self.assertEqual(out['total'], 4)
            other = textindex.query_messages('', limit=2, mode='enumerate', order='recent', cursor=out['next_cursor'])
            self.assertFalse({r['seq'] for r in out['rows']} & {r['seq'] for r in other['rows']})
        with retrieval.source_scope(['mail']):
            self.assertEqual(textindex.query_messages('')['total'], 0)
        with retrieval.source_scope(['vault:primary']):
            self.assertEqual(textindex.query_messages('')['rows'], [])


class PlanningScopeTests(unittest.TestCase):
    def test_note_only_plan_never_reads_identity_directory(self):
        with retrieval.source_scope(['vault:primary']), mock.patch.object(find.crm, '_load', side_effect=AssertionError('outside selected scope')):
            p = find.plan('What did Alexandra Example say about the lease?')
            self.assertIsNone(p['filters']['person'])
            self.assertIn('Alexandra', p['text'])
            self.assertEqual(p['databases'], ['notes'])
            self.assertNotIn('p_private', p['why'])

    def test_scoped_llm_planner_has_no_directory_or_inferred_identity(self):
        response = '{"person":"p_private", "sender":"p_private", "databases":["people","notes"], "query":"lease"}'
        with retrieval.source_scope(['vault:primary']), \
                mock.patch('server.search._people_for_prompt', side_effect=AssertionError('outside selected scope')), \
                mock.patch('server.suggest.complete', return_value=response) as complete:
            p = find.plan_llm('Who discussed the lease?')
            self.assertIsNone(p['filters']['person'])
            self.assertIsNone(p['filters']['sender'])
            self.assertEqual(p['databases'], ['notes'])
            self.assertEqual(complete.call_args.kwargs['tools'], [])
            self.assertLessEqual(complete.call_args.kwargs['timeout'], 2)

    def test_find_planning_obeys_same_absolute_deadline(self):
        gate = threading.Event()
        def delayed(*args, **kwargs):
            gate.wait(.5)
            return find._blank_plan('fixture')
        started = time.monotonic()
        try:
            with mock.patch.object(find, 'plan', side_effect=delayed), mock.patch.dict(find.ADAPTERS, {'notes': mock.Mock()}):
                out = find.find('fixture', deadline=started + .03,
                    policy=retrieval.policy_from_sources(['vault:primary']))
                self.assertEqual(out['planning_status'], 'timed_out')
                self.assertFalse(out['complete'])
                find.ADAPTERS['notes'].assert_not_called()
                self.assertLess(time.monotonic() - started, .2)
        finally:
            gate.set()

    def test_explicit_dates_are_validated_and_never_relaxed(self):
        with retrieval.source_scope(['imessage']):
            p = find.plan('lease since:2026-01-01 until:2026-02-01')
            with mock.patch.dict(find.ADAPTERS, {'messages': mock.Mock(return_value={'rows': [], 'count': 0})}):
                got, relaxed = find._relax(p, 'messages', 10)
                self.assertEqual(relaxed, [])
                find.ADAPTERS['messages'].assert_not_called()
            for query in ('lease since:nonsense', 'lease since:2026-02-01 until:2026-01-01'):
                with self.assertRaises(ValueError):
                    find.plan(query)
            for call in (vault.query_notes, textindex.query_messages):
                with self.assertRaises(ValueError):
                    call('lease', since='nonsense')


class ScopedMessageHydrationTests(TextIndexTests):
    def test_message_scope_does_not_load_people_for_display_names(self):
        self.index()
        with retrieval.source_scope(['imessage']), mock.patch.object(textindex.crm, '_load', side_effect=AssertionError('people outside scope')):
            out = textindex.query_messages('lease')
            self.assertEqual(out['total'], 2)
            self.assertTrue(all(r['person'] is None and r['person_id'] is None for r in out['rows']))

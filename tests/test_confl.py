import json
from pathlib import Path
import tempfile
import unittest
from confl.kb import CKB
from confl.localize import localize
from tests.fakes import FirstOptionModel
from confl.metrics import evaluate_ranking
from confl.config import ROOT
from confl.dsl import build_interaction_dsl


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import subprocess
        cls.temp = tempfile.TemporaryDirectory()
        facts = Path(cls.temp.name)/'facts'
        subprocess.run(['java', '-jar', str(ROOT/'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar'), 'test', str(ROOT/'examples/concurrency'), str(facts)], check=True, capture_output=True)
        cls.kb = CKB(facts, ROOT/'examples/concurrency')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_lazy_dsl_matches_full_distinct_entry_dsl(self):
        full = build_interaction_dsl(self.kb.facts, self.kb.reach)
        pair = next(i['entry_pair'] for i in full if not i['same_entry_instances'])
        expected = [i for i in full if i['entry_pair'] == pair]
        self.assertEqual(expected, self.kb.get_interaction_dsl(pair))
        with self.assertRaises(ValueError):
            self.kb.get_interaction_dsl([pair[0], pair[0]])

    def test_dsl_prior_is_optional(self):
        from confl.retrieval import retrieve_methods
        rows = retrieve_methods(self.kb, 'count', None, [])[1]['candidates']
        self.assertFalse(any('dsl_suspicion' in r['evidence_sources'] for r in rows))
        self.kb.dsl_prior_weight = 4.0
        try:
            ranks = self.kb.suspicion_ranks()
            rows = retrieve_methods(self.kb, 'count', None, [])[1]['candidates']
            tagged = {r['name'] for r in rows if 'dsl_suspicion' in r['evidence_sources']}
            self.assertTrue(tagged and tagged <= set(ranks))
            self.assertIn('dsl_suspicion_rank', self.kb.get_method_summary(next(iter(ranks))))
        finally:
            self.kb.dsl_prior_weight = 0.0

    def test_nested_class_package_comes_from_source(self):
        self.assertEqual({'demo'}, {x['package'] for x in self.kb.meta.values()})

    def test_end_to_end_and_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = localize(self.kb, 'Concurrent tasks update count in demo.Example', out, FirstOptionModel())
            self.assertEqual('completed', result['status'])
            self.assertTrue(result['ranking'])
            self.assertTrue((out/'selected_interactions.json').exists())
            self.assertEqual(result, json.loads((out/'result.json').read_text()))
            evidence = json.loads((out/'dsl_evidence.json').read_text())
            self.assertEqual([m['method'] for m in evidence['methods']], [r['method'] for r in result['ranking']])
            self.assertIn('thread_entry_count', evidence['methods'][0])
            report = (out/'report.md').read_text()
            self.assertIn('## Suspicious methods', report)
            self.assertIn('Concurrency evidence from the DSL', report)
            localize(self.kb, 'Concurrent tasks update count in demo.Example', out/'quiet', FirstOptionModel(), explain=False)
            self.assertFalse((out/'quiet/report.md').exists())
            method = next(r for r in result['ranking'] if '#<init>' not in r['method'])
            owner, name = method['method'].rsplit('#', 1)
            gt = out/'gt.json'
            gt.write_text(json.dumps([{'bugID': 'test', 'files': [{'file': method['file'], 'methods': [owner.rsplit('.', 1)[-1]+'::'+name]}]}]))
            self.assertEqual(evaluate_ranking(result, gt, 'test')['methods'][0]['rank'], method['rank'])
            gt.write_text(json.dumps([{'bugID': 'test', 'files': [{'file': 'different.java', 'methods': [owner.rsplit('.', 1)[-1]+'::'+name]}]}]))
            self.assertFalse(evaluate_ranking(result, gt, 'test')['top5'])

if __name__ == '__main__':
    unittest.main()

class ComponentHierarchyTest(unittest.TestCase):
    def test_root_and_recursive_children(self):
        from confl.kb import component_options
        modules = [{'concurrency_evidence': {'thread_entries': 1}, 'name': n, 'description': n} for n in ['server', 'extensions/kafka', 'extensions/storage/s3']]
        root = component_options(modules)
        self.assertEqual([x['name'] for x in root], ['extensions', 'server'])
        self.assertTrue(root[0]['has_children'])
        children = component_options(modules, 'extensions')
        self.assertEqual([x['name'] for x in children], ['extensions/kafka', 'extensions/storage'])
        self.assertEqual(component_options(modules, 'extensions/storage')[0]['name'], 'extensions/storage/s3')

    def test_parent_module_and_child_module_remain_selectable(self):
        from confl.kb import component_options
        modules = [{'concurrency_evidence': {'thread_entries': 1}, 'name': 'a'}, {'concurrency_evidence': {'thread_entries': 1}, 'name': 'a/b'}]
        self.assertTrue(component_options(modules)[0]['has_children'])
        children = component_options(modules, 'a')
        self.assertEqual([x['name'] for x in children], ['a', 'a/b'])
        self.assertFalse(children[0]['has_children'])

class ComponentFilterTest(unittest.TestCase):
    def test_evidence_and_demo_filter(self):
        from collections import defaultdict
        kb = CKB.__new__(CKB)
        kb.min_thread_entries = 0
        kb.exclude_components = []
        kb.meta = {m: {'component': c} for m, c in [
            ('entry', 'server'), ('helper', 'common'), ('sequential', 'plain'),
            ('sync', 'locks'), ('demo', 'examples'), ('bench', 'nested/benchmarks'),
            ('demo_helper', 'demo_only_helper')]}
        kb.entries = {'entry', 'demo', 'bench'}
        kb.reach = {('entry', 'entry'), ('entry', 'helper'), ('demo', 'demo_helper')}
        kb.accesses = defaultdict(list, {'helper': [1], 'demo_helper': [1]})
        kb.facts = {'Sync': {('sync', 'lock', 'acquire', 'file:1:1')}}
        report = {r['name']: r for r in kb.module_filter_report()}
        self.assertEqual({n for n,r in report.items() if r['included']}, {'server', 'common', 'locks'})
        self.assertEqual(report['examples']['reason'], 'excluded_demo_or_benchmark')
        self.assertEqual(report['plain']['reason'], 'no_extracted_concurrency_evidence')

        kb.min_thread_entries = 1
        report = {r['name']: r for r in kb.module_filter_report()}
        self.assertEqual({n for n,r in report.items() if r['included']}, {'server'})
        self.assertEqual(report['common']['reason'], 'below_min_thread_entries')
        kb.min_thread_entries = 2
        self.assertFalse(any(r['included'] for r in kb.module_filter_report()))

    def test_invalid_threshold_rejected_before_loading_facts(self):
        for value in (-1, True, 1.5, '1'):
            with self.assertRaisesRegex(ValueError, 'min_thread_entries'):
                CKB('unused', 'unused', min_thread_entries=value)

class ComponentExclusionTest(unittest.TestCase):
    def test_explicit_names_override_threshold_with_path_boundaries(self):
        from collections import defaultdict
        kb = CKB.__new__(CKB)
        kb.min_thread_entries = 1
        kb.exclude_components = ['extensions/kafka', 'server']
        kb.meta = {n: {'component': n} for n in ['server', 'extensions/kafka', 'extensions/kafka/child', 'extensions/kafka-extra']}
        kb.entries = set(kb.meta)
        kb.reach = {(m,m) for m in kb.entries}
        kb.accesses = defaultdict(list)
        kb.facts = {'Sync': set()}
        rows = kb.module_filter_report()
        self.assertEqual([r['name'] for r in rows if r['included']], ['extensions/kafka-extra'])
        self.assertTrue(all(r['reason'] == 'excluded_by_config' for r in rows if not r['included']))
        kb.exclude_components = ['extensions']
        self.assertEqual([r['name'] for r in kb.module_filter_report() if r['included']], ['server'])

    def test_exclusion_validation(self):
        from confl.kb import normalize_component_exclusions
        self.assertEqual(normalize_component_exclusions(['extensions/kafka/']), ['extensions/kafka'])
        for value in ['server', ['../server'], ['*'], [1]]:
            with self.assertRaises(ValueError):
                normalize_component_exclusions(value)

class EvidenceRequiredTest(unittest.TestCase):
    def test_missing_empty_and_zero_evidence_are_excluded(self):
        from confl.kb import component_options
        modules = [{'name':'missing'}, {'name':'empty','concurrency_evidence':{}},
                   {'name':'zero','concurrency_evidence':{'thread_entries':0}},
                   {'name':'extensions/a','concurrency_evidence':{'thread_entries':2}},
                   {'name':'extensions/b','concurrency_evidence':{'thread_entries':3}}]
        root=component_options(modules)
        self.assertEqual([x['name'] for x in root], ['extensions'])
        self.assertEqual(root[0]['concurrency_evidence']['thread_entries'], 5)
        self.assertEqual(root[0]['evidence_scope'], 'included_modules_in_subtree')
        self.assertEqual(len(component_options(modules, 'extensions')), 2)

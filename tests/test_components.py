import unittest
from confl.metrics import component_for_file, score_selection, summarize
import json
from pathlib import Path
import shutil
import tempfile
from confl.evaluation import case_inputs


class ComponentEvaluationTest(unittest.TestCase):
    def test_component_mapping(self):
        self.assertEqual(component_for_file('extensions/kafka/src/main/java/A.java'), 'extensions/kafka')
        self.assertEqual(component_for_file('src/main/java/A.java'), '.')
        self.assertEqual(component_for_file('server\\src\\main\\java\\A.java'), 'server')

    def test_root_and_leaf_are_distinct(self):
        bug = {'files': [{'file': 'extensions/a/src/main/java/A.java'}]}
        r = score_selection(bug, ['extensions', 'extensions/b'], [{'name': 'extensions/b'}])
        self.assertTrue(r['root_hit'])
        self.assertFalse(r['leaf_hit'])
        self.assertFalse(r['any_target_available'])

    def test_summary_does_not_call_missing_data_model_mistakes(self):
        r = summarize([{'status': 'missing_report'}, {'status': 'completed', 'root_hit': True, 'leaf_hit': False, 'any_target_available': True}])
        self.assertEqual(r['total_cases'], 2)
        self.assertEqual(r['evaluated_cases'], 1)
        self.assertEqual(r['root_accuracy'], 1)
        self.assertEqual(r['leaf_accuracy'], 0)
        self.assertIsNone(summarize([])['leaf_accuracy'])


class CaseLayoutTest(unittest.TestCase):
    def test_project_layout_and_missing_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = dict(source_root=root/'sources', reports_dir=root/'reports', facts_root=root/'facts')
            bug = {'bugID': 'druid_582'}
            self.assertEqual(case_inputs(bug, dataset)['missing'], ['source', 'report', 'facts'])
            (root/'sources/druid/druid_582').mkdir(parents=True)
            (root/'reports/druid').mkdir(parents=True)
            (root/'reports/druid/druid_582.txt').write_text('report')
            record = case_inputs(bug, dataset)
            self.assertEqual(record['missing'], ['facts'])
            self.assertEqual(record['facts'], str(root/'facts/druid_582/facts'))
            (root/'reports/druid/druid_582.txt').write_text('  ')
            self.assertIn('report', case_inputs(bug, dataset)['missing'])


class DatasetEvaluationTest(unittest.TestCase):
    def test_extract_localize_score_and_resume(self):
        from confl.config import ROOT, load_config
        from confl.evaluation import evaluate_dataset
        from tests.fakes import FirstOptionModel
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT/'examples/concurrency', root/'sources/demo/demo_1')
            (root/'reports/demo').mkdir(parents=True)
            (root/'reports/demo/demo_1.txt').write_text('Concurrent tasks update count in demo.Example')
            (root/'gt.json').write_text(json.dumps([{'bugID': 'demo_1', 'files': [{'file': 'src/main/java/demo/Example.java', 'methods': ['Example::run()']}]}]))
            (root/'config.json').write_text(json.dumps({'llm': {'provider': 'openai', 'model': 'test'}, 'extraction': {'extractor_jar': str(ROOT/'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar')},
                                                        'dataset': {'ground_truth': 'gt.json', 'source_root': 'sources', 'reports_dir': 'reports', 'facts_root': 'facts'}}))
            config = load_config(root/'config.json')
            results = evaluate_dataset(config, FirstOptionModel(), root/'out', extract=True, progress=lambda m: None)
            self.assertEqual(results[0]['status'], 'completed')
            self.assertIn('method_evaluation', results[0])
            self.assertTrue((root/'facts/demo_1/facts/schema-version.txt').is_file())
            summary = json.loads((root/'out/summary.json').read_text())
            self.assertEqual(summary['method_metrics']['evaluated_cases'], 1)
            self.assertIn('## Ground truth', (root/'out/cases/demo_1/report.md').read_text())
            model = FirstOptionModel()
            model.__call__ = None  # a resumed completed case must not reach the model
            self.assertEqual(evaluate_dataset(config, model, root/'out', resume=True, progress=lambda m: None)[0]['status'], 'completed')

    def test_missing_facts_are_reported_not_extracted_by_default(self):
        from confl.config import load_config
        from confl.evaluation import evaluate_dataset
        from tests.fakes import FirstOptionModel
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'sources/demo/demo_1').mkdir(parents=True)
            (root/'reports/demo').mkdir(parents=True)
            (root/'reports/demo/demo_1.txt').write_text('report')
            (root/'gt.json').write_text(json.dumps([{'bugID': 'demo_1', 'files': []}]))
            (root/'config.json').write_text(json.dumps({'dataset': {'ground_truth': 'gt.json', 'source_root': 'sources', 'reports_dir': 'reports', 'facts_root': 'facts'}}))
            results = evaluate_dataset(load_config(root/'config.json'), FirstOptionModel(), root/'out', progress=lambda m: None)
            self.assertEqual(results[0]['status'], 'missing_facts')


class ProjectBatchTest(unittest.TestCase):
    def test_project_order_and_limit_after_filter(self):
        from confl.evaluation import select_cases
        bugs = [{'bugID': x} for x in ['druid_1','pulsar_1','druid_2','pulsar_2']]
        self.assertEqual([b['bugID'] for b in select_cases(bugs, ['pulsar','druid'], 3)], ['pulsar_1','pulsar_2','druid_1'])
        self.assertEqual([b['bugID'] for b in select_cases(bugs, ['druid'])], ['druid_1','druid_2'])
        self.assertEqual([b['bugID'] for b in select_cases(bugs)], ['druid_1','druid_2','pulsar_1','pulsar_2'])
        for value in ([], ['unknown'], ['druid','druid'], 'druid'):
            with self.assertRaises(ValueError):
                select_cases(bugs, value)

    def test_per_project_accuracy_separates_missing_data(self):
        from confl.metrics import summarize_projects
        results = [dict(bug_id='druid_1', status='completed', root_hit=True, leaf_hit=False, any_target_available=True),
                   dict(bug_id='pulsar_1', status='missing_facts')]
        summary = summarize_projects(results)
        self.assertEqual(summary['druid']['root_accuracy'], 1)
        self.assertEqual(summary['druid']['leaf_accuracy'], 0)
        self.assertIsNone(summary['pulsar']['root_accuracy'])

class FailureSummaryTest(unittest.TestCase):
    def test_failures_are_named_and_separated_from_run_errors(self):
        rows=[dict(bug_id='p_1',status='completed',root_hit=True,leaf_hit=False,any_target_available=False,selected_path=['extensions','extensions/a'],target_components=['extensions/b']),
              dict(bug_id='p_2',status='completed',root_hit=False,leaf_hit=False,any_target_available=True),
              dict(bug_id='p_3',status='model_error')]
        result=summarize(rows)
        self.assertEqual(result['root_failed_cases'],['p_2'])
        self.assertEqual(result['leaf_failed_count'],2)
        self.assertEqual(result['all_targets_unavailable_cases'],['p_1'])
        self.assertEqual(result['leaf_failed_cases'][0]['reason'],'all_target_components_unavailable')
        self.assertEqual(result['not_completed_cases'],[dict(bug_id='p_3',status='model_error')])

class FullPipelineMetricsTest(unittest.TestCase):
    def test_method_metrics_only_completed_and_failures_visible(self):
        result = dict(bug_id='druid_1', status='completed', root_hit=True, leaf_hit=True,
                      any_target_available=True, method_evaluation=dict(top1=False, top3=True, top5=True, reciprocal_rank=.5))
        summary = summarize([result, dict(bug_id='druid_2', status='no_valid_candidate')])
        self.assertEqual(summary['method_metrics']['evaluated_cases'], 1)
        self.assertEqual(summary['method_metrics']['reciprocal_rank'], .5)
        self.assertEqual(summary['status_counts']['no_valid_candidate'], 1)

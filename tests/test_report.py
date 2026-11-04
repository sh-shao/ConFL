import json
from pathlib import Path
import tempfile
import unittest

from confl.cli import main
from confl.report import collect_dsl_evidence, render, resource_evidence, short


def access(method, action, location, guarded_by=()):
    return dict(method=method, action=action, location=location, guarded_by=list(guarded_by))


def interaction(resource, side1, side2):
    return dict(shared_resource=resource, entry_pair=['a.A#run()', 'a.B#run()'], has_write=True,
                contexts=[dict(side=1, entry='a.A#run()', accesses=side1), dict(side=2, entry='a.B#run()', accesses=side2)])


class ResourceEvidenceTest(unittest.TestCase):
    def test_lock_status(self):
        guarded = interaction('a.C#x', [access('a.C#get()', 'R', 'src/a/C.java:10:5', ['a.C#lock'])],
                              [access('a.C#set()', 'W', 'src/a/C.java:20:5', ['a.C#lock'])])
        record = resource_evidence(guarded, 'a.C#get()')
        self.assertEqual((record['status'], record['common_locks']), ('common_lock', ['a.C#lock']))
        self.assertEqual(record['accesses'][0]['location'], 'C.java:10')
        unguarded = interaction('a.C#x', [access('a.C#get()', 'R', 'src/a/C.java:10:5', ['a.C#lock'])],
                                [access('a.C#set()', 'W', 'src/a/C.java:20:5'), access('a.C#peek()', 'R', 'src/a/C.java:30:5')])
        record = resource_evidence(unguarded, 'a.C#get()')
        self.assertEqual((record['status'], record['pairs_without_common_lock']), ('no_common_lock', 1))
        self.assertEqual([b['method'] for b in record['other_thread']], ['a.C#set()', 'a.C#peek()'])
        self.assertTrue(record['other_thread'][0]['unguarded'])

    def test_constructor_writes_and_reads_are_not_conflicts(self):
        published = interaction('a.C#x', [access('a.C#get()', 'R', 'src/a/C.java:10:5')],
                                [access('a.C#<init>()', 'W', 'src/a/C.java:5:5'), access('a.C#peek()', 'R', 'src/a/C.java:30:5')])
        self.assertEqual(resource_evidence(published, 'a.C#get()')['status'], 'no_conflict')

    def test_evidence_per_ranked_method(self):
        rows = [interaction('a.C#x', [access('a.C#get()', 'R', 'C.java:1:1')], [access('a.C#set()', 'W', 'C.java:2:1')]),
                interaction('a.C#y', [access('a.C#set()', 'W', 'C.java:3:1')], [access('a.D#run()', 'R', 'D.java:4:1')])]
        evidence = collect_dsl_evidence(rows, [dict(rank=1, method='a.C#set()'), dict(rank=2, method='a.E#other()')],
                                        pool=[dict(name='a.C#set()', evidence_sources=['report_anchor'])])
        first, second = evidence['methods']
        self.assertEqual((first['shared_resource_count'], first['unprotected_count'], first['evidence_sources']), (2, 2, ['report_anchor']))
        self.assertEqual(second['shared_resource_count'], 0)

    def test_short_names(self):
        self.assertEqual(short('io.x.Peon$anonymous@server/src/io/x/Peon.java:205:13#run(io.x.Node)'), 'Peon$anonymous@Peon.java:205#run(Node)')


class RenderTest(unittest.TestCase):
    def write_case(self, out):
        method = 'a.C#set()'
        files = {
            'result.json': dict(bug_id='demo_1', status='completed', entry_pair=['a.A#run()', 'a.B#run()'],
                                ranking=[dict(rank=1, method=method, file='src/a/C.java', explanation='Writes x without the lock.')],
                                limitations=['MAY reachability.']),
            'trace.json': [dict(request=dict(stage='component', bug_report='x is lost under load'), response=dict(name='core', explanation='Report names C.')),
                           dict(stage='method_ranking', tool='get_code_snippet', arguments=dict(method_name=method), response={}),
                           dict(request=dict(stage='method_ranking'), response=dict(name=method, explanation='Writes x without the lock.'))],
            'selected_interactions.json': [interaction('a.C#x', [access('a.C#get()', 'R', 'src/a/C.java:1:1', ['a.C#lock'])],
                                                       [access(method, 'W', 'src/a/C.java:2:1')])],
            'fault_hypothesis.json': dict(hypothesis='H1 - set() races with get().'),
            'code_rank_1.json': dict(start_line=2, lines=['2: void set() { x = 1; }']),
            'evaluation.json': dict(methods=[dict(file='src/a/C.java', method='C::set()', rank=1)], reciprocal_rank=1.0, average_precision=1.0),
        }
        for name, data in files.items():
            (out / name).write_text(json.dumps(data))

    def test_report_from_recorded_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.write_case(out)
            text = render(out)
            for expected in ('x is lost under load', '**Component:** `core`', 'Thread A:** `A#run()`', 'H1 - set() races',
                             '### 1. `C#set()`', 'Writes x without the lock.', '**no common lock**', '`get_code_snippet(C#set())`',
                             '2: void set() { x = 1; }', '✔ match', '## Ground truth'):
                self.assertIn(expected, text)

    def test_report_command_covers_evaluation_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for case in ('demo_1', 'demo_2'):
                (root / 'cases' / case).mkdir(parents=True)
                self.write_case(root / 'cases' / case)
            self.assertEqual(main(['report', str(root)]), 0)
            self.assertTrue((root / 'cases/demo_2/report.md').exists())


if __name__ == '__main__':
    unittest.main()

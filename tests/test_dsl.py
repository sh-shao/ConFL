import json
import tempfile
import unittest
from pathlib import Path
from confl.dsl import ARITIES, OPTIONAL_ARITIES, load_facts, derive_reachability, build_document


def base():
    f = {name: set() for name in dict(ARITIES, **OPTIONAL_ARITIES)}
    for cls in ('A','B'):
        f['Class'].add((cls,))
    for method, cls, name in [('A#run()', 'A','run'), ('B#run()', 'B','run')]:
        f['Method'].add((method,))
        f['InClass'].add((method, cls))
        f['MethodSig'].add((method, name, '()'))
        f['ThreadEntry'].add((method,))
        f['DispatchMethod'].add((method,))
    return f


class PipelineTests(unittest.TestCase):
    def test_different_methods_same_resource_and_no_labels(self):
        f = base()
        f['Access'] = {('A#run()', 'Owner#field', 'W', 'A.java:1:1'), ('B#run()', 'Owner#field', 'R', 'B.java:1:1')}
        doc = build_document(f, derive_reachability(f))
        self.assertEqual(len(doc['interactions']), 1)
        self.assertNotIn('ground_truth', json.dumps(doc))
        self.assertEqual({a['method'] for i in doc['interactions'] for c in i['contexts'] for a in c['accesses']}, {'A#run()', 'B#run()'})

    def test_self_pair_requires_reentrant_evidence(self):
        f = base(); f['ThreadEntry'] = {('A#run()',)}
        f['Access'] = {('A#run()', 'Owner#field', 'RW', 'A.java:1:1')}
        self.assertFalse(build_document(f, derive_reachability(f))['interactions'])
        f['ReentrantEntry'].add(('A#run()',))
        result = build_document(f, derive_reachability(f))['interactions']
        self.assertEqual(result[0]['entry_pair'], ['A#run()', 'A#run()'])
        self.assertTrue(result[0]['same_entry_instances'])

    def test_read_only_optional_and_resource_separation(self):
        f = base(); f['Access'] = {('A#run()', 'A#x', 'R', 'a:1'), ('B#run()', 'B#x', 'W', 'b:1')}
        self.assertFalse(build_document(f, derive_reachability(f))['interactions'])
        f['Access'] = {('A#run()', 'A#x', 'R', 'a:1'), ('B#run()', 'A#x', 'R', 'b:1')}
        self.assertFalse(build_document(f, derive_reachability(f))['interactions'])
        self.assertEqual(len(build_document(f, derive_reachability(f), True)['interactions']), 1)

    def test_inherited_interface_implementation_and_overload(self):
        f = base(); f['Class'].update({('Base',), ('Child',), ('I',)})
        f['Extends'].add(('Child','Base')); f['Implements'].add(('Child','I'))
        for sig in ('(int)', '(java.lang.String)'):
            m = 'Base#work' + sig
            f['Method'].add((m,)); f['InClass'].add((m,'Base'))
            f['MethodSig'].add((m,'work',sig)); f['DispatchMethod'].add((m,))
        f['VirtualCall'].add(('A#run()','I','work','(int)'))
        reach = derive_reachability(f)
        self.assertIn(('A#run()','Base#work(int)'),reach)
        self.assertNotIn(('A#run()','Base#work(java.lang.String)'),reach)

    def test_async_edges_do_not_propagate_submitter(self):
        f = base(); f['AsyncCall'].add(('A#run()','B#run()','site:1'))
        self.assertNotIn(('A#run()','B#run()'),derive_reachability(f))

    def test_guards_and_determinism(self):
        f = base(); f['Access'] = {('A#run()','C#x','W','a:1'), ('B#run()','C#x','R','b:1')}
        f['Guard'].add(('A#run()','C#x','a:1','C#lock'))
        doc = build_document(f, derive_reachability(f))
        self.assertEqual(doc['interactions'][0]['contexts'][0]['accesses'][0]['guarded_by'],['C#lock'])
        self.assertEqual(doc, build_document(f, derive_reachability(f)))

    def test_schema_and_dangling_ids_fail_fast(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory); (p/'schema-version.txt').write_text('2\n')
            f = base()
            for name, rows in f.items():
                (p/f'{name}.facts').write_text(''.join('\t'.join(row)+'\n' for row in rows))
            self.assertEqual(load_facts(p, model=False), f)
            (p/'ThreadEntry.facts').write_text('unknown\n')
            with self.assertRaisesRegex(ValueError,'undeclared'):
                load_facts(p)

if __name__ == '__main__':
    unittest.main()

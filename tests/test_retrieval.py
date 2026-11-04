import unittest
from types import SimpleNamespace
from confl.retrieval import retrieve_methods, method_interactions

class RetrievalTest(unittest.TestCase):
    def test_report_class_outside_pair_and_filtered_modules(self):
        kb = SimpleNamespace(meta={m:dict(component=c) for m,c in [('A#x()', 'chosen'), ('JobHelper#run()', 'other'), ('Hidden#run()', 'excluded')]}, owners={'A#x()':'A','JobHelper#run()':'JobHelper','Hidden#run()':'Hidden'}, list_modules=lambda:[dict(name='chosen'),dict(name='other')])
        rows, info=retrieve_methods(kb, 'JobHelper Hidden', 'chosen', [], limit=1)
        self.assertEqual(rows[0]['name'],'JobHelper#run()')
        self.assertEqual(info['pool_size'],2)
        self.assertEqual(info['omitted_count'],1)
        self.assertNotIn('Hidden#run()',str(info))

    def test_declarations_redirect_and_overloads_share_a_slot(self):
        methods = {'api.Lock#unlock()': 'api.Lock', 'impl.LockImpl#unlock()': 'impl.LockImpl',
                   'impl.LockImpl#run()': 'impl.LockImpl', 'impl.LockImpl#run(int)': 'impl.LockImpl'}
        kb = SimpleNamespace(meta={m: dict(component='c') for m in methods}, owners=methods,
                             list_modules=lambda: [dict(name='c')],
                             subtypes=lambda c: {'impl.LockImpl'} if c == 'api.Lock' else set(),
                             is_declaration=lambda m: m.startswith('api.'))
        rows, info = retrieve_methods(kb, 'Lock unlock run', 'c', [], limit=3)
        names = [r['name'] for r in rows]
        self.assertEqual(names[0], 'impl.LockImpl#unlock()')
        self.assertIn('implements_ranked_declaration', rows[0]['evidence_sources'])
        self.assertIn('report_class_subtype', rows[0]['evidence_sources'])
        self.assertIn('declaration_only', info['candidates'][1]['evidence_sources'])
        # Three slots, four methods: the second run() overload is the one left out.
        self.assertEqual(sum(n.startswith('impl.LockImpl#run(') for n in names), 1)

    def test_thin_bodies_rank_below_the_code_they_reach(self):
        methods = {'p.Locks#getLock()': 'p.Locks', 'p.Lock#acquire()': 'p.Lock'}
        kb = SimpleNamespace(meta={m: dict(component='c') for m in methods}, owners=methods,
                             list_modules=lambda: [dict(name='c')],
                             body_kind=lambda m: 'factory' if m.endswith('getLock()') else None)
        rows, _ = retrieve_methods(kb, 'getLock() then acquire() hangs', 'c', [])
        self.assertEqual(rows[0]['name'], 'p.Lock#acquire()')
        self.assertIn('thin_body_factory', rows[1]['evidence_sources'])

    def test_method_evidence_only(self):
        d=[dict(shared_resource='x', contexts=[dict(entry='e',accesses=[dict(method='A'),dict(method='B')])])]
        self.assertEqual(method_interactions(d,'missing'),[])
        self.assertEqual(method_interactions(d,'A')[0]['contexts'][0]['accesses'],[dict(method='A')])

    def test_search_recovers_beyond_initial_shortlist(self):
        from confl.retrieval import search_methods
        rows=[dict(name='pkg.Target#fix()',component='other',score=0), dict(name='pkg.Other#run()',component='chosen',score=3)]
        self.assertEqual(search_methods(rows,'Target')['matches'][0]['name'],'pkg.Target#fix()')
        self.assertEqual(search_methods(rows,'Target',excluded={'pkg.Target#fix()'})['matches'],[])

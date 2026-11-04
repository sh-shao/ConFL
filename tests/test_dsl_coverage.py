import random
import unittest
from tools.dsl_coverage import summarize, targets, ranking
from confl.dsl import build_interaction_dsl, method_evidence

class CoverageTest(unittest.TestCase):
    def test_equivalent_to_materialized_distinct_dsl(self):
        rng=random.Random(3)
        for _ in range(30):
            f=dict(ThreadEntry={(f'e{i}',) for i in range(3)},Method={(f'm{i}',) for i in range(6)},Guard=set(),ReentrantEntry=set())
            f['Access']={(f'm{i}',f'r{j}',rng.choice(['R','W','RW']),f's{i}') for i in range(6) for j in range(3) if rng.random()<.4}
            reach={(f'e{i}',f'm{j}') for i in range(3) for j in range(6) if rng.random()<.4}
            evidence,stats=method_evidence(f,reach,context_depth=0)
            dsl=build_interaction_dsl(f,reach)
            expected={a['method'] for interaction in dsl for c in interaction['contexts'] for a in c['accesses']}
            self.assertEqual(set(evidence),expected)
            self.assertEqual(stats['interactions'],len(dsl))
            # Grouped count = distinct (resource, unordered pair of per-entry access sets).
            key=lambda c: frozenset((a['method'],a['action']) for a in c['accesses'])
            classes={(i['shared_resource'],frozenset([key(i['contexts'][0]),key(i['contexts'][1])])) for i in dsl}
            self.assertEqual(stats['grouped_interactions'],len(classes))
    def test_construction_writes_option(self):
        f=dict(ThreadEntry={('e1',),('e2',)},Method={('A#<init>()',),('A#get()',)},Guard=set(),ReentrantEntry=set(),
               Access={('A#<init>()','A#x','W','s1'),('A#get()','A#x','R','s2')})
        reach={('e1','A#<init>()'),('e1','A#get()'),('e2','A#get()')}
        # Constructor writes make A#x eligible, but the constructor itself is not a member by default.
        self.assertEqual(set(method_evidence(f,reach,context_depth=0)[0]),{'A#get()'})
        self.assertEqual(set(method_evidence(f,reach,context_depth=0,construction_members=True)[0]),{'A#<init>()','A#get()'})
        self.assertEqual(method_evidence(f,reach,construction_writes=False,context_depth=0)[0],{})
    def test_ranking_order(self):
        evidence={'b':dict(tier=1,score=2),'a':dict(tier=1,score=2),'c':dict(tier=2,score=9),'d':dict(tier=1,score=5)}
        self.assertEqual(ranking(evidence),['d','a','b','c'])
    def test_unknown_input_denominators(self):
        size=dict(entries=1)
        rows=[dict(status='evaluated',target_count=2,covered_count=1,accessor_count=1,context_count=0,
                   recall_at={'50':1,'100':1,'200':1,'500':1},lossless_prefix=7,size=size,methods=[]),
              dict(status='missing_facts',target_count=2,covered_count=0)]
        s=summarize(rows)
        self.assertEqual(s['global_dsl']['method_coverage'],.5)
        self.assertEqual(s['all_cases_lower_bound']['method_coverage'],.25)
        self.assertEqual(s['recall_at_budget']['200']['method_recall'],.5)
        self.assertEqual(s['median_size']['lossless_prefix'],7)
    def test_nested_and_overload_dedup(self):
        t=targets(dict(files=[dict(file='A.java',methods=['A::B::m(int)','A::B::m(long)'])]))
        self.assertEqual(t,[('A.java','A.B','m')])

if __name__=='__main__':
    unittest.main()

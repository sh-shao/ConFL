import tempfile
import unittest
from pathlib import Path
from tools.diagnose_unreachable import call_graph, reverse_frontier, source_hints
from confl.dsl import derive_reachability

class DiagnosisTest(unittest.TestCase):
    def test_graph_matches_reachability_and_preserves_edge_types(self):
        f=dict(Class={('A',),('B',)},Extends={('B','A')},Implements=set(),
            Method={('A#start()',),('A#run()',),('B#run()',),('B#end()',)},
            InClass={('A#run()','A'),('B#run()','B')},DispatchMethod={('A#run()',),('B#run()',)},
            MethodSig={('A#run()','run','()'),('B#run()','run','()')},
            DirectCall={('A#start()','A#run()')},CandidateCall={('B#run()','B#end()','site','fallback')},
            VirtualCall={('A#start()','A','run','()')},ThreadEntry={('A#start()',)})
        incoming,_=call_graph(f);seen={'A#start()'}
        while True:
            new=seen|{target for target,callers in incoming.items() if seen & callers.keys()}
            if new==seen:break
            seen=new
        self.assertEqual({('A#start()',m) for m in seen},derive_reachability(f))
        self.assertEqual(incoming['A#run()']['A#start()'],{'direct','virtual_CHA'})
    def test_cycles_are_not_fake_roots(self):
        d={'a':{'b':{'direct'}},'b':{'a':{'direct'}}}
        self.assertEqual(reverse_frontier('a',d)['roots'],[])
        self.assertFalse(reverse_frontier('a',d)['truncated'])
    def test_hints_are_source_located(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'A.java';p.write_text('@Inject\nvoid f() { Class.forName("X"); }')
            h=source_hints(p)['hints']
            self.assertEqual(h['dependency_injection']['examples'][0]['line'],1)
            self.assertIn('reflection',h)

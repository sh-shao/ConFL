from pathlib import Path
import subprocess
import tempfile
import unittest

from confl.config import ROOT
from confl.kb import CKB
from confl.model import validate_entry_rules
from confl.report import collect_dsl_evidence

NODE = '''package rpc;

import org.apache.thrift.TProcessor;

public class Node implements Service, TProcessor {
    int term;
    public void handle(String message) { term++; }
    @Endpoint public void vote() { term = 0; }
    public void tick() { term--; }
    public boolean process(Object in, Object out) { return term > 0; }
    public static void helper() { }
}
'''


class DeclaredEntryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        src = root / 'node/src/main/java/rpc'
        src.mkdir(parents=True)
        (src / 'Node.java').write_text(NODE)
        (src / 'Service.java').write_text('package rpc;\npublic interface Service { void handle(String message); }\n')
        (src / 'Endpoint.java').write_text('package rpc;\npublic @interface Endpoint { }\n')
        cls.source, cls.facts = root, root / 'facts'
        subprocess.run(['java', '-jar', str(ROOT / 'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar'), 'rpc', str(root), str(cls.facts)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def kb(self, rules):
        return CKB(self.facts, self.source, extra_entries=validate_entry_rules(rules))

    def test_framework_methods_are_not_entries_by_default(self):
        self.assertFalse({'rpc.Node#handle(java.lang.String)', 'rpc.Node#vote()', 'rpc.Node#tick()'} & self.kb([]).entries)

    def test_each_rule_kind(self):
        kb = self.kb([{'supertype': 'rpc.Service', 'methods': ['handle']}, {'annotation': 'Endpoint'},
                      {'method': 'rpc.Node#tick'}, {'supertype': 'TProcessor'}, {'method': 'rpc.Node#missing'}])
        declared = {m for m, kind in kb.facts['EntryKind'] if kind == 'user-declared'}
        # process comes from the external supertype with all methods ('*'); static helper() is never an entry.
        self.assertEqual(declared, {'rpc.Node#handle(java.lang.String)', 'rpc.Node#vote()', 'rpc.Node#tick()',
                                    'rpc.Node#process(java.lang.Object,java.lang.Object)'})
        self.assertLessEqual(declared, kb.entries)
        self.assertEqual(kb.declared_entry_matches['method:rpc.Node#missing'], 0)
        self.assertEqual(kb.declared_entry_matches['supertype:rpc.Service[handle]'], 1)

    def test_declared_entries_interact_in_the_dsl(self):
        kb = self.kb([{'supertype': 'Service', 'methods': ['handle']}, {'annotation': 'rpc.Endpoint'}])
        pair = ['rpc.Node#handle(java.lang.String)', 'rpc.Node#vote()']
        interactions = kb.get_interaction_dsl(pair)
        self.assertIn('rpc.Node#term', {i['shared_resource'] for i in interactions})
        evidence = collect_dsl_evidence(interactions, [dict(rank=1, method='rpc.Node#vote()')], pair, kb)
        self.assertEqual(evidence['entry_kinds']['rpc.Node#vote()'], ['user-declared'])
        self.assertIn('rpc.Node#vote()', evidence['methods'][0]['declared_thread_entries'])

    def test_invalid_rules(self):
        for rule in ({}, {'method': 'noHash'}, {'annotation': 'A', 'methods': ['x']}, {'method': 'a.B#c', 'annotation': 'A'},
                     {'supertype': 'S', 'methods': []}, {'supertype': ''}, 'a.B#c'):
            with self.assertRaises(ValueError):
                validate_entry_rules([rule])


if __name__ == '__main__':
    unittest.main()

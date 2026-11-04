import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from confl.runner import Runner
from confl.ranking_review import comparison_packet

class ReviewTest(unittest.TestCase):
    def test_provisional_selection_can_switch_implementation(self):
        class Model:
            calls=[]
            def begin(self): self.n=0
            def __call__(self,request):
                self.n+=1
                if self.n==1:return dict(name='api.Lock#unlock()',explanation='initial hypothesis')
                if self.n==2:
                    assert request['last_tool_result']['proposed']=='api.Lock#unlock()'
                    assert any(o['name']=='impl.Lock#unlock()' for o in request['options'])
                    return dict(name='impl.Lock#unlock()',explanation='implementation releases state')
                assert request['last_tool_result']['proposed']=='impl.Lock#unlock()'
                return dict(name='impl.Lock#unlock()',explanation='Compared source; implementation updates shared state')
        kb=SimpleNamespace(get_code_snippet=lambda m:dict(method=m,lines=['void unlock();' if m.startswith('api') else 'void unlock() { state = 0; }']))
        with tempfile.TemporaryDirectory() as d:
            model=Model();r=Runner(kb,'unlock fails',Path(d),model=model)
            r.source_review_required=True;r.allowed_methods={'api.Lock#unlock()'};r.ranked_methods=set()
            r.full_method_pool=[dict(name=m,score=0) for m in ['api.Lock#unlock()','impl.Lock#unlock()']]
            self.assertEqual(r.choose('method_ranking',[dict(name='api.Lock#unlock()')]),'impl.Lock#unlock()')
            self.assertEqual(model.n,2)
            self.assertEqual(sum('provisional_selection' in t for t in r.trace),1)

    def test_packet_is_bounded_and_reports_missing_source(self):
        def read(m):
            if m=='A#x()':raise OSError()
            return dict(method=m,lines=['x'*1000]*100)
        packet=comparison_packet(SimpleNamespace(get_code_snippet=read),'A#x()', [dict(name=f'C{i}#x()') for i in range(10)])
        self.assertEqual(len(packet['sources']),4)
        self.assertEqual(packet['sources'][0]['unavailable'],'OSError')
        self.assertLessEqual(sum(map(len,packet['sources'][1]['lines'])),6000)
        self.assertTrue(packet['sources'][1]['truncated'])


class DispatchTest(unittest.TestCase):
    def make_kb(self, directory):
        """Minimal CKB without an extraction run: base lock, report-named read lock, factory."""
        from collections import defaultdict
        from confl.kb import CKB
        root = Path(directory)
        sources = {
            'p/Lock.java': 'package p;\nclass Lock {\n  void unlock() { release(); }\n  void release() { int a = 1; int b = 2; }\n  String name() { return name; }\n}\n',
            'p/ReadLock.java': 'package p;\nclass ReadLock extends Lock {\n  void release() { int a = 1; int b = 2; int c = 3; }\n}\n',
            'p/WriteLock.java': 'package p;\nclass WriteLock extends Lock {\n  void release() { int a = 1; }\n}\n',
            'p/Locks.java': 'package p;\nclass Locks {\n  Lock readLock() { return new ReadLock(); }\n}\n'}
        for file, text in sources.items():
            (root/file).parent.mkdir(parents=True, exist_ok=True)
            (root/file).write_text(text)
        kb = CKB.__new__(CKB)
        kb.source, kb._texts = root.resolve(), {}
        kb.locations = {'p.Lock#unlock()': 'p/Lock.java:3:3', 'p.Lock#release()': 'p/Lock.java:4:3', 'p.Lock#name()': 'p/Lock.java:5:3',
                        'p.ReadLock#release()': 'p/ReadLock.java:3:3', 'p.WriteLock#release()': 'p/WriteLock.java:3:3',
                        'p.Locks#readLock()': 'p/Locks.java:3:3'}
        kb.owners = {m: m.split('#')[0] for m in kb.locations}
        kb.methods = set(kb.locations)
        kb.facts = dict(DirectCall={('p.Locks#readLock()', 'p.ReadLock#<init>()')}, CandidateCall=set(),
                        VirtualCall={('p.Lock#unlock()', 'p.Lock', 'release', '()')})
        kb.children, kb.parents = defaultdict(set), defaultdict(set)
        for child in ('p.ReadLock', 'p.WriteLock'):
            kb.children['p.Lock'].add(child)
            kb.parents[child].add('p.Lock')
        return kb

    def test_override_that_runs_for_report_object_leads_comparison(self):
        with tempfile.TemporaryDirectory() as d:
            kb = self.make_kb(d)
            self.assertEqual(kb.body_kind('p.Locks#readLock()'), 'factory')
            self.assertEqual(kb.body_kind('p.Lock#name()'), 'accessor')
            self.assertEqual(kb.body_kind('p.Lock#unlock()'), 'wrapper')
            self.assertIsNone(kb.body_kind('p.Lock#release()'))
            # The report only calls readLock(); ReadLock is its object, WriteLock is just a sibling.
            self.assertIn('created by p.Locks#readLock()', kb.report_types('Locks.readLock() fails')['p.ReadLock'])
            pool = [dict(name=m) for m in sorted(kb.locations)]
            packet = comparison_packet(kb, 'p.Lock#unlock()', pool, report='Locks.readLock().unlock() throws')
            self.assertEqual([d['override'] for d in packet['runtime_dispatch']], ['p.ReadLock#release()'])
            self.assertEqual(packet['runtime_dispatch'][0]['called_from'], 'p.Lock#unlock()')
            self.assertEqual(packet['sources'][1]['method'], 'p.ReadLock#release()')
            self.assertEqual(packet['body_kind'], 'wrapper')
            self.assertIn('p.Lock#release()', packet['delegate_candidates'])

    def test_named_subclass_override_of_proposed_method(self):
        with tempfile.TemporaryDirectory() as d:
            kb = self.make_kb(d)
            packet = comparison_packet(kb, 'p.Lock#release()', [dict(name=m) for m in sorted(kb.locations)], report='ReadLock release is wrong')
            self.assertEqual(packet['runtime_dispatch'], [dict(override='p.ReadLock#release()', of='p.Lock#release()', report_evidence='named in report')])
            self.assertNotIn('body_kind', packet)

    def test_inert_means_short_and_without_state_effect(self):
        with tempfile.TemporaryDirectory() as d:
            kb = self.make_kb(d)
            kb.facts['Access'] = {('p.ReadLock#release()', 'p.ReadLock#state', 'W', 'p/ReadLock.java:3:3')}
            self.assertTrue(kb.is_inert('p.Lock#name()'))
            self.assertFalse(kb.is_inert('p.ReadLock#release()'))  # writes a field
            self.assertFalse(kb.is_inert('p.Lock#name()', max_lines=0))

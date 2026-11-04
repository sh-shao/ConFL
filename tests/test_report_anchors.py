import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from confl.report_anchors import AnchorIndex, report_anchors, expand
from confl.retrieval import retrieve_methods


class AnchorTest(unittest.TestCase):
    def make_kb(self, root):
        (root/'src/p').mkdir(parents=True)
        (root/'src/p/Lock.java').write_text(
            'package p;\nclass Lock {\n  void unlock() {\n    if (bad) throw new IllegalStateException("attempt to unlock lock, not locked by current thread: " + id);\n'
            '    release();\n  }\n  void release() { }\n  void lock() { }\n}\n')
        locations = {'p.Lock#unlock()': 'src/p/Lock.java:3:3', 'p.Lock#release()': 'src/p/Lock.java:7:3', 'p.Lock#lock()': 'src/p/Lock.java:8:3'}
        calls = {'p.Lock#unlock()': ['p.Lock#release()']}
        return SimpleNamespace(source=root.resolve(), locations=locations, methods=set(locations),
                               report_types=lambda report: {'p.Lock': 'named in report'},
                               methods_of=lambda cls: sorted(locations) if cls == 'p.Lock' else [],
                               is_declaration=lambda m: False, callees=lambda m: calls.get(m, []), overrides=lambda m: [])

    def test_frames_messages_and_calls_anchor_methods(self):
        with tempfile.TemporaryDirectory() as d:
            kb = self.make_kb(Path(d))
            report = ('Lock fails\njava.lang.IllegalStateException: attempt to unlock lock, not locked by current thread: 42\n'
                      '\tat p.Lock.unlock(Lock.java:4)\n\tat Main.main(Main.java:9)\n```\nlock.lock();\n```')
            anchors = report_anchors(kb, report, AnchorIndex(kb))
            self.assertEqual(anchors['p.Lock#unlock()']['kind'], 'stack_frame')  # frame wins over the quoted message
            self.assertEqual(anchors['p.Lock#lock()']['kind'], 'report_call')
            self.assertNotIn('p.Lock#release()', anchors)
            self.assertEqual(report_anchors(kb, report, AnchorIndex(kb), kinds=('message',)), {
                'p.Lock#unlock()': dict(kind='message', evidence='report quotes literal "attempt to unlock lock, not locked by current thread" at src/p/Lock.java:4')})
            self.assertEqual(expand(kb, anchors, report, hops=1)['p.Lock#release()'], 1)

    def test_anchors_take_every_fourth_shortlist_slot(self):
        methods = {f'p.C{i}#m{i}()': f'p.C{i}' for i in range(8)}
        kb = SimpleNamespace(meta={m: dict(component='c') for m in methods}, owners=methods, list_modules=lambda: [dict(name='c')])
        report = ' '.join(f'C{i}' for i in range(7))  # C7 is not named: lexically last
        base, _ = retrieve_methods(kb, report, 'c', [])
        rows, _ = retrieve_methods(kb, report, 'c', [], anchors={'p.C7#m7()': 0})
        self.assertEqual(base[-1]['name'], 'p.C7#m7()')
        self.assertEqual(rows[0]['name'], 'p.C7#m7()')
        self.assertIn('report_anchor', rows[0]['evidence_sources'])
        self.assertEqual([r['name'] for r in rows[1:]], [r['name'] for r in base[:-1]])

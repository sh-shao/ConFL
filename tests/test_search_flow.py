import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from confl.runner import Runner
from tests.fakes import FirstOptionModel
from confl.kb import fit_options

class SearchFlowTest(unittest.TestCase):
    def test_search_adds_selectable_method(self):
        class Model:
            calls=[]
            def begin(self): self.n=0
            def __call__(self,request):
                self.n+=1
                if self.n==1:return dict(tool='search_methods',arguments={'query':'Target'})
                assert any(o['name']=='pkg.Target#fix()' for o in request['options'])
                return dict(name='pkg.Target#fix()',explanation='Found matching source candidate')
        with tempfile.TemporaryDirectory() as d:
            runner=Runner(SimpleNamespace(), 'Target fails',Path(d),model=Model())
            runner.allowed_methods={'Other#run()'}
            runner.full_method_pool=[dict(name='pkg.Target#fix()',score=0,component='other')]
            runner.ranked_methods=set()
            self.assertEqual(runner.choose('method_ranking',[dict(name='Other#run()')]),'pkg.Target#fix()')

    def test_budget_forces_choice(self):
        class Model:
            calls=[]
            def begin(self): self.n=0
            def __call__(self,request):
                self.n+=1
                if request['tools']:return dict(tool='get_method_summary',arguments={'method_name':'A#m()'})
                return dict(name='A#m()',explanation='Choose using available evidence')
        with tempfile.TemporaryDirectory() as d:
            model=Model();runner=Runner(SimpleNamespace(get_method_summary=lambda **kw: {}),'report',Path(d),model=model)
            runner.allowed_methods={'A#m()'}
            self.assertEqual(runner.choose('method_ranking',[dict(name='A#m()')]),'A#m()')
            self.assertEqual(model.n,13)


class OverloadRankingTest(unittest.TestCase):
    def test_overloads_take_one_rank(self):
        methods = {'p.A#put()': 'p.A', 'p.A#put(int)': 'p.A', 'p.B#get()': 'p.B'}
        kb = SimpleNamespace(meta={m: dict(component='c', file='A.java') for m in methods}, owners=methods,
                             list_modules=lambda: [dict(name='c')], get_code_snippet=lambda m: dict(method=m))
        with tempfile.TemporaryDirectory() as d:
            runner = Runner(kb, 'put get', Path(d), model=FirstOptionModel(), top_k=3)
            runner.select_pair = lambda: ('c', None, None, None)
            runner.component_path = ['c']
            ranking = [r['method'].split('(')[0] for r in runner.run()['ranking']]
        self.assertEqual(sorted(ranking), ['p.A#put', 'p.B#get'])


class EntrySummaryTest(unittest.TestCase):
    def test_partners_capped_and_report_relevant_first(self):
        partners = [dict(entry=f'e{i}', resource_count=i) for i in range(40)] + [dict(entry='hit', resource_count=0)]
        kb = SimpleNamespace(get_entry_summary=lambda e: dict(entry=e, interacting_entries=partners, kinds=[]),
                             report_classes=lambda report: ({'p.Named'}, set()),
                             reached={'root': set(), 'hit': {'p.Named#m()'}, **{f'e{i}': set() for i in range(40)}},
                             owners={'p.Named#m()': 'p.Named'})
        with tempfile.TemporaryDirectory() as d:
            summary = Runner(kb, 'Named fails', Path(d)).entry_summary('root', limit=5)
        self.assertEqual([p['entry'] for p in summary['interacting_entries']], ['hit', 'e39', 'e38', 'e37', 'e36'])
        self.assertEqual((summary['interacting_entry_count'], summary['omitted_partner_count']), (41, 36))


class CodeToolTest(unittest.TestCase):
    def test_callers_callees_and_source_search_make_pool_methods_selectable(self):
        from tests.test_ranking_review import DispatchTest
        with tempfile.TemporaryDirectory() as d:
            kb = DispatchTest().make_kb(d)
            self.assertEqual(kb.callers('p.Lock#release()'), ['p.Lock#unlock()'])
            hits = kb.search_source('release()')
            self.assertEqual([(h['line'], h['method']) for h in hits['matches'] if h['file'] == 'p/Lock.java'], [(3, 'p.Lock#unlock()'), (4, 'p.Lock#release()')])
            runner = Runner(kb, 'ReadLock unlock fails', Path(d))
            runner.full_method_pool = [dict(name='p.Lock#release()'), dict(name='p.ReadLock#release()')]
            runner.allowed_methods, runner.ranked_methods = {'p.Lock#unlock()'}, set()
            options = [dict(name='p.Lock#unlock()')]
            response = runner.code_tool('get_callees', {'method_name': 'p.Lock#unlock()'}, options)
            self.assertEqual([(r['method'], r['relation']) for r in response['rows']],
                             [('p.Lock#release()', 'callee'), ('p.ReadLock#release()', 'override that runs for named in report')])
            self.assertEqual([o['name'] for o in options], ['p.Lock#unlock()', 'p.Lock#release()', 'p.ReadLock#release()'])
            runner.code_tool('get_callers', {'method_name': 'p.Lock#release()'})  # hypothesis stage: readable, not selectable
            self.assertIn('p.Lock#unlock()', runner.allowed_methods)


class HypothesisFlowTest(unittest.TestCase):
    def test_hypothesis_is_recorded_then_shown_to_every_rank(self):
        methods = {'p.A#put()': 'p.A', 'p.B#get()': 'p.B'}
        kb = SimpleNamespace(meta={m: dict(component='c', file='A.java') for m in methods}, owners=methods,
                             list_modules=lambda: [dict(name='c')], get_code_snippet=lambda m: dict(method=m),
                             search_source=lambda text: dict(matches=[dict(file='A.java', line=1, text='put', method='p.A#put()')]))
        seen, leaders = [], []
        class Model:
            calls = []
            def begin(self): self.turn = 0
            def __call__(self, request):
                self.turn += 1
                if request['stage'] == 'fault_hypothesis':
                    leaders.append([r['method'] for r in request['evidence']['retrieval_leaders']])
                    if self.turn == 1:
                        assert 'search_source' in request['tools'] and 'search_methods' not in request['tools']
                        return {'tool': 'search_source', 'arguments': {'text': 'put'}}
                    return {'name': 'record_hypothesis', 'explanation': 'put races with get on the shared map'}
                seen.append(request['evidence']['fault_hypothesis'])
                return {'name': request['options'][0]['name'], 'explanation': 'matches hypothesis'}
        with tempfile.TemporaryDirectory() as d:
            runner = Runner(kb, 'put get', Path(d), model=Model(), top_k=2)
            runner.select_pair = lambda: ('c', None, None, None)
            runner.component_path = ['c']
            runner.run()
            self.assertEqual(json.loads((Path(d)/'fault_hypothesis.json').read_text())['hypothesis'], 'put races with get on the shared map')
        self.assertEqual(set(seen), {'put races with get on the shared map'})
        # The hypothesis sees the shortlist head, not only report anchors (none here).
        self.assertTrue(leaders[0] and set(leaders[0]) <= set(methods))


class FitOptionsTest(unittest.TestCase):
    def test_keeps_leading_options_within_size_and_at_least_one(self):
        options = [dict(name='e%d' % i, pad='x' * 100) for i in range(50)]
        kept = fit_options(options, limit=1000)
        self.assertEqual(kept, options[:len(kept)])
        self.assertTrue(0 < len(kept) < 50)
        self.assertEqual(fit_options([dict(name='big', pad='x' * 5000)], limit=10), [dict(name='big', pad='x' * 5000)])


class ResolveMethodTest(unittest.TestCase):
    def runner(self, d):
        runner = Runner(None, 'report', Path(d))
        runner.allowed_methods = {'p.Lock#tryLock(long,java.util.concurrent.TimeUnit)'}
        runner.full_method_pool = [dict(name=m) for m in ('p.Out#write(int)', 'p.Enc#write(?Ctx,?Object)', 'p.List$anonymous@L.java:9:1#hasNext()')]
        return runner

    def test_whitespace_and_unique_names_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            runner = self.runner(d)
            self.assertEqual(runner.resolve_method('p.Lock#tryLock(long, java.util.concurrent.TimeUnit)'), 'p.Lock#tryLock(long,java.util.concurrent.TimeUnit)')
            # The class's anonymous iterator is the only hasNext(): resolved and now readable.
            self.assertEqual(runner.resolve_method('p.List#hasNext()'), 'p.List$anonymous@L.java:9:1#hasNext()')
            self.assertIn('p.List$anonymous@L.java:9:1#hasNext()', runner.allowed_methods)

    def test_ambiguous_names_suggest_readable_ids(self):
        with tempfile.TemporaryDirectory() as d:
            runner = self.runner(d)
            with self.assertRaisesRegex(ValueError, r'did you mean p.Out#write\(int\), p.Enc#write'):
                runner.resolve_method('write')
            self.assertIn('p.Out#write(int)', runner.allowed_methods)
            with self.assertRaisesRegex(ValueError, 'search_methods or search_source'):
                runner.resolve_method('write operation cancellation')

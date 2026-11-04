import tempfile
import unittest
from pathlib import Path
from confl.kb import CKB
from confl.runner import Runner


class NavigationTest(unittest.TestCase):
    def setUp(self):
        self.kb = CKB.__new__(CKB)
        self.kb.meta = {
            'a': dict(component='extensions/one', package='demo.one', file='one/A.java'),
            'b': dict(component='extensions/two', package='demo.two', file='two/A.java'),
            'c': dict(component='excluded', package='demo.hidden', file='hidden/A.java'),
            'd': dict(component='extensions-extra', package='demo.oneish', file='extra/B.java')}
        self.kb.owners = {'a':'demo.one.Helper', 'b':'demo.two.Helper', 'c':'demo.hidden.Helper', 'd':'demo.oneish.Other'}
        self.kb.entries = {'a','b','d'}
        self.kb.list_modules = lambda: [dict(name=n, concurrency_evidence={'thread_entries':1}) for n in ['extensions/one','extensions/two','extensions-extra']]

        self.kb.module_filter_report = lambda: [dict(name=n, included=n != 'excluded', reason='excluded_by_config' if n == 'excluded' else 'concurrency_evidence', evidence={'thread_entries': 1}, min_thread_entries=1, excluded_by=['excluded'] if n == 'excluded' else []) for n in ['extensions/one', 'extensions/two', 'extensions-extra', 'excluded']]

    def test_class_lookup_handles_ambiguity_and_exclusions(self):
        matches = self.kb.locate_class('Helper')['matches']
        self.assertEqual(len(matches), 3)
        self.assertEqual({m['component'] for m in matches}, {'extensions/one','extensions/two','excluded'})
        self.assertEqual(len(self.kb.locate_class('demo.one.Helper')['matches']),1)
        hidden = self.kb.locate_class('demo.hidden.Helper')['matches'][0]
        self.assertFalse(hidden['candidate_eligible'])
        self.assertEqual(hidden['filter_reason'], 'excluded_by_config')
        with self.assertRaises(ValueError):
            self.kb.locate_class(None)

    def test_package_and_parent_component_boundaries(self):
        self.assertEqual(len(self.kb.locate_package('demo.one')['matches']),1)
        self.assertEqual(len(self.kb.locate_package('demo')['matches']),4)
        packages=self.kb.get_component_packages('extensions')['packages']
        self.assertEqual({p['component'] for p in packages}, {'extensions/one','extensions/two'})
        self.assertEqual(packages[0]['classes'], ['demo.one.Helper'])
        with self.assertRaises(ValueError):
            self.kb.get_component_packages('excluded')

    def test_agent_queries_and_carries_evidence_to_subcomponent(self):
        class Model:
            calls=[]
            def begin(self): self.turn=0
            def __call__(self,request):
                self.turn+=1
                if request['stage']=='component':
                    if self.turn==1:
                        return dict(tool='locate_class', arguments={'class_name':'demo.one.Helper'})
                    assert 'extensions/one' in request['last_tool_result']['content']
                    return dict(name='extensions',explanation='The report class belongs to this subtree.')
                assert request['navigation_evidence'][0]['tool']=='locate_class'
                if self.turn==1:
                    return dict(tool='get_component_packages',arguments={'component_name':'extensions/one'})
                return dict(name='extensions/one',explanation='The package contains the report class.')
        with tempfile.TemporaryDirectory() as tmp:
            messages=[]
            runner=Runner(self.kb,'Report mentions demo.one.Helper',Path(tmp),model=Model(),progress=messages.append)
            self.assertEqual(runner.choose('component',[{'name':'extensions'}]),'extensions')
            self.assertEqual(runner.choose('subcomponent',[{'name':'extensions/one'},{'name':'extensions/two'}]),'extensions/one')
            self.assertEqual([x['tool'] for x in runner.trace if 'tool' in x],['locate_class','get_component_packages'])
            self.assertTrue(any('Tool: locate_class' in x for x in messages))

class LazyComponentDetailsTest(unittest.TestCase):
    def test_initial_prompt_is_compact_and_details_require_tool(self):
        from unittest.mock import Mock
        record=dict(name='server', description='Brief description.', detailed_description='LONG DETAILS',
                    knowledge={'representative_classes':['Server']}, concurrency_evidence={'thread_entries':9})
        kb=Mock()
        kb.get_component_details.return_value={'component':'server','modules':[record]}
        class Model:
            calls=[]
            def begin(self): self.turn=0
            def __call__(self, request):
                self.turn+=1
                assert request['options']==[{'name':'server','description':'Brief description.'}]
                if self.turn==1:
                    assert 'LONG DETAILS' not in str(request)
                    return {'tool':'get_component_details','arguments':{'component_name':'server'}}
                assert 'LONG DETAILS' in request['last_tool_result']['content']
                return {'name':'server','explanation':'Based on requested details'}
        with tempfile.TemporaryDirectory() as tmp:
            runner=Runner(kb,'report',Path(tmp),model=Model())
            self.assertEqual(runner.choose('component',[record]),'server')
            kb.get_component_details.assert_called_once_with(component_name='server')

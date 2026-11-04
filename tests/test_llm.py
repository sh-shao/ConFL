import copy
import json
import os
import unittest
from unittest.mock import patch
from confl.llm import APIModel, ModelError
from tests.fakes import FirstOptionModel


class APITest(unittest.TestCase):
    def make(self, provider, transport):
        with patch.dict(os.environ, {'TEST_KEY': 'secret-test-value'}):
            return APIModel({'provider': provider, 'model': 'test-model', 'api_key_env': 'TEST_KEY'}, transport)

    def test_both_providers_keep_tool_context(self):
        for provider in ('openai', 'anthropic'):
            with self.subTest(provider=provider):
                sent = []
                def transport(url, headers, body):
                    sent.append((url, headers, copy.deepcopy(body)))
                    action = {'method_name': 'm', 'offset': 0} if len(sent) == 1 else {'name': 'm', 'explanation': 'source evidence'}
                    function_name = 'get_code_snippet' if len(sent) == 1 else 'select_candidate'
                    if provider == 'openai':
                        return {'status': 'completed', 'output': [{'type': 'function_call', 'name': function_name, 'call_id': 'c1', 'arguments': json.dumps(action)}]}
                    return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 'c1', 'name': function_name, 'input': action}]}
                model = self.make(provider, transport)
                request = {'stage': 'method_ranking', 'options': [{'name': 'm'}], 'tools': {'get_code_snippet': ['method_name']}}
                self.assertEqual(model(request)['tool'], 'get_code_snippet')
                request['last_tool_result'] = {'content': 'source line'}
                self.assertEqual(model(request)['name'], 'm')
                body = sent[-1][2]
                history = body.get('input', body.get('messages'))
                self.assertIn('source line', json.dumps(history))
                self.assertIn('c1', json.dumps(history))
                self.assertNotIn('secret-test-value', json.dumps(model.calls))
                if provider == 'openai':
                    self.assertTrue(sent[0][0].endswith('/v1/responses'))
                    self.assertIn('Authorization', sent[0][1])
                    self.assertFalse(body['store'])
                else:
                    self.assertTrue(sent[0][0].endswith('/v1/messages'))
                    self.assertIn('x-api-key', sent[0][1])
                model.begin()
                self.assertEqual(model.history, [])
                self.assertIsNone(model.pending)

    def test_config_paths_and_top_k_reach_localize(self):
        import tempfile
        from pathlib import Path
        from confl import cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/'src').mkdir()
            (root/'report.txt').write_text('bug report')
            cfg = root/'config.json'
            cfg.write_text(json.dumps({'llm': {'provider': 'openai', 'model': 'test'}, 'knowledge_base': {'dsl_prior_weight': 0},
                                       'localization': {'top_k': 2}}))
            with patch.object(cli, 'APIModel') as api, patch.object(cli, 'ensure_facts', return_value={'status': 'skipped_valid'}) as facts, \
                    patch.object(cli, 'load_kb') as kb, patch.object(cli, 'localize', return_value={'status': 'completed', 'ranking': []}) as run, \
                    patch('builtins.print'):
                self.assertEqual(cli.main(['localize', '--config', str(cfg), '--source', str(root/'src'), '--report', str(root/'report.txt'),
                                           '--output', str(root/'out')]), 0)
                self.assertEqual(facts.call_args.args[:2], (root/'src', root/'out'/'facts'))
                self.assertEqual(kb.call_args.args[2], dict(min_thread_entries=1, exclude_components=[], client_api=False, dsl_prior_weight=0, extra_entries=[]))
                self.assertEqual(run.call_args.args[3], api.return_value)
                self.assertEqual(run.call_args.args[4], 2)

    def test_missing_key_fails_before_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Missing API key'):
                APIModel({'provider': 'openai', 'model': 'test'})

    def test_config_rejects_literal_secret_and_bad_provider(self):
        with self.assertRaises(ValueError):
            APIModel({'provider': 'bad', 'model': 'test'})
        with self.assertRaisesRegex(ValueError, 'api_key_env'):
            APIModel({'provider': 'openai', 'model': 'test', 'api_key': 'do-not-store'})

    def test_incomplete_responses_are_not_rankings(self):
        for provider, response in [('openai', {'status': 'incomplete'}), ('anthropic', {'stop_reason': 'max_tokens'})]:
            model = self.make(provider, lambda *a: response)
            with self.assertRaises(ModelError):
                model({'options': []})

    def test_claude_text_reply_is_nudged_to_a_tool_call(self):
        sent = []
        def transport(url, headers, body):
            sent.append(copy.deepcopy(body))
            if len(sent) == 1:
                return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'I pick m.'}]}
            return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'select_candidate',
                                                            'input': {'name': 'm', 'explanation': 'evidence'}}]}
        with patch.dict(os.environ, {'TEST_KEY': 'k'}):
            model = APIModel({'provider': 'anthropic', 'model': 'test-model', 'api_key_env': 'TEST_KEY', 'effort': 'high'}, transport)
        self.assertEqual(model({'options': [{'name': 'm'}]})['name'], 'm')
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]['tool_choice'], {'type': 'auto', 'disable_parallel_tool_use': True})
        self.assertEqual(sent[0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(sent[0]['output_config'], {'effort': 'high'})
        # The text turn stays in history (append-only) before the reminder.
        self.assertEqual([m['role'] for m in sent[1]['messages']], ['user', 'assistant', 'user'])
        self.assertEqual(len(model.calls), 2)

    def test_http_error_does_not_leak_body(self):
        import io
        import urllib.error
        model = self.make('openai', None)
        error = urllib.error.HTTPError('https://example.com', 401, 'secret-test-value', {}, io.BytesIO(b'secret-test-value'))
        with patch('urllib.request.OpenerDirector.open', side_effect=error):
            with self.assertRaises(ModelError) as caught:
                model({'options': []})
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('secret-test-value', str(caught.exception))

    def test_http_error_reports_provider_message(self):
        import io
        import urllib.error
        model = self.make('anthropic', None)
        body = json.dumps({'error': {'type': 'invalid_request_error', 'message': 'tools.0.strict: unsupported keyword minimum'}}).encode()
        error = urllib.error.HTTPError('https://example.com', 400, 'bad', {}, io.BytesIO(body))
        with patch('urllib.request.OpenerDirector.open', side_effect=error):
            with self.assertRaises(ModelError) as caught:
                model({'options': []})
        self.assertIn('400: tools.0.strict: unsupported keyword minimum', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

class ScopeRecoveryTest(unittest.TestCase):
    def test_early_method_call_returns_error_then_allows_selection(self):
        import tempfile
        from pathlib import Path
        from confl.runner import Runner
        class Model:
            calls = []
            def begin(self):
                self.count = 0
            def __call__(self, request):
                self.count += 1
                if self.count == 1:
                    assert 'get_code_snippet' not in request['tools']
                    assert 'locate_class' in request['tools']
                    return {'tool': 'get_code_snippet', 'arguments': {'method_name': 'invented'}}
                assert request['last_tool_result']['is_error']
                return {'name': 'component', 'explanation': 'Corrected selection'}
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(None, 'report', Path(directory), model=Model())
            self.assertEqual(runner.choose('component', [{'name': 'component'}]), 'component')
            self.assertTrue(runner.trace[0]['response']['is_error'])

    def test_out_of_scope_method_is_never_executed(self):
        import tempfile
        from pathlib import Path
        from confl.runner import Runner
        from unittest.mock import Mock
        class Model:
            calls = []
            def begin(self):
                self.count = 0
            def __call__(self, request):
                self.count += 1
                if self.count == 1:
                    return {'tool': 'get_code_snippet', 'arguments': {'method_name': 'outside'}}
                assert request['last_tool_result']['is_error']
                return {'name': 'inside', 'explanation': 'Corrected selection'}
        with tempfile.TemporaryDirectory() as directory:
            kb = Mock()
            runner = Runner(kb, 'report', Path(directory), model=Model())
            runner.allowed_methods = {'inside'}
            self.assertEqual(runner.choose('method_ranking', [{'name': 'inside'}]), 'inside')
            kb.get_code_snippet.assert_not_called()

class BacktrackingTest(unittest.TestCase):
    def test_dead_component_backtracks_without_inventing_pairs(self):
        from confl.runner import Runner
        from pathlib import Path
        from unittest.mock import Mock
        import tempfile
        kb = Mock()
        kb.entries = {'dead_entry', 'a', 'b'}
        kb.meta = {'dead_entry': {'component': 'dead'}, 'a': {'component': 'live'}, 'b': {'component': 'live'}}
        kb.list_modules.return_value = [{'concurrency_evidence': {'thread_entries': 1}, 'name': 'dead'}, {'concurrency_evidence': {'thread_entries': 1}, 'name': 'live'}]
        kb.get_entry_summary.side_effect = lambda e: {'interacting_entries': [] if e == 'dead_entry' else [{'entry': 'b' if e == 'a' else 'a', 'resource_count': 1}]}
        kb.list_packages_under_component.side_effect = lambda c: [{'name': c+'.pkg'}]
        kb.list_entries_under_package.side_effect = lambda p,c: [{'name': 'dead_entry'}] if c == 'dead' else [{'name': 'a'}]
        kb.report_classes.return_value = (set(), set())
        kb.reached, kb.owners = {'dead_entry': set(), 'a': set(), 'b': set()}, {}
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(kb, 'report', Path(tmp))
            runner.choose = lambda stage, options, evidence=None: options[0]['name']
            self.assertEqual(runner.select_pair(), ('live', 'live.pkg', 'a', 'b'))
            self.assertEqual(runner.trace[0]['backtrack']['component'], 'dead')

    def test_all_dead_terminates(self):
        from confl.runner import Runner
        from pathlib import Path
        from unittest.mock import Mock
        import tempfile
        kb = Mock()
        kb.entries = set()
        kb.list_modules.return_value = [{'concurrency_evidence': {'thread_entries': 1}, 'name': 'dead'}]
        kb.list_packages_under_component.return_value = []
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(kb, 'report', Path(tmp), model=FirstOptionModel())
            self.assertEqual(runner.select_pair(), ('dead', None, None, None))

class SelectionRecoveryTest(unittest.TestCase):
    def test_invalid_selection_is_logged_and_retried(self):
        from confl.runner import Runner
        from pathlib import Path
        import tempfile
        class Model:
            calls = []
            def begin(self):
                self.count = 0
            def __call__(self, request):
                self.count += 1
                if self.count == 1:
                    return {'name': 'previously_ranked', 'explanation': 'invalid'}
                assert request['last_tool_result']['allowed_names'] == ['remaining']
                return {'name': 'remaining', 'explanation': 'corrected'}
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(None, 'report', Path(tmp), model=Model())
            self.assertEqual(runner.choose('method_ranking', [{'name': 'remaining'}]), 'remaining')
            self.assertEqual(runner.trace[0]['rejected_selection']['name'], 'previously_ranked')

    def test_repeated_invalid_selections_stop_at_budget(self):
        from confl.runner import Runner
        from pathlib import Path
        from unittest.mock import Mock
        import tempfile
        model = Mock()
        model.calls = []
        model.return_value = {'name': 'outside', 'explanation': 'invalid'}
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(None, 'report', Path(tmp), model=model)
            with self.assertRaisesRegex(ModelError, '16 attempts'):
                runner.choose('component', [{'name': 'allowed'}])
            self.assertEqual(model.call_count, 16)
            self.assertEqual(len(runner.trace), 16)

class NativeToolSchemaTest(unittest.TestCase):
    def test_component_argument_is_required_in_native_schema(self):
        from confl.llm import tool_definitions
        definitions = tool_definitions({'options':[{'name':'server'}], 'tools':{'get_component_packages':['component_name']}})
        by_name = {d['name']:d['schema'] for d in definitions}
        self.assertEqual(by_name['get_component_packages']['required'], ['component_name','offset'])
        self.assertNotIn('arguments_json', by_name['get_component_packages']['properties'])
        self.assertEqual(by_name['select_candidate']['properties']['name']['enum'], ['server'])

    def test_missing_argument_reports_its_name_and_recovers(self):
        from confl.runner import Runner
        from pathlib import Path
        import tempfile
        class Model:
            calls=[]
            def begin(self): self.count=0
            def __call__(self, request):
                self.count+=1
                if self.count==1:
                    return {'tool':'get_component_packages','arguments':{}}
                assert 'component_name' in request['last_tool_result']['error']
                return {'name':'server','explanation':'corrected'}
        with tempfile.TemporaryDirectory() as tmp:
            runner=Runner(None,'report',Path(tmp),model=Model())
            self.assertEqual(runner.choose('component',[{'name':'server'}]),'server')

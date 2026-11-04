import unittest
from confl.costs import estimate, pricing, summarize_costs, combine_costs


class CostTest(unittest.TestCase):
    def test_openai_cache_and_reasoning(self):
        call = dict(provider='openai', usage=dict(input_tokens=1000, output_tokens=100,
                    input_tokens_details={'cached_tokens': 400}, output_tokens_details={'reasoning_tokens': 50}))
        self.assertAlmostEqual(estimate(call, {'input': 1, 'cached_input': .5, 'output': 2}), .001)

    def test_anthropic_cache_is_additive(self):
        call = dict(provider='anthropic', usage=dict(input_tokens=1000, output_tokens=100,
                    cache_read_input_tokens=400, cache_creation_input_tokens=200))
        self.assertAlmostEqual(estimate(call, {'input': 1, 'cached_input': .5, 'cache_write': 2, 'output': 2}), .0018)
        self.assertIsNone(estimate(call, {'input': 1, 'output': 2}))

    def test_missing_and_unknown(self):
        self.assertIsNone(estimate({'provider': 'openai', 'usage': {}}, {'input': 1, 'output': 2}))
        self.assertIsNone(pricing({'provider': 'openai', 'model': 'unknown'}))
        self.assertIsNone(pricing({'provider': 'openai', 'model': 'gpt-4o-mini', 'base_url': 'https://custom/v1'}))
        summary = summarize_costs([{'estimated_cost_usd': .01}, {}])
        self.assertIsNone(summary['estimated_cost_usd'])
        self.assertEqual(summary['priced_subtotal_usd'], .01)
        self.assertEqual(combine_costs([summary])['unpriced_calls'], 1)

    def test_invalid_rates(self):
        for value in (-1, float('nan'), True):
            with self.assertRaises(ValueError):
                pricing({'pricing': {'input': value, 'output': 1}})

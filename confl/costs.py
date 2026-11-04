"""Local token-cost estimates; never queries billing or sends API requests."""
import math

DEFAULT = {'input': 0.15, 'cached_input': 0.075, 'output': 0.60}
SOURCE = 'https://developers.openai.com/api/docs/models/gpt-4o-mini'


def pricing(config):
    rates = config.get('pricing')
    if rates is not None:
        if not isinstance(rates, dict) or not {'input', 'output'} <= rates.keys():
            raise ValueError('llm.pricing requires input and output USD per million tokens')
        for key, value in rates.items():
            if key not in ('input', 'output', 'cached_input', 'cache_write') or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid llm.pricing rate: ' + key)
        return dict(rates)
    if (config.get('provider') == 'openai'
            and config.get('model') in ('gpt-4o-mini', 'gpt-4o-mini-2024-07-18')
            and config.get('base_url', 'https://api.openai.com/v1').rstrip('/') == 'https://api.openai.com/v1'):
        return dict(DEFAULT)
    return None


def estimate(call, rates):
    usage = call.get('usage') or {}
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens', 'output_tokens')):
        return None
    details = usage.get('input_tokens_details') or {}
    cached = details.get('cached_tokens', 0) if call['provider'] == 'openai' else usage.get('cache_read_input_tokens', 0)
    write = details.get('cache_write_tokens', 0) if call['provider'] == 'openai' else usage.get('cache_creation_input_tokens', 0)
    if any(type(n) is not int or n < 0 for n in (cached, write)):
        return None
    normal = usage['input_tokens'] - cached - write if call['provider'] == 'openai' else usage['input_tokens']
    if normal < 0 or rates is None:
        return None
    counts = {'input': normal, 'cached_input': cached, 'cache_write': write, 'output': usage['output_tokens']}
    if any(n and key not in rates for key, n in counts.items()):
        return None
    return sum(n * rates.get(key, 0) for key, n in counts.items()) / 1_000_000


def summarize_costs(calls):
    amounts = [c.get('estimated_cost_usd') for c in calls]
    missing = sum(v is None for v in amounts)
    subtotal = round(sum(v for v in amounts if v is not None), 10)
    return dict(calls=len(calls), unpriced_calls=missing,
                estimated_cost_usd=None if missing else subtotal,
                priced_subtotal_usd=subtotal, currency='USD',
                note='Estimate for recorded API responses only; not account billing. Failed requests without usage, discounts and taxes are not included.')


def display_cost(summary):
    amount = summary['estimated_cost_usd']
    return f'${amount:.6f}' if amount is not None else f"N/A ({summary['unpriced_calls']} unpriced calls; known subtotal ${summary['priced_subtotal_usd']:.6f})"


def combine_costs(summaries):
    result = summarize_costs([])
    for key in ('calls', 'unpriced_calls', 'priced_subtotal_usd'):
        result[key] = sum(s[key] for s in summaries)
    result['priced_subtotal_usd'] = round(result['priced_subtotal_usd'], 10)
    result['estimated_cost_usd'] = None if result['unpriced_calls'] else result['priced_subtotal_usd']
    return result

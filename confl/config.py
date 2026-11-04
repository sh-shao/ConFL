"""One JSON configuration for extraction, localization and evaluation; relative paths resolve against the file."""
import copy
import json
from pathlib import Path

from .kb import normalize_component_exclusions
from .model import validate_entry_rules
from .prefilter import normalize_exclusions

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / 'configs'
DEFAULT_MODEL = 'gpt-6.1-sol'
DEFAULT_CONFIG = CONFIG_DIR / f'{DEFAULT_MODEL}.json'

DEFAULTS = {
    'llm': {},
    'knowledge_base': {'min_thread_entries': 1, 'client_api_entries': False, 'dsl_prior_weight': 8, 'exclude_components': [],
                       'extra_entries': []},
    'extraction': {'extractor_jar': str(ROOT / 'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar'),
                   'prefilter': 'concurrency_modules', 'exclude_modules': [], 'java_options': [], 'jobs': 1},
    'localization': {'top_k': 5},
    'dataset': {'ground_truth': str(ROOT / 'ground-truth/github/all.json'), 'source_root': None,
                'reports_dir': str(ROOT / 'examples/reports'), 'facts_root': str(ROOT / 'output/facts'),
                'output': str(ROOT / 'output/evaluation'), 'projects': None, 'limit': None},
}
PATHS = {'extraction': ('extractor_jar',), 'dataset': ('ground_truth', 'source_root', 'reports_dir', 'facts_root', 'output')}


def resolve_config(value=None):
    """A configuration file: a path, or a name in configs/ (e.g. claude-opus-5-5 for configs/claude-opus-5-5.json)."""
    if value is None:
        return DEFAULT_CONFIG
    path = Path(value)
    if path.is_file():
        return path
    named = CONFIG_DIR / (path.name if path.suffix == '.json' else path.name + '.json')
    if path.parent == Path('.') and named.is_file():
        return named
    available = ', '.join(sorted(p.stem for p in CONFIG_DIR.glob('*.json')))
    raise ValueError(f'Configuration not found: {value} (available in configs/: {available})')


def load_config(path=None):
    """Defaults overlaid with the file's sections; path-valued fields become absolute Paths (None when unset)."""
    path = resolve_config(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ValueError('Unknown configuration section(s): ' + ', '.join(sorted(unknown)))
    config = copy.deepcopy(DEFAULTS)
    for section, values in data.items():
        if not isinstance(values, dict):
            raise ValueError(f'Configuration section {section} must be an object')
        config[section].update(values)
    for section, keys in PATHS.items():
        for key in keys:
            value = config[section][key]
            config[section][key] = (path.parent / value).resolve() if value else None
    validate(config)
    config['path'] = path
    return config


def validate(config):
    kb = config['knowledge_base']
    if type(kb['min_thread_entries']) is not int or kb['min_thread_entries'] < 0:
        raise ValueError('knowledge_base.min_thread_entries must be a nonnegative integer')
    if type(kb['client_api_entries']) is not bool:
        raise ValueError('knowledge_base.client_api_entries must be true or false')
    if type(kb['dsl_prior_weight']) not in (int, float) or kb['dsl_prior_weight'] < 0:
        raise ValueError('knowledge_base.dsl_prior_weight must be a nonnegative number')
    kb['exclude_components'] = normalize_component_exclusions(kb['exclude_components'])
    validate_entry_rules(kb['extra_entries'])
    ex = config['extraction']
    if ex['prefilter'] not in ('off', 'exclude_only', 'concurrency_modules'):
        raise ValueError('extraction.prefilter must be off, exclude_only or concurrency_modules')
    ex['exclude_modules'] = normalize_exclusions(ex['exclude_modules'])
    if not isinstance(ex['java_options'], list) or not all(isinstance(x, str) for x in ex['java_options']):
        raise ValueError('extraction.java_options must be a list of strings')
    if type(ex['jobs']) is not int or ex['jobs'] < 1:
        raise ValueError('extraction.jobs must be a positive integer')
    top_k = config['localization']['top_k']
    if type(top_k) is not int or top_k < 1:
        raise ValueError('localization.top_k must be a positive integer')
    limit = config['dataset']['limit']
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('dataset.limit must be null or a positive integer')


def kb_options(config):
    """Keyword arguments for CKB."""
    kb = config['knowledge_base']
    return dict(min_thread_entries=kb['min_thread_entries'], exclude_components=kb['exclude_components'],
                client_api=kb['client_api_entries'], dsl_prior_weight=kb['dsl_prior_weight'], extra_entries=kb['extra_entries'])

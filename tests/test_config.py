import json
from pathlib import Path
import tempfile
import unittest

from confl.config import CONFIG_DIR, DEFAULT_CONFIG, load_config, resolve_config


class ConfigTest(unittest.TestCase):
    def test_model_names_resolve_to_configs(self):
        self.assertEqual(resolve_config(), DEFAULT_CONFIG)
        self.assertEqual(resolve_config('claude-opus-5-5'), CONFIG_DIR / 'claude-opus-5-5.json')
        self.assertEqual(resolve_config('gpt-4o-mini.json'), CONFIG_DIR / 'gpt-4o-mini.json')
        with self.assertRaisesRegex(ValueError, 'available in configs/: .*gpt-6.1-sol'):
            resolve_config('no-such-model')

    def test_every_shipped_config_loads(self):
        for path in CONFIG_DIR.glob('*.json'):
            config = load_config(path.stem)
            self.assertEqual(config['llm']['model'], path.stem)
            self.assertTrue(config['extraction']['extractor_jar'].name.endswith('.jar'))

    def test_file_path_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mine.json'
            path.write_text(json.dumps({'llm': {'provider': 'openai', 'model': 'mine'}}))
            self.assertEqual(load_config(str(path))['llm']['model'], 'mine')


if __name__ == '__main__':
    unittest.main()

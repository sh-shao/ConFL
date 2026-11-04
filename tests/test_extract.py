from pathlib import Path
import shutil
import tempfile
import unittest
from confl.config import ROOT, DEFAULTS
from confl.extract import ensure_facts, extract_dataset


class ExtractTest(unittest.TestCase):
    def test_dataset_extract_validate_and_skip_without_gt_or_report(self):
        settings = dict(DEFAULTS['extraction'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT/'examples/concurrency', root/'sources/demo_1')
            cases = [('demo_1', root/'sources/demo_1', root/'facts/demo_1/facts')]
            run = lambda: extract_dataset(cases, settings, root/'logs', progress=lambda m: None)[0]['status']
            self.assertEqual(run(), 'generated')
            self.assertEqual(run(), 'skipped_valid')
            (root/'facts/demo_1/facts/schema-version.txt').write_text('invalid')
            self.assertEqual(run(), 'invalid_existing')
            self.assertEqual((root/'facts/demo_1/facts/schema-version.txt').read_text(), 'invalid')
            with self.assertRaisesRegex(ValueError, 'failed validation'):
                ensure_facts(root/'sources/demo_1', root/'facts/demo_1/facts', settings, root/'logs/x.log')

    def test_missing_jar_explains_how_to_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = dict(DEFAULTS['extraction'], extractor_jar=Path(tmp)/'missing.jar')
            with self.assertRaisesRegex(ValueError, 'mvn -f FactsExtractor/pom.xml package'):
                ensure_facts(ROOT/'examples/concurrency', Path(tmp)/'facts', settings, Path(tmp)/'x.log')


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import tempfile
import unittest
from confl.prefilter import select_sources


class PrefilterTest(unittest.TestCase):
    def test_module_dependencies_and_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for module,name,body in [('service','Worker','class Worker implements Runnable { Helper h; }'),
                                     ('common','Helper','class Helper { }'),
                                     ('unused','Unrelated','class Unrelated { }'),
                                     ('examples','Demo','class Demo implements Runnable { }')]:
                p=root/module/'src/main/java'/f'{name}.java';p.parent.mkdir(parents=True);p.write_text(body)
            selected, report=select_sources(root)
            self.assertEqual({p.split('/')[0] for p in selected}, {'service','common'})
            self.assertEqual(report['selected_java_files'],2)
            self.assertEqual(len(select_sources(root,'off')[0]),4)
            self.assertEqual(len(select_sources(root,'exclude_only')[0]),3)

    def test_no_signals_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'Plain.java').write_text('class Plain {}')
            self.assertEqual(select_sources(tmp)[0],[])

    def test_java_honors_allowlist_before_analysis(self):
        import os
        import subprocess
        root_repo=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'src';source.mkdir()
            (source/'Keep.java').write_text('class Keep { int x; void f(){x++;} }')
            (source/'Drop.java').write_text('class Drop { int y; void g(){y++;} }')
            listing=root/'allow.txt';listing.write_text('Keep.java\n')
            out=root/'facts'
            proc=subprocess.run(['java','-jar',str(root_repo/'FactsExtractor/target/ConFL-1.0-SNAPSHOT.jar'),'test',str(source),str(out)],env=dict(os.environ,CONFL_SOURCE_ALLOWLIST=str(listing)),capture_output=True,text=True,check=True)
            methods=(out/'Method.facts').read_text()
            self.assertIn('Keep#f()',methods)
            self.assertNotIn('Drop#',methods)
            self.assertIn('Source prefilter: 1/2',proc.stdout)

    def test_explicit_exclusion_is_prefix_bounded_and_overrides_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for module,name,body in [('app','Worker','class Worker implements Runnable { Helper h; }'),
                                     ('extensions/kafka','Helper','class Helper {}'),
                                     ('extensions/kafka-extra','Other','class Other implements Runnable {}')]:
                f=root/module/'src/main/java'/f'{name}.java';f.parent.mkdir(parents=True);f.write_text(body)
            files, report=select_sources(root, 'concurrency_modules', ['extensions/kafka/'])
            self.assertFalse(any(f.startswith('extensions/kafka/') for f in files))
            self.assertTrue(any(f.startswith('extensions/kafka-extra/') for f in files))
            self.assertIn('extensions/kafka',report['explicitly_excluded_directories'])
            files, _=select_sources(root, 'off', ['extensions'])
            self.assertEqual(len(files),1)

    def test_invalid_exclusion(self):
        from confl.prefilter import normalize_exclusions
        for value in ['examples', [None], ['../src'], ['/tmp'], [''], ['extensions/*']]:
            with self.assertRaises(ValueError):
                normalize_exclusions(value)

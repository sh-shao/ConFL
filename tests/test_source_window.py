import unittest
from confl.source_window import method_window

class WindowTest(unittest.TestCase):
    def test_strings_comments_and_nested_blocks(self):
        text='void run() {\n String lua="redis.call(\" + "\\\"{\\\"" + ")";\n /* } */ if (true) { act(); }\n}\nvoid next() {}'
        window=method_window(text,1)
        self.assertTrue(window['boundary_found'])
        self.assertNotIn('void next',window['text'])
        self.assertIn('redis.call',window['text'])
    def test_interface_and_annotation(self):
        text='@SuppressWarnings({"a", "b"})\nvoid run();\nvoid next();'
        self.assertEqual(method_window(text,1)['text'],text.split('\nvoid next')[0])
    def test_long_body_not_cut_at_80_lines(self):
        text='void run() {\n'+'doWork();\n'*150+'}\nvoid next() {}'
        window=method_window(text,1)
        self.assertEqual(window['text'].count('doWork'),150)
        self.assertNotIn('next',window['text'])

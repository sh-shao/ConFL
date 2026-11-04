import json
import tempfile
import unittest
from pathlib import Path
from confl.metrics import evaluate_ranking
from confl.metrics import summarize


class RankingMetricsTest(unittest.TestCase):
    def test_multiple_relevant_and_missing_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            gt = Path(directory)/'gt.json'
            gt.write_text(json.dumps([dict(bugID='test', files=[dict(file='C.java', methods=['C::a', 'C::b', 'C::c', 'C::a'])])]))
            ranking = [dict(rank=i, file='C.java', method='C#'+name+'()') for i, name in enumerate(['x', 'a', 'a', 'b', 'z'], 1)]
            e = evaluate_ranking(dict(mode='test', ranking=ranking, ranking_cutoff=5), gt, 'test')
            self.assertAlmostEqual(e['average_precision'], (1/2+2/4)/3)
            self.assertEqual(e['reciprocal_rank'], .5)
            self.assertEqual(e['relevant_method_count'], 3)
            r = dict(status='completed', root_hit=True, leaf_hit=True, any_target_available=True, method_evaluation=e)
            m = summarize([r])['method_metrics']
            self.assertEqual(m['mrr'], .5)
            self.assertAlmostEqual(m['map'], 1/3)
            e = evaluate_ranking(dict(mode='test', ranking=[], ranking_cutoff=5), gt, 'test')
            self.assertEqual(e['average_precision'], 0)

    def test_nested_class_ground_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            gt=Path(directory)/'gt.json'
            gt.write_text(json.dumps([dict(bugID='nested',files=[dict(file='Outer.java',methods=['Outer::Inner::get()'])])]))
            e=evaluate_ranking(dict(mode='test',ranking=[dict(rank=1,file='Outer.java',method='pkg.Outer.Inner#get()')]),gt,'nested')
            self.assertTrue(e['top1'])

    def test_constructor_ground_truth_matches_init(self):
        with tempfile.TemporaryDirectory() as directory:
            gt=Path(directory)/'gt.json'
            gt.write_text(json.dumps([dict(bugID='ctor',files=[dict(file='S.java',methods=['Server::Server(a, b)','Outer::Inner::Inner()'])])]))
            ranking=[dict(rank=1,file='S.java',method='p.Server#start()'),dict(rank=2,file='S.java',method='p.Server#<init>(?A,?B)'),
                     dict(rank=3,file='S.java',method='p.Outer.Inner#<init>()')]
            e=evaluate_ranking(dict(mode='test',ranking=ranking,ranking_cutoff=5),gt,'ctor')
            self.assertEqual([m['rank'] for m in e['methods']],[2,3])

    def test_partial_ranking_of_a_stopped_run(self):
        with tempfile.TemporaryDirectory() as directory:
            gt=Path(directory)/'gt.json'
            gt.write_text(json.dumps([dict(bugID='stopped',files=[dict(file='C.java',methods=['C::cancel()'])])]))
            partial=dict(status='model_error',error='timeout',ranking=[dict(rank=1,file='C.java',method='p.C#cancel()')],ranking_cutoff=5)
            self.assertTrue(evaluate_ranking(partial,gt,'stopped')['top1'])

    def test_label_without_class_uses_file_type(self):
        with tempfile.TemporaryDirectory() as directory:
            gt=Path(directory)/'gt.json'
            gt.write_text(json.dumps([dict(bugID='bare',files=[dict(file='src/p/Provider.java',methods=['factory(vertx, options)'])])]))
            e=evaluate_ranking(dict(mode='test',ranking=[dict(rank=1,file='src/p/Provider.java',method='p.Provider#factory(?Vertx,?Options)')],ranking_cutoff=5),gt,'bare')
            self.assertTrue(e['top1'])

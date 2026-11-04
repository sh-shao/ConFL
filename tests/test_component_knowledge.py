import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from confl.components import build_component_record
from confl.kb import component_options


class ComponentKnowledgeTest(unittest.TestCase):
    def test_description_is_grounded_in_docs_and_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)
            (source/'worker').mkdir()
            (source/'worker/pom.xml').write_text('<project><name>Worker</name><description>Executes background jobs</description></project>')
            (source/'worker/README.md').write_text('# Worker\n\nThis module schedules background work and manages worker execution.\n')
            kb=SimpleNamespace(source=source,
                meta={'m':{'component':'worker','package':'demo.jobs'}},
                owners={'m':'demo.jobs.Worker'}, entries={'m'},
                facts={'EntryKind':{('m','executor')},'Sync':{('m','lock','acquire','file:1:1')},
                       'Access':{('m','Worker#state','RW','file:1:1')}})
            record=build_component_record(kb, {'name':'worker','evidence':{'thread_entries':1}})
            self.assertIn('Executes background jobs',record['description'])
            self.assertIn('demo.jobs.Worker',record['detailed_description'])
            self.assertLessEqual(len(record['description']),280)
            self.assertEqual(record['knowledge']['entry_kinds'], {'executor':1})
            self.assertEqual(record['knowledge']['written_resource_count'],1)
            self.assertEqual(len(record['knowledge']['sources']),2)
            self.assertEqual(build_component_record(kb, {'name':'worker','evidence':{'thread_entries':1}}),record)

    def test_parent_includes_child_purpose_without_losing_evidence(self):
        root=component_options([{'name':'extensions/a','description':'Handles event ingestion.',
                                'concurrency_evidence':{'thread_entries':3}}])[0]
        self.assertIn('Handles event ingestion.',root['description'])
        self.assertEqual(root['concurrency_evidence']['thread_entries'],3)

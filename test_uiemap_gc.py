import json,unittest
from unittest.mock import patch
import test_uiemap_serve as fixtures
import uiemap_gc as gc
import dimension_binding as binding


class ConfigGCTests(unittest.TestCase):
    setUp=fixtures.WebConfigTests.setUp
    index=fixtures.WebConfigTests.index

    def test_default_never_requests_deletion_and_only_selected_store(self):
        with patch.object(gc.hires_gc,'collect',return_value={'state':'dry-run','removed':0}) as collect:
            result=gc.collect(self.file,'42',max_files=1,max_bytes=100)
            collect.assert_called_once_with(self.stores['42'],execute=False,max_files=1,max_bytes=100)
            self.assertEqual(result['dimension'],'42')

    def test_foreign_binding_and_unselected_dimension_refuse_before_gc(self):
        for dimension in ('99','42'):
            if dimension=='42':(self.stores['42']/binding.NAME).write_text('{}')
            with patch.object(gc.hires_gc,'collect') as collect,self.assertRaises(ValueError):gc.collect(self.file,dimension,apply=True)
            collect.assert_not_called()

    def test_caves_refused_before_gc(self):
        self.doc['dimensions'][1]['mode']='caves';self.file.write_text(json.dumps(self.doc))
        with patch.object(gc.hires_gc,'collect') as collect,self.assertRaisesRegex(ValueError,'surface'):gc.collect(self.file,'42',apply=True)
        collect.assert_not_called()

    def test_apply_forwards_limits_and_busy_state(self):
        with patch.object(gc.hires_gc,'collect',return_value={'state':'producer-busy','removed':0}) as collect:
            result=gc.collect(self.file,'0',apply=True,max_files=2,max_bytes=200)
            collect.assert_called_once_with(self.stores['0'],execute=True,max_files=2,max_bytes=200)
            self.assertEqual(result['collection']['state'],'producer-busy')

    def test_changed_config_refused_before_deleting(self):
        with patch.object(gc.hires_gc,'collect') as collect,self.assertRaisesRegex(ValueError,'changed'):
            gc.collect(self.file,'0',apply=True,expected_config_sha='0'*64)
        collect.assert_not_called()


if __name__=='__main__':unittest.main()

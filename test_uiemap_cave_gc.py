import unittest
from unittest.mock import patch
import test_uiemap_serve as fixtures
import uiemap_cave_gc as gc
import dimension_binding as binding


class CaveConfigGCTests(unittest.TestCase):
    setUp=fixtures.WebConfigTests.setUp
    index=fixtures.WebConfigTests.index
    cave_fixture=fixtures.WebConfigTests.cave_fixture

    def test_companion_only_and_dry_run_default(self):
        file,store=self.cave_fixture()
        with patch.object(gc.cave_gc,'collect',return_value={'state':'dry-run','removed':0}) as collector:
            result=gc.collect(file,'42')
            collector.assert_called_once_with(store,execute=False,max_files=64,max_bytes=16*1024*1024)
            self.assertEqual(result['scope'],'cave-companion-only')

    def test_surface_or_unknown_dimension_never_collects(self):
        for dimension in ('0','99'):
            with patch.object(gc.cave_gc,'collect') as collector,self.assertRaises(ValueError):
                gc.collect(self.file,dimension,apply=True)
            collector.assert_not_called()

    def test_wrong_binding_refused(self):
        file,store=self.cave_fixture();(store/binding.NAME).write_text('{}')
        with patch.object(gc.cave_gc,'collect') as collector,self.assertRaises(ValueError):gc.collect(file,'42',apply=True)
        collector.assert_not_called()

    def test_explicit_apply_limits_and_producer_busy(self):
        file,store=self.cave_fixture()
        with patch.object(gc.cave_gc,'collect',return_value={'state':'producer-busy','removed':0}) as collector:
            result=gc.collect(file,'42',apply=True,max_files=1,max_bytes=100)
            collector.assert_called_once_with(store,execute=True,max_files=1,max_bytes=100)
            self.assertEqual(result['collection']['state'],'producer-busy')


if __name__=='__main__':unittest.main()

import os,tempfile,unittest
from pathlib import Path
import hires_bundle as hb
from cave_storage_report import inspect_storage,KINDS

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.out=Path(self.temp.name).resolve()
        (self.out/hb.OWNED_MARKER).write_text('test');self.root=self.out/'cave-data';self.root.mkdir()
        for kind in KINDS:(self.root/kind).mkdir()
    def test_hardlinks_are_not_counted_as_independent_allocations_or_reclaimable_bytes(self):
        first=self.root/'objects'/('a'*64+'.uih');first.write_bytes(b'abc')
        os.link(first,self.root/'objects'/('b'*64+'.uih'))
        result=inspect_storage(self.out)
        self.assertEqual(result['uniqueInodes'],1);self.assertEqual(result['logicalBytes'],6)
        self.assertEqual(result['kinds']['objects']['multiplyLinkedFiles'],2)
        self.assertIsNone(result['reclaimableBytes']);self.assertFalse(result['deleted'])
    def test_unknown_names_are_reported_and_scan_budget_is_enforced(self):
        (self.root/'caps/junk').write_text('x');(self.root/'caps/junk2').write_text('x')
        self.assertEqual(inspect_storage(self.out)['kinds']['caps']['unknownFiles'],2)
        with self.assertRaisesRegex(ValueError,'entry limit'):inspect_storage(self.out,max_entries=1)
    def test_directories_inside_object_folder_are_refused(self):
        (self.root/'objects/unexpected').mkdir()
        with self.assertRaisesRegex(ValueError,'nonregular'):inspect_storage(self.out)

import unittest
from pathlib import Path
from unittest.mock import patch,Mock
import test_uiemap_collisions as fixtures
import uiemap_collisions as export
import uiemap_collision_gc as cleanup
import dimension_binding as binding

class ConfiguredGcTests(unittest.TestCase):
    index=fixtures.CollisionConfigTests.index
    def setUp(self):
        fixtures.CollisionConfigTests.setUp(self)
        self.work=export.plan(self.file,'42');self.out=Path(self.work['out']);self.out.mkdir()
        (self.out/'store').mkdir();(self.out/binding.NAME).write_bytes(binding.canonical(self.work['binding']))

    def test_foreign_binding_and_busy_lock_cannot_adopt(self):
        with patch.object(cleanup.export,'_lock',return_value=None),self.assertRaisesRegex(ValueError,'busy'):
            cleanup.collect(self.work)
        self.assertFalse((self.out/cleanup.gc.legacy.OWNED_MARKER).exists())
        wrong=dict(self.work['binding'],dimension='0');(self.out/binding.NAME).write_bytes(binding.canonical(wrong))
        with patch.object(cleanup.export,'_lock') as lock,self.assertRaises(ValueError):cleanup.collect(self.work)
        lock.assert_not_called()

    def test_missing_or_corrupt_publication_never_marks_owned(self):
        with patch.object(cleanup.export,'_lock',return_value=Mock()),patch.object(cleanup.store_api,'_read_pointer',return_value=None):
            with self.assertRaisesRegex(ValueError,'No collision'):cleanup.collect(self.work)
        with patch.object(cleanup.export,'_lock',return_value=Mock()),patch.object(cleanup.store_api,'_read_pointer',return_value='a'*64),\
             patch.object(cleanup.store_api,'_read_index',return_value={'0,0':'b'*64}),\
             patch.object(cleanup.bundle,'_verify_pair',side_effect=ValueError('corrupt payload')):
            with self.assertRaisesRegex(ValueError,'corrupt'):cleanup.collect(self.work)
        self.assertFalse((self.out/cleanup.gc.legacy.OWNED_MARKER).exists())

    def test_gc_flag_survives_systemd_worker_dispatch(self):
        self.work['collectAfterPublication']=True
        self.assertIn('--gc',export.service_command(self.work,'uiemap-render-test.service'))

if __name__=='__main__':unittest.main()

import os
import unittest
from unittest.mock import patch
import hires_backfill as bf
import hires_bundle as hb
from hires_publish_session import PublishSession
import test_hires_backfill as fixtures

@unittest.skipUnless(os.name=='posix','session bounded verification requires POSIX')
class SessionTests(unittest.TestCase):
    setUp=fixtures.BackfillTests.setUp
    alias=fixtures.BackfillTests.alias
    publish_legacy=fixtures.BackfillTests.publish_legacy

    def test_first_publication_requires_complete_plan_then_warms_in_bounded_ticks(self):
        prepared=bf.step(self.out);session=PublishSession()
        result=session.publish_prepared(self.out,prepared)
        self.assertEqual(result['state'],'waiting-for-complete-plan')
        self.assertFalse((self.out/'current.json').exists())
        for _ in range(4):
            result=session.publish_prepared(self.out,prepared,plan_complete=True,max_tile_reads=1)
            self.assertEqual(result['state'],'verifying')
            self.assertFalse((self.out/'current.json').exists())
        result=session.publish_prepared(self.out,prepared,plan_complete=True,max_tile_reads=1)
        self.assertTrue(result['published'])
        self.assertEqual(session.publish_prepared(self.out,prepared,max_tile_reads=0)['state'],'published')

    def test_stale_preparation_cannot_replace_existing_pointer(self):
        prepared=bf.step(self.out);session=PublishSession()
        session.publish_prepared(self.out,prepared,plan_complete=True)
        before=(self.out/'current.json').read_bytes()
        raw=(self.out/'index.json').read_bytes();(self.out/'index.json').write_bytes(raw+b' ')
        with self.assertRaisesRegex(hb.BundleError,'no longer current'):
            session.publish_prepared(self.out,prepared,plan_complete=True)
        self.assertEqual((self.out/'current.json').read_bytes(),before)

    def test_renderer_change_is_gated_even_when_all_references_prepared(self):
        prepared=bf.step(self.out);session=PublishSession()
        session.publish_prepared(self.out,prepared,plan_complete=True)
        before=(self.out/'current.json').read_bytes()
        self.meta['identity']='a'*64;self.publish_legacy()
        prepared=bf.step(self.out,restart=True)
        self.assertEqual(session.publish_prepared(self.out,prepared)['state'],'waiting-for-complete-plan')
        self.assertEqual((self.out/'current.json').read_bytes(),before)
        self.assertTrue(session.publish_prepared(self.out,prepared,plan_complete=True)['published'])

    def test_error_is_not_misreported_as_warmup(self):
        prepared=bf.step(self.out)
        with patch.object(hb,'publish_index',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):PublishSession().publish_prepared(self.out,prepared,plan_complete=True)
        self.assertFalse((self.out/'current.json').exists())

    def test_incomplete_preparation_does_not_publish_subset(self):
        prepared=bf.step(self.out,max_tiles=1)
        self.assertEqual(PublishSession().publish_prepared(self.out,prepared,plan_complete=True)['state'],'preparing')
        self.assertFalse((self.out/'current.json').exists())

    def test_corrupt_previous_pointer_is_not_silently_overwritten(self):
        prepared=bf.step(self.out);(self.out/'current.json').write_bytes(b'{}')
        with self.assertRaisesRegex(hb.BundleError,'invalid previous pointer'):
            PublishSession().publish_prepared(self.out,prepared,plan_complete=True)
        self.assertEqual((self.out/'current.json').read_bytes(),b'{}')

    def test_journal_failure_keeps_pointer_and_pointer_failure_keeps_journal(self):
        prepared=bf.step(self.out);session=PublishSession()
        session.publish_prepared(self.out,prepared,plan_complete=True)
        before=(self.out/'current.json').read_bytes()
        self.meta['identity']='b'*64;self.publish_legacy();prepared=bf.step(self.out,restart=True)
        real_atomic=hb._atomic
        def fail_history(path,raw):
            if path.name=='publication-history.json':raise OSError('journal failure')
            return real_atomic(path,raw)
        with patch.object(hb,'_atomic',side_effect=fail_history):
            with self.assertRaisesRegex(OSError,'journal failure'):session.publish_prepared(self.out,prepared,plan_complete=True)
        self.assertEqual((self.out/'current.json').read_bytes(),before)
        def fail_pointer(path,raw):
            if path.name=='current.json':raise OSError('pointer failure')
            return real_atomic(path,raw)
        with patch.object(hb,'_atomic',side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError,'pointer failure'):session.publish_prepared(self.out,prepared,plan_complete=True)
        self.assertEqual((self.out/'current.json').read_bytes(),before)
        import json
        journal=json.loads((self.out/'publication-history.json').read_bytes())
        self.assertNotEqual(journal['currentIndexSha256'],json.loads(before)['indexSha256'])
        self.assertEqual(journal['previousIndexSha256'],json.loads(before)['indexSha256'])

import hashlib,json,os,unittest
from types import SimpleNamespace
from unittest.mock import patch
import hires_bundle as hb
import hires_publish_tick as ht
from hires_publish_session import PublishSession
import test_hires_backfill as fixtures

@unittest.skipUnless(os.name=='posix','publisher cache uses POSIX')
class TickTests(unittest.TestCase):
    alias=fixtures.BackfillTests.alias
    publish_legacy=fixtures.BackfillTests.publish_legacy
    def setUp(self):
        fixtures.BackfillTests.setUp(self)
        (self.out/'atlas-layout.json').write_text(json.dumps({'format':'UIE_ATLAS_LAYOUT1','registered':{}}))
        self.session=PublishSession()
        p=patch.object(ht.shutil,'disk_usage',return_value=SimpleNamespace(free=ht.MIN_FREE_BYTES+1));p.start();self.addCleanup(p.stop)
    def receipt(self,complete=True):
        doc=dict(format='UIE_HIRES_STATUS1',state='done',planComplete=complete,finishedAt=1700000000,
                 plannedJobs=1,notScheduled=0,failedJobs=0,failedTiles=0,
                 immutablePreparation={'legacyIndexSha256':hashlib.sha256((self.out/'index.json').read_bytes()).hexdigest()})
        (self.out/'status.json').write_text(json.dumps(doc))
    def tick(self,**kw):return ht.tick(self.out,self.session,world='uie',dimension='0',**kw)
    def test_missing_and_stale_producer_receipt_cannot_publish(self):
        self.assertEqual(self.tick()['state'],'waiting-for-producer')
        self.receipt();(self.out/'index.json').write_bytes((self.out/'index.json').read_bytes()+b' ')
        self.assertEqual(self.tick()['state'],'waiting-for-producer')
        self.assertFalse((self.out/'current.json').exists())
    def test_tick_converges_preparation_and_verification_then_publishes_context(self):
        self.receipt();states=[]
        for _ in range(12):
            result=self.tick(max_tiles=1);states.append(result['state'])
            if result['published']:break
        self.assertIn('preparing',states);self.assertIn('verifying',states);self.assertEqual(states[-1],'published')
        index=json.loads((self.out/'indexes'/f'{result["indexSha256"]}.json').read_bytes())
        self.assertEqual(index['context']['dimension'],'0');self.assertEqual(index['format'],hb.INDEX_FORMAT_V3)
        self.assertEqual(index['source']['producerFinishedAt'],1700000000)
        self.assertTrue((self.out/'publication-history.json').exists())
    def test_incomplete_plan_does_not_publish_first_map(self):
        self.receipt(False)
        self.assertEqual(self.tick()['state'],'waiting-for-complete-plan')
        self.assertFalse((self.out/'current.json').exists())
    def partial_cave_receipt(self):
        self.meta['caves']=True;self.publish_legacy();self.receipt(False)
        doc=json.loads((self.out/'status.json').read_bytes());doc.update(plannedJobs=2,notScheduled=1)
        (self.out/'status.json').write_text(json.dumps(doc))
    def test_explicit_partial_cave_bootstrap_preserves_incomplete_source(self):
        self.partial_cave_receipt()
        self.assertEqual(self.tick()['state'],'waiting-for-complete-plan')
        result=self.tick(allow_initial_partial_caves=True);self.assertTrue(result['published'])
        doc=json.loads((self.out/'indexes'/f'{result["indexSha256"]}.json').read_bytes())
        self.assertTrue(doc['context']['caves']);self.assertFalse(doc['source']['planComplete'])
        self.assertEqual(doc['source']['notScheduled'],1)
        before=(self.out/'current.json').read_bytes()
        self.meta['identity']='b'*64;self.publish_legacy();self.receipt(False)
        # An explicit preparation restart does not grant permission to migrate a
        # partially rendered existing store. Test the publication gate itself.
        import hires_backfill
        hires_backfill.step(self.out,restart=True)
        self.assertEqual(self.tick(allow_initial_partial_caves=True)['state'],'waiting-for-complete-plan')
        self.assertEqual((self.out/'current.json').read_bytes(),before)
    def test_cave_bootstrap_cannot_override_surface_gate_or_failed_cave_data(self):
        self.receipt(False)
        self.assertEqual(self.tick(allow_initial_partial_caves=True)['state'],'waiting-for-complete-plan')
        self.partial_cave_receipt()
        doc=json.loads((self.out/'status.json').read_bytes());doc.update(failedTiles=1,failedJobs=1)
        (self.out/'status.json').write_text(json.dumps(doc))
        self.assertEqual(self.tick(allow_initial_partial_caves=True)['state'],'waiting-for-complete-plan')
        self.assertFalse((self.out/'current.json').exists())
    def test_cave_bootstrap_requires_explicit_boolean(self):
        for value in [1,'yes',None]:
            with self.assertRaises(ValueError):self.tick(allow_initial_partial_caves=value)
    def test_repeated_ticks_keep_hash_and_partial_update_keeps_stale_flags(self):
        self.receipt();first=self.tick();self.assertTrue(first['published'])
        self.assertEqual(first['indexSha256'],self.tick()['indexSha256'])
        doc=json.loads((self.out/'index.json').read_bytes());doc['stale']=['0,0']
        (self.out/'index.json').write_text(json.dumps(doc));self.receipt(False)
        partial=self.tick();self.assertTrue(partial['published'])
        self.assertNotEqual(first['indexSha256'],partial['indexSha256'])
        index=json.loads((self.out/'indexes'/f'{partial["indexSha256"]}.json').read_bytes())
        self.assertEqual(index['source']['staleTiles'],['0,0']);self.assertFalse(index['source']['planComplete'])
        self.assertTrue((self.out/'indexes'/f'{first["indexSha256"]}.json').exists())
    def test_bad_source_keeps_existing_pointer(self):
        self.receipt();self.tick();before=(self.out/'current.json').read_bytes()
        status=json.loads((self.out/'status.json').read_bytes());status.pop('finishedAt')
        (self.out/'status.json').write_text(json.dumps(status))
        with self.assertRaises(ValueError):self.tick()
        self.assertEqual(before,(self.out/'current.json').read_bytes())
    def test_low_disk_leaves_everything_untouched(self):
        self.receipt()
        with patch.object(ht.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            self.assertEqual(self.tick()['state'],'low-disk')
        self.assertFalse((self.out/'objects').exists())
    def test_bad_receipt_and_layout_fail_without_pointer(self):
        (self.out/'status.json').write_bytes(b'[]')
        with self.assertRaises(hb.BundleError):self.tick()
        self.receipt();(self.out/'atlas-layout.json').write_bytes(b'{}')
        with self.assertRaises(hb.BundleError):self.tick()
        self.assertFalse((self.out/'current.json').exists())

import os,time,unittest
from unittest.mock import patch
import test_cave_retention as fixtures
import hires_retention as retention
import hires_gc

class GeometryRetentionTests(unittest.TestCase):
    store_pair=fixtures.RetentionTests.store_pair
    link_root=fixtures.RetentionTests.link_root
    write_geometry=fixtures.RetentionTests.write_geometry
    write_job=fixtures.RetentionTests.write_job
    publish=fixtures.RetentionTests.publish
    next_generation=fixtures.RetentionTests.next_generation
    def setUp(self):
        fixtures.RetentionTests.setUp(self)
        for name in ['atlases','layouts']:(self.out/name).mkdir()
        (self.out/'atlases'/(self.geometry['atlasSha256']+'.png')).write_bytes(b'fixture atlas')
        (self.out/'layouts'/(self.geometry['context']['layoutSha256']+'.json')).write_bytes(b'{}')
        self.publish();self.now=time.time()+3*fixtures.GRACE

    def candidates(self):return {v['path'] for v in retention.plan_retention(self.out,now=self.now)['candidates']}

    def test_old_cap_index_keeps_geometry_after_root_history_expires(self):
        first=self.sha;old=self.doc['geometrySha256']
        self.next_generation(1);self.next_generation(2)
        paths=self.candidates()
        self.assertNotIn('indexes/'+first+'.json',paths)
        self.assertNotIn('objects/'+old+'.uih',paths)
        # Even a single old leftover pair protects its geometry after cap index cleanup.
        for p in (self.root/'indexes').glob('*.json'):
            if fixtures.json.loads(p.read_bytes())['geometryIndexSha256']==first:p.unlink()
        self.assertIn('indexes/'+first+'.json',self.candidates())
        self.assertNotIn('objects/'+old+'.uih',self.candidates())
        (self.root/'pairs'/(self.pair_sha+'.json')).unlink()
        self.assertIn('objects/'+old+'.uih',self.candidates())

    def test_corrupt_old_unreferenced_pair_refuses_root_collection(self):
        (self.root/'pairs'/('f'*64+'.json')).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.candidates()

    def test_bootstrap_suppresses_root_orphans(self):
        orphan=self.out/'objects'/('e'*64+'.uih');orphan.write_bytes(b'orphan')
        with patch.object(retention.hh,'GRACE_SECONDS',0):
            plan=retention.plan_retention(self.out,now=time.time())
        self.assertTrue(plan['caveProtection']['suppressed']);self.assertEqual(plan['candidates'],[])

    @unittest.skipUnless(os.name=='posix','POSIX GC')
    def test_executor_deletes_only_unreferenced_root_object(self):
        orphan=self.out/'objects'/('e'*64+'.uih');orphan.write_bytes(b'orphan')
        current=(self.out/'current.json').read_bytes();caps=(self.root/'current.json').read_bytes()
        result=hires_gc.collect(self.out,execute=True,max_files=1,now=self.now)
        self.assertEqual(result['removed'],1);self.assertFalse(orphan.exists())
        self.assertEqual((self.out/'current.json').read_bytes(),current)
        self.assertEqual((self.root/'current.json').read_bytes(),caps)

if __name__=='__main__':unittest.main()

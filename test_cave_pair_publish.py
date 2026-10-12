import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import test_cave_pair as pairs
import test_cave_pair_index as index_fixture
import cave_pair_store as store
from cave_pair_publish import publish_from_jobs
import hires_bundle as hb

def canonical(doc):return json.dumps(doc,sort_keys=True,separators=(',',':')).encode()

class PublishTests(unittest.TestCase):
    def setUp(self):
        pairs.PairTests.setUp(self)
        self.raw_geometry=self.geometry
        index_fixture.IndexTests.setUp(self)
        self.temp=tempfile.TemporaryDirectory(prefix='uie-cap-publish-')
        self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name).resolve()
        (self.out/hb.OWNED_MARKER).write_text('test')
        for folder in ['indexes','objects','jobs']:(self.out/folder).mkdir()
        self.pair_sha,_=store.store(self.out,self.raw_geometry,self.caps,atlas_sha=self.doc['atlasSha256'],renderer_identity=self.doc['rendererIdentity'])
        os.link(self.out/'cave-data/objects'/f"{self.doc['geometrySha256']}.uih",self.out/'objects'/f"{self.doc['geometrySha256']}.uih")
        self.write_geometry()
        self.job={'capPairs':{'-1,2':self.pair_sha}}
        self.write_job()
    def write_job(self):
        (self.out/'jobs/0_0.json').write_text(json.dumps(self.job))
    def write_geometry(self):
        raw=json.dumps(self.geometry,sort_keys=True,separators=(',',':')).encode()
        self.sha=hashlib.sha256(raw).hexdigest()
        (self.out/'indexes'/f'{self.sha}.json').write_bytes(raw)
        (self.out/'current.json').write_text(json.dumps({'format':'UIE_HIRES_POINTER1','indexSha256':self.sha}))
    def publish(self,**kwargs):return publish_from_jobs(self.out,self.sha,**kwargs)
    def cap_pointer(self):return (self.out/'cave-data/current.json').read_bytes()
    def published_index(self):
        sha=self.publish()['indexSha256']
        return json.loads((self.out/'cave-data/indexes'/f'{sha}.json').read_bytes())
    def point_cap(self,raw,sha=None):
        sha=sha or hashlib.sha256(raw).hexdigest()
        (self.out/'cave-data/indexes'/f'{sha}.json').write_bytes(raw)
        (self.out/'cave-data/current.json').write_bytes(canonical({'format':'UIE_CAVE_CAP_POINTER1','indexSha256':sha}))
    def symlink(self,target,link,directory=False):
        try:os.symlink(target,link,target_is_directory=directory)
        except (OSError,NotImplementedError,AttributeError) as error:self.skipTest(f'symlinks unavailable: {error}')

    def test_publish_is_idempotent_and_surface_pointer_untouched(self):
        before=(self.out/'current.json').read_bytes()
        first=self.publish();self.assertTrue(first['published']);self.assertEqual(first['pairs'],1)
        self.assertFalse(first['unchanged'])
        again=self.publish();self.assertTrue(again['unchanged']);self.assertTrue(again['fastPath'])
        self.assertEqual(again['indexSha256'],first['indexSha256']);self.assertEqual(again['pairs'],1)
        self.assertEqual(before,(self.out/'current.json').read_bytes())
    def test_fast_path_reads_no_jobs_or_pairs(self):
        self.publish()
        with patch.object(hb,'_read',wraps=hb._read) as reads,patch.object(Path,'glob') as glob:
            result=self.publish()
        self.assertTrue(result['fastPath']);glob.assert_not_called()
        names=[str(call.args[0]) for call in reads.call_args_list]
        self.assertFalse([name for name in names if 'jobs' in name or 'pairs' in name],names)
    def test_fast_path_rechecks_locked_geometry_pointer_before_return(self):
        self.publish();real=hb._current_index;calls=[]
        def current(out):
            calls.append(out);return real(out) if len(calls)==1 else None
        with patch.object(hb,'_current_index',side_effect=current):
            with self.assertRaisesRegex(ValueError,'changed during cap check'):self.publish()
        self.assertEqual(len(calls),2)
    def test_missing_pairs_prevent_first_publication(self):
        before=(self.out/'current.json').read_bytes()
        self.job['capPairs']={};self.write_job()
        result=self.publish()
        self.assertEqual((result['state'],result['missing'],result['stale'],result['remaining']),('waiting-for-pairs',1,0,1))
        self.assertFalse(result['published']);self.assertFalse((self.out/'cave-data/current.json').exists())
        self.assertEqual(before,(self.out/'current.json').read_bytes())
    def test_fast_path_skips_later_job_changes_for_published_exact_geometry(self):
        first=self.publish();before=self.cap_pointer()
        self.job['capPairs']={};self.write_job()
        result=self.publish()
        self.assertTrue(result['published']);self.assertTrue(result['fastPath']);self.assertEqual(result['indexSha256'],first['indexSha256'])
        self.assertEqual(before,self.cap_pointer())
    def test_new_geometry_with_stale_pair_preserves_previous_cap_pointer(self):
        self.publish();before=self.cap_pointer()
        self.geometry['tiles']['-1,2']='0'*64;self.write_geometry();surface_before=(self.out/'current.json').read_bytes()
        result=self.publish()
        self.assertEqual((result['published'],result['missing'],result['stale'],result['remaining']),(False,0,1,1))
        self.assertEqual(before,self.cap_pointer());self.assertEqual(surface_before,(self.out/'current.json').read_bytes())
    def test_corrupt_pair_and_missing_payload_fail_without_cap_pointer(self):
        manifest=self.out/'cave-data/pairs'/f'{self.pair_sha}.json';saved=manifest.read_bytes();manifest.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'hash'):self.publish()
        manifest.write_bytes(saved)
        (self.out/'cave-data/caps'/f"{self.doc['capSha256']}.uic.gz").unlink()
        with self.assertRaises(FileNotFoundError):self.publish()
        self.assertFalse((self.out/'cave-data/current.json').exists())
    def test_wrong_geometry_payload_size_fails_without_cap_pointer(self):
        path=self.out/'objects'/f"{self.doc['geometrySha256']}.uih"
        path.unlink();path.write_bytes(self.raw_geometry+b'x')
        with self.assertRaisesRegex(ValueError,'size'):self.publish()
        self.assertFalse((self.out/'cave-data/current.json').exists())
    def test_wrong_origin_fails_without_cap_pointer(self):
        self.geometry['tiles']={'0,2':self.doc['geometrySha256']};self.write_geometry()
        self.job={'capPairs':{'0,2':self.pair_sha}};self.write_job()
        with self.assertRaisesRegex(ValueError,'differs from geometry generation'):self.publish()
        self.assertFalse((self.out/'cave-data/current.json').exists())
    def test_wrong_current_generation_and_surface_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'changed'):publish_from_jobs(self.out,'0'*64)
        self.geometry['context']['caves']=False;self.write_geometry()
        with self.assertRaisesRegex(ValueError,'surface'):self.publish()
    def test_stale_pair_is_waiting_not_silently_published(self):
        self.geometry['tiles']['-1,2']='0'*64;self.write_geometry()
        result=self.publish()
        self.assertEqual((result['state'],result['missing'],result['stale'],result['remaining']),('waiting-for-pairs',0,1,1))
        self.assertFalse((self.out/'cave-data/current.json').exists())
    def test_cap_identity_mismatch_is_stale_waiting_and_explicit_identity_respected(self):
        result=self.publish(cap_identity='c'*64)
        self.assertEqual((result['state'],result['missing'],result['stale'],result['remaining']),('waiting-for-pairs',0,1,1))
        self.assertFalse((self.out/'cave-data/current.json').exists())
        self.publish();before=self.cap_pointer()
        result=self.publish(cap_identity='c'*64)                 # published index has another identity: no fast path
        self.assertEqual((result['published'],result['stale']),(False,1));self.assertNotIn('fastPath',result)
        self.assertEqual(before,self.cap_pointer())
        with self.assertRaisesRegex(ValueError,'cap identity'):self.publish(cap_identity='x')
    def test_partial_cap_index_for_same_geometry_cannot_bypass_checks(self):
        doc=self.published_index();doc['pairs']={};raw=canonical(doc);self.point_cap(raw);before=self.cap_pointer()
        # Simulate an older partial publication without the new journal; replacing
        # only the pointer while retaining another journal is now corruption.
        (self.out/'cave-data/cap-history.json').unlink()
        self.job['capPairs']={};self.write_job()
        result=self.publish()
        self.assertEqual((result['published'],result['missing']),(False,1));self.assertEqual(before,self.cap_pointer())
        self.job={'capPairs':{'-1,2':self.pair_sha}};self.write_job()
        result=self.publish()
        self.assertTrue(result['published']);self.assertFalse(result['unchanged']);self.assertNotIn('fastPath',result)
        self.assertNotEqual(before,self.cap_pointer())
    def test_malformed_cap_pointer_or_index_fails_closed(self):
        doc=self.published_index();surface=(self.out/'current.json').read_bytes()
        index_sha=hashlib.sha256(canonical(doc)).hexdigest()
        pointer=self.out/'cave-data/current.json'
        cases=[
          ('Invalid cave cap pointer',lambda:pointer.write_text(json.dumps({'format':'UIE_CAVE_CAP_POINTER1','indexSha256':index_sha}))),
          ('Invalid cave cap pointer',lambda:pointer.write_bytes(canonical({'format':'UIE_CAVE_CAP_POINTER1','indexSha256':index_sha,'x':1}))),
          ('missing index',lambda:pointer.write_bytes(canonical({'format':'UIE_CAVE_CAP_POINTER1','indexSha256':'f'*64}))),
          ('hash mismatch',lambda:self.point_cap(canonical(doc),'e'*64)),
          ('not canonical',lambda:self.point_cap(json.dumps(doc,sort_keys=True,indent=1).encode())),
          ('Invalid cave cap index',lambda:self.point_cap(canonical({**doc,'extra':1}))),
          ('Invalid cave cap index',lambda:self.point_cap(canonical({**doc,'capIdentity':'X'}))),
          ('reference',lambda:self.point_cap(canonical({**doc,'pairs':{'-1,2':'x'}}))),
          ('reference',lambda:self.point_cap(canonical({**doc,'pairs':{'bad':'a'*64}}))),
        ]
        for message,damage in cases:
            with self.subTest(message):
                damage();before=self.cap_pointer()
                with self.assertRaisesRegex(ValueError,message):self.publish()
                self.assertEqual(before,self.cap_pointer());self.assertEqual(surface,(self.out/'current.json').read_bytes())
    def test_symlinked_cap_pointer_rejected(self):
        self.publish();pointer=self.out/'cave-data/current.json';saved=pointer.read_bytes()
        target=Path(self.temp.name)/'elsewhere.json';target.write_bytes(saved);pointer.unlink()
        self.symlink(target,pointer)
        with self.assertRaises(ValueError):self.publish()
        self.assertTrue(pointer.is_symlink());self.assertEqual(saved,target.read_bytes())
    def test_symlinked_cap_indexes_rejected(self):
        self.publish();indexes=self.out/'cave-data/indexes';real=self.out/'cave-data/real-indexes'
        indexes.rename(real);self.symlink(real,indexes,directory=True)
        with self.assertRaises(ValueError):self.publish()
        self.assertTrue(indexes.is_symlink())
    def test_conflicting_job_references_rejected(self):
        (self.out/'jobs/1_0.json').write_text(json.dumps({'capPairs':{'-1,2':'0'*64}}))
        with self.assertRaisesRegex(ValueError,'Conflicting'):self.publish()

import contextlib,hashlib,io,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
import hires_bundle as hb
import model_candidate_plan as mp

def canonical(doc):return json.dumps(doc,sort_keys=True,separators=(',',':')).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
GiB=1024**3

class CandidatePlanTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='uie-model-plan-');self.addCleanup(temp.cleanup)
        self.base=Path(temp.name).resolve();self.store=self.base/'hires-store';self.dest=self.base/'candidates'
        for folder in ('objects','atlases','layouts','indexes'):(self.store/folder).mkdir(parents=True)
        self.dest.mkdir();(self.store/hb.OWNED_MARKER).write_text('owned')
        self.objects={}
        for key,size in (('0,0',100),('1,0',200),('2,0',300)):
            raw=bytes([len(self.objects)+1])*size;self.objects[key]=sha(raw)
            (self.store/'objects'/f'{sha(raw)}.uih').write_bytes(raw)
        self.objects['3,0']=self.objects['2,0']                     # two tiles, one content-addressed object
        self.atlas=hb.PNG_SIGNATURE+b'\0'*42;(self.store/'atlases'/f'{sha(self.atlas)}.png').write_bytes(self.atlas)
        self.layout=b'{"layout":1}'+b' '*18;(self.store/'layouts'/f'{sha(self.layout)}.json').write_bytes(self.layout)
        self.publish(self.objects)
    def index_doc(self,tiles,caves=False):
        return {'format':hb.INDEX_FORMAT_V3,'tileSize':64,'rendererIdentity':'c'*64,'atlasSha256':sha(self.atlas),
                'tiles':tiles,'context':{'world':'uie','dimension':'0','renderer':'uie-offline-mesher/1+hires',
                                         'caves':caves,'layoutSha256':sha(self.layout)},
                'source':{'legacyIndexSha256':'d'*64,'producerFinishedAt':1700000000,'planComplete':True,
                          'plannedJobs':1,'notScheduled':0,'failedJobs':0,'failedTiles':0,'staleTiles':[]}}
    def publish(self,tiles,caves=False,raw=None):
        raw=raw or canonical(self.index_doc(tiles,caves));self.index_raw=raw
        (self.store/'indexes'/f'{sha(raw)}.json').write_bytes(raw)
        (self.store/'current.json').write_bytes(canonical({'format':hb.POINTER_FORMAT,'indexSha256':sha(raw)}))
        return sha(raw)
    def plan(self,free=100*GiB,reserve=10*GiB,**kwargs):
        return mp.plan(self.store,self.dest,reserve_bytes=reserve,disk_usage=lambda path:SimpleNamespace(free=free),**kwargs)
    def snapshot(self):
        return {str(p):(os.lstat(p).st_mode,os.lstat(p).st_size,os.lstat(p).st_mtime_ns) for p in self.base.rglob('*')}

    def test_valid_published_surface_estimate(self):
        before=self.snapshot();result=self.plan()
        self.assertEqual(before,self.snapshot())                          # nothing written or created
        self.assertEqual((result['tiles'],result['distinctObjects'],result['geometryLogicalBytes']),(4,3,600))
        self.assertEqual((result['atlasBytes'],result['layoutBytes'],result['indexBytes']),
                         (len(self.atlas),len(self.layout),len(self.index_raw)))
        self.assertGreaterEqual(result['geometryAccountedBytes'],600)
        if result['geometryAllocatedBytes'] is None:self.assertEqual(result['geometryAccountedBytes'],600)
        self.assertEqual(result['requiredBytes'],2*(result['geometryAccountedBytes']+len(self.index_raw))
                         +len(self.atlas)+len(self.layout)+10*GiB)
        self.assertTrue(result['sufficientSpace']);self.assertEqual(result['marginBytes'],100*GiB-result['requiredBytes'])
        self.assertEqual((result['readOnly'],result['generations'],result['indexSha256']),(True,2,sha(self.index_raw)))
        self.assertIn('not a guarantee',result['caveat']);json.dumps(result)
    def test_free_space_boundary_and_insufficient_disk(self):
        required=self.plan()['requiredBytes']
        self.assertTrue(self.plan(free=required)['sufficientSpace'])
        short=self.plan(free=required-1);self.assertFalse(short['sufficientSpace']);self.assertEqual(short['marginBytes'],-1)
        self.assertFalse(self.plan(free=0,reserve=0)['sufficientSpace'])
        seen=[]
        mp.plan(self.store,self.dest,reserve_bytes=0,disk_usage=lambda path:seen.append(path) or SimpleNamespace(free=1))
        self.assertEqual(seen,[self.dest])                               # free space of the destination parent
    def test_unpublished_and_empty_stores_are_errors_not_zero(self):
        (self.store/'current.json').unlink()
        with self.assertRaisesRegex(mp.PlanError,'no published immutable index'):self.plan()
        self.publish({})
        with self.assertRaisesRegex(mp.PlanError,'no tiles'):self.plan()
    def test_corrupt_or_incomplete_metadata_fails_closed(self):
        valid=self.index_raw;key=self.objects['0,0']
        cases=[
          ('pointer',lambda:(self.store/'current.json').write_bytes(b'{bad')),
          ('hash mismatch',lambda:(self.store/'indexes'/f'{sha(valid)}.json').write_bytes(valid+b' ')),
          ('canonical',lambda:self.publish(None,raw=json.dumps(self.index_doc(self.objects),indent=1).encode())),
          ('surface',lambda:self.publish(self.objects,caves=True)),
          ('geometry object is missing',lambda:(self.store/'objects'/f'{key}.uih').rename(self.base/'moved.uih')),
          ('geometry object outside size limit',lambda:(self.store/'objects'/f'{key}.uih').write_bytes(b'')),
          ('atlas is missing',lambda:(self.store/'atlases'/f'{sha(self.atlas)}.png').rename(self.base/'atlas.png')),
          ('atlas layout is missing',lambda:(self.store/'layouts'/f'{sha(self.layout)}.json').rename(self.base/'l.json')),
          ('not a regular file',lambda:((self.store/'objects'/f'{key}.uih').unlink(),(self.store/'objects'/f'{key}.uih').mkdir())),
          ('ownership marker',lambda:(self.store/hb.OWNED_MARKER).unlink()),
        ]
        for message,damage in cases:
            with self.subTest(message):
                self.setUp();damage()
                with self.assertRaises(ValueError) as raised:self.plan()
                self.assertRegex(str(raised.exception),message.replace('ownership marker','marker|ownership'))
    def test_older_index_formats_are_not_accepted_as_surface_v3(self):
        doc=self.index_doc(self.objects);doc.pop('source');doc['format']=hb.INDEX_FORMAT_V2
        self.publish(None,raw=canonical(doc))
        with self.assertRaisesRegex(mp.PlanError,'expected immutable V3'):self.plan()
    def test_destination_must_be_an_existing_real_directory_outside_the_store(self):
        for destination,message in ((self.base/'missing','does not exist'),(self.store,'outside the current store'),
                                    (self.store/'objects','outside the current store'),(Path('relative'),'absolute')):
            with self.subTest(str(destination)):
                with self.assertRaisesRegex(mp.PlanError,message):
                    mp.plan(self.store,destination,reserve_bytes=0,disk_usage=lambda p:SimpleNamespace(free=1))
        self.assertFalse((self.base/'missing').exists())                  # never created
        (self.base/'file').write_text('x')
        with self.assertRaisesRegex(mp.PlanError,'not a real directory'):
            mp.plan(self.store,self.base/'file',reserve_bytes=0,disk_usage=lambda p:SimpleNamespace(free=1))
    def symlink(self,target,link,directory=False):
        try:os.symlink(target,link,target_is_directory=directory)
        except (OSError,NotImplementedError) as error:self.skipTest(f'symlinks unavailable: {error}')
    def test_symlinked_object_store_and_destination_are_refused(self):
        key=self.objects['1,0'];path=self.store/'objects'/f'{key}.uih';real=self.base/'real.uih'
        path.rename(real);self.symlink(real,path)
        with self.assertRaisesRegex(ValueError,'not a regular file'):self.plan()
        path.unlink();real.rename(path)
        link=self.base/'store-link';self.symlink(self.store,link,directory=True)
        with self.assertRaises(ValueError):mp.plan(link,self.dest,reserve_bytes=0,disk_usage=lambda p:SimpleNamespace(free=1))
        dest_link=self.base/'dest-link';self.symlink(self.dest,dest_link,directory=True)
        with self.assertRaisesRegex(mp.PlanError,'symlink|real directory'):
            mp.plan(self.store,dest_link,reserve_bytes=0,disk_usage=lambda p:SimpleNamespace(free=1))
    def test_publication_change_during_estimate_is_an_error(self):
        other=dict(self.objects);other.pop('3,0')
        def usage(path):
            self.publish(other);return SimpleNamespace(free=100*GiB)
        with self.assertRaisesRegex(mp.PlanError,'changed during the estimate'):
            mp.plan(self.store,self.dest,reserve_bytes=0,disk_usage=usage)
    def test_reserve_validation(self):
        for reserve in (-1,True,1.5,'1'):
            with self.subTest(reserve=reserve):
                with self.assertRaises(ValueError):self.plan(reserve=reserve)
    def test_partial_or_failed_publication_is_not_a_full_world_estimate(self):
        for field,value in [('planComplete',False),('notScheduled',1),('failedJobs',1),('failedTiles',1)]:
            with self.subTest(field=field):
                doc=self.index_doc(self.objects);doc['source'][field]=value
                if field=='notScheduled':doc['source']['planComplete']=False
                self.publish(None,raw=canonical(doc))
                with self.assertRaises(ValueError):self.plan()
    def test_cli_is_read_only_and_reports_json(self):
        out=io.StringIO();before=self.snapshot()
        with contextlib.redirect_stdout(out):
            code=mp.main(['--store',str(self.store),'--destination-parent',str(self.dest),'--reserve-bytes','0'],
                         disk_usage=lambda p:SimpleNamespace(free=1))
        self.assertEqual(code,0);doc=json.loads(out.getvalue());self.assertFalse(doc['sufficientSpace'])
        self.assertEqual(before,self.snapshot())
        err=io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(mp.main(['--store',str(self.base/'none'),'--destination-parent',str(self.dest),
                                      '--reserve-bytes','0']),1)
        self.assertIn('model_candidate_plan',err.getvalue())
        with self.assertRaises(SystemExit) as raised,contextlib.redirect_stderr(io.StringIO()):
            mp.main(['--store',str(self.store),'--destination-parent',str(self.dest),'--reserve-bytes','-5'])
        self.assertEqual(raised.exception.code,2)

if __name__=='__main__':unittest.main()

import gzip,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import cave_release_verify as v
import test_cave_pair as fixture


class VerifyTests(unittest.TestCase):
    def setUp(self):
        fixture.PairTests.setUp(self)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.out=Path(tmp.name).resolve()
        (self.out/v.hb.OWNED_MARKER).touch()
        for d in ('indexes','objects','atlases','layouts','cave-data/indexes','cave-data/pairs','cave-data/caps'):(self.out/d).mkdir(parents=True,exist_ok=True)
        atlas=v.hb.PNG_SIGNATURE+b'\0'*32;layout=v.canonical(dict(format='UIE_ATLAS_LAYOUT1',registered={}))
        self.doc['atlasSha256']=self.obj('atlases','.png',atlas);layout_sha=self.obj('layouts','.json',layout)
        self.obj('objects','.uih',self.geometry)
        self.capfile=self.out/'cave-data/caps'/(self.doc['capSha256']+'.uic.gz');self.capfile.write_bytes(gzip.compress(self.caps))
        self.g=dict(format='UIE_HIRES_IMMUTABLE_INDEX3',tileSize=64,rendererIdentity=self.doc['rendererIdentity'],atlasSha256=self.doc['atlasSha256'],tiles={'-1,2':self.doc['geometrySha256']},
                    context=dict(world='uie',dimension='0',renderer='offline',caves=True,layoutSha256=layout_sha),
                    source=dict(legacyIndexSha256='e'*64,producerFinishedAt=1700000000,planComplete=True,plannedJobs=1,notScheduled=0,failedJobs=0,failedTiles=0,staleTiles=[]))
        self.publish()
    def obj(self,folder,suffix,data):
        sha=hashlib.sha256(data).hexdigest();(self.out/folder/(sha+suffix)).write_bytes(data);return sha
    def publish(self):
        gr=v.canonical(self.g);gs=self.obj('indexes','.json',gr)
        self.obj('cave-data/pairs','.json',v.cp.canonical(self.doc))
        cs,cr=v.ci.build(gr,{'-1,2':self.doc},cap_identity=self.doc['capIdentity']);self.obj('cave-data/indexes','.json',cr)
        for root,fmt,sha in [(self.out,v.hb.POINTER_FORMAT,gs),(self.out/'cave-data','UIE_CAVE_CAP_POINTER1',cs)]:
            (root/'current.json').write_bytes(v.canonical(dict(format=fmt,indexSha256=sha)))
    def verify(self,**kw):return v.verify_release(self.out,expected_identity=self.doc['rendererIdentity'],**kw)
    def test_complete_readonly_success(self):
        before={p:p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        self.assertEqual(self.verify()['verifiedPairs'],1)
        self.assertEqual(before,{p:p.read_bytes() for p in self.out.rglob('*') if p.is_file()})
    def test_wrong_identity_and_incomplete(self):
        with self.assertRaises(ValueError):v.verify_release(self.out,expected_identity='0'*64)
        self.g['source'].update(planComplete=False,plannedJobs=2,notScheduled=1);self.publish()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.verify()
        self.assertFalse(self.verify(require_complete=False)['planComplete'])
    def test_corrupt_and_missing_payload(self):
        self.capfile.write_bytes(gzip.compress(self.caps[:-1]+b'x'))
        with self.assertRaises(ValueError):self.verify()
        self.capfile.unlink()
        with self.assertRaises(OSError):self.verify()
    def test_gzip_bounds_trailing_and_truncated(self):
        for raw in (gzip.compress(self.caps+b'x'),gzip.compress(self.caps)+b'trail',gzip.compress(self.caps)[:-4]):
            with self.subTest(size=len(raw)),self.assertRaises(ValueError):v.decode_caps(raw)
    def test_pair_coverage_and_index_hash(self):
        pointer=json.loads((self.out/'cave-data/current.json').read_bytes());p=self.out/'cave-data/indexes'/(pointer['indexSha256']+'.json')
        c=json.loads(p.read_bytes());c['pairs']={};sha=self.obj('cave-data/indexes','.json',v.canonical(c))
        (self.out/'cave-data/current.json').write_bytes(v.canonical(dict(format='UIE_CAVE_CAP_POINTER1',indexSha256=sha)))
        with self.assertRaisesRegex(ValueError,'coverage'):self.verify()
        self.publish();p.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'hash'):self.verify()
    def test_pointer_change_during_payload_check(self):
        original=v.cp.verify
        def changed(*args):
            result=original(*args);(self.out/'current.json').write_bytes(b'{}');return result
        with patch.object(v.cp,'verify',side_effect=changed),self.assertRaisesRegex(ValueError,'pointers changed'):self.verify()

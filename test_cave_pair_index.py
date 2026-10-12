import hashlib,json,unittest
import test_cave_pair as fixtures
from cave_pair_index import build
class IndexTests(unittest.TestCase):
    def setUp(self):
        fixtures.PairTests.setUp(self)
        self.geometry={'format':'UIE_HIRES_IMMUTABLE_INDEX3','tileSize':64,'rendererIdentity':self.doc['rendererIdentity'],
          'atlasSha256':self.doc['atlasSha256'],'tiles':{'-1,2':self.doc['geometrySha256']},
          'context':{'world':'uie','dimension':'0','renderer':'uie-offline-mesher/1+hires','caves':True,'layoutSha256':'d'*64},
          'source':{'legacyIndexSha256':'e'*64,'producerFinishedAt':1700000000,'planComplete':False,'plannedJobs':983,
            'notScheduled':982,'failedJobs':0,'failedTiles':0,'staleTiles':[]}}
    def call(self,pairs=None):return build(json.dumps(self.geometry).encode(),{'-1,2':self.doc} if pairs is None else pairs,cap_identity=self.doc['capIdentity'])
    def test_partial_index_pins_exact_geometry_bytes_and_pairs(self):
        sha,raw=self.call();doc=json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),sha)
        self.assertEqual(doc['geometryIndexSha256'],hashlib.sha256(json.dumps(self.geometry).encode()).hexdigest())
        self.assertEqual(len(doc['pairs']),1)
        _,empty=self.call({});self.assertEqual(json.loads(empty)['pairs'],{})
    def test_wrong_origin_geometry_atlas_and_cap_rules_rejected(self):
        for changed in [{'origin':[0,128]},{'geometrySha256':'0'*64},{'atlasSha256':'0'*64},{'rendererIdentity':'0'*64},{'capIdentity':'0'*64}]:
            with self.assertRaises(ValueError):self.call({'-1,2':{**self.doc,**changed}})
    def test_surface_and_malformed_geometry_fail_closed(self):
        self.geometry['context']['caves']=False
        with self.assertRaisesRegex(ValueError,'cave geometry'):self.call()
        self.geometry['context']['caves']=True;self.geometry['tiles']['bad']='a'*64
        with self.assertRaisesRegex(ValueError,'geometry tile'):self.call()

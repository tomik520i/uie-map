"""POSIX integration: cave_batch.controller with the REAL publisher path (no mocked publisher results).

Real parts: cave_pair_store (pairs, caps, aliases), world_hires._write_index (legacy index), hires_backfill.step
(preparation, done beforehand like the renderer's --prepare-immutable), hires_publish_tick.tick with a real
PublishSession (VerificationCache, publish_index, publication history) and cave_pair_publish (cap index + history).
Fakes: only the renderer subprocess (it writes a fresh producer receipt) and free disk space (temporary
directories are usually below the 20 GiB floor). tick(max_tiles=1) makes a cold cache need one 'verifying' tick per
tile, so 13 tiles need 12 unfinished warmup ticks: more than the default limit of 8.
"""
import hashlib,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import cave_batch as cb
import cave_pair_store as store
import hires_backfill as backfill
import hires_bundle as hb
import hires_publish_tick as ht
import world_hires as wh
from cave_cap_codec import encode,CELLS
from hires_publish_session import PublishSession
from test_world_hires import quad

IDENTITY=hashlib.sha256(b'cave renderer').hexdigest()
INITIAL=12

@unittest.skipUnless(os.name=='posix','verification cache and bounded warmup are POSIX only')
class CaveBatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='uie-cave-batch-it-');self.addCleanup(temp.cleanup)
        self.out=Path(temp.name).resolve()/'cave'
        for folder in ('tiles','jobs'):(self.out/folder).mkdir(parents=True)
        (self.out/hb.OWNED_MARKER).write_text('private integration test')
        atlas=hb.PNG_SIGNATURE+bytes(range(256))+b'\x00'*144
        (self.out/'atlas.png').write_bytes(atlas)
        (self.out/'atlas-layout.json').write_text(json.dumps({'format':'UIE_ATLAS_LAYOUT1','registered':{}}))
        self.meta={'renderer':'uie-offline-mesher/1+hires','identity':IDENTITY,'caves':True,
                   'atlas':'atlas.png','atlasSha256':hashlib.sha256(atlas).hexdigest()}
        disk=patch.object(ht.shutil,'disk_usage',return_value=SimpleNamespace(free=wh.MIN_FREE_BYTES+1))
        disk.start();self.addCleanup(disk.stop)
        self.pairs={}
        for tx in range(INITIAL):self.add_tile(tx)
        self.produce()
        # Initial cave generation (context world uie / dimension 0 / caves true) with its own warm session.
        session=PublishSession()
        for _ in range(4):
            first=self.tick(session,max_tiles=128)
            if first['published']:break
        self.assertTrue(first['published'],first);self.assertTrue(first['capPublication']['published'],first)
        self.initial={name:(self.out/name).read_bytes() for name in
                      ('current.json','publication-history.json','cave-data/current.json','cave-data/cap-history.json')}

    # ---- the "renderer": real stores and writers, only the subprocess is skipped
    def add_tile(self,tx):
        key=f'{tx},0'
        if key in self.pairs:return
        geometry=wh.encode_tile(tx*wh.TILE,0,[quad(tx*wh.TILE+1,64,1)])
        caps=encode(tx*wh.TILE,0,bytearray(CELLS),present_mask=1)
        sha,doc=store.store(self.out,geometry,caps,atlas_sha=self.meta['atlasSha256'],renderer_identity=IDENTITY)
        store.publish_geometry_alias(self.out,doc['origin'],doc['geometrySha256'])
        self.pairs[key]=sha
    def produce(self):
        """Legacy index + job record + fresh receipt, then preparation exactly as --prepare-immutable does."""
        refs={key:hashlib.sha256((self.out/'tiles'/f"{key.replace(',','_')}.uih").read_bytes()).hexdigest()[:12]
              for key in self.pairs}
        wh._write_index(self.out,self.meta,{(0,0):{'tiles':refs,'chunks':len(refs)}})
        (self.out/'jobs/0_0.json').write_text(json.dumps({'job':[0,0],'tiles':refs,'capPairs':self.pairs,'errors':[]}))
        for _ in range(10):
            if backfill.step(self.out,max_tiles=128,reconcile=True)['complete']:break
        else:self.fail('preparation did not complete')
        status={'format':'UIE_HIRES_STATUS1','state':'done','planComplete':True,'startedAt':1700000000,
                'finishedAt':1700000001,'total':1,'done':1,'plannedJobs':1,'notScheduled':0,'failedJobs':0,
                'failedTiles':0,'idle':None,
                'immutablePreparation':{'state':'prepared','legacyIndexSha256':
                                        hashlib.sha256((self.out/'index.json').read_bytes()).hexdigest()}}
        (self.out/'status.json').write_text(json.dumps(status))
        return status
    def render(self,step):
        self.add_tile(INITIAL)                                  # the batch region adds one new cave tile
        status=self.produce()
        return {'returncode':0,'timedOut':False,'groupGone':True,'seconds':0.0,'fresh':True,
                'status':cb._renderer_summary(status),'log':'cave-batch-logs/step-1.log'}
    def tick(self,session,max_tiles=1):
        return ht.tick(self.out,session,world='uie',dimension='0',max_tiles=max_tiles,
                       allow_initial_partial_caves=True,publish_cave_pairs=True)
    def run_batch(self,session,**limits):
        sleeps=[]
        result=cb.controller(self.out,max_steps=1,render=self.render,publish=lambda s:self.tick(s),session=session,
                             sleep=sleeps.append,**limits)
        self.assertEqual(sleeps,[])                              # never busy/waiting in this fixture
        return result

    def assert_published_generation(self,result):
        self.assertEqual((result['state'],result['completedSteps']),('completed',1),result)
        last=result['lastPublisher']
        self.assertEqual((last['verifyTicks'],last['retryTicks'],last['ticks']),(INITIAL,0,INITIAL+1))
        self.assertTrue(last['published']);self.assertTrue(last['capPublication']['published'])
        geometry_sha=json.loads((self.out/'current.json').read_bytes())['indexSha256']
        self.assertEqual(geometry_sha,last['indexSha256'])
        geometry_raw=(self.out/'indexes'/f'{geometry_sha}.json').read_bytes()
        geometry=json.loads(geometry_raw)
        self.assertEqual(hashlib.sha256(geometry_raw).hexdigest(),geometry_sha)
        self.assertEqual((geometry['context']['world'],geometry['context']['dimension'],geometry['context']['caves']),
                         ('uie','0',True))
        self.assertEqual(set(geometry['tiles']),set(self.pairs));self.assertEqual(len(geometry['tiles']),INITIAL+1)
        cap_sha=json.loads((self.out/'cave-data/current.json').read_bytes())['indexSha256']
        self.assertEqual(cap_sha,last['capPublication']['indexSha256'])
        cap=json.loads((self.out/'cave-data/indexes'/f'{cap_sha}.json').read_bytes())
        self.assertEqual(cap['geometryIndexSha256'],geometry_sha)            # exact geometry generation
        self.assertEqual(cap['pairs'],self.pairs)                           # every pair of the whole fixture
        self.assertEqual((cap['rendererIdentity'],cap['atlasSha256']),(IDENTITY,self.meta['atlasSha256']))
        history=json.loads((self.out/'cave-data/cap-history.json').read_bytes())
        self.assertEqual(history['currentIndexSha256'],cap_sha)
        self.assertEqual(json.loads((self.out/cb.STATUS_NAME).read_bytes()),result)

    def test_default_warmup_limit_stops_safely_then_explicit_limit_completes(self):
        stopped=self.run_batch(PublishSession())                 # cold cache, default limit 8
        self.assertEqual((stopped['state'],stopped['reason']),('error','publisher-verify-limit'))
        last=stopped['lastPublisher']
        self.assertEqual((last['state'],last['verifyTicks'],last['ticks'],last['published']),
                         ('verifying',cb.MAX_VERIFY_TICKS+1,cb.MAX_VERIFY_TICKS+1,False))
        for name,raw in self.initial.items():                       # both pointers and both histories untouched
            self.assertEqual((self.out/name).read_bytes(),raw,name)
        completed=self.run_batch(PublishSession(),max_verify_ticks=16)   # a new process: cold cache again
        self.assert_published_generation(completed)
        self.assertNotEqual((self.out/'current.json').read_bytes(),self.initial['current.json'])
    def test_explicit_warmup_limit_publishes_geometry_and_caps_from_cold_cache(self):
        result=self.run_batch(PublishSession(),max_verify_ticks=16)
        self.assertGreater(result['lastPublisher']['verifyTicks'],cb.MAX_VERIFY_TICKS)
        self.assert_published_generation(result)
        previous=json.loads(self.initial['cave-data/current.json'])['indexSha256']
        history=json.loads((self.out/'cave-data/cap-history.json').read_bytes())
        self.assertEqual(history['previousIndexSha256'],previous)        # previous cap generation retained

"""POSIX integration: cave_full_render.recover_publication with the REAL publisher (no mocked publisher results).

Reuses the fixture of test_cave_batch_integration (12 published cave tiles; a batch step adds tile 13). A batch
interrupted by the default warmup limit leaves 9 verified tiles in its PublishSession; recovery must finish
publication with hires_publisher.run_once (real lock, tick, PublishSession, publish_index, cap publication) and
the SAME session, without rendering. Fakes: free disk space only (as in the fixture) and the batch renderer
callback of the interrupted step. VerificationCache.verify is wrapped (real result returned) to count file reads.
"""
import hashlib,json,os,unittest
from unittest.mock import patch
import cave_batch as cb
import cave_full_render as full
import cave_pair as cp
import hires_bundle as hb
import hires_publish_tick as ht
import world_hires as wh
from hires_publish_session import PublishSession
import test_cave_batch_integration as fixture

TILES=fixture.INITIAL+1                                   # 13 after the interrupted batch step
VERIFIED_BEFORE_STOP=cb.MAX_VERIFY_TICKS+1                # one tile per unfinished tick with tick(max_tiles=1)
POINTERS=('current.json','publication-history.json','cave-data/current.json','cave-data/cap-history.json')

@unittest.skipUnless(os.name=='posix','verification cache, flock and bounded warmup are POSIX only')
class FullRecoveryIntegrationTests(unittest.TestCase):
    setUp=fixture.CaveBatchIntegrationTests.setUp
    add_tile=fixture.CaveBatchIntegrationTests.add_tile
    produce=fixture.CaveBatchIntegrationTests.produce
    tick=fixture.CaveBatchIntegrationTests.tick
    run_batch=fixture.CaveBatchIntegrationTests.run_batch

    def render(self,step):
        self.renders=getattr(self,'renders',0)+1
        return fixture.CaveBatchIntegrationTests.render(self,step)
    def interrupted_batch(self):
        session=PublishSession()
        stopped=self.run_batch(session)                    # renders tile 13, stops after 9 verifying ticks
        self.assertEqual((stopped['state'],stopped['reason']),('error','publisher-verify-limit'))
        self.assertEqual(stopped['lastPublisher']['verifyTicks'],VERIFIED_BEFORE_STOP)
        self.assert_pointers_unchanged()
        return session
    def assert_pointers_unchanged(self,names=POINTERS):
        for name in names:self.assertEqual((self.out/name).read_bytes(),self.initial[name],name)
    def recover(self,session,**kwargs):
        """Real recovery; counts real tile reads; any rendering path fails the test."""
        real=hb.VerificationCache.verify
        self.reads=[];self.beats=[]
        def counted(cache,path,key,sha,allow_read=True,max_read_bytes=None):
            used=real(cache,path,key,sha,allow_read,max_read_bytes)
            if used:self.reads.append(key)
            return used
        renders=getattr(self,'renders',0)
        def no_render(*args,**kw):raise AssertionError('publication recovery must not render')
        with patch.object(hb.VerificationCache,'verify',counted),patch.object(cb,'render_subprocess',side_effect=no_render),\
             patch.object(wh,'build',side_effect=no_render),patch.object(cb,'controller',side_effect=no_render):
            try:
                return full.recover_publication(self.out,session,heartbeat=lambda n,state:self.beats.append((n,state)),
                                                sleep=lambda seconds:self.fail('no busy/waiting expected'),**kwargs)
            finally:self.assertEqual(getattr(self,'renders',0),renders)

    def assert_published(self,outcome):
        geometry_sha=json.loads((self.out/'current.json').read_bytes())['indexSha256']
        self.assertEqual(geometry_sha,outcome['indexSha256'])
        geometry_raw=(self.out/'indexes'/f'{geometry_sha}.json').read_bytes()
        self.assertEqual(hashlib.sha256(geometry_raw).hexdigest(),geometry_sha)
        geometry=json.loads(geometry_raw)
        self.assertEqual((geometry['context']['world'],geometry['context']['dimension'],geometry['context']['caves']),
                         ('uie','0',True))
        self.assertEqual(set(geometry['tiles']),set(self.pairs));self.assertEqual(len(self.pairs),TILES)
        cap_sha=json.loads((self.out/'cave-data/current.json').read_bytes())['indexSha256']
        self.assertEqual(cap_sha,outcome['capIndexSha256'])
        cap_raw=(self.out/'cave-data/indexes'/f'{cap_sha}.json').read_bytes()
        self.assertEqual(hashlib.sha256(cap_raw).hexdigest(),cap_sha)
        cap=json.loads(cap_raw)
        self.assertEqual(cap['geometryIndexSha256'],geometry_sha)           # cap index of exactly this geometry
        self.assertEqual(cap['pairs'],self.pairs)                          # every pair of the fixture
        for key,pair_sha in self.pairs.items():                            # each referenced pair really valid
            raw=(self.out/'cave-data/pairs'/f'{pair_sha}.json').read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),pair_sha,key)
            doc=cp.validate(json.loads(raw));self.assertEqual(cp.canonical(doc),raw)
            self.assertEqual(doc['origin'],[int(key.split(',')[0])*wh.TILE,0])
            self.assertEqual(doc['geometrySha256'],geometry['tiles'][key])
            self.assertEqual((self.out/'objects'/f"{doc['geometrySha256']}.uih").stat().st_size,doc['geometryBytes'])
            self.assertTrue((self.out/'cave-data/caps'/f"{doc['capSha256']}.uic.gz").is_file())
        history=json.loads((self.out/'cave-data/cap-history.json').read_bytes())
        self.assertEqual(history['currentIndexSha256'],cap_sha)
        self.assertEqual(history['previousIndexSha256'],json.loads(self.initial['cave-data/current.json'])['indexSha256'])

    def test_recovery_continues_same_proof_cache_without_rendering(self):
        session=self.interrupted_batch()
        outcome=self.recover(session)                       # default: hires_publisher.run_once (128 tiles/tick)
        self.assertEqual(outcome['ticks'],1);self.assertEqual(self.beats,[(1,'published')])
        self.assertEqual(len(self.reads),TILES-VERIFIED_BEFORE_STOP)       # only tiles not proven before the stop
        self.assertEqual(len(set(self.reads)),len(self.reads))
        self.assert_published(outcome)
    def test_new_session_needs_full_verification(self):
        self.interrupted_batch()
        outcome=self.recover(PublishSession())             # what a supervisor restart would cost
        self.assertEqual(len(self.reads),TILES)
        self.assert_published(outcome)
    def test_bounded_failures_preserve_pointers_and_keep_proofs(self):
        session=self.interrupted_batch()
        # Tick bound with real one-tile ticks: two more tiles proven, nothing published.
        with self.assertRaisesRegex(full.RecoveryError,'tick limit 2'):
            self.recover(session,publish=lambda current:self.tick(current),max_ticks=2)
        self.assertEqual(self.beats,[(1,'verifying'),(2,'verifying')]);self.assertEqual(len(self.reads),2)
        self.assert_pointers_unchanged()
        # A real non-transient publisher state (low disk) stops before any write.
        with patch.object(ht.shutil,'disk_usage',return_value=type('Usage',(),{'free':0})()):
            with self.assertRaisesRegex(full.RecoveryError,'stopped: low-disk'):self.recover(session)
        self.assert_pointers_unchanged()
        # The same session still holds every proof gathered so far: only the last two tiles are read.
        outcome=self.recover(session)
        self.assertEqual(len(self.reads),TILES-VERIFIED_BEFORE_STOP-2)
        self.assert_published(outcome)
    def test_cap_failure_is_not_success_and_keeps_old_cap_publication(self):
        session=self.interrupted_batch()
        job=json.loads((self.out/'jobs/0_0.json').read_bytes())
        job['capPairs'][f'{fixture.INITIAL},0']='f'*64                  # manifest that does not exist
        (self.out/'jobs/0_0.json').write_text(json.dumps(job))
        with self.assertRaisesRegex(full.RecoveryError,'caps not published \\(error\\)'):self.recover(session)
        # Geometry publication is real and already committed by the tick; the cap pointer and history are not.
        self.assertNotEqual((self.out/'current.json').read_bytes(),self.initial['current.json'])
        self.assert_pointers_unchanged(('cave-data/current.json','cave-data/cap-history.json'))
        cap=json.loads((self.out/'cave-data/indexes'/
                        f"{json.loads(self.initial['cave-data/current.json'])['indexSha256']}.json").read_bytes())
        self.assertNotEqual(cap['geometryIndexSha256'],json.loads((self.out/'current.json').read_bytes())['indexSha256'])

    # ---- supervisor startup after a stop between the geometry commit and the cap commit
    def geometry_ahead_of_caps(self):
        """Real state: geometry pointer of the 13-tile generation committed, cap pointer still on the 12-tile one."""
        session=self.interrupted_batch()
        job_path=self.out/'jobs/0_0.json';valid=job_path.read_bytes()
        job=json.loads(valid);job['capPairs'][f'{fixture.INITIAL},0']='f'*64
        job_path.write_text(json.dumps(job))
        with self.assertRaisesRegex(full.RecoveryError,'caps not published'):self.recover(session)
        job_path.write_bytes(valid)                         # the producer's real, valid job record again
        self.assert_pointers_unchanged(('cave-data/current.json','cave-data/cap-history.json'))
        with patch.object(full,'plan_jobs',return_value=[(0,0)]):
            with self.assertRaises(full.PublicationPending):full.coverage(self.out,self.out)
    def run_supervisor(self):
        """Real coverage, real publisher, new supervisor process state; every render path fails the test."""
        def no_render(*args,**kw):raise AssertionError('startup recovery must not render')
        renders=getattr(self,'renders',0)
        with patch.object(full,'plan_jobs',return_value=[(0,0)]),patch.object(cb,'render_subprocess',side_effect=no_render),\
             patch.object(full.batch,'run_batch',side_effect=no_render),patch.object(wh,'build',side_effect=no_render),\
             patch.object(full,'maintain',side_effect=no_render):
            try:return full.run(self.out,self.out,self.out,sleep=lambda seconds:self.fail('no waiting expected'))
            finally:self.assertEqual(getattr(self,'renders',0),renders)
    def test_supervisor_startup_finishes_pending_cap_publication_then_reports_complete(self):
        self.geometry_ahead_of_caps()
        result=self.run_supervisor()
        self.assertEqual(result['state'],'completed')
        self.assertEqual((result['totalRegions'],result['publishedRegions'],result['remainingRegions']),(1,1,0))
        self.assertEqual(result['publicationRecovery'],{'ticks':1,'state':'published'})
        self.assertEqual(result['completedSteps'],0)                      # nothing rendered by the supervisor
        geometry=json.loads((self.out/'current.json').read_bytes())['indexSha256']
        cap=json.loads((self.out/'cave-data/current.json').read_bytes())['indexSha256']
        self.assertEqual((result['geometryIndexSha256'],result['capIndexSha256']),(geometry,cap))
        self.assert_published({'indexSha256':geometry,'capIndexSha256':cap})
        status=json.loads((self.out/full.STATUS).read_bytes());self.assertEqual(status['state'],'completed')
    def test_supervisor_startup_with_corrupt_cap_index_stays_a_hard_failure(self):
        self.geometry_ahead_of_caps()
        cap=json.loads((self.out/'cave-data/current.json').read_bytes())['indexSha256']
        path=self.out/'cave-data/indexes'/f'{cap}.json';path.write_bytes(path.read_bytes()+b' ')
        before={name:(self.out/name).read_bytes() for name in POINTERS}
        with patch.object(full,'_publish_once',side_effect=AssertionError('corruption must not be republished')):
            with self.assertRaisesRegex(ValueError,'cap index hash'):self.run_supervisor()
        for name,raw in before.items():self.assertEqual((self.out/name).read_bytes(),raw,name)
        self.assertEqual(json.loads((self.out/full.STATUS).read_bytes())['state'],'error')

if __name__=='__main__':unittest.main()

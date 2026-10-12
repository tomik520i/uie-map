import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import cave_full_render as full
import test_cave_pair_publish as fixture

VERIFYING={'state':'verifying','published':False}
PUBLISHED={'state':'published','published':True,'indexSha256':'a'*64,
           'capPublication':{'state':'published','published':True,'indexSha256':'b'*64}}

@unittest.skipUnless(os.name=='posix','POSIX full renderer')
class CoverageTests(unittest.TestCase):
    setUp=fixture.PublishTests.setUp
    write_geometry=fixture.PublishTests.write_geometry
    write_job=fixture.PublishTests.write_job
    publish=fixture.PublishTests.publish
    def test_only_published_complete_job_counts(self):
        self.publish();self.job.update(job=[0,0],errors=[],tiles={'-1,2':'revision'});self.write_job()
        with patch.object(full,'plan_jobs',return_value=[(0,0),(1,0)]):
            self.assertEqual(full.coverage(self.out,self.out)['publishedRegions'],1)
            self.job['errors']=['failed'];self.write_job()
            self.assertEqual(full.coverage(self.out,self.out)['publishedRegions'],0)
            self.job['errors']=[];self.job['capPairs']['-1,2']='0'*64;self.write_job()
            self.assertEqual(full.coverage(self.out,self.out)['publishedRegions'],0)
    # ---- geometry committed, caps not: typed recoverable state vs. hard corruption
    def cap_index_path(self):
        sha=json.loads((self.out/'cave-data/current.json').read_bytes())['indexSha256']
        return self.out/'cave-data/indexes'/f'{sha}.json'
    def test_valid_pointers_of_different_generations_are_publication_pending(self):
        self.publish();self.job.update(job=[0,0],errors=[],tiles={'-1,2':'revision'});self.write_job()
        self.geometry['source']['producerFinishedAt']+=1;self.write_geometry()   # newer geometry, caps not yet
        with patch.object(full,'plan_jobs',return_value=[(0,0)]):
            with self.assertRaisesRegex(full.PublicationPending,'belongs to geometry'):full.coverage(self.out,self.out)
    def test_corrupt_or_missing_publication_data_is_not_publication_pending(self):
        self.publish();self.job.update(job=[0,0],errors=[],tiles={'-1,2':'revision'});self.write_job()
        index=self.cap_index_path();saved=index.read_bytes();pointer=self.out/'cave-data/current.json'
        def write_index(doc):
            raw=json.dumps(doc,sort_keys=True,separators=(',',':')).encode();sha=__import__('hashlib').sha256(raw).hexdigest()
            (self.out/'cave-data/indexes'/f'{sha}.json').write_bytes(raw)
            pointer.write_bytes(json.dumps({'format':'UIE_CAVE_CAP_POINTER1','indexSha256':sha},sort_keys=True,separators=(',',':')).encode())
        doc=json.loads(saved);original=pointer.read_bytes()
        cases=[('cap index hash',lambda:index.write_bytes(saved+b' ')),
               ('invalid cap index',lambda:write_index({**doc,'geometryIndexSha256':'x'})),
               ('invalid cap index',lambda:write_index({k:v for k,v in doc.items() if k!='pairs'})),
               ('invalid cap index',lambda:write_index({**doc,'format':'OTHER'})),
               ('no published geometry index',lambda:(self.out/'current.json').unlink())]
        with patch.object(full,'plan_jobs',return_value=[(0,0)]):
            for message,damage in cases:
                with self.subTest(message):
                    damage()
                    with self.assertRaises(ValueError) as raised:full.coverage(self.out,self.out)
                    self.assertNotIsInstance(raised.exception,full.PublicationPending)
                    self.assertIn(message,str(raised.exception))
                    index.write_bytes(saved);pointer.write_bytes(original)
            pointer.unlink()
            with self.assertRaises(FileNotFoundError):full.coverage(self.out,self.out)
    def test_startup_publication_pending_recovers_once_without_rendering(self):
        sessions=[]
        def publish(out,session):sessions.append(session);return PUBLISHED
        done={'totalRegions':2,'remainingRegions':0,'publishedRegions':2}
        with patch.object(full,'coverage',side_effect=[full.PublicationPending('stopped between commits'),done]),\
             patch.object(full,'_publish_once',side_effect=publish),\
             patch.object(full.batch,'run_batch',side_effect=AssertionError('must not render')):
            result=full.run(self.out,self.out,self.out,sleep=lambda n:None)
        self.assertEqual(result['state'],'completed');self.assertEqual(len(sessions),1)
        self.assertEqual(result['publicationRecovery'],{'ticks':1,'state':'published'})
        self.assertIn('stopped between commits',result['publicationPending'])
    def test_repeated_publication_pending_after_recovery_fails(self):
        calls=[]
        with patch.object(full,'coverage',side_effect=[full.PublicationPending('first'),full.PublicationPending('again')]),\
             patch.object(full,'_publish_once',side_effect=lambda out,session:calls.append(1) or PUBLISHED),\
             patch.object(full.batch,'run_batch',side_effect=AssertionError('must not render')):
            with self.assertRaisesRegex(RuntimeError,'still differ after recovery'):full.run(self.out,self.out,self.out)
        self.assertEqual(len(calls),1)
        self.assertEqual(json.loads((self.out/full.STATUS).read_bytes())['state'],'error')
    def test_other_coverage_errors_are_not_recovered(self):
        with patch.object(full,'coverage',side_effect=ValueError('cap index hash')),\
             patch.object(full,'_publish_once',side_effect=AssertionError('must not publish')),\
             patch.object(full.batch,'run_batch',side_effect=AssertionError('must not render')):
            with self.assertRaisesRegex(ValueError,'cap index hash'):full.run(self.out,self.out,self.out)
    def test_failed_recovery_of_pending_publication_stops(self):
        cap_failed={'state':'published','published':True,'capPublication':{'state':'waiting-for-pairs','published':False}}
        with patch.object(full,'coverage',side_effect=[full.PublicationPending('x')]),\
             patch.object(full,'_publish_once',return_value=cap_failed),\
             patch.object(full.batch,'run_batch',side_effect=AssertionError('must not render')):
            with self.assertRaisesRegex(full.RecoveryError,'caps not published'):full.run(self.out,self.out,self.out)
    def test_runs_until_full_coverage_not_batch_limit(self):
        progress=[{'totalRegions':12,'remainingRegions':12-i} for i in range(13)]
        with patch.object(full,'coverage',side_effect=progress),patch.object(full,'maintain',return_value=[]),patch.object(full.batch,'run_batch',return_value={'state':'completed'}) as run:
            result=full.run(self.out,self.out,self.out)
        self.assertEqual(run.call_count,12);self.assertEqual(result['state'],'completed')
    def test_failed_region_can_yield_to_other_regions(self):
        progress=[{'totalRegions':1,'remainingRegions':1}]*2+[{'totalRegions':1,'remainingRegions':0}]
        with patch.object(full,'coverage',side_effect=progress),patch.object(full,'maintain',return_value=[]),patch.object(full.batch,'run_batch',side_effect=[
            {'state':'error','reason':'render-failed'},{'state':'completed'}]):
            result=full.run(self.out,self.out,self.out,sleep=lambda n:None)
        self.assertEqual(result['state'],'completed')
    def test_publication_error_stops_without_false_completion(self):
        with patch.object(full,'coverage',return_value={'totalRegions':12,'remainingRegions':12}),patch.object(full.batch,'run_batch',return_value={'state':'error','reason':'cap-error'}):
            with self.assertRaisesRegex(RuntimeError,'cap-error'):full.run(self.out,self.out,self.out)
    def test_live_save_race_retries_only_fresh_reaped_known_failure(self):
        race={'state':'error','reason':'renderer-exit-1','lastRenderer':{'fresh':True,'groupGone':True,'returncode':1,
              'status':{'state':'stopped: build failed: OverviewError: r.0.0.mca kept changing while it was copied'}}}
        self.assertTrue(full.changing_snapshot(race))
        for change in [{'fresh':False},{'groupGone':False},{'returncode':2},
                       {'status':{'state':'stopped: build failed: disk error'}}]:
            self.assertFalse(full.changing_snapshot({**race,'lastRenderer':{**race['lastRenderer'],**change}}))
        progress=[{'totalRegions':1,'remainingRegions':1}]*2+[{'totalRegions':1,'remainingRegions':0}]
        with patch.object(full,'coverage',side_effect=progress),patch.object(full,'maintain',return_value=[]),patch.object(full.batch,'run_batch',side_effect=[race,{'state':'completed'}]) as render:
            self.assertEqual(full.run(self.out,self.out,self.out,sleep=lambda n:None)['state'],'completed')
            self.assertEqual(render.call_count,2)
        with patch.object(full,'coverage',return_value=progress[0]),patch.object(full.batch,'run_batch',return_value=race) as render:
            with self.assertRaisesRegex(RuntimeError,'renderer-exit-1'):full.run(self.out,self.out,self.out,sleep=lambda n:None)
            self.assertEqual(render.call_count,10)
    def test_unreaped_renderer_group_stops(self):
        with patch.object(full,'coverage',return_value={'totalRegions':1,'remainingRegions':1}),patch.object(full.batch,'run_batch',return_value={'state':'error','reason':'renderer-timeout-group-alive'}) as render:
            with self.assertRaisesRegex(RuntimeError,'group-alive'):full.run(self.out,self.out,self.out,sleep=lambda n:None)
            self.assertEqual(render.call_count,1)
    # ---- publication-only recovery inside run(): same session, no re-render
    def run_with(self,progress,batches,ticks,maintenance=()):
        sessions=[];published=[]
        def run_batch(out,**kwargs):sessions.append(kwargs['session']);return batches.pop(0)
        def publish(out,session):published.append(session);return ticks.pop(0)
        patches=[patch.object(full,'coverage',side_effect=progress),patch.object(full.batch,'run_batch',side_effect=run_batch),
                 patch.object(full,'_publish_once',side_effect=publish),
                 patch.object(full,'maintain',side_effect=list(maintenance) or (lambda out:[]))]
        for p in patches:p.start()
        try:return full.run(self.out,self.out,self.out,sleep=lambda n:None),sessions,published
        finally:
            for p in reversed(patches):p.stop()
    def status(self):return json.loads((self.out/full.STATUS).read_bytes())
    def test_publisher_deadline_finishes_publication_with_same_session_without_rerender(self):
        for reason in full.RECOVERABLE_PUBLISH_REASONS:
            with self.subTest(reason):
                result,sessions,published=self.run_with(
                    [{'totalRegions':2,'remainingRegions':1,'publishedRegions':1},{'totalRegions':2,'remainingRegions':0,'publishedRegions':2}],
                    [{'state':'error','reason':reason}],[VERIFYING,VERIFYING,PUBLISHED])
                self.assertEqual(result['state'],'completed');self.assertEqual(len(sessions),1)   # no second render
                self.assertEqual(len(published),3);self.assertTrue(all(s is sessions[0] for s in published))
                self.assertEqual(result['completedSteps'],1);self.assertEqual(result['publicationRecovery'],{'ticks':3,'state':'published'})
    def test_recovery_with_failed_caps_never_completes(self):
        cap_failed={'state':'published','published':True,'capPublication':{'state':'waiting-for-pairs','published':False}}
        with self.assertRaisesRegex(full.RecoveryError,'caps not published'):
            self.run_with([{'totalRegions':2,'remainingRegions':1,'publishedRegions':1}]*3,
                          [{'state':'error','reason':'publisher-deadline'}],[VERIFYING,cap_failed])
        self.assertEqual(self.status()['state'],'error');self.assertIn('caps not published',self.status()['lastReason'])
    def test_idle_nothing_pending_publishes_once_then_requires_progress(self):
        result,sessions,published=self.run_with(
            [{'totalRegions':2,'remainingRegions':1,'publishedRegions':1},{'totalRegions':2,'remainingRegions':0,'publishedRegions':2},
             {'totalRegions':2,'remainingRegions':0,'publishedRegions':2}],
            [{'state':'idle','reason':'nothing-pending'}],[PUBLISHED])
        self.assertEqual(result['state'],'completed');self.assertEqual((len(sessions),len(published)),(1,1))
        with self.assertRaisesRegex(RuntimeError,'no progress'):
            self.run_with([{'totalRegions':2,'remainingRegions':1,'publishedRegions':1}]*2,
                          [{'state':'idle','reason':'nothing-pending'}],[PUBLISHED])
        self.assertEqual(self.status()['state'],'error')
    def test_three_consecutive_maintenance_errors_stop_and_success_resets(self):
        error=[{'kind':'caps','state':'error','error':'cave retention metadata byte capacity exceeded'}]
        ok=[{'kind':'caps','state':'executed','removed':1,'metadataBytes':10}]
        busy=[{'kind':'caps','state':'producer-busy','removed':0}]
        progress=[{'totalRegions':9,'remainingRegions':9,'publishedRegions':0}]*9
        with self.assertRaisesRegex(RuntimeError,'maintenance failed 3 consecutive times: caps'):
            self.run_with(progress,[{'state':'completed'}]*9,[],maintenance=[error,error,ok,error,busy,error,error])
        status=self.status()
        self.assertEqual(status['maintenanceFailures'],{'geometry':0,'caps':3});self.assertEqual(status['completedSteps'],7)
        self.assertEqual(status['state'],'error')
    def test_maintenance_tracking_is_per_kind_and_warns_on_large_metadata(self):
        big=full.METADATA_WARNING_BYTES+1
        summary,failures,warning=full.track_maintenance(
            [{'kind':'geometry','state':'executed','removed':2,'metadataBytes':5},{'kind':'geometry','state':'executed','removed':0},
             {'kind':'caps','state':'error','error':'x'*400}],{'geometry':2,'caps':1})
        self.assertEqual(failures,{'geometry':0,'caps':2});self.assertEqual(summary['geometry']['removed'],2)
        self.assertEqual(len(summary['caps']['error']),300);self.assertIsNone(warning)
        _,same,_=full.track_maintenance([{'kind':'caps','state':'producer-busy','removed':0}],failures)
        self.assertEqual(same,failures)                                    # busy: neither success nor error
        _,_,warning=full.track_maintenance([{'kind':'caps','state':'executed','removed':0,'metadataBytes':big}],failures)
        self.assertIn('caps retention metadata',warning)
    def test_maintenance_bounds_and_collector_error_do_not_loop(self):
        with patch.object(full.hires_gc,'collect',return_value={'removed':128}) as root,patch.object(full.cave_gc,'collect',side_effect=ValueError('capacity')) as caps:
            result=full.maintain(self.out)
        self.assertEqual(root.call_count,2);self.assertEqual(caps.call_count,1)
        self.assertEqual(result[-1]['state'],'error')

@unittest.skipUnless(os.name=='posix','POSIX full renderer')
class RecoveryTests(unittest.TestCase):
    """recover_publication with mocked publisher ticks and an injected monotonic clock."""
    def recover(self,ticks,**kwargs):
        self.calls=[];self.beats=[];self.sleeps=[];self.now={'t':0.0}
        session=object()
        def publish(current):
            self.assertIs(current,session);self.calls.append(1)
            item=ticks(len(self.calls)) if callable(ticks) else ticks[len(self.calls)-1]
            if isinstance(item,Exception):raise item
            return item
        return full.recover_publication(Path('/unused'),session,publish=publish,
            heartbeat=lambda n,state:self.beats.append((n,state)),sleep=self.sleeps.append,
            monotonic=lambda:self.now['t'],**kwargs)
    def test_success_after_warmup_with_heartbeat_each_tick(self):
        outcome=self.recover([VERIFYING,{'state':'producer-busy','published':False},PUBLISHED])
        self.assertEqual(outcome['ticks'],3);self.assertEqual(self.beats,[(1,'verifying'),(2,'producer-busy'),(3,'published')])
        self.assertEqual(self.sleeps,[full.RECOVERY_WAIT_SECONDS])
    def test_tick_and_wait_bounds(self):
        with self.assertRaisesRegex(full.RecoveryError,'tick limit 5'):self.recover(lambda n:VERIFYING,max_ticks=5)
        self.assertEqual(len(self.calls),5)
        with self.assertRaisesRegex(full.RecoveryError,'still producer-busy after 4'):
            self.recover(lambda n:{'state':'producer-busy','published':False},max_wait_ticks=4)
        self.assertEqual(len(self.calls),4)
    def test_monotonic_time_bound_and_run_deadline(self):
        def slow(n):
            self.now['t']+=100.0;return VERIFYING
        with self.assertRaisesRegex(full.RecoveryError,'time limit'):self.recover(slow,max_seconds=1000)
        self.assertEqual(len(self.calls),10)                              # ticks at 0..900 s, none at 1000 s
        with self.assertRaisesRegex(full.RecoveryError,'time limit'):self.recover(slow,max_seconds=1000,run_deadline=250)
        self.assertEqual(len(self.calls),3)
    def test_non_transient_states_caps_and_exceptions_fail(self):
        for item,pattern in (({'state':'low-disk','published':False},'stopped: low-disk'),
                             ({'state':'waiting-for-complete-plan','published':False},'stopped: waiting-for-complete-plan'),
                             ({'state':'published','published':True},'caps not published \\(missing\\)'),
                             ({'state':'published','published':True,'capPublication':{'state':'error','published':False}},'caps not published \\(error\\)'),
                             (ValueError('build lock replaced'),'build lock replaced')):
            with self.subTest(pattern):
                with self.assertRaisesRegex(full.RecoveryError,pattern):self.recover([item])
                self.assertEqual(len(self.calls),1)

if __name__=='__main__':unittest.main()

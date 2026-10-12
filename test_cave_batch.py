import json,os,sys,tempfile,textwrap,time,unittest
from pathlib import Path
from unittest.mock import patch
import cave_batch as cb
import hires_bundle as hb
import test_cave_pair_publish as store_fixture

T=1_760_000_000.0
LINUX=sys.platform.startswith('linux')

def renderer_status(**changes):
    raw={'state':'done','total':1,'done':1,'failedJobs':0,'failedTiles':0,'notScheduled':5,'skippedUnchanged':2,
         'planComplete':False,'idle':None,'startedAt':T,'errors':[[0,0,'secret path /x']],
         'immutablePreparation':{'state':'prepared'},'schedule':{'selected':{'coverage':1,'update':0}}}
    raw.update(changes);return cb._renderer_summary(raw)
def render_result(**changes):
    result={'returncode':0,'timedOut':False,'groupGone':True,'seconds':1.0,'fresh':True,'status':renderer_status(),
            'log':'cave-batch-logs/step-1.log'}
    result.update(changes);return result
PUBLISHED={'state':'published','published':True,'indexSha256':'a'*64,
           'capPublication':{'state':'published','published':True,'indexSha256':'b'*64,'pairs':64}}

class ControllerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='uie-cave-batch-');self.addCleanup(temp.cleanup)
        self.out=Path(temp.name).resolve();self.events=[];self.sleeps=[];self.session=object()
        self.clock=iter(range(10**6))
    def status(self):return json.loads((self.out/cb.STATUS_NAME).read_bytes())
    def run_controller(self,renders=None,publishes=None,max_steps=3,**kwargs):
        renders=renders or (lambda step:render_result())
        publishes=publishes or (lambda session,tick:PUBLISHED)
        ticks={'n':0}
        def render(step):
            current=self.status()                                  # running status is durable before rendering
            self.assertEqual((current['state'],current['phase'],current['step']),('running','render',step))
            self.events.append(('render',step));return renders(step)
        def publish(session):
            self.assertIs(session,self.session);ticks['n']+=1
            self.assertEqual(self.status()['phase'],'publish')
            self.events.append(('publish',ticks['n']));return publishes(session,ticks['n'])
        return cb.controller(self.out,max_steps=max_steps,render=render,publish=publish,session=self.session,
                             clock=lambda:T+next(self.clock),sleep=self.sleeps.append,**kwargs)

    def test_bounded_steps_render_then_publish_in_order(self):
        result=self.run_controller(max_steps=3)
        self.assertEqual([e[0] for e in self.events],['render','publish']*3)
        self.assertEqual([e[1] for e in self.events if e[0]=='render'],[1,2,3])
        self.assertEqual((result['state'],result['completedSteps'],result['maxSteps'],result['reason']),('completed',3,3,None))
        self.assertEqual(self.status(),result)
        self.assertEqual(result['lastPublisher']['capPublication']['published'],True)
        self.assertNotIn('errors',json.dumps(result['lastRenderer']))   # renderer error text never copied
        self.assertGreater(result['elapsedSeconds'],0)
    def test_idle_stops_without_publish_or_further_render(self):
        for reason in ('backoff','disk-reserve','nothing-pending'):
            with self.subTest(reason):
                self.events.clear()
                renders=lambda step:render_result(status=renderer_status(idle=reason,total=0,done=0)) if step==2 else render_result()
                result=self.run_controller(renders=renders,max_steps=5)
                self.assertEqual(self.events,[('render',1),('publish',1),('render',2)])
                self.assertEqual((result['state'],result['reason'],result['completedSteps']),('idle',reason,1))
    def test_render_errors_stop_before_publication(self):
        cases=[('render-failed',render_result(status=renderer_status(failedTiles=2))),
               ('render-failed',render_result(status=renderer_status(failedJobs=1))),
               ('renderer-exit-1',render_result(returncode=1)),
               ('renderer-timeout',render_result(timedOut=True,returncode=-9)),
               ('renderer-timeout-group-alive',render_result(timedOut=True,groupGone=False,returncode=-9)),
               ('renderer-no-fresh-status',render_result(fresh=False,status=None)),
               ('renderer-stopped',render_result(status=renderer_status(state='stopped: free disk below 20 GiB'))),
               ('render-incomplete',render_result(status=renderer_status(total=0,done=0))),
               ('preparation-skipped',render_result(status=renderer_status(immutablePreparation={'state':'skipped'}))),
               ('preparation-None',render_result(status=renderer_status(immutablePreparation=None)))]
        for reason,result in cases:
            with self.subTest(reason):
                self.events.clear()
                final=self.run_controller(renders=lambda step,r=result:r)
                self.assertEqual(self.events,[('render',1)])
                self.assertEqual((final['state'],final['reason'],final['completedSteps']),('error',reason,0))
    def test_incomplete_cap_publication_stops_immediately(self):
        waiting={'state':'published','published':True,'indexSha256':'a'*64,
                 'capPublication':{'state':'waiting-for-pairs','published':False,'missing':3,'stale':1,'remaining':4}}
        for record,reason in ((waiting,'cap-waiting-for-pairs'),
                              ({'state':'published','published':True,'capPublication':{'state':'error','published':False,'error':'x'}},'cap-error'),
                              ({'state':'published','published':True},'cap-missing')):
            with self.subTest(reason):
                self.events.clear()
                final=self.run_controller(publishes=lambda s,t,r=record:r)
                self.assertEqual(self.events,[('render',1),('publish',1)])   # no retry, no next render
                self.assertEqual((final['state'],final['reason'],final['completedSteps']),('error',reason,0))
        self.assertEqual(self.run_controller(publishes=lambda s,t:waiting)['lastPublisher']['capPublication']['remaining'],4)
    def test_publisher_retries_are_bounded(self):
        busy=self.run_controller(publishes=lambda s,t:{'state':'producer-busy','published':False},max_steps=2)
        self.assertEqual([e for e in self.events if e[0]=='publish'],[('publish',n) for n in range(1,9)])
        self.assertEqual(self.sleeps,[cb.RETRY_SLEEP_SECONDS]*7)
        self.assertEqual((busy['state'],busy['reason'],busy['completedSteps'],busy['lastPublisher']['ticks']),
                         ('error','publisher-incomplete',0,8))
        self.events.clear();self.sleeps.clear()
        sequence=lambda s,t:PUBLISHED if t==3 else {'state':'preparing','published':False,'captured':128,'remaining':9}
        done=self.run_controller(publishes=sequence,max_steps=1)
        self.assertEqual((done['state'],done['lastPublisher']['ticks']),('completed',3))
        self.assertEqual(self.sleeps,[])                                # preparation continues at once
        self.events.clear()
        limited=self.run_controller(publishes=lambda s,t:{'state':'preparing','published':False},max_steps=1,max_publish_ticks=2)
        self.assertEqual((limited['reason'],len([e for e in self.events if e[0]=='publish'])),('publisher-incomplete',2))
    def publish_calls(self):return len([e for e in self.events if e[0]=='publish'])
    def test_explicit_verify_limit_allows_longer_warmup_and_default_stops(self):
        warmup=lambda s,t:PUBLISHED if t==17 else {'state':'verifying','published':False}
        done=self.run_controller(publishes=warmup,max_steps=1,max_verify_ticks=16)
        self.assertEqual(done['state'],'completed')
        last=done['lastPublisher'];self.assertEqual((last['ticks'],last['verifyTicks'],last['retryTicks']),(17,16,0))
        self.assertEqual(self.sleeps,[]);self.assertEqual(self.status()['lastPublisher']['verifyTicks'],16)
        self.events.clear()
        stopped=self.run_controller(publishes=warmup,max_steps=1)          # default 8: never raised automatically
        self.assertEqual((stopped['state'],stopped['reason']),('error','publisher-verify-limit'))
        self.assertEqual(self.publish_calls(),cb.MAX_VERIFY_TICKS+1)
        self.assertEqual(stopped['lastPublisher']['verifyTicks'],cb.MAX_VERIFY_TICKS+1)
    def test_exact_verify_boundary_allows_the_following_successful_tick(self):
        for unfinished,state in ((cb.MAX_VERIFY_TICKS,'completed'),(cb.MAX_VERIFY_TICKS+1,'error')):
            with self.subTest(unfinished=unfinished):
                self.events.clear()
                final=self.run_controller(publishes=lambda s,t,n=unfinished:PUBLISHED if t==n+1 else
                                          {'state':'verifying','published':False},max_steps=1)
                self.assertEqual(final['state'],state)
                self.assertEqual(self.publish_calls(),min(unfinished+1,cb.MAX_VERIFY_TICKS+1))
        self.assertEqual(final['reason'],'publisher-verify-limit')
    def test_mixed_verifying_and_busy_cannot_bypass_either_limit(self):
        alternating=lambda s,t:{'state':'verifying' if t%2 else 'producer-busy','published':False}
        final=self.run_controller(publishes=alternating,max_steps=1,max_verify_ticks=512)
        self.assertEqual((final['reason'],self.publish_calls()),('publisher-incomplete',16))  # 8 busy: retry bound
        self.assertEqual((final['lastPublisher']['verifyTicks'],final['lastPublisher']['retryTicks']),(8,8))
        self.assertEqual(self.sleeps,[cb.RETRY_SLEEP_SECONDS]*7)
        self.events.clear()
        # A busy tick between warmup ticks does not reset the verify counter.
        sequence=lambda s,t:{'state':'producer-busy' if t==9 else 'verifying','published':False}
        final=self.run_controller(publishes=sequence,max_steps=1)
        self.assertEqual((final['reason'],self.publish_calls()),('publisher-verify-limit',10))
        self.assertEqual((final['lastPublisher']['verifyTicks'],final['lastPublisher']['retryTicks']),(9,1))
    def test_counters_reset_for_each_step_but_session_is_shared(self):
        warmup=lambda s,t:PUBLISHED if t in (9,18) else {'state':'verifying','published':False}
        final=self.run_controller(publishes=warmup,max_steps=2)        # 8 verifying + success in each step
        self.assertEqual((final['state'],final['completedSteps'],final['lastPublisher']['verifyTicks']),('completed',2,8))
    def test_publish_deadline_stops_before_a_new_tick(self):
        now={'t':0.0}
        def slow(session,tick):
            now['t']+=4.0;return {'state':'verifying','published':False}  # each tick takes 4 monotonic seconds
        final=self.run_controller(publishes=slow,max_steps=1,max_verify_ticks=512,publish_deadline=10,
                                  monotonic=lambda:now['t'])
        self.assertEqual((final['state'],final['reason']),('error','publisher-deadline'))
        self.assertEqual(self.publish_calls(),3)                         # ticks started at 0, 4, 8; none at 12
        self.assertEqual(final['lastPublisher']['ticks'],3)
        self.events.clear();now['t']=0.0
        exact=self.run_controller(publishes=lambda s,t:(now.__setitem__('t',now['t']+5.0) or
                                  (PUBLISHED if t==2 else {'state':'verifying','published':False})),
                                  max_steps=1,publish_deadline=10,monotonic=lambda:now['t'])
        self.assertEqual(exact['state'],'completed')                    # tick at 5 < 10 may still run
    def test_publish_limit_arguments(self):
        for kwargs in ({'max_verify_ticks':0},{'max_verify_ticks':513},{'max_verify_ticks':True},
                       {'publish_deadline':0},{'publish_deadline':901},{'publish_deadline':float('nan')},
                       {'publish_deadline':True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    cb.controller(self.out,max_steps=1,render=None,publish=None,session=None,**kwargs)
        self.assertFalse((self.out/cb.STATUS_NAME).exists())
    def test_publisher_errors_stop_without_retry(self):
        def raising(session,tick):raise hb.BundleError('build lock replaced')
        cases=[(lambda s,t:{'state':'error','published':False,'error':'boom'},'publisher-error'),
               (lambda s,t:{'state':'low-disk','published':False},'publisher-low-disk'),
               (lambda s,t:{'state':'waiting-for-complete-plan','published':False},'publisher-waiting-for-complete-plan'),
               (raising,'publisher-error')]
        for publishes,reason in cases:
            with self.subTest(reason):
                self.events.clear()
                final=self.run_controller(publishes=publishes)
                self.assertEqual(self.events,[('render',1),('publish',1)])
                self.assertEqual((final['state'],final['reason']),('error',reason))
        self.assertIn('build lock replaced',final['lastPublisher']['error'])
    def test_exception_still_writes_error_status(self):
        def crash(step):raise RuntimeError('renderer launch failed')
        with self.assertRaisesRegex(RuntimeError,'launch failed'):self.run_controller(renders=crash)
        status=self.status()
        self.assertEqual((status['state'],status['phase'],status['completedSteps']),('error',None,0))
        self.assertIn('RuntimeError: renderer launch failed',status['reason'])
    def test_argument_bounds(self):
        for kwargs in ({'max_steps':0},{'max_steps':11},{'max_steps':True},{'max_publish_ticks':0},{'max_publish_ticks':9}):
            with self.subTest(kwargs=kwargs):
                args={'max_steps':1,**kwargs}
                with self.assertRaises(ValueError):
                    cb.controller(self.out,render=None,publish=None,session=None,**args)
        self.assertFalse((self.out/cb.STATUS_NAME).exists())

class HelperTests(unittest.TestCase):
    def test_renderer_command_is_one_job_fair_cave_with_default_budget(self):
        command=cb.renderer_command(Path('/s'),Path('/w'),Path('/c'))
        joined=' '.join(command)
        for flag in ('--workers 1','--max-jobs 1','--fair-schedule','--caves','--cave-caps','--prepare-immutable',
                     f'--lock-wait-seconds {cb.RENDER_LOCK_WAIT_SECONDS}'):
            self.assertIn(flag,joined)
        self.assertNotIn('--cave-store-budget-mib',joined);self.assertNotIn('--cave-reserve-gib',joined)
        self.assertNotIn('--center',joined)
        self.assertEqual(cb.renderer_command(Path('/s'),Path('/w'),Path('/c'),(-512,1024))[-2:],['--center','-512,1024'])
    def test_renderer_summary_whitelist(self):
        summary=renderer_status(total=True,state=7,idle=['x'])
        self.assertEqual((summary['total'],summary['state'],summary['idle']),(None,None,None))
        self.assertNotIn('errors',summary);self.assertIsNone(cb._renderer_summary([]))
    def test_cli_argument_bounds(self):
        base=['--world','w','--catalog-dir','c','--out','o','--world-id','uie','--dimension-id','0']
        for extra in (['--max-steps','0'],['--max-steps','11'],['--step-timeout','901'],['--step-timeout','0'],
                      ['--center','1'],['--center','40000000,0'],['--max-verify-ticks','0'],
                      ['--max-verify-ticks','513'],['--publish-deadline','0'],['--publish-deadline','901']):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as raised,patch('sys.stderr'):cb.main(base+extra)
                self.assertEqual(raised.exception.code,2)

class StoreTests(unittest.TestCase):
    setUp=store_fixture.PublishTests.setUp
    write_geometry=store_fixture.PublishTests.write_geometry
    write_job=store_fixture.PublishTests.write_job
    def test_check_store_accepts_only_existing_owned_cave_store(self):
        self.assertEqual(cb.check_store(self.out),self.out)
        self.geometry['context']['caves']=False;self.write_geometry()
        with self.assertRaisesRegex(cb.BatchError,'not cave geometry'):cb.check_store(self.out)
        (self.out/'current.json').unlink()
        with self.assertRaisesRegex(cb.BatchError,'new store'):cb.check_store(self.out)
        (self.out/hb.OWNED_MARKER).unlink()
        with self.assertRaises((OSError,ValueError)):cb.check_store(self.out)
    def test_check_store_requires_cave_data(self):
        os.rename(self.out/'cave-data',self.out/'moved')
        with self.assertRaises(FileNotFoundError):cb.check_store(self.out)

@unittest.skipUnless(LINUX,'Linux process groups and flock')
class PosixTests(unittest.TestCase):
    setUp=store_fixture.PublishTests.setUp
    write_geometry=store_fixture.PublishTests.write_geometry
    write_job=store_fixture.PublishTests.write_job
    def script(self,body):return [sys.executable,'-c',textwrap.dedent(body)]
    def test_batch_lock_is_exclusive_regular_and_nofollow(self):
        fd=cb.acquire_batch_lock(self.out)
        try:self.assertIsNone(cb.acquire_batch_lock(self.out))
        finally:os.close(fd)
        os.close(cb.acquire_batch_lock(self.out))
        (self.out/cb.LOCK_NAME).unlink();target=self.out/'target';target.write_text('keep')
        (self.out/cb.LOCK_NAME).symlink_to(target)
        with self.assertRaises(OSError):cb.acquire_batch_lock(self.out)
        self.assertEqual(target.read_text(),'keep')
    def test_run_batch_holds_lock_and_reports_busy(self):
        events=[]
        def render(step):events.append('render');return render_result(status=renderer_status(startedAt=time.time()))
        result=cb.run_batch(self.out,world=self.out,catalog_dir=self.out,world_id='uie',dimension_id='0',max_steps=2,
                            render=render,publish=lambda s:PUBLISHED,session=object())
        self.assertEqual((result['state'],result['completedSteps'],events),('completed',2,['render','render']))
        fd=cb.acquire_batch_lock(self.out)
        try:
            busy=cb.run_batch(self.out,world=self.out,catalog_dir=self.out,world_id='uie',dimension_id='0',
                              render=lambda s:self.fail('must not render'),publish=None,session=object())
        finally:os.close(fd)
        self.assertEqual(busy['state'],'busy');self.assertEqual(json.loads((self.out/cb.STATUS_NAME).read_bytes()),result)
    def test_subprocess_fresh_status_and_bounded_log(self):
        command=self.script(f'''
            import json,sys,time
            sys.stdout.write('x'*({cb.MAX_LOG_BYTES}*2));sys.stdout.flush()
            open({str(self.out/'status.json')!r},'w').write(json.dumps({{'state':'done','total':1,'done':1,
                'failedJobs':0,'failedTiles':0,'idle':None,'startedAt':time.time(),'immutablePreparation':{{'state':'prepared'}}}}))
        ''')
        result=cb.render_subprocess(self.out,1,command,timeout=60)
        self.assertEqual((result['returncode'],result['timedOut'],result['fresh']),(0,False,True))
        self.assertEqual(cb.render_outcome(result),('ok',None))
        log=(self.out/cb.LOG_DIR/'step-1.log').read_bytes()
        self.assertLessEqual(len(log),cb.MAX_LOG_BYTES+100);self.assertIn(b'log truncated',log)
    def test_subprocess_stale_status_is_not_trusted(self):
        (self.out/'status.json').write_text(json.dumps({'state':'done','total':1,'done':1,'startedAt':1.0}))
        result=cb.render_subprocess(self.out,1,self.script('print("skipped: another build holds the lock")'),timeout=60)
        self.assertEqual((result['fresh'],result['status']),(False,None))
        self.assertEqual(cb.render_outcome(result),('error','renderer-no-fresh-status'))
    def test_timeout_kills_whole_process_group(self):
        marker=self.out/'grandchild.pid'
        command=self.script(f'''
            import subprocess,sys,time
            child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])
            open({str(marker)!r},'w').write(str(child.pid))
            time.sleep(120)
        ''')
        started=time.monotonic()
        result=cb.render_subprocess(self.out,1,command,timeout=5)
        self.assertLess(time.monotonic()-started,60)
        self.assertTrue(result['timedOut']);self.assertLess(result['returncode'],0)
        self.assertEqual(cb.render_outcome(result)[1][:16],'renderer-timeout')
        pid=int(marker.read_text())
        try:state=Path(f'/proc/{pid}/stat').read_text().split(')')[-1].split()[0]
        except FileNotFoundError:state='gone'
        self.assertIn(state,('gone','Z'))                              # killed (a zombie awaits its new parent)

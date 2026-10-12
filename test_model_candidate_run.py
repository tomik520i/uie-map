import contextlib,io,json,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hires_bundle as hb
import model_candidate_run as mr

LINUX=sys.platform.startswith('linux')
WRITE_STATUS='''import json,sys,time
d=json.loads(sys.argv[2]);d['startedAt']=time.time();d['finishedAt']=time.time()
open(sys.argv[1],'w').write(json.dumps(d))
sys.exit(int(sys.argv[3]))
'''
SPAWN='''import subprocess,sys,time
c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
open(sys.argv[1],'w').write(str(c.pid))
'''
# Interrupted the first time (writes partial output, then sleeps); completes once the flag file exists.
RESUMABLE='''import json,os,sys,time
out,flag,status=sys.argv[1],sys.argv[2],sys.argv[3]
if os.path.exists(flag):
    d=json.loads(status);d['startedAt']=time.time();open(os.path.join(out,'status.json'),'w').write(json.dumps(d));sys.exit(0)
os.makedirs(os.path.join(out,'jobs'),exist_ok=True)
open(os.path.join(out,'jobs','0_0.json'),'w').write('{"job":[0,0]}')
open(os.path.join(out,'index.json'),'w').write('{"caves":false,"tiles":{}}')
time.sleep(120)
'''
COMPLETE={'format':'UIE_HIRES_STATUS1','state':'done','planComplete':True,'notScheduled':0,'failedJobs':0,
          'failedTiles':0,'total':983,'done':983,'plannedJobs':983,'errors':[[0,0,'secret path']]}

def state_of(pid):
    try:return Path(f'/proc/{pid}/stat').read_text().split(')')[-1].split()[0]
    except FileNotFoundError:return 'gone'

@unittest.skipUnless(LINUX,'Linux process groups')
class CandidateRunTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='uie-candidate-run-');self.addCleanup(temp.cleanup)
        self.base=Path(temp.name).resolve();self.out=self.base/'candidate'
        for folder in (self.out,self.base/'world',self.base/'catalog',self.base/'world2'):folder.mkdir()
        (self.out/hb.OWNED_MARKER).write_text('owned')
        self.pid_file=self.base/'grandchild.pid';self.flag=self.base/'second-attempt'
    def writer(self,exit_code=0,**changes):
        return [sys.executable,'-c',WRITE_STATUS,str(self.out/'status.json'),json.dumps({**COMPLETE,**changes}),str(exit_code)]
    def resumable(self):
        return [sys.executable,'-c',RESUMABLE,str(self.out),str(self.flag),json.dumps(COMPLETE)]
    def run_candidate(self,command,world=None,catalog=None,**kwargs):
        args=dict(command=command,poll_seconds=0.2,kill_grace=2,reserve_bytes=0,
                  disk_usage=lambda path:SimpleNamespace(free=10**15));args.update(kwargs)
        return mr.run(self.out,world or self.base/'world',catalog or self.base/'catalog',**args)
    def status(self):return json.loads((self.out/mr.STATUS_NAME).read_bytes())
    def assert_group_gone(self,result):
        self.assertTrue(result['groupGone'])
        with self.assertRaises(ProcessLookupError):os.killpg(result['rendererPid'],0)
        if self.pid_file.exists():self.assertIn(state_of(int(self.pid_file.read_text())),('gone','Z'))

    # ---- outcomes
    def test_complete_renderer_status_is_completed(self):
        result=self.run_candidate(self.writer())
        self.assertEqual((result['state'],result['reason'],result['returncode']),('completed',None,0))
        self.assertEqual(result['renderer']['planComplete'],True);self.assertNotIn('errors',result['renderer'])
        self.assertEqual(self.status(),result);self.assertIsNotNone(result['finishedAt'])
        self.assertFalse(result['leftoverGroup']);self.assert_group_gone(result)
        self.assertTrue((self.out/mr.LOG_NAME).is_file());self.assertEqual(result['attempt'],1)
        self.assertEqual(result['bindings']['codeSha256'],mr.code_identity())
    def test_exit_zero_but_incomplete_or_failed_is_not_success(self):
        for changes,reason in (({'planComplete':False,'notScheduled':12},'plan-incomplete'),({'notScheduled':1},'plan-incomplete'),
                               ({'failedTiles':2,'planComplete':False},'render-failed'),({'failedJobs':1},'render-failed')):
            with self.subTest(reason=reason,changes=changes):
                self.setUp();result=self.run_candidate(self.writer(**changes))
                self.assertEqual((result['state'],result['reason'],result['returncode']),('incomplete',reason,0))
    def test_failures_from_exit_code_or_missing_fresh_status(self):
        stale=json.dumps({**COMPLETE,'startedAt':1.0})                     # a complete status of an OLDER run
        cases=[(lambda:self.writer(exit_code=3),'renderer-exit-3'),
               (lambda:self.writer(state='stopped: free disk below 20 GiB'),'renderer-stopped'),
               (lambda:[sys.executable,'-c',f'open({str(self.out/"status.json")!r},"w").write({stale!r})'],
                'renderer-no-fresh-status'),
               (lambda:[sys.executable,'-c','print("skipped: another build holds the lock")'],'renderer-no-fresh-status')]
        for command,reason in cases:
            with self.subTest(reason):
                self.setUp();result=self.run_candidate(command())
                self.assertEqual((result['state'],result['reason']),('failed',reason))
    def test_surviving_group_after_cleanup_is_never_completed(self):
        with patch.object(mr,'_group_alive',return_value=True),patch.object(mr.cave_batch,'_kill_group',return_value=False) as kill:
            result=self.run_candidate(self.writer())          # real process exits normally: nothing really leaks
        self.assertEqual((result['state'],result['reason']),('failed','renderer-group-alive'))
        self.assertEqual((result['leftoverGroup'],result['groupGone']),(True,False));self.assertEqual(kill.call_count,1)
        self.assertEqual(result['renderer']['state'],'done')    # a "complete" status does not override survivors
    def test_stray_group_members_after_success_are_stopped_and_recorded(self):
        script=SPAWN+WRITE_STATUS.replace('sys.argv[1]','sys.argv[4]').replace('sys.argv[2]','sys.argv[5]').replace('sys.argv[3]','sys.argv[6]')
        command=[sys.executable,'-c',script,str(self.pid_file),'unused','unused',str(self.out/'status.json'),json.dumps(COMPLETE),'0']
        result=self.run_candidate(command)
        self.assertEqual(result['state'],'completed');self.assertTrue(result['leftoverGroup']);self.assert_group_gone(result)

    # ---- bounds
    def test_deadline_bounds_the_wait_itself(self):
        late=f'import time;time.sleep(3)\n'+WRITE_STATUS                 # would succeed after the deadline
        command=[sys.executable,'-c',late,str(self.out/'status.json'),json.dumps(COMPLETE),'0']
        started=time.monotonic()
        result=self.run_candidate(command,max_seconds=0.5,poll_seconds=30)
        self.assertLess(time.monotonic()-started,2.5)                     # not one 30 s poll, not the 3 s sleep
        self.assertEqual((result['state'],result['reason']),('stopped','deadline'))
        self.assertFalse((self.out/'status.json').exists());self.assert_group_gone(result)
    def test_deadline_stops_whole_group_without_leaking(self):
        started=time.monotonic()
        result=self.run_candidate([sys.executable,'-c',SPAWN+'time.sleep(120)\n',str(self.pid_file)],max_seconds=1)
        self.assertLess(time.monotonic()-started,40)
        self.assertEqual((result['state'],result['reason']),('stopped','deadline'));self.assertLess(result['returncode'],0)
        self.assert_group_gone(result);self.assertEqual(self.status()['state'],'stopped')
    def test_low_disk_during_render_stops_group(self):
        samples=iter([10**15]+[5]*100)
        result=self.run_candidate([sys.executable,'-c',SPAWN+'time.sleep(120)\n',str(self.pid_file)],reserve_bytes=10,
                                  disk_usage=lambda path:SimpleNamespace(free=next(samples)))
        self.assertEqual((result['state'],result['reason'],result['freeBytes']),('stopped','low-disk',5))
        self.assert_group_gone(result)
    def test_low_disk_at_start_refuses_without_starting(self):
        marker=self.base/'started'
        result=self.run_candidate([sys.executable,'-c',f'open({str(marker)!r},"w")'],reserve_bytes=100,
                                  disk_usage=lambda path:SimpleNamespace(free=99))
        self.assertEqual((result['state'],result['reason'],result['rendererPid']),('refused','low-disk-at-start',None))
        self.assertFalse(marker.exists());self.assertFalse((self.out/mr.LOG_NAME).exists())
        again=self.run_candidate(self.writer())                          # own refused status is not "content"
        self.assertEqual(again['state'],'completed')
    def test_interrupt_stops_group_and_records_status(self):
        calls=[]
        def usage(path):
            calls.append(1)
            if len(calls)>1:raise KeyboardInterrupt
            return SimpleNamespace(free=10**15)
        with self.assertRaises(KeyboardInterrupt):
            self.run_candidate([sys.executable,'-c',SPAWN+'time.sleep(120)\n',str(self.pid_file)],disk_usage=usage)
        status=self.status();self.assertEqual((status['state'],status['reason']),('stopped','interrupted'))
        self.assert_group_gone(status)

    # ---- runner lock
    def test_second_coordinator_is_refused_without_touching_anything(self):
        fd=mr.acquire_lock(self.out)
        try:
            (self.out/mr.STATUS_NAME).write_text('{"sentinel":1}');before=sorted(os.listdir(self.out))
            with self.assertRaisesRegex(mr.CandidateError,'holds the lock'):self.run_candidate(self.writer())
            with self.assertRaisesRegex(mr.CandidateError,'holds the lock'):self.run_candidate(self.writer(),resume=True)
            self.assertEqual((self.out/mr.STATUS_NAME).read_text(),'{"sentinel":1}');self.assertEqual(sorted(os.listdir(self.out)),before)
        finally:os.close(fd)
    def test_concurrent_runs_cannot_both_pass_the_empty_check(self):
        command=[sys.executable,'-c','import time;time.sleep(1.5)\n'+WRITE_STATUS,str(self.out/'status.json'),json.dumps(COMPLETE),'0']
        results={}
        first=threading.Thread(target=lambda:results.setdefault('first',self.run_candidate(command)))
        first.start()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and not ((self.out/mr.STATUS_NAME).exists() and self.status().get('state')=='running'):
            time.sleep(0.05)
        pid=self.status()['rendererPid']
        with self.assertRaisesRegex(mr.CandidateError,'holds the lock'):self.run_candidate(self.writer())
        first.join(20)
        self.assertEqual(results['first']['state'],'completed');self.assertEqual(results['first']['rendererPid'],pid)
    def test_symlinked_or_foreign_lock_is_refused(self):
        target=self.base/'target';target.write_text('keep');(self.out/mr.LOCK_NAME).symlink_to(target)
        with self.assertRaises(OSError):self.run_candidate(self.writer())
        self.assertEqual(target.read_text(),'keep');self.assertFalse((self.out/mr.STATUS_NAME).exists())
        (self.out/mr.LOCK_NAME).unlink();(self.out/mr.LOCK_NAME).mkdir()
        with self.assertRaises(OSError):self.run_candidate(self.writer())

    # ---- fresh start and resume authorization
    def test_out_must_be_an_owned_empty_store(self):
        (self.out/'index.json').write_text('{}')
        with self.assertRaisesRegex(mr.CandidateError,'not empty'):self.run_candidate(self.writer())
        self.assertFalse((self.out/mr.STATUS_NAME).exists())
        (self.out/'index.json').unlink();(self.out/hb.OWNED_MARKER).unlink()
        with self.assertRaisesRegex(mr.CandidateError,'marker'):self.run_candidate(self.writer())
        (self.out/hb.OWNED_MARKER).write_text('owned')
        link=self.base/'link';os.symlink(self.out,link,target_is_directory=True)
        with self.assertRaises(ValueError):
            mr.run(link,self.base/'world',self.base/'catalog',command=self.writer(),disk_usage=lambda p:SimpleNamespace(free=1))
        self.assertFalse((self.out/mr.STATUS_NAME).exists())
    def interrupted(self):
        first=self.run_candidate(self.resumable(),max_seconds=1)
        self.assertEqual((first['state'],first['reason']),('stopped','deadline'))
        self.assertTrue((self.out/'jobs/0_0.json').exists())
        return first
    def test_interrupted_candidate_resumes_with_identical_bindings(self):
        first=self.interrupted()
        with self.assertRaisesRegex(mr.CandidateError,'use --resume'):self.run_candidate(self.resumable())
        self.flag.write_text('1')
        result=self.run_candidate(self.resumable(),resume=True)
        self.assertEqual((result['state'],result['attempt'],result['resumedFromStartedAt']),('completed',2,first['startedAt']))
        self.assertEqual(result['bindings'],first['bindings'])
        self.assertTrue((self.out/'jobs/0_0.json').exists())              # nothing deleted on resume
    def test_resume_refuses_other_bindings_or_unsafe_stores(self):
        self.interrupted();self.flag.write_text('1');saved=(self.out/mr.STATUS_NAME).read_bytes()
        def refused(pattern,**kwargs):
            with self.assertRaisesRegex(mr.CandidateError,pattern):self.run_candidate(kwargs.pop('command',self.resumable()),resume=True,**kwargs)
            self.assertEqual((self.out/mr.STATUS_NAME).read_bytes(),saved)
        refused('different',world=self.base/'world2')
        refused('different',catalog=self.base/'world2')
        refused('different',command=self.resumable()+['--extra'])
        with patch.object(mr,'code_identity',return_value='0'*64):refused('different')
        with patch.object(mr,'_group_alive',return_value=True):refused('still alive')
        for name,make,pattern in (('current.json',lambda p:p.write_text('{}'),'published'),
                                  ('cave-data',lambda p:p.mkdir(),'cave-data'),
                                  ('index.json',lambda p:p.write_text('{"caves":true}'),'not a surface index')):
            with self.subTest(name):
                path=self.out/name;backup=path.read_bytes() if path.is_file() else None
                if path.is_file():path.unlink()
                make(path)
                try:refused(pattern)
                finally:
                    if path.is_dir():path.rmdir()
                    else:path.unlink()
                    if backup is not None:path.write_bytes(backup)
        (self.out/hb.OWNED_MARKER).unlink()
        with self.assertRaisesRegex(mr.CandidateError,'marker'):self.run_candidate(self.resumable(),resume=True)
    def test_resume_needs_an_own_previous_run(self):
        with self.assertRaisesRegex(mr.CandidateError,'nothing to resume'):self.run_candidate(self.writer(),resume=True)
        (self.out/'jobs').mkdir();(self.out/'index.json').write_text('{"caves":false}')   # unrelated store content
        with self.assertRaisesRegex(mr.CandidateError,'nothing to resume'):self.run_candidate(self.writer(),resume=True)
        (self.out/mr.STATUS_NAME).write_text(json.dumps({'format':'OTHER'}))
        with self.assertRaisesRegex(mr.CandidateError,'unknown format'):self.run_candidate(self.writer(),resume=True)

    # ---- static
    def test_production_command_is_full_surface_one_worker_sibling_renderer(self):
        command=mr.renderer_command(self.out,self.base/'world',self.base/'catalog')
        self.assertEqual(Path(command[1]),Path(mr.__file__).with_name('world_hires.py'))
        self.assertEqual(command[command.index('--workers')+1],'1')
        for flag in ('--caves','--cave-caps','--max-jobs','--fair-schedule'):self.assertNotIn(flag,command)
        self.assertIn('--prepare-immutable',command)  # producer receipt required by hires_publish_tick
    def test_argument_validation(self):
        for kwargs in ({'reserve_bytes':-1},{'reserve_bytes':True},{'max_seconds':0},{'max_seconds':mr.MAX_SECONDS+1},
                       {'poll_seconds':0},{'resume':1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):self.run_candidate(self.writer(),**kwargs)
        for extra in (['--reserve-bytes','-1'],['--max-seconds','0'],['--max-seconds',str(mr.MAX_SECONDS+1)]):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as raised,contextlib.redirect_stderr(io.StringIO()):
                    mr.main(['--out',str(self.out),'--world','w','--catalog-dir','c']+extra)
                self.assertEqual(raised.exception.code,2)

if __name__=='__main__':unittest.main()

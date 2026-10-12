import os,time,unittest
from unittest.mock import patch
import cave_gc as gc
import test_cave_retention as fixture

@unittest.skipUnless(os.name=='posix','requires real POSIX flock')
class GcTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.RetentionTests('test_invalid_now_rejected');self.f.setUp()
        self.addCleanup(self.f.doCleanups);self.out=self.f.out
        self.f.publish();self.now=time.time()+3*fixture.GRACE
    def collect(self,**kwargs):return gc.collect(self.out,now=self.now,**kwargs)
    def test_dry_run_then_only_alias_removed_and_reader_payload_survives(self):
        before=self.f.snapshot();result=self.collect()
        self.assertEqual(result['removed'],0)
        # Lock creation is the only permissible dry-run filesystem addition.
        after=self.f.snapshot();after.pop(str(self.out/'.build.lock'),None)
        self.assertEqual(before,after)
        payload=self.out/'objects'/f"{self.f.doc['geometrySha256']}.uih"
        original=payload.read_bytes();pointer=(self.out/'cave-data/current.json').read_bytes()
        done=self.collect(execute=True)
        self.assertEqual(done['removed'],1);self.assertIsNone(done['reclaimableBytes'])
        self.assertEqual(payload.read_bytes(),original)
        self.assertEqual((self.out/'cave-data/current.json').read_bytes(),pointer)
        self.assertEqual(self.collect(execute=True)['removed'],0)
    def test_busy_lock_does_not_plan(self):
        import fcntl
        with (self.out/'.build.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(gc.retention,'plan_retention',side_effect=AssertionError('must not plan')):
                self.assertEqual(self.collect(execute=True)['state'],'producer-busy')
    def test_limits_restrict_actual_unlinks(self):
        self.f.orphan()
        result=self.collect(execute=True,max_files=1)
        self.assertEqual(result['removed'],1)
        self.assertGreater(self.collect()['candidateCount'],0)
        self.assertEqual(self.collect(execute=True,max_bytes=1)['removed'],0)
    def test_byte_limit_does_not_skip_index_and_remove_its_pairs(self):
        pair,_=self.f.orphan()
        current=self.f.publish();doc=self.f.cap_index(current)
        doc['geometryIndexSha256']='9'*64
        doc['pairs']={f'{n},2':pair for n in range(20)}
        sha=self.f.write_cap_index(doc)
        index=self.out/'cave-data/indexes'/f'{sha}.json'
        manifest=self.out/'cave-data/pairs'/f'{pair}.json'
        limit=manifest.stat().st_size+1
        self.assertGreater(index.stat().st_size,limit)
        self.assertEqual(self.collect(execute=True,max_bytes=limit)['removed'],0)
        self.assertTrue(index.exists());self.assertTrue(manifest.exists())
        self.assertGreater(self.collect(execute=True)['removed'],0)
        self.assertEqual(self.collect()['candidateCount'],0)
    def test_execute_preserves_dot_job_and_unpublished_generation(self):
        orphan,doc=self.f.orphan()
        self.f.write_job('.pending.json',{'capPairs':{'0,2':orphan}})
        pending,pending_doc,_=self.f.next_generation(1,publish=False,link=False)
        self.collect(execute=True)
        for sha in [orphan,pending]:self.assertTrue((self.out/'cave-data/pairs'/f'{sha}.json').exists())
        for d in [doc,pending_doc]:
            self.assertTrue((self.out/'cave-data/caps'/f"{d['capSha256']}.uic.gz").exists())
            self.assertTrue((self.out/'cave-data/objects'/f"{d['geometrySha256']}.uih").exists())
        self.assertEqual(self.collect()['candidateCount'],0)
    def test_bootstrap_grace_execute_removes_nothing(self):
        result=gc.collect(self.out,execute=True)
        self.assertEqual((result['planState'],result['removed']),('bootstrap-grace',0))
    def test_candidate_outside_allowlist_never_unlinked(self):
        real=gc.retention.plan_retention
        def bad(out,**kwargs):
            plan=real(out,**kwargs);plan['candidates'][0]['path']='../world/level.dat';return plan
        before=(self.out/'current.json').read_bytes()
        with patch.object(gc.retention,'plan_retention',side_effect=bad):
            with self.assertRaisesRegex(ValueError,'outside'):self.collect(execute=True)
        self.assertEqual(before,(self.out/'current.json').read_bytes())
    def test_changed_candidate_prevents_unlink(self):
        real=gc.retention.plan_retention
        def changed(out,**kwargs):
            plan=real(out,**kwargs);plan['candidates'][0]['identity']['mtimeNs']-=1;return plan
        with patch.object(gc.retention,'plan_retention',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'changed'):self.collect(execute=True)
        self.assertTrue((self.out/'cave-data/objects'/f"{self.f.doc['geometrySha256']}.uih").exists())
    def test_symlink_lock_refused(self):
        target=self.out/'other';target.write_text('keep')
        (self.out/'.build.lock').symlink_to(target)
        with self.assertRaises(OSError):self.collect(execute=True)
        self.assertEqual(target.read_text(),'keep')
    def test_invalid_arguments(self):
        for kwargs in ({'execute':1},{'max_files':True},{'max_files':65},{'max_bytes':0},{'max_bytes':gc.MAX_BYTES+1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):self.collect(**kwargs)
    def test_partial_unlink_failure_syncs_and_next_run_recovers(self):
        self.f.orphan();real_unlink=os.unlink;calls=[]
        def fail_second(path,*args,**kwargs):
            calls.append(path)
            if len(calls)==2:raise OSError('injected second unlink failure')
            return real_unlink(path,*args,**kwargs)
        with patch.object(gc.os,'unlink',side_effect=fail_second),patch.object(gc.hb,'_fsync_dir',wraps=gc.hb._fsync_dir) as sync:
            with self.assertRaisesRegex(gc.PartialCollectionError,'second unlink') as captured:self.collect(execute=True)
            self.assertTrue(sync.called)
        self.assertEqual(captured.exception.report['removed'],1)
        self.assertEqual(captured.exception.report['removedPaths'],[calls[0].relative_to(self.out).as_posix()])
        self.assertFalse(calls[0].exists());self.assertTrue(calls[1].exists())
        self.assertGreater(self.collect(execute=True)['removed'],0)
        self.assertEqual(self.collect()['candidateCount'],0)
    def test_sync_failure_is_reported_and_lock_is_released(self):
        with patch.object(gc.hb,'_fsync_dir',side_effect=OSError('injected fsync failure')):
            with self.assertRaisesRegex(OSError,'fsync'):self.collect(execute=True)
        self.assertEqual(self.collect()['candidateCount'],0)

if __name__=='__main__':unittest.main()

import json,unittest
from unittest.mock import patch
import world_hires as wh
import hires_bundle as hb
import hires_schedule as schedule
import cave_run_budget as budget
import test_world_hires as harness

GiB=1024**3
NOW=10000

class IntegrationTests(unittest.TestCase):
    setUp=harness.HiresStatusTests.setUp
    record=harness.HiresStatusTests.record
    build=harness.HiresStatusTests.build
    status_file=harness.HiresStatusTests.status_file
    def prepare(self,plan=((0,0),(1,0),(2,0))):
        self.out.mkdir();(self.out/hb.OWNED_MARKER).write_text('test')
        self.plan=list(plan);self.sources={job:'initial' for job in self.plan}
    def fair(self,results,free_bytes=30*GiB,**kwargs):
        return self.build(results,max_jobs=1,clock=lambda:NOW,free_bytes=free_bytes,
                          caves=True,cave_caps=True,fair_schedule=True,**kwargs)
    def ledger(self):return schedule.load(self.out.resolve())
    def success(self,selected):
        def worker(jobs):
            for x,z,fp in jobs:selected.append((x,z));yield self.record(x,z,fp)
        return worker

    def test_failed_nearest_does_not_starve_missing_regions(self):
        self.prepare();selected=[]
        def worker(jobs):
            for x,z,fp in jobs:
                selected.append((x,z))
                # Ledger must be written before the worker is handed its work.
                self.assertTrue((self.out/'schedule.json').is_file())
                yield self.record(x,z,fp,errors=[(0,0,'bad chunk')] if x==0 else [])
        for _ in range(3):self.fair(worker)
        self.assertEqual(selected,[(0,0),(1,0),(2,0)])
    def test_ledger_marks_attempt_before_dispatch_and_persists_outcome(self):
        self.prepare();seen=[]
        def worker(jobs):
            for x,z,fp in jobs:
                seen.append(self.ledger()['regions'][f'{x},{z}'])      # durable intent visible to the worker
                yield self.record(x,z,fp,errors=[(0,0,'bad chunk')] if x==0 else [])
        self.fair(worker)
        self.assertEqual(seen[-1],{'pendingSince':NOW,'attempts':1,'lastAttemptAt':NOW,'lastResult':'attempted'})
        self.assertEqual(self.ledger()['regions']['0,0'],
                         {'pendingSince':NOW,'attempts':1,'lastAttemptAt':NOW,'lastResult':'failed'})
        self.fair(worker)                                              # (0,0) backed off: (1,0) runs
        self.assertEqual(seen[-1]['lastResult'],'attempted')
        ledger=self.ledger()
        self.assertEqual(ledger['regions']['1,0'],{'pendingSince':NOW,'attempts':0,'lastAttemptAt':NOW,'lastResult':'ok'})
        self.assertEqual(ledger['regions']['0,0']['lastResult'],'failed')
        self.fair(worker)                                              # (1,0) is clean now: pruned from the ledger
        self.assertNotIn('1,0',self.ledger()['regions'])
    def test_crash_before_record_does_not_retry_same_region_immediately(self):
        self.prepare()
        def crash(jobs):
            self.assertEqual(jobs[0][:2],(0,0));raise RuntimeError('worker crash')
        with self.assertRaisesRegex(RuntimeError,'worker crash'):self.fair(crash)
        self.assertEqual(self.ledger()['regions']['0,0']['lastResult'],'attempted')
        selected=[]
        self.fair(self.success(selected));self.assertEqual(selected,[(1,0)])
    def test_changed_near_spawn_alternates_with_expansion_in_exact_order(self):
        # Run 1 (no ledger) starts with coverage: nearest missing (0,0). The spawn region then changes before every
        # later run; slots alternate update/coverage via the persisted lastClass, so expansion still advances:
        # update (0,0), coverage (1,0), update (0,0), coverage (2,0). The order guessed in review 164 holds.
        self.prepare();selected=[]
        self.fair(self.success(selected))
        for n in range(4):
            self.sources[(0,0)]=str(n);self.fair(self.success(selected))
        self.assertEqual(selected,[(0,0),(0,0),(1,0),(0,0),(2,0)])
        self.assertEqual(self.status_file()['schedule']['selected'],{'coverage':1,'update':0})

    def test_idle_reasons_are_explicit_and_state_stays_done(self):
        self.prepare(plan=[(0,0)])
        def failing(jobs):
            for x,z,fp in jobs:yield self.record(x,z,fp,errors=[(0,0,'bad chunk')])
        first=self.fair(failing)
        self.assertEqual((first['state'],first['total'],first['idle']),('done',1,None))
        dispatched=[]
        def none(jobs):
            dispatched.append(list(jobs));return iter(())
        waiting=self.fair(none)                                        # the only dirty region is in backoff
        self.assertEqual((waiting['state'],waiting['total'],waiting['idle'],waiting['notScheduled']),('done',0,'backoff',1))
        self.assertEqual(dispatched,[[]]);self.assertEqual(self.status_file()['idle'],'backoff')
        self.assertEqual(waiting['schedule']['backedOff'],1)
    def test_idle_disk_reserve_and_nothing_pending(self):
        self.prepare(plan=[(0,0)])
        # Above the 20 GiB floor (budget preflight passes) but below the 24 GiB expansion reserve.
        blocked=self.fair(lambda jobs:iter(()),free_bytes=22*GiB)
        self.assertEqual((blocked['state'],blocked['total'],blocked['idle']),('done',0,'disk-reserve'))
        self.assertTrue(blocked['schedule']['budgetBlocked']['coverage'])
        self.fair(self.success([]))
        clean=self.fair(lambda jobs:iter(()))
        self.assertEqual((clean['state'],clean['total'],clean['idle'],clean['skippedUnchanged']),('done',0,'nothing-pending',1))
    def test_geometry_identity_and_job_fingerprints_identical_with_and_without_fair_flag(self):
        self.prepare(plan=[(0,0)]);plain_jobs=[];fair_jobs=[]
        def capture(into):
            def worker(jobs):
                for x,z,fp in jobs:into.append((x,z,fp));yield self.record(x,z,fp)
            return worker
        self.fair(capture(fair_jobs))
        fair_index=json.loads((self.out/'index.json').read_text(encoding='utf-8'))
        fair_out=self.out;self.out=self.out.parent/'plain'
        try:
            self.build(capture(plain_jobs),max_jobs=1,clock=lambda:NOW,free_bytes=30*GiB,caves=True,cave_caps=True)
            plain_index=json.loads((self.out/'index.json').read_text(encoding='utf-8'))
            self.assertFalse((self.out/'schedule.json').exists())        # default path writes no ledger
            self.assertNotIn('idle',self.status_file())
        finally:self.out=fair_out
        self.assertEqual(fair_index['identity'],plain_index['identity'])
        self.assertEqual(fair_jobs,plain_jobs)                          # fingerprints embed the identity
    def test_budget_refuses_before_catalog_or_world_reads(self):
        self.prepare()
        with self.assertRaisesRegex(wh.HiresError,'budget blocked'):
            self.fair(lambda jobs:iter(()),cave_store_budget_bytes=1)
        self.assertFalse((self.out/'atlas.png').exists())
        self.assertFalse((self.out/'schedule.json').exists())
    def test_corrupt_ledger_is_not_reset(self):
        self.prepare();p=self.out/'schedule.json';p.write_bytes(b'{}')
        with self.assertRaises(ValueError):self.fair(lambda jobs:iter(()))
        self.assertEqual(p.read_bytes(),b'{}')
    def test_scan_limit_refuses_before_atlas_or_ledger_writes(self):
        self.prepare()
        (self.out/'extra').write_bytes(b'x')
        with patch.object(budget,'MAX_ENTRIES',1):
            with self.assertRaisesRegex(ValueError,'capacity'):
                self.fair(lambda jobs:iter(()))
        self.assertFalse((self.out/'atlas.png').exists())
        self.assertFalse((self.out/'schedule.json').exists())
    def test_fair_mode_requires_single_cave_job(self):
        self.prepare()
        with self.assertRaisesRegex(wh.HiresError,'Fair cave pilot'):
            wh.build(self.world,self.catalog,self.out,1,max_jobs=2,caves=True,cave_caps=True,fair_schedule=True)

if __name__=='__main__':unittest.main()

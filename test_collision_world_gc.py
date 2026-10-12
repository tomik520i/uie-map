from pathlib import Path
import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
import collision_world_gc as gc
import collision_world_retention as retention
import collision_world_store as publisher
import collision_bundle as cb
import collision_gc as legacy
from test_collision_world_store import make_pair


class WorldGcTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.out=Path(temp.name).resolve()/'out';self.store=self.out/'store';self.store.mkdir(parents=True)
        self.now=time.time();self.cache=gc.RetentionProofCache()
        (self.out/legacy.OWNED_MARKER).write_text('owned temporary test store')
        (self.out/gc.PROFILE_MARKER).write_text('world GC test opt-in')
        self.first=make_pair(self.store,0,0)[0]
        self.old=cb.publish_index(self.store,{'0,0':self.first})['indexSha256']
        self.second=make_pair(self.store,1,0)[0]
        self.current=cb.publish_index(self.store,{'1,0':self.second})['indexSha256']
        for folder in ('indexes','pairs','objects'):
            for p in (self.store/folder).iterdir():os.utime(p,(self.now-10000,self.now-10000))
        legacy.enable_gc(self.out,now=self.now)

    def tick(self,now=None,budget=None,**kw):
        return gc.run_gc(self.out,cache=self.cache,
                         budget=publisher.Budget(1000,128*1024**2) if budget is None else budget,
                         now=self.now if now is None else now,**kw)

    def extra_index(self,pairs,age=20000):
        """An additional immutable index that is NOT current (written like collision_bundle, pointer untouched)."""
        raw=cb.canonical({'format':cb.INDEX_FORMAT,'tileSize':64,'pairs':pairs})
        sha=hashlib.sha256(raw).hexdigest();path=self.store/'indexes'/f'{sha}.json'
        path.write_bytes(raw);os.utime(path,(self.now-age,self.now-age))
        return sha

    def ledger_bytes(self):
        path=self.out/gc.STATE_NAME
        return path.read_bytes() if path.exists() else None

    # ------------------------------------------------------------------ original behaviour

    def test_dry_run_is_default_and_grace_ledger_precedes_index_deletion(self):
        result=self.tick();self.assertEqual(result['state'],'dry-run')
        self.assertFalse((self.out/gc.STATE_NAME).exists())
        first=self.tick(dry_run=False);self.assertEqual(first['state'],'done');self.assertEqual(first['deleted'],[])
        target='indexes/'+self.old+'.json';seen=[]
        def unlink(path):
            ledger=json.loads((self.out/gc.STATE_NAME).read_bytes())
            self.assertIn(target,ledger['entries']);seen.append(str(path));os.unlink(path)
        result=self.tick(self.now+1801,dry_run=False,unlink=unlink)
        self.assertEqual(result['deleted'],[target]);self.assertEqual(len(seen),1)
        self.assertTrue((self.store/'pairs'/(self.first+'.json')).exists(),'separate pair phase')
        cb._verify_pair(self.store,'1,0',self.second)

    def test_three_phases_never_leave_dangling_pair_objects(self):
        self.tick(dry_run=False);self.tick(self.now+1801,dry_run=False)
        a=self.tick(self.now+1802,dry_run=False);self.assertEqual(a['deleted'],[])
        b=self.tick(self.now+3603,dry_run=False);self.assertEqual(b['phase'],'pairs')
        self.assertEqual(len(b['deleted']),1)
        c=self.tick(self.now+3604,dry_run=False);self.assertEqual(c['deleted'],[])
        d=self.tick(self.now+5405,dry_run=False);self.assertEqual(d['phase'],'objects')
        self.assertEqual(len(d['deleted']),2);cb._verify_pair(self.store,'1,0',self.second)

    def test_busy_lock_and_missing_profile_refuse(self):
        lock=legacy.default_export_lock(self.out)
        try:self.assertEqual(self.tick(dry_run=False)['state'],'busy')
        finally:lock.close()
        (self.out/gc.PROFILE_MARKER).unlink()
        self.assertEqual(self.tick(dry_run=False)['state'],'refused')

    def test_pointer_changes_after_ledger_prevent_unlink(self):
        self.tick(dry_run=False)
        original=gc.write_ledger
        def change(*a,**k):
            original(*a,**k)
            cb._replace_atomic(self.store/'current.json',cb.canonical(dict(format=cb.POINTER_FORMAT,indexSha256=self.old)))
        with patch.object(gc,'write_ledger',side_effect=change):
            result=self.tick(self.now+1801,dry_run=False)
        self.assertEqual(result['state'],'refused');self.assertEqual(result['deleted'],[])
        self.assertTrue((self.store/'indexes'/(self.old+'.json')).exists())

    def test_failed_unlink_never_claims_deleted(self):
        self.tick(dry_run=False)
        def fail(path):raise OSError('disk error')
        result=self.tick(self.now+1801,dry_run=False,unlink=fail)
        self.assertEqual(result['state'],'refused');self.assertEqual(result['deleted'],[])
        self.assertTrue((self.store/'indexes'/(self.old+'.json')).exists())

    def test_retention_cache_supports_history_without_changing_publisher_limit(self):
        self.assertEqual(gc.RetentionProofCache(180000).max_entries,180000)
        with self.assertRaises(ValueError):publisher.PairProofCache(180000)
        with self.assertRaises(ValueError):gc.RetentionProofCache(500001)

    # ------------------------------------------------------------------ review corrections (task 208)

    def test_unknown_files_stay_and_never_block_an_unchanged_store(self):
        (self.store/'indexes'/'.object-leftover').write_bytes(b'temporary of a crashed publisher')
        (self.store/'pairs'/'notes.txt').write_text('foreign')
        self.assertEqual(self.tick(dry_run=False)['state'],'done')
        result=self.tick(self.now+1801,dry_run=False)
        self.assertEqual((result['state'],result['deleted']),('done',['indexes/'+self.old+'.json']))
        self.assertTrue((self.store/'indexes'/'.object-leftover').exists())
        self.assertTrue((self.store/'pairs'/'notes.txt').exists())

    def test_partial_unlink_failure_still_fsyncs_the_phase_directory(self):
        older=self.extra_index({})                                  # oldest candidate, deleted first
        self.tick(dry_run=False)
        calls=[];synced=[];original=cb._fsync_directory
        def unlink(path):
            calls.append(path)
            if len(calls)==2:raise OSError('injected failure on the second unlink')
            os.unlink(path)
        def fsync(path):
            synced.append(Path(path).name);return original(path)
        before=self.ledger_bytes()
        with patch.object(cb,'_fsync_directory',side_effect=fsync):
            result=self.tick(self.now+1801,dry_run=False,unlink=unlink)
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(result['deleted'],['indexes/'+older+'.json'])
        self.assertTrue(result['phaseFsynced'])
        self.assertIn('indexes',synced,'phase directory fsynced although the loop failed')
        self.assertFalse((self.store/'indexes'/(older+'.json')).exists())
        self.assertTrue((self.store/'indexes'/(self.old+'.json')).exists())
        self.assertNotEqual(self.ledger_bytes(),before,'observations were persisted before deleting')

    def test_final_ledger_write_failure_is_reported_as_stopped(self):
        self.tick(dry_run=False)
        original=gc.write_ledger;writes=[]
        def flaky(*a,**k):
            writes.append(1)
            if len(writes)==2:raise OSError('ledger disk full')
            return original(*a,**k)
        with patch.object(gc,'write_ledger',side_effect=flaky):
            result=self.tick(self.now+1801,dry_run=False)
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(result['deleted'],['indexes/'+self.old+'.json'])
        self.assertTrue(result['phaseFsynced'])
        self.assertFalse((self.store/'indexes'/(self.old+'.json')).exists())
        again=self.tick(self.now+1802,dry_run=False)                  # stale ledger entry is simply dropped
        self.assertEqual(again['state'],'done')

    def reach_phase(self,phase):
        self.tick(dry_run=False);self.tick(self.now+1801,dry_run=False)          # index deleted
        self.tick(self.now+1802,dry_run=False)                                   # pair observed
        if phase=='pairs':return self.now+3603
        self.tick(self.now+3603,dry_run=False);self.tick(self.now+3604,dry_run=False)   # pair deleted, objects seen
        return self.now+5405

    def test_index_added_after_the_ledger_stops_pair_deletion(self):
        at=self.reach_phase('pairs')
        original=legacy._check_before_unlink
        def add_index(*a,**k):
            self.extra_index({'0,0':self.first},age=1)               # a new index referencing the pair
            return original(*a,**k)
        with patch.object(legacy,'_check_before_unlink',side_effect=add_index):
            result=self.tick(at,dry_run=False)
        self.assertEqual((result['state'],result['deleted']),('refused',[]))
        self.assertIn('indexes directory changed',result['reason'])
        self.assertTrue((self.store/'pairs'/(self.first+'.json')).exists())

    def test_pair_added_after_the_ledger_stops_object_deletion(self):
        at=self.reach_phase('objects')
        objects=sorted(p.name for p in (self.store/'objects').iterdir())
        original=legacy._check_before_unlink
        def add_pair(*a,**k):
            (self.store/'pairs'/('c'*64+'.json')).write_bytes(b'{}')
            return original(*a,**k)
        with patch.object(legacy,'_check_before_unlink',side_effect=add_pair):
            result=self.tick(at,dry_run=False)
        self.assertEqual((result['state'],result['deleted']),('refused',[]))
        self.assertIn('pairs directory changed',result['reason'])
        self.assertEqual(sorted(p.name for p in (self.store/'objects').iterdir()),objects)

    def test_listing_changed_between_plan_and_deletion_refuses(self):
        self.tick(dry_run=False)
        original=gc.write_ledger
        def add(*a,**k):
            original(*a,**k);self.extra_index({},age=1)
        with patch.object(gc,'write_ledger',side_effect=add):
            result=self.tick(self.now+1801,dry_run=False)
        self.assertEqual((result['state'],result['deleted']),('refused',[]))
        self.assertIn('listing changed',result['reason'])

    def test_malformed_ledger_refuses_and_is_left_unchanged(self):
        bad=[b'not json',
             json.dumps({'format':gc.STATE_FORMAT,'store':'/elsewhere','updatedAt':1,'pointerSha256':'a'*64,
                         'entries':{}}).encode(),
             b'{"format":"'+gc.STATE_FORMAT.encode()+b'","store":"'+str(self.store).encode()+
             b'","updatedAt":Infinity,"pointerSha256":"'+b'a'*64+b'","entries":{}}',
             json.dumps({'format':gc.STATE_FORMAT,'store':str(self.store),'updatedAt':1,'pointerSha256':'a'*64,
                         'entries':{'indexes/x.json':{}}}).encode()]
        for raw in bad:
            with self.subTest(raw=raw[:40]):
                (self.out/gc.STATE_NAME).write_bytes(raw)
                result=self.tick(self.now+5000,dry_run=False)
                self.assertEqual((result['state'],result['deleted']),('refused',[]))
                self.assertEqual(self.ledger_bytes(),raw)
        self.assertTrue((self.store/'indexes'/(self.old+'.json')).exists())

    def test_cold_cache_reports_warming_without_ledger_or_deletion(self):
        legacy.record_publication(self.out,None,self.old,self.now)     # two retained pairs need proofs
        result=self.tick(dry_run=False,budget=publisher.Budget(4,1<<30))
        self.assertEqual(result['state'],'warming')
        self.assertIsNone(self.ledger_bytes())
        self.assertEqual(self.tick(dry_run=False,budget=publisher.Budget(4,1<<30))['state'],'done')

    def test_delete_budget_leaves_the_rest_for_the_next_run(self):
        older=self.extra_index({})
        self.tick(dry_run=False)
        limits=retention.WorldRetentionLimits(max_delete_files=1)
        first=self.tick(self.now+1801,dry_run=False,limits=limits)
        self.assertEqual(first['deleted'],['indexes/'+older+'.json'])
        second=self.tick(self.now+1802,dry_run=False,limits=limits)
        self.assertEqual(second['deleted'],['indexes/'+self.old+'.json'])

    def test_journal_protection_and_prune(self):
        legacy.record_publication(self.out,None,self.old,self.now+1000)
        self.assertEqual(self.tick(dry_run=False)['deleted'],[])
        self.assertEqual(self.tick(self.now+1801,dry_run=False)['deleted'],[],'protected by the journal')
        observed=self.tick(self.now+2900,dry_run=False)                 # protection expired: first observation
        self.assertEqual(observed['deleted'],[])
        self.assertIn(self.old,legacy.load_journal(self.out,self.store)['indexes'],'kept while its index exists')
        deleted=self.tick(self.now+4701,dry_run=False)
        self.assertEqual(deleted['deleted'],['indexes/'+self.old+'.json'])
        self.assertNotIn(self.old,legacy.load_journal(self.out,self.store)['indexes'],'pruned after deletion')
        self.assertIn(self.current,legacy.load_journal(self.out,self.store)['indexes'],'current index entry kept')

    def test_dry_run_must_be_a_real_bool(self):
        self.tick(dry_run=False)
        before=self.ledger_bytes()
        for value in (0,'',None,'no'):
            with self.subTest(value=value):
                result=self.tick(self.now+1801,dry_run=value)
                self.assertEqual((result['state'],result['deleted']),('refused',[]))
        self.assertEqual(self.ledger_bytes(),before)
        self.assertTrue((self.store/'indexes'/(self.old+'.json')).exists())

    def test_missing_ownership_marker_is_refused_not_raised(self):
        (self.out/legacy.OWNED_MARKER).unlink()
        result=self.tick(dry_run=False)
        self.assertEqual((result['state'],result['deleted']),('refused',[]))

    def test_legacy_ledger_in_the_same_output_is_refused(self):
        (self.out/legacy.STATE_NAME).write_text('{}')
        result=self.tick(dry_run=False)
        self.assertEqual(result['state'],'refused')
        self.assertIn('legacy GC ledger',result['reason'])

    def test_legacy_gc_refuses_world_profile_before_writing_its_ledger(self):
        result=legacy.run_gc(self.out,now=self.now+10000)
        self.assertEqual(result['state'],'refused')
        self.assertIn('world GC profile',result['reason'])
        self.assertFalse((self.out/legacy.STATE_NAME).exists())
        self.assertEqual(result['deleted'],[])

    def test_parent_phase_sync_failure_prevents_descendant_deletion(self):
        at=self.reach_phase('pairs')
        original=cb._fsync_directory
        def fail(path):
            if Path(path)==self.store/'indexes':raise OSError('prior unlink not durable')
            return original(path)
        with patch.object(cb,'_fsync_directory',side_effect=fail):
            result=self.tick(at,dry_run=False)
        self.assertEqual((result['state'],result['deleted']),('refused',[]))
        self.assertTrue((self.store/'pairs'/(self.first+'.json')).exists())

    def test_phase_sync_failure_is_reported_after_actual_unlink(self):
        self.tick(dry_run=False);original=cb._fsync_directory
        def fail(path):
            if Path(path)==self.store/'indexes':raise OSError('fsync failed')
            return original(path)
        with patch.object(cb,'_fsync_directory',side_effect=fail):
            result=self.tick(self.now+1801,dry_run=False)
        self.assertEqual(result['state'],'stopped')
        self.assertFalse(result['phaseFsynced'])
        self.assertEqual(result['deleted'],['indexes/'+self.old+'.json'])
        self.assertIn('fsync failed',result['fsyncError'])


if __name__=='__main__':unittest.main()

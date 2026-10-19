"""Real immutable pairs: unchanged region verification is not a whole-store audit."""
import os
from unittest import mock
import collision_world_store as cws
from test_collision_world_store import StoreCase,make_pair

class RegionNoopTests(StoreCase):
 def setUp(self):
  super().setUp()
  self.pairs={f'{x},0':make_pair(self.store,x,0)[0] for x in range(2)}
  self.index=self.publish(cws.PairProofCache(),None,self.pairs)['indexSha256']
  self.calls.clear()
 def run_region(self,updates=None,removals=frozenset(),cache=None,budget=None):
  return cws.publish_region(self.store,expected_index=self.index,
   updates={'0,0':self.pairs['0,0']} if updates is None else updates,removals=removals,
   cache=cws.PairProofCache() if cache is None else cache,budget=budget or cws.Budget(100,10**9))
 def snapshot(self):return {str(p.relative_to(self.store)):p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
 def test_cold_cache_reads_only_selected_pair_without_any_write(self):
  before=self.snapshot()
  result=self.run_region()
  self.assertEqual(self.calls,['0,0']);self.assertEqual(result['verificationScope'],'updates-only')
  self.assertFalse(result['published']);self.assertEqual(result['entries'],2)
  self.assertEqual(self.snapshot(),before)
 def test_same_pair_corruption_is_not_hidden(self):
  pair=__import__('json').loads((self.store/'pairs'/(self.pairs['0,0']+'.json')).read_bytes())
  (self.store/'objects'/(pair['blocksSha256']+'.uib')).write_bytes(b'bad')
  with self.assertRaises(cws.StoreError):self.run_region()
  self.assertEqual(self.pointer(),self.index)
 def test_unrelated_corruption_is_not_claimed_as_audited_and_next_region_rejects_it(self):
  (self.store/'pairs'/(self.pairs['1,0']+'.json')).write_bytes(b'bad')
  self.assertEqual(self.run_region()['verificationScope'],'updates-only')
  with self.assertRaises(cws.StoreError):self.run_region({'1,0':self.pairs['1,0']})
  self.assertEqual(self.pointer(),self.index)
 def test_changed_pair_keeps_full_verification(self):
  new=make_pair(self.store,0,0,height=63)[0]
  result=self.run_region({'0,0':new})
  self.assertEqual(sorted(self.calls),['0,0','1,0']);self.assertFalse(result['unchanged'])
  self.assertNotEqual(result['indexSha256'],self.index)
 def test_removal_uses_full_publisher(self):
  result=self.run_region({},removals={'0,0'})
  self.assertEqual(self.calls,['1,0']);self.assertEqual(result['removed'],1)
 def test_empty_updates_keep_whole_audit_semantics(self):
  self.run_region({});self.assertEqual(sorted(self.calls),['0,0','1,0'])
 def test_pointer_race_refuses_success(self):
  cache=cws.PairProofCache();original=cache.verify
  def verify(*a,**kw):
   value=original(*a,**kw)
   (self.store/'current.json').write_bytes(b'{}')
   return value
  with mock.patch.object(cache,'verify',side_effect=verify),self.assertRaises(cws.StoreError):self.run_region(cache=cache)
 def test_budget_warmup_is_bounded_and_never_rewrites_pointer(self):
  before=self.snapshot();cache=cws.PairProofCache()
  with self.assertRaises(cws.WarmupPending):self.run_region(self.pairs,cache=cache,budget=cws.Budget(1,1))
  self.assertEqual(self.snapshot(),before)
  result=self.run_region(self.pairs,cache=cache)
  self.assertTrue(result['unchanged']);self.assertEqual(self.snapshot(),before)
 def test_conflicting_mapping_is_refused(self):
  with self.assertRaises(cws.StoreError):self.run_region(removals={'0,0'})

if __name__=='__main__':__import__('unittest').main()

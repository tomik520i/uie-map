from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock
import copy
import collision_world_runner as runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.out=Path(tmp.name).resolve()/'out';self.out.mkdir()
        self.tools=SimpleNamespace(read_index=lambda p:dict(tiles={'0,0':'a'*64,'8,0':'b'*64}))
        self.calls=[]
        def stage(jx,jz,**kw):
            self.calls.append((jx,jz))
            return dict(region=[jx,jz],ok=True,tiles={f'{x},{z}':{} for x,z in kw['tiles']})
        self.stage=stage
        self.check=Mock(side_effect=lambda report,**kw:{k:'c'*64 for k in report['tiles']})
        self.publish=Mock(return_value=dict(indexSha256='d'*64,entries=2))

    def run_pass(self,**kwargs):
        return runner.run_pass(out=self.out,world=self.out.parent/'world',hires_store=self.out.parent/'hires',
                               tools=self.tools,stage_fn=self.stage,check_fn=self.check,
                               publish_fn=self.publish,disk_free=lambda p:10**15,**kwargs)

    def test_entire_queue_and_report_reuse(self):
        result=self.run_pass()
        self.assertEqual(result['state'],'complete');self.assertEqual(self.calls,[(0,0),(1,0)])
        self.calls.clear();result=self.run_pass()
        self.assertEqual(result['reusedRegions'],2);self.assertEqual(self.calls,[])

    def test_failed_region_does_not_publish_or_starve_remaining_queue(self):
        def check(report,**kw):
            if report['region']==[0,0]:raise ValueError('source changed')
            return {'8,0':'c'*64}
        self.check.side_effect=check
        result=self.run_pass()
        self.assertEqual(result['state'],'partial');self.assertEqual(result['failedRegions'],1)
        self.assertEqual(result['completedRegions'],1);self.assertEqual(self.publish.call_count,1)

    def test_warmup_rechecks_sources_and_does_not_rerender(self):
        self.publish.side_effect=[runner.publisher.WarmupPending('cold'),dict(indexSha256='d'*64,entries=1)]
        result=self.run_pass(max_regions=1)
        self.assertEqual(result['state'],'partial','limited run is not whole-world complete')
        self.assertEqual(self.calls,[(0,0)]);self.assertEqual(self.check.call_count,2)

    def test_budget_stops_before_render(self):
        result=self.run_pass(budget_bytes=1)
        self.assertEqual(result['state'],'stopped-budget');self.assertEqual(self.calls,[])
        self.publish.assert_not_called()

    def test_negative_coordinates_plan_and_gc_refusal(self):
        jobs=runner.plan(dict(tiles={'-1,-1':'a'*64,'-8,-8':'b'*64,'-9,-8':'c'*64}))
        self.assertEqual([j[0] for j in jobs],[(-1,-1),(-2,-1)])
        (self.out/runner.cb.GC_ENABLED_MARKER).write_text('')
        with self.assertRaisesRegex(ValueError,'GC'):self.run_pass()

    def test_reused_report_missing_object_gets_one_restage_attempt(self):
        self.run_pass(max_regions=1);self.calls.clear()
        self.publish.side_effect=[runner.publisher.StoreError('missing object'),dict(indexSha256='d'*64,entries=1)]
        result=self.run_pass(max_regions=1)
        self.assertEqual(self.calls,[(0,0)]);self.assertEqual(result['failedRegions'],0)

    def test_stalled_warmup_does_not_block_next_region(self):
        self.publish.side_effect=[runner.publisher.WarmupPending('stalled')]*3+[dict(indexSha256='d'*64,entries=1)]
        result=self.run_pass()
        self.assertEqual(result['failedRegions'],1);self.assertEqual(result['completedRegions'],1)
        self.assertIn('warmup-stalled',result['errors'][0]['message'])

    def test_recover_own_scratch_only_and_preserve_unexpected_files(self):
        scratch=self.out/'scratch'/'collision-probe-abcd';scratch.mkdir(parents=True)
        (scratch/'r.0.0.mca').write_bytes(b'old copy')
        result=self.run_pass(max_regions=1)
        self.assertEqual(result['recoveredScratchDirectories'],1);self.assertFalse(scratch.exists())
        scratch.mkdir();(scratch/'do-not-remove.txt').write_text('unexpected')
        with self.assertRaisesRegex(ValueError,'unexpected scratch'):self.run_pass()
        self.assertTrue((scratch/'do-not-remove.txt').exists())

    def mismatch(self):
        return dict(region=[0,0],ok=False,fatal=None,scratchCleanup=True,
                    source=dict(before='a',copy='a',after='a',registrySha256='b',registryAfter='b'),
                    tiles={'0,0':dict(result='mismatch',geometrySha256='e'*64)})

    def test_one_cold_retry_must_pass_normal_check_before_publication(self):
        fresh=object();self.tools.fresh=Mock(return_value=fresh)
        bad=self.mismatch();good=copy.deepcopy(bad);good['ok']=True;good['tiles']['0,0']['result']='match'
        self.stage=Mock(side_effect=[bad,good])
        def check(report,**kw):
            if not report['ok']:raise ValueError('not matched')
            return {'0,0':'c'*64}
        self.check.side_effect=check
        result=self.run_pass(max_regions=1)
        self.assertEqual((result['completedRegions'],result['coldRetries']),(1,1))
        self.assertIs(self.stage.call_args_list[1].kwargs['tools'],fresh)
        self.publish.assert_called_once()

    def test_cold_retry_mismatch_still_fails_and_never_loops(self):
        self.tools.fresh=Mock(return_value=object());self.stage=Mock(side_effect=[self.mismatch(),self.mismatch()])
        self.check.side_effect=ValueError('still mismatched')
        result=self.run_pass(max_regions=1)
        self.assertEqual(result['failedRegions'],1);self.assertEqual(self.stage.call_count,2)
        self.publish.assert_not_called()

    def test_failed_inputs_do_not_trigger_cold_retry(self):
        base=self.mismatch()
        changes=[dict(fatal='changed'),dict(scratchCleanup=False),dict(ok=True),dict(source={}),
                 dict(tiles={'0,0':dict(result='stale')}),dict(tiles={'0,0':dict(result='error')}),
                 dict(source={**base['source'],'after':'changed'})]
        for change in changes:
            with self.subTest(change=change):self.assertFalse(runner.cold_retry_candidate({**base,**change}))
        self.assertTrue(runner.cold_retry_candidate(base))

    # ------------------------------------------------------------------ task 211

    def detailed_mismatch(self):
        report=self.mismatch();report.update(counts={'mismatch':1},seconds={'total':1.5})
        return report

    def test_cold_retry_report_on_disk_keeps_the_previous_attempt(self):
        self.tools.fresh=Mock(return_value=object())
        bad=self.detailed_mismatch();good=copy.deepcopy(bad);good['ok']=True;good['tiles']['0,0']['result']='match'
        self.stage=Mock(side_effect=[bad,good])
        self.check.side_effect=lambda report,**kw:(_ for _ in ()).throw(ValueError('not matched')) if not report['ok'] else {'0,0':'c'*64}
        self.run_pass(max_regions=1)
        stored=runner.load_report(self.out/'reports'/'0,0.json')
        self.assertIs(stored['ok'],True,'the retry report replaced the first one')
        self.assertEqual(stored['coldRetry']['reason'],'stable-geometry-mismatch')
        self.assertEqual(stored['coldRetry']['previousAttempt'],
                         dict(counts={'mismatch':1},seconds={'total':1.5},geometry={'0,0':'e'*64}))

    def test_failed_cold_retry_is_still_recorded_on_disk(self):
        self.tools.fresh=Mock(return_value=object())
        self.stage=Mock(side_effect=[self.detailed_mismatch(),self.detailed_mismatch()])
        self.check.side_effect=ValueError('still mismatched')
        result=self.run_pass(max_regions=1)
        stored=runner.load_report(self.out/'reports'/'0,0.json')
        self.assertIs(stored['ok'],False)
        self.assertEqual(stored['coldRetry']['previousAttempt']['geometry'],{'0,0':'e'*64})
        self.assertEqual((result['failedRegions'],result['coldRetries']),(1,1))

    def test_retry_budget_error_fails_only_that_region_and_the_queue_continues(self):
        fresh=object();self.tools.fresh=Mock(return_value=fresh);calls=[]
        def stage(jx,jz,**kw):
            calls.append((jx,jz,kw['tools'] is fresh))
            if (jx,jz)==(1,0):return dict(region=[1,0],ok=True,tiles={'8,0':{}})
            if kw['tools'] is fresh:raise ValueError('store budget exhausted during region')
            return self.detailed_mismatch()
        self.stage=stage
        self.check.side_effect=lambda report,**kw:(_ for _ in ()).throw(ValueError('not ok')) if not report['ok'] else {k:'c'*64 for k in report['tiles']}
        result=self.run_pass()
        self.assertEqual(calls,[(0,0,False),(0,0,True),(1,0,False)])
        self.assertEqual((result['state'],result['failedRegions'],result['completedRegions']),('partial',1,1))
        self.assertEqual(result['errors'][0]['region'],[0,0]);self.assertIn('budget',result['errors'][0]['message'])
        self.assertEqual(result['coldRetries'],1);self.publish.assert_called_once()
        stored=runner.load_report(self.out/'reports'/'0,0.json')
        self.assertNotIn('coldRetry',stored,'the first attempt stays on disk when the retry did not finish')

    def test_tools_without_a_callable_fresh_never_retry(self):
        for fresh in (None,'not callable',Ellipsis):
            with self.subTest(fresh=fresh):
                self.tools=SimpleNamespace(read_index=self.tools.read_index)
                if fresh is not Ellipsis:self.tools.fresh=fresh          # Ellipsis: attribute absent
                self.stage=Mock(return_value=self.detailed_mismatch())
                self.check.side_effect=ValueError('mismatch')
                result=self.run_pass(max_regions=1)
                self.assertEqual(self.stage.call_count,1)
                self.assertEqual((result['coldRetries'],result['failedRegions']),(0,1))

    def test_unstable_or_unclean_reports_are_never_retried_in_a_real_pass(self):
        base=self.detailed_mismatch()
        changes={'copy differs':dict(source={**base['source'],'copy':'z'}),
                 'registry changed':dict(source={**base['source'],'registryAfter':'z'}),
                 'registry missing':dict(source={k:v for k,v in base['source'].items() if k!='registrySha256'}),
                 'cleanup failed':dict(scratchCleanup='leftover'),
                 'fatal':dict(fatal='world input changed'),
                 'tile changed':dict(tiles={'0,0':dict(result='changed')}),
                 'only unknown':dict(tiles={'0,0':dict(result='unknown')})}
        for label,change in changes.items():
            with self.subTest(label):
                self.tools.fresh=Mock(return_value=object())
                self.stage=Mock(return_value={**copy.deepcopy(base),**change})
                self.check.side_effect=ValueError('not ok')
                result=self.run_pass(max_regions=1)
                self.tools.fresh.assert_not_called()
                self.assertEqual(self.stage.call_count,1)
                self.assertEqual((result['coldRetries'],result['failedRegions']),(0,1))
        for bad in ({**base,'tiles':{}},{**base,'tiles':{'0,0':'mismatch'}},'not a report'):
            self.assertFalse(runner.cold_retry_candidate(bad))

    def test_repair_after_reused_report_renders_at_most_twice(self):
        # Upper bound in the real control flow: an initial render only happens when no usable report exists, and
        # the StoreError repair only when the report was reused (no render yet). Either path renders once plus at
        # most one cold retry: 2 stage calls per region and pass, never 4.
        self.run_pass(max_regions=1)                                   # ok report stored and published
        self.tools.fresh=Mock(return_value=object())
        self.stage=Mock(side_effect=[self.detailed_mismatch(),self.detailed_mismatch(),self.detailed_mismatch()])
        self.check.side_effect=lambda report,**kw:(_ for _ in ()).throw(ValueError('not ok')) if report.get('ok') is False else {k:'c'*64 for k in report['tiles']}
        self.publish.reset_mock()
        self.publish.side_effect=[runner.publisher.StoreError('missing object'),dict(indexSha256='d'*64,entries=1)]
        result=self.run_pass(max_regions=1)
        self.assertEqual(self.stage.call_count,2,'repair render + one cold retry')
        self.assertEqual(self.publish.call_count,1,'no publication after a failed repair')
        self.assertEqual((result['failedRegions'],result['coldRetries'],result['reusedRegions']),(1,1,0))


if __name__=='__main__':unittest.main()

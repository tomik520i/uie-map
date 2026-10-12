"""Tests for diagnostic_queue.py: one job per invocation, nearest first, cooldown rotation, lock, bounds, honest
complete / waiting states. backfill_job, the lock, the renderer check and fingerprints are injected; the real
backfill is covered by test_diagnose_hires_job. Stdlib only.
    python -m unittest test_diagnostic_queue -v      (from work/uie-map)
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnose_hires_job as dh
import diagnostic_queue as dq
import offline_mesher
from world_diagnostics import empty_tile_diagnostics, job_diagnostics

COMPLETE = job_diagnostics([empty_tile_diagnostics()] * 64, 64)
ORDER = [(-1, 0), (0, 0), (1, 1)]          # world_hires.plan_jobs order (distance to the origin, then key)


class Handle:
    def __init__(self, owner):
        self.owner = owner

    def close(self):
        self.owner.closed += 1


class QueueHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.world, self.catalog_dir, self.hires = base / 'world', base / 'catalog', base / 'hires'
        self.scratch, self.queue = base / 'scratch', base / 'queue' / 'state.json'
        (self.world / 'region').mkdir(parents=True)
        for jx, jz in ORDER:
            (self.world / 'region' / f'r.{jx}.{jz}.mca').write_bytes(b'')
        (self.hires / 'jobs').mkdir(parents=True)
        self.catalog_dir.mkdir()
        self.queue.parent.mkdir()
        self.clock = 1_000_000.0
        self.calls, self.results, self.fps, self.fp_errors = [], {}, {}, {}
        self.closed = 0
        self.busy = False
        self.lock_error = None
        self.renderer_error = None
        self.refresh_error = None
        self.write_error = None
        self.refreshes = 0
        self.events = []

    def record(self, job, *, fp='fp', errors=(), diagnostics=None):
        rec = {'job': list(job), 'fingerprint': fp, 'tiles': {}, 'chunks': 0, 'errors': [list(e) for e in errors]}
        if diagnostics is not None:
            rec['diagnostics'] = diagnostics
        (self.hires / 'jobs' / f'{job[0]}_{job[1]}.json').write_text(json.dumps(rec), encoding='utf-8')

    def record_bytes(self):
        return {p.name: p.read_bytes() for p in (self.hires / 'jobs').iterdir()}

    # injected
    def backfill(self, jx, jz, **kw):
        self.calls.append((jx, jz))
        result = dict(self.results.get((jx, jz), {'state': 'written', 'worldRefreshed': True}))
        if result['state'] in ('written', 'skipped'):
            self.record((jx, jz), diagnostics=COMPLETE)
        return result

    def lock(self, _hires):
        if self.lock_error:
            raise self.lock_error
        if self.busy:
            return None
        self.events.append('lock')
        return Handle(self)

    def refresh(self, hires_store, _region_dir, _renderer, planned, _now):
        """Stand-in for diagnose_hires_job.refresh_world_diagnostics (republishes diagnostics.json)."""
        self.refreshes += 1
        if self.refresh_error:
            raise self.refresh_error
        (Path(hires_store) / 'diagnostics.json').write_text(json.dumps({'jobs': {'planned': len(planned)}}), encoding='utf-8')
        return {'planned': len(planned)}

    def expected(self, _region_dir, _identity, jx, jz):
        if (jx, jz) in self.fp_errors:
            raise self.fp_errors[(jx, jz)]
        return self.fps.get((jx, jz), 'fp')

    def check_renderer(self, *_args):
        if self.renderer_error:
            raise dh.Refused(self.renderer_error)
        return {'identity': 'ID', 'atlasSha256': 'a' * 64, 'renderer': 'r'}

    def run_queue(self, persisted=True):
        real_load, real_write = dq.load_state, dq.write_state

        def load(path):
            self.events.append('load')
            return real_load(path)

        def write(path, doc):
            self.events.append('write')
            if self.write_error:
                raise self.write_error
            return real_write(path, doc)

        original_close = Handle.close

        def close(handle):
            self.events.append('close')
            original_close(handle)

        with (patch.object(dh, 'current_identity', return_value='ID'),
              patch.object(dh, 'check_renderer', side_effect=self.check_renderer),
              patch.object(dh, 'expected_fingerprint', side_effect=self.expected),
              patch.object(dh, 'refresh_world_diagnostics', side_effect=self.refresh),
              patch.object(dq, 'load_state', side_effect=load), patch.object(dq, 'write_state', side_effect=write),
              patch.object(Handle, 'close', close),
              patch.object(offline_mesher.Mesher, 'mesh_batch', side_effect=AssertionError('mesher called'))):
            doc, code = dq.run_once(world=self.world, catalog_dir=self.catalog_dir, hires_store=self.hires,
                                    scratch_out=self.scratch, queue_state=self.queue, catalog=object(), registry={},
                                    now=lambda: self.clock, backfill=self.backfill, lock=self.lock)
        if persisted:
            self.assertEqual(json.loads(self.queue.read_text(encoding='utf-8')), doc, 'queue state persisted')
            self.assertEqual(self.events[-4:], ['lock', 'load', 'write', 'close'],
                             'queue state read and written only while the build lock is held')
        return doc, code


class QueueTests(QueueHarness):
    def test_one_job_per_invocation_nearest_first_until_complete(self):
        for job in ORDER:
            self.record(job)
        for expected in ORDER:
            doc, code = self.run_queue()
            self.assertEqual((doc['state'], code, doc['lastJob']), ('processed', 0, list(expected)))
        self.assertEqual(self.calls, ORDER, 'exactly one backfill per invocation, nearest to the origin first')
        first_counts = {'planned': 3, 'noRecord': 0, 'failedGeometry': 0, 'complete': 0, 'withoutDiagnostics': 3,
                        'staleGeometry': 0, 'staleDiagnosed': 0, 'coolingDown': 0, 'checkFailed': 0}
        self.assertEqual(self.closed, 3, 'the lock is released every time')
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('complete', 0))
        self.assertEqual(doc['counts']['complete'], 3)
        self.assertEqual(len(self.calls), 3, 'nothing to do: no backfill')
        self.setUp()
        for job in ORDER:
            self.record(job)
        self.assertEqual(self.run_queue()[0]['counts'], first_counts)

    def test_stale_failed_or_unrendered_jobs_wait_for_the_renderer_never_complete(self):
        self.record((-1, 0), diagnostics=COMPLETE)
        self.record((0, 0), errors=[(0, 0, 'mesh failed')])          # failed geometry
        doc, code = self.run_queue()                                 # (1,1) has no record at all
        self.assertEqual((doc['state'], code), ('waiting-for-render', 4))
        self.assertEqual((doc['counts']['failedGeometry'], doc['counts']['noRecord'], doc['counts']['complete']), (1, 1, 1))
        self.record((0, 0))
        self.fps[(0, 0)] = 'changed-source'                          # stale fingerprint
        self.record((1, 1), diagnostics=COMPLETE)
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('waiting-for-render', 4))
        self.assertEqual(doc['counts']['staleGeometry'], 1)
        self.assertEqual(self.calls, [], 'stale or failed geometry is left to the normal renderer')

    def test_refusal_cooldown_rotation_and_retry_after_30_min(self):
        for job in ORDER:
            self.record(job)
        self.results[(-1, 0)] = {'state': 'refused', 'reason': 'the job source changed during the backfill (chunk payloads)',
                                 'worldRefreshed': False}
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('refused', 1))
        self.assertIn('chunk payloads', doc['reason'])
        self.assertEqual(doc['cooldowns']['-1,0']['until'], self.clock + dq.COOLDOWN_SECONDS)
        self.assertEqual(doc['cooldowns']['-1,0']['failures'], 1)
        doc, _ = self.run_queue()
        self.assertEqual(doc['lastJob'], [0, 0], 'the next invocation takes another candidate')
        self.assertEqual(doc['counts']['coolingDown'], 1)
        del self.results[(-1, 0)]
        self.clock += dq.COOLDOWN_SECONDS - 1
        self.assertEqual(self.run_queue()[0]['lastJob'], [1, 1])
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('cooling-down', 4), 'only a cooling candidate is left')
        self.clock += 2
        doc, code = self.run_queue()
        self.assertEqual((doc['lastJob'], doc['state']), ([-1, 0], 'processed'))
        self.assertNotIn('-1,0', doc['cooldowns'], 'success clears the cooldown')

    def test_cooldowns_are_bounded_and_expired_ones_dropped(self):
        for job in ORDER:
            self.record(job)
        cooldowns = {f'{i},9': {'until': self.clock + 10 + i, 'failures': 1, 'reason': 'x'} for i in range(1500)}
        cooldowns['old,1'] = {'until': self.clock - 1, 'failures': 3, 'reason': 'expired'}
        self.queue.write_text(json.dumps({'format': dq.FORMAT, 'cooldowns': cooldowns}), encoding='utf-8')
        doc, _ = self.run_queue()
        self.assertEqual(len(doc['cooldowns']), dq.MAX_COOLDOWNS)
        self.assertIn('1499,9', doc['cooldowns'])
        self.assertNotIn('0,9', doc['cooldowns'])
        self.assertNotIn('old,1', doc['cooldowns'])

    def test_partial_result_and_unrefreshed_summary_are_not_success(self):
        for job in ORDER:
            self.record(job)
        self.results[(-1, 0)] = {'state': 'partial', 'reason': '1 tiles could not be diagnosed', 'worldRefreshed': True}
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('partial', 2))
        self.assertIn('-1,0', doc['cooldowns'], 'other jobs get their turn first')
        self.results[(0, 0)] = {'state': 'written', 'worldRefreshed': False, 'worldError': 'diagnostics.json not refreshed: disk full'}
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('processed', 1))
        self.assertIn('disk full', doc['reason'])

    def test_busy_or_failing_lock_never_touches_the_queue_state(self):
        for job in ORDER:
            self.record(job)
        before = self.record_bytes()
        self.queue.write_text(json.dumps({'format': dq.FORMAT, 'cooldowns': {
            '5,5': {'until': self.clock + 100, 'failures': 2, 'reason': 'kept'}}}), encoding='utf-8')
        queue_bytes = self.queue.read_bytes()
        self.busy = True
        doc, code = self.run_queue(persisted=False)
        self.assertEqual((doc['state'], code, doc['counts']), ('busy', 3, None))
        self.busy = False
        self.lock_error = PermissionError('lock file not writable')
        doc, code = self.run_queue(persisted=False)
        self.assertEqual((doc['state'], code), ('error', 1))
        self.assertIn('lock file not writable', doc['reason'])
        self.assertEqual(self.queue.read_bytes(), queue_bytes, 'no write from an unlocked (stale) snapshot')
        self.assertNotIn('load', self.events, 'the queue state is not even read without the lock')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record_bytes(), before)

    def test_renderer_mismatch_waits_for_render_without_touching_the_summary(self):
        self.record((-1, 0))
        self.renderer_error = 'renderer identity differs from the last published build (re-render first)'
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('waiting-for-render', 4))
        self.assertIn('renderer identity', doc['reason'])
        self.assertEqual((self.calls, self.refreshes), ([], 0))

    def test_stale_source_of_an_already_diagnosed_job_is_not_complete(self):
        for job in ORDER:
            self.record(job, diagnostics=COMPLETE)
        self.fps[(0, 0)] = 'changed-source'
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('waiting-for-render', 4), 'no false all-done')
        self.assertEqual((doc['counts']['complete'], doc['counts']['staleGeometry'], doc['counts']['staleDiagnosed']),
                         (2, 1, 1))
        self.assertEqual(self.calls, [], 'stale geometry is re-rendered by the renderer, not backfilled')

    def test_terminal_run_repairs_a_missing_or_corrupt_world_summary(self):
        for job in ORDER:
            self.record(job, diagnostics=COMPLETE)
        summary = self.hires / 'diagnostics.json'
        doc, code = self.run_queue()                                 # summary missing after a partial failure
        self.assertEqual((doc['state'], code, doc['worldRefreshed']), ('complete', 0, True))
        self.assertTrue(summary.exists())
        summary.write_text('{broken', encoding='utf-8')
        doc, _ = self.run_queue()
        self.assertEqual(json.loads(summary.read_text(encoding='utf-8')), {'jobs': {'planned': 3}})
        self.assertEqual((self.refreshes, self.calls), (2, []), 'repaired without any backfill')

    def test_terminal_summary_failure_is_an_error_not_complete(self):
        for job in ORDER:
            self.record(job, diagnostics=COMPLETE)
        self.refresh_error = OSError('disk full')
        doc, code = self.run_queue()
        self.assertEqual((doc['state'], code, doc['worldRefreshed']), ('error', 1, False))
        self.assertIn('disk full', doc['worldError'])
        self.assertIn('complete jobs', doc['reason'])
        self.assertEqual(doc['counts']['complete'], 3, 'the job-level facts are still reported')
        self.assertFalse((self.hires / 'diagnostics.json').exists())

    def test_queue_state_write_failure_is_reported(self):
        for job in ORDER:
            self.record(job)
        self.write_error = OSError('queue disk full')
        doc, code = self.run_queue(persisted=False)
        self.assertEqual(code, 1)
        self.assertIn('queue disk full', doc['stateError'])
        self.assertEqual(self.events[-1], 'close', 'the lock is released after the failed write')

    def test_fingerprint_check_failure_cools_down_and_moves_on(self):
        for job in ORDER:
            self.record(job)
        self.fp_errors[(-1, 0)] = OSError('region header unreadable')
        doc, code = self.run_queue()
        self.assertEqual((doc['lastJob'], code), ([0, 0], 0))
        self.assertIn('header unreadable', doc['cooldowns']['-1,0']['reason'])

    def test_job_bound_and_corrupt_queue_state(self):
        self.record((-1, 0))
        self.queue.write_text('{broken', encoding='utf-8')
        with patch.object(dh, 'MAX_JOB_RECORDS', 2):
            doc, code = self.run_queue()
        self.assertEqual((doc['state'], code), ('error', 1))
        self.assertIn('exceed the bound', doc['reason'])
        self.assertIn('previous queue state ignored', doc['note'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.closed, 1)

    def test_cli_refuses_queue_state_or_scratch_inside_inputs(self):
        common = ['--world', str(self.world), '--catalog-dir', str(self.catalog_dir), '--hires-store', str(self.hires)]
        with patch.object(dq.os, 'nice', create=True):
            self.assertEqual(dq.main([*common, '--scratch-out', str(self.scratch),
                                      '--queue-state', str(self.hires / 'queue.json')]), 1)
            self.assertEqual(dq.main([*common, '--scratch-out', str(self.world / 's'),
                                      '--queue-state', str(self.queue)]), 1)
        self.assertFalse((self.hires / 'queue.json').exists())

    def test_cli_refuses_queue_directory_collision_before_loading_catalog(self):
        common = ['--world', str(self.world), '--catalog-dir', str(self.catalog_dir), '--hires-store', str(self.hires)]
        for queue in [self.scratch, self.scratch.parent]:
            self.assertEqual(dq.main([*common, '--scratch-out', str(self.scratch), '--queue-state', str(queue)]), 1)


if __name__ == '__main__':
    unittest.main()

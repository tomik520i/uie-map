"""Tests of collision_world_gc_cli: injected run/clock (no store), plus one real refusal on an unowned temp dir."""
import io
import json
from pathlib import Path
import tempfile
import unittest

import collision_world_gc as gc
import collision_world_gc_cli as cli
import collision_world_store as store_api


class FakeRun:
    """Returns the given results in order (the last one repeats) and records every call."""

    def __init__(self, *states, deleted=()):
        self.states, self.calls, self.deleted = list(states), [], list(deleted)

    def __call__(self, out, **kw):
        self.calls.append(dict(out=out, **kw))
        state = self.states[min(len(self.calls), len(self.states)) - 1]
        result = {'format': gc.RESULT_FORMAT, 'state': state, 'deleted': list(self.deleted) if state == 'stopped' else []}
        if state not in ('busy',):
            result['dryRun'] = kw['dry_run']
        return result


class Clock:
    def __init__(self, step=0.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


class CliTests(unittest.TestCase):
    def run_cli(self, *args, run=None, clock=None):
        stream = io.StringIO()
        code = cli.main(['--out', '/srv/out', *args], run=run, clock=clock or Clock(), stream=stream)
        return code, json.loads(stream.getvalue())

    def test_dry_run_is_the_default_and_reported(self):
        run = FakeRun('dry-run')
        code, report = self.run_cli(run=run)
        self.assertEqual((code, report['state'], report['dryRun']), (0, 'dry-run', True))
        self.assertIs(run.calls[0]['dry_run'], True)
        self.assertEqual(len(run.calls), 1)

    def test_explicit_apply_deletes_and_stops_after_one_decision(self):
        run = FakeRun('done')
        code, report = self.run_cli('--apply', '--max-ticks', '10', run=run)
        self.assertEqual((code, report['state'], report['dryRun']), (0, 'done', False))
        self.assertIs(run.calls[0]['dry_run'], False)
        self.assertEqual(len(run.calls), 1, 'never advances to a second deletion phase in one invocation')

    def test_one_cache_across_warm_up_ticks_with_fresh_finite_budgets(self):
        run = FakeRun('warming', 'warming', 'dry-run')
        code, report = self.run_cli('--budget-reads', '7', '--budget-mib', '3', '--cache-capacity', '1234',
                                    '--max-delete-files', '5', '--max-delete-mib', '2', run=run)
        self.assertEqual((code, report['state'], report['ticks'], report['warmingTicks']), (0, 'dry-run', 3, 2))
        caches = {id(call['cache']) for call in run.calls}
        self.assertEqual(len(caches), 1, 'the same proof cache for every tick')
        cache = run.calls[0]['cache']
        self.assertIsInstance(cache, gc.RetentionProofCache)
        self.assertEqual(cache.max_entries, 1234)
        budgets = [call['budget'] for call in run.calls]
        self.assertEqual(len({id(b) for b in budgets}), 3, 'a fresh budget per tick')
        for budget in budgets:
            self.assertIsInstance(budget, store_api.Budget)
            self.assertEqual((budget.max_reads, budget.max_bytes), (7, 3 * cli.MIB))
        limits = run.calls[0]['limits']
        self.assertEqual((limits.max_delete_files, limits.max_delete_bytes), (5, 2 * cli.MIB))

    def test_warming_is_bounded_by_ticks_and_deadline(self):
        run = FakeRun('warming')
        code, report = self.run_cli('--max-ticks', '4', run=run)
        self.assertEqual((code, report['state'], report['ticks']), (3, 'warming-incomplete', 4))
        self.assertEqual(report['reason'], 'tick limit reached')
        run = FakeRun('warming')
        code, report = self.run_cli('--max-ticks', '1000', '--deadline-seconds', '250', run=run, clock=Clock(step=50))
        self.assertEqual((code, report['state'], report['reason']), (3, 'warming-incomplete', 'deadline reached'))
        self.assertLess(len(run.calls), 10, 'the deadline stopped the warm-up long before the tick limit')

    def test_busy_ends_immediately_without_retry(self):
        run = FakeRun('busy', 'done')
        code, report = self.run_cli('--apply', '--max-ticks', '10', run=run)
        self.assertEqual((code, report['state'], report['ticks']), (4, 'busy', 1))
        self.assertEqual(len(run.calls), 1)

    def test_refusal_and_partial_deletion_are_failures_with_details(self):
        code, report = self.run_cli('--apply', run=FakeRun('refused'))
        self.assertEqual((code, report['state']), (1, 'refused'))
        run = FakeRun('warming', 'stopped', deleted=['indexes/' + 'a' * 64 + '.json'])
        code, report = self.run_cli('--apply', run=run)
        self.assertEqual((code, report['state'], report['ticks']), (1, 'stopped', 2))
        self.assertEqual(report['result']['deleted'], ['indexes/' + 'a' * 64 + '.json'])

    def test_unexpected_results_are_errors(self):
        def broken(out, **kw):
            raise RuntimeError('boom')
        code, report = self.run_cli(run=broken)
        self.assertEqual((code, report['state']), (1, 'error'))
        self.assertIn('boom', report['reason'])
        lying = lambda out, **kw: {'state': 'done', 'dryRun': False, 'deleted': []}   # asked dry run, got deletion
        code, report = self.run_cli(run=lying)
        self.assertEqual((code, report['state']), (1, 'error'))
        code, report = self.run_cli(run=lambda out, **kw: {'state': 'mystery'})
        self.assertEqual((code, report['state']), (1, 'error'))
        code, report = self.run_cli('--apply', run=lambda out, **kw: {'state': 'dry-run', 'dryRun': False})
        self.assertEqual((code, report['state']), (1, 'error'), 'apply must not end in a dry-run state')

    def test_invalid_limits_are_refused(self):
        for args in (['--max-ticks', '0'], ['--max-ticks', '1001'], ['--deadline-seconds', 'x'],
                     ['--budget-mib', '0'], ['--cache-capacity', '500001'], ['--max-delete-files', '1001'],
                     ['--max-delete-mib', '-1']):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit) as raised:
                    self.run_cli(*args, run=FakeRun('dry-run'))
                self.assertEqual(raised.exception.code, 2)
        run = FakeRun('dry-run')
        for kwargs in ({'apply': 'yes'}, {'apply': 1}, {'max_ticks': True}, {'max_ticks': 2.0},
                       {'budget_reads': 0}, {'deadline_seconds': None}, {'unknown': 1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    cli.run_maintenance('/srv/out', run=run, **kwargs)
        self.assertEqual(run.calls, [], 'nothing runs with invalid parameters')

    def test_real_run_gc_refuses_an_unowned_directory_without_creating_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp).resolve()
            stream = io.StringIO()
            code = cli.main(['--out', str(out), '--apply'], stream=stream)
            report = json.loads(stream.getvalue())
            self.assertEqual((code, report['state'], report['ticks']), (1, 'refused', 1))
            self.assertEqual(list(out.iterdir()), [], 'no markers, lock or ledger created')


if __name__ == '__main__':
    unittest.main()

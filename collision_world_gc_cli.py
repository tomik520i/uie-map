"""Bounded maintenance entry point for collision_world_gc (one invocation, NOT a service and NOT an activation).

    python3 collision_world_gc_cli.py --out OUT [--apply] [--max-ticks 50] [--deadline-seconds 900]
                                      [--budget-reads 4096] [--budget-mib 512] [--cache-capacity 300000]
                                      [--max-delete-files 100] [--max-delete-mib 256]

- Default is a DRY RUN; deletion needs the explicit --apply flag.
- Never creates the ownership, profile or enabled markers and never enables GC: an output without them is refused
  by collision_world_gc itself.
- Every tick goes through collision_world_gc.run_gc, which takes and releases out/.export.lock.
- One RetentionProofCache is kept across the ticks of THIS invocation (warm-up progresses); each tick has a fresh,
  finite verification Budget.
- Ticks repeat ONLY while the result is "warming". The first non-warming result ends the invocation, so at most one
  deletion decision (one phase) happens per invocation. "busy" ends it immediately (no busy retry). The number of
  ticks and the elapsed time (checked before each tick) are bounded.

Prints one JSON report (UIE_COLLISION_WORLD_GC_CLI1). Exit codes: 0 dry-run/done, 3 warm-up not finished within the
bounds, 4 busy (export lock held), 1 refused/stopped/error, 2 invalid arguments.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import collision_world_gc as gc
import collision_world_retention as retention
import collision_world_store as store_api

REPORT_FORMAT = 'UIE_COLLISION_WORLD_GC_CLI1'
MIB = 1024 * 1024
LIMITS = {                                    # name: (minimum, maximum, default)
    'max_ticks': (1, 1000, 50),
    'deadline_seconds': (1, 86400, 900),
    'budget_reads': (1, 100_000, 4096),
    'budget_bytes': (1, 4096 * MIB, 512 * MIB),
    'cache_capacity': (1, 500_000, 300_000),
    'max_delete_files': (1, 1000, 100),
    'max_delete_bytes': (1, 1024 * MIB, 256 * MIB),
}
EXIT = {'dry-run': 0, 'done': 0, 'warming-incomplete': 3, 'busy': 4}


def _int(name, value):
    low, high, _ = LIMITS[name]
    if type(value) is not int or not low <= value <= high:              # bool is rejected (type check)
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def run_maintenance(out, *, apply=False, run=gc.run_gc, clock=time.monotonic, cache_factory=gc.RetentionProofCache,
                    **options) -> dict:
    """Bounded ticks of run_gc; returns the JSON-safe report. Raises ValueError for invalid parameters."""
    if type(apply) is not bool:
        raise ValueError('apply must be a bool')
    unknown = set(options) - set(LIMITS)
    if unknown:
        raise ValueError(f'unknown options: {sorted(unknown)}')
    values = {name: _int(name, options.get(name, LIMITS[name][2])) for name in LIMITS}
    limits = retention.WorldRetentionLimits(max_delete_files=values['max_delete_files'],
                                            max_delete_bytes=values['max_delete_bytes'])
    cache = cache_factory(values['cache_capacity'])
    dry_run = not apply
    report = {'format': REPORT_FORMAT, 'out': str(out), 'dryRun': dry_run, 'state': None, 'ticks': 0,
              'warmingTicks': 0, 'elapsedSeconds': 0.0, 'cacheProofs': 0, 'result': None, 'reason': None,
              'limits': {k: values[k] for k in LIMITS}}
    started = clock()
    while True:
        elapsed = clock() - started
        if report['ticks'] >= values['max_ticks'] or (report['ticks'] and elapsed >= values['deadline_seconds']):
            report.update(state='warming-incomplete',
                          reason='tick limit reached' if report['ticks'] >= values['max_ticks'] else 'deadline reached')
            break
        report['ticks'] += 1
        try:
            result = run(out, cache=cache, budget=store_api.Budget(values['budget_reads'], values['budget_bytes']),
                         limits=limits, dry_run=dry_run)
        except Exception as error:                                     # a broken GC call never looks like success
            report.update(state='error', reason=f'{type(error).__name__}: {error}'[:500])
            break
        report['result'] = result
        state = result.get('state') if isinstance(result, dict) else None
        if isinstance(result, dict) and 'dryRun' in result and result['dryRun'] is not dry_run:
            report.update(state='error', reason='GC result dryRun differs from the requested mode')
            break
        if state == 'warming':
            report['warmingTicks'] += 1
            continue
        report['state'] = state if state in ('dry-run', 'done', 'busy', 'refused', 'stopped') else 'error'
        if report['state'] == 'error':
            report['reason'] = f'unexpected GC state {state!r}'
        elif state == 'dry-run' and not dry_run or state == 'done' and dry_run:
            report.update(state='error', reason='GC state does not match the requested mode')
        break
    report['elapsedSeconds'] = round(clock() - started, 3)
    report['cacheProofs'] = len(cache) if hasattr(cache, '__len__') else None
    return report


def exit_code(report) -> int:
    return EXIT.get(report.get('state'), 1)


def _bounded(name, scale=1):
    def parse(text):
        try:
            value = int(text, 10) * scale
        except ValueError:
            raise argparse.ArgumentTypeError(f'{name} must be an integer') from None
        low, high, _ = LIMITS[name]
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f'{name} out of range')
        return value
    return parse


def main(argv=None, *, run=gc.run_gc, clock=time.monotonic, stream=None) -> int:
    stream = sys.stdout if stream is None else stream
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--apply', action='store_true', help='really delete (default: dry run)')
    ap.add_argument('--max-ticks', type=_bounded('max_ticks'), default=LIMITS['max_ticks'][2])
    ap.add_argument('--deadline-seconds', type=_bounded('deadline_seconds'), default=LIMITS['deadline_seconds'][2])
    ap.add_argument('--budget-reads', type=_bounded('budget_reads'), default=LIMITS['budget_reads'][2])
    ap.add_argument('--budget-mib', dest='budget_bytes', type=_bounded('budget_bytes', MIB),
                    default=LIMITS['budget_bytes'][2])
    ap.add_argument('--cache-capacity', type=_bounded('cache_capacity'), default=LIMITS['cache_capacity'][2])
    ap.add_argument('--max-delete-files', type=_bounded('max_delete_files'), default=LIMITS['max_delete_files'][2])
    ap.add_argument('--max-delete-mib', dest='max_delete_bytes', type=_bounded('max_delete_bytes', MIB),
                    default=LIMITS['max_delete_bytes'][2])
    a = ap.parse_args(argv)
    report = run_maintenance(a.out, apply=a.apply, run=run, clock=clock,
                             **{name: getattr(a, name) for name in LIMITS})
    stream.write(json.dumps(report, sort_keys=True, default=str) + '\n')
    return exit_code(report)


if __name__ == '__main__':
    sys.exit(main())

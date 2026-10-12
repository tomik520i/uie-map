"""Diagnostics backfill queue driver: at most ONE hires job per invocation, no geometry, no scheduler of its own.

  nice -n 15 python3 diagnostic_queue.py --world W --catalog-dir C --hires-store H --scratch-out S --queue-state Q

Each invocation:
  1. checks paths (S and Q outside W, H and C), loads the catalog and the level.dat registry once;
  2. takes the hires store's .build.lock non-blocking and holds it for everything below: reading the queue state,
     selecting, the one backfill (diagnose_hires_job.backfill_job is called directly, no nested lock) and writing
     the queue state. Busy (exit 3) or a lock error (exit 1) only print a status; Q is not touched;
  3. verifies the renderer identity and atlas against index.json (mismatch: the normal renderer must run first);
  4. walks the planned jobs (world_hires.plan_jobs: existing region files, nearest to the origin first, at most
     diagnose_hires_job.MAX_JOB_RECORDS), checking the region-header fingerprint of every clean record (headers only,
     never chunk payloads; jobs cooling down without diagnostics are not checked), and selects the first clean,
     current job lacking complete diagnostics; records with tile errors, missing records and stale fingerprints
     (also already diagnosed ones) are left to the normal renderer;
  5. runs backfill_job for that one job (payload digests, budget 512 MiB, free floor 20 GiB, no mesher, no tile,
     index or atlas writes) and records the result; without a candidate it refreshes diagnostics.json instead
     (repairs a summary lost after an earlier partial failure; a failure turns the state into error);
  6. writes the queue state Q atomically, still under the lock.

Q (UIE_DIAGNOSTIC_QUEUE1): { format, state, updatedAt (epoch s), lastJob [jx,jz] | null, result (trimmed backfill
result) | null, reason, note (previous state ignored) | null, counts | null, worldRefreshed | null, worldError,
cooldowns {"jx,jz": {until, failures, reason}} (<= 1000) }
  state: processed | partial | refused | failed | complete | waiting-for-render | cooling-down | error
  (busy is only printed, never stored)
  counts (of this walk, every planned job counted once): planned, noRecord, failedGeometry, complete (clean,
  current, complete diagnostics), withoutDiagnostics (clean, current, lacking - the selected one included),
  staleGeometry (clean but source changed; staleDiagnosed of them already had diagnostics), coolingDown,
  checkFailed (fingerprint could not be computed).
  complete only when every planned job is counted in "complete".
  A refused, failed or partial job gets a 30 min cooldown, so the next invocation takes another candidate.

Exit codes: 0 a job was diagnosed (written / skipped) or everything is complete; 2 partial job; 4 nothing to do now
(waiting for the renderer or all candidates cooling down); 3 the hires lock is busy; 1 refused, failed, error, or
the world summary (diagnostics.json) could not be refreshed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

import collision_export as ce
import diagnose_hires_job as dh
import world_hires as wh

FORMAT = 'UIE_DIAGNOSTIC_QUEUE1'
COOLDOWN_SECONDS = 30 * 60
MAX_COOLDOWNS = 1000
MAX_STATE_BYTES = 1024 * 1024
RESULT_FIELDS = ('state', 'reason', 'coveredTiles', 'expectedTiles', 'failedTiles', 'seconds', 'startedAt',
                 'finishedAt', 'contentSha256', 'worldRefreshed', 'worldError')


# ------------------------------------------------------------------ queue state

def load_state(path: Path):
    """(cooldowns, note): previous cooldowns, or none with a note when the state is missing or unreadable."""
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('queue state too large')
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get('format') != FORMAT or not isinstance(doc.get('cooldowns'), dict):
            raise ValueError('not a queue state')
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as error:
        return {}, f'previous queue state ignored: {error}'[:300]
    cooldowns = {}
    for key, value in doc['cooldowns'].items():
        if (isinstance(key, str) and isinstance(value, dict) and isinstance(value.get('until'), (int, float))
                and isinstance(value.get('failures'), int)):
            cooldowns[key] = {'until': float(value['until']), 'failures': value['failures'],
                              'reason': str(value.get('reason', ''))[:300]}
    return cooldowns, None


def bounded_cooldowns(cooldowns: dict, now: float) -> dict:
    """Active cooldowns only, at most MAX_COOLDOWNS (the latest ending ones are kept)."""
    active = sorted(((k, v) for k, v in cooldowns.items() if v['until'] > now), key=lambda kv: (-kv[1]['until'], kv[0]))
    return dict(sorted(active[:MAX_COOLDOWNS]))


def write_state(path: Path, doc: dict):
    wh._atomic_write(path, json.dumps(doc, indent=1).encode())


# ------------------------------------------------------------------ selection

def select(hires_store: Path, region_dir: Path, identity: str, planned, cooldowns: dict, now: float):
    """(job | None, counts, check failures [(job, reason)]) for one walk over the planned jobs."""
    counts = {'planned': len(planned), 'noRecord': 0, 'failedGeometry': 0, 'complete': 0, 'withoutDiagnostics': 0,
              'staleGeometry': 0, 'staleDiagnosed': 0, 'coolingDown': 0, 'checkFailed': 0}
    chosen, check_failures = None, []
    for jx, jz in planned:
        key = f'{jx},{jz}'
        try:
            _raw, record = dh.load_record(hires_store / 'jobs' / f'{jx}_{jz}.json')
        except (OSError, ValueError, dh.Refused):
            counts['noRecord'] += 1                     # not rendered yet (or unreadable): the renderer's job
            continue
        if record.get('job') != [jx, jz] or record.get('errors') != []:
            counts['failedGeometry'] += 1               # tile errors: the renderer retries this job
            continue
        complete = dh.has_complete_diagnostics(record)
        if not complete and cooldowns.get(key, {}).get('until', 0) > now:
            counts['coolingDown'] += 1
            continue
        try:
            # Region headers of this job only (no chunk payloads), also for already diagnosed jobs: a "complete"
            # state must not hide a source that changed since the render.
            current = dh.expected_fingerprint(region_dir, identity, jx, jz)
        except Exception as error:
            counts['checkFailed'] += 1
            check_failures.append(((jx, jz), f'fingerprint check failed: {type(error).__name__}: {error}'[:300]))
            continue
        if record.get('fingerprint') != current:
            counts['staleGeometry'] += 1                # source changed since the render: the renderer's job
            if complete:
                counts['staleDiagnosed'] += 1
            continue
        if complete:
            counts['complete'] += 1
            continue
        counts['withoutDiagnostics'] += 1
        if chosen is None:
            chosen = (jx, jz)
    return chosen, counts, check_failures


def _cool(cooldowns: dict, key: str, now: float, reason: str):
    previous = cooldowns.get(key, {})
    cooldowns[key] = {'until': now + COOLDOWN_SECONDS, 'failures': previous.get('failures', 0) + 1,
                      'reason': (reason or '')[:300]}


# ------------------------------------------------------------------ one invocation

def run_once(*, world, catalog_dir, hires_store, scratch_out, queue_state, catalog, registry,
             now=time.time, backfill=dh.backfill_job, lock=dh._lock, **backfill_options):
    """(queue state doc, exit code). Never more than one backfill_job call.

    The queue state is read and written only while the hires .build.lock is held, so two invocations (or a build)
    never interleave. Busy or lock errors return a status without touching the queue state."""
    world, catalog_dir, hires_store, scratch_out, queue_state = (
        Path(p).resolve() for p in (world, catalog_dir, hires_store, scratch_out, queue_state))
    doc = {'format': FORMAT, 'state': None, 'updatedAt': None, 'lastJob': None, 'result': None, 'reason': None,
           'note': None, 'counts': None, 'cooldowns': None, 'worldRefreshed': None, 'worldError': None}
    try:
        handle = lock(hires_store)
    except OSError as error:
        doc.update(state='error', reason=f'hires lock unavailable: {type(error).__name__}: {error}'[:300],
                   updatedAt=now())
        return doc, 1                        # queue state untouched: no lock, no authoritative snapshot
    if handle is None:
        doc.update(state='busy', reason='a hires build holds the lock', updatedAt=now())
        return doc, 3                        # queue state untouched
    code = 1
    try:
        cooldowns, doc['note'] = load_state(queue_state)          # authoritative read under the lock
        try:
            region_dir = world / 'region'
            identity = dh.current_identity(catalog)
            try:
                renderer = dh.check_renderer(hires_store, catalog_dir, identity)
            except dh.Refused as error:
                doc.update(state='waiting-for-render', reason=str(error)[:300])
                code = 4
            else:
                planned = wh.plan_jobs(region_dir)
                if len(planned) > dh.MAX_JOB_RECORDS:
                    raise dh.Refused(f'{len(planned)} planned jobs exceed the bound {dh.MAX_JOB_RECORDS}')
                started = now()
                job, counts, check_failures = select(hires_store, region_dir, identity, planned, cooldowns, started)
                for (jx, jz), reason in check_failures:
                    _cool(cooldowns, f'{jx},{jz}', started, reason)
                doc['counts'] = counts
                if job is None:
                    pending = sum(counts[k] for k in ('noRecord', 'failedGeometry', 'staleGeometry', 'coolingDown',
                                                      'checkFailed', 'withoutDiagnostics'))
                    if pending == 0:
                        doc.update(state='complete', reason='every planned job has a clean, current record (region '
                                                            'headers checked) with complete diagnostics')
                        code = 0
                    elif counts['coolingDown'] or counts['checkFailed']:
                        doc.update(state='cooling-down', reason='remaining diagnosable jobs are cooling down after '
                                                                'a failure; others wait for the renderer')
                        code = 4
                    else:
                        doc.update(state='waiting-for-render', reason='remaining jobs have no record, tile errors or '
                                                                      'a stale fingerprint: the renderer must run first')
                        code = 4
                    # No backfill ran, so nothing else refreshed the world summary: repair it here (e.g. after a
                    # job was written but diagnostics.json failed). A failure is an error, never a quiet success.
                    try:
                        dh.refresh_world_diagnostics(hires_store, region_dir, renderer, planned, now)
                        doc['worldRefreshed'] = True
                    except Exception as error:
                        doc.update(worldRefreshed=False, state='error',
                                   worldError=f'diagnostics.json not refreshed: {type(error).__name__}: {error}'[:300],
                                   reason=f'{doc["state"]} jobs, but the world summary could not be refreshed')
                        code = 1
                else:
                    jx, jz = job
                    key = f'{jx},{jz}'
                    result = backfill(jx, jz, world=world, catalog_dir=catalog_dir, hires_store=hires_store,
                                      scratch_out=scratch_out, catalog=catalog, registry=registry, **backfill_options)
                    doc['lastJob'] = [jx, jz]
                    doc['result'] = {k: result.get(k) for k in RESULT_FIELDS}
                    state = result.get('state')
                    if state in ('written', 'skipped'):
                        cooldowns.pop(key, None)
                        doc.update(state='processed', reason=result.get('reason'))
                        code = 0
                    elif state == 'partial':
                        _cool(cooldowns, key, now(), result.get('reason'))     # let other jobs take their turn
                        doc.update(state='partial', reason=result.get('reason'))
                        code = 2
                    else:
                        _cool(cooldowns, key, now(), result.get('reason'))
                        doc.update(state='refused' if state == 'refused' else 'failed', reason=result.get('reason'))
                        code = 1
                    doc['worldRefreshed'] = bool(result.get('worldRefreshed'))
                    doc['worldError'] = result.get('worldError')
                    if state in ('written', 'skipped', 'partial') and not result.get('worldRefreshed'):
                        doc['reason'] = result.get('worldError') or 'diagnostics.json was not refreshed'
                        code = 1
        except Exception as error:
            doc.update(state='error', reason=f'{type(error).__name__}: {error}'[:300])
            code = 1
        finished = now()
        doc['updatedAt'] = finished
        doc['cooldowns'] = bounded_cooldowns(cooldowns, finished)
        try:
            write_state(queue_state, doc)                          # authoritative write, still under the lock
        except OSError as error:
            doc['stateError'] = f'queue state not written: {type(error).__name__}: {error}'[:300]
            code = 1
    finally:
        handle.close()
    return doc, code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    for name in ('--world', '--catalog-dir', '--hires-store', '--scratch-out', '--queue-state'):
        ap.add_argument(name, required=True, type=Path)
    a = ap.parse_args(argv)
    try:
        inputs = dict(world=a.world, hires=a.hires_store, catalog=a.catalog_dir)
        ce.check_paths(a.scratch_out, **inputs)
        ce.check_paths(a.queue_state, **inputs)
        scratch, queue = a.scratch_out.resolve(), a.queue_state.resolve()
        if queue == scratch or queue in scratch.parents or queue.is_dir():
            raise ce.ExportError('--queue-state must be a file, not the scratch directory or its ancestor')
    except ce.ExportError as error:
        print(json.dumps({'state': 'refused', 'reason': str(error)}))
        return 1
    if hasattr(os, 'nice'):
        os.nice(15)
    try:
        from offline_catalog import Catalog
        catalog = Catalog.load_compiled(a.catalog_dir / 'catalog.json')       # loaded once per invocation
        registry = ce.read_registry(a.world)
    except Exception as error:
        print(json.dumps({'state': 'error', 'reason': f'{type(error).__name__}: {error}'[:300]}))
        return 1
    a.queue_state.parent.mkdir(parents=True, exist_ok=True)
    doc, code = run_once(world=a.world, catalog_dir=a.catalog_dir, hires_store=a.hires_store,
                         scratch_out=a.scratch_out, queue_state=a.queue_state, catalog=catalog, registry=registry)
    print(json.dumps(doc, indent=1))
    return code


if __name__ == '__main__':
    sys.exit(main())

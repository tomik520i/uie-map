"""Private fair region scheduler for bounded world_hires runs (pure; no rendering, no service).

The caller holds out/.build.lock, computes the FULL dirty set (every region that is missing, changed or failed),
calls select(), persists the returned ledger atomically BEFORE dispatching the selected jobs, and records each
outcome with finish(). Functions never mutate their inputs and never write files.

API (invalid input raises hb.BundleError, a ValueError):
    select(pending, ledger, *, now, max_jobs, free_bytes, floor_bytes, reserve_bytes, job_reserve_bytes=64 MiB)
        -> (selected, ledger2, telemetry)
        pending: list of {"job": [rx, rz], "kind": "missing"|"changed"|"failed", "distance": number >= 0}, unique jobs.
        selected: copies of chosen pending dicts in dispatch order. ledger2: the new ledger with every selected job
        already marked attempted (persist it before dispatch: a crash then counts as a failed attempt).
    finish(ledger, job, *, ok, now) -> ledger2
        Record the outcome of a job selected earlier (lastResult "attempted"). Repeating the same outcome is
        idempotent; a contradicting outcome raises.
    load(out) -> ledger | None        out/schedule.json; None only when absent; symlink/non-regular/malformed fails.
    parse(raw) -> ledger, serialize(ledger) -> bytes    strict canonical JSON, <= MAX_BYTES, <= MAX_REGIONS.

Ledger UIE_HIRES_SCHEDULE1: {"format", "lastClass": null|"coverage"|"update",
    "regions": {"rx,rz": {"pendingSince", "attempts", "lastAttemptAt", "lastResult": null|"attempted"|"failed"|"ok"}}}

Rules:
- Only currently pending regions are kept; others are pruned. A pending region without an entry, or whose last
  result was ok (dirty again after success), is replaced ONCE by a fresh unattempted entry (pendingSince = now,
  attempts 0, lastAttemptAt/lastResult null); later calls keep that pendingSince even while the region cannot be
  dispatched. Other entries keep their pendingSince and attempts even when the kind changes (a crashed or failed
  region stays backed off although its fingerprint changed meanwhile).
- distance: int/float in 0..MAX_DISTANCE (bool, NaN, inf and out-of-range values are rejected, never raised as
  OverflowError).
- Backoff after an attempt that did not finish ok: eligible at lastAttemptAt + min(600 s * 2**(attempts-1), 24 h)
  (exponent capped, no big integers). A clock earlier than lastAttemptAt keeps the region waiting.
- Two queues: coverage = missing by (distance, rx, rz); update = changed/failed by (pendingSince, distance, rx, rz).
  Each slot prefers the class not chosen last (lastClass persists across runs), else takes the other class.
- Disk: every selected job reserves job_reserve_bytes from free_bytes; a coverage job needs the remainder to stay
  >= max(reserve_bytes, floor_bytes), an update job >= floor_bytes. job_reserve_bytes is an accounting reservation,
  NOT a guarantee that a job fits; the caller keeps its own preflight and stop checks.
"""
from __future__ import annotations

import json
import math
import re

import hires_bundle as hb


FORMAT = 'UIE_HIRES_SCHEDULE1'
NAME = 'schedule.json'
MAX_REGIONS = 10_000
MAX_BYTES = 4 * 1024 * 1024
MAX_JOBS = 16
MAX_ATTEMPTS = 1_000_000
MAX_TIMESTAMP = 2 ** 53
MAX_DISTANCE = 2 ** 53
BACKOFF_SECONDS = 600
MAX_BACKOFF_SECONDS = 24 * 3600
JOB_RESERVE_BYTES = 64 * 1024 * 1024
KINDS = ('missing', 'changed', 'failed')
CLASSES = ('coverage', 'update')
RESULTS = (None, 'attempted', 'failed', 'ok')
_KEY = re.compile(r'(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})')
_FIELDS = {'format', 'lastClass', 'regions'}
_ENTRY = {'pendingSince', 'attempts', 'lastAttemptAt', 'lastResult'}


def _timestamp(value):
    # bool excluded by type(); ints range-checked without float conversion (no OverflowError).
    if type(value) is int:
        return 0 <= value <= MAX_TIMESTAMP
    return type(value) is float and math.isfinite(value) and 0 <= value <= MAX_TIMESTAMP


def _distance(value):
    # Same bounded check as timestamps: real block distances stay far below 2**53 (~9e15); huge ints are rejected
    # by range without float conversion, so math.isfinite never sees them (no OverflowError).
    if type(value) is int:
        return 0 <= value <= MAX_DISTANCE
    return type(value) is float and math.isfinite(value) and 0 <= value <= MAX_DISTANCE


def _count(value, high):
    return type(value) is int and 0 <= value <= high


def _key(job):
    if (not isinstance(job, (list, tuple)) or len(job) != 2 or any(type(v) is not int for v in job)
            or not _KEY.fullmatch(f'{job[0]},{job[1]}')):
        raise hb.BundleError('invalid region job')
    return f'{job[0]},{job[1]}'


def _validate(doc):
    if not isinstance(doc, dict) or set(doc) != _FIELDS or doc['format'] != FORMAT:
        raise hb.BundleError('invalid schedule ledger fields')
    if doc['lastClass'] not in (None, *CLASSES):
        raise hb.BundleError('invalid schedule ledger class')
    regions = doc['regions']
    if not isinstance(regions, dict) or len(regions) > MAX_REGIONS:
        raise hb.BundleError('invalid schedule ledger regions')
    for key, entry in regions.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(entry, dict) or set(entry) != _ENTRY:
            raise hb.BundleError('invalid schedule ledger entry')
        attempts, last, result = entry['attempts'], entry['lastAttemptAt'], entry['lastResult']
        if (not _timestamp(entry['pendingSince']) or not _count(attempts, MAX_ATTEMPTS) or result not in RESULTS
                or (last is not None and not _timestamp(last))):
            raise hb.BundleError('invalid schedule ledger entry values')
        # attempted/failed <=> attempts > 0; ok: attempts 0 with a known attempt; never tried: nothing recorded.
        consistent = (attempts > 0 and last is not None) if result in ('attempted', 'failed') else \
            (attempts == 0 and last is not None) if result == 'ok' else (attempts == 0 and last is None)
        if not consistent:
            raise hb.BundleError('inconsistent schedule ledger entry')
    return doc


def serialize(doc):
    _validate(doc)
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    if len(raw) > MAX_BYTES:
        raise hb.BundleError('schedule ledger byte capacity exhausted')
    return raw


def parse(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise hb.BundleError('schedule ledger outside size limit')
    try:
        doc = hb._strict_json(raw, 'schedule ledger')
    except RecursionError:
        raise hb.BundleError('schedule ledger nesting too deep') from None
    if serialize(doc) != raw:
        raise hb.BundleError('schedule ledger is not canonical')
    return doc


def load(out):
    hb._real_dir(out)
    try:
        raw, _ = hb._read(out / NAME, MAX_BYTES)          # lstat-regular: symlink/non-regular refused
    except FileNotFoundError:
        return None
    return parse(raw)


def _delay(attempts):
    return min(BACKOFF_SECONDS * 2 ** min(attempts - 1, 8), MAX_BACKOFF_SECONDS)


def _eligible(entry, now):
    if entry['attempts'] == 0:
        return True
    return now >= entry['lastAttemptAt'] + _delay(entry['attempts'])


def _pending(pending):
    if not isinstance(pending, list) or len(pending) > MAX_REGIONS:
        raise hb.BundleError('invalid pending region list')
    items = {}
    for item in pending:
        if not isinstance(item, dict) or set(item) != {'job', 'kind', 'distance'} or item['kind'] not in KINDS:
            raise hb.BundleError('invalid pending region')
        if not _distance(item['distance']):
            raise hb.BundleError('invalid pending region distance')
        distance = item['distance']
        key = _key(item['job'])
        if key in items:
            raise hb.BundleError('duplicate pending region')
        items[key] = {'job': [item['job'][0], item['job'][1]], 'kind': item['kind'], 'distance': distance}
    return items


def select(pending, ledger, *, now, max_jobs, free_bytes, floor_bytes, reserve_bytes,
           job_reserve_bytes=JOB_RESERVE_BYTES):
    if not _timestamp(now):
        raise ValueError('now must be a finite nonnegative timestamp')
    if type(max_jobs) is not int or not 1 <= max_jobs <= MAX_JOBS:
        raise ValueError(f'max_jobs must be 1..{MAX_JOBS}')
    for name, value in (('free_bytes', free_bytes), ('floor_bytes', floor_bytes), ('reserve_bytes', reserve_bytes)):
        if type(value) is not int or value < 0:
            raise ValueError(f'{name} must be a nonnegative integer')
    if type(job_reserve_bytes) is not int or job_reserve_bytes < 1:
        raise ValueError('job_reserve_bytes must be a positive integer')
    items = _pending(pending)
    old = {} if ledger is None else _validate(ledger)['regions']
    last_class = None if ledger is None else ledger['lastClass']
    regions = {}
    for key in items:                                   # prune: only the current dirty set is kept
        entry = old.get(key)
        if entry is None:
            regions[key] = {'pendingSince': now, 'attempts': 0, 'lastAttemptAt': None, 'lastResult': None}
        elif entry['lastResult'] == 'ok':
            # Dirty again after success: a genuinely fresh, unattempted entry, created ONCE. Later calls keep this
            # pendingSince (lastResult is None now), so a region that cannot be dispatched keeps its FIFO age.
            regions[key] = {'pendingSince': now, 'attempts': 0, 'lastAttemptAt': None, 'lastResult': None}
        else:
            regions[key] = dict(entry)
    eligible = {key for key in items if _eligible(regions[key], now)}
    queues = {
        'coverage': sorted((key for key in eligible if items[key]['kind'] == 'missing'),
                           key=lambda k: (items[k]['distance'], *items[k]['job'])),
        'update': sorted((key for key in eligible if items[key]['kind'] != 'missing'),
                         key=lambda k: (regions[k]['pendingSince'], items[k]['distance'], *items[k]['job'])),
    }
    floors = {'coverage': max(reserve_bytes, floor_bytes), 'update': floor_bytes}
    heads = {name: 0 for name in CLASSES}
    blocked = {name: False for name in CLASSES}
    available = free_bytes
    selected = []
    counts = {name: 0 for name in CLASSES}
    while len(selected) < max_jobs:
        preferred = 'update' if last_class == 'coverage' else 'coverage'
        chosen = None
        for name in (preferred, 'update' if preferred == 'coverage' else 'coverage'):
            if heads[name] >= len(queues[name]):
                continue
            if available - job_reserve_bytes < floors[name]:
                blocked[name] = True
                continue
            chosen = name
            break
        if chosen is None:
            break
        key = queues[chosen][heads[chosen]]
        heads[chosen] += 1
        available -= job_reserve_bytes
        entry = regions[key]
        regions[key] = {'pendingSince': entry['pendingSince'], 'attempts': min(entry['attempts'] + 1, MAX_ATTEMPTS),
                        'lastAttemptAt': now, 'lastResult': 'attempted'}
        selected.append(dict(items[key], job=list(items[key]['job'])))
        counts[chosen] += 1
        last_class = chosen
    doc = {'format': FORMAT, 'lastClass': last_class, 'regions': regions}
    serialize(doc)                                      # enforce the byte bound before anyone persists it
    kinds = {kind: sum(1 for item in items.values() if item['kind'] == kind) for kind in KINDS}
    telemetry = {'pending': len(items), **kinds, 'eligible': len(eligible), 'backedOff': len(items) - len(eligible),
                 'selected': counts, 'budgetBlocked': blocked,
                 'waiting': {name: len(queues[name]) - heads[name] for name in CLASSES},
                 'reservedBytes': len(selected) * job_reserve_bytes, 'freeAfterReserve': available}
    return selected, doc, telemetry


def finish(ledger, job, *, ok, now):
    if not _timestamp(now):
        raise ValueError('now must be a finite nonnegative timestamp')
    if type(ok) is not bool:
        raise ValueError('ok must be boolean')
    _validate(ledger)
    key = _key(job)
    entry = ledger['regions'].get(key)
    if entry is None or entry['lastResult'] not in ('attempted', 'ok' if ok else 'failed'):
        raise hb.BundleError('finish without a matching attempted job')
    regions = {name: dict(value) for name, value in ledger['regions'].items()}
    if entry['lastResult'] != 'attempted':              # same outcome recorded again: unchanged
        return {'format': FORMAT, 'lastClass': ledger['lastClass'], 'regions': regions}
    if ok:
        updated = {**entry, 'attempts': 0, 'lastResult': 'ok'}
    else:
        # Backoff counts from the end of the failed attempt, never backwards on clock regression.
        updated = {**entry, 'lastResult': 'failed', 'lastAttemptAt': max(now, entry['lastAttemptAt'])}
    regions[key] = updated
    doc = {'format': FORMAT, 'lastClass': ledger['lastClass'], 'regions': regions}
    serialize(doc)
    return doc

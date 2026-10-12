"""Bounded public progress snapshot of the LAST publisher tick. Not a publication receipt or proof of coverage.

    write_status(out, record, *, now=None) -> dict      writes <out>/publisher-status.json (UIE_HIRES_PUBLISHER_STATUS1)

The document is rebuilt from a whitelist on every call and atomically replaces the previous one (hires_bundle._atomic:
temp file in the store, fsync, rename, directory fsync); the owned-store rules of hires_bundle._owned apply. Fields:
  format, updatedAt (epoch seconds of the write), state (one of STATES)
  captured / remaining      backfill preparation progress of this tick (tiles), when reported
  verificationCachedTiles   in-memory verification proofs of the running publisher process - NOT the number of tiles of
                            the published map; it grows during warmup and restarts at 0 with the process
  entries, indexSha256      the index this tick published or confirmed (state "published" only)
  published                 whether THIS tick committed/confirmed a publication; false on a later busy/error tick does
                            not mean that no map is published (current.json is authoritative)
  unchanged                 the published index was already current (no pointer write)
Never published: error messages, paths, tick numbers, timings, memory, session data or any other record field.
Invalid input raises ValueError before anything is written, so the previous status file stays as it was.
A reader must treat an old updatedAt as stale: the file is only rewritten while the publisher runs with --write-status.
"""
import json
import math
import time

import hires_bundle as hb

FORMAT = 'UIE_HIRES_PUBLISHER_STATUS1'
NAME = 'publisher-status.json'
MAX_BYTES = 4096
STATES = frozenset(('preparing', 'verifying', 'published', 'producer-busy',
                    'waiting-for-producer', 'waiting-for-complete-plan', 'low-disk', 'error'))
COUNTS = ('captured', 'remaining', 'verificationCachedTiles', 'entries')
FLAGS = ('published', 'unchanged')


def write_status(out, record, *, now=None):
    out = hb._owned(out)
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise ValueError('invalid status timestamp')
    if (not isinstance(record, dict) or not isinstance(record.get('state'), str)
            or record['state'] not in STATES):
        raise ValueError('invalid publisher state')
    # Do not publish exception messages, paths, session ids or arbitrary log fields.
    doc = {'format': FORMAT, 'updatedAt': now, 'state': record['state']}
    for key in COUNTS:
        value = record.get(key)
        if value is not None:                 # absent or unknown (null telemetry) is omitted, never reported as 0
            if type(value) is not int or not 0 <= value <= hb.MAX_INDEX_ENTRIES:
                raise ValueError(f'invalid status count: {key}')
            doc[key] = value
    for key in FLAGS:
        if key in record:
            if type(record[key]) is not bool:
                raise ValueError(f'invalid status flag: {key}')
            doc[key] = record[key]
    if 'indexSha256' in record:
        value = record['indexSha256']
        if not isinstance(value, str) or hb.HEX64.fullmatch(value) is None:
            raise ValueError('invalid status index hash')
        doc['indexSha256'] = value
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:                  # explicit check: an assert would vanish under python -O
        raise ValueError('publisher status exceeds 4 KiB')
    hb._atomic(out / NAME, raw)
    return doc

"""Generation-bound producer metadata for private immutable index V3."""
import hashlib
import math
import re

KEYS = {'legacyIndexSha256', 'producerFinishedAt', 'planComplete', 'plannedJobs',
        'notScheduled', 'failedJobs', 'failedTiles', 'staleTiles'}
MAX_SAFE_INTEGER = 2**53 - 1
TILE_KEY = re.compile(r'(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})')


def validate_source(source, tiles):
    if not isinstance(source, dict) or set(source) != KEYS:
        raise ValueError('invalid source metadata fields')
    if not isinstance(tiles, dict):
        raise ValueError('source tiles must be an object')
    sha = source['legacyIndexSha256']
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('invalid source index hash')
    stamp = source['producerFinishedAt']
    if type(stamp) is not int or not 0 < stamp <= MAX_SAFE_INTEGER:
        raise ValueError('invalid source completion time')
    if type(source['planComplete']) is not bool:
        raise ValueError('invalid source planComplete')
    for key in ('plannedJobs', 'notScheduled', 'failedJobs', 'failedTiles'):
        if type(source[key]) is not int or not 0 <= source[key] <= MAX_SAFE_INTEGER:
            raise ValueError(f'invalid source {key}')
    if max(source['notScheduled'], source['failedJobs']) > source['plannedJobs']:
        raise ValueError('source job count exceeds planned jobs')
    stale = source['staleTiles']
    if not isinstance(stale, list) or len(stale) > 100000:
        raise ValueError('invalid source stale tiles')
    for key in stale:
        match = TILE_KEY.fullmatch(key) if isinstance(key, str) else None
        if (match is None or max(abs(int(v)) for v in match.groups()) > 30000000 // 64
                or key not in tiles):
            raise ValueError('invalid source stale tile reference')
    if stale != sorted(set(stale)):
        raise ValueError('source stale tiles must be sorted and unique')
    if source['planComplete'] and (source['notScheduled'] or source['failedJobs']
                                   or source['failedTiles'] or stale):
        raise ValueError('complete source contains pending or failed data')
    return {**source, 'staleTiles': list(stale)}


def from_producer(raw, legacy, status):
    sha = hashlib.sha256(raw).hexdigest()
    receipt = status.get('immutablePreparation')
    if (status.get('format') != 'UIE_HIRES_STATUS1' or status.get('state') != 'done'
            or not isinstance(receipt, dict) or receipt.get('legacyIndexSha256') != sha):
        raise ValueError('source producer receipt does not match index')
    stale = legacy.get('stale', [])
    if not isinstance(stale, list) or any(not isinstance(key, str) for key in stale):
        raise ValueError('invalid legacy stale references')
    if len(stale) != len(set(stale)):
        raise ValueError('duplicate legacy stale references')
    source = {key: status.get(key) for key in
              ('planComplete', 'plannedJobs', 'notScheduled', 'failedJobs', 'failedTiles')}
    finished = status.get('finishedAt')
    if type(finished) not in (int, float) or not math.isfinite(finished) or finished < 1:
        raise ValueError('invalid producer completion time')
    source.update(legacyIndexSha256=sha, producerFinishedAt=int(finished),
                  staleTiles=sorted(stale))
    return validate_source(source, legacy['tiles'])

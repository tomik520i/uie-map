"""Private, bounded publication history for immutable hires indexes.

The caller holds the exclusive store build lock. Call after the candidate index is
durable and before replacing current.json. This journal records references only; it
never deletes files and is not evidence of what current.json points to. A failed
pointer replacement can leave a conservative, extra history reference. A timestamp
is the latest publication or retirement protection time for its index.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

import hires_bundle as hb
from hires_context import validate_context


FORMAT = 'UIE_HIRES_PUBLICATION_HISTORY1'
NAME = 'publication-history.json'
GRACE_SECONDS = 30 * 60
MAX_RECORDS = 256
MAX_JOURNAL_BYTES = 64 * 1024
_FIELDS = {'format', 'currentIndexSha256', 'previousIndexSha256', 'lastPublished'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise hb.BundleError(f'duplicate JSON key {key!r}')
        result[key] = value
    return result


def _constant(name):
    raise hb.BundleError(f'non-finite JSON value {name}')


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=_constant)
    except (UnicodeError, ValueError) as error:
        raise hb.BundleError(f'invalid publication history/index JSON: {error}') from None


def _sha(value):
    return isinstance(value, str) and hb.HEX64.fullmatch(value) is not None


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _check_index(out, sha):
    """Validate the named index's bytes and schema without reading tile objects."""
    if not _sha(sha):
        raise hb.BundleError('invalid index SHA-256')
    hb._real_dir(out / 'indexes')
    raw, _ = hb._read(out / 'indexes' / f'{sha}.json', hb.MAX_INDEX_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise hb.BundleError('publication index hash mismatch')
    doc = _json(raw)
    if not isinstance(doc, dict):
        raise hb.BundleError('invalid publication index')
    v3 = doc.get('format') == hb.INDEX_FORMAT_V3
    v2 = doc.get('format') in (hb.INDEX_FORMAT_V2, hb.INDEX_FORMAT_V3)
    fields = {'format', 'tileSize', 'rendererIdentity', 'atlasSha256', 'tiles'}
    if v2:
        fields.add('context')
    if v3:
        fields.add('source')
    if (set(doc) != fields or doc.get('format') not in (hb.INDEX_FORMAT, hb.INDEX_FORMAT_V2, hb.INDEX_FORMAT_V3)
            or type(doc.get('tileSize')) is not int or doc['tileSize'] != 64
            or not _sha(doc.get('rendererIdentity')) or not _sha(doc.get('atlasSha256'))
            or not isinstance(doc.get('tiles'), dict) or len(doc['tiles']) > hb.MAX_INDEX_ENTRIES):
        raise hb.BundleError('invalid publication index schema')
    for key, object_sha in doc['tiles'].items():
        hb._tile_coords(key)
        if not _sha(object_sha):
            raise hb.BundleError('invalid publication index reference')
    if v2:
        try:
            validate_context(doc['context'])
        except ValueError as error:
            raise hb.BundleError(f'invalid publication index context: {error}') from None
    if v3:
        from hires_source import validate_source
        try:validate_source(doc['source'], doc['tiles'])
        except ValueError as error:raise hb.BundleError(f'invalid publication source: {error}') from None


def _read_journal(out):
    try:
        raw, _ = hb._read(out / NAME, MAX_JOURNAL_BYTES)
    except FileNotFoundError:
        return {}
    doc = _json(raw)
    if (not isinstance(doc, dict) or set(doc) != _FIELDS or doc['format'] != FORMAT
            or not _sha(doc['currentIndexSha256'])
            or (doc['previousIndexSha256'] is not None and not _sha(doc['previousIndexSha256']))
            or not isinstance(doc['lastPublished'], dict)
            or len(doc['lastPublished']) > MAX_RECORDS):
        raise hb.BundleError('invalid publication history schema')
    records = doc['lastPublished']
    if (doc['currentIndexSha256'] not in records
            or (doc['previousIndexSha256'] is not None and doc['previousIndexSha256'] not in records)
            or any(not _sha(sha) or not _timestamp(stamp) for sha, stamp in records.items())):
        raise hb.BundleError('invalid publication history records')
    return records


def record_publication(out, indexSha256, previousSha256=None, *, now=None):
    """Durably record candidate/previous indexes before committing the pointer.

    Entries younger than 30 minutes (and future dated entries) are never evicted to
    fit limits. Candidate and previous roots survive pruning regardless of age.
    """
    out = hb._owned(out)
    if now is None:
        now = time.time()
    if not _timestamp(now):
        raise ValueError('now must be a finite nonnegative number')
    if previousSha256 is not None and not _sha(previousSha256):
        raise hb.BundleError('invalid previous index SHA-256')
    _check_index(out, indexSha256)
    if previousSha256 is not None and previousSha256 != indexSha256:
        _check_index(out, previousSha256)
    records = dict(_read_journal(out))
    # A retired current index needs a fresh grace period even if it was first
    # published long ago. Never move protection time backwards on clock regression.
    records[indexSha256] = max(now, records.get(indexSha256, 0))
    if previousSha256 is not None:
        records[previousSha256] = max(now, records.get(previousSha256, 0))
    roots = {indexSha256, previousSha256}
    cutoff = now - GRACE_SECONDS
    before_prune = len(records)
    records = {sha: stamp for sha, stamp in records.items() if sha in roots or stamp >= cutoff}
    if len(records) > MAX_RECORDS:
        raise hb.BundleError('publication history capacity exhausted; recent indexes cannot be evicted')
    doc = {'format': FORMAT, 'currentIndexSha256': indexSha256,
           'previousIndexSha256': previousSha256, 'lastPublished': records}
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_JOURNAL_BYTES:
        raise hb.BundleError('publication history byte capacity exhausted')
    hb._atomic(out / NAME, raw)
    return {'currentIndexSha256': indexSha256, 'previousIndexSha256': previousSha256,
            'records': len(records), 'bytes': len(raw), 'pruned': before_prune - len(records)}

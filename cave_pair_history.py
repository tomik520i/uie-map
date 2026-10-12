"""Private, bounded publication history for cave cap indexes (cave-data/cap-history.json).

References only: never deletes, never writes. Not evidence of what cave-data/current.json points to; the caller reads
that pointer itself (under the exclusive build lock) and passes it to prepare(). Intended order for a later publisher
integration: durable cap index -> atomic history write of prepare(...).raw -> recheck geometry -> atomic cap pointer.
A crash between history and pointer leaves the history one publication ahead; prepare() recovers from that.

API (all raise hb.BundleError, a ValueError, on invalid input; no globals, no caches):
    parse(raw: bytes) -> dict
        Strict canonical UIE_CAVE_CAP_HISTORY1 document (<= MAX_BYTES, <= MAX_RECORDS).
    serialize(doc: dict) -> bytes
        Validate and return canonical bytes (sort_keys, compact, ASCII, no NaN), bounded by MAX_BYTES.
    load(root: Path) -> dict | None
        Read root/NAME, root = the real cave-data directory. None only when the history file is absent. A symlinked
        root or file, a non-regular, empty, oversized, changing or malformed file fails closed.
    prepare(existing, *, pointer_sha, index_sha, geometry_sha, now, pointer_geometry_sha=None,
            protected_geometry=()) -> Prepared(doc, raw, changed, pruned)
        Pure. existing: parsed history or None. pointer_sha: actual cap pointer indexSha256 or None.
        index_sha/geometry_sha: the cap index about to be published and its geometryIndexSha256.
        changed False only when the actual pointer already equals index_sha AND the recorded current (nothing to
        write, no time refresh). Whenever the pointer differs from the candidate, readers may still be served by the
        actual pointer, so its protection time is refreshed before the switch, also when retrying the same candidate
        after a crash that left the history ahead. publishedAt never moves backwards (max(now, recorded)).
        An ahead candidate abandoned for another one gets the same refresh and is then kept by grace only.

Document:
    {"format":"UIE_CAVE_CAP_HISTORY1","bootstrapAt":T,"currentIndexSha256":H,"previousIndexSha256":H|null,
     "records":{H:{"publishedAt":T,"geometryIndexSha256":H}}}
bootstrapAt is the creation time of this history (no older cap history exists before it) and is preserved forever.
current and previous (if any) are records; previous != current. previous is always the actual pointer at preparation
time (never an unserved ahead candidate). publishedAt is the latest publication or retirement protection time of that
index: for a retired index the grace starts when the actual pointer advances away from it. Times are int/float in
0..MAX_TIMESTAMP (bool, NaN, inf, negative and out-of-range values are rejected, never raised as OverflowError).

Retention of records on change: current, previous, publishedAt >= now - GRACE_SECONDS (future-dated included) and
records whose geometryIndexSha256 is in protected_geometry. Overflow of records or bytes fails closed; protected
records are never evicted to fit.
"""
from __future__ import annotations

import json
import math
from collections import namedtuple

import hires_bundle as hb


FORMAT = 'UIE_CAVE_CAP_HISTORY1'
NAME = 'cap-history.json'
GRACE_SECONDS = 30 * 60
MAX_RECORDS = 256
MAX_BYTES = 64 * 1024
MAX_PROTECTED_GEOMETRY = 1024
MAX_TIMESTAMP = 2 ** 53                                   # seconds; exact in float, far beyond any real clock
_FIELDS = {'format', 'bootstrapAt', 'currentIndexSha256', 'previousIndexSha256', 'records'}
_RECORD_FIELDS = {'publishedAt', 'geometryIndexSha256'}

Prepared = namedtuple('Prepared', 'doc raw changed pruned')


def _sha(value):
    return isinstance(value, str) and hb.HEX64.fullmatch(value) is not None


def _timestamp(value):
    # type() excludes bool. Ints are range-checked without float conversion (math.isfinite(10**400) raises
    # OverflowError); isfinite rejects inf/NaN floats, including overflowing literals such as 1e999.
    if type(value) is int:
        return 0 <= value <= MAX_TIMESTAMP
    return type(value) is float and math.isfinite(value) and 0 <= value <= MAX_TIMESTAMP


def _validate(doc):
    if not isinstance(doc, dict) or set(doc) != _FIELDS or doc['format'] != FORMAT:
        raise hb.BundleError('invalid cave cap history fields')
    if not _timestamp(doc['bootstrapAt']):
        raise hb.BundleError('invalid cave cap history bootstrap time')
    current, previous, records = doc['currentIndexSha256'], doc['previousIndexSha256'], doc['records']
    if not _sha(current) or (previous is not None and not _sha(previous)) or previous == current:
        raise hb.BundleError('invalid cave cap history pointers')
    if not isinstance(records, dict) or not 1 <= len(records) <= MAX_RECORDS:
        raise hb.BundleError('invalid cave cap history records')
    for sha, record in records.items():
        if (not _sha(sha) or not isinstance(record, dict) or set(record) != _RECORD_FIELDS
                or not _timestamp(record['publishedAt']) or not _sha(record['geometryIndexSha256'])):
            raise hb.BundleError('invalid cave cap history record')
    if current not in records or (previous is not None and previous not in records):
        raise hb.BundleError('cave cap history pointer has no record')
    return doc


def serialize(doc):
    _validate(doc)
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    if len(raw) > MAX_BYTES:
        raise hb.BundleError('cave cap history byte capacity exhausted')
    return raw


def parse(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise hb.BundleError('cave cap history outside size limit')
    try:
        doc = hb._strict_json(raw, 'cave cap history')
    except RecursionError:
        raise hb.BundleError('cave cap history nesting too deep') from None
    _validate(doc)
    if serialize(doc) != raw:
        raise hb.BundleError('cave cap history is not canonical')
    return doc


def load(root):
    hb._real_dir(root)
    try:
        raw, _ = hb._read(root / NAME, MAX_BYTES)       # lstat-regular: symlink/non-regular refused
    except FileNotFoundError:
        return None
    return parse(raw)


def _protected(values):
    if not isinstance(values, (set, frozenset, list, tuple)) or len(values) > MAX_PROTECTED_GEOMETRY:
        raise hb.BundleError('invalid protected geometry set')
    if not all(_sha(value) for value in values):
        raise hb.BundleError('invalid protected geometry hash')
    return frozenset(values)


def prepare(existing, *, pointer_sha, index_sha, geometry_sha, now, pointer_geometry_sha=None, protected_geometry=()):
    if not _timestamp(now):
        raise ValueError('now must be a finite nonnegative number')
    if not _sha(index_sha) or not _sha(geometry_sha):
        raise hb.BundleError('invalid cap index or geometry hash')
    if pointer_sha is not None and not _sha(pointer_sha):
        raise hb.BundleError('invalid cap pointer hash')
    if pointer_geometry_sha is not None and not _sha(pointer_geometry_sha):
        raise hb.BundleError('invalid cap pointer geometry hash')
    protected = _protected(protected_geometry)
    current = None
    if existing is None:
        records = {}
        bootstrap = now
    else:
        existing_raw = serialize(existing)
        records = {sha: dict(record) for sha, record in existing['records'].items()}
        bootstrap = existing['bootstrapAt']
        current, previous = existing['currentIndexSha256'], existing['previousIndexSha256']
        # The actual pointer is either the recorded current, or the recorded previous after a crash between the
        # history write and the pointer write (history ahead). Anything else: history and pointer disagree.
        if pointer_sha not in (current, previous):
            raise hb.BundleError('actual cap pointer is not in cave cap history')
    known = records.get(index_sha)
    if known is not None and known['geometryIndexSha256'] != geometry_sha:
        raise hb.BundleError('cap index recorded with different geometry')
    if pointer_sha is not None and pointer_sha in records and pointer_geometry_sha is not None \
            and records[pointer_sha]['geometryIndexSha256'] != pointer_geometry_sha:
        raise hb.BundleError('cap pointer recorded with different geometry')
    if existing is not None and pointer_sha == index_sha == existing['currentIndexSha256']:
        return Prepared(existing, existing_raw, False, 0)   # already served and recorded: no rewrite, no refresh
    new_previous = None if pointer_sha == index_sha else pointer_sha
    if new_previous is not None and new_previous not in records:   # only without history (legacy pointer)
        if pointer_geometry_sha is None:
            raise hb.BundleError('geometry of the actual cap pointer is required to record it')
        records[new_previous] = {'publishedAt': now, 'geometryIndexSha256': pointer_geometry_sha}
    if index_sha not in records:
        records[index_sha] = {'publishedAt': now, 'geometryIndexSha256': geometry_sha}
    # Grace starts now for: the candidate; the actual pointer, which keeps serving readers until the pointer switch
    # (also when the history was ahead after a crash); and an abandoned ahead candidate (kept conservatively).
    # max(): protection time never moves backwards on clock regression.
    for sha in {index_sha, pointer_sha, current}-{None}:
        if sha in records:
            records[sha]['publishedAt'] = max(now, records[sha]['publishedAt'])
    cutoff = now - GRACE_SECONDS
    keep = {sha: record for sha, record in records.items()
            if sha in (index_sha, new_previous) or record['publishedAt'] >= cutoff
            or record['geometryIndexSha256'] in protected}
    if len(keep) > MAX_RECORDS:
        raise hb.BundleError('cave cap history capacity exhausted; protected records cannot be evicted')
    doc = {'format': FORMAT, 'bootstrapAt': bootstrap, 'currentIndexSha256': index_sha,
           'previousIndexSha256': new_previous, 'records': keep}
    return Prepared(doc, serialize(doc), True, len(records) - len(keep))

"""Private read-only retention planner for an owned immutable hires store.

Caller holds the exclusive build lock. No file is deleted or modified. The returned
candidate identities are hints for a separate, guarded executor; this is not a
production garbage collector. The actual current.json pointer is authoritative.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time

import hires_bundle as hb
import hires_backfill as bf
import hires_history as hh
import hires_snapshot as hs
from hires_context import validate_context


MAX_SCANNED = 200_000
# Full saved world with 62,912 tiles, valid backfill and twelve protected
# generations charges ~78 MiB. Keep a bounded read budget with room for this
# normal grace window (verified by work/cave_retention_scale.py).
MAX_METADATA_BYTES = 128 * 1024 * 1024
MAX_CANDIDATES = 1_000
POINTER_BYTES = 4096
_KINDS = {'indexes': ('.json', hb.MAX_INDEX_BYTES),
          'objects': ('.uih', hb.MAX_OBJECT_BYTES),
          'atlases': ('.png', hb.MAX_OBJECT_BYTES),
          'layouts': ('.json', hb.MAX_INDEX_BYTES)}


class _MetadataLimit(hb.BundleError):
    pass


def _sha(value):
    return isinstance(value, str) and hb.HEX64.fullmatch(value) is not None


def _identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
            'mtimeNs': info.st_mtime_ns, 'ctimeNs': info.st_ctime_ns}


def _named(name, suffix):
    return name.endswith(suffix) and _sha(name[:-len(suffix)])


def _pointer(raw):
    doc = hh._json(raw)
    if (not isinstance(doc, dict) or set(doc) != {'format', 'indexSha256'}
            or doc['format'] != hb.POINTER_FORMAT or not _sha(doc['indexSha256'])):
        raise hb.BundleError('invalid current pointer')
    return doc['indexSha256']


def _journal(raw):
    doc = hh._json(raw)
    if (not isinstance(doc, dict) or set(doc) != hh._FIELDS or doc['format'] != hh.FORMAT
            or not _sha(doc['currentIndexSha256'])
            or (doc['previousIndexSha256'] is not None and not _sha(doc['previousIndexSha256']))
            or not isinstance(doc['lastPublished'], dict)
            or len(doc['lastPublished']) > hh.MAX_RECORDS):
        raise hb.BundleError('invalid publication history')
    records = doc['lastPublished']
    if (doc['currentIndexSha256'] not in records
            or (doc['previousIndexSha256'] is not None and doc['previousIndexSha256'] not in records)
            or any(not _sha(sha) or not hh._timestamp(stamp) for sha, stamp in records.items())):
        raise hb.BundleError('invalid publication history records')
    return doc


def _index(out, sha, charge):
    path = out / 'indexes' / f'{sha}.json'
    before = hb._regular(path)
    if not 1 <= before.st_size <= hb.MAX_INDEX_BYTES:
        raise hb.BundleError(f'retained index outside size limit: {sha}')
    # Reserve its full size before reading, so the metadata budget is a read bound.
    charge(before.st_size)
    raw, after = hb._read(path, hb.MAX_INDEX_BYTES)
    if len(raw) != before.st_size or _identity(after) != _identity(before):
        raise hb.BundleError(f'retained index {sha} changed during scan')
    if hashlib.sha256(raw).hexdigest() != sha:
        raise hb.BundleError(f'retained index {sha} hash mismatch')
    doc = hh._json(raw)
    if not isinstance(doc, dict):
        raise hb.BundleError('invalid retained index')
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
        raise hb.BundleError('invalid retained index schema')
    for key, object_sha in doc['tiles'].items():
        hb._tile_coords(key)
        if not _sha(object_sha):
            raise hb.BundleError('invalid retained tile reference')
    context = None
    if v2:
        try:
            context = validate_context(doc['context'])
        except ValueError as error:
            raise hb.BundleError(f'invalid retained context: {error}') from None
    if v3:
        from hires_source import validate_source
        try:validate_source(doc['source'], doc['tiles'])
        except ValueError as error:raise hb.BundleError(f'invalid retained source: {error}') from None
    if raw != json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8'):
        raise hb.BundleError('retained index is not canonical JSON')
    return doc, context


def _required(out, folder, sha, suffix, limit):
    path = out / folder / f'{sha}{suffix}'
    info = hb._regular(path)
    if not 1 <= info.st_size <= limit:
        raise hb.BundleError(f'retained {folder} file outside size limit: {path.name}')


def _progress_snapshot(out, charge):
    """Capture progress without following a symlink; unreadable input remains opaque."""
    path = out / bf.PROGRESS
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None, None
    identity = _identity(before)
    if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= bf.MAX_PROGRESS_BYTES:
        return identity, None
    charge(before.st_size)  # The budget is reserved before the bounded read.
    try:
        raw, after = hb._read(path, bf.MAX_PROGRESS_BYTES)
    except (OSError, hb.BundleError):
        return identity, None
    if len(raw) != before.st_size or _identity(after) != identity:
        raise hb.BundleError('backfill progress changed during retention scan')
    return identity, raw


def _progress_references(out, raw, charge):
    """Return exact backfill roots, or None when progress cannot be trusted."""
    if raw is None:
        return None
    try:
        doc = hh._json(raw)
        if (not isinstance(doc, dict) or set(doc) != bf.PROGRESS_KEYS
                or doc['format'] != bf.PROGRESS_FORMAT
                or type(doc['tileCount']) is not int
                or not 0 < doc['tileCount'] <= hb.MAX_INDEX_ENTRIES
                or type(doc['atlasLinked']) is not bool
                or not isinstance(doc['objects'], dict)
                or len(doc['objects']) > doc['tileCount']
                or (doc['objects'] and not doc['atlasLinked'])
                or any(not _sha(doc[k]) for k in ('legacyIndexSha256', 'rendererIdentity', 'atlasSha256'))):
            return None
        if raw != json.dumps(doc, sort_keys=True, separators=(',', ':')).encode('utf-8'):
            return None
        legacy_path = out / hs.LEGACY_INDEX
        legacy_before = hb._regular(legacy_path)
        if not 1 <= legacy_before.st_size <= hs.MAX_LEGACY_INDEX_BYTES:
            return None
        charge(legacy_before.st_size)
        legacy_raw, legacy_after = hb._read(legacy_path, hs.MAX_LEGACY_INDEX_BYTES)
        if len(legacy_raw) != legacy_before.st_size or _identity(legacy_after) != _identity(legacy_before):
            raise hb.BundleError('legacy index changed during retention scan')
        legacy = hs._parse_index(legacy_raw)
        tiles = legacy['tiles']
        if (hashlib.sha256(legacy_raw).hexdigest() != doc['legacyIndexSha256']
                or legacy['identity'] != doc['rendererIdentity']
                or legacy['atlasSha256'] != doc['atlasSha256']
                or len(tiles) != doc['tileCount']):
            return None
        for key, sha in doc['objects'].items():
            hb._tile_coords(key)
            if key not in tiles or not _sha(sha) or sha[:12] != tiles[key]:
                return None
        return doc
    except _MetadataLimit:
        raise
    except (OSError, ValueError, UnicodeError, KeyError):
        return None


def _progress_unchanged(out, before, raw, charge):
    path = out / bf.PROGRESS
    try:
        final = os.lstat(path)
    except FileNotFoundError:
        final = None
    if (final is None) != (before is None) or (final is not None and _identity(final) != before):
        raise hb.BundleError('backfill progress changed during retention scan')
    if raw is not None:
        charge(len(raw))
        final_raw, final_info = hb._read(path, bf.MAX_PROGRESS_BYTES)
        if final_raw != raw or _identity(final_info) != before:
            raise hb.BundleError('backfill progress changed during retention scan')


def _scan(out, charge):
    entries = {kind: {} for kind in _KINDS}
    unknown = {kind: 0 for kind in _KINDS}
    scanned = 0
    for kind, (suffix, _) in _KINDS.items():
        folder = out / kind
        try:
            hb._real_dir(folder)
        except FileNotFoundError:
            if kind in ('indexes', 'objects', 'atlases'):
                raise
            continue
        with os.scandir(folder) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > MAX_SCANNED:
                    raise hb.BundleError('retention scan entry capacity exceeded')
                charge(128 + len(entry.name.encode('utf-8', 'surrogatepass')))
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise hb.BundleError(f'symlink in immutable store: {kind}/{entry.name}')
                if not stat.S_ISREG(info.st_mode) or not _named(entry.name, suffix):
                    unknown[kind] += 1
                    continue
                entries[kind][entry.name[:-len(suffix)]] = info
    return entries, unknown, scanned


def plan_retention(out, *, now=None):
    """Return at most 1000 stale named-file candidates plus complete counts.

    All retained indexes and their object closure must be valid before any plan is
    returned. Valid backfill progress protects its exact captured references;
    invalid/unreadable progress protects all tile, atlas and layout objects.
    """
    out = hb._owned(out)
    if now is None:
        now = time.time()
    if not hh._timestamp(now):
        raise ValueError('now must be a finite nonnegative number')
    metadata = 0

    def charge(amount):
        nonlocal metadata
        metadata += amount
        if metadata > MAX_METADATA_BYTES:
            raise _MetadataLimit('retention metadata byte capacity exceeded')

    pointer_raw, pointer_info = hb._read(out / 'current.json', POINTER_BYTES)
    journal_raw, journal_info = hb._read(out / hh.NAME, hh.MAX_JOURNAL_BYTES)
    charge(len(pointer_raw) + len(journal_raw))
    current = _pointer(pointer_raw)
    journal = _journal(journal_raw)
    progress_identity, progress_raw = _progress_snapshot(out, charge)
    preparation = progress_identity is not None
    progress_doc = _progress_references(out, progress_raw, charge) if preparation else None
    entries, unknown, scanned = _scan(out, charge)
    cutoff = now - hh.GRACE_SECONDS
    roots = {current, journal['currentIndexSha256'], journal['previousIndexSha256']}
    roots.discard(None)
    roots.update(sha for sha, stamp in journal['lastPublished'].items() if stamp >= cutoff)
    roots.update(sha for sha, info in entries['indexes'].items()
                 if max(info.st_mtime, info.st_ctime) >= cutoff)
    protected = {kind: set() for kind in _KINDS}
    protected['indexes'] = roots
    for sha in roots:
        doc, context = _index(out, sha, charge)
        protected['objects'].update(doc['tiles'].values())
        protected['atlases'].add(doc['atlasSha256'])
        if context is not None:
            protected['layouts'].add(context['layoutSha256'])
    for kind, suffix, limit in (('objects', '.uih', hb.MAX_OBJECT_BYTES),
                                ('atlases', '.png', hb.MAX_OBJECT_BYTES),
                                ('layouts', '.json', hb.MAX_INDEX_BYTES)):
        for sha in protected[kind]:
            _required(out, kind, sha, suffix, limit)
    if progress_doc is not None:
        for sha in progress_doc['objects'].values():
            info = entries['objects'].get(sha)
            if info is None or not 1 <= info.st_size <= hb.MAX_OBJECT_BYTES:
                progress_doc = None
                break
        if progress_doc is not None and progress_doc['atlasLinked']:
            info = entries['atlases'].get(progress_doc['atlasSha256'])
            if info is None or not 1 <= info.st_size <= hb.MAX_OBJECT_BYTES:
                progress_doc = None
    if progress_doc is not None:
        protected['objects'].update(progress_doc['objects'].values())
        if progress_doc['atlasLinked']:
            protected['atlases'].add(progress_doc['atlasSha256'])
    elif preparation:
        for kind in ('objects', 'atlases', 'layouts'):
            protected[kind].update(entries[kind])
    candidates = []
    candidate_totals = {kind: 0 for kind in _KINDS}
    for kind, (suffix, _) in _KINDS.items():
        for sha, info in entries[kind].items():
            if sha in protected[kind] or max(info.st_mtime, info.st_ctime) >= cutoff:
                continue
            candidate_totals[kind] += 1
            if len(candidates) < MAX_CANDIDATES:
                candidates.append({'path': f'{kind}/{sha}{suffix}', 'identity': _identity(info)})
    # Detect cooperative pointer/journal change during the scan before returning a plan.
    final_pointer, final_pointer_info = hb._read(out / 'current.json', POINTER_BYTES)
    if final_pointer != pointer_raw or _identity(final_pointer_info) != _identity(pointer_info):
        raise hb.BundleError('current pointer changed during retention scan')
    final_journal, final_journal_info = hb._read(out / hh.NAME, hh.MAX_JOURNAL_BYTES)
    if final_journal != journal_raw or _identity(final_journal_info) != _identity(journal_info):
        raise hb.BundleError('publication history changed during retention scan')
    _progress_unchanged(out, progress_identity, progress_raw, charge)
    return {'currentIndexSha256': current, 'retainedIndexes': len(roots),
            'preparationProgressPresent': preparation,
            'preparationProgressValid': progress_doc is not None, 'scanned': scanned,
            'metadataBytes': metadata, 'unknown': unknown,
            'candidateTotals': candidate_totals,
            'candidateCount': sum(candidate_totals.values()),
            'candidatesOmitted': sum(candidate_totals.values()) - len(candidates),
            'candidates': candidates}

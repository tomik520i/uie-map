"""PRIVATE resumable backfill preparer: immutable object references for a large legacy hires index, in bounded steps.

    step(out, *, max_tiles=128, max_source_bytes=64 MiB, restart=False) -> dict

Not producer integration and never a publication: this module does not write current.json or indexes/ and nothing
schedules it. The caller holds the exclusive out/.build.lock for each call; `out` is an owned hires output
(hires_bundle rules). Nothing is copied or deleted; objects are hardlinks made by hires_bundle (no copying fallback).

Optional reconcile=True retains references matching the current tile hashes when the index changed BETWEEN
steps, only with identical renderer identity and atlas. Removed/changed references are dropped from progress,
not deleted from disk. The default still refuses a changed index. Changes DURING a step always refuse.
Retained references remain untrusted hints: final publication MUST fully verify object content as before;
this option is not a trusted inode ledger and does not remove final publication cost.

One call:
  1. read and strictly validate the legacy index.json (hires_snapshot rules, <= 16 MiB, <= 100 000 tiles; an empty
     map is refused);
  2. load backfill-progress.json unless restart=True. It is bound to the exact legacy index bytes (SHA-256), the
     renderer identity, the atlas hash and the tile count; any other input index refuses and asks for restart=True
     (no silent mixing). Malformed or inconsistent progress (keys outside the index, a full hash not starting with
     the indexed sha12, a referenced object or atlas that is not a regular file) refuses as well. restart=True ignores
     the old progress metadata only; objects are never removed;
  3. link the atlas once (its full SHA-256 must equal atlasSha256), then pending tiles in key order: at most
     max_tiles tiles and max_source_bytes bytes this call (the atlas, when linked in this call, counts against the
     same byte budget). A single atlas or tile larger than the whole per-call budget refuses clearly. Every tile's
     full SHA-256 must start with its legacy sha12, otherwise the call refuses without advancing it;
  4. re-read index.json and require exactly the bytes of step 1;
  5. only then atomically replace the ONE progress file. A failed call leaves the previous progress unchanged (objects
     linked by it stay as reusable orphans).

When every tile is captured the result carries the complete reference map. It was verified at ingest time against
the same bound legacy index; the guarded final publication must still re-check the unchanged legacy index under the
lock and run hires_bundle.publish_index (full verification of every object).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat

import hires_bundle as hb
import hires_snapshot as hs

PROGRESS = 'backfill-progress.json'
PROGRESS_FORMAT = 'UIE_HIRES_BACKFILL_PROGRESS1'
RESULT_FORMAT = 'UIE_HIRES_BACKFILL_STEP1'
PROGRESS_KEYS = {'format', 'legacyIndexSha256', 'rendererIdentity', 'atlasSha256', 'tileCount', 'atlasLinked', 'objects'}
MAX_PROGRESS_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TILES = 128
DEFAULT_MAX_SOURCE_BYTES = 64 * 1024 * 1024
HARD_MAX_SOURCE_BYTES = 2 * 1024 ** 3
NOTE = 'private backfill preparer, no publication, no automatic integration'
RESTART = 'call step(..., restart=True) to discard the progress metadata and begin again'


class BackfillError(hs.SnapshotError):
    pass


def _regular_object(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _load_progress(out, binding, tiles, reconcile=False):
    """Validated progress for exactly this legacy input, None when absent; BackfillError for anything else."""
    path = out / PROGRESS
    try:
        raw, _ = hb._read(path, MAX_PROGRESS_BYTES)
    except FileNotFoundError:
        return None
    except hb.BundleError as error:
        raise BackfillError(f'backfill progress unreadable ({error}); {RESTART}') from None
    try:
        doc = json.loads(raw.decode('utf-8'), object_pairs_hook=hs._no_duplicates, parse_constant=hs._reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise BackfillError(f'backfill progress is not valid JSON; {RESTART}') from None
    if (not isinstance(doc, dict) or set(doc) != PROGRESS_KEYS or doc['format'] != PROGRESS_FORMAT
            or type(doc['tileCount']) is not int or type(doc['atlasLinked']) is not bool
            or not isinstance(doc['objects'], dict)
            or not all(isinstance(doc[k], str) and hb.HEX64.fullmatch(doc[k])
                       for k in ('legacyIndexSha256', 'rendererIdentity', 'atlasSha256'))):
        raise BackfillError(f'backfill progress is malformed; {RESTART}')
    changed = any(doc[k] != v for k, v in binding.items())
    compatible = all(doc[k] == binding[k] for k in ('rendererIdentity', 'atlasSha256'))
    if changed and (not reconcile or not compatible):
        raise BackfillError(f'legacy index changed since this backfill started (progress is bound to another input); '
                            f'{RESTART}')
    objects = doc['objects']
    if not 0 < doc['tileCount'] <= hb.MAX_INDEX_ENTRIES or len(objects) > doc['tileCount'] or (objects and not doc['atlasLinked']):
        raise BackfillError(f'backfill progress is inconsistent; {RESTART}')
    if objects:
        hb._real_dir(out / 'objects')
    if doc['atlasLinked']:
        hb._real_dir(out / 'atlases')
    kept = {}
    for key, full in objects.items():
        if not isinstance(key, str) or not hb.TILE_KEY.fullmatch(key) or not isinstance(full, str) or not hb.HEX64.fullmatch(full):
            raise BackfillError(f'backfill progress entry is malformed; {RESTART}')
        if changed and reconcile and (key not in tiles or full[:12] != tiles[key]):
            continue  # removed/replaced source: recapture only this tile, never reuse its old hash
        if key not in tiles or not isinstance(full, str) or not hb.HEX64.fullmatch(full) or full[:12] != tiles[key]:
            raise BackfillError(f'backfill progress entry {key!r} does not match the legacy index; {RESTART}')
        if not _regular_object(out / 'objects' / f'{full}.uih'):
            raise BackfillError(f'object of progress entry {key} is missing; {RESTART}')
        kept[key] = full
    if doc['atlasLinked'] and not _regular_object(out / 'atlases' / f'{doc["atlasSha256"]}.png'):
        raise BackfillError(f'atlas object of the progress is missing; {RESTART}')
    return {**doc, 'objects': kept}


def _within(name, size, budget):
    if size > budget:
        raise BackfillError(f'{name} ({size} bytes) exceeds the per-call byte budget of {budget}; '
                            f'raise max_source_bytes for this item')


def step(out, *, max_tiles=DEFAULT_MAX_TILES, max_source_bytes=DEFAULT_MAX_SOURCE_BYTES, restart=False, reconcile=False):
    """One bounded backfill step (see the module docstring). Caller holds the lock. Never publishes."""
    hs._budget('max_tiles', max_tiles, hb.MAX_INDEX_ENTRIES)
    hs._budget('max_source_bytes', max_source_bytes, HARD_MAX_SOURCE_BYTES)
    if type(restart) is not bool:
        raise ValueError('restart must be a boolean')
    if type(reconcile) is not bool or (restart and reconcile):
        raise ValueError('reconcile must be a boolean and cannot be combined with restart')
    out = hb._owned(out)
    raw = hs._read_index(out)
    doc = hs._parse_index(raw)
    tiles = doc['tiles']
    if not tiles:
        raise BackfillError('legacy index has no tiles: an empty map is not backfilled (use hires_snapshot with '
                            'allow_empty=True for a legitimately empty map)')
    if len(tiles) > hb.MAX_INDEX_ENTRIES:
        raise BackfillError(f'legacy index has more than {hb.MAX_INDEX_ENTRIES} tiles')
    binding = {'legacyIndexSha256': hashlib.sha256(raw).hexdigest(), 'rendererIdentity': doc['identity'],
               'atlasSha256': doc['atlasSha256'], 'tileCount': len(tiles)}
    progress = None if restart else _load_progress(out, binding, tiles, reconcile)
    objects = dict(progress['objects']) if progress else {}
    atlas_linked = bool(progress and progress['atlasLinked'])
    hb._real_dir(out / 'tiles')

    used = 0
    if not atlas_linked:
        size = hs._alias_size(out / hs.LEGACY_ATLAS)
        _within('atlas.png', size, max_source_bytes)
        if hb.ingest_atlas(out, out / hs.LEGACY_ATLAS) != doc['atlasSha256']:
            raise BackfillError('atlas.png does not match the legacy atlasSha256')
        used += os.lstat(out / 'atlases' / f'{doc["atlasSha256"]}.png').st_size
        _within('atlas.png', used, max_source_bytes)
        atlas_linked = True

    captured = 0
    for key in sorted(tiles):
        if captured >= max_tiles:
            break
        if key in objects:
            continue
        alias = hs._alias(out, key)
        size = hs._alias_size(alias)
        _within(f'tile {key}', size, max_source_bytes)
        if used + size > max_source_bytes:
            break                                   # the next call continues here
        try:
            full = hb.ingest_tile(out, alias, key)
        except FileNotFoundError:
            raise BackfillError(f'tile {key} disappeared during the backfill') from None
        if full[:12] != tiles[key]:
            raise BackfillError(f'tile {key} no longer matches its legacy sha12; it is not advanced')
        used += os.lstat(out / 'objects' / f'{full}.uih').st_size
        if used > max_source_bytes:
            raise BackfillError(f'tile {key} grew beyond the per-call byte budget while it was linked')
        objects[key] = full
        captured += 1

    if hs._read_index(out) != raw:
        raise BackfillError('legacy index.json changed during this step; progress not advanced')
    state = {'format': PROGRESS_FORMAT, **binding, 'atlasLinked': atlas_linked, 'objects': objects}
    encoded = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
    if len(encoded) > MAX_PROGRESS_BYTES:
        raise BackfillError('backfill progress would exceed its size limit')
    # A completed unchanged preparation is polled repeatedly by the publisher.
    # Keep its file/inode stable instead of rewriting megabytes of identical JSON.
    if state != progress:
        hb._atomic(out / PROGRESS, encoded)
    complete = len(objects) == len(tiles)
    return {'format': RESULT_FORMAT, 'private': True, 'note': NOTE, 'restarted': restart,
            'reconciled': bool(progress and progress['legacyIndexSha256'] != binding['legacyIndexSha256']),
            'legacyIndexSha256': binding['legacyIndexSha256'], 'rendererIdentity': doc['identity'],
            'atlasSha256': doc['atlasSha256'], 'renderer': doc['renderer'], 'caves': doc['caves'],
            'total': len(tiles), 'captured': len(objects), 'remaining': len(tiles) - len(objects),
            'stepTiles': captured, 'stepBytes': used, 'complete': complete,
            'references': dict(sorted(objects.items())) if complete else None}

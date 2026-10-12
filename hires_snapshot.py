"""PRIVATE adapter: snapshot an existing legacy hires output into the immutable store of hires_bundle.

    snapshot(out, *, max_tiles=256, max_source_bytes=128 MiB, allow_empty=False) -> dict

No automatic integration: world_hires, the server and the web do not call this. The caller holds the exclusive
out/.build.lock and `out` is an owned hires output (hires_bundle rules: absolute, normalized, real directory with
the regular `.uie-hires-bundle-owned` marker). Nothing is copied, deleted or garbage collected; objects are hardlinks
made by hires_bundle (no copying fallback).

Order (fail closed; any error before the final publish_index leaves the previous current.json untouched):
  1. read the legacy index.json (bounded) and validate it strictly: UIE_HIRES_INDEX1, tileSize 64, atlas
     "atlas.png", full atlasSha256 and renderer identity, canonical in-world "tx,tz" keys -> sha12, no duplicate keys;
  2. preflight BEFORE any ingest: tile count and the summed size of every alias + the atlas against the budgets;
     an unexpected missing or non-regular alias refuses;
  3. ingest the atlas (its full SHA-256 must equal atlasSha256) and every tile (the full SHA-256 must start with the
     indexed sha12); the bytes actually linked count against the same budget;
  4. re-read index.json and require exactly the bytes of step 1 (a concurrent publication refuses);
  5. hires_bundle.publish_index (full re-verification of every object: fine for this small private tool, NOT for a
     production cadence), which writes the immutable index and then replaces current.json.

Empty map policy: an index with no tiles is refused unless allow_empty=True; a malformed index is refused either way.
A failed attempt may leave orphan hardlinks in objects/ or atlases/ (never published, harmless, no GC yet).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

import hires_bundle as hb
import world_hires as wh

RESULT_FORMAT = 'UIE_HIRES_SNAPSHOT_RESULT1'
LEGACY_INDEX = 'index.json'
LEGACY_ATLAS = 'atlas.png'
MAX_LEGACY_INDEX_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TILES = 256
DEFAULT_MAX_SOURCE_BYTES = 128 * 1024 * 1024
HARD_MAX_TILES = 4096                      # a whole world (~56k tiles) can never be migrated by accident
HARD_MAX_SOURCE_BYTES = 2 * 1024 ** 3
SHA12 = re.compile(r'[0-9a-f]{12}')
NOTE = 'private adapter, no automatic integration'


class SnapshotError(hb.BundleError):
    pass


def _budget(name, value, high):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise ValueError(f'{name} must be an integer in 1..{high}')


def _no_duplicates(pairs):
    doc = {}
    for key, value in pairs:
        if key in doc:
            raise SnapshotError(f'duplicate key {key!r} in legacy index')
        doc[key] = value
    return doc


def _reject_constant(name):
    raise SnapshotError(f'non-finite number {name} in legacy index')


def _read_index(out):
    try:
        raw, _ = hb._read(out / LEGACY_INDEX, MAX_LEGACY_INDEX_BYTES)
    except FileNotFoundError:
        raise SnapshotError('legacy index.json is missing') from None
    return raw


def _parse_index(raw):
    try:
        doc = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as error:
        if isinstance(error, SnapshotError):
            raise
        raise SnapshotError(f'legacy index is not JSON: {error}') from None
    if not isinstance(doc, dict) or doc.get('format') != wh.FORMAT_INDEX:
        raise SnapshotError(f'legacy index is not {wh.FORMAT_INDEX}')
    if type(doc.get('tileSize')) is not int or doc['tileSize'] != wh.TILE:
        raise SnapshotError(f'legacy tileSize is not {wh.TILE}')
    if doc.get('atlas') != LEGACY_ATLAS:
        raise SnapshotError('legacy atlas is not atlas.png')
    for field in ('atlasSha256', 'identity'):
        if not isinstance(doc.get(field), str) or not hb.HEX64.fullmatch(doc[field]):
            raise SnapshotError(f'legacy {field} is not a full SHA-256')
    if not isinstance(doc.get('renderer'), str) or not isinstance(doc.get('caves'), bool):
        raise SnapshotError('legacy renderer/caves missing')
    tiles = doc.get('tiles')
    if not isinstance(tiles, dict):
        raise SnapshotError('legacy tiles is not an object')
    for key, sha12 in tiles.items():
        try:
            hb._tile_coords(key)
        except hb.BundleError as error:
            raise SnapshotError(str(error)) from None
        if not isinstance(sha12, str) or not SHA12.fullmatch(sha12):
            raise SnapshotError(f'invalid legacy sha12 for {key}')
    return doc


def _alias_size(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise SnapshotError(f'{path.name} is missing') from None
    if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= hb.MAX_OBJECT_BYTES:
        raise SnapshotError(f'{path.name} is not a regular file within the object size limit')
    return info.st_size


def _alias(out, key):
    return out / 'tiles' / f"{key.replace(',', '_')}.uih"


def snapshot(out, *, max_tiles=DEFAULT_MAX_TILES, max_source_bytes=DEFAULT_MAX_SOURCE_BYTES, allow_empty=False):
    """Publish the legacy index of `out` as an immutable index (see the module docstring). Caller holds the lock."""
    _budget('max_tiles', max_tiles, HARD_MAX_TILES)
    _budget('max_source_bytes', max_source_bytes, HARD_MAX_SOURCE_BYTES)
    if type(allow_empty) is not bool:
        raise ValueError('allow_empty must be a boolean')
    out = hb._owned(out)
    raw = _read_index(out)
    doc = _parse_index(raw)
    tiles = doc['tiles']
    if not tiles and not allow_empty:
        raise SnapshotError('legacy index has no tiles (pass allow_empty=True for a legitimately empty map)')
    if len(tiles) > max_tiles:
        raise SnapshotError(f'{len(tiles)} tiles exceed the budget of {max_tiles}')

    # Preflight: every source exists and the total fits the budget before a single link is made.
    hb._real_dir(out / 'tiles')
    planned = _alias_size(out / LEGACY_ATLAS)
    if planned > max_source_bytes:
        raise SnapshotError(f'sources exceed the budget of {max_source_bytes} bytes')
    for key in tiles:
        planned += _alias_size(_alias(out, key))
        if planned > max_source_bytes:
            raise SnapshotError(f'sources exceed the budget of {max_source_bytes} bytes')

    linked_bytes = 0

    def count(folder, sha, suffix):
        nonlocal linked_bytes
        linked_bytes += os.lstat(out / folder / f'{sha}.{suffix}').st_size
        if linked_bytes > max_source_bytes:
            raise SnapshotError(f'linked objects exceed the budget of {max_source_bytes} bytes')

    atlas_sha = hb.ingest_atlas(out, out / LEGACY_ATLAS)
    if atlas_sha != doc['atlasSha256']:
        raise SnapshotError('atlas.png does not match the legacy atlasSha256')
    count('atlases', atlas_sha, 'png')
    full = {}
    for key in sorted(tiles):
        try:
            sha = hb.ingest_tile(out, _alias(out, key), key)
        except FileNotFoundError:
            raise SnapshotError(f'tile {key} disappeared during the snapshot') from None
        if sha[:12] != tiles[key]:
            raise SnapshotError(f'tile {key} does not match its legacy sha12')
        count('objects', sha, 'uih')
        full[key] = sha

    if _read_index(out) != raw:
        raise SnapshotError('legacy index.json changed during the snapshot')
    hb._folder(out, 'objects')            # an allowed empty map links no tile, publish_index still requires the folder
    published = hb.publish_index(out, full, atlas_sha, doc['identity'])
    return {'format': RESULT_FORMAT, 'private': True, 'note': NOTE,
            'tiles': len(full), 'sourceBytes': linked_bytes, 'preflightBytes': planned,
            'legacyIndexSha256': hashlib.sha256(raw).hexdigest(), 'atlasSha256': atlas_sha,
            'rendererIdentity': doc['identity'], 'renderer': doc['renderer'], 'caves': doc['caves'],
            'indexSha256': published['indexSha256']}

"""READ-ONLY space preflight for a separate full-surface model candidate (handoff/CLAUDE-MODEL-ROLLOUT-20261011.md §3).

    python3 model_candidate_plan.py --store SURFACE_STORE --destination-parent DIR --reserve-bytes N

Estimates whether a candidate store with the four confirmed mod cube fixes (offline_catalog.CONFIRMED_CUBES) fits on
the destination file system while other writers (the full cave render, the live surface producer) keep running.
It never writes, creates directories, deletes, hashes tile payloads or starts anything.

Inputs actually read: the owned store's current.json and the published immutable index (exact SHA-256, strict V3
schema, canonical JSON through hires_bundle._current_index), then ONE lstat per distinct referenced object and the
atlas/layout of that index. Symlinks, missing or non-regular files, an unpublished store or an index without tiles
fail closed (never reported as zero).

Estimate:
    geometryAccountedBytes = max(sum of st_size, sum of allocated blocks when the platform reports them) over the
                             DISTINCT objects of the published generation (hardlinks inside one generation once)
    requiredBytes = 2 * (geometryAccountedBytes + indexBytes) + atlasBytes + layoutBytes + reserveBytes
Two generations: the candidate's own full generation plus one retained/updated generation before GC. The caller's
reserve must cover everything else (cave growth, the live producer, the 20 GiB floor). This is an estimate, not a
guarantee: new tiles are assumed to have the current sizes (the fixes change only geometry near four block
variants and their neighbours, sizes are not proven), and job records, diagnostics, logs and unpublished leftovers
are not counted. freeBytes is one shutil.disk_usage sample of the destination parent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

import hires_bundle as hb

FORMAT = 'UIE_MODEL_CANDIDATE_PLAN1'
GENERATIONS = 2
CAVEAT = ('Estimate, not a guarantee: sizes of the current published generation stand in for the candidate; other '
          'writers change free space after this sample; job records, diagnostics, logs and unpublished files are not '
          'counted.')


class PlanError(ValueError):
    pass


def _directory(path, what):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise PlanError(f'{what} must be absolute, normalized and without symlink ancestors')
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise PlanError(f'{what} does not exist') from None
    if not stat.S_ISDIR(info.st_mode):
        raise PlanError(f'{what} is not a real directory')
    return path


def _file(path, what, limit):
    try:
        info = hb._regular(path)                     # lstat: a symlink is not a regular file
    except FileNotFoundError:
        raise PlanError(f'{what} is missing: {path.name}') from None
    if not 1 <= info.st_size <= limit:
        raise PlanError(f'{what} outside size limit: {path.name}')
    return info


def plan(store, destination_parent, *, reserve_bytes, disk_usage=shutil.disk_usage):
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError('reserve_bytes must be a nonnegative integer')
    try:
        store = hb._owned(Path(store))
    except FileNotFoundError:
        raise PlanError('store or its ownership marker is missing') from None
    destination = _directory(destination_parent, 'destination parent')
    if destination == store or store in destination.parents:
        raise PlanError('destination parent must lie outside the current store')
    current = hb._current_index(store)
    if current is None:
        raise PlanError('store has no published immutable index')
    sha, index_format, context = current
    if index_format != hb.INDEX_FORMAT_V3 or context is None:
        raise PlanError(f'published index is {index_format}, expected immutable V3')
    if context['caves'] is not False:
        raise PlanError('published index is not a surface store')
    raw, _ = hb._read(store/'indexes'/f'{sha}.json', hb.MAX_INDEX_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise PlanError('published index changed during the estimate')
    doc = hb._strict_json(raw, 'published index')
    source = doc.get('source', {})
    if (source.get('planComplete') is not True or source.get('notScheduled') != 0
            or source.get('failedJobs') != 0 or source.get('failedTiles') != 0):
        raise PlanError('published surface is incomplete or has failed jobs/tiles; cannot size a full candidate')
    tiles = doc['tiles']
    if not tiles:
        raise PlanError('published index has no tiles')
    hb._real_dir(store/'objects')
    logical = allocated = 0
    allocation_known = True
    objects = sorted(set(tiles.values()))
    for object_sha in objects:
        info = _file(store/'objects'/f'{object_sha}.uih', 'geometry object', hb.MAX_OBJECT_BYTES)
        logical += info.st_size
        blocks = getattr(info, 'st_blocks', None)
        if blocks is None:
            allocation_known = False
        else:
            allocated += blocks * 512
    hb._real_dir(store/'atlases')
    atlas = _file(store/'atlases'/f"{doc['atlasSha256']}.png", 'atlas', hb.MAX_OBJECT_BYTES).st_size
    hb._real_dir(store/'layouts')
    layout = _file(store/'layouts'/f"{context['layoutSha256']}.json", 'atlas layout', hb.MAX_INDEX_BYTES).st_size
    accounted = max(logical, allocated) if allocation_known else logical
    required = GENERATIONS * (accounted + len(raw)) + atlas + layout + reserve_bytes
    free = disk_usage(destination).free
    if type(free) is not int or free < 0:
        raise PlanError('invalid free space sample')
    final = hb._current_index(store)
    if final is None or final[0] != sha:
        raise PlanError('published index changed during the estimate')
    try:
        from offline_catalog import CONFIRMED_CUBES
        fixes = sorted(f'{name}@{",".join(str(m) for m in sorted(rule[0]))}' for (name, _, _), rule in CONFIRMED_CUBES.items())
    except Exception:                                  # informational only; never affects the estimate
        fixes = None
    return {'format': FORMAT, 'readOnly': True, 'store': str(store), 'destinationParent': str(destination),
            'indexSha256': sha, 'rendererIdentity': doc['rendererIdentity'], 'atlasSha256': doc['atlasSha256'],
            'tiles': len(tiles), 'distinctObjects': len(objects),
            'geometryLogicalBytes': logical, 'geometryAllocatedBytes': allocated if allocation_known else None,
            'geometryAccountedBytes': accounted, 'indexBytes': len(raw), 'atlasBytes': atlas, 'layoutBytes': layout,
            'generations': GENERATIONS, 'reserveBytes': reserve_bytes, 'requiredBytes': required,
            'freeBytes': free, 'marginBytes': free - required, 'sufficientSpace': free >= required,
            'candidateFixes': fixes, 'caveat': CAVEAT}


def _reserve(text):
    try:
        value = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError('reserve-bytes must be an integer') from None
    if value < 0:
        raise argparse.ArgumentTypeError('reserve-bytes must be nonnegative')
    return value


def main(argv=None, *, disk_usage=shutil.disk_usage):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--store', required=True, type=Path)
    parser.add_argument('--destination-parent', required=True, type=Path)
    parser.add_argument('--reserve-bytes', required=True, type=_reserve)
    args = parser.parse_args(argv)
    try:
        result = plan(args.store, args.destination_parent, reserve_bytes=args.reserve_bytes, disk_usage=disk_usage)
    except (OSError, ValueError) as error:              # BundleError and PlanError are ValueErrors
        print(f'model_candidate_plan: {type(error).__name__}: {error}'.replace('\n', ' ')[:500], file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())

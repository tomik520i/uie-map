"""Stage one UIB1 pair, sharing already published immutable surface geometry by hardlink.

No index/pointer publication, renderer launch, or deletion. Caller supplies a mesh from the
SAME Volume and holds its output .export.lock across staging and later publication. Caller
also owns source stability checks and total-store budget. This helper limits each write,
checks the published surface binding twice, and never replaces an immutable object.
"""
from pathlib import Path
import hashlib
import os
import shutil
import stat

import collision_bundle as cb
import collision_tile as ct
import collision_region_probe as probe
import world_hires as wh


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_dir():
        raise ValueError('store must be an existing absolute real directory')
    return path


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('immutable object must be a regular non-symlink file')
    return info


def _immutable(path, payload):
    if path.exists() or path.is_symlink():
        info = _regular(path)
        if info.st_size != len(payload) or probe._read_bounded(path, len(payload), 'existing object') != payload:
            raise ValueError('existing immutable object differs')
    else:
        cb._immutable(path, payload)


def stage_pair(store, hires_store, volume, mesh, *, max_new_bytes=8 * 1024**2,
               min_free_bytes=24 * 1024**3, disk_free=lambda p: shutil.disk_usage(p).free, read_index=None):
    read_index = probe.read_visible if read_index is None else read_index
    store, hires_store = _directory(store), _directory(hires_store)
    if store == hires_store or store.is_relative_to(hires_store) or hires_store.is_relative_to(store):
        raise ValueError('collision and geometry stores must be separate')
    core = volume.core
    if core.width != 64 or core.depth != 64 or core.x % 64 or core.z % 64:
        raise ValueError('pair requires an aligned 64x64 core')
    key = f'{core.x // 64},{core.z // 64}'
    before = read_index(hires_store)
    if key in before['stale'] or key not in before['tiles']:
        raise ValueError('tile is not a current published surface tile')
    geometry = wh.encode_tile(core.x, core.z, [d for _, d in sorted(mesh['chunks'].items())])
    if geometry is None or hashlib.sha256(geometry).hexdigest() != before['tiles'][key]:
        raise ValueError('mesh differs from published geometry')
    geometry_sha = before['tiles'][key]
    raw = ct.encode_raw(volume, core.x, core.z)
    blocks_sha = ct.identity(raw)
    blocks = ct.compressed(raw)
    pair = dict(format=cb.PAIR_FORMAT, x=core.x, z=core.z, atlasSha256=before['atlasSha256'],
                geometrySha256=geometry_sha, blocksSha256=blocks_sha)
    pair_raw = cb.canonical(pair)
    pair_sha = hashlib.sha256(pair_raw).hexdigest()
    # Conservative reservation includes existing objects; it never underestimates this write.
    reserved = len(blocks) + len(pair_raw) + 4096
    if reserved > max_new_bytes or disk_free(store) - reserved < min_free_bytes:
        raise ValueError('pair write exceeds budget or free-space floor')
    for name in ('objects', 'pairs'):
        folder = store / name
        folder.mkdir(exist_ok=True)
        _directory(folder)
    source = hires_store / 'objects' / (geometry_sha + '.uih')
    source_info = _regular(source)
    if probe._read_bounded(source, cb.MAX_OBJECT_BYTES, 'published geometry') != geometry:
        raise ValueError('published geometry object differs from its reference')
    target = store / 'objects' / source.name
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        _regular(target)
        if probe._read_bounded(target, cb.MAX_OBJECT_BYTES, 'staged geometry') != geometry:
            raise ValueError('existing staged geometry is corrupt')
    else:
        target_info = _regular(target)
        if (target_info.st_dev, target_info.st_ino) != (source_info.st_dev, source_info.st_ino):
            raise ValueError('geometry source changed while linking')
        cb._fsync_directory(target.parent)
    if probe._read_bounded(target, cb.MAX_OBJECT_BYTES, 'linked geometry') != geometry:
        raise ValueError('geometry changed while linking')
    _immutable(store / 'objects' / (blocks_sha + '.uib'), blocks)
    after = read_index(hires_store)
    if (after['tiles'].get(key) != geometry_sha or key in after['stale']
            or after['atlasSha256'] != before['atlasSha256'] or after['identity'] != before['identity']):
        raise ValueError('published surface changed while staging')
    # Pair is the last staged object. Orphans on failure are not referenced by any new index.
    _immutable(store / 'pairs' / (pair_sha + '.json'), pair_raw)
    cb._fsync_directory(store / 'objects'); cb._fsync_directory(store / 'pairs')
    return dict(pair, pairSha256=pair_sha, blockGzipBytes=len(blocks), geometryBytes=len(geometry),
                geometryShared=os.path.samefile(source, target), indexSha256=after['indexSha256'])

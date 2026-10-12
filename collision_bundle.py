"""Publish an immutable geometry/block pair from the SAME decoded Volume, to a private candidate store.

This is the publication unit for future walking data, not a live world exporter. The caller supplies
the already rendered mesh result for that volume. Index references can only be emitted after both
objects exist. Geometry is UIH1 gzip; collision identity hashes uncompressed UIB1.

publish_index(store, pairs) publishes which pair covers which 64 x 64 tile:
  indexes/<sha256>.json  immutable, canonical JSON {"format":"UIE_COLLISION_INDEX1","pairs":{"tx,tz":pairSha},
                         "tileSize":64}; the name is the SHA-256 of exactly these bytes
  current.json           mutable pointer {"format":"UIE_COLLISION_POINTER1","indexSha256":...}, replaced atomically
Every referenced pair and object is verified before anything is written; the index is written before the pointer,
so a failed publication leaves the previous pointer untouched. The atlas is only checked for identity syntax: it
belongs to the external visible renderer and is not required in this store.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import zlib

import collision_tile as ct
import world_hires as wh

INDEX_FORMAT = 'UIE_COLLISION_INDEX1'
POINTER_FORMAT = 'UIE_COLLISION_POINTER1'
PAIR_FORMAT = 'UIE_COLLISION_PAIR1'
TILE = 64
MAX_TILE_COORD = 30_000_000 // TILE        # Minecraft world border, in tiles
# Worst entry '"-468750,-468750":"<64 hex>",' is 85 bytes: 90 000 entries always fit in 8 MiB.
MAX_INDEX_ENTRIES = 90_000
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_PAIR_BYTES = 16384                     # same limit as the HTTP endpoint
MAX_OBJECT_BYTES = 64 * 1024 * 1024        # same limit as the HTTP endpoint
MAX_UIB_RAW = ct.HEADER.size + ct.MAX_REGISTRY + ct.COUNT * 3
HEX64 = re.compile(r'[0-9a-f]{64}')
TILE_KEY = re.compile(r'(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})')


def canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()


def _immutable(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError('immutable object differs: ' + str(path))
        return
    fd, temporary = tempfile.mkstemp(prefix='.object-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        # Hard-link publication cannot overwrite a concurrent writer's object.
        try: os.link(temporary,path)
        except FileExistsError:
            if path.read_bytes() != payload: raise ValueError('concurrent immutable object mismatch')
    finally:
        os.unlink(temporary)


def publish_pair(store, volume, mesh_result, atlas_sha256):
    if len(atlas_sha256) != 64 or any(c not in '0123456789abcdef' for c in atlas_sha256):
        raise ValueError('invalid atlas identity')
    core = volume.core
    if core.width != 64 or core.depth != 64:
        raise ValueError('pair requires a 64x64 core')
    blocks = ct.encode_raw(volume,core.x,core.z)
    docs = [doc for _,doc in sorted(mesh_result['chunks'].items())]
    geometry = wh.encode_tile(core.x,core.z,docs)
    # An empty rendered tile still needs known air/block information; represent no geometry explicitly.
    geometry_sha = hashlib.sha256(geometry).hexdigest() if geometry else None
    blocks_sha = ct.identity(blocks)
    record = dict(format='UIE_COLLISION_PAIR1',x=core.x,z=core.z,atlasSha256=atlas_sha256,
                  geometrySha256=geometry_sha,blocksSha256=blocks_sha)
    store=Path(store)
    _immutable(store/'objects'/f'{blocks_sha}.uib',ct.compressed(blocks))
    if geometry:
        _immutable(store/'objects'/f'{geometry_sha}.uih',geometry)
    raw=json.dumps(record,sort_keys=True,separators=(',',':')).encode()
    digest=hashlib.sha256(raw).hexdigest()
    _immutable(store/'pairs'/f'{digest}.json',raw)
    return dict(record,pairSha256=digest)


# ------------------------------------------------------------------ index

def _read_object(store, folder, name, limit):
    """Bytes of store/folder/name, refusing symlink escapes, non-files and oversized files."""
    base = store.resolve(strict=True)
    try:
        path = (store / folder / name).resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(f'missing {folder}/{name}') from None
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError(f'invalid {folder}/{name}')
    with open(path, 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{folder}/{name} is too large')
    return data


def _gunzip(data, limit, label):
    """Decompressed gzip member, at most `limit` bytes (no decompression bomb)."""
    try:
        inflater = zlib.decompressobj(wbits=31)
        raw = inflater.decompress(data, limit + 1)
    except zlib.error:
        raise ValueError(f'{label} is not valid gzip') from None
    if len(raw) > limit:
        raise ValueError(f'{label} is too large')
    return raw, inflater


def _verify_blocks(store, sha, x, z):
    data = _read_object(store, 'objects', f'{sha}.uib', MAX_OBJECT_BYTES)
    raw, inflater = _gunzip(data, MAX_UIB_RAW, f'objects/{sha}.uib')
    if not inflater.eof or inflater.unused_data:
        raise ValueError(f'objects/{sha}.uib is truncated or has trailing data')
    if ct.identity(raw) != sha:
        raise ValueError(f'objects/{sha}.uib content does not match its identity')
    if len(raw) < ct.HEADER.size:
        raise ValueError(f'objects/{sha}.uib has no UIB1 header')
    magic, bx, bz, _present, reserved, registry_len = ct.HEADER.unpack_from(raw)
    if magic != b'UIB1' or reserved or (bx, bz) != (x, z) or len(raw) != ct.HEADER.size + registry_len + ct.COUNT * 3:
        raise ValueError(f'objects/{sha}.uib is not the UIB1 tile at {x},{z}')


def _verify_geometry(store, sha, x, z):
    data = _read_object(store, 'objects', f'{sha}.uih', MAX_OBJECT_BYTES)
    if hashlib.sha256(data).hexdigest() != sha:    # UIH identity hashes the compressed bytes (world_hires)
        raise ValueError(f'objects/{sha}.uih content does not match its identity')
    # This is a prefix read, not a maximum size for the whole geometry payload.
    # _gunzip's strict size guard is appropriate for UIB validation but rejects every nonempty UIH here.
    try:
        head = zlib.decompressobj(wbits=31).decompress(data, wh.HEADER.size)
    except zlib.error:
        raise ValueError(f'objects/{sha}.uih is not valid gzip') from None
    if len(head) < wh.HEADER.size:
        raise ValueError(f'objects/{sha}.uih has no UIH1 header')
    magic, version, _flags, gx, gz, _opaque, _translucent = wh.HEADER.unpack_from(head)
    if magic != wh.MAGIC or version != 1 or (gx, gz) != (x, z):
        raise ValueError(f'objects/{sha}.uih is not the UIH1 tile at {x},{z}')


def _verify_pair(store, key, pair_sha):
    m = TILE_KEY.fullmatch(key) if isinstance(key, str) else None
    if not m:
        raise ValueError(f'invalid tile key {key!r}')
    tx, tz = int(m.group(1)), int(m.group(2))
    if abs(tx) > MAX_TILE_COORD or abs(tz) > MAX_TILE_COORD:
        raise ValueError(f'tile key {key} is outside the world')
    if not isinstance(pair_sha, str) or not HEX64.fullmatch(pair_sha):
        raise ValueError(f'invalid pair hash for {key}')
    raw = _read_object(store, 'pairs', f'{pair_sha}.json', MAX_PAIR_BYTES)
    if hashlib.sha256(raw).hexdigest() != pair_sha:
        raise ValueError(f'pairs/{pair_sha}.json content does not match its hash')
    try:
        pair = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f'pairs/{pair_sha}.json is not JSON') from None
    fields = {'format', 'x', 'z', 'atlasSha256', 'geometrySha256', 'blocksSha256'}
    if not isinstance(pair, dict) or set(pair) != fields or pair['format'] != PAIR_FORMAT or canonical(pair) != raw:
        raise ValueError(f'pairs/{pair_sha}.json is not a canonical {PAIR_FORMAT}')
    x, z = pair['x'], pair['z']
    if type(x) is not int or type(z) is not int or (x, z) != (tx * TILE, tz * TILE):
        raise ValueError(f'pair {pair_sha} covers {x},{z}, not tile {key}')
    if not isinstance(pair['atlasSha256'], str) or not HEX64.fullmatch(pair['atlasSha256']):
        raise ValueError(f'pair {pair_sha} has an invalid atlas identity')
    blocks, geometry = pair['blocksSha256'], pair['geometrySha256']
    if not isinstance(blocks, str) or not HEX64.fullmatch(blocks):
        raise ValueError(f'pair {pair_sha} has an invalid blocks identity')
    if geometry is not None and (not isinstance(geometry, str) or not HEX64.fullmatch(geometry)):
        raise ValueError(f'pair {pair_sha} has an invalid geometry identity')
    _verify_blocks(store, blocks, x, z)
    if geometry is not None:                    # null = a rendered tile without geometry (known, not missing)
        _verify_geometry(store, geometry, x, z)
    return f'{tx},{tz}'


def _replace_atomic(path, payload):
    fd, temporary = tempfile.mkstemp(prefix='.pointer-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _fsync_directory(path):
    """Persist a completed rename on POSIX; Windows has no portable directory fsync."""
    if os.name != 'posix':
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


GC_ENABLED_MARKER = '.uie-collision-gc-enabled'     # opt-in, next to the store (store.parent); absent = no hook


def _gc_enabled(store: Path) -> bool:
    try:
        info = os.lstat(store.parent / GC_ENABLED_MARKER)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('GC enabled marker is not a regular file; publication refused')
    return True


def _current_index(store: Path):
    """indexSha256 of current.json, None without a pointer; anything unreadable refuses (GC-enabled stores only)."""
    path = store / 'current.json'
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(raw)
        if len(raw) > 4096 or doc.get('format') != POINTER_FORMAT or not HEX64.fullmatch(doc.get('indexSha256')):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('current.json unreadable; publication refused while GC is enabled') from None
    return doc['indexSha256']


def publish_index(store, pairs, *, now=None):
    """Verify every pair of {'tx,tz': pairSha256}, write the immutable index, then point current.json at it.

    Returns {'indexSha256', 'entries'}. Raises ValueError (nothing written, pointer unchanged) on any invalid key,
    hash, pair or object. Callers that want to keep previously good tiles pass their old pair hashes again.

    Only when store.parent contains GC_ENABLED_MARKER (opt-in; production has none): before current.json is
    replaced, collision_gc.record_publication protects the old and the new index in store.parent's publication
    journal; a journal failure refuses the publication and keeps the old pointer. The caller holds the same
    exclusive export lock as for the rest of the publication. Without the marker nothing changes."""
    store = Path(store)
    if not isinstance(pairs, dict):
        raise ValueError('pairs must be a dict of "tx,tz" -> pair hash')
    if len(pairs) > MAX_INDEX_ENTRIES:
        raise ValueError(f'index exceeds {MAX_INDEX_ENTRIES} entries')
    if not store.is_dir():
        raise ValueError('collision store does not exist')
    checked = {}
    for key, pair_sha in pairs.items():
        checked[_verify_pair(store, key, pair_sha)] = pair_sha
    raw = canonical({'format': INDEX_FORMAT, 'tileSize': TILE, 'pairs': checked})
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError('index exceeds 8 MiB')
    digest = hashlib.sha256(raw).hexdigest()
    _immutable(store / 'indexes' / f'{digest}.json', raw)
    gc_enabled = _gc_enabled(store)
    if gc_enabled:
        old = _current_index(store)
        import collision_gc   # lazy: collision_gc imports this module (through collision_retention)
        try:
            collision_gc.record_publication(store.parent, old, digest, time.time() if now is None else now)
        except Exception as error:
            raise ValueError(f'GC publication journal not updated, publication refused: {error}') from error
    _replace_atomic(store / 'current.json', canonical({'format': POINTER_FORMAT, 'indexSha256': digest}))
    if gc_enabled:
        _fsync_directory(store)
    return {'indexSha256': digest, 'entries': len(checked)}

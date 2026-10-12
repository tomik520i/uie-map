"""UIB1: lossless 64x64x256 block sidecar, independent of rendering textures.

Uncompressed: <4siiHHI> magic, origin x/z, present-chunk mask, reserved=0,
registry JSON byte length; registry UTF-8 JSON; column-major little-endian uint16
IDs then uint8 metadata. Gzip is transport only; identity hashes uncompressed bytes.
Missing chunks are unknown, never inferred as air. Export is not wired into LIVE yet.
"""
from array import array
import gzip
import hashlib
import json
import struct
import sys

SIZE = 64
HEIGHT = 256
COUNT = SIZE * SIZE * HEIGHT
HEADER = struct.Struct('<4siiHHI')
MAX_REGISTRY = 1024 * 1024


def encode_raw(volume, x0, z0):
    if x0 % SIZE or z0 % SIZE:
        raise ValueError('collision tile origin must align to 64 blocks')
    ids = array('H', [0]) * COUNT
    metas = bytearray(COUNT)
    present = 0
    used = {0}
    for cz in range(4):
        for cx in range(4):
            if (x0 // 16 + cx, z0 // 16 + cz) in volume.present:
                present |= 1 << (cz * 4 + cx)
    for z in range(SIZE):
        for x in range(SIZE):
            if not present & (1 << ((z // 16) * 4 + x // 16)):
                continue
            col = volume.column_index(x0 + x, z0 + z)
            if col < 0:
                raise ValueError('present mask contradicts voxel window')
            start = (z * SIZE + x) * HEIGHT
            source = col * HEIGHT
            values = volume.ids[source:source + HEIGHT]
            metadata = volume.metas[source:source + HEIGHT]
            if len(values) != HEIGHT or len(metadata) != HEIGHT or max(metadata) > 15:
                raise ValueError('invalid voxel column')
            ids[start:start + HEIGHT] = values
            metas[start:start + HEIGHT] = metadata
            used.update(values)
    registry = {}
    for block_id in sorted(used):
        name = volume.registry.get(block_id)
        if not isinstance(name, str) or not name:
            raise ValueError(f'missing registry name for {block_id}')
        registry[str(block_id)] = name
    registry_bytes = json.dumps(registry, ensure_ascii=False, separators=(',', ':')).encode()
    if len(registry_bytes) > MAX_REGISTRY:
        raise ValueError('registry too large')
    if sys.byteorder != 'little':
        ids.byteswap()
    return HEADER.pack(b'UIB1', x0, z0, present, 0, len(registry_bytes)) + registry_bytes + ids.tobytes() + metas


def compressed(raw):
    data = gzip.compress(raw, compresslevel=6, mtime=0)
    return data[:9] + b'\xff' + data[10:]


def identity(raw):
    return hashlib.sha256(raw).hexdigest()


def decode_raw(raw):
    if len(raw) < HEADER.size:
        raise ValueError('truncated UIB1 header')
    magic, x, z, present, reserved, registry_len = HEADER.unpack_from(raw)
    if magic != b'UIB1' or reserved or x % SIZE or z % SIZE:
        raise ValueError('invalid UIB1 header')
    if not 1 <= registry_len <= MAX_REGISTRY or len(raw) != HEADER.size + registry_len + COUNT * 3:
        raise ValueError('invalid UIB1 length')
    registry = json.loads(raw[HEADER.size:HEADER.size + registry_len])
    if not isinstance(registry, dict):
        raise ValueError('invalid registry')
    for key, name in registry.items():
        if not key.isascii() or not key.isdecimal() or str(int(key)) != key or not 0 <= int(key) <= 65535:
            raise ValueError('invalid registry ID')
        if not isinstance(name, str) or not name:
            raise ValueError('invalid registry name')
    offset = HEADER.size + registry_len
    ids = array('H'); ids.frombytes(raw[offset:offset + COUNT * 2])
    if sys.byteorder != 'little':
        ids.byteswap()
    metas = raw[offset + COUNT * 2:]
    if max(metas) > 15 or any(str(i) not in registry for i in set(ids)):
        raise ValueError('invalid voxel value')
    return {'x': x, 'z': z, 'present': present, 'registry': registry, 'ids': ids, 'metas': metas}


def block_at(tile, x, y, z):
    dx, dz = x - tile['x'], z - tile['z']
    if not 0 <= dx < SIZE or not 0 <= dz < SIZE or not 0 <= y < HEIGHT:
        return None
    if not tile['present'] & (1 << ((dz // 16) * 4 + dx // 16)):
        return None
    i = (dz * SIZE + dx) * HEIGHT + y
    block_id = tile['ids'][i]
    return block_id, tile['metas'][i], tile['registry'][str(block_id)]

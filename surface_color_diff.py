"""Pure, bounded comparison of two gzip UIH1 payloads of the SAME tile (rollout gate G5, diagnostic only).

    compare_tiles(key, old_bytes, new_bytes) -> JSON-safe dict

No file, network or process access. Both payloads are first validated by hires_bundle._verify_tile (bounded streaming
inflate, magic/version/flags, origin == tile key, no truncation or trailing bytes, exact length for the header counts),
then inflated again with the same bounds. Arrays are compared as exact little-endian bytes (quantized positions,
UVs, colours), never decoded into Python floats.

Classification ("class"):
  identical        the compressed payloads are byte-identical;
  compression-only the decompressed UIH1 bytes are identical (only the gzip stream differs);
  colors-only      same header counts, identical position and UV bytes in both groups; only colour bytes differ;
  geometry-or-uv   quad counts per group, positions or UVs differ (structural change).
Invalid input raises SurfaceDiffError (nothing is classified).

This is a READ-ONLY diagnostic for a rollout gate. It never approves anything: a "colors-only" tile is still a
different geometry revision, and collision pairs remain bound to the FULL geometry SHA-256.
"""
from __future__ import annotations

import hashlib
import zlib

import hires_bundle as hb
import world_hires as wh

FORMAT = 'UIE_SURFACE_COLOR_DIFF1'
POSITION_BYTES = 24        # per quad: 4 vertices x 3 uint16
UV_BYTES = 16              # per quad: 4 vertices x 2 uint16
COLOR_BYTES = 12           # per quad: 4 vertices x 3 uint8
QUAD_BYTES = POSITION_BYTES + UV_BYTES + COLOR_BYTES
CHUNK = 4096


class SurfaceDiffError(ValueError):
    """Invalid or unsupported input; no classification."""


def _inflate(data: bytes, limit: int) -> bytes:
    """Whole gzip member, at most `limit` bytes of output, no trailing data (same loop as hires_bundle)."""
    inflater = zlib.decompressobj(wbits=31)
    out = bytearray()
    try:
        for offset in range(0, len(data), 65536):
            block = data[offset:offset + 65536]
            while block:
                out += inflater.decompress(block, min(65536, limit - len(out) + 1))
                if len(out) > limit:
                    raise SurfaceDiffError('UIH1 decompressed size exceeds limit')
                block = inflater.unconsumed_tail
    except zlib.error as error:
        raise SurfaceDiffError(f'invalid UIH1 gzip: {error}') from None
    if not inflater.eof or inflater.unused_data:
        raise SurfaceDiffError('UIH1 gzip is truncated or has trailing bytes')
    return bytes(out)


def _parse(key: str, data, what: str, max_compressed: int, max_raw: int) -> dict:
    if not isinstance(data, (bytes, bytearray)):
        raise SurfaceDiffError(f'{what} payload must be bytes')
    if not 1 <= len(data) <= max_compressed:
        raise SurfaceDiffError(f'{what} payload size outside 1..{max_compressed} bytes')
    data = bytes(data)
    try:
        hb._verify_tile(data, key)                        # bounded validation before our own inflate
    except hb.BundleError as error:
        raise SurfaceDiffError(f'{what} payload invalid: {error}') from None
    raw = _inflate(data, max_raw)
    magic, version, flags, x, z, opaque, translucent = wh.HEADER.unpack_from(raw)
    if len(raw) != wh.HEADER.size + (opaque + translucent) * QUAD_BYTES:
        raise SurfaceDiffError(f'{what} payload length does not match its quad counts')
    groups, offset = [], wh.HEADER.size
    for quads in (opaque, translucent):
        view = memoryview(raw)
        positions = view[offset:offset + quads * POSITION_BYTES]; offset += quads * POSITION_BYTES
        uvs = view[offset:offset + quads * UV_BYTES]; offset += quads * UV_BYTES
        colors = view[offset:offset + quads * COLOR_BYTES]; offset += quads * COLOR_BYTES
        groups.append({'quads': quads, 'positions': positions, 'uvs': uvs, 'colors': colors})
    return {'sha256': hashlib.sha256(data).hexdigest(), 'rawSha256': hashlib.sha256(raw).hexdigest(),
            'raw': raw, 'origin': [x, z], 'counts': [opaque, translucent], 'groups': groups}


def _differing_bytes(a, b) -> int:
    """Count differing bytes of equal-length buffers; Python iteration only inside differing 4 KiB chunks."""
    count = 0
    for start in range(0, len(a), CHUNK):
        x, y = a[start:start + CHUNK], b[start:start + CHUNK]
        if x != y:
            count += sum(1 for p, q in zip(x, y) if p != q)
    return count


def compare_tiles(key: str, old: bytes, new: bytes, *, max_compressed: int = hb.MAX_OBJECT_BYTES,
                  max_raw: int = hb.MAX_UIH_RAW) -> dict:
    """Classify the difference between two UIH1 payloads of tile `key` ("tx,tz"). See the module documentation."""
    for name, value in (('max_compressed', max_compressed), ('max_raw', max_raw)):
        if type(value) is not int or value < 1:
            raise SurfaceDiffError(f'{name} must be a positive integer')
    try:
        hb._tile_coords(key)
    except hb.BundleError as error:
        raise SurfaceDiffError(str(error)) from None
    a = _parse(key, old, 'old', max_compressed, max_raw)
    b = _parse(key, new, 'new', max_compressed, max_raw)
    result = {'format': FORMAT, 'key': key, 'diagnosticOnly': True,
              'old': {'sha256': a['sha256'], 'rawSha256': a['rawSha256'], 'counts': a['counts']},
              'new': {'sha256': b['sha256'], 'rawSha256': b['rawSha256'], 'counts': b['counts']},
              'positionsEqual': None, 'uvsEqual': None, 'colorsEqual': None, 'differingColorBytes': None,
              'reason': None}
    if a['sha256'] == b['sha256'] and bytes(old) == bytes(new):
        result.update({'class': 'identical', 'positionsEqual': True, 'uvsEqual': True, 'colorsEqual': True,
                       'differingColorBytes': 0})
        return result
    if a['raw'] == b['raw']:
        result.update({'class': 'compression-only', 'positionsEqual': True, 'uvsEqual': True, 'colorsEqual': True,
                       'differingColorBytes': 0})
        return result
    if a['counts'] != b['counts']:
        result.update({'class': 'geometry-or-uv', 'reason': 'quad counts per group differ'})
        return result
    positions = all(x['positions'] == y['positions'] for x, y in zip(a['groups'], b['groups']))
    uvs = all(x['uvs'] == y['uvs'] for x, y in zip(a['groups'], b['groups']))
    result.update(positionsEqual=positions, uvsEqual=uvs)
    if not positions or not uvs:
        result.update({'class': 'geometry-or-uv',
                       'reason': 'position bytes differ' if not positions else 'uv bytes differ'})
        return result
    differing = sum(_differing_bytes(x['colors'], y['colors']) for x, y in zip(a['groups'], b['groups']))
    result.update({'class': 'colors-only', 'colorsEqual': differing == 0, 'differingColorBytes': differing})
    return result

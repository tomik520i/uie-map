"""UIE2 batch snapshot codec: a UIE1-style dense snapshot plus a render core and a chunk presence mask.

Pure codec, no I/O. A building block for tiled rendering (see render_plan.py), not a world renderer.

Wire format (big endian):
    b"UIE2"
    int32 x, z, width, depth, registry_count          input window, same meaning as UIE1
    int32 core_x, core_z, core_width, core_depth      area whose geometry is published
    int32 presence_count                              == (width/16) * (depth/16)
    presence_count * uint8 flag (0 absent, 1 present) chunks of the input window, z-major then x
    registry_count * (int32 block_id, uint16 length, Java modified UTF-8 name)   sorted by id
    width*depth columns, z-major then x: uint8 biome, 256 * (uint16 block_id, uint8 meta)

Invariants, checked by encode_batch, parse_batch and from_chunks:
    - input and core are aligned to 16 blocks (x, z, width, depth) and fit int32; input is at most
      128x128; core lies inside input with a halo of at least 16 blocks on all four sides.
    - the mask is authoritative. Every column of an absent chunk is all zero bytes and is NOT air;
      column() refuses it. At least one core chunk is present.
    - registry: 1..65536 entries, ids 0..65535, non-empty names, and id 0 must be "minecraft:air"
      so that zero bytes inside present chunks keep their Anvil meaning.
    - present columns: meta 0..15 and every block id in the registry.
    - no trailing bytes.

API:
    BatchSnapshot(area, core, registry, body, present_chunks)   frozen dataclass
        .present_at(x, z) -> bool      False outside the input window or in an absent chunk
        .column(x, z)                  (biome, [(id, meta)] * 256); BatchError for absent chunks
    encode_batch(snapshot) -> bytes
    parse_batch(data) -> BatchSnapshot
    from_chunks(area, core, registry, chunks) -> BatchSnapshot   dense columns from reader.Chunk objects;
        chunks missing from the mapping are absent (zero padding), never invented as air.
    BatchError(cs.SnapshotError)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Mapping

import capture_snapshot as cs
import reader

MAGIC = b"UIE2"
CHUNK = 16
AIR_NAME = "minecraft:air"
_AREA = struct.Struct(">5i")
_CORE = struct.Struct(">4i")
_COUNT = struct.Struct(">i")
_ENTRY = struct.Struct(">iH")
_ZERO_COLUMN = bytes(cs.COLUMN_BYTES)


class BatchError(cs.SnapshotError):
    """The batch snapshot violates the UIE2 contract."""


@dataclass(frozen=True)
class BatchSnapshot:
    area: cs.Area
    core: cs.Area
    registry: dict
    body: bytes
    present_chunks: frozenset

    def present_at(self, x: int, z: int) -> bool:
        a = self.area
        if not (a.x <= x < a.x + a.width and a.z <= z < a.z + a.depth):
            return False
        return (x >> 4, z >> 4) in self.present_chunks

    def column(self, x: int, z: int) -> tuple[int, list[tuple[int, int]]]:
        if not self.present_at(x, z):
            raise BatchError(f"column ({x},{z}) is outside the input window or in an absent chunk")
        return cs.Snapshot(self.area, self.registry, self.body).column(x, z)


def _chunk_window(area: cs.Area) -> list[tuple[int, int]]:
    """Chunk coords of the input window, z-major then x (the mask order)."""
    return [(cx, cz) for cz in range(area.z >> 4, (area.z + area.depth) >> 4)
            for cx in range(area.x >> 4, (area.x + area.width) >> 4)]


def _validate_areas(area, core) -> None:
    for label, a in (("area", area), ("core", core)):
        if not isinstance(a, cs.Area):
            raise BatchError(f"{label} must be a capture_snapshot.Area")
        if any(v % CHUNK for v in (a.x, a.z, a.width, a.depth)):
            raise BatchError(f"{label} {a} is not aligned to 16 blocks")
    for start, span, cstart, cspan, axis in ((area.x, area.width, core.x, core.width, "x"),
                                             (area.z, area.depth, core.z, core.depth, "z")):
        if cstart - start < CHUNK or (start + span) - (cstart + cspan) < CHUNK:
            raise BatchError(f"core must lie inside the input with a halo of at least 16 blocks along {axis}")


def _validate_registry(registry) -> None:
    if not isinstance(registry, dict) or not 1 <= len(registry) <= cs.MAX_REGISTRY_ENTRIES:
        raise BatchError(f"registry must be a dict with 1..{cs.MAX_REGISTRY_ENTRIES} entries")
    for block_id, name in registry.items():
        if not isinstance(block_id, int) or isinstance(block_id, bool) or not 0 <= block_id <= reader.MAX_BLOCK_ID:
            raise BatchError(f"invalid registry id {block_id!r}")
        if not isinstance(name, str) or not name:
            raise BatchError(f"registry id {block_id} has no name")
        cs.encode_mutf8(name)  # length limit
    if registry.get(0) != AIR_NAME:
        raise BatchError(f"registry id 0 must be {AIR_NAME!r}")


def _validate(s: BatchSnapshot) -> None:
    _validate_areas(s.area, s.core)
    _validate_registry(s.registry)
    area, width = s.area, s.area.width
    if not isinstance(s.body, bytes) or len(s.body) != width * area.depth * cs.COLUMN_BYTES:
        raise BatchError(f"body must be {width * area.depth * cs.COLUMN_BYTES} bytes")
    window = _chunk_window(area)
    if (not isinstance(s.present_chunks, frozenset)
            or any(not isinstance(c, tuple) or len(c) != 2
                   or any(not isinstance(v, int) or isinstance(v, bool) for v in c) for c in s.present_chunks)
            or not s.present_chunks <= set(window)):
        raise BatchError("present_chunks must be a frozenset of chunk coords inside the input window")
    core_chunks = {(cx, cz) for cx, cz in window
                   if s.core.x <= cx * CHUNK < s.core.x + s.core.width and s.core.z <= cz * CHUNK < s.core.z + s.core.depth}
    if not s.present_chunks & core_chunks:
        raise BatchError("no chunk inside the core is present")
    known = set(s.registry)
    body = memoryview(s.body)
    for z in range(area.z, area.z + area.depth):
        row = (z - area.z) * width
        for x in range(area.x, area.x + width):
            pos = (row + x - area.x) * cs.COLUMN_BYTES
            col = body[pos:pos + cs.COLUMN_BYTES]
            if (x >> 4, z >> 4) not in s.present_chunks:
                if col != _ZERO_COLUMN:
                    raise BatchError(f"column ({x},{z}) of an absent chunk is not zero")
                continue
            raw = col.tobytes()
            if max(raw[3::3]) > 15:
                raise BatchError(f"column ({x},{z}) has metadata above 15")
            ids = {(hi << 8) | lo for hi, lo in set(zip(raw[1::3], raw[2::3]))}
            unknown = ids - known
            if unknown:
                raise BatchError(f"column ({x},{z}) uses block ids missing from the registry: {sorted(unknown)[:10]}")


def encode_batch(snapshot: BatchSnapshot) -> bytes:
    _validate(snapshot)
    a, c = snapshot.area, snapshot.core
    entries = sorted(snapshot.registry.items())
    parts = [MAGIC, _AREA.pack(a.x, a.z, a.width, a.depth, len(entries)), _CORE.pack(c.x, c.z, c.width, c.depth)]
    window = _chunk_window(a)
    parts += [_COUNT.pack(len(window)), bytes(1 if chunk in snapshot.present_chunks else 0 for chunk in window)]
    for block_id, name in entries:
        encoded = cs.encode_mutf8(name)
        parts += [_ENTRY.pack(block_id, len(encoded)), encoded]
    parts.append(snapshot.body)
    return b"".join(parts)


def parse_batch(data: bytes) -> BatchSnapshot:
    if not isinstance(data, (bytes, bytearray)):
        raise BatchError("data must be bytes")
    data = bytes(data)
    pos = 0

    def take(n: int, what: str) -> bytes:
        nonlocal pos
        if pos + n > len(data):
            raise BatchError(f"UIE2 data truncated in {what}")
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    if take(4, "magic") != MAGIC:
        raise BatchError("not a UIE2 batch snapshot")
    x, z, width, depth, count = _AREA.unpack(take(_AREA.size, "header"))
    cx, cz, cw, cd = _CORE.unpack(take(_CORE.size, "core"))
    try:
        area, core = cs.Area(x, z, width, depth), cs.Area(cx, cz, cw, cd)
    except cs.SnapshotError as exc:
        raise BatchError(f"invalid area: {exc}") from None
    _validate_areas(area, core)
    if not 1 <= count <= cs.MAX_REGISTRY_ENTRIES:
        raise BatchError(f"registry count {count} outside 1..{cs.MAX_REGISTRY_ENTRIES}")
    window = _chunk_window(area)
    (presence_count,) = _COUNT.unpack(take(_COUNT.size, "presence count"))
    if presence_count != len(window):
        raise BatchError(f"presence_count {presence_count} does not match the {len(window)} chunks of the input")
    flags = take(presence_count, "presence flags")
    if any(f > 1 for f in flags):
        raise BatchError("presence flags must be 0 or 1")
    registry = {}
    for _ in range(count):
        block_id, length = _ENTRY.unpack(take(_ENTRY.size, "registry"))
        if not 0 <= block_id <= reader.MAX_BLOCK_ID or block_id in registry:
            raise BatchError(f"invalid or duplicate registry id {block_id}")
        try:
            registry[block_id] = cs.decode_mutf8(take(length, "registry name"))
        except cs.SnapshotError as exc:
            raise BatchError(str(exc)) from None
    body = take(width * depth * cs.COLUMN_BYTES, "column data")
    if pos != len(data):
        raise BatchError(f"{len(data) - pos} trailing bytes after the column data")
    snapshot = BatchSnapshot(area, core, registry, body,
                             frozenset(chunk for chunk, flag in zip(window, flags) if flag))
    _validate(snapshot)
    return snapshot


def from_chunks(area: cs.Area, core: cs.Area, registry: Mapping[int, str],
                chunks: Mapping[tuple[int, int], reader.Chunk]) -> BatchSnapshot:
    """Dense batch from decoded chunks; window chunks missing from `chunks` stay absent (zero padding)."""
    _validate_areas(area, core)
    registry = dict(registry)
    _validate_registry(registry)
    window = set(_chunk_window(area))
    body = bytearray(area.width * area.depth * cs.COLUMN_BYTES)
    row_bytes = CHUNK * cs.COLUMN_BYTES
    for key, chunk in chunks.items():
        if (not isinstance(key, tuple) or len(key) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in key) or key not in window):
            raise BatchError(f"chunk key {key!r} is not a chunk of the input window")
        if not isinstance(chunk, reader.Chunk) or (chunk.x, chunk.z) != key:
            raise BatchError(f"chunk for {key} reports position ({getattr(chunk, 'x', None)},{getattr(chunk, 'z', None)})")
        if not isinstance(chunk.biomes, bytes) or len(chunk.biomes) != 256:
            raise BatchError(f"chunk {key} has no 256-byte Biomes array")
        columns, first_seen = cs.build_columns(cs.Area(key[0] * CHUNK, key[1] * CHUNK, CHUNK, CHUNK), {key: chunk})
        unknown = sorted(i for i in first_seen if i not in registry)
        if unknown:
            raise BatchError(f"chunk {key} uses block ids missing from the registry: "
                             + ", ".join(f"{i} at {first_seen[i]}" for i in unknown[:10]))
        for lz in range(CHUNK):
            dest = ((key[1] * CHUNK + lz - area.z) * area.width + key[0] * CHUNK - area.x) * cs.COLUMN_BYTES
            body[dest:dest + row_bytes] = columns[lz * row_bytes:(lz + 1) * row_bytes]
    snapshot = BatchSnapshot(area, core, registry, bytes(body), frozenset(chunks))
    _validate(snapshot)
    return snapshot

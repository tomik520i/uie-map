"""Read-only reader for legacy Anvil worlds (Minecraft Forge 1.7.10).

Covers NBT, gzip level.dat, the FML.ItemData ID registry, region (.mca) files
and pre-flattening chunk sections (Blocks/Add/Data, plus the NotEnoughIDs
Blocks16 array). Nothing in this module opens a file for writing. Formats newer
than 1.7.10 or other extended-ID storage are refused with UnsupportedFormatError
instead of being decoded incorrectly.
"""
from __future__ import annotations

import os
import re
import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO, Mapping

__all__ = [
    "FormatError", "UnsupportedFormatError", "Limits", "DEFAULT_LIMITS",
    "decompress", "parse_nbt", "parse_level_dat", "read_level_dat",
    "FmlRegistry", "parse_fml_registry", "Section", "Chunk", "Surface",
    "decode_chunk", "ChunkLocation", "RegionFile", "region_coords_from_name",
    "chunk_to_region",
]

LEGACY_ANVIL_VERSION = 19133
# NotEnoughIDs stores one 16-bit block ID per voxel, so the registry may exceed 4095.
MAX_BLOCK_ID = 0xFFFF
BLOCK_PREFIX = "\x01"
ITEM_PREFIX = "\x02"
SECTOR_BYTES = 4096
COMPRESSION_GZIP = 1
COMPRESSION_ZLIB = 2

# Section tags of extended metadata or post-1.12 storage that this reader does not
# decode; ignoring them would silently return wrong blocks or metadata.
UNSUPPORTED_SECTION_KEYS = frozenset({"Data16", "Palette", "BlockStates", "block_states"})

(TAG_END, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG, TAG_FLOAT, TAG_DOUBLE, TAG_BYTE_ARRAY,
 TAG_STRING, TAG_LIST, TAG_COMPOUND, TAG_INT_ARRAY, TAG_LONG_ARRAY) = range(13)


class FormatError(ValueError):
    """Input is corrupt, truncated or exceeds a configured limit."""


class UnsupportedFormatError(FormatError):
    """Input is well-formed but uses a format this legacy reader does not decode."""


@dataclass(frozen=True)
class Limits:
    max_depth: int = 256
    max_nodes: int = 2_000_000
    max_list_len: int = 1_000_000
    max_array_bytes: int = 16 * 1024 * 1024
    max_compressed: int = 16 * 1024 * 1024
    max_decompressed: int = 32 * 1024 * 1024


DEFAULT_LIMITS = Limits()


def decompress(data: bytes, compression: int, limits: Limits = DEFAULT_LIMITS) -> bytes:
    """Inflate one complete gzip (1) or zlib (2) stream with an explicit output cap."""
    if compression == COMPRESSION_GZIP:
        wbits = 16 + zlib.MAX_WBITS
    elif compression == COMPRESSION_ZLIB:
        wbits = zlib.MAX_WBITS
    else:
        raise FormatError(f"unknown compression type {compression}")
    if len(data) > limits.max_compressed:
        raise FormatError(f"compressed input of {len(data)} bytes exceeds limit {limits.max_compressed}")
    inflater = zlib.decompressobj(wbits)
    try:
        out = inflater.decompress(data, limits.max_decompressed + 1)
    except zlib.error as exc:
        raise FormatError(f"corrupt compressed stream: {exc}") from None
    if len(out) > limits.max_decompressed or inflater.unconsumed_tail:
        raise FormatError(f"decompressed size exceeds limit {limits.max_decompressed}")
    if not inflater.eof:
        raise FormatError("compressed stream is truncated")
    if inflater.unused_data:
        raise FormatError("unexpected data after end of compressed stream")
    return out


# ---------------------------------------------------------------- NBT

_U8, _U16, _I32 = struct.Struct(">B"), struct.Struct(">H"), struct.Struct(">i")
_SCALARS = {
    TAG_BYTE: struct.Struct(">b"), TAG_SHORT: struct.Struct(">h"), TAG_INT: _I32,
    TAG_LONG: struct.Struct(">q"), TAG_FLOAT: struct.Struct(">f"), TAG_DOUBLE: struct.Struct(">d"),
}
_NUM_ARRAYS = {TAG_INT_ARRAY: (4, "i"), TAG_LONG_ARRAY: (8, "q")}


def _decode_mutf8(raw: bytes) -> str:
    """Decode Java modified UTF-8 (C0 80 for NUL, CESU-8 surrogate pairs)."""
    if raw.isascii():
        return raw.decode("ascii")
    try:
        text = raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError:
        raise FormatError(f"invalid modified UTF-8 string {raw[:32]!r}") from None


class _NbtParser:
    def __init__(self, data: bytes, limits: Limits) -> None:
        self.buf = memoryview(data)
        self.pos = 0
        self.limits = limits
        self.nodes = 0

    def take(self, n: int) -> memoryview:
        end = self.pos + n
        if end > len(self.buf):
            raise FormatError(f"NBT truncated at offset {self.pos}: need {n} bytes, "
                              f"{len(self.buf) - self.pos} left")
        view = self.buf[self.pos:end]
        self.pos = end
        return view

    def num(self, st: struct.Struct):
        return st.unpack(self.take(st.size))[0]

    def string(self) -> str:
        return _decode_mutf8(bytes(self.take(self.num(_U16))))

    def array_len(self, item_size: int) -> int:
        n = self.num(_I32)
        if n < 0:
            raise FormatError(f"negative NBT array length {n}")
        if n * item_size > self.limits.max_array_bytes:
            raise FormatError(f"NBT array of {n * item_size} bytes exceeds limit {self.limits.max_array_bytes}")
        return n

    def payload(self, tag: int, depth: int):
        self.nodes += 1
        if self.nodes > self.limits.max_nodes:
            raise FormatError(f"NBT exceeds node limit {self.limits.max_nodes}")
        if depth > self.limits.max_depth:
            raise FormatError(f"NBT exceeds nesting limit {self.limits.max_depth}")
        if tag in _SCALARS:
            return self.num(_SCALARS[tag])
        if tag == TAG_BYTE_ARRAY:
            return bytes(self.take(self.array_len(1)))
        if tag == TAG_STRING:
            return self.string()
        if tag in _NUM_ARRAYS:
            size, code = _NUM_ARRAYS[tag]
            n = self.array_len(size)
            return list(struct.unpack(f">{n}{code}", self.take(n * size)))
        if tag == TAG_LIST:
            item = self.num(_U8)
            n = self.num(_I32)
            if n < 0 or n > self.limits.max_list_len:
                raise FormatError(f"NBT list length {n} outside 0..{self.limits.max_list_len}")
            if item > TAG_LONG_ARRAY or (item == TAG_END and n > 0):
                raise FormatError(f"invalid NBT list item type {item} for {n} items")
            items = []
            for _ in range(n):
                items.append(self.payload(item, depth + 1))
            return items
        if tag == TAG_COMPOUND:
            out = {}
            while True:
                child = self.num(_U8)
                if child == TAG_END:
                    return out
                if child > TAG_LONG_ARRAY:
                    raise FormatError(f"invalid NBT tag type {child} at offset {self.pos - 1}")
                name = self.string()
                out[name] = self.payload(child, depth + 1)
        raise FormatError(f"invalid NBT tag type {tag}")


def parse_nbt(data: bytes, limits: Limits = DEFAULT_LIMITS) -> tuple[str, dict]:
    """Parse uncompressed NBT whose root is a compound; returns (root name, dict).

    Mapping: numeric tags -> int/float, byte array -> bytes, int/long array ->
    list[int], string -> str, list -> list, compound -> dict. Trailing bytes
    after the root are rejected.
    """
    parser = _NbtParser(data, limits)
    tag = parser.num(_U8)
    if tag != TAG_COMPOUND:
        raise FormatError(f"NBT root must be a compound, got tag type {tag}")
    name = parser.string()
    root = parser.payload(TAG_COMPOUND, 0)
    if parser.pos != len(parser.buf):
        raise FormatError(f"{len(parser.buf) - parser.pos} trailing bytes after NBT root")
    return name, root


# ---------------------------------------------------------------- level.dat / FML

def parse_level_dat(raw: bytes, limits: Limits = DEFAULT_LIMITS) -> dict:
    """Decode gzip level.dat bytes, require legacy Anvil storage, return the root compound."""
    _, root = parse_nbt(decompress(raw, COMPRESSION_GZIP, limits), limits)
    data = root.get("Data")
    if not isinstance(data, dict):
        raise FormatError("level.dat has no Data compound")
    if "DataVersion" in data:
        raise UnsupportedFormatError(f"level.dat has DataVersion {data['DataVersion']!r} "
                                     "(Minecraft 1.9+); only 1.7.10 worlds are supported")
    if data.get("version") != LEGACY_ANVIL_VERSION:
        raise UnsupportedFormatError(f"level.dat storage version {data.get('version')!r} "
                                     f"is not Anvil {LEGACY_ANVIL_VERSION}")
    return root


def read_level_dat(path: str | os.PathLike, limits: Limits = DEFAULT_LIMITS) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read(limits.max_compressed + 1)
    return parse_level_dat(raw, limits)


@dataclass(frozen=True)
class FmlRegistry:
    """Numeric ID -> registry name tables from FML.ItemData, split by entry prefix."""
    blocks: Mapping[int, str]
    items: Mapping[int, str]
    skipped_entries: int = 0

    def block_name(self, block_id: int) -> str | None:
        return self.blocks.get(block_id)


def parse_fml_registry(level_root: Mapping) -> FmlRegistry:
    """Read FML.ItemData ({K: "\\x01name" block | "\\x02name" item, V: id}) from level.dat."""
    fml = level_root.get("FML")
    if not isinstance(fml, dict):
        raise FormatError("level.dat has no FML compound; not a Forge 1.7.10 world")
    entries = fml.get("ItemData")
    if entries is None:
        if "Registries" in fml:
            raise UnsupportedFormatError("FML.Registries layout (Forge 1.8+) is not supported")
        raise FormatError("FML compound has no ItemData list")
    if not isinstance(entries, list):
        raise FormatError("FML.ItemData is not a list")
    blocks: dict[int, str] = {}
    items: dict[int, str] = {}
    skipped = 0
    for entry in entries:
        key = entry.get("K") if isinstance(entry, dict) else None
        value = entry.get("V") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not isinstance(value, int) or len(key) < 2:
            raise FormatError(f"malformed FML.ItemData entry {entry!r:.120}")
        prefix, name = key[0], key[1:]
        if prefix == BLOCK_PREFIX:
            if not 0 <= value <= MAX_BLOCK_ID:
                raise UnsupportedFormatError(f"block {name!r} has ID {value} outside 0..{MAX_BLOCK_ID}")
            table = blocks
        elif prefix == ITEM_PREFIX:
            if value < 0:
                raise FormatError(f"item {name!r} has negative ID {value}")
            table = items
        else:
            skipped += 1
            continue
        previous = table.get(value)
        if previous is not None and previous != name:
            raise FormatError(f"ID {value} assigned to both {previous!r} and {name!r}")
        table[value] = name
    return FmlRegistry(blocks, items, skipped)


# ---------------------------------------------------------------- chunks

def _nibble(arr: bytes, index: int) -> int:
    byte = arr[index >> 1]
    return byte >> 4 if index & 1 else byte & 0x0F


def _local_index(x: int, y: int, z: int) -> int:
    if not (0 <= x < 16 and 0 <= y < 16 and 0 <= z < 16):
        raise IndexError(f"local coordinates ({x},{y},{z}) outside 0..15")
    return (y << 8) | (z << 4) | x


@dataclass(frozen=True)
class Section:
    """16x16x16 slice; arrays are YZX ordered (index = y*256 + z*16 + x), even index = low nibble.

    blocks16 (NotEnoughIDs, big-endian u16 per voxel) takes precedence over blocks/add.
    """
    y: int
    blocks: bytes | None
    data: bytes
    add: bytes | None = None
    blocks16: bytes | None = None

    def _id_at(self, i: int) -> int:
        if self.blocks16 is not None:
            return (self.blocks16[2 * i] << 8) | self.blocks16[2 * i + 1]
        if self.add is None:
            return self.blocks[i]
        return self.blocks[i] | (_nibble(self.add, i) << 8)

    def block_id(self, x: int, y: int, z: int) -> int:
        return self._id_at(_local_index(x, y, z))

    def meta(self, x: int, y: int, z: int) -> int:
        return _nibble(self.data, _local_index(x, y, z))


@dataclass(frozen=True)
class Surface:
    """Topmost non-air block per column; lists indexed z*16 + x, height -1 = empty column."""
    chunk_x: int
    chunk_z: int
    heights: tuple[int, ...]
    ids: tuple[int, ...]
    metas: tuple[int, ...]

    def at(self, x: int, z: int) -> tuple[int, int, int] | None:
        """Return (y, block_id, meta) or None for a column without any non-air block."""
        if not (0 <= x < 16 and 0 <= z < 16):
            raise IndexError(f"column ({x},{z}) outside 0..15")
        col = (z << 4) | x
        if self.heights[col] < 0:
            return None
        return self.heights[col], self.ids[col], self.metas[col]


@dataclass(frozen=True)
class Chunk:
    x: int
    z: int
    sections: Mapping[int, Section]
    biomes: bytes | None = None

    def block(self, x: int, y: int, z: int) -> tuple[int, int]:
        """Return (block_id, meta) at local x/z 0..15 and world y 0..255; missing sections are air."""
        if not 0 <= y < 256:
            raise IndexError(f"y={y} outside 0..255")
        i = _local_index(x, y & 15, z)
        section = self.sections.get(y >> 4)
        if section is None:
            return 0, 0
        return section._id_at(i), _nibble(section.data, i)

    def surface(self) -> Surface:
        """Topmost block with ID != 0 per column. No opacity judgement: that needs block metadata
        from the asset pipeline, so glass, leaves or modded non-solid blocks count as surface."""
        heights, ids, metas = [-1] * 256, [0] * 256, [0] * 256
        pending = 256
        for sy in sorted(self.sections, reverse=True):
            section = self.sections[sy]
            for ly in range(15, -1, -1):
                base = ly << 8
                for col in range(256):
                    if heights[col] >= 0:
                        continue
                    i = base | col
                    block_id = section._id_at(i)
                    if block_id == 0:
                        continue
                    heights[col], ids[col], metas[col] = (sy << 4) | ly, block_id, _nibble(section.data, i)
                    pending -= 1
                if pending == 0:
                    break
            if pending == 0:
                break
        return Surface(self.x, self.z, tuple(heights), tuple(ids), tuple(metas))


def _byte_array(raw: Mapping, key: str, size: int, required: bool) -> bytes | None:
    value = raw.get(key)
    if value is None:
        if required:
            raise FormatError(f"section is missing {key}")
        return None
    if not isinstance(value, bytes) or len(value) != size:
        raise FormatError(f"section {key} must be a {size}-byte array, got {type(value).__name__}"
                          f"[{len(value) if hasattr(value, '__len__') else '?'}]")
    return value


def _decode_section(raw) -> Section:
    if not isinstance(raw, dict):
        raise FormatError("section is not a compound")
    unsupported = UNSUPPORTED_SECTION_KEYS.intersection(raw)
    if unsupported:
        raise UnsupportedFormatError(f"section uses {sorted(unsupported)}: extended-metadata or "
                                     "post-1.12 block storage is not supported")
    y = raw.get("Y")
    if not isinstance(y, int):
        raise FormatError("section has no integer Y")
    if not 0 <= y <= 15:
        raise UnsupportedFormatError(f"section Y={y} outside 0..15 (tall/cubic world mod?)")
    # NotEnoughIDs reads Blocks16 when present and ignores Blocks/Add; legacy arrays
    # are still validated if written alongside it.
    blocks16 = _byte_array(raw, "Blocks16", 8192, False)
    return Section(y, _byte_array(raw, "Blocks", 4096, blocks16 is None), _byte_array(raw, "Data", 2048, True),
                   _byte_array(raw, "Add", 2048, False), blocks16)


def decode_chunk(root: Mapping) -> Chunk:
    """Decode a parsed 1.7.10 chunk root compound ({"Level": {...}})."""
    if "DataVersion" in root:
        raise UnsupportedFormatError(f"chunk has DataVersion {root['DataVersion']!r} (Minecraft 1.9+)")
    level = root.get("Level")
    if not isinstance(level, dict):
        raise FormatError("chunk has no Level compound")
    cx, cz = level.get("xPos"), level.get("zPos")
    if not isinstance(cx, int) or not isinstance(cz, int):
        raise FormatError("chunk has no integer xPos/zPos")
    raw_sections = level.get("Sections", [])
    if not isinstance(raw_sections, list):
        raise FormatError("chunk Sections is not a list")
    sections: dict[int, Section] = {}
    for raw in raw_sections:
        section = _decode_section(raw)
        if section.y in sections:
            raise FormatError(f"duplicate section Y={section.y}")
        sections[section.y] = section
    biomes = level.get("Biomes")
    if isinstance(biomes, list):
        raise UnsupportedFormatError("int-array Biomes (Minecraft 1.13+) are not supported")
    if biomes is not None and not (isinstance(biomes, bytes) and len(biomes) == 256):
        raise FormatError("chunk Biomes must be a 256-byte array")
    return Chunk(cx, cz, sections, biomes)


# ---------------------------------------------------------------- regions

_REGION_NAME = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca")


def region_coords_from_name(path: str | os.PathLike) -> tuple[int, int]:
    match = _REGION_NAME.fullmatch(os.path.basename(os.fspath(path)))
    if match is None:
        raise FormatError(f"not a region file name: {os.fspath(path)!r}")
    return int(match.group(1)), int(match.group(2))


def chunk_to_region(chunk_x: int, chunk_z: int) -> tuple[int, int, int, int]:
    """Absolute chunk coords -> (region_x, region_z, local_x, local_z), floor semantics for negatives."""
    return chunk_x >> 5, chunk_z >> 5, chunk_x & 31, chunk_z & 31


@dataclass(frozen=True)
class ChunkLocation:
    local_x: int
    local_z: int
    sector_offset: int
    sector_count: int
    timestamp: int


class RegionFile:
    """Read-only view of one r.X.Z.mca file; accepts a path or a readable binary file object.

    Header entries are validated lazily per chunk, so one corrupt chunk does not hide the others.
    """

    def __init__(self, source: str | os.PathLike | BinaryIO, region_x: int | None = None,
                 region_z: int | None = None, limits: Limits = DEFAULT_LIMITS) -> None:
        is_path = isinstance(source, (str, os.PathLike))
        if region_x is None or region_z is None:
            if not is_path:
                raise ValueError("region_x and region_z are required for file objects")
            region_x, region_z = region_coords_from_name(source)
        self.region_x, self.region_z, self.limits = region_x, region_z, limits
        self._fh: BinaryIO = open(source, "rb") if is_path else source
        self._owns = is_path
        try:
            self._locations = self._read_header()
        except BaseException:
            self.close()
            raise

    def _read_header(self) -> dict[tuple[int, int], ChunkLocation]:
        self._fh.seek(0, os.SEEK_END)
        self._size = self._fh.tell()
        if self._size == 0:
            return {}
        self._fh.seek(0)
        header = self._fh.read(2 * SECTOR_BYTES)
        if len(header) != 2 * SECTOR_BYTES:
            raise FormatError(f"region header truncated: file has {self._size} bytes")
        locations = {}
        for i in range(1024):
            (entry,) = struct.unpack_from(">I", header, i * 4)
            if entry == 0:
                continue
            (timestamp,) = struct.unpack_from(">i", header, SECTOR_BYTES + i * 4)
            loc = ChunkLocation(i & 31, i >> 5, entry >> 8, entry & 0xFF, timestamp)
            locations[(loc.local_x, loc.local_z)] = loc
        return locations

    def close(self) -> None:
        if self._owns:
            self._fh.close()

    def __enter__(self) -> RegionFile:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def locations(self) -> list[ChunkLocation]:
        return sorted(self._locations.values(), key=lambda loc: (loc.local_z, loc.local_x))

    def read_chunk_nbt(self, local_x: int, local_z: int) -> dict | None:
        """Return the parsed chunk root compound, or None if the chunk is absent."""
        if not (0 <= local_x < 32 and 0 <= local_z < 32):
            raise IndexError(f"local chunk ({local_x},{local_z}) outside 0..31")
        loc = self._locations.get((local_x, local_z))
        if loc is None:
            return None
        where = f"chunk ({local_x},{local_z}) of region ({self.region_x},{self.region_z})"
        if loc.sector_offset < 2:
            raise FormatError(f"{where}: sector offset {loc.sector_offset} overlaps header")
        if loc.sector_count == 0:
            raise FormatError(f"{where}: zero sector count")
        start = loc.sector_offset * SECTOR_BYTES
        self._fh.seek(start)
        head = self._fh.read(5)
        if len(head) != 5:
            raise FormatError(f"{where}: record at byte {start} is beyond end of file ({self._size})")
        length, compression = struct.unpack(">iB", head)
        if length < 1 or length + 4 > loc.sector_count * SECTOR_BYTES:
            raise FormatError(f"{where}: invalid length {length} for {loc.sector_count} sectors")
        if compression & 0x80:
            raise UnsupportedFormatError(f"{where}: external .mcc chunk storage is not supported")
        if compression == 3:
            raise UnsupportedFormatError(f"{where}: uncompressed chunks are not a 1.7.10 format")
        if compression not in (COMPRESSION_GZIP, COMPRESSION_ZLIB):
            raise FormatError(f"{where}: unknown compression type {compression}")
        payload = self._fh.read(length - 1)
        if len(payload) != length - 1:
            raise FormatError(f"{where}: truncated, expected {length - 1} bytes, got {len(payload)}")
        try:
            _, root = parse_nbt(decompress(payload, compression, self.limits), self.limits)
        except FormatError as exc:
            raise type(exc)(f"{where}: {exc}") from None
        return root

    def read_chunk(self, local_x: int, local_z: int) -> Chunk | None:
        """Decode a chunk and verify its xPos/zPos matches its slot in this region."""
        root = self.read_chunk_nbt(local_x, local_z)
        if root is None:
            return None
        chunk = decode_chunk(root)
        expected = (self.region_x * 32 + local_x, self.region_z * 32 + local_z)
        if (chunk.x, chunk.z) != expected:
            raise FormatError(f"chunk in slot ({local_x},{local_z}) of region ({self.region_x},"
                              f"{self.region_z}) claims position ({chunk.x},{chunk.z}), expected {expected}")
        return chunk

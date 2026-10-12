"""Build a UIE1 capture snapshot for GeometryCapture from an OFFLINE copy of a Forge 1.7.10 world.

Inputs are only read: a copy of level.dat (FML block registry) and the region directory
holding r.X.Z.mca files. Every chunk covering the requested area must exist; a missing
region file, chunk or Biomes array is an error instead of being painted as air, and every
block ID in the area must be present in the registry before anything is published.

Output layout (big endian), consumed by GeometryCapture.read():
    b"UIE1", int32 x, int32 z, int32 width, int32 depth, int32 registry_count
    registry_count * (int32 block_id, uint16 length, Java modified UTF-8 name)
    width*depth columns, z-major then x: uint8 biome, 256 * (uint16 block_id, uint8 meta)

The binary is written to a temporary file in the output directory, read back and validated,
the sources are re-hashed (the run fails if level.dat or a used region changed while being
read), and only then are <output>.provenance.json and <output> moved into place with os.replace.

Example:
    python capture_snapshot.py --level-dat copy/level.dat --region-dir copy/region \\
        --x 192 --z 192 --width 64 --depth 64 --output client-instance/uie-capture.bin
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import reader

MAGIC = b"UIE1"
MAX_SPAN = 128
MAX_REGISTRY_ENTRIES = 65536
WORLD_HEIGHT = 256
COLUMN_BYTES = 1 + WORLD_HEIGHT * 3
INT32_MIN, INT32_MAX = -(2 ** 31), 2 ** 31 - 1
_HEADER = struct.Struct(">5i")
_ENTRY = struct.Struct(">iH")
_AIR_SECTION_COLUMN = bytes(16 * 3)


class SnapshotError(Exception):
    """The snapshot cannot be produced safely; nothing has been published."""


@dataclass(frozen=True)
class Area:
    """Block-coordinate rectangle: X in [x, x+width), Z in [z, z+depth), all 256 Y levels."""
    x: int
    z: int
    width: int
    depth: int

    def __post_init__(self) -> None:
        for name in ("x", "z", "width", "depth"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise SnapshotError(f"{name} must be an integer, got {value!r}")
        for name in ("width", "depth"):
            if not 1 <= getattr(self, name) <= MAX_SPAN:
                raise SnapshotError(f"{name} must be 1..{MAX_SPAN}, got {getattr(self, name)}")
        for start, span, label in ((self.x, self.width, "x"), (self.z, self.depth, "z")):
            if start < INT32_MIN or start + span - 1 > INT32_MAX:
                raise SnapshotError(f"{label} range {start}..{start + span - 1} does not fit int32")


@dataclass(frozen=True)
class Snapshot:
    area: Area
    registry: dict[int, str]
    body: bytes

    def column(self, x: int, z: int) -> tuple[int, list[tuple[int, int]]]:
        """Return (biome, [(block_id, meta)] * 256) for world coordinates inside the area."""
        a = self.area
        if not (a.x <= x < a.x + a.width and a.z <= z < a.z + a.depth):
            raise IndexError(f"column ({x},{z}) outside snapshot area")
        pos = ((z - a.z) * a.width + (x - a.x)) * COLUMN_BYTES
        col = self.body[pos:pos + COLUMN_BYTES]
        return col[0], [((col[i] << 8) | col[i + 1], col[i + 2]) for i in range(1, COLUMN_BYTES, 3)]


# ---------------------------------------------------------------- Java modified UTF-8

def encode_mutf8(text: str) -> bytes:
    """Encode like java.io.DataOutput.writeUTF (without the length prefix)."""
    units = text.encode("utf-16-be", "surrogatepass")
    out = bytearray()
    for i in range(0, len(units), 2):
        u = (units[i] << 8) | units[i + 1]
        if 0 < u < 0x80:
            out.append(u)
        elif u < 0x800:
            out += bytes((0xC0 | (u >> 6), 0x80 | (u & 0x3F)))
        else:
            out += bytes((0xE0 | (u >> 12), 0x80 | ((u >> 6) & 0x3F), 0x80 | (u & 0x3F)))
    if len(out) > 0xFFFF:
        raise SnapshotError(f"name of {len(out)} encoded bytes exceeds the 65535-byte writeUTF limit")
    return bytes(out)


def decode_mutf8(raw: bytes) -> str:
    """Strict inverse of encode_mutf8; rejects raw NUL, 4-byte UTF-8 and overlong forms."""
    try:
        text = raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    except UnicodeError:
        raise SnapshotError(f"invalid modified UTF-8 name {raw[:40]!r}") from None
    if encode_mutf8(text) != raw:
        raise SnapshotError(f"non-canonical modified UTF-8 name {raw[:40]!r}")
    return text


# ---------------------------------------------------------------- planning and reading

def plan_chunks(area: Area) -> dict[tuple[int, int], list[tuple[int, int, int, int]]]:
    """Group the chunks covering the area by region: {(rx, rz): [(cx, cz, local_x, local_z)]}."""
    plan: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
    for cz in range(area.z >> 4, ((area.z + area.depth - 1) >> 4) + 1):
        for cx in range(area.x >> 4, ((area.x + area.width - 1) >> 4) + 1):
            rx, rz, lx, lz = reader.chunk_to_region(cx, cz)
            plan.setdefault((rx, rz), []).append((cx, cz, lx, lz))
    return plan


def region_path(region_dir: Path, rx: int, rz: int) -> Path:
    return region_dir / f"r.{rx}.{rz}.mca"


def sha256_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def provenance_path(output: Path) -> Path:
    return output.with_name(output.name + ".provenance.json")


def _check_outputs(output: Path, inputs: list[Path], region_dir: Path, force: bool) -> None:
    if not output.parent.is_dir():
        raise SnapshotError(f"output directory {output.parent} does not exist")
    resolved_inputs = {p.resolve() for p in inputs}
    region_root = region_dir.resolve()
    for target in (output, provenance_path(output)):
        if target in resolved_inputs or (target.exists() and any(
                p.exists() and os.path.samefile(target, p) for p in resolved_inputs)):
            raise SnapshotError(f"refusing to overwrite input file {target}")
        if target == region_root or region_root in target.parents:
            raise SnapshotError(f"refusing to write {target} inside the region directory {region_root}")
        if target.is_dir():
            raise SnapshotError(f"output path {target} is a directory")
        if target.exists() and not force:
            raise SnapshotError(f"{target} already exists; pass --force to replace it")


def _read_registry(level_dat: Path) -> tuple[reader.FmlRegistry, str, int]:
    with open(level_dat, "rb") as fh:
        raw = fh.read(reader.DEFAULT_LIMITS.max_compressed + 1)
    registry = reader.parse_fml_registry(reader.parse_level_dat(raw))
    return registry, hashlib.sha256(raw).hexdigest(), len(raw)


def _read_chunks(region_dir: Path, plan) -> tuple[dict[tuple[int, int], reader.Chunk], list[dict]]:
    missing = [region_path(region_dir, rx, rz).name for rx, rz in sorted(plan)
               if not region_path(region_dir, rx, rz).is_file()]
    if missing:
        raise SnapshotError(f"missing region file(s) in {region_dir}: {', '.join(missing)}")
    chunks: dict[tuple[int, int], reader.Chunk] = {}
    sources = []
    for (rx, rz), slots in sorted(plan.items()):
        path = region_path(region_dir, rx, rz)
        digest, size = sha256_file(path)
        with reader.RegionFile(path, rx, rz) as region:
            for cx, cz, lx, lz in slots:
                chunk = region.read_chunk(lx, lz)
                if chunk is None:
                    raise SnapshotError(f"chunk ({cx},{cz}) is not generated in {path.name} "
                                        f"(slot {lx},{lz}); refusing to paint it as air")
                if chunk.biomes is None:
                    raise SnapshotError(f"chunk ({cx},{cz}) in {path.name} has no Biomes array")
                chunks[(cx, cz)] = chunk
        sources.append({"path": str(path.resolve()), "region": [rx, rz], "sha256": digest,
                        "size": size, "chunksUsed": len(slots)})
    return chunks, sources


def build_columns(area: Area, chunks: Mapping[tuple[int, int], reader.Chunk]) -> tuple[bytes, dict]:
    """Return (column bytes, {block_id: first (x, y, z)}) in UIE1 order."""
    body = bytearray()
    first_seen: dict[int, tuple[int, int, int]] = {}
    for z in range(area.z, area.z + area.depth):
        for x in range(area.x, area.x + area.width):
            chunk = chunks[(x >> 4, z >> 4)]
            lx, lz = x & 15, z & 15
            body.append(chunk.biomes[(lz << 4) | lx])
            for sy in range(16):
                section = chunk.sections.get(sy)
                if section is None:  # absent section = air by Anvil definition, not missing data
                    body += _AIR_SECTION_COLUMN
                    continue
                for ly in range(16):
                    block_id, meta = section.block_id(lx, ly, lz), section.meta(lx, ly, lz)
                    body.extend((block_id >> 8, block_id & 0xFF, meta))
                    if block_id not in first_seen:
                        first_seen[block_id] = (x, (sy << 4) | ly, z)
    return bytes(body), first_seen


# ---------------------------------------------------------------- encoding

def encode_snapshot(area: Area, blocks: Mapping[int, str], body: bytes) -> bytes:
    entries = sorted(blocks.items())
    if not 1 <= len(entries) <= MAX_REGISTRY_ENTRIES:
        raise SnapshotError(f"registry must have 1..{MAX_REGISTRY_ENTRIES} block entries, got {len(entries)}")
    if len(body) != area.width * area.depth * COLUMN_BYTES:
        raise SnapshotError(f"column data has {len(body)} bytes, expected {area.width * area.depth * COLUMN_BYTES}")
    parts = [MAGIC, _HEADER.pack(area.x, area.z, area.width, area.depth, len(entries))]
    for block_id, name in entries:
        if not 0 <= block_id <= reader.MAX_BLOCK_ID or not name:
            raise SnapshotError(f"invalid registry entry {block_id}: {name!r}")
        encoded = encode_mutf8(name)
        parts += [_ENTRY.pack(block_id, len(encoded)), encoded]
    parts.append(body)
    return b"".join(parts)


def parse_snapshot(data: bytes) -> Snapshot:
    """Parse and validate a UIE1 snapshot exactly as GeometryCapture expects it."""
    if len(data) < 4 + _HEADER.size or data[:4] != MAGIC:
        raise SnapshotError("not a UIE1 snapshot")
    x, z, width, depth, count = _HEADER.unpack_from(data, 4)
    area = Area(x, z, width, depth)
    if not 1 <= count <= MAX_REGISTRY_ENTRIES:
        raise SnapshotError(f"registry count {count} outside 1..{MAX_REGISTRY_ENTRIES}")
    pos, registry = 4 + _HEADER.size, {}
    for _ in range(count):
        if pos + _ENTRY.size > len(data):
            raise SnapshotError("snapshot truncated in registry")
        block_id, length = _ENTRY.unpack_from(data, pos)
        pos += _ENTRY.size
        if not 0 <= block_id <= reader.MAX_BLOCK_ID or block_id in registry or pos + length > len(data):
            raise SnapshotError(f"invalid, duplicate or truncated registry entry for ID {block_id}")
        registry[block_id] = decode_mutf8(data[pos:pos + length])
        pos += length
    body = data[pos:]
    if len(body) != width * depth * COLUMN_BYTES:
        raise SnapshotError(f"column data has {len(body)} bytes, expected {width * depth * COLUMN_BYTES}")
    return Snapshot(area, registry, body)


# ---------------------------------------------------------------- publication

def _write_temp(directory: Path, name: str, data: bytes) -> Path:
    fd, temp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        os.unlink(temp)
        raise
    return Path(temp)


def create_snapshot(level_dat: Path, region_dir: Path, area: Area, output: Path, *, force: bool = False) -> dict:
    """Read the offline world copy, publish <output> and its provenance, return the provenance."""
    level_dat, region_dir, output = Path(level_dat), Path(region_dir), Path(output).resolve()
    if not level_dat.is_file():
        raise SnapshotError(f"level.dat not found: {level_dat}")
    if not region_dir.is_dir():
        raise SnapshotError(f"region directory not found: {region_dir}")
    plan = plan_chunks(area)
    region_files = [region_path(region_dir, rx, rz) for rx, rz in sorted(plan)]
    _check_outputs(output, [level_dat, *region_files], region_dir, force)

    registry, level_sha, level_size = _read_registry(level_dat)
    chunks, sources = _read_chunks(region_dir, plan)
    body, first_seen = build_columns(area, chunks)
    unknown = sorted(i for i in first_seen if i != 0 and i not in registry.blocks)
    if unknown:
        listed = ", ".join(f"{i} at {first_seen[i]}" for i in unknown[:20])
        raise SnapshotError(f"{len(unknown)} block ID(s) missing from the FML registry: {listed}")
    data = encode_snapshot(area, registry.blocks, body)

    prov_target = provenance_path(output)
    temps: list[Path] = []
    try:
        temps.append(_write_temp(output.parent, output.name, data))
        if temps[0].read_bytes() != data:
            raise SnapshotError("temporary snapshot does not read back identically")
        parse_snapshot(data)
        changed = [str(level_dat)] if sha256_file(level_dat) != (level_sha, level_size) else []
        changed += [s["path"] for s in sources if sha256_file(Path(s["path"])) != (s["sha256"], s["size"])]
        if changed:
            raise SnapshotError(f"source changed while reading, snapshot discarded: {', '.join(changed)}")
        cx0, cz0, cx1, cz1 = area.x >> 4, area.z >> 4, (area.x + area.width - 1) >> 4, (area.z + area.depth - 1) >> 4
        provenance = {
            "format": "UIE1",
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "area": {"x": area.x, "z": area.z, "width": area.width, "depth": area.depth, "y": [0, WORLD_HEIGHT - 1]},
            "chunks": {"min": [cx0, cz0], "max": [cx1, cz1], "count": len(chunks)},
            "levelDat": {"path": str(level_dat.resolve()), "sha256": level_sha, "size": level_size},
            "regions": sources,
            "registry": {"blockEntries": len(registry.blocks), "distinctIdsInArea": len(first_seen)},
            "output": {"path": str(output), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
        }
        temps.append(_write_temp(output.parent, prov_target.name,
                                 (json.dumps(provenance, indent=2) + "\n").encode("utf-8")))
        for target in (prov_target, output):
            if target.exists() and not force:
                raise SnapshotError(f"{target} appeared during the run; pass --force to replace it")
        # Provenance first: whenever the new binary is visible, its provenance already is.
        os.replace(temps[1], prov_target)
        temps.pop()
        os.replace(temps[0], output)
        temps.pop()
        return provenance
    finally:
        for temp in temps:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- CLI

def _span(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_SPAN:
        raise argparse.ArgumentTypeError(f"must be 1..{MAX_SPAN}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="capture_snapshot.py",
        description="Build a UIE1 snapshot for GeometryCapture from an offline copy of a Forge 1.7.10 world. "
                    "Inputs are only read; do not point it at a live server world.")
    parser.add_argument("--level-dat", type=Path, required=True, help="copy of the world's level.dat (FML registry)")
    parser.add_argument("--region-dir", type=Path, required=True, help="copy of the world's region/ directory")
    parser.add_argument("--x", type=int, required=True, help="minimum block X of the area")
    parser.add_argument("--z", type=int, required=True, help="minimum block Z of the area")
    parser.add_argument("--width", type=_span, required=True, help=f"blocks along X, 1..{MAX_SPAN}")
    parser.add_argument("--depth", type=_span, required=True, help=f"blocks along Z, 1..{MAX_SPAN}")
    parser.add_argument("--output", type=Path, required=True,
                        help="snapshot path; <output>.provenance.json is written next to it")
    parser.add_argument("--force", action="store_true", help="replace an existing output and provenance")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        provenance = create_snapshot(args.level_dat, args.region_dir, Area(args.x, args.z, args.width, args.depth),
                                     args.output, force=args.force)
    except (SnapshotError, reader.FormatError, OSError) as exc:
        print(f"capture_snapshot: error: {exc}", file=sys.stderr)
        return 1
    out = provenance["output"]
    print(f"wrote {out['path']} ({out['size']} bytes, {provenance['chunks']['count']} chunks, "
          f"{len(provenance['regions'])} region files, sha256 {out['sha256']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

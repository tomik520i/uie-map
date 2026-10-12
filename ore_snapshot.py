"""Extract GregTech ore tile data for a UIE1 snapshot area from an OFFLINE copy of the world.

GregTech ores are a single block (gregtech:gt.blockores) whose material lives in the
GT_TileEntity_Ores tile entity; without it the renderer produces no geometry. This tool reads
only those tile records, only inside the area of an existing UIE1 snapshot, and writes:

    {"format": "UIE_ORES1", "snapshotSha256": "<sha256 of the snapshot binary>",
     "tiles": [{"x": int, "y": int, "z": int, "m": int, "n": 0|1}, ...],
     "sources": [{"path": "<region file>", "sha256": "..."}, ...]}

Rules:
- every chunk covering the snapshot area must exist; a missing TileEntities list means "no tiles";
- other tile entities are skipped and never serialized (no inventories or other data);
- a selected record needs integer x/y/z inside its own chunk, 0 <= y <= 255, signed 16-bit m,
  n of 0 or 1, unique coordinates, and the snapshot block at that position must be exactly
  gregtech:gt.blockores;
- the snapshot and used region files are hashed before and after reading; any change fails the run;
- the output is written to a temporary file next to it and moved into place with os.replace;
  it never replaces an input, is never inside the region directory, and an existing output
  is only replaced with --force.

Example:
    python ore_snapshot.py --snapshot client-instance/uie-capture.bin --region-dir copy/region \\
        --output client-instance/uie-ores.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping

import capture_snapshot as cs
import reader

FORMAT = "UIE_ORES1"
GT_ORE_TILE_ID = "GT_TileEntity_Ores"
GT_ORE_BLOCK = "gregtech:gt.blockores"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class OreSnapshotError(Exception):
    """Ore data cannot be extracted safely; nothing has been published."""


def _int_field(record: Mapping, key: str, where: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise OreSnapshotError(f"{where}: {key} must be an integer, got {value!r}")
    return value


def select_ore_tiles(snapshot: cs.Snapshot, chunk_roots: Mapping[tuple[int, int], Mapping]) -> list[dict]:
    """Return validated GT ore tiles inside the snapshot area, sorted by (z, x, y)."""
    area = snapshot.area
    tiles: dict[tuple[int, int, int], dict] = {}
    for (cx, cz), root in sorted(chunk_roots.items()):
        entries = root["Level"].get("TileEntities", [])
        if not isinstance(entries, list):
            raise OreSnapshotError(f"chunk ({cx},{cz}): TileEntities is not a list")
        for i, record in enumerate(entries):
            where = f"chunk ({cx},{cz}) TileEntities[{i}]"
            if not isinstance(record, dict):
                raise OreSnapshotError(f"{where}: tile entity is not a compound")
            tile_id = record.get("id")
            if not isinstance(tile_id, str):
                raise OreSnapshotError(f"{where}: id must be a string, got {tile_id!r}")
            if tile_id != GT_ORE_TILE_ID:
                continue
            x, y, z = (_int_field(record, key, where) for key in ("x", "y", "z"))
            if (x >> 4, z >> 4) != (cx, cz):
                raise OreSnapshotError(f"{where}: position ({x},{y},{z}) lies outside its chunk")
            if not 0 <= y < cs.WORLD_HEIGHT:
                raise OreSnapshotError(f"{where}: y={y} outside 0..{cs.WORLD_HEIGHT - 1}")
            if not (area.x <= x < area.x + area.width and area.z <= z < area.z + area.depth):
                continue
            m = _int_field(record, "m", where)
            if not -32768 <= m <= 32767:
                raise OreSnapshotError(f"{where}: m={m} is not a signed 16-bit value")
            n = _int_field(record, "n", where)
            if n not in (0, 1):
                raise OreSnapshotError(f"{where}: n={n} must be 0 or 1")
            if (x, y, z) in tiles:
                raise OreSnapshotError(f"{where}: duplicate GT ore tile at ({x},{y},{z})")
            block_id = snapshot.column(x, z)[1][y][0]
            name = snapshot.registry.get(block_id)
            if name != GT_ORE_BLOCK:
                raise OreSnapshotError(f"{where}: snapshot block at ({x},{y},{z}) is {name!r} (ID {block_id}), "
                                       f"not {GT_ORE_BLOCK}")
            tiles[(x, y, z)] = {"x": x, "y": y, "z": z, "m": m, "n": n}
    return [tiles[key] for key in sorted(tiles, key=lambda p: (p[2], p[0], p[1]))]


def _read_chunk_roots(region_dir: Path, plan) -> tuple[dict[tuple[int, int], dict], list[dict]]:
    missing = [cs.region_path(region_dir, rx, rz).name for rx, rz in sorted(plan)
               if not cs.region_path(region_dir, rx, rz).is_file()]
    if missing:
        raise OreSnapshotError(f"missing region file(s) in {region_dir}: {', '.join(missing)}")
    roots: dict[tuple[int, int], dict] = {}
    sources = []
    for (rx, rz), slots in sorted(plan.items()):
        path = cs.region_path(region_dir, rx, rz)
        digest, size = cs.sha256_file(path)
        with reader.RegionFile(path, rx, rz) as region:
            for cx, cz, lx, lz in slots:
                root = region.read_chunk_nbt(lx, lz)
                if root is None:
                    raise OreSnapshotError(f"chunk ({cx},{cz}) is not generated in {path.name} (slot {lx},{lz})")
                chunk = reader.decode_chunk(root)  # validates the legacy chunk layout
                if (chunk.x, chunk.z) != (cx, cz):
                    raise OreSnapshotError(f"chunk in slot ({lx},{lz}) of {path.name} claims position "
                                           f"({chunk.x},{chunk.z}), expected ({cx},{cz})")
                roots[(cx, cz)] = root
        sources.append({"path": str(path.resolve()), "sha256": digest, "size": size})
    return roots, sources


def _check_output(output: Path, inputs: list[Path], region_dir: Path, force: bool) -> None:
    if not output.parent.is_dir():
        raise OreSnapshotError(f"output directory {output.parent} does not exist")
    region_root = region_dir.resolve()
    if output == region_root or region_root in output.parents:
        raise OreSnapshotError(f"refusing to write {output} inside the region directory {region_root}")
    for path in inputs:
        if output == path.resolve() or (output.exists() and path.exists() and os.path.samefile(output, path)):
            raise OreSnapshotError(f"refusing to overwrite input file {output}")
    if output.is_dir():
        raise OreSnapshotError(f"output path {output} is a directory")
    if output.exists() and not force:
        raise OreSnapshotError(f"{output} already exists; pass --force to replace it")


def create_ore_snapshot(snapshot_path: Path, region_dir: Path, output: Path, *, force: bool = False) -> dict:
    """Read the snapshot and region copies, publish the ore JSON, return its content."""
    snapshot_path, region_dir, output = Path(snapshot_path), Path(region_dir), Path(output).resolve()
    if not snapshot_path.is_file():
        raise OreSnapshotError(f"snapshot not found: {snapshot_path}")
    if not region_dir.is_dir():
        raise OreSnapshotError(f"region directory not found: {region_dir}")
    with open(snapshot_path, "rb") as fh:
        data = fh.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise OreSnapshotError(f"snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes")
    snapshot_sha = hashlib.sha256(data).hexdigest()
    snapshot = cs.parse_snapshot(data)
    plan = cs.plan_chunks(snapshot.area)
    region_files = [cs.region_path(region_dir, rx, rz) for rx, rz in sorted(plan)]
    _check_output(output, [snapshot_path, *region_files], region_dir, force)

    roots, sources = _read_chunk_roots(region_dir, plan)
    result = {
        "format": FORMAT,
        "snapshotSha256": snapshot_sha,
        "tiles": select_ore_tiles(snapshot, roots),
        "sources": [{"path": s["path"], "sha256": s["sha256"]} for s in sources],
    }
    payload = (json.dumps(result, indent=2) + "\n").encode("utf-8")

    fd, temp_name = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        changed = [str(snapshot_path)] if cs.sha256_file(snapshot_path) != (snapshot_sha, len(data)) else []
        changed += [s["path"] for s in sources if cs.sha256_file(Path(s["path"])) != (s["sha256"], s["size"])]
        if changed:
            raise OreSnapshotError(f"source changed while reading, output discarded: {', '.join(changed)}")
        if output.exists() and not force:
            raise OreSnapshotError(f"{output} appeared during the run; pass --force to replace it")
        os.replace(temp, output)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ore_snapshot.py",
        description="Extract GregTech ore tile data (GT_TileEntity_Ores) for the area of a UIE1 snapshot "
                    "from an offline copy of the world's region files. Inputs are only read.")
    parser.add_argument("--snapshot", type=Path, required=True, help="UIE1 snapshot produced by capture_snapshot.py")
    parser.add_argument("--region-dir", type=Path, required=True, help="copy of the world's region/ directory")
    parser.add_argument("--output", type=Path, required=True, help="UIE_ORES1 JSON file to write")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = create_ore_snapshot(args.snapshot, args.region_dir, args.output, force=args.force)
    except (OreSnapshotError, cs.SnapshotError, reader.FormatError, OSError) as exc:
        print(f"ore_snapshot: error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {Path(args.output).resolve()} ({len(result['tiles'])} GT ore tiles "
          f"from {len(result['sources'])} region files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

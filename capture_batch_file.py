"""Create a UIE2 batch (snapshot + GT ore sidecar + provenance) from an OFFLINE world copy.

Nothing is rendered and no input is written. For one planned tile the input window (core plus a
16-block halo) is read from a copy of level.dat and the region directory:

    - the FML block registry comes from level.dat and must map id 0 to minecraft:air;
    - every region file touched by the window is opened if it exists; a missing region file or an
      ungenerated chunk is recorded as ABSENT in the UIE2 presence mask (never invented as air);
      a region or chunk that exists but cannot be parsed is an error, not "missing";
    - present chunks are decoded and position-checked; they need a Biomes array and registered ids;
    - GT_TileEntity_Ores records of present chunks inside the window go to the UIE_ORES1 sidecar via
      ore_snapshot.select_ore_tiles; other tile entities are ignored and never serialized. Every GT ore
      block in the core must have its record (fail before publication); halo ores may lack one.
    - area/core alignment and the 16-block halo are checked before any input is read.

Output directory (must not exist; not inside the region directory):
    snapshot.bin          UIE2 (capture_batch)
    snapshot.tiles.json   {"format": "UIE_ORES1", "snapshotSha256", "tiles", "sources"}
    provenance.json       inputs, hashes, region states, present/absent chunks, output hashes
    COMPLETE              JSON marker, written last; only a directory with COMPLETE is a batch

Publication: everything is built and read back in a private temporary directory next to the target.
The sources are re-hashed (level.dat and used regions unchanged, absent regions still absent). Then
the target is reserved with an exclusive mkdir (an existing path is never reused or overwritten), the
files are moved in, re-verified, and COMPLETE is moved in last. On failure only this run's temporary
directory, the files it moved and the directory it created are removed.

Hash checks only detect changes during this run. They do not make a copy of a live world consistent;
use an offline, consistent copy.

CLI:
    python capture_batch_file.py --level-dat COPY/level.dat --region-dir COPY/region \\
        --tile-x -1 --tile-z 0 --output-dir batches/tile_-1_0
    Tile (tx, tz): core X [tx*96, tx*96+96), input X [tx*96-16, tx*96+112) (same for Z).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import capture_batch as cb
import capture_snapshot as cs
import ore_snapshot as ore
import reader

SNAPSHOT_FILE = "snapshot.bin"
TILES_FILE = "snapshot.tiles.json"
PROVENANCE_FILE = "provenance.json"
COMPLETE_FILE = "COMPLETE"
TILE_BLOCKS = 96
HALO_BLOCKS = 16
CONSISTENCY_NOTE = ("Hashes only detect changes while this batch was read; they are not a substitute "
                    "for a consistent offline copy of the world.")


class BatchFileError(cs.SnapshotError):
    """The batch cannot be created or published safely."""


def tile_areas(tile_x: int, tile_z: int) -> tuple[cs.Area, cs.Area]:
    """(input window, core) of a planned 6x6-chunk tile with a one-chunk halo."""
    for value in (tile_x, tile_z):
        if not isinstance(value, int) or isinstance(value, bool):
            raise BatchFileError("tile coordinates must be integers")
    core = cs.Area(tile_x * TILE_BLOCKS, tile_z * TILE_BLOCKS, TILE_BLOCKS, TILE_BLOCKS)
    area = cs.Area(core.x - HALO_BLOCKS, core.z - HALO_BLOCKS, TILE_BLOCKS + 2 * HALO_BLOCKS, TILE_BLOCKS + 2 * HALO_BLOCKS)
    return area, core


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _area_json(a: cs.Area) -> dict:
    return {"x": a.x, "z": a.z, "width": a.width, "depth": a.depth}


def _check_paths(level_dat: Path, region_dir: Path, output: Path) -> None:
    if not level_dat.is_file():
        raise BatchFileError(f"level.dat not found: {level_dat}")
    if not region_dir.is_dir():
        raise BatchFileError(f"region directory not found: {region_dir}")
    if os.path.lexists(output):
        raise BatchFileError(f"output {output} already exists; a batch is only written to a new directory")
    if not output.parent.is_dir():
        raise BatchFileError(f"parent directory {output.parent} does not exist")
    # Compare the real location: a symlinked/junctioned parent must not lead into the inputs.
    real = output.resolve(strict=False)
    region_root = region_dir.resolve()
    if real == region_root or region_root in real.parents:
        raise BatchFileError(f"refusing to write {output} inside the region directory {region_root}")
    if real == level_dat.resolve():
        raise BatchFileError("refusing to write over level.dat")


def _check_areas(area, core) -> None:
    """UIE2 geometry, checked before any input is read (capture_batch validates it again)."""
    for label, a in (("area", area), ("core", core)):
        if not isinstance(a, cs.Area):
            raise BatchFileError(f"{label} must be a capture_snapshot.Area")
        if any(v % 16 for v in (a.x, a.z, a.width, a.depth)):
            raise BatchFileError(f"{label} {a} is not aligned to 16 blocks")
    for start, span, cstart, cspan, axis in ((area.x, area.width, core.x, core.width, "x"),
                                             (area.z, area.depth, core.z, core.depth, "z")):
        if cstart - start < HALO_BLOCKS or (start + span) - (cstart + cspan) < HALO_BLOCKS:
            raise BatchFileError(f"core must lie inside the input with a halo of at least {HALO_BLOCKS} blocks along {axis}")


def _read_inputs(level_dat: Path, region_dir: Path, area: cs.Area):
    with open(level_dat, "rb") as fh:
        raw = fh.read(reader.DEFAULT_LIMITS.max_compressed + 1)
    registry = reader.parse_fml_registry(reader.parse_level_dat(raw))
    level = {"path": str(level_dat.resolve()), "sha256": _sha_bytes(raw), "size": len(raw)}
    chunks, roots, regions = {}, {}, []
    for (rx, rz), slots in sorted(cs.plan_chunks(area).items()):
        path = cs.region_path(region_dir, rx, rz)
        entry = {"file": path.name, "path": str(path.resolve()), "region": [rx, rz]}
        if not os.path.lexists(path):
            entry.update(state="absent", chunksPresent=0, chunksAbsent=len(slots))
            regions.append(entry)
            continue
        if not path.is_file():
            raise BatchFileError(f"{path} exists but is not a regular file")
        digest, size = cs.sha256_file(path)
        present = 0
        with reader.RegionFile(path, rx, rz) as region:
            for cx, cz, lx, lz in slots:
                root = region.read_chunk_nbt(lx, lz)
                if root is None:
                    continue
                chunk = reader.decode_chunk(root)
                if (chunk.x, chunk.z) != (cx, cz):
                    raise BatchFileError(f"chunk in slot ({lx},{lz}) of {path.name} claims position "
                                         f"({chunk.x},{chunk.z}), expected ({cx},{cz})")
                chunks[(cx, cz)], roots[(cx, cz)] = chunk, root
                present += 1
        entry.update(state="present", sha256=digest, size=size, chunksPresent=present, chunksAbsent=len(slots) - present)
        regions.append(entry)
    return registry, level, chunks, roots, regions


def _verify_sources(level: dict, regions: list[dict]) -> None:
    changed = []
    if cs.sha256_file(Path(level["path"])) != (level["sha256"], level["size"]):
        changed.append(level["path"])
    for entry in regions:
        path = Path(entry["path"])
        if entry["state"] == "absent":
            if os.path.lexists(path):
                changed.append(f"{path} (appeared)")
        elif not path.is_file() or cs.sha256_file(path) != (entry["sha256"], entry["size"]):
            changed.append(str(path))
    if changed:
        raise BatchFileError(f"source changed while reading, batch discarded: {', '.join(changed)}")


def _gt_core_counts(batch: cb.BatchSnapshot, tiles: list[dict]) -> dict:
    """Every GT ore block in the core needs its tile record (the renderer fails otherwise).

    select_ore_tiles already guarantees unique tiles that sit on GT ore blocks, so comparing positions
    is exact. Halo ore blocks without a record are allowed: the halo is lookup-only.
    """
    ore_ids = {i for i, name in batch.registry.items() if name == ore.GT_ORE_BLOCK}
    core = batch.core
    blocks = set()
    if ore_ids:
        for z in range(core.z, core.z + core.depth):
            for x in range(core.x, core.x + core.width):
                if batch.present_at(x, z):
                    blocks.update((x, y, z) for y, (block_id, _) in enumerate(batch.column(x, z)[1]) if block_id in ore_ids)
    in_core = {(t["x"], t["y"], t["z"]) for t in tiles
               if core.x <= t["x"] < core.x + core.width and core.z <= t["z"] < core.z + core.depth}
    missing = sorted(blocks - in_core, key=lambda p: (p[2], p[0], p[1]))
    if missing:
        raise BatchFileError(f"{len(missing)} GT ore block(s) in the core have no GT_TileEntity_Ores record, "
                             f"e.g. {missing[:5]}")
    return {"coreOreBlocks": len(blocks), "coreOreTiles": len(in_core), "windowOreTiles": len(tiles)}


def _write(path: Path, data: bytes) -> None:
    with open(path, "xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def create_batch(level_dat: Path, region_dir: Path, area: cs.Area, core: cs.Area, output_dir: Path) -> dict:
    """Build and publish one UIE2 batch directory; returns the provenance document."""
    level_dat, region_dir = Path(level_dat), Path(region_dir)
    output = Path(os.path.abspath(output_dir))
    _check_paths(level_dat, region_dir, output)
    _check_areas(area, core)

    registry, level, chunks, roots, regions = _read_inputs(level_dat, region_dir, area)
    batch = cb.from_chunks(area, core, registry.blocks, chunks)
    snapshot_bytes = cb.encode_batch(batch)
    snapshot_sha = _sha_bytes(snapshot_bytes)
    tiles = ore.select_ore_tiles(batch, roots)
    sidecar = {"format": ore.FORMAT, "snapshotSha256": snapshot_sha, "tiles": tiles,
               "sources": [{"path": r["path"], "sha256": r["sha256"]} for r in regions if r["state"] == "present"]}
    sidecar_bytes = _json_bytes(sidecar)
    window = [(cx, cz) for cz in range(area.z >> 4, (area.z + area.depth) >> 4)
              for cx in range(area.x >> 4, (area.x + area.width) >> 4)]
    provenance = {
        "format": "UIE2_BATCH_PROVENANCE1",
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": _area_json(area),
        "core": _area_json(core),
        "levelDat": level,
        "registryBlocks": len(registry.blocks),
        "regions": regions,
        "presentChunks": [[cx, cz] for cx, cz in window if (cx, cz) in batch.present_chunks],
        "absentChunks": [[cx, cz] for cx, cz in window if (cx, cz) not in batch.present_chunks],
        "snapshot": {"file": SNAPSHOT_FILE, "format": "UIE2", "sha256": snapshot_sha, "size": len(snapshot_bytes)},
        "tiles": {"file": TILES_FILE, "sha256": _sha_bytes(sidecar_bytes), "count": len(tiles)},
        "gtOres": _gt_core_counts(batch, tiles),
        "note": CONSISTENCY_NOTE,
    }
    provenance_bytes = _json_bytes(provenance)
    complete_bytes = _json_bytes({"format": "UIE2_BATCH_COMPLETE1", "snapshotSha256": snapshot_sha,
                                  "files": {SNAPSHOT_FILE: snapshot_sha, TILES_FILE: provenance["tiles"]["sha256"],
                                            PROVENANCE_FILE: _sha_bytes(provenance_bytes)}})
    files = [(SNAPSHOT_FILE, snapshot_bytes), (TILES_FILE, sidecar_bytes), (PROVENANCE_FILE, provenance_bytes)]

    temp = Path(tempfile.mkdtemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent))
    reserved = False
    moved: list[str] = []
    try:
        for name, data in files + [(COMPLETE_FILE, complete_bytes)]:
            _write(temp / name, data)
        # Read back what will be published.
        readback = (temp / SNAPSHOT_FILE).read_bytes()
        if readback != snapshot_bytes:
            raise BatchFileError("snapshot.bin does not read back identically")
        cb.parse_batch(readback)
        if json.loads((temp / TILES_FILE).read_text("utf-8"))["snapshotSha256"] != _sha_bytes(readback):
            raise BatchFileError("ore sidecar is not bound to the written snapshot")
        _verify_sources(level, regions)
        try:
            os.mkdir(output)  # exclusive reservation: fails if anything appeared at the target
        except FileExistsError:
            raise BatchFileError(f"output {output} appeared during the run; nothing was overwritten") from None
        reserved = True
        for name, data in files:
            os.replace(temp / name, output / name)
            moved.append(name)
            if _sha_bytes((output / name).read_bytes()) != _sha_bytes(data):
                raise BatchFileError(f"{name} changed while being published")
        os.replace(temp / COMPLETE_FILE, output / COMPLETE_FILE)  # commit marker, last
        moved.append(COMPLETE_FILE)
    except BaseException:
        if reserved:
            for name in reversed(moved):
                try:
                    (output / name).unlink()
                except FileNotFoundError:
                    pass
            try:
                output.rmdir()
            except OSError:
                pass  # something foreign appeared inside; leave it untouched
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return provenance


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="capture_batch_file.py",
        description="Create a UIE2 batch (snapshot, GT ore sidecar, provenance) for one planned tile from an "
                    "OFFLINE world copy. Inputs are only read; the output directory must not exist.")
    parser.add_argument("--level-dat", type=Path, required=True, help="copy of the world's level.dat")
    parser.add_argument("--region-dir", type=Path, required=True, help="copy of the world's region/ directory")
    parser.add_argument("--tile-x", type=int, required=True, help="tile X (core X = tile*96 .. +96)")
    parser.add_argument("--tile-z", type=int, required=True, help="tile Z (core Z = tile*96 .. +96)")
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory to create for the batch")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        area, core = tile_areas(args.tile_x, args.tile_z)
        provenance = create_batch(args.level_dat, args.region_dir, area, core, args.output_dir)
    except (cs.SnapshotError, reader.FormatError, ore.OreSnapshotError, OSError) as exc:
        print(f"capture_batch_file: error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {os.path.abspath(args.output_dir)} (UIE2 {provenance['snapshot']['sha256']}, "
          f"{len(provenance['presentChunks'])} present / {len(provenance['absentChunks'])} absent chunks, "
          f"{provenance['tiles']['count']} GT ore tiles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

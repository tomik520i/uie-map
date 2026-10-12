"""Whole-world overview, stage A: topmost block per column of each region (UIE_SURFACE1). Stdlib only.

Reads a world's region files read-only. Each region is first copied to a private directory and accepted only
when its size and mtime are unchanged before and after the copy (observed-stable-files; this is NOT an atomic
world snapshot). The copy is decoded with reader.py and reduced to four 512x512 column grids:

    present  1 byte   1 = the chunk exists and decoded, 0 = no chunk / undecodable chunk
    height   1 byte   topmost non-air block Y (0..255); only meaningful where present and kind != empty
    ids      2 bytes  big-endian block id of that block (FML registry id, see registry.json)
    metas    1 byte   its metadata

Columns with no non-air block keep present=1, height=0, id=0. The file is

    b'UIES' + u32 header length + header JSON (UTF-8) + zlib(present | height | ids | metas)

Header: {format, region:[rx,rz], source:{name,size,mtimeNs,sha256,observedAt:{startUtc,endUtc}}, chunks,
errors:[{chunk:[cx,cz], error}]}. Colours are a separate, cheap stage (overview_tiles.py), so colour tables
can improve without re-reading the world.

    python3 overview_surface.py --world /path/world --out DIR [--regions r.0.0.mca ...] [--workers 8]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import reader

MAGIC = b"UIES"
FORMAT = "UIE_SURFACE1"
SIZE = 512
CELLS = SIZE * SIZE
# Thin, see-through or floating blocks skipped when looking for the column's top (plants, crops, torches,
# rails, clouds). Applied to the local registry name; KEEP_PATTERN protects solid blocks with similar words.
SKIP_PATTERN = (r"(tallgrass|double_?plant|flower|foliage|longgrass|sapling|reeds|torch|vine|^web$|rail$|"
                r"redstone_wire|wheat|carrots|potatoes|waterlily|lilypad|deadbush|cloud|^crop|crops?$|bush$|fern|"
                r"tuft|_mushroom$|^(brown|red)_mushroom$|shrub|sprout|plant$)")
KEEP_PATTERN = r"(brick|stair|door|slab|block|processing|lamp|square|essence|stone|log|wood|plank|fence|wall|chest|machine)"
# Always skipped (KEEP_PATTERN does not apply): clouds and floating slime islands; a heightfield turns them into
# towers from the ground up (InventoryPets:cloudblock, TConstruct:slime.grass / slime.leaves ...). Liquid slime falls
# from the islands to the ground in one connected column (blue pillars, 2026-10-02), so it is skipped too, and so is
# the island soil (TConstruct:CraftedSoil, 3-5 blocks thick: the floating rule only checks the gap under the top block).
ALWAYS_SKIP_PATTERN = r"(cloud|^slime\.(grass|leaves|dirt|gel|sapling|grass\.tall)|^liquid\.slime$|^CraftedSoil$)"


# Canopy blocks: the colour comes from them, but the overview heightfield uses the ground below (no tree towers).
# Tree parts and things hanging in or lying on crowns (checked against LIVE-area columns 2026-10-02: BoP bamboo,
# fruit, willow, ivy; HarvestCraft beehives; Natura "tree"/"Rare Tree" logs; snow on leaves).
CANOPY_PATTERN = (r"(leaves|leaf|^log|logs?\d*$|_log$|branch|mushroom_block|huge_mushroom|cactus|vine|bamboo|fruit"
                  r"|beehive|^willow$|^ivy$|tree$|trunk|^snow_layer$)")


def canopy_ids(blocks: dict) -> list[int]:
    canopy = re.compile(CANOPY_PATTERN, re.I)
    return sorted(int(i) for i, name in blocks.items() if canopy.search(name.split(":", 1)[-1]))


def ground_heights(chunk, heights, skip: frozenset, canopy: frozenset) -> list:
    """Per column the highest block below the top that is neither air/skipped nor canopy (or the top itself)."""
    ground = list(heights)
    if not canopy:
        return ground
    for col in range(256):
        h = heights[col]
        if h < 0:
            continue
        lx, lz = col & 15, col >> 4
        y = h
        while y >= 0:
            block_id, _ = chunk.block(lx, y, lz)
            if block_id and block_id not in skip and block_id not in canopy:
                break
            y -= 1
        ground[col] = y if y >= 0 else h
    return ground


def skipped_ids(blocks: dict) -> list[int]:
    """Registry ids whose local name matches SKIP_PATTERN and not KEEP_PATTERN."""
    skip, keep = re.compile(SKIP_PATTERN, re.I), re.compile(KEEP_PATTERN, re.I)
    always = re.compile(ALWAYS_SKIP_PATTERN, re.I)
    result = []
    for block_id, name in blocks.items():
        local = name.split(":", 1)[-1]
        if always.search(local) or (skip.search(local) and not keep.search(local)):
            result.append(int(block_id))
    return sorted(result)


class OverviewError(Exception):
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def copy_stable(source: Path, target: Path, attempts: int = 3) -> dict:
    """Copy one region file; accept it only when size and mtime did not change around the copy."""
    for _ in range(attempts):
        start = _utc()
        before = os.stat(source)
        shutil.copyfile(source, target)
        after = os.stat(source)
        end = _utc()
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) \
                and os.stat(target).st_size == before.st_size:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            return {"name": source.name, "size": before.st_size, "mtimeNs": before.st_mtime_ns, "sha256": digest,
                    "observedAt": {"startUtc": start, "endUtc": end}, "consistency": "observed-stable-files"}
        time.sleep(0.2)
    raise OverviewError(f"{source.name} kept changing while it was copied")


def chunk_surface(chunk: reader.Chunk, skip: frozenset = frozenset()):
    """(heights, ids, metas) per column z*16+x; -1 height for an empty column. Fast path over reader.Chunk.
    Block ids in skip are treated like air."""
    heights, ids, metas = [-1] * 256, [0] * 256, [0] * 256
    pending = set(range(256))
    for sy in sorted(chunk.sections, reverse=True):
        section = chunk.sections[sy]
        for ly in range(15, -1, -1):
            base = ly << 8
            found = []
            for col in pending:
                i = base | col
                block_id = section._id_at(i)
                if block_id and block_id not in skip:
                    heights[col], ids[col], metas[col] = (sy << 4) | ly, block_id, reader._nibble(section.data, i)
                    found.append(col)
            pending.difference_update(found)
            if not pending:
                return _drop_floating(chunk, heights, ids, metas, skip)
    return _drop_floating(chunk, heights, ids, metas, skip)


# A top block with at least this many air (or skipped) blocks right below it, and something further down, is a
# floating object (sky island, crystal tree, cloud): a heightfield would turn it into a tower from the ground, so the
# overview uses the surface below the gap instead. Normal tree canopies have shorter gaps.
FLOAT_GAP = 8
SURFACE_RULES = f"floatGap={FLOAT_GAP};ground=2;canopy={CANOPY_PATTERN}"   # any change re-reads every region


def _drop_floating(chunk, heights, ids, metas, skip):
    for col in range(256):
        h = heights[col]
        if h < FLOAT_GAP + 1:
            continue
        lx, lz = col & 15, col >> 4
        while True:
            y, gap = h - 1, 0
            below = None
            while y >= 0:
                block_id, meta = chunk.block(lx, y, lz)
                if block_id == 0 or block_id in skip:
                    gap += 1
                    y -= 1
                    continue
                below = (y, block_id, meta)
                break
            if below is None or gap < FLOAT_GAP:
                break
            h = below[0]
            heights[col], ids[col], metas[col] = below
    return heights, ids, metas


def extract_region(path: Path, rx: int, rz: int, skip: frozenset = frozenset(),
                   canopy: frozenset = frozenset()) -> tuple[bytes, dict]:
    """(zlib body, header without source) for one (copied) region file."""
    present = bytearray(CELLS)
    height = bytearray(CELLS)
    ground = bytearray(CELLS)
    ids = bytearray(2 * CELLS)
    metas = bytearray(CELLS)
    chunks, errors = 0, []
    with reader.RegionFile(path, rx, rz) as region:
        for loc in region.locations():
            cx, cz = rx * 32 + loc.local_x, rz * 32 + loc.local_z
            try:
                chunk = region.read_chunk(loc.local_x, loc.local_z)
                if chunk is None:
                    continue
                h, b, m = chunk_surface(chunk, skip)
                g = ground_heights(chunk, h, skip, canopy)
            except (reader.FormatError, ValueError, IndexError) as exc:
                errors.append({"chunk": [cx, cz], "error": str(exc)[:200]})
                continue
            chunks += 1
            for lz in range(16):
                row = (loc.local_z * 16 + lz) * SIZE + loc.local_x * 16
                for lx in range(16):
                    col = lz * 16 + lx
                    cell = row + lx
                    present[cell] = 1
                    if h[col] >= 0:
                        height[cell] = h[col]
                        ids[2 * cell] = b[col] >> 8
                        ids[2 * cell + 1] = b[col] & 0xFF
                        metas[cell] = m[col]
                        ground[cell] = g[col]
    body = zlib.compress(bytes(present) + bytes(height) + bytes(ids) + bytes(metas) + bytes(ground), 6)
    return body, {"format": FORMAT, "region": [rx, rz], "chunks": chunks, "errors": errors,
                  "layers": ["present", "height", "ids", "metas", "ground"]}


def write_surface(path: Path, header: dict, body: bytes) -> None:
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(MAGIC + struct.pack(">I", len(raw)) + raw + body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_header(path: Path) -> dict:
    """Only the JSON header of a UIE_SURFACE1 file (cheap: no decompression)."""
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:4] != MAGIC or len(head) < 8:
            raise OverviewError(f"{path}: not a UIE_SURFACE1 file")
        (n,) = struct.unpack(">I", head[4:8])
        header = json.loads(f.read(n))
    if header.get("format") != FORMAT:
        raise OverviewError(f"{path}: wrong format")
    return header


def unchanged(source: Path, target: Path, skip: list) -> bool:
    """True if target was built from a source with the same size and mtime and the same skipped ids."""
    try:
        header = read_header(target)
        info = os.stat(source)
    except (OSError, ValueError, OverviewError):
        return False
    src = header.get("source", {})
    return (src.get("size"), src.get("mtimeNs"), header.get("skippedIdsSha256")) ==         (info.st_size, info.st_mtime_ns, _skip_digest(skip))


def _skip_digest(skip) -> str:
    # Also binds the surface rules: changing them makes every region "changed" for --changed-only.
    return hashlib.sha256(json.dumps([sorted(skip), SURFACE_RULES]).encode()).hexdigest()


def read_surface(path: Path) -> tuple[dict, dict]:
    """(header, grids {present, height, ids, metas}) with strict size checks."""
    raw = Path(path).read_bytes()
    if raw[:4] != MAGIC or len(raw) < 8:
        raise OverviewError(f"{path}: not a UIE_SURFACE1 file")
    (n,) = struct.unpack(">I", raw[4:8])
    header = json.loads(raw[8:8 + n])
    if header.get("format") != FORMAT:
        raise OverviewError(f"{path}: wrong format")
    body = zlib.decompress(raw[8 + n:])
    if len(body) not in (5 * CELLS, 6 * CELLS):
        raise OverviewError(f"{path}: body has {len(body)} bytes, expected {5 * CELLS} or {6 * CELLS}")
    height = body[CELLS:2 * CELLS]
    # Files from before the ground layer: the ground is the top.
    ground = body[5 * CELLS:] if len(body) == 6 * CELLS else height
    return header, {"present": body[:CELLS], "height": height, "ids": body[2 * CELLS:4 * CELLS],
                    "metas": body[4 * CELLS:5 * CELLS], "ground": ground}


def process_region(job) -> dict:
    """Worker: copy stable → extract → write; the private copy is always removed. Returns a summary.
    job = (source, out, scratch[, skipped ids])."""
    source, out, scratch = map(Path, job[:3])
    skip = frozenset(job[3]) if len(job) > 3 else frozenset()
    canopy = frozenset(job[4]) if len(job) > 4 else frozenset()
    rx, rz = reader.region_coords_from_name(source)
    target = out / f"r.{rx}.{rz}.surface"
    copy = scratch / f"{source.name}.{os.getpid()}.copy"
    started = time.monotonic()
    try:
        info = copy_stable(source, copy)
        body, header = extract_region(copy, rx, rz, skip, canopy)
    except (OverviewError, reader.FormatError, OSError) as exc:
        return {"region": [rx, rz], "ok": False, "error": str(exc)[:300]}
    finally:
        copy.unlink(missing_ok=True)
    header["source"] = info
    header["skippedIds"] = len(skip)
    header["skippedIdsSha256"] = _skip_digest(skip)
    write_surface(target, header, body)
    return {"region": [rx, rz], "ok": True, "chunks": header["chunks"], "errors": len(header["errors"]),
            "seconds": round(time.monotonic() - started, 3), "bytes": target.stat().st_size}


def write_registry(world: Path, out: Path) -> dict:
    """registry.json: {id: name} of the world's FML block registry (from a stable copy of level.dat)."""
    copy = out / "level.dat.copy"
    info = copy_stable(world / "level.dat", copy)
    try:
        registry = reader.parse_fml_registry(reader.read_level_dat(copy))
    finally:
        copy.unlink(missing_ok=True)
    blocks = {str(k): v for k, v in sorted(registry.blocks.items())}
    doc = {"format": "UIE_SURFACE_REGISTRY1", "source": info, "blocks": blocks,
           "skipPattern": SKIP_PATTERN, "keepPattern": KEEP_PATTERN, "alwaysSkipPattern": ALWAYS_SKIP_PATTERN,
           "skippedIds": skipped_ids(blocks), "canopyPattern": CANOPY_PATTERN, "canopyIds": canopy_ids(blocks)}
    (out / "registry.json").write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    return doc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--world", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--regions", nargs="*", help="region file names; default: all r.*.mca")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--changed-only", action="store_true",
                        help="skip regions whose source size/mtime and skip list equal the existing surface file")
    args = parser.parse_args(argv)
    world, out = args.world.resolve(), args.out.resolve()
    region_dir = world / "region"
    if out == world or out.is_relative_to(world) or world.is_relative_to(out):
        raise OverviewError("output must lie outside the world")
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / ".copies"
    scratch.mkdir(exist_ok=True)
    names = args.regions or sorted(p.name for p in region_dir.glob("r.*.mca"))
    registry = write_registry(world, out)
    skip = registry["skippedIds"]
    jobs = []
    for name in names:
        rx, rz = reader.region_coords_from_name(name)
        if args.skip_existing and (out / f"r.{rx}.{rz}.surface").exists():
            continue
        if args.changed_only and unchanged(region_dir / name, out / f"r.{rx}.{rz}.surface", skip):
            continue
        jobs.append((str(region_dir / name), str(out), str(scratch), skip, registry["canopyIds"]))
    started = time.monotonic()
    results = []
    progress = out / "progress.json"
    if args.workers > 1:
        from multiprocessing import Pool
        with Pool(args.workers) as pool:
            for result in pool.imap_unordered(process_region, jobs):
                results.append(result)
                progress.write_text(json.dumps({"done": len(results), "total": len(jobs),
                                                "elapsedSeconds": round(time.monotonic() - started, 1)}))
    else:
        for job in jobs:
            results.append(process_region(job))
    summary = {"format": "UIE_SURFACE_RUN1", "world": str(world), "regions": len(jobs),
               "ok": sum(r["ok"] for r in results), "failed": [r for r in results if not r["ok"]],
               "chunks": sum(r.get("chunks", 0) for r in results),
               "chunkErrors": sum(r.get("errors", 0) for r in results),
               "elapsedSeconds": round(time.monotonic() - started, 1), "finishedAt": _utc(), "results": results}
    (out / "run.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=1))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())

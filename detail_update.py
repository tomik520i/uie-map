"""Automatic detail updates with the offline renderer (no Minecraft): detect changed tiles, render, publish.

    python3 detail_update.py --world W --state DIR --profile PROFILE_DIR --store STORE --tiles X0,Z0,X1,Z1
                             [--status FILE] [--max-tiles N]

Tiles are the 6x6-chunk planning tiles (capture_batch_file.tile_areas); the configured rectangle is inclusive.

1. Fingerprint every tile from the region headers of its input window (core + one-chunk halo): per chunk the
   saved timestamp, sector offset and length. Reading 8 KiB headers is cheap and never touches chunk data.
2. Mode:
   - "full"   when the store's current generation is not area-tracked by this renderer (no `areas`, another
              pipeline key, or no publication yet): every configured tile is rendered and published with
              publish_attempt replace=True (must cover every chunk of the current map: never a smaller cut).
   - "update" otherwise: only tiles whose fingerprint changed (at most --max-tiles per run, oldest first) are
              rendered and published with carry-forward of every other tile.
3. Changed tiles are staged from stable copies (stage_saved_tile + create_staged_batch, observed-stable-files),
   planned (prepare_warm_plan with the offline profile's identity), rendered by warm_worker + offline_batch,
   verified (unsupported blocks allowed and counted) and published. Fingerprints are stored only for published
   tiles, so a failure is retried next run. state/status.json records what happened.

A file lock prevents overlapping runs. The game is never touched; only region headers/copies are read.

Bounds (checked before the state directory, the world or any staging is touched):
- --tiles is exactly four integers X0,Z0,X1,Z1 with X0<=X1, Z0<=Z1, every tile's input window (96-block core plus
  16-block halo, capture_batch_file.tile_areas) inside the 30 000 000-block world border, and at most
  MAX_RECT_TILES = 256 tiles (16 x 16 tiles = 1536 x 1536 blocks, 9216 chunks); the size is computed before any
  tile list is built.
- --max-tiles is an integer 1..16 (MAX_TILES_PER_RUN, the existing default).
- "full" mode must publish every configured tile in one run (a replacement may never be a smaller cut), so it is
  REFUSED when the rectangle has more tiles than --max-tiles, before anything is staged. Grow a larger area by
  first publishing a rectangle of at most --max-tiles tiles and then widening it: later runs are "update" runs that
  render at most --max-tiles changed or new tiles each and carry every other tile forward.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import capture_batch_file as cbf
import reader


WORLD_BORDER = 30_000_000
# Tile indices whose whole input window (core + halo) lies inside +-WORLD_BORDER blocks.
MIN_TILE = -((WORLD_BORDER - cbf.HALO_BLOCKS) // cbf.TILE_BLOCKS)
MAX_TILE = (WORLD_BORDER - cbf.TILE_BLOCKS - cbf.HALO_BLOCKS) // cbf.TILE_BLOCKS
MAX_RECT_TILES = 256
MAX_TILES_PER_RUN = 16
_COORD = re.compile(r"-?[0-9]{1,7}")


class DetailUpdateError(ValueError):
    """Refused before the state directory, the world or any staging was touched."""


def validate_rect(rect) -> tuple:
    """(X0, Z0, X1, Z1) of a finite, ordered, in-world tile rectangle of at most MAX_RECT_TILES tiles."""
    if not isinstance(rect, (tuple, list)) or len(rect) != 4 or any(type(v) is not int for v in rect):
        raise DetailUpdateError("tiles must be exactly four integers X0,Z0,X1,Z1")
    x0, z0, x1, z1 = rect
    if x0 > x1 or z0 > z1:
        raise DetailUpdateError("tiles rectangle must satisfy X0<=X1 and Z0<=Z1")
    if any(not MIN_TILE <= v <= MAX_TILE for v in rect):
        raise DetailUpdateError(f"tile coordinates must lie in {MIN_TILE}..{MAX_TILE} (inside the world border)")
    count = (x1 - x0 + 1) * (z1 - z0 + 1)          # computed, never enumerated, before the check
    if count > MAX_RECT_TILES:
        raise DetailUpdateError(f"tiles rectangle has {count} tiles; at most {MAX_RECT_TILES} are allowed")
    return x0, z0, x1, z1


def validate_max_tiles(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TILES_PER_RUN:
        raise DetailUpdateError(f"max_tiles must be an integer in 1..{MAX_TILES_PER_RUN}")
    return value


def parse_rect(text: str) -> tuple:
    parts = str(text).split(",")
    if len(parts) != 4 or not all(_COORD.fullmatch(p) for p in parts):
        raise DetailUpdateError("--tiles must be X0,Z0,X1,Z1 (four integers, no spaces)")
    return validate_rect(tuple(int(p) for p in parts))


def plan_todo(mode: str, tiles: list, changed: list, max_tiles: int) -> list:
    """Tiles to render this run. Full mode never drops a tile: more tiles than the budget is refused."""
    if mode == "full":
        if len(tiles) > max_tiles:
            raise DetailUpdateError(
                f"full publication needs all {len(tiles)} configured tiles in one run but max_tiles is {max_tiles}; "
                f"a replacement may not be a smaller cut. Publish at most {max_tiles} tiles first, then widen "
                f"the rectangle (update runs add at most max_tiles tiles each)")
        return list(tiles)
    return changed[:max_tiles]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def tile_fingerprint(region_dir: Path, tile_x: int, tile_z: int, cache: dict) -> str:
    """sha256 over (chunk, timestamp, offset, sectors) of every chunk in the tile's input window."""
    area, _core = cbf.tile_areas(tile_x, tile_z)
    h = hashlib.sha256()
    for cz in range(area.z >> 4, (area.z + area.depth) >> 4):
        for cx in range(area.x >> 4, (area.x + area.width) >> 4):
            rx, rz, lx, lz = reader.chunk_to_region(cx, cz)
            key = (rx, rz)
            if key not in cache:
                path = region_dir / f"r.{rx}.{rz}.mca"
                table = {}
                if path.exists():
                    with reader.RegionFile(path, rx, rz) as region:
                        for loc in region.locations():
                            table[(loc.local_x, loc.local_z)] = (loc.timestamp, loc.sector_offset, loc.sector_count)
                cache[key] = table
            h.update(json.dumps([cx, cz, cache[key].get((lx, lz))]).encode())
    return h.hexdigest()


STAGE_ATTEMPTS = 6
STAGE_RETRY_SECONDS = 5.0


def _stage_stable(stage, stage_error, level_dat: Path, region_dir: Path, staged: Path, tx: int, tz: int,
                  sleep=time.sleep) -> int:
    """stage_saved_tile with retries while the game is saving (a region changed during the copy). The rejected
    partial copy is removed before the next attempt; other errors fail at once. Returns the number of retries."""
    for attempt in range(STAGE_ATTEMPTS):
        try:
            stage(level_dat, region_dir, staged, tile_x=tx, tile_z=tz)
            return attempt
        except stage_error as err:
            if "changed while" not in str(err) or attempt == STAGE_ATTEMPTS - 1:
                raise
            if staged.exists():
                shutil.rmtree(staged)
            sleep(STAGE_RETRY_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def current_base(store: Path, pipeline_key: str):
    """(generation, tracked, active tile snapshots) of the store's current pointer; tracked = area records of
    this pipeline."""
    pointer = store / "current.json"
    if not pointer.exists():
        return None, False, set()
    generation = json.loads(pointer.read_bytes())["generation"]
    inventory = json.loads((store / "generations" / generation / "COMPLETE.json").read_bytes())
    tracked = bool(inventory.get("areas")) and inventory.get("pipelineKey") == pipeline_key
    superseded = set(inventory.get("supersededTiles", []))
    active = {t["snapshotSha256"] for a in inventory.get("areas", []) for t in a["tiles"]} - superseded
    return generation, tracked, active


def update(world: Path, state: Path, profile_dir: Path, store: Path, tiles_rect, status_file=None,
           max_tiles: int = MAX_TILES_PER_RUN) -> dict:
    # Bounds first: nothing below runs (no state directory, world read, staging or pipeline import) when refused.
    x0, z0, x1, z1 = validate_rect(tiles_rect)
    max_tiles = validate_max_tiles(max_tiles)
    started = time.monotonic()
    profile = json.loads((profile_dir / "profile.json").read_bytes())
    key = profile["identity"]["pipelineKey"]
    base, tracked, active = current_base(store, key)
    mode = "update" if tracked else "full"
    tiles = [(tx, tz) for tz in range(z0, z1 + 1) for tx in range(x0, x1 + 1)]   # <= MAX_RECT_TILES
    if mode == "full":
        plan_todo(mode, tiles, [], max_tiles)        # refuse an over-budget full run before any fingerprint/stage

    from prepare_warm_plan import prepare_warm_plan
    from publish_attempt import publish_verified_attempt
    from render_queue import RenderQueue
    from runtime_adapter import make_prepare
    from stage_saved_tile import StageError, stage_saved_tile
    from staged_batch import create_staged_batch
    import warm_worker as ww

    state.mkdir(parents=True, exist_ok=True)
    region_dir = world / "region"
    cache = {}
    prints = {f"{tx},{tz}": tile_fingerprint(region_dir, tx, tz, cache) for tx, tz in tiles}
    known_path = state / "fingerprints.json"
    known = json.loads(known_path.read_text()) if known_path.exists() else {}
    if known.get("pipelineKey") != key:
        known = {"pipelineKey": key, "tiles": {}}
    changed = [t for t in tiles if known["tiles"].get(f"{t[0]},{t[1]}") != prints[f"{t[0]},{t[1]}"]]
    todo = plan_todo(mode, tiles, changed, max_tiles)
    status = {"format": "UIE_DETAIL_UPDATE1", "lastRunAt": _now(), "mode": mode, "tiles": len(tiles),
              "changedTiles": len(changed), "renderedTiles": 0, "base": base, "pipelineKey": key}
    if not todo:
        status.update({"published": False, "reason": "no tile changed"})
    else:
        from detail_storage import require_run_capacity, DetailStorageError
        try:
            status['workingStorage'] = require_run_capacity(state)
        except DetailStorageError as error:
            raise DetailUpdateError(str(error)) from error
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run = state / "runs" / stamp
        batches, rendered, unchanged = [], [], []
        for tx, tz in todo:
            staged, batch = run / "staged" / f"tile_{tx}_{tz}", run / "batches" / f"tile_{tx}_{tz}"
            retries = _stage_stable(stage_saved_tile, StageError, world / "level.dat", region_dir, staged, tx, tz)
            status["stageRetries"] = status.get("stageRetries", 0) + retries
            create_staged_batch(staged, batch, tile_x=tx, tile_z=tz, accept_consistency="observed-stable-files")
            shutil.rmtree(staged)   # the batch keeps its own copy of what it needs
            snapshot = json.loads((batch / "COMPLETE").read_bytes())["snapshotSha256"]
            if mode == "update" and snapshot in active:
                # Saved again (entities, light) but the blocks are identical: nothing to render.
                unchanged.append((tx, tz))
                shutil.rmtree(batch)
                continue
            batches.append(str(batch))
            rendered.append((tx, tz))
        for tx, tz in unchanged:
            known["tiles"][f"{tx},{tz}"] = prints[f"{tx},{tz}"]
        status["unchangedBlocksTiles"] = len(unchanged)
        if not batches:
            known_path.write_text(json.dumps(known, indent=1))
            status.update({"published": False, "reason": "changed tiles have identical blocks"})
            status["elapsedSeconds"] = round(time.monotonic() - started, 1)
            (state / "status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
            return status
        todo = rendered
        plan = run / "plan"
        prepare_warm_plan(batches, plan, world="uie", dimension="0", pipeline_key=key,
                          pipeline_identity=profile["identity"], min_free_bytes=2 * 1024 ** 3)
        plan_sha = hashlib.sha256((plan / "plan.json").read_bytes()).hexdigest()
        job = hashlib.sha256((plan / "COMPLETE").read_bytes()).hexdigest()
        (run / "attempts").mkdir(mode=0o700)
        for d in ("work", "ledger"):
            (run / d).mkdir()
        with RenderQueue(run / "queue.sqlite", "uie", "0", key, max_attempts=3) as queue:
            queue.enqueue([{"id": job, "payload": {"planSha256": plan_sha}}])
            prepare = make_prepare(plan, profile_dir / "runtime", profile, max_copy_bytes=4 * 1024 ** 3,
                                   min_free_bytes=2 * 1024 ** 3, timeout=300)
            result = ww.run_one(queue, run / "attempts", prepare=prepare, verify=ww.verify_attempt_allowing_unsupported,
                                timeout=3600, poll_interval=0.5, term_timeout=10, kill_timeout=10, isolate_cgroup=True)
            status["worker"] = {k: result.get(k) for k in ("outcome", "error", "jobId")}
            if result["outcome"] == "succeeded":
                kwargs = dict(store=store, work_dir=run / "work", ledger_dir=run / "ledger", min_free_bytes=2 * 1024 ** 3,
                              status_file=str(status_file) if status_file else None)
                if base is None:
                    publication = publish_verified_attempt(queue, run / "attempts", job, **kwargs)
                elif mode == "full":
                    publication = publish_verified_attempt(queue, run / "attempts", job, expected_revision=base,
                                                           replace=True, **kwargs)
                else:
                    publication = publish_verified_attempt(queue, run / "attempts", job, expected_revision=base, **kwargs)
                status.update({"published": True, "publication": publication, "renderedTiles": len(todo)})
                for tx, tz in todo:
                    known["tiles"][f"{tx},{tz}"] = prints[f"{tx},{tz}"]
                known_path.write_text(json.dumps(known, indent=1))
            else:
                status["published"] = False
    status["elapsedSeconds"] = round(time.monotonic() - started, 1)
    (state / "status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True, type=Path)
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--profile", required=True, type=Path)
    ap.add_argument("--store", required=True, type=Path)
    ap.add_argument("--tiles", required=True, type=_arg(parse_rect),
                    help=f"X0,Z0,X1,Z1 inclusive tile rectangle (96-block tiles, at most {MAX_RECT_TILES} tiles)")
    ap.add_argument("--status")
    ap.add_argument("--max-tiles", type=_arg(lambda text: validate_max_tiles(int(text, 10))),
                    default=MAX_TILES_PER_RUN, help=f"1..{MAX_TILES_PER_RUN} tiles rendered per run")
    a = ap.parse_args(argv)                          # invalid bounds: exit 2 before anything is written
    import fcntl
    a.state.mkdir(parents=True, exist_ok=True)
    with open(a.state / "update.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped": "another detail update is running"}))
            return 0
        try:
            status = update(a.world.resolve(), a.state.resolve(), a.profile.resolve(), a.store.resolve(), a.tiles,
                            a.status, a.max_tiles)
        except DetailUpdateError as error:
            print(json.dumps({"refused": str(error)}), file=sys.stderr)
            return 1
    print(json.dumps(status, indent=1))
    return 0


def _arg(parse):
    """argparse type: DetailUpdateError/ValueError -> a usage error (exit 2)."""
    def convert(text):
        try:
            return parse(text)
        except ValueError as error:
            raise argparse.ArgumentTypeError(str(error)) from None
    return convert


if __name__ == "__main__":
    sys.exit(main())

"""Pure planner for tiled geometry render batches (no I/O, no rendering, no persistence).

The world is split into core tiles of 6x6 chunks (96x96 blocks). A render job for tile (tileX, tileZ)
covers its core plus a 1-chunk halo on every side: 8x8 chunks = 128x128 blocks, the largest area a
UIE1 snapshot / GeometryCapture accepts. Only core chunks are meant to be published from a job; the
halo exists so faces and connected textures at the core edge see their real neighbours.

    tileX = cx // 6            (Python floor division, correct for negative chunks)
    core chunks               cx in [tileX*6, tileX*6+5]
    input window              cx in [tileX*6-1, tileX*6+6]   (same for z)
    core origin (blocks)      tileX*96,       width 96
    snapshot origin (blocks)  tileX*96 - 16,  width 128

Inputs
    current, previous   {"cx,cz": sha256}   canonical decimal chunk keys ("-3,0"; no "+", "-0" or leading
                        zeros), values are 64 lowercase hex digits of the serialized chunk (blocks, tile
                        entities, biomes, light). Only generated chunks are present.
    pipeline_key        sha256 of everything that affects output (modpack, config, atlas, exporter, options)
    dimension           non-empty identity string of the dimension

plan_render(...) returns
    {"format": "UIE_RENDER_PLAN1", "dimension", "pipelineKey", "full": bool,
     "jobs": [job, ...]           sorted by (tileZ, tileX),
     "removedTiles": [{"tileX", "tileZ"}, ...]   sorted; tiles with previous core chunks and none now,
     "tileCount": number of tiles with at least one current core chunk}

    job = {"tileX", "tileZ", "dimension",
           "originX", "originZ", "width": 128, "depth": 128,       snapshot area in blocks
           "coreOriginX", "coreOriginZ", "coreWidth": 96, "coreDepth": 96,
           "coreChunks": [[cx, cz], ...]                           existing core chunks only
           "inputChunks": [{"x", "z", "sha256"}, ...]              existing core + halo chunks
           "missingChunks": [[cx, cz], ...]                        absent chunks in the whole 8x8 window
           "fingerprint": sha256 hex}
    Chunk lists are sorted by (z, x). Missing chunks are only listed: they are neither loaded nor air.

    fingerprint = sha256 of canonical JSON (sorted keys, no whitespace, ASCII) of
        {"format": "UIE_RENDER_TILE1", "dimension", "pipeline", "tileX", "tileZ",
         "window": [[cx, cz, sha256 or null], ...]}   all 64 cells, (z, x) order, null = missing

A full plan (every current tile) is made when previous is None, previous_pipeline_key is None or differs
from pipeline_key. Otherwise only tiles whose window contains an added, changed or removed chunk and that
still have a current core chunk are planned; their fingerprint necessarily differs from the previous one.

Caller contract: current and previous must refer to the SAME dimension/world. On switching worlds or
dimensions, pass previous=None (the function cannot infer the origin of a bare coordinate dictionary).
This is an invalidation plan, not a completion ledger. Persist outstanding jobs before advancing an
observed-input inventory; failed/unpublished jobs must survive even when the next inventory is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

TILE_CHUNKS = 6
HALO_CHUNKS = 1
WINDOW_CHUNKS = TILE_CHUNKS + 2 * HALO_CHUNKS
CHUNK_BLOCKS = 16
TILE_BLOCKS = TILE_CHUNKS * CHUNK_BLOCKS
WINDOW_BLOCKS = WINDOW_CHUNKS * CHUNK_BLOCKS
INT32_MIN, INT32_MAX = -(2 ** 31), 2 ** 31 - 1

_KEY = re.compile(r"(0|-?[1-9][0-9]*),(0|-?[1-9][0-9]*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha(value, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be 64 lowercase hex digits")
    return value


def _chunks(mapping, label: str) -> dict[tuple[int, int], str]:
    if not isinstance(mapping, Mapping):
        raise TypeError(f"{label} must be a mapping of 'cx,cz' to sha256")
    chunks = {}
    for key, value in mapping.items():
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None:
            raise ValueError(f"{label}: malformed chunk key {key!r}")
        cx, cz = int(match.group(1)), int(match.group(2))
        for c in (cx, cz):
            # Every window containing this chunk must stay inside int32 block coordinates.
            if (c - TILE_CHUNKS - HALO_CHUNKS) * CHUNK_BLOCKS < INT32_MIN \
                    or (c + TILE_CHUNKS + HALO_CHUNKS + 1) * CHUNK_BLOCKS - 1 > INT32_MAX:
                raise ValueError(f"{label}: chunk key {key!r} is outside the int32 block range")
        chunks[(cx, cz)] = _sha(value, f"{label}[{key!r}]")
    return chunks


def tile_of(cx: int, cz: int) -> tuple[int, int]:
    return cx // TILE_CHUNKS, cz // TILE_CHUNKS


def _window(tx: int, tz: int) -> list[tuple[int, int]]:
    """All 8x8 chunk coords of a tile window in (z, x) order."""
    x0, z0 = tx * TILE_CHUNKS - HALO_CHUNKS, tz * TILE_CHUNKS - HALO_CHUNKS
    return [(x, z) for z in range(z0, z0 + WINDOW_CHUNKS) for x in range(x0, x0 + WINDOW_CHUNKS)]


def _tiles_touching(cx: int, cz: int) -> set[tuple[int, int]]:
    """Tiles whose 8x8 window contains the chunk (its own tile plus neighbours when on a core edge)."""
    xs = {(cx - HALO_CHUNKS) // TILE_CHUNKS, (cx + HALO_CHUNKS) // TILE_CHUNKS}
    zs = {(cz - HALO_CHUNKS) // TILE_CHUNKS, (cz + HALO_CHUNKS) // TILE_CHUNKS}
    return {(tx, tz) for tx in xs for tz in zs}


def _core_tiles(chunks) -> set[tuple[int, int]]:
    return {tile_of(cx, cz) for cx, cz in chunks}


def _fingerprint(dimension: str, pipeline_key: str, tx: int, tz: int, chunks) -> str:
    document = {"format": "UIE_RENDER_TILE1", "dimension": dimension, "pipeline": pipeline_key,
                "tileX": tx, "tileZ": tz,
                "window": [[x, z, chunks.get((x, z))] for x, z in _window(tx, tz)]}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _job(dimension: str, pipeline_key: str, tx: int, tz: int, chunks) -> dict:
    window = _window(tx, tz)
    origin_x, origin_z = tx * TILE_BLOCKS - CHUNK_BLOCKS, tz * TILE_BLOCKS - CHUNK_BLOCKS
    return {
        "tileX": tx, "tileZ": tz, "dimension": dimension,
        "originX": origin_x, "originZ": origin_z, "width": WINDOW_BLOCKS, "depth": WINDOW_BLOCKS,
        "coreOriginX": tx * TILE_BLOCKS, "coreOriginZ": tz * TILE_BLOCKS,
        "coreWidth": TILE_BLOCKS, "coreDepth": TILE_BLOCKS,
        "coreChunks": [[x, z] for x, z in window if (x, z) in chunks and tile_of(x, z) == (tx, tz)],
        "inputChunks": [{"x": x, "z": z, "sha256": chunks[(x, z)]} for x, z in window if (x, z) in chunks],
        "missingChunks": [[x, z] for x, z in window if (x, z) not in chunks],
        "fingerprint": _fingerprint(dimension, pipeline_key, tx, tz, chunks),
    }


def plan_render(current: dict[str, str], previous: dict[str, str] | None, pipeline_key: str,
                previous_pipeline_key: str | None, dimension: str) -> dict:
    """Plan render jobs for changed tiles; see the module docstring for the format."""
    if not isinstance(dimension, str) or not dimension.strip():
        raise ValueError("dimension must be a non-empty string")
    _sha(pipeline_key, "pipeline_key")
    if previous_pipeline_key is not None:
        _sha(previous_pipeline_key, "previous_pipeline_key")
    now = _chunks(current, "current")
    before = None if previous is None else _chunks(previous, "previous")

    tiles_now = _core_tiles(now)
    full = before is None or previous_pipeline_key is None or previous_pipeline_key != pipeline_key
    if full:
        dirty = tiles_now
    else:
        changed = {c for c in now.keys() | before.keys() if now.get(c) != before.get(c)}
        dirty = {t for c in changed for t in _tiles_touching(*c)} & tiles_now
    removed = sorted(_core_tiles(before or {}) - tiles_now, key=lambda t: (t[1], t[0]))
    return {
        "format": "UIE_RENDER_PLAN1",
        "dimension": dimension,
        "pipelineKey": pipeline_key,
        "full": full,
        "jobs": [_job(dimension, pipeline_key, tx, tz, now) for tx, tz in sorted(dirty, key=lambda t: (t[1], t[0]))],
        "removedTiles": [{"tileX": tx, "tileZ": tz} for tx, tz in removed],
        "tileCount": len(tiles_now),
    }

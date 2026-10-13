"""Whole-world hires tiles like BlueMap: surface-only offline meshes in compact binary tiles, rendered in parallel.

  python3 world_hires.py --world W --catalog-dir C --out OUT [--workers 6] [--max-jobs N] [--center X,Z]

No Minecraft client and nothing written outside OUT: region files are copied to a private scratch directory and
accepted only when size and mtime did not change around the copy (observed-stable files, not an atomic snapshot).

Layout of OUT
  tiles/<tx>_<tz>.uih     gzip(UIH1) for one 64 x 64 block tile; absent when the tile has no geometry
  jobs/<rx>_<rz>.json     one region job (512 x 512 blocks, 64 tiles): source fingerprint + its tiles -> later runs
                          skip unchanged regions (a game save changes the fingerprint: automatic updates)
  index.json              UIE_HIRES_INDEX1: tile size, atlas, renderer identity, tiles {"tx,tz": sha12}, optional
                          stale ["tx,tz"]: previous good revisions kept while a tile failed or its job is pending
                          (same renderer + atlas only, file hash verified); they stay listed as errors for retry
  status.json             UIE_HIRES_STATUS1 of the running/last build (epoch seconds):
                          startedAt, updatedAt (heartbeat, at least every 30 s while running), finishedAt, state
                          running | done | stopped: <reason>; total/done = region jobs of THIS run (changed or
                          previously failed regions), skippedUnchanged = clean unchanged regions taken over,
                          plannedJobs = all region files, notScheduled = changed regions cut by --max-jobs;
                          failedJobs/failedTiles/errors (first 20) = tile failures workers reported in this run;
                          staleTiles = previous good references kept in the index (NOT rendered in this run);
                          planComplete = every planned region has a current error-free record. "done" means the run
                          finished, not that the world is complete or error-free. Private --fair-schedule runs add
                          schedule/budget telemetry and idle: null, or why no job was selected (nothing-pending |
                          disk-reserve | backoff) while state stays "done".
  atlas.png, atlas-layout.json   the catalog atlas (same atlas as the detail map)

UIH1 (little endian): '<4sHHiiII' magic b"UIH1", version 1, flags 0, originX, originZ, opaqueQuads, translucentQuads;
then for the opaque group and then the translucent group: positions uint16[q*12] = round((v - origin + 2) * 128)
(y uses origin 0), uvs uint16[q*8] = round(uv * 65535), colors uint8[q*12] = round(c * 255). Every quad is 4
vertices drawn as (0,1,2)(0,2,3); no index array is stored.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
from array import array
from pathlib import Path

TILE = 64                     # blocks per hires tile side (4 x 4 chunks); one mesh batch per tile (+16 halo = 96)
JOB = 512                     # blocks per job side: one region file (8 x 8 tiles)
HALO = 16
MAGIC = b"UIH1"
HEADER = struct.Struct("<4sHHiiII")
SCALE = 128
BIAS = 2
FORMAT_INDEX = "UIE_HIRES_INDEX1"
MIN_FREE_BYTES = 20 * 1024 ** 3


class HiresError(Exception):
    pass


# ------------------------------------------------------------------ binary tile format

def _parts(doc):
    return [doc, *doc.get("transparentParts", [])]


def encode_tile(origin_x: int, origin_z: int, docs: list) -> bytes | None:
    """gzip(UIH1) of the chunk mesh docs of one tile, or None without geometry."""
    groups = ([], [])          # (opaque, translucent) lists of parts
    for doc in docs:
        for part in _parts(doc):
            groups[1 if part.get("transparent") else 0].append(part)
    counts = []
    body = []
    for group in groups:
        pos, uv, col = array("H"), array("H"), array("B")
        quads = 0
        for part in group:
            p, u, c = part["positions"], part["uvs"], part["colors"]
            n = len(p) // 3
            if n % 4:
                raise HiresError("mesh part is not made of quads")
            quads += n // 4
            for i in range(0, len(p), 3):
                for v, o in ((p[i], origin_x), (p[i + 1], 0), (p[i + 2], origin_z)):
                    q = round((v - o + BIAS) * SCALE)
                    if not 0 <= q <= 65535:
                        raise HiresError(f"vertex {v} outside the tile range of origin {o}")
                    pos.append(q)
            uv.extend(min(65535, max(0, round(t * 65535))) for t in u)
            col.extend(min(255, max(0, round(t * 255))) for t in c)
        counts.append(quads)
        for a in (pos, uv, col):
            if sys.byteorder != "little" and a.itemsize > 1:
                a.byteswap()
            body.append(a.tobytes())
    if not sum(counts):
        return None
    raw = HEADER.pack(MAGIC, 1, 0, origin_x, origin_z, counts[0], counts[1]) + b"".join(body)
    out = gzip.compress(raw, compresslevel=9, mtime=0)
    return out[:9] + b"\xff" + out[10:]    # platform-independent gzip header OS byte


def decode_tile(data: bytes) -> dict:
    """Inverse of encode_tile (tests and tools): {origin, groups: [(positions, uvs, colors)]} as floats."""
    raw = gzip.decompress(data)
    magic, version, _flags, ox, oz, n0, n1 = HEADER.unpack_from(raw)
    if magic != MAGIC or version != 1:
        raise HiresError("not a UIH1 tile")
    off = HEADER.size
    groups = []
    for quads in (n0, n1):
        verts = quads * 4
        pos = array("H"); pos.frombytes(raw[off:off + verts * 6]); off += verts * 6
        uv = array("H"); uv.frombytes(raw[off:off + verts * 4]); off += verts * 4
        col = array("B"); col.frombytes(raw[off:off + verts * 3]); off += verts * 3
        if sys.byteorder != "little":
            pos.byteswap(); uv.byteswap()
        origin = (ox, 0, oz)
        groups.append(([q / SCALE - BIAS + origin[i % 3] for i, q in enumerate(pos)],
                       [q / 65535 for q in uv], [q / 255 for q in col]))
    if off != len(raw):
        raise HiresError("trailing bytes in UIH1 tile")
    return {"origin": (ox, oz), "groups": groups}


# ------------------------------------------------------------------ sources

class _Rect:
    """Plain rectangle for job windows (capture_snapshot.Area is limited to 128 blocks)."""
    def __init__(self, x, z, width, depth):
        self.x, self.z, self.width, self.depth = x, z, width, depth


def job_area(jx: int, jz: int):
    """(input window, core) of a region job: the region plus a 16 block halo."""
    core = _Rect(jx * JOB, jz * JOB, JOB, JOB)
    return _Rect(core.x - HALO, core.z - HALO, JOB + 2 * HALO, JOB + 2 * HALO), core


def tile_area(tx: int, tz: int):
    """(input window, core) capture_snapshot Areas of one tile batch."""
    import capture_snapshot as cs
    core = cs.Area(tx * TILE, tz * TILE, TILE, TILE)
    return cs.Area(core.x - HALO, core.z - HALO, TILE + 2 * HALO, TILE + 2 * HALO), core


def _regions_of(area) -> list[tuple[int, int]]:
    return [(rx, rz) for rz in range(area.z // 512, (area.z + area.depth - 1) // 512 + 1)
            for rx in range(area.x // 512, (area.x + area.width - 1) // 512 + 1)]


def fingerprint(region_dir: Path, area) -> str:
    """sha256 over (chunk, timestamp, offset, sectors) of every chunk slot in the area (region headers only)."""
    import reader
    h = hashlib.sha256()
    for rx, rz in _regions_of(area):
        path = region_dir / f"r.{rx}.{rz}.mca"
        table = {}
        if path.exists():
            with reader.RegionFile(path, rx, rz) as region:
                for loc in region.locations():
                    table[(loc.local_x, loc.local_z)] = (loc.timestamp, loc.sector_offset, loc.sector_count)
        for cz in range(max(area.z >> 4, rz * 32), min((area.z + area.depth) >> 4, rz * 32 + 32)):
            for cx in range(max(area.x >> 4, rx * 32), min((area.x + area.width) >> 4, rx * 32 + 32)):
                h.update(json.dumps([cx, cz, table.get((cx & 31, cz & 31))]).encode())
    return h.hexdigest()


def copy_regions(region_dir: Path, scratch: Path, area) -> dict:
    """Stable private copies of the region files touching the area: {(rx, rz): path}."""
    import overview_surface as ovs
    copies = {}
    for rx, rz in _regions_of(area):
        source = region_dir / f"r.{rx}.{rz}.mca"
        if source.exists():
            copy = scratch / source.name
            ovs.copy_stable(source, copy)
            copies[(rx, rz)] = copy
    return copies


def read_chunks(copies: dict, area, failed: set | None = None) -> dict:
    """Decoded chunks of the area from the private region copies (only this area is held in memory).

    A chunk that is not generated (no region file, empty slot) is absent. A chunk that exists but cannot be read
    or decoded is skipped as before; when `failed` is a set (strict mode, the hires worker) its (cx, cz) is added
    there instead of being indistinguishable from "absent", and an unreadable region file marks all its chunks
    of the area as failed rather than raising. Without `failed` the behaviour is unchanged."""
    import reader
    chunks = {}
    for rx, rz in _regions_of(area):
        copy = copies.get((rx, rz))
        if copy is None:
            continue
        cells = [(cx, cz) for cz in range(max(area.z >> 4, rz * 32), min((area.z + area.depth) >> 4, rz * 32 + 32))
                 for cx in range(max(area.x >> 4, rx * 32), min((area.x + area.width) >> 4, rx * 32 + 32))]
        try:
            region = reader.RegionFile(copy, rx, rz)
        except Exception:
            if failed is None:
                raise
            failed.update(cells)
            continue
        with region:
            for cx, cz in cells:
                try:
                    root = region.read_chunk_nbt(cx & 31, cz & 31)
                    if root is None:
                        continue
                    chunk = reader.decode_chunk(root)
                except Exception:     # a damaged chunk is skipped, never fatal (strict mode: reported)
                    if failed is not None:
                        failed.add((cx, cz))
                    continue
                if (chunk.x, chunk.z) == (cx, cz) and isinstance(chunk.biomes, bytes) and len(chunk.biomes) == 256:
                    chunks[(cx, cz)] = chunk
                elif failed is not None:
                    failed.add((cx, cz))
    return chunks


# ------------------------------------------------------------------ worker

_W = {}


def _init(catalog_dir: str, registry: dict, region_dir: str, out: str, caves=False, cap_binding=None):
    from offline_catalog import Catalog
    from offline_mesher import Colormap, Mesher
    d = Path(catalog_dir)
    catalog = Catalog.load_compiled(d / "catalog.json")
    _W.update(mesher=Mesher(catalog, Colormap(d / "grass.png", d / "foliage.png"), caves=caves),
              registry=registry, region_dir=Path(region_dir), out=Path(out), cap_binding=cap_binding,
              scratch=Path(tempfile.mkdtemp(prefix="uie-hires-")))


def _batch(cb, area, core, window):
    """from_chunks; block ids absent from level.dat (removed mods) get a placeholder name and render as the
    catalog fallback instead of failing the tile."""
    from offline_volume import missing_registry_ids
    for _ in range(8):
        try:
            return cb.from_chunks(area, core, _W["registry"], window)
        except cb.BatchError as error:
            ids=missing_registry_ids(str(error),_W['registry'])
            if not ids:raise
            for block_id in ids:
                _W["registry"].setdefault(block_id, f"uie:unknown_{block_id}")
    raise HiresError("too many unknown block ids")


def _atomic_write(path: Path, raw: bytes):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)      # the previous file at `path` stays as it was
        raise


def _publish_catalog_asset(source: Path, target: Path):
    """Replace an asset inode atomically, preserving any immutable hardlinks to the old bytes."""
    raw = source.read_bytes()
    if target.exists() and target.read_bytes() == raw:
        return
    _atomic_write(target, raw)


def render_job(job):
    """Render one 256 x 256 job into tiles; returns its job record."""
    import capture_batch as cb
    from offline_volume import Volume
    jx, jz, fp = job
    started = time.perf_counter()
    area, core = job_area(jx, jz)
    out = _W["out"]
    record = {"job": [jx, jz], "fingerprint": fp, "tiles": {}, "chunks": 0, "quads": 0, "bytes": 0, "fallback": 0,
              "errors": []}
    if _W.get('cap_binding'):
        record['capPairs']={}
    copies = copy_regions(_W["region_dir"], _W["scratch"], area)
    try:
        _render_rows(record, copies, area, core, out, cb, Volume)
    finally:
        for copy in copies.values():
            copy.unlink(missing_ok=True)
    record["seconds"] = round(time.perf_counter() - started, 2)
    _atomic_write(out / "jobs" / f"{jx}_{jz}.json", json.dumps(record, separators=(",", ":")).encode())
    return record


def _render_rows(record, copies, area, core, out, cb, Volume):
    """One row of tiles at a time: only a 96 block high strip of the region is decoded in memory.

    A tile that fails (unreadable source chunk in its core, mesh, encoding or write error) is reported in
    record["errors"] (retried next run) and its previous file is left untouched; only a tile rendered
    successfully as empty removes its file. record["tiles"] lists only tiles rendered in this job."""
    from world_diagnostics import tile_diagnostics, empty_tile_diagnostics, job_diagnostics
    diagnostics = []
    diagnostic_errors = []
    def fail(tx, tz, error):
        record["errors"].append([tx, tz, (error if isinstance(error, str) else f"{type(error).__name__}: {error}")[:300]])

    for tz in range(core.z // TILE, (core.z + core.depth) // TILE):
        strip = _Rect(area.x, tz * TILE - HALO, area.width, TILE + 2 * HALO)
        unreadable = set()
        chunks = read_chunks(copies, strip, failed=unreadable)
        for tx in range(core.x // TILE, (core.x + core.width) // TILE):
            path = out / "tiles" / f"{tx}_{tz}.uih"
            t_area, t_core = tile_area(tx, tz)
            core_keys = {(cx, cz) for cx in range(t_core.x >> 4, (t_core.x + TILE) >> 4)
                         for cz in range(t_core.z >> 4, (t_core.z + TILE) >> 4)}
            # A chunk that exists but cannot be read is not "absent": never render (or delete) the tile without it.
            bad = core_keys & unreadable
            if bad:
                fail(tx, tz, f"source read failed for {len(bad)} chunk(s), e.g. {min(bad)}")
                continue
            docs = []
            fallback = 0
            volume = result = None
            if core_keys & set(chunks):
                window = {k: c for k, c in chunks.items()
                          if t_area.x >> 4 <= k[0] < (t_area.x + t_area.width) >> 4
                          and t_area.z >> 4 <= k[1] < (t_area.z + t_area.depth) >> 4}
                try:
                    volume = Volume(_batch(cb, t_area, t_core, window))
                    result = _W["mesher"].mesh_batch(volume)
                    docs = [doc for _, doc in sorted(result["chunks"].items())]
                    fallback = sum(result["stats"]["fallbackBlocks"].values())
                except Exception as error:      # one bad tile never stops the world build; it is reported
                    fail(tx, tz, error)
                    continue
            try:
                data = encode_tile(tx * TILE, tz * TILE, docs) if docs else None
                pair_sha=None
                if data is not None and _W.get('cap_binding'):
                    from cave_cap_export import build_cap_tile
                    from cave_pair_store import store
                    caps,cap_stats=build_cap_tile(volume,_W['mesher'].catalog)
                    pair_sha,pair_doc=store(out,data,caps,**_W['cap_binding'])
                if data is None:
                    path.unlink(missing_ok=True)     # known empty: only this builder's own output
                elif pair_sha is not None:
                    from cave_pair_store import publish_geometry_alias
                    publish_geometry_alias(out,[tx*TILE,tz*TILE],pair_doc['geometrySha256'])
                else:
                    _atomic_write(path, data)
            except Exception as error:          # the previous file stays; it is retried next run
                fail(tx, tz, error)
                continue
            record["fallback"] += fallback
            record["chunks"] += len(core_keys & set(chunks))
            # Diagnostics never change geometry/renderer identity and cannot turn a successful render into a failure.
            try:
                diagnostics.append(tile_diagnostics(volume, _W["mesher"].catalog, result["stats"])
                                   if volume is not None else empty_tile_diagnostics())
            except Exception as error:
                if len(diagnostic_errors) < 20:
                    diagnostic_errors.append([tx, tz, f"{type(error).__name__}: {error}"[:300]])
            if data is None:
                continue
            record["tiles"][f"{tx},{tz}"] = hashlib.sha256(data).hexdigest()[:12]
            if pair_sha is not None:
                record.setdefault('capPairs',{})[f'{tx},{tz}']=pair_sha
                record.setdefault('capDiagnostics',{})[f'{tx},{tz}']=cap_stats
            record["bytes"] += len(data)
            record["quads"] += sum(len(p["positions"]) // 12 for d in docs for p in _parts(d))
    record["diagnostics"] = job_diagnostics(diagnostics, (core.width // TILE) * (core.depth // TILE))
    if diagnostic_errors:
        record["diagnosticErrors"] = diagnostic_errors


# ------------------------------------------------------------------ build

def plan_jobs(region_dir: Path, center=(0, 0)) -> list[tuple[int, int]]:
    jobs = set()
    for path in region_dir.glob("r.*.mca"):
        try:
            _, rx, rz, _ = path.name.split(".")
            jobs.add((int(rx), int(rz)))
        except ValueError:
            continue
    cx, cz = center
    return sorted(jobs, key=lambda j: (math.hypot(j[0] * JOB + JOB / 2 - cx, j[1] * JOB + JOB / 2 - cz), j))


_TILE_KEY = __import__("re").compile(r"-?[0-9]{1,7},-?[0-9]{1,7}")
_SHA12 = __import__("re").compile(r"[0-9a-f]{12}")


class LastGood:
    """Tile references of the previously published index, usable only for the SAME renderer and atlas.

    Tile paths are mutable (tiles/<tx>_<tz>.uih is rewritten in place), so a reference is handed out only while
    the file still hashes to it; the check is cached per (mtime, size, inode) and repeated when the file changes."""

    def __init__(self, out: Path, meta: dict):
        self.out = out
        self.refs = {}
        self._checked = {}
        try:
            doc = json.loads((out / "index.json").read_bytes())
        except (OSError, ValueError):
            doc = None
        compatible = (isinstance(doc, dict) and doc.get("format") == FORMAT_INDEX and doc.get("tileSize") == TILE
                      and all(doc.get(k) == v for k, v in meta.items()) and isinstance(doc.get("tiles"), dict))
        if compatible:   # renderer, identity, caves, atlas name and atlas hash all equal: geometry fits this atlas
            self.refs = {k: v for k, v in doc["tiles"].items()
                         if isinstance(k, str) and _TILE_KEY.fullmatch(k) and isinstance(v, str) and _SHA12.fullmatch(v)}
        self.by_job = {}
        for key in self.refs:
            tx, tz = (int(v) for v in key.split(","))
            self.by_job.setdefault((tx * TILE // JOB, tz * TILE // JOB), []).append(key)

    def get(self, key: str):
        """The previous reference of `key` if its file still has exactly that content, else None."""
        sha = self.refs.get(key)
        if sha is None:
            return None
        path = self.out / "tiles" / f"{key.replace(',', '_')}.uih"
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
            cached = self._checked.get(key)
            if cached is None or cached[0] != stamp:
                cached = (stamp, hashlib.sha256(path.read_bytes()).hexdigest()[:12] == sha)
                self._checked[key] = cached
        except OSError:
            return None
        return sha if cached[1] else None


def _index_tiles(records: dict, last_good: LastGood | None, planned) -> tuple[dict, list]:
    """Current tiles of the records, plus verified previous references (marked stale) for tiles that failed in
    this run and for planned jobs not rendered (yet) in this run. A tile rendered empty is not carried over."""
    tiles = {}
    for rec in records.values():
        tiles.update(rec["tiles"])
    stale = []
    if last_good is None or not last_good.refs:
        return tiles, stale

    def keep(key):
        if key not in tiles:
            sha = last_good.get(key)
            if sha is not None:
                tiles[key] = sha
                stale.append(key)

    for rec in records.values():
        for error in rec.get("errors", []):
            keep(f"{error[0]},{error[1]}")
    for job, keys in last_good.by_job.items():
        if job in planned and job not in records:
            for key in keys:
                keep(key)
    return tiles, sorted(stale)


def _write_index(out: Path, meta: dict, records: dict, last_good: LastGood | None = None, planned=()):
    tiles, stale = _index_tiles(records, last_good, set(planned))
    doc = {"format": FORMAT_INDEX, "tileSize": TILE, **meta, "jobs": len(records),
           "chunks": sum(r["chunks"] for r in records.values()), "tiles": tiles}
    if stale:
        doc["stale"] = stale      # previous good revisions, NOT rendered in this run (failed or still pending)
    _atomic_write(out / "index.json", json.dumps(doc, separators=(",", ":")).encode())
    # Old clean jobs lack diagnostics: count them as unknown without forcing a full-world render.
    from world_diagnostics import world_diagnostics, validate_diagnostics, DiagnosticsError
    diagnostic_docs = []
    for rec in records.values():
        item = rec.get("diagnostics")
        try:
            if item is not None:
                validate_diagnostics(item)
                if item.get("scope") != "job":
                    item = None
        except (DiagnosticsError, TypeError, KeyError):
            item = None
        diagnostic_docs.append(item)
    diagnostic_doc = world_diagnostics(diagnostic_docs, max(len(planned), len(records)))
    diagnostic_doc.update(generatedAt=time.time(), atlasSha256=meta.get("atlasSha256"), rendererIdentity=meta.get("identity"))
    try:
        _atomic_write(out / "diagnostics.json", json.dumps(diagnostic_doc, separators=(",", ":")).encode())
    except OSError as error:
        print(f"diagnostics publication failed: {error}", file=sys.stderr)
    return len(stale)


HEARTBEAT_SECONDS = 30        # status.json updatedAt is refreshed at least this often while a build runs
MAX_STATUS_ERRORS = 20        # failed tiles listed in status.json (all are counted in failedTiles)


def _next_result(results, timeout: float):
    """Next worker result; multiprocessing's iterator waits at most `timeout` (raises multiprocessing.TimeoutError),
    plain iterators (tests) are simply advanced."""
    wait = getattr(results, "next", None)
    return wait(timeout) if callable(wait) else next(results)


def _note_errors(status: dict, rec: dict):
    """Aggregate the tile failures a worker reported for one job (read, mesh, encoding or write errors)."""
    errors = rec.get("errors") or []
    if not errors:
        return
    status["failedJobs"] += 1
    status["failedTiles"] += len(errors)
    for tx, tz, message in errors:
        if len(status["errors"]) >= MAX_STATUS_ERRORS:
            break
        status["errors"].append({"job": list(rec["job"]), "tile": [tx, tz], "message": str(message)[:300]})


def check_render_mode(out: Path, caves: bool):
    """A cave layer must have its own store; never overwrite a surface layer in place."""
    if type(caves) is not bool:
        raise HiresError('caves must be a boolean')
    index = out / 'index.json'
    if index.exists():
        doc = json.loads(index.read_bytes())
        previous = doc.get('caves', False)
        if type(previous) is not bool or previous != caves:
            raise HiresError('Surface and cave renders require separate output stores')
    elif caves and any((out / name).exists() and any((out / name).iterdir()) for name in ('jobs', 'tiles')):
        raise HiresError('Cannot identify existing output mode; use a fresh cave store')


def geometry_identity(catalog_identity, code, caves=False, cave_caps=False):
    base = hashlib.sha256(catalog_identity.encode() + hashlib.sha256(code).digest()).hexdigest()
    base=hashlib.sha256((base + ':caves=true').encode()).hexdigest() if caves else base
    if cave_caps:
        if not caves:raise HiresError('Cave caps require cave geometry')
        from cave_pair import cap_identity
        base=hashlib.sha256((base+':caps='+cap_identity()).encode()).hexdigest()
    return base


def build(world: Path, catalog_dir: Path, out: Path, workers: int, max_jobs=None, center=(0, 0),
          *, immutable_prepare=False, caves=False, cave_caps=False, fair_schedule=False,
          cave_store_budget_bytes=2*1024**3, cave_reserve_bytes=24*1024**3) -> dict:
    import reader
    from offline_catalog import Catalog
    world, out, catalog_dir = world.resolve(), out.resolve(), catalog_dir.resolve()
    if type(cave_caps) is not bool or (cave_caps and not caves):
        raise HiresError('Cave caps require cave geometry and a boolean option')
    if out == world or out.is_relative_to(world):
        raise HiresError("output must lie outside the world")
    check_render_mode(out, caves)
    budget_report = None
    if type(fair_schedule) is not bool:
        raise HiresError('fair_schedule must be boolean')
    if fair_schedule:
        if not caves or not cave_caps or type(workers) is not int or workers != 1 or type(max_jobs) is not int or max_jobs != 1:
            raise HiresError('Fair cave pilot requires caves, caps, one worker and max_jobs=1')
        if type(cave_reserve_bytes) is not int or cave_reserve_bytes < MIN_FREE_BYTES:
            raise HiresError('Cave reserve must be at least the free disk floor')
        from cave_run_budget import inspect_budget
        budget_report = inspect_budget(out,max_store_bytes=cave_store_budget_bytes)
        if budget_report['state'] != 'ready':
            raise HiresError('Cave budget blocked: '+','.join(budget_report['reasons']))
    region_dir = world / "region"
    for sub in ("tiles", "jobs"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for name in ("atlas.png", "atlas-layout.json"):
        _publish_catalog_asset(catalog_dir / name, out / name)
    raw = (world / "level.dat").read_bytes()
    registry = dict(reader.parse_fml_registry(reader.parse_level_dat(raw)).blocks)
    catalog = Catalog.load_compiled(catalog_dir / "catalog.json")
    # Output-defining code only (mesher, catalog, volume) + the tile format version: scheduling changes in this
    # file must not force a whole-world re-render.
    code = MAGIC + b"".join(Path(__file__).with_name(n).read_bytes()
                            for n in ("offline_mesher.py", "offline_catalog.py", "offline_volume.py"))
    identity = geometry_identity(catalog.identity, code, caves, cave_caps)
    meta = {"renderer": "uie-offline-mesher/1+hires", "identity": identity, "caves": caves,
            "atlas": "atlas.png", "atlasSha256": hashlib.sha256((out / "atlas.png").read_bytes()).hexdigest()}
    cap_binding=dict(atlas_sha=meta['atlasSha256'],renderer_identity=identity) if cave_caps else None
    # Read before anything is rewritten: the last published references, if they belong to this renderer + atlas.
    last_good = LastGood(out, meta)
    planned = plan_jobs(region_dir, center)
    records, todo = {}, []
    schedule_pending = []
    for jx, jz in planned:
        area, _ = job_area(jx, jz)
        fp = hashlib.sha256((identity + fingerprint(region_dir, area)).encode()).hexdigest()
        path = out / "jobs" / f"{jx}_{jz}.json"
        old = json.loads(path.read_bytes()) if path.exists() else None
        # An unchanged source does not make a partial job complete: retry tiles that failed last time.
        if old and old.get("fingerprint") == fp and old.get("errors") == []:
            records[(jx, jz)] = old
        else:
            todo.append((jx, jz, fp))
            if fair_schedule:
                schedule_pending.append({'job':[jx,jz],
                    'kind':'missing' if not old else ('failed' if old.get('errors') else 'changed'),
                    'distance':(jx*JOB+JOB//2-center[0])**2+(jz*JOB+JOB//2-center[1])**2})
    changed = len(todo)
    schedule_ledger = schedule_report = schedule_idle = None
    if fair_schedule:
        import hires_schedule as schedule
        import hires_bundle as schedule_bundle
        chosen,schedule_ledger,schedule_report = schedule.select(schedule_pending,schedule.load(out),
            now=time.time(),max_jobs=max_jobs,free_bytes=shutil.disk_usage(out).free,
            floor_bytes=MIN_FREE_BYTES,reserve_bytes=cave_reserve_bytes)
        fingerprints={(jx,jz):fp for jx,jz,fp in todo}
        todo=[(*entry['job'],fingerprints[tuple(entry['job'])]) for entry in chosen]
        # Intent must be durable before workers start: crashes get a retry delay.
        schedule_bundle._atomic(out/'schedule.json',schedule.serialize(schedule_ledger))
        if not chosen:
            # Explicit reason for a run without work (state stays "done" for existing consumers): nothing dirty, an
            # eligible queue head refused by the disk reserve/floor, or every dirty region waiting in retry backoff.
            schedule_idle=('nothing-pending' if not schedule_report['pending'] else
                           'disk-reserve' if any(schedule_report['budgetBlocked'].values()) else 'backoff')
    elif max_jobs is not None:
        todo = todo[:max_jobs]
    started = time.time()
    # total/done: region jobs of THIS run (changed or previously failed regions); skippedUnchanged: clean regions
    # taken over; plannedJobs: every region file of the world; notScheduled: changed regions cut by --max-jobs.
    status = {"format": "UIE_HIRES_STATUS1", "startedAt": started, "updatedAt": started,
              "heartbeatSeconds": HEARTBEAT_SECONDS, "total": len(todo), "done": 0,
              "skippedUnchanged": len(records), "plannedJobs": len(planned), "notScheduled": changed - len(todo),
              "chunks": 0, "bytes": 0, "failedJobs": 0, "failedTiles": 0, "errors": [], "planComplete": False,
               "state": "running"}
    if fair_schedule:
        status['schedule']=schedule_report
        status['budget']=budget_report
        status['idle']=schedule_idle      # None when a job was selected

    def write_status():
        status["updatedAt"] = time.time()
        _atomic_write(out / "status.json", json.dumps(status).encode())
        return status["updatedAt"]

    last_status = write_status()
    last_index = 0.0
    import multiprocessing as mp
    try:
        with mp.get_context("fork" if hasattr(os, "fork") else "spawn").Pool(
                workers, initializer=_init, initargs=(str(catalog_dir), registry, str(region_dir), str(out), caves, cap_binding)) as pool:
            results = pool.imap_unordered(render_job, todo)
            while True:
                # Wait for the next job at most until the next heartbeat is due (no busy loop, no rescans).
                try:
                    rec = _next_result(results, max(0.1, HEARTBEAT_SECONDS - (time.time() - last_status)))
                except StopIteration:
                    break
                except mp.TimeoutError:
                    last_status = write_status()      # alive, still waiting for the workers
                    continue
                records[tuple(rec["job"])] = rec
                if fair_schedule:
                    schedule_ledger=schedule.finish(schedule_ledger,rec['job'],ok=not bool(rec.get('errors')),now=time.time())
                    schedule_bundle._atomic(out/'schedule.json',schedule.serialize(schedule_ledger))
                status["done"] += 1
                status["chunks"] += rec["chunks"]
                status["bytes"] += rec["bytes"]
                _note_errors(status, rec)
                elapsed = time.time() - started
                status["etaSeconds"] = round(elapsed / status["done"] * (status["total"] - status["done"]))
                if time.time() - last_index > 30 or status["done"] == status["total"]:
                    status["staleTiles"] = _write_index(out, meta, records, last_good, planned)
                    last_status = write_status()
                    last_index = time.time()
                elif time.time() - last_status >= HEARTBEAT_SECONDS:
                    last_status = write_status()
                if shutil.disk_usage(out).free < MIN_FREE_BYTES:
                    status["state"] = "stopped: free disk below 20 GiB"
                    pool.terminate()
                    break
    except BaseException as error:
        # A crashed build is never reported as done: the status says stopped (with the progress and failures known
        # so far), the last index stays, and it raises.
        status["state"] = f"stopped: build failed: {type(error).__name__}: {error}"[:300]
        status["finishedAt"] = time.time()
        try:
            write_status()
        except OSError:
            pass
        raise
    if status["state"] == "running":
        status["state"] = "done"    # the run finished; failedTiles/planComplete say whether everything is clean
    status["finishedAt"] = time.time()
    status["staleTiles"] = _write_index(out, meta, records, last_good, planned)
    # Every region of the plan has a current record without tile errors (rendered now or unchanged and clean).
    status["planComplete"] = (len(records) == len(planned)
                              and all(not r.get("errors") for r in records.values()))
    if immutable_prepare:
        # Opt-in staging only, under the same caller-owned build lock. Never publishes current.json.
        # A staging failure is independent of the completed legacy render and preserves its index.
        if status["state"] != "done" or shutil.disk_usage(out).free < MIN_FREE_BYTES:
            status["immutablePreparation"] = {"state": "skipped", "published": False,
                                               "reason": "build stopped or free disk below 20 GiB"}
        else:
            try:
                from hires_backfill import step
                prepared = step(out, max_tiles=128, max_source_bytes=64*1024*1024, reconcile=True)
                status["immutablePreparation"] = {
                    "state": "prepared" if prepared["complete"] else "pending", "published": False,
                    **{k: prepared[k] for k in ("total", "captured", "remaining", "stepTiles", "stepBytes",
                                               "reconciled", "legacyIndexSha256")}}
            except Exception as error:
                status["immutablePreparation"] = {"state": "error", "published": False,
                                                   "message": f"{type(error).__name__}: {error}"[:300]}
    write_status()
    return status


LOCK_RETRY_SECONDS = 0.2
MAX_LOCK_WAIT_SECONDS = 300


def _valid_lock_wait(value) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 <= value <= MAX_LOCK_WAIT_SECONDS):
        raise ValueError(f"lock wait must be a finite number of seconds in 0..{MAX_LOCK_WAIT_SECONDS}")
    return float(value)


def _lock_wait_arg(text: str) -> float:
    try:
        return _valid_lock_wait(float(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def acquire_build_lock(handle, wait_seconds=0.0, *, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Exclusive flock on the caller's open lock file: LOCK_NB attempts, retried every 0.2 s (or the remaining time)
    until a monotonic deadline of wait_seconds after the first attempt. True: `handle` holds the lock. False: still
    busy at the deadline; nothing is held or released (the caller owns and closes `handle`). wait_seconds 0 is the
    previous single attempt. Errors other than "busy" are raised. Without fcntl (Windows tests) there is no
    concurrent scheduler: True."""
    wait_seconds = _valid_lock_wait(wait_seconds)
    try:
        import fcntl
    except ImportError:
        return True
    import errno
    deadline = clock() + wait_seconds
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as error:
            if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(LOCK_RETRY_SECONDS, remaining))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--world", required=True, type=Path)
    ap.add_argument("--catalog-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-jobs", type=int)
    ap.add_argument("--center", default="0,0")
    ap.add_argument("--caves", action="store_true", help="include enclosed underground surfaces; requires a separate output store")
    ap.add_argument('--cave-caps',action='store_true',help='private cave pair objects from the same volume; requires --caves; no public cap index yet')
    ap.add_argument('--fair-schedule',action='store_true',help='private cave pilot: alternate expansion/updates with persistent retry delay; requires one worker/max-jobs 1')
    ap.add_argument('--cave-store-budget-mib',type=int,default=2048,help='fair cave pilot store budget, default 2048 MiB')
    ap.add_argument('--cave-reserve-gib',type=int,default=24,help='fair cave expansion minimum free space, default 24 GiB')
    ap.add_argument("--prepare-immutable", action="store_true",
                    help="private opt-in: stage <=128 tiles/64MiB after the build; requires owned bundle marker; never publishes")
    ap.add_argument("--lock-wait-seconds", type=_lock_wait_arg, default=0.0,
                    help=f"wait up to this many seconds (0..{MAX_LOCK_WAIT_SECONDS}) for a busy .build.lock, e.g. a "
                         "publisher tick; default 0 = skip at once as before; never waits without bound")
    a = ap.parse_args(argv)
    center = tuple(int(v) for v in a.center.split(","))
    a.out.mkdir(parents=True, exist_ok=True)
    lock = open(a.out / ".build.lock", "a")
    try:
        acquired = acquire_build_lock(lock, a.lock_wait_seconds)
    except OSError:                 # as before: any other lock error skips this run at once
        lock.close()
        print(json.dumps({"state": "skipped: another build holds the lock"}))
        return 0
    if not acquired:
        lock.close()
        if a.lock_wait_seconds == 0:
            print(json.dumps({"state": "skipped: another build holds the lock"}))
        else:
            print(json.dumps({"state": "skipped: another build still holds the lock after waiting",
                              "lockWaitSeconds": a.lock_wait_seconds}))
        return 0
    try:
        extra = ({'fair_schedule':True,'cave_store_budget_bytes':a.cave_store_budget_mib*1024**2,
                  'cave_reserve_bytes':a.cave_reserve_gib*1024**3} if a.fair_schedule else {})
        print(json.dumps(build(a.world, a.catalog_dir, a.out, a.workers, a.max_jobs, center,
                              immutable_prepare=a.prepare_immutable, caves=a.caves,cave_caps=a.cave_caps,**extra), indent=1))
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())

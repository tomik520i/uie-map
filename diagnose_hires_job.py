"""Diagnostics backfill for ONE existing, clean, current hires job: adds block diagnostics to its job record without
rendering geometry (no mesher, no tile writes or deletes, no index/atlas changes, no scheduler).

  nice -n 15 python3 diagnose_hires_job.py --job 1,-2 --world W --catalog-dir C --hires-store H --scratch-out S

Steps (all refusals leave every file as it was):
  1. paths disjoint (S outside W, H and C); the hires store's .build.lock is taken non-blocking (busy -> exit 3);
  2. catalog and level.dat registry are read once; the renderer identity is derived exactly like world_hires.build
     and must equal index.json's identity, and the catalog atlas must equal index.json's atlasSha256 and atlas.png;
  3. the job must be planned (its region file exists), its record clean (errors == []) and current (fingerprint ==
     sha256(identity + world_hires.fingerprint(job input incl. halo)) - the region-header guard, the best available
     check for an old record); a record with complete diagnostics is skipped (diagnostics.json is still refreshed);
  4. the level.dat registry digest must equal the registry loaded for the run; the stored chunk payloads of the job
     input are digested (collision_export.content_fingerprint); the region files are copied privately
     (copy_regions_bounded: budget 512 MiB for everything in S, free space floor 20 GiB, growth detected) and the
     copies must have the same payload digest; they are decoded row by row like _render_rows;
     a tile whose input window (core + halo) has an unreadable chunk FAILS (never air); a tile without any present
     core chunk gets the empty diagnostics; every other tile gets tile_diagnostics(stats=None);
  5. afterwards identity, atlas, header fingerprint, live payload digest, registry digest and the job file bytes are
     checked again; only then the job record is replaced atomically with all previous fields plus diagnostics /
     diagnosticsSource (/ diagnosticErrors);
  6. diagnostics.json is recomputed from the records of all planned jobs that are clean and current (others count as
     without diagnostics), index.json and the geometry stay untouched.

Exit codes: 0 written complete or skipped (summary refreshed), 2 written partial, 1 refused/failed or the summary
could not be refreshed, 3 lock busy.
The diagnostics describe the CURRENT observed stable world data. The existing geometry was rendered earlier and only
its region-header fingerprint was stored, so equivalence between these diagnostics and that geometry is unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

import collision_export as ce
import world_hires as wh
from world_diagnostics import (DiagnosticsError, empty_tile_diagnostics, job_diagnostics, tile_diagnostics,
                               validate_diagnostics, world_diagnostics)

MIB = 1024 * 1024
DEFAULT_BUDGET_BYTES = 512 * MIB
MIN_FREE_BYTES = 20 * 1024 * MIB
MAX_RECORD_BYTES = 8 * MIB
MAX_JOB_RECORDS = 100_000
MAX_ERRORS = 20
TILES_PER_JOB = (wh.JOB // wh.TILE) ** 2


class Refused(Exception):
    """Nothing is written; the reason is reported."""


# ------------------------------------------------------------------ identity and records

def current_identity(catalog) -> str:
    """The renderer identity exactly as world_hires.build computes it (catalog identity + output-defining code)."""
    code = wh.MAGIC + b"".join(Path(wh.__file__).with_name(n).read_bytes()
                               for n in ("offline_mesher.py", "offline_catalog.py", "offline_volume.py"))
    return hashlib.sha256(catalog.identity.encode() + hashlib.sha256(code).digest()).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_limited(path: Path, limit: int) -> bytes:
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Refused(f"{path.name} is larger than {limit} bytes")
    return raw


def check_renderer(hires_store: Path, catalog_dir: Path, identity: str) -> dict:
    """index.json must belong to this renderer identity and this catalog atlas."""
    try:
        index = json.loads(_read_limited(hires_store / "index.json", 64 * MIB))
    except (OSError, ValueError) as error:
        raise Refused(f"hires index unreadable: {error}") from None
    atlas = _sha_file(catalog_dir / "atlas.png")
    if not isinstance(index, dict) or index.get("format") != wh.FORMAT_INDEX or index.get("identity") != identity:
        raise Refused("renderer identity differs from the last published build (re-render first)")
    if index.get("atlasSha256") != atlas or _sha_file(hires_store / "atlas.png") != atlas:
        raise Refused("atlas differs between catalog, hires store and index")
    return {"identity": identity, "atlasSha256": atlas, "renderer": index.get("renderer")}


def expected_fingerprint(region_dir: Path, identity: str, jx: int, jz: int) -> str:
    area, _ = wh.job_area(jx, jz)
    return hashlib.sha256((identity + wh.fingerprint(region_dir, area)).encode()).hexdigest()


def load_record(path: Path):
    raw = _read_limited(path, MAX_RECORD_BYTES)
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError("job record is not an object")
    return raw, record


def has_complete_diagnostics(record: dict) -> bool:
    try:
        doc = validate_diagnostics(record.get("diagnostics"))
    except DiagnosticsError:
        return False
    return doc["scope"] == "job" and doc["coverage"]["complete"]


# ------------------------------------------------------------------ diagnostics of one job

def _tile_volume(t_area, t_core, window):
    """Volume of one tile input, exactly as _render_rows builds it (placeholder names for unknown ids)."""
    import capture_batch
    from offline_volume import Volume
    return Volume(wh._batch(capture_batch, t_area, t_core, window))


def diagnose_rows(copies: dict, area, core, catalog):
    """(tile docs, failures [[tx, tz, message]]) of every tile of the job core, row by row like _render_rows."""
    docs, failures = [], []
    for tz in range(core.z // wh.TILE, (core.z + core.depth) // wh.TILE):
        strip = wh._Rect(area.x, tz * wh.TILE - wh.HALO, area.width, wh.TILE + 2 * wh.HALO)
        unreadable = set()
        chunks = wh.read_chunks(copies, strip, failed=unreadable)
        for tx in range(core.x // wh.TILE, (core.x + core.width) // wh.TILE):
            t_area, t_core = wh.tile_area(tx, tz)
            inside = lambda k: (t_area.x >> 4 <= k[0] < (t_area.x + t_area.width) >> 4
                                and t_area.z >> 4 <= k[1] < (t_area.z + t_area.depth) >> 4)
            bad = sorted(k for k in unreadable if inside(k))
            if bad:   # strict: unreadable input (halo included) is never treated as air
                failures.append([tx, tz, f"source read failed for {len(bad)} chunk(s), e.g. {tuple(bad[0])}"])
                continue
            core_keys = {(cx, cz) for cx in range(t_core.x >> 4, (t_core.x + wh.TILE) >> 4)
                         for cz in range(t_core.z >> 4, (t_core.z + wh.TILE) >> 4)}
            try:
                if core_keys & set(chunks):
                    window = {k: c for k, c in chunks.items() if inside(k)}
                    docs.append(tile_diagnostics(_tile_volume(t_area, t_core, window), catalog, None))
                else:
                    docs.append(empty_tile_diagnostics())      # no core chunk exists: known absent, not unknown
            except Exception as error:
                failures.append([tx, tz, f"{type(error).__name__}: {error}"[:300]])
    return docs, failures


# ------------------------------------------------------------------ world diagnostics.json

def refresh_world_diagnostics(hires_store: Path, region_dir: Path, renderer: dict, planned, now=time.time) -> dict:
    """diagnostics.json from the records of all planned jobs that are clean and current (as build() would keep them);
    everything else counts as without diagnostics. index.json and the geometry are not touched."""
    if len(planned) > MAX_JOB_RECORDS:
        raise Refused("too many planned jobs")
    docs = []
    for jx, jz in planned:
        path = hires_store / "jobs" / f"{jx}_{jz}.json"
        try:
            _raw, record = load_record(path)
        except (OSError, ValueError, Refused):
            continue                                            # missing or unreadable: without diagnostics
        if record.get("errors") != [] or record.get("fingerprint") != expected_fingerprint(region_dir, renderer["identity"], jx, jz):
            continue
        item = record.get("diagnostics")
        try:
            item = validate_diagnostics(item) if item is not None and item.get("scope") == "job" else None
        except (DiagnosticsError, AttributeError):
            item = None
        docs.append(item)
    doc = world_diagnostics(docs, len(planned))
    doc.update(generatedAt=now(), atlasSha256=renderer["atlasSha256"], rendererIdentity=renderer["identity"],
               source="diagnose_hires_job backfill (clean, current job records only)")
    wh._atomic_write(hires_store / "diagnostics.json", json.dumps(doc, separators=(",", ":")).encode())
    return doc["jobs"]


# ------------------------------------------------------------------ one job

PROVENANCE_BASIS = ("current observed stable world data: the live region payload digest of the job input (halo "
                    "included) was equal before the copy, in the private copies and after processing, and the "
                    "level.dat block registry was equal before and after")
HISTORICAL_EQUIVALENCE = ("unknown: the existing geometry was rendered earlier and only its region header fingerprint "
                          "was stored; payload and registry used for that geometry cannot be proven equal")


def backfill_job(jx, jz, *, world, catalog_dir, hires_store, scratch_out, catalog, registry,
                 budget_bytes=DEFAULT_BUDGET_BYTES, min_free_bytes=MIN_FREE_BYTES,
                 disk_free=lambda p: shutil.disk_usage(p).free, now=time.time, load_registry=None) -> dict:
    """registry: the registry the diagnostics use (main reads it from level.dat once); load_registry(world) re-reads
    level.dat for the before/after registry digest (default collision_export.read_registry)."""
    world, catalog_dir, hires_store, scratch_out = (Path(p).resolve() for p in (world, catalog_dir, hires_store, scratch_out))
    load_registry = load_registry or ce.read_registry
    started = now()
    result = {"format": "UIE_DIAGNOSTIC_BACKFILL1", "job": [jx, jz], "state": None, "reason": None,
              "startedAt": started, "finishedAt": None, "seconds": None, "coveredTiles": 0,
              "expectedTiles": TILES_PER_JOB, "failedTiles": 0, "errors": [], "contentSha256": None,
              "registrySha256": None, "world": None, "worldRefreshed": False, "worldError": None}

    def refresh(region_dir, renderer, planned):
        """diagnostics.json is (re)published on every successful or skipped run, so a summary lost after an earlier
        run is repaired; a failure is reported, never hidden."""
        try:
            result["world"] = refresh_world_diagnostics(hires_store, region_dir, renderer, planned, now)
            result["worldRefreshed"] = True
        except Exception as error:
            result["worldError"] = f"diagnostics.json not refreshed: {type(error).__name__}: {error}"[:300]

    try:
        ce.check_paths(scratch_out, world=world, hires=hires_store, catalog=catalog_dir)
        region_dir = world / "region"
        identity = current_identity(catalog)
        renderer = check_renderer(hires_store, catalog_dir, identity)
        planned = wh.plan_jobs(region_dir)
        if (jx, jz) not in planned:
            raise Refused("the job has no region file (not planned)")
        path = hires_store / "jobs" / f"{jx}_{jz}.json"
        try:
            raw, record = load_record(path)
        except (OSError, ValueError) as error:
            raise Refused(f"job record unreadable: {error}") from None
        fp = expected_fingerprint(region_dir, identity, jx, jz)
        if record.get("job") != [jx, jz] or record.get("errors") != [] or record.get("fingerprint") != fp:
            raise Refused("the job record is not a clean record of the current source (render it first)")
        if has_complete_diagnostics(record):
            result.update(state="skipped", reason="the job already has complete diagnostics")
            refresh(region_dir, renderer, planned)
            return result

        # The registry the diagnostics use must be the one level.dat holds now (and still holds at the end).
        registry_sha = ce.registry_sha256(registry)
        if ce.registry_sha256(load_registry(world)) != registry_sha:
            raise Refused("level.dat block registry differs from the registry loaded for this run")
        result["registrySha256"] = registry_sha
        wh._W["registry"] = dict(registry)                 # world_hires._batch reads (and extends) it from here
        area, core = wh.job_area(jx, jz)
        # Strong evidence: digest of the stored chunk payloads of the job input (halo included), not just headers.
        live_before = ce.content_fingerprint(region_dir, area)
        scratch_out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch_out, prefix=".diagnose-") as scratch:
            copies = ce.copy_regions_bounded(region_dir, Path(scratch), area, out=scratch_out,
                                             budget_bytes=budget_bytes, min_free_bytes=min_free_bytes,
                                             disk_free=disk_free)
            try:
                if ce.content_fingerprint(Path(scratch), area) != live_before:
                    raise Refused("the private copies differ from the observed live source")
                docs, failures = diagnose_rows(copies, area, core, catalog)
            finally:
                for copy in copies.values():
                    copy.unlink(missing_ok=True)

        # Nothing may have changed while the copies were decoded.
        if check_renderer(hires_store, catalog_dir, current_identity(catalog)) != renderer:
            raise Refused("renderer or atlas changed during the backfill")
        if expected_fingerprint(region_dir, identity, jx, jz) != fp:
            raise Refused("the job source changed during the backfill (region headers)")
        if ce.content_fingerprint(region_dir, area) != live_before:
            raise Refused("the job source changed during the backfill (chunk payloads)")
        if ce.registry_sha256(load_registry(world)) != registry_sha:
            raise Refused("level.dat block registry changed during the backfill")
        result["contentSha256"] = live_before
        diagnostics = job_diagnostics(docs, TILES_PER_JOB)
        updated = dict(record)                             # every previous field and value stays
        updated["diagnostics"] = diagnostics
        updated["diagnosticsSource"] = {
            "mode": "backfill", "computedAt": now(), "basis": PROVENANCE_BASIS,
            "contentSha256": live_before, "registrySha256": registry_sha,
            "historicalGeometryEquivalence": HISTORICAL_EQUIVALENCE,
            "headerGuard": {"sourceFingerprint": fp,
                            "meaning": "existing job fingerprint (region header timestamps and allocation) matched "
                                       "before and after: the best available guard for the old job, not proof"},
            "coveredTiles": len(docs), "failedTiles": len(failures)}
        if failures:
            updated["diagnosticErrors"] = failures[:MAX_ERRORS]
        else:
            updated.pop("diagnosticErrors", None)
        if path.read_bytes() != raw:
            raise Refused("the job record changed during the backfill")
        wh._atomic_write(path, json.dumps(updated, separators=(",", ":")).encode())
        result.update(coveredTiles=len(docs), failedTiles=len(failures), errors=failures[:MAX_ERRORS],
                      state="written" if diagnostics["coverage"]["complete"] else "partial",
                      reason=None if not failures else f"{len(failures)} tiles could not be diagnosed")
        refresh(region_dir, renderer, planned)   # the job record is written; a failed summary is reported
    except (Refused, ce.ExportError) as error:
        result.update(state="refused", reason=str(error)[:300])
    except ce.TileError as error:            # copy budget / free space / unstable region: nothing written
        result.update(state="refused", reason=f"{type(error).__name__}: {error}"[:300])
    except Exception as error:               # unexpected: reported as failed, the job record was not replaced
        if result["state"] is None:
            result.update(state="failed", reason=f"{type(error).__name__}: {error}"[:300])
        else:
            raise
    finally:
        result["finishedAt"] = now()
        result["seconds"] = round(result["finishedAt"] - started, 3)
    return result


def _lock(hires_store: Path):
    handle = open(hires_store / ".build.lock", "a")
    try:
        import fcntl
    except ImportError:
        return handle                        # Windows (tests): no concurrent builder there
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--job", required=True, help="jx,jz of ONE region job")
    ap.add_argument("--world", required=True, type=Path)
    ap.add_argument("--catalog-dir", required=True, type=Path)
    ap.add_argument("--hires-store", required=True, type=Path)
    ap.add_argument("--scratch-out", required=True, type=Path)
    a = ap.parse_args(argv)
    try:
        jx, jz = (int(v) for v in a.job.split(","))
        if f"{jx},{jz}" != a.job:
            raise ValueError
    except ValueError:
        print(json.dumps({"state": "refused", "reason": "--job must be jx,jz"}))
        return 1
    try:
        ce.check_paths(a.scratch_out, world=a.world, hires=a.hires_store, catalog=a.catalog_dir)
    except ce.ExportError as error:
        print(json.dumps({"state": "refused", "reason": str(error)}))
        return 1
    if hasattr(os, "nice"):
        os.nice(15)
    lock = _lock(a.hires_store)
    if lock is None:
        print(json.dumps({"state": "skipped", "reason": "a hires build holds the lock"}))
        return 3
    try:
        from offline_catalog import Catalog
        catalog = Catalog.load_compiled(a.catalog_dir / "catalog.json")
        registry = ce.read_registry(a.world)
        result = backfill_job(jx, jz, world=a.world, catalog_dir=a.catalog_dir, hires_store=a.hires_store,
                              scratch_out=a.scratch_out, catalog=catalog, registry=registry)
    finally:
        lock.close()
    print(json.dumps(result, indent=1))
    if result["state"] in ("written", "skipped", "partial") and not result["worldRefreshed"]:
        return 1                              # the job may be written, but the published summary is not current
    return {"written": 0, "skipped": 0, "partial": 2}.get(result["state"], 1)


if __name__ == "__main__":
    sys.exit(main())

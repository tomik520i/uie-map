"""Area provenance record of ONE finite, verified warm-plan attempt (UIE_AREA_RECORD1). Stdlib only.

    record = build_area_record(plan_dir, verified_raw, verified_sha256=..., job_id=..., attempt=...)
    record = validate_area_record(record, expected=None)    # strict; returns a normalized independent copy

The builder re-verifies the attempt's staged plan strictly, requires the report to equal the one inside the
VERIFIED.json envelope, and records each job ("tile") from the parsed UIE2 snapshot and the verified manifest.
Trust boundary: the record is only as authentic as the caller's binding of verified_sha256 to the queue's
succeeded result; validate_area_record checks structure and internal consistency, not authenticity.
Nothing is published, merged or derived from published generation blobs. See AREA-RECORDS.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import capture_batch as cb
import verify_rendered_plan as vrp
from publish_generation import OBSERVED_STABLE, PublicationError, staged_source_record

FORMAT = "UIE_AREA_RECORD1"
VERIFIED_FORMAT = "UIE_WARM_VERIFIED1"   # warm_worker.VERIFIED_FORMAT
MARKER = "PLAN-COMPLETE.json"            # warm_worker.MARKER
HEX64 = re.compile(r"[0-9a-f]{64}")
JOB_ID = re.compile(r"[a-zA-Z0-9_-]{1,80}")
CHUNK = 16
GRID = 96                                # capture_batch_file.tile_areas: 6x6-chunk cores on a 96-block grid
MAX_CORE = 96                            # UIE2: input <= 128 blocks with a >= 16-block halo on every side
INT32 = (-2 ** 31, 2 ** 31 - 1)          # UIE2 stores the core as signed int32 x, z, width, depth
RECORD_KEYS = {"format", "jobId", "attempt", "verifiedSha256", "planSha256", "pipelineKey",
               "pipelineIdentitySha256", "atlasSha256", "layoutIdentitySha256", "bakedLighting", "tiles", "aggregate"}
TILE_KEYS = {"job", "snapshotSha256", "core", "gridTile", "coreChunks", "present", "absent", "manifestSha256",
             "bounds", "coverage", "generatedAt", "stagedSourceSha256", "sourceInterval"}
COVERAGE_KEYS = {"renderedBlocks", "unsupportedBlocks", "emptyBlocks", "unsupportedNames"}
AGGREGATE_KEYS = {"chunks", "bounds", "coverage", "generatedAt"}
EXPECTED_KEYS = {"pipelineKey", "atlasSha256", "layoutIdentitySha256", "bakedLighting", "publishedChunks",
                 "snapshots"}


class AreaRecordError(ValueError):
    """The area record cannot be built or is not valid."""


# ------------------------------------------------------------------ small checks

def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _same(a, b) -> bool:
    """Type-exact equality (True != 1, 1.0 != 1): compares canonical JSON, not Python ==."""
    dump = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return dump(a) == dump(b)


def _pair(value) -> bool:
    return type(value) is list and len(value) == 2 and all(type(v) is int for v in value)


def _hex(value, where: str) -> str:
    if type(value) is not str or not HEX64.fullmatch(value):
        raise AreaRecordError(f"{where} must be 64 lowercase hex")
    return value


def _int(value, where: str, low=0) -> int:
    if type(value) is not int or value < low or value > 2 ** 53 - 1:
        raise AreaRecordError(f"{where} must be an integer >= {low}")
    return value


def _time(value, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise AreaRecordError(f"{where} must be a timezone-aware ISO timestamp")
    return parsed


def _bounds(value, where: str) -> dict:
    if type(value) is not dict or set(value) != {"min", "max"} or not all(
            type(value[k]) is list and len(value[k]) == 3
            and all(type(v) in (int, float) and math.isfinite(v) for v in value[k]) for k in ("min", "max")) \
            or any(a > b for a, b in zip(value["min"], value["max"])):
        raise AreaRecordError(f"{where} must be finite {{min, max}} with min <= max")
    return value


def _coverage(value, where: str) -> dict:
    if type(value) is not dict or set(value) != COVERAGE_KEYS:
        raise AreaRecordError(f"{where} must be exactly {sorted(COVERAGE_KEYS)}")
    for key in ("renderedBlocks", "unsupportedBlocks", "emptyBlocks"):
        _int(value[key], f"{where}.{key}")
    names = value["unsupportedNames"]
    if type(names) is not list or not all(type(n) is str and n for n in names) or names != sorted(set(names)):
        raise AreaRecordError(f"{where}.unsupportedNames must be sorted unique non-empty strings")
    return value


def _chunks(value, where: str) -> list:
    if type(value) is not list or not all(_pair(c) for c in value):
        raise AreaRecordError(f"{where} must be a list of [x, z] integer pairs")
    if value != sorted(value, key=lambda c: (c[1], c[0])) or len({tuple(c) for c in value}) != len(value):
        raise AreaRecordError(f"{where} must be sorted by (z, x) without duplicates")
    return value


def _sorted_chunks(chunks) -> list:
    return [list(c) for c in sorted(chunks, key=lambda c: (c[1], c[0]))]


def _core_chunks(core: dict) -> list:
    return _sorted_chunks((cx, cz) for cz in range(core["z"] // CHUNK, (core["z"] + core["depth"]) // CHUNK)
                          for cx in range(core["x"] // CHUNK, (core["x"] + core["width"]) // CHUNK))


def _grid_tile(core: dict):
    """[tx, tz] when the core is exactly a capture_batch_file.tile_areas core, else None (never assumed)."""
    if core["width"] == core["depth"] == GRID and core["x"] % GRID == 0 and core["z"] % GRID == 0:
        return [core["x"] // GRID, core["z"] // GRID]
    return None


def _aggregate(tiles: list) -> dict:
    mins = [min(t["bounds"]["min"][i] for t in tiles) for i in range(3)]
    maxs = [max(t["bounds"]["max"][i] for t in tiles) for i in range(3)]
    coverage = {key: sum(t["coverage"][key] for t in tiles) for key in ("renderedBlocks", "unsupportedBlocks", "emptyBlocks")}
    coverage["unsupportedNames"] = sorted({n for t in tiles for n in t["coverage"]["unsupportedNames"]})
    latest = max(tiles, key=lambda t: (_time(t["generatedAt"], "generatedAt"), t["job"]))["generatedAt"]
    return {"chunks": sum(len(t["present"]) for t in tiles), "bounds": {"min": mins, "max": maxs},
            "coverage": coverage, "generatedAt": latest}


# ------------------------------------------------------------------ validation

def validate_area_record(record, *, expected=None) -> dict:
    """Strict structural and consistency check; returns an independent normalized copy.

    expected (optional) may give any of: pipelineKey, atlasSha256, layoutIdentitySha256, bakedLighting,
    publishedChunks ([[x, z], ...], must equal the union of present chunks) and snapshots ([sha, ...])."""
    try:
        record = json.loads(json.dumps(record, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise AreaRecordError(f"record is not plain JSON: {exc}") from None
    if type(record) is not dict or set(record) != RECORD_KEYS or record["format"] != FORMAT:
        raise AreaRecordError(f"record must be exactly {sorted(RECORD_KEYS)} with format {FORMAT}")
    if type(record["jobId"]) is not str or not HEX64.fullmatch(record["jobId"]):
        raise AreaRecordError("jobId must be 64 lowercase hex (a queue job id)")
    _int(record["attempt"], "attempt", 1)
    for key in ("verifiedSha256", "planSha256", "pipelineKey", "pipelineIdentitySha256", "atlasSha256",
                "layoutIdentitySha256"):
        _hex(record[key], key)
    if type(record["bakedLighting"]) is not bool:
        raise AreaRecordError("bakedLighting must be a boolean")
    tiles = record["tiles"]
    if type(tiles) is not list or not tiles:
        raise AreaRecordError("tiles must be a non-empty list")
    owners, snapshots = {}, set()
    for i, tile in enumerate(tiles):
        where = f"tiles[{i}]"
        if type(tile) is not dict or set(tile) != TILE_KEYS:
            raise AreaRecordError(f"{where} must be exactly {sorted(TILE_KEYS)}")
        if type(tile["job"]) is not str or not JOB_ID.fullmatch(tile["job"]):
            raise AreaRecordError(f"{where}.job is not a plan job id")
        for key in ("snapshotSha256", "manifestSha256"):
            _hex(tile[key], f"{where}.{key}")
        if tile["snapshotSha256"] in snapshots:
            raise AreaRecordError(f"{where}: snapshot listed twice")
        snapshots.add(tile["snapshotSha256"])
        core = tile["core"]
        if type(core) is not dict or set(core) != {"x", "z", "width", "depth"} \
                or not all(type(core[k]) is int and core[k] % CHUNK == 0 for k in core) \
                or not all(CHUNK <= core[k] <= MAX_CORE for k in ("width", "depth")) \
                or not all(INT32[0] <= core[a] and core[a] + core[s] - 1 <= INT32[1]
                           for a, s in (("x", "width"), ("z", "depth"))):
            raise AreaRecordError(f"{where}.core must be a 16-aligned int32 rectangle of 16..{MAX_CORE} blocks")
        grid = tile["gridTile"]
        if not (grid is None or _pair(grid)) or not _same(grid, _grid_tile(core)):
            raise AreaRecordError(f"{where}.gridTile must be null or the [tx, tz] integers of its core")
        expected_core = _core_chunks(core)
        if _chunks(tile["coreChunks"], f"{where}.coreChunks") != expected_core:
            raise AreaRecordError(f"{where}.coreChunks are not exactly the chunks of its core")
        present, absent = _chunks(tile["present"], f"{where}.present"), _chunks(tile["absent"], f"{where}.absent")
        if not present or {tuple(c) for c in present} & {tuple(c) for c in absent} \
                or _sorted_chunks([*map(tuple, present), *map(tuple, absent)]) != expected_core:
            raise AreaRecordError(f"{where}: present and absent must partition the core (present non-empty)")
        for c in expected_core:
            if tuple(c) in owners:
                raise AreaRecordError(f"{where}: core chunk {c} also belongs to {owners[tuple(c)]}")
            owners[tuple(c)] = tile["job"]
        _bounds(tile["bounds"], f"{where}.bounds")
        _coverage(tile["coverage"], f"{where}.coverage")
        _time(tile["generatedAt"], f"{where}.generatedAt")
        staged, interval = tile["stagedSourceSha256"], tile["sourceInterval"]
        if staged is not None:
            _hex(staged, f"{where}.stagedSourceSha256")
        if interval is not None:   # an interval only ever comes from a staged-source sidecar
            if staged is None:
                raise AreaRecordError(f"{where}: sourceInterval without a staged-source sidecar")
            _interval(interval, f"{where}.sourceInterval")
    if [t["job"] for t in tiles] != sorted(t["job"] for t in tiles) or len({t["job"] for t in tiles}) != len(tiles):
        raise AreaRecordError("tiles must be sorted by unique job id")
    aggregate = record["aggregate"]
    if type(aggregate) is not dict or set(aggregate) != AGGREGATE_KEYS:
        raise AreaRecordError(f"aggregate must be exactly {sorted(AGGREGATE_KEYS)}")
    _int(aggregate["chunks"], "aggregate.chunks", 1)   # strict types first: False must not pass as 0
    _bounds(aggregate["bounds"], "aggregate.bounds")
    _coverage(aggregate["coverage"], "aggregate.coverage")
    _time(aggregate["generatedAt"], "aggregate.generatedAt")
    if not _same(aggregate, _aggregate(tiles)):
        raise AreaRecordError("aggregate is not exactly recomputed from the tiles")
    if expected is not None:
        _check_expected(record, expected)
    return record


def _interval(value, where: str) -> dict:
    if type(value) is not dict or set(value) != {"startUtc", "endUtc"} \
            or _time(value["startUtc"], where) > _time(value["endUtc"], where):
        raise AreaRecordError(f"{where} must be exactly {{startUtc <= endUtc}} with timezone-aware times")
    return value


def _check_expected(record: dict, expected) -> None:
    """Every expected value is type-checked first; comparisons are type-exact; errors are AreaRecordError."""
    if type(expected) is not dict or not set(expected) <= EXPECTED_KEYS:
        raise AreaRecordError(f"expected must be an object with keys from {sorted(EXPECTED_KEYS)}")
    for key in ("pipelineKey", "atlasSha256", "layoutIdentitySha256"):
        if key in expected and _hex(expected[key], f"expected.{key}") != record[key]:
            raise AreaRecordError(f"{key} differs from the expected context")
    if "bakedLighting" in expected:
        if type(expected["bakedLighting"]) is not bool:
            raise AreaRecordError("expected.bakedLighting must be a boolean")
        if expected["bakedLighting"] is not record["bakedLighting"]:
            raise AreaRecordError("bakedLighting differs from the expected context")
    if "publishedChunks" in expected:
        published = expected["publishedChunks"]
        if type(published) is not list or not all(_pair(list(c)) if type(c) in (list, tuple) else False
                                                  for c in published):
            raise AreaRecordError("expected.publishedChunks must be a list of [x, z] integer pairs")
        given = [tuple(c) for c in published]
        present = {tuple(c) for t in record["tiles"] for c in t["present"]}
        if len(set(given)) != len(given) or set(given) != present:
            raise AreaRecordError("the present chunks are not exactly the expected published chunks")
    if "snapshots" in expected:
        snapshots = expected["snapshots"]
        if type(snapshots) is not list or not all(type(s) is str and HEX64.fullmatch(s) for s in snapshots):
            raise AreaRecordError("expected.snapshots must be a list of 64-hex hashes")
        if sorted(snapshots) != sorted(t["snapshotSha256"] for t in record["tiles"]):
            raise AreaRecordError("the tile snapshots are not exactly the expected snapshots")


# ------------------------------------------------------------------ builder

def _read(base: Path, relative: str, limit: int) -> bytes:
    try:
        return vrp._read(base, relative, limit)
    except vrp.RenderVerificationError as exc:
        raise AreaRecordError(str(exc)) from None


def _load(base: Path, relative: str, limit: int = vrp.LIMIT_JSON):
    try:
        return vrp._json(_read(base, relative, limit), relative)
    except vrp.RenderVerificationError as exc:
        raise AreaRecordError(str(exc)) from None


def _fresh_report(plan: Path, envelope: dict) -> dict:
    try:
        allowed = isinstance(envelope.get("report"), dict) and envelope["report"].get("unsupportedPolicy") == "allowed"
        report = vrp.verify_rendered_plan(plan, expected_plan_sha256=envelope["planSha256"], marker_name=MARKER,
                                          allow_unsupported=allowed)
    except vrp.RenderVerificationError as exc:
        raise AreaRecordError(f"strict verification failed: {exc}") from None
    if report != envelope["report"] or report.get("verified") is not True \
            or report.get("pipelineKeySource") != "declared-inputs-sha256" \
            or report.get("pipelineKey") != envelope["pipelineKey"] \
            or not all(isinstance(j.get("geometryFiles"), dict) and j["geometryFiles"] for j in report["perJob"]):
        raise AreaRecordError("the fresh strict report is not verified, lacks per-file hashes or differs from "
                              "the VERIFIED.json report")
    return report


def _tile(plan: Path, job: dict, info: dict, files: dict, marker: dict, per_job: dict) -> dict:
    snapshot = _read(plan, job["input"], vrp.LIMIT_SNAPSHOT)
    if _sha(snapshot) != job["snapshotSha256"] or marker.get(job["input"]) != job["snapshotSha256"]:
        raise AreaRecordError(f"job {job['id']}: the snapshot is not the verified input")
    try:
        batch = cb.parse_batch(snapshot)
    except Exception as exc:
        raise AreaRecordError(f"job {job['id']}: the snapshot cannot be parsed: {exc}") from None
    core = {"x": batch.core.x, "z": batch.core.z, "width": batch.core.width, "depth": batch.core.depth}
    core_chunks = _core_chunks(core)
    present = _sorted_chunks(c for c in map(tuple, core_chunks) if c in batch.present_chunks)
    absent = _sorted_chunks(c for c in map(tuple, core_chunks) if c not in batch.present_chunks)
    if present != _sorted_chunks(map(tuple, info["presentCoreChunks"])) or len(present) != per_job["chunks"]:
        raise AreaRecordError(f"job {job['id']}: jobs.json or the report disagrees with the parsed snapshot")
    base = f"results/{job['id']}/geometry"
    manifest_raw = _read(plan, f"{base}/manifest.json", vrp.LIMIT_JSON)
    if _sha(manifest_raw) != files.get("manifest.json"):
        raise AreaRecordError(f"job {job['id']}: manifest.json changed since verification")
    manifest = _parse(manifest_raw, f"job {job['id']}: manifest.json")
    coverage = manifest.get("coverage")
    names = coverage.get("unsupportedNames") if isinstance(coverage, dict) else None
    if not isinstance(coverage, dict) or type(names) is not list or not all(type(n) is str for n in names):
        raise AreaRecordError(f"job {job['id']}: manifest coverage is malformed")
    tile = {"job": job["id"], "snapshotSha256": job["snapshotSha256"], "core": core, "gridTile": _grid_tile(core),
            "coreChunks": core_chunks, "present": present, "absent": absent, "manifestSha256": files["manifest.json"],
            "bounds": manifest.get("bounds"),
            "coverage": {"renderedBlocks": coverage.get("renderedBlocks"),
                         "unsupportedBlocks": coverage.get("unsupportedBlocks"),
                         "emptyBlocks": coverage.get("emptyBlocks", 0),   # optional in the manifest contract
                         "unsupportedNames": sorted(set(names))},
            "generatedAt": manifest.get("generatedAt"), "stagedSourceSha256": None, "sourceInterval": None}
    policy = info.get("sourcePolicy")
    if isinstance(policy, dict) and policy.get("stagedSource") is True:
        relative = f"inputs/{job['id']}/staged-source.json"
        raw = _read(plan, relative, vrp.LIMIT_JSON)
        digest = _sha(raw)
        if digest != marker.get(relative) or digest != policy.get("stagedSourceSha256"):
            raise AreaRecordError(f"job {job['id']}: staged-source sidecar is not bound to the plan")
        tile["stagedSourceSha256"] = digest
        tile["sourceInterval"] = _staged_interval(raw, digest, job)
    return tile


def _parse(raw: bytes, label: str) -> dict:
    try:
        doc = vrp._json(raw, label)
    except vrp.RenderVerificationError as exc:
        raise AreaRecordError(str(exc)) from None
    if type(doc) is not dict:
        raise AreaRecordError(f"{label} is not a JSON object")
    return doc


def _staged_interval(raw: bytes, digest: str, job: dict):
    """The sidecar's source interval, or None when it records none (unknown; never invented).

    With an interval, the publisher's own validator checks the whole sidecar. Without one (prepare_warm_plan
    accepts that; publish_generation.staged_source_record currently refuses it), the same policy and snapshot
    checks are applied here. A present but malformed interval is refused."""
    doc = _parse(raw, f"job {job['id']}: staged-source.json")
    tile = doc.get("stagedTile")
    interval = tile.get("sourceInterval") if isinstance(tile, dict) else None
    if interval is not None:
        try:   # observed-stable-files, non-atomic, bound to this snapshot, valid interval
            staged_source_record({"file": "staged-source.json", "sha256": digest}, raw, job["snapshotSha256"])
        except PublicationError as exc:
            raise AreaRecordError(f"job {job['id']}: {exc}") from None
        return dict(_interval(interval, f"job {job['id']}: sourceInterval"))
    policy, batch = doc.get("policy"), doc.get("batch")
    files = batch.get("files") if isinstance(batch, dict) else None
    valid = (doc.get("format") == "UIE_STAGED_SOURCE1" and isinstance(policy, dict) and isinstance(tile, dict)
             and policy.get("acceptedConsistency") == OBSERVED_STABLE and policy.get("atomicWorldSnapshot") is False
             and tile.get("consistency") == OBSERVED_STABLE and tile.get("atomicWorldSnapshot") is False
             and isinstance(doc.get("warning"), str) and doc["warning"].strip()
             and isinstance(batch, dict) and batch.get("snapshotSha256") == job["snapshotSha256"]
             and isinstance(files, dict) and files.get("snapshot.bin") == job["snapshotSha256"])
    if not valid:
        raise AreaRecordError(f"job {job['id']}: staged-source sidecar is not an observed-stable-files, "
                              f"non-atomic record of this snapshot")
    return None


def build_area_record(plan_dir, verified_raw: bytes, *, verified_sha256: str, job_id: str, attempt: int) -> dict:
    """Area record of one verified attempt. verified_raw are the exact VERIFIED.json bytes, verified_sha256 the
    hash from the queue's succeeded result (the caller is responsible for that binding)."""
    if type(verified_raw) is not bytes or _sha(verified_raw) != _hex(verified_sha256, "verified_sha256"):
        raise AreaRecordError("VERIFIED.json bytes do not hash to verified_sha256")
    try:
        envelope = vrp._json(verified_raw, "VERIFIED.json")
    except vrp.RenderVerificationError as exc:
        raise AreaRecordError(str(exc)) from None
    if type(envelope) is not dict or set(envelope) != {"format", "jobId", "attempt", "planSha256", "pipelineKey",
                                                       "report"} \
            or envelope["format"] != VERIFIED_FORMAT or envelope["jobId"] != job_id \
            or type(envelope["attempt"]) is not int or envelope["attempt"] != attempt \
            or not all(type(envelope[k]) is str and HEX64.fullmatch(envelope[k]) for k in ("planSha256", "pipelineKey")):
        raise AreaRecordError("VERIFIED.json is not the envelope of this job and attempt")
    if not isinstance(plan_dir, (str, os.PathLike)) or not os.path.isabs(plan_dir) \
            or os.path.realpath(plan_dir) != str(Path(plan_dir)) or not os.path.isdir(plan_dir):
        raise AreaRecordError("plan_dir must be a canonical existing directory")
    plan = Path(plan_dir)
    report = _fresh_report(plan, envelope)
    plan_jobs, detail = _load(plan, "plan.json")["jobs"], _load(plan, "jobs.json")["jobs"]
    marker = _load(plan, MARKER)["files"]
    per_job = {j["id"]: j for j in report["perJob"]}
    if [j["id"] for j in plan_jobs] != [d["id"] for d in detail] or sorted(per_job) != sorted(j["id"] for j in plan_jobs):
        raise AreaRecordError("plan.json, jobs.json and the report do not list the same jobs")
    tiles = sorted((_tile(plan, job, info, per_job[job["id"]]["geometryFiles"], marker, per_job[job["id"]])
                    for job, info in zip(plan_jobs, detail)), key=lambda t: t["job"])
    record = {"format": FORMAT, "jobId": job_id, "attempt": attempt, "verifiedSha256": verified_sha256,
              "planSha256": envelope["planSha256"], "pipelineKey": envelope["pipelineKey"],
              "pipelineIdentitySha256": report.get("pipelineIdentitySha256"), "atlasSha256": report["atlasSha256"],
              "layoutIdentitySha256": report["layoutIdentitySha256"], "bakedLighting": report["bakedLighting"],
              "tiles": tiles}
    try:
        record["aggregate"] = _aggregate(tiles)
    except (AreaRecordError, KeyError, TypeError, ValueError) as exc:
        raise AreaRecordError(f"tile metadata is incomplete: {exc}") from None
    record = validate_area_record(record, expected={"publishedChunks": [c for t in tiles for c in t["present"]],
                                                    "snapshots": [j["snapshotSha256"] for j in plan_jobs]})
    if _fresh_report(plan, envelope) != report:   # nothing changed while the record was built
        raise AreaRecordError("the attempt changed while its area record was built")
    return record

"""Independent read-only verification of a finished finite warm-plan render.

verify_rendered_plan(root, *, expected_plan_sha256, marker_name="PLAN-COMPLETE.json",
                     allow_unsupported=False) -> report

root is a prepare_warm_plan output (plan.json, jobs.json, inputs/<id>/..., its COMPLETE copied as
marker_name) after WarmBatch ran: results/<id>/receipt.json and results/<id>/geometry/ (GeometryCapture).

Integrity failures raise RenderVerificationError: plan SHA, marker hashes of every copied input and
jobs.json, plan/jobs correspondence, UIE2 inputs, receipts (captured, same id/snapshot, manifest SHA),
geometry manifests (UIE2 snapshot binding, chunk set exactly the input's present core chunks, meshes valid),
one atlas PNG for all jobs with a bound atlas layout (atlas_identity.load_layout), consistent bakedLighting.

Unsupported blocks are not an integrity failure: they are reported, and verified is False unless
allow_unsupported=True. Empty chunks and empty-block counts are reported, never a failure.

The report states what was checked. It carries each job's sourcePolicy from jobs.json unchanged and does
not assert global world coherence. Nothing is copied, rewritten, published or launched.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
from pathlib import Path

import atlas_identity
import capture_batch as cb
import capture_snapshot as cs
import pipeline_identity
from publish_generation import PublicationError, validate_mesh

IDENTITY_FILE = "pipeline-identity.json"   # optional binding written by prepare_warm_plan(pipeline_identity=...)

REPORT_FORMAT = "UIE_RENDERED_PLAN_REPORT1"
HEX64 = re.compile(r"[0-9a-f]{64}")
SAFE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
LIMIT_JSON = 16 * 1024 * 1024
LIMIT_SNAPSHOT = 64 * 1024 * 1024
LIMIT_TILES = 64 * 1024 * 1024  # snapshot.tiles.json, as prepare_warm_plan.LIMITS
LIMIT_MESH = 128 * 1024 * 1024
BLOCK = 1 << 20
GEOMETRY_FIXED = {"manifest.json", "atlas.png", "atlas-layout.json", "unsupported.json", "empty.json"}
NOTE = ("Verified: plan and input hashes, receipts, geometry manifests against the input UIE2 present core "
        "chunks, mesh structure and a single bound atlas. Not verified: that the rendered faces are correct, or "
        "that the world sources were globally coherent (see each job's sourcePolicy).")


class RenderVerificationError(ValueError):
    """The rendered plan does not match its inputs or is malformed."""


def _redirected(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_root(root: Path) -> None:
    try:
        for entry in (root, *root.parents):
            if _redirected(os.lstat(entry)):
                raise RenderVerificationError(f"{entry} is a symlink or reparse point; pass the real path")
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise RenderVerificationError(f"{root} is not a directory")
    except FileNotFoundError:
        raise RenderVerificationError(f"{root} does not exist") from None


def _path(base: Path, relative: str, directory: bool = False) -> Path:
    """base/relative with every component a real, non-link entry (the last one a regular file, or a
    directory when directory=True)."""
    if not isinstance(relative, str) or not SAFE.fullmatch(relative) or any(p in (".", "..") for p in relative.split("/")):
        raise RenderVerificationError(f"unsafe relative path {relative!r}")
    path = base
    parts = relative.split("/")
    for i, part in enumerate(parts):
        path = path / part
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise RenderVerificationError(f"{path} is missing") from None
        last_is_file = i == len(parts) - 1 and not directory
        if _redirected(info) or not (stat.S_ISREG(info.st_mode) if last_is_file else stat.S_ISDIR(info.st_mode)):
            raise RenderVerificationError(f"{path} is a link or has the wrong type")
    return path


def _read(base: Path, relative: str, limit: int) -> bytes:
    path = _path(base, relative)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise RenderVerificationError(f"{path} is larger than {limit} bytes")
        # Read the actual size (+1 to detect growth): read(limit + 1) preallocates the whole limit on Windows.
        size = os.fstat(stream.fileno()).st_size
        raw = stream.read(size + 1)
        if len(raw) > size:
            raw += stream.read(limit + 1 - len(raw))
    if len(raw) > limit:
        raise RenderVerificationError(f"{path} is larger than {limit} bytes")
    return raw


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RenderVerificationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _no_constant(name):
    raise RenderVerificationError(f"non-finite JSON number {name}")


def _json(raw: bytes, label: str):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderVerificationError(f"{label}: not UTF-8 JSON ({exc})") from None


def _entries(directory: Path) -> dict:
    result = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            info = os.lstat(entry.path)
            if _redirected(info):
                raise RenderVerificationError(f"{entry.path} is a symlink or reparse point")
            result[entry.name] = info
    return result


def _int(value) -> bool:
    return type(value) is int


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _chunks(a: cs.Area) -> set:
    return {(cx, cz) for cz in range(a.z >> 4, (a.z + a.depth) >> 4) for cx in range(a.x >> 4, (a.x + a.width) >> 4)}


def _triangles(mesh: dict) -> int:
    return sum(len(part["indices"]) // 3 for part in [mesh, *mesh.get("transparentParts", [])])


def _verify_plan(root: Path, expected_plan_sha256: str, marker_name: str) -> tuple[list, list, dict]:
    marker = _json(_read(root, marker_name, LIMIT_JSON), marker_name)
    if not isinstance(marker, dict) or set(marker) != {"format", "files"} or marker["format"] != "UIE_WARM_PLAN_COMPLETE1" \
            or not isinstance(marker["files"], dict) or not all(_sha_ok(v) for v in marker["files"].values()):
        raise RenderVerificationError(f"{marker_name} is not a UIE_WARM_PLAN_COMPLETE1 marker")
    files = marker["files"]
    if files.get("plan.json") != expected_plan_sha256:
        raise RenderVerificationError("plan.json in the marker is not the expected plan")
    for relative, digest in files.items():
        limit = (LIMIT_SNAPSHOT if relative.endswith(".bin")
                 else LIMIT_TILES if relative.endswith("/snapshot.tiles.json") else LIMIT_JSON)
        if _sha(_read(root, relative, limit)) != digest:
            raise RenderVerificationError(f"{relative} does not match {marker_name}")
    plan = _json(_read(root, "plan.json", LIMIT_JSON), "plan.json")
    jobs_doc = _json(_read(root, "jobs.json", LIMIT_JSON), "jobs.json")
    if not isinstance(plan, dict) or plan.get("format") != "UIE_WARM_BATCH1" or not isinstance(plan.get("jobs"), list) \
            or not 1 <= len(plan["jobs"]) <= 256:
        raise RenderVerificationError("plan.json is not a UIE_WARM_BATCH1 plan with 1..256 jobs")
    if not isinstance(jobs_doc, dict) or jobs_doc.get("format") != "UIE_WARM_JOBS1" or not isinstance(jobs_doc.get("jobs"), list):
        raise RenderVerificationError("jobs.json is not UIE_WARM_JOBS1")
    plan_jobs, detail = plan["jobs"], jobs_doc["jobs"]
    ids = [j.get("id") if isinstance(j, dict) else None for j in plan_jobs]
    # Types first: a non-string id (e.g. a list) must be refused, not break set().
    if not all(isinstance(i, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", i) for i in ids) or len(set(ids)) != len(ids):
        raise RenderVerificationError("plan job ids are missing, invalid or not unique")
    if not all(isinstance(d, dict) and isinstance(d.get("source"), dict) for d in detail):
        raise RenderVerificationError("jobs.json entries must be objects with a source record")
    if [d.get("id") if isinstance(d, dict) else None for d in detail] != ids:
        raise RenderVerificationError("jobs.json does not list the plan jobs in the same order")
    expected_inputs = {"plan.json", "jobs.json"}
    for job, info in zip(plan_jobs, detail):
        input_path = f"inputs/{job['id']}/snapshot.bin"
        if job.get("input") != input_path or info.get("input") != input_path or not _sha_ok(job.get("snapshotSha256")) \
                or files.get(input_path) != job["snapshotSha256"] \
                or info["source"].get("snapshotSha256") != job["snapshotSha256"] \
                or not isinstance(info.get("files"), dict) \
                or any(files.get(f"inputs/{job['id']}/{name}") != digest for name, digest in info["files"].items()):
            raise RenderVerificationError(f"job {job['id']}: plan, jobs.json and marker disagree")
        expected_inputs.update(f"inputs/{job['id']}/{name}" for name in info["files"])
        policy = info.get("sourcePolicy")
        if isinstance(policy, dict) and policy.get("stagedSource") is True:
            if not _sha_ok(policy.get("stagedSourceSha256")) or policy["stagedSourceSha256"] != info["files"].get("staged-source.json"):
                raise RenderVerificationError(f"job {job['id']}: source policy sidecar hash differs")
    binding = _verify_pipeline_binding(root, jobs_doc, files)
    if binding["pipelineIdentity"] is not None:
        expected_inputs.add(IDENTITY_FILE)
    if set(files) != expected_inputs:
        raise RenderVerificationError(f"{marker_name} lists other files than the plan inputs, jobs.json "
                                      f"and the declared pipeline identity")
    if set(_entries(_path(root, "inputs", directory=True))) != set(ids):
        raise RenderVerificationError("inputs/ does not contain exactly the plan job directories")
    return plan_jobs, detail, binding


def _verify_pipeline_binding(root: Path, jobs_doc: dict, files: dict) -> dict:
    """Legacy 'caller-assertion' (no identity file) or 'declared-inputs-sha256' bound to pipeline-identity.json.

    The marker already re-hashed every listed file; here the binding, the record and its key are checked.
    This validates the recorded identity only; it is not a fresh scan of the renderer inputs."""
    key, source = jobs_doc.get("pipelineKey"), jobs_doc.get("pipelineKeySource")
    if not _sha_ok(key):
        raise RenderVerificationError("jobs.json pipelineKey must be 64 lowercase hex")
    if source == "caller-assertion":
        if "pipelineIdentity" in jobs_doc or IDENTITY_FILE in files or os.path.lexists(root / IDENTITY_FILE):
            raise RenderVerificationError("a caller-assertion plan must not carry a pipeline identity")
        return {"pipelineKey": key, "pipelineKeySource": source, "pipelineIdentity": None}
    if source != "declared-inputs-sha256":
        raise RenderVerificationError(f"unknown pipelineKeySource {source!r}")
    ref = jobs_doc.get("pipelineIdentity")
    if not isinstance(ref, dict) or set(ref) != {"file", "sha256"} or ref["file"] != IDENTITY_FILE \
            or not _sha_ok(ref["sha256"]) or files.get(IDENTITY_FILE) != ref["sha256"]:
        raise RenderVerificationError("jobs.json pipelineIdentity is missing or not bound to the tracked "
                                      f"{IDENTITY_FILE}")
    record = _json(_read(root, IDENTITY_FILE, LIMIT_JSON), IDENTITY_FILE)
    try:
        record = pipeline_identity.validate_pipeline_identity(record)
    except pipeline_identity.PipelineIdentityError as exc:
        raise RenderVerificationError(f"{IDENTITY_FILE} is invalid: {exc}") from None
    if record["pipelineKey"] != key:
        raise RenderVerificationError(f"{IDENTITY_FILE} pipelineKey differs from jobs.json pipelineKey")
    return {"pipelineKey": key, "pipelineKeySource": source, "pipelineIdentity": ref["sha256"]}


def _sha_ok(value) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _verify_job(root: Path, job: dict, info: dict) -> dict:
    job_id = job["id"]
    try:
        batch = cb.parse_batch(_read(root, job["input"], LIMIT_SNAPSHOT))
    except (cs.SnapshotError, ValueError, IndexError, struct.error) as exc:
        raise RenderVerificationError(f"job {job_id}: input is not a valid UIE2 snapshot ({exc})") from None
    present_core = _chunks(batch.core) & set(batch.present_chunks)
    listed = {tuple(c) for c in info.get("presentCoreChunks", [])}
    if listed != present_core:
        raise RenderVerificationError(f"job {job_id}: jobs.json presentCoreChunks differ from the input snapshot")

    result_dir = _path(root, f"results/{job_id}", directory=True)
    receipt_raw = _read(root, f"results/{job_id}/receipt.json", LIMIT_JSON)
    receipt = _json(receipt_raw, f"results/{job_id}/receipt.json")
    if not isinstance(receipt, dict) or receipt.get("format") != "UIE_WARM_RESULT1" or receipt.get("state") != "captured" \
            or receipt.get("id") != job_id or receipt.get("snapshotSha256") != job["snapshotSha256"] \
            or not _sha_ok(receipt.get("manifestSha256")) or not _finite(receipt.get("elapsedSeconds")) \
            or receipt["elapsedSeconds"] < 0:
        raise RenderVerificationError(f"job {job_id}: receipt is not a captured result for this job "
                                      f"(state {receipt.get('state') if isinstance(receipt, dict) else None!r})")
    if set(_entries(result_dir)) != {"receipt.json", "geometry"}:
        raise RenderVerificationError(f"results/{job_id} must contain exactly receipt.json and geometry/")

    base = f"results/{job_id}/geometry"
    geometry = _path(root, base, directory=True)
    manifest_raw = _read(root, f"{base}/manifest.json", LIMIT_JSON)
    if _sha(manifest_raw) != receipt["manifestSha256"]:
        raise RenderVerificationError(f"job {job_id}: geometry/manifest.json does not match the receipt")
    m = _json(manifest_raw, f"{base}/manifest.json")
    if not isinstance(m, dict) or m.get("format") != 1 or type(m.get("format")) is not int \
            or m.get("snapshotFormat") != "UIE2" or m.get("snapshotSha256") != job["snapshotSha256"]:
        raise RenderVerificationError(f"job {job_id}: manifest is not format 1 for this UIE2 snapshot")
    if type(m.get("bakedLighting")) is not bool:
        raise RenderVerificationError(f"job {job_id}: bakedLighting must be a boolean")
    bounds = m.get("bounds")
    if not isinstance(bounds, dict) or not all(isinstance(bounds.get(k), list) and len(bounds[k]) == 3
                                               and all(_finite(v) for v in bounds[k]) for k in ("min", "max")) \
            or any(a > b for a, b in zip(bounds["min"], bounds["max"])):
        raise RenderVerificationError(f"job {job_id}: invalid bounds")
    coverage = m.get("coverage")
    counts = {}
    for key in ("renderedBlocks", "unsupportedBlocks", "emptyBlocks"):
        # emptyBlocks is optional in the manifest contract; the other two are required.
        value = (coverage.get(key, 0) if key == "emptyBlocks" else coverage.get(key)) if isinstance(coverage, dict) else None
        if not _int(value) or value < 0:
            raise RenderVerificationError(f"job {job_id}: coverage.{key} must be a non-negative integer")
        counts[key] = value
    names = coverage.get("unsupportedNames")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise RenderVerificationError(f"job {job_id}: coverage.unsupportedNames must be strings")

    chunks = m.get("chunks")
    if not isinstance(chunks, list):
        raise RenderVerificationError(f"job {job_id}: chunks is not a list")
    coords, files = set(), set()
    for entry in chunks:
        if not isinstance(entry, dict) or set(entry) != {"file", "x", "z"} or not isinstance(entry["file"], str) \
                or not _int(entry["x"]) or not _int(entry["z"]):
            raise RenderVerificationError(f"job {job_id}: invalid chunk entry {entry!r}")
        key = (entry["x"], entry["z"])
        if key in coords or entry["file"] in files:
            raise RenderVerificationError(f"job {job_id}: duplicate chunk {key} or file {entry['file']}")
        coords.add(key)
        files.add(entry["file"])
    if coords != present_core:
        raise RenderVerificationError(f"job {job_id}: rendered chunks differ from the input's present core chunks: "
                                      f"missing {sorted(present_core - coords)}, extra (halo/foreign) "
                                      f"{sorted(coords - present_core)}")
    present_files = set(_entries(geometry))
    unexpected = present_files - GEOMETRY_FIXED - files
    if unexpected:
        raise RenderVerificationError(f"job {job_id}: geometry has unexpected files {sorted(unexpected)}")

    triangles = mesh_bytes = 0
    geometry_files = {"manifest.json": _sha(manifest_raw)}
    for entry in chunks:
        raw = _read(geometry, entry["file"], LIMIT_MESH)
        mesh = _json(raw, f"{base}/{entry['file']}")
        try:
            validate_mesh(mesh)
        except PublicationError as exc:
            raise RenderVerificationError(f"job {job_id}: {entry['file']}: {exc}") from None
        triangles += _triangles(mesh)
        mesh_bytes += len(raw)
        geometry_files[entry["file"]] = _sha(raw)

    if m.get("atlas") != "atlas.png" or m.get("atlasLayout") != "atlas-layout.json" or not _sha_ok(m.get("atlasLayoutSha256")):
        raise RenderVerificationError(f"job {job_id}: manifest needs atlas.png, atlasLayout and atlasLayoutSha256")
    if _sha(_read(geometry, "atlas-layout.json", atlas_identity.MAX_LAYOUT_BYTES)) != m["atlasLayoutSha256"]:
        raise RenderVerificationError(f"job {job_id}: atlas-layout.json does not match atlasLayoutSha256")
    try:
        layout, atlas_sha = atlas_identity.load_layout(_path(geometry, "atlas.png"), _path(geometry, "atlas-layout.json"))
    except ValueError as exc:
        raise RenderVerificationError(f"job {job_id}: atlas/layout binding failed: {exc}") from None
    geometry_files["atlas.png"] = atlas_sha
    geometry_files["atlas-layout.json"] = m["atlasLayoutSha256"]
    record = {"id": job_id, "chunks": len(coords), "triangles": triangles, "meshBytes": mesh_bytes,
              "geometryFiles": geometry_files,
              "elapsedSeconds": receipt["elapsedSeconds"], "bakedLighting": m["bakedLighting"],
              "atlasSha256": atlas_sha, "layoutIdentitySha256": atlas_identity.layout_identity(layout),
              "layoutReusable": layout["reusable"], **counts, "unsupportedNames": sorted(set(names)),
              "sourcePolicy": info.get("sourcePolicy")}
    # The full core region (present and absent chunks) from the parsed UIE2 input, not from jobs.json.
    return record, _chunks(batch.core)


def verify_rendered_plan(root, *, expected_plan_sha256, marker_name="PLAN-COMPLETE.json", allow_unsupported=False) -> dict:
    if not _sha_ok(expected_plan_sha256):
        raise RenderVerificationError("expected_plan_sha256 must be 64 lowercase hex")
    if type(allow_unsupported) is not bool:
        raise RenderVerificationError("allow_unsupported must be a boolean")
    if not isinstance(marker_name, str) or "/" in marker_name or "\\" in marker_name or marker_name in (".", ".."):
        raise RenderVerificationError("marker_name must be a plain file name")
    root = Path(os.path.abspath(root))
    _check_root(root)
    plan_jobs, detail, binding = _verify_plan(root, expected_plan_sha256, marker_name)
    results = _entries(_path(root, "results", directory=True)) if os.path.lexists(root / "results") else {}
    missing = [job["id"] for job in plan_jobs if job["id"] not in results]
    if missing:
        raise RenderVerificationError(f"incomplete render: no result for {len(missing)} job(s), e.g. {missing[:3]}")
    if set(results) != {job["id"] for job in plan_jobs}:
        raise RenderVerificationError("results/ contains directories that are not plan jobs")

    per_job, owners = [], {}
    for job, info in zip(plan_jobs, detail):
        record, core = _verify_job(root, job, info)
        for chunk in sorted(core):
            if chunk in owners:
                raise RenderVerificationError(f"jobs {owners[chunk]} and {job['id']} have overlapping core chunks "
                                              f"(e.g. {chunk})")
            owners[chunk] = job["id"]
        per_job.append(record)
    for key, label in (("atlasSha256", "atlas PNG"), ("layoutIdentitySha256", "atlas layout"), ("bakedLighting", "bakedLighting")):
        if len({j[key] for j in per_job}) != 1:
            raise RenderVerificationError(f"mixed {label} across jobs")
    unsupported = sum(j["unsupportedBlocks"] for j in per_job)
    reasons = [] if unsupported == 0 or allow_unsupported else [f"{unsupported} unsupported block(s) in the render"]
    # Opt-in only (strict reports stay byte-identical): later re-verifications read the policy from here.
    policy = {"unsupportedPolicy": "allowed"} if allow_unsupported else {}
    return {**policy,
        "format": REPORT_FORMAT, "verified": not reasons, "reasons": reasons, "note": NOTE,
        "planSha256": expected_plan_sha256, "jobs": len(per_job),
        "pipelineKey": binding["pipelineKey"], "pipelineKeySource": binding["pipelineKeySource"],
        "pipelineIdentitySha256": binding["pipelineIdentity"],
        "coreChunks": sum(j["chunks"] for j in per_job), "triangles": sum(j["triangles"] for j in per_job),
        "meshBytes": sum(j["meshBytes"] for j in per_job), "elapsedSeconds": sum(j["elapsedSeconds"] for j in per_job),
        "atlasSha256": per_job[0]["atlasSha256"], "layoutIdentitySha256": per_job[0]["layoutIdentitySha256"],
        "layoutReusable": all(j["layoutReusable"] for j in per_job), "bakedLighting": per_job[0]["bakedLighting"],
        "coverage": {"renderedBlocks": sum(j["renderedBlocks"] for j in per_job), "unsupportedBlocks": unsupported,
                     "emptyBlocks": sum(j["emptyBlocks"] for j in per_job),
                     "unsupportedNames": sorted({n for j in per_job for n in j["unsupportedNames"]})},
        "perJob": per_job,
    }

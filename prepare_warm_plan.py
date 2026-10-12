"""Finite warm-capture plan (UIE_WARM_BATCH1, read by client-exporter WarmBatch.java) from prepared UIE2 batches.

prepare_warm_plan(batches, output_dir, *, world, dimension, pipeline_key,
                  max_write_bytes=512 MiB, min_free_bytes=1 GiB) -> jobs record

Each source batch is a capture_batch_file output directory (optionally converted by staged_batch.py):
    snapshot.bin, snapshot.tiles.json, provenance.json, [staged-source.json], COMPLETE (UIE2_BATCH_COMPLETE1)
It is verified completely (exact files, bounded reads, COMPLETE hashes, UIE2 parse, ore sidecar bound to the
snapshot, provenance bound to the snapshot and its presence mask, staged-source policy if present). Core
chunks of different jobs must not overlap. Sources are only read and are re-verified before publication.

Output (must not exist; built in a fresh sibling ".<name>.warm-<uuid>" directory, reserved with mkdir):
    inputs/<job-id>/snapshot.bin, snapshot.tiles.json   what GeometryCapture reads
    inputs/<job-id>/provenance.json, staged-source.json   copied for audit only
    plan.json    {"format": "UIE_WARM_BATCH1", "jobs": [{"id", "input", "snapshotSha256"}]}
    jobs.json    UIE_WARM_JOBS1: identities, areas, core chunk sets, source policy per job
    COMPLETE     written last: SHA-256 of plan.json, jobs.json and every copied file
results/ is NOT created: WarmBatch reserves results/<id> itself and refuses existing ones.

pipeline_key is a caller assertion recorded as such; nothing here derives it from mods or configuration.
A staged-source policy (observed-stable-files, atomicWorldSnapshot false) is carried per job unchanged and
never upgraded; batches without it keep create_batch's own offline-copy assumption, which is not verified
here. Nothing is launched, queued or sent anywhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import uuid
from pathlib import Path

import capture_batch as cb
import capture_snapshot as cs

PLAN_FORMAT = "UIE_WARM_BATCH1"
JOBS_FORMAT = "UIE_WARM_JOBS1"
COMPLETE_FORMAT = "UIE_WARM_PLAN_COMPLETE1"
SNAPSHOT, TILES, PROVENANCE, SIDECAR, COMPLETE = (
    "snapshot.bin", "snapshot.tiles.json", "provenance.json", "staged-source.json", "COMPLETE")
DATA_FILES = (SNAPSHOT, TILES, PROVENANCE)
LIMITS = {SNAPSHOT: 64 * 1024 * 1024, TILES: 64 * 1024 * 1024, PROVENANCE: 16 * 1024 * 1024,
          SIDECAR: 1024 * 1024, COMPLETE: 1024 * 1024}
MAX_JOBS = 256
JOB_ID = re.compile(r"[a-zA-Z0-9_-]{1,80}")
HEX64 = re.compile(r"[0-9a-f]{64}")
OBSERVED = "observed-stable-files"
BLOCK = 1 << 20
NO_POLICY_NOTE = ("No staged-source policy sidecar: create_batch documents that it expects an offline, consistent "
                  "world copy; that assumption is carried, not verified, by this plan.")


class WarmPlanError(cs.SnapshotError):
    """The plan cannot be prepared safely; no complete output exists."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode()


def _pretty(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode()


def _area(a: cs.Area) -> dict:
    return {"x": a.x, "z": a.z, "width": a.width, "depth": a.depth}


def _redirected(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _refuse_redirection(path: Path, label: str) -> None:
    """path and every existing ancestor must be real entries (lstat only; casing is never compared)."""
    for entry in (path, *path.parents):
        try:
            info = os.lstat(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WarmPlanError(f"{label}: cannot inspect {entry} ({exc})") from None
        if _redirected(info):
            raise WarmPlanError(f"{label}: {entry} is a symlink or reparse point; pass the real path")


def _listing(directory: Path) -> set:
    names = set()
    for entry in os.scandir(directory):
        info = os.lstat(entry.path)
        if _redirected(info) or not stat.S_ISREG(info.st_mode):
            raise WarmPlanError(f"{directory}/{entry.name} is not a regular file")
        names.add(entry.name)
    return names


def _open(path: Path, limit: int):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise WarmPlanError(f"{path} is missing") from None
    if _redirected(info) or not stat.S_ISREG(info.st_mode):
        raise WarmPlanError(f"{path} is not a regular file")
    stream = os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)), "rb")
    if os.fstat(stream.fileno()).st_size > limit:
        stream.close()
        raise WarmPlanError(f"{path} is larger than {limit} bytes")
    return stream


def _read(path: Path, limit: int) -> bytes:
    with _open(path, limit) as stream:
        # Read the actual size (+1 to detect growth): read(limit + 1) preallocates the whole limit on Windows.
        size = os.fstat(stream.fileno()).st_size
        raw = stream.read(size + 1)
        if len(raw) > size:
            raw += stream.read(limit + 1 - len(raw))
    if len(raw) > limit:
        raise WarmPlanError(f"{path} is larger than {limit} bytes")
    return raw


def _hash(path: Path, limit: int) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with _open(path, limit) as stream:
        while block := stream.read(BLOCK):
            size += len(block)
            if size > limit:
                raise WarmPlanError(f"{path} is larger than {limit} bytes")
            digest.update(block)
    return digest.hexdigest(), size


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WarmPlanError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _no_constant(name):
    raise WarmPlanError(f"non-finite JSON number {name}")


def _json(raw: bytes, label: str):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WarmPlanError(f"{label}: not UTF-8 JSON ({exc})") from None


def _chunks(a: cs.Area) -> set:
    return {(cx, cz) for cz in range(a.z >> 4, (a.z + a.depth) >> 4) for cx in range(a.x >> 4, (a.x + a.width) >> 4)}


def inspect_batch(directory: Path) -> dict:
    """Verify one UIE2 batch directory and return its identities, geometry and policy."""
    label = str(directory)
    complete_raw = _read(directory / COMPLETE, LIMITS[COMPLETE])
    complete = _json(complete_raw, f"{label}/COMPLETE")
    staged = isinstance(complete, dict) and "stagedSource" in complete
    if not isinstance(complete, dict) or set(complete) != {"format", "snapshotSha256", "files"} | ({"stagedSource"} if staged else set()) \
            or complete["format"] != "UIE2_BATCH_COMPLETE1" or not isinstance(complete["files"], dict) \
            or set(complete["files"]) != set(DATA_FILES) or not all(isinstance(v, str) and HEX64.fullmatch(v) for v in complete["files"].values()) \
            or complete["snapshotSha256"] != complete["files"][SNAPSHOT]:
        raise WarmPlanError(f"{label}: COMPLETE is not a UIE2_BATCH_COMPLETE1 marker")
    expected_names = {COMPLETE, *DATA_FILES} | ({SIDECAR} if staged else set())
    if _listing(directory) != expected_names:
        raise WarmPlanError(f"{label}: batch must contain exactly {sorted(expected_names)}")
    files = {COMPLETE: (_sha(complete_raw), len(complete_raw))}

    snapshot_raw = _read(directory / SNAPSHOT, LIMITS[SNAPSHOT])
    snapshot_sha = _sha(snapshot_raw)
    if snapshot_sha != complete["files"][SNAPSHOT]:
        raise WarmPlanError(f"{label}: snapshot.bin does not match COMPLETE")
    try:
        batch = cb.parse_batch(snapshot_raw)
    except (cs.SnapshotError, ValueError, IndexError, struct.error) as exc:
        raise WarmPlanError(f"{label}: snapshot.bin is not a valid UIE2 batch ({exc})") from None
    files[SNAPSHOT] = (snapshot_sha, len(snapshot_raw))
    del snapshot_raw

    tiles_raw = _read(directory / TILES, LIMITS[TILES])
    if _sha(tiles_raw) != complete["files"][TILES]:
        raise WarmPlanError(f"{label}: snapshot.tiles.json does not match COMPLETE")
    tiles = _json(tiles_raw, f"{label}/snapshot.tiles.json")
    if not isinstance(tiles, dict) or tiles.get("format") != "UIE_ORES1" or tiles.get("snapshotSha256") != snapshot_sha:
        raise WarmPlanError(f"{label}: ore sidecar is not bound to snapshot.bin")
    files[TILES] = (_sha(tiles_raw), len(tiles_raw))

    provenance_raw = _read(directory / PROVENANCE, LIMITS[PROVENANCE])
    if _sha(provenance_raw) != complete["files"][PROVENANCE]:
        raise WarmPlanError(f"{label}: provenance.json does not match COMPLETE")
    provenance = _json(provenance_raw, f"{label}/provenance.json")
    window = _chunks(batch.area)
    try:
        bound = (provenance["format"] == "UIE2_BATCH_PROVENANCE1" and provenance["snapshot"]["sha256"] == snapshot_sha
                 and provenance["input"] == _area(batch.area) and provenance["core"] == _area(batch.core)
                 and {tuple(c) for c in provenance["presentChunks"]} == set(batch.present_chunks)
                 and {tuple(c) for c in provenance["absentChunks"]} == window - set(batch.present_chunks)
                 and len(provenance["presentChunks"]) + len(provenance["absentChunks"]) == len(window))
    except (KeyError, TypeError):
        bound = False
    if not bound:
        raise WarmPlanError(f"{label}: provenance.json is not bound to snapshot.bin (identity, areas or chunk mask)")
    files[PROVENANCE] = (_sha(provenance_raw), len(provenance_raw))

    policy = {"stagedSource": False, "note": NO_POLICY_NOTE}
    if staged:
        ref = complete["stagedSource"]
        sidecar_raw = _read(directory / SIDECAR, LIMITS[SIDECAR])
        if not isinstance(ref, dict) or set(ref) != {"file", "sha256"} or ref["file"] != SIDECAR or ref["sha256"] != _sha(sidecar_raw):
            raise WarmPlanError(f"{label}: staged-source.json does not match COMPLETE.stagedSource")
        sidecar = _json(sidecar_raw, f"{label}/staged-source.json")
        try:
            valid = (sidecar["format"] == "UIE_STAGED_SOURCE1"
                     and sidecar["policy"]["acceptedConsistency"] == OBSERVED
                     and sidecar["policy"]["atomicWorldSnapshot"] is False
                     and sidecar["stagedTile"]["consistency"] == OBSERVED
                     and sidecar["stagedTile"]["atomicWorldSnapshot"] is False
                     and sidecar["batch"]["snapshotSha256"] == snapshot_sha
                     and isinstance(sidecar["warning"], str) and sidecar["warning"])
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise WarmPlanError(f"{label}: staged-source policy is not observed-stable-files/non-atomic for this snapshot")
        files[SIDECAR] = (ref["sha256"], len(sidecar_raw))
        policy = {"stagedSource": True, "acceptedConsistency": OBSERVED, "atomicWorldSnapshot": False,
                  "warning": sidecar["warning"], "stagedSourceSha256": ref["sha256"],
                  "sourceInterval": sidecar["stagedTile"].get("sourceInterval")}

    core = _chunks(batch.core)
    present_core = core & set(batch.present_chunks)
    if not present_core:
        raise WarmPlanError(f"{label}: no present core chunk")
    return {"path": directory, "files": files, "snapshotSha256": snapshot_sha, "area": batch.area, "core": batch.core,
            "coreChunks": core, "presentCoreChunks": present_core, "policy": policy}


def job_id(world: str, dimension: str, pipeline_key: str, record: dict) -> str:
    """Deterministic: SHA-256 of the plan identity and the source snapshot/sidecar identities (64 hex <= 80)."""
    identity = {"world": world, "dimension": dimension, "pipelineKey": pipeline_key,
                "snapshotSha256": record["snapshotSha256"], "tilesSha256": record["files"][TILES][0],
                "stagedSourceSha256": record["files"].get(SIDECAR, (None,))[0]}
    return _sha(_canonical(identity))


def _copy(source: Path, target: Path, expected: tuple[str, int], limit: int, reserve) -> None:
    reserve(expected[1])
    digest, size = hashlib.sha256(), 0
    with _open(source, limit) as stream, open(target, "xb") as out:
        while block := stream.read(BLOCK):
            size += len(block)
            if size > expected[1]:
                raise WarmPlanError(f"{source} grew while it was copied")
            digest.update(block)
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    if (digest.hexdigest(), size) != expected:
        raise WarmPlanError(f"{source} changed while it was copied")


def _write_new(path: Path, raw: bytes, reserve) -> None:
    reserve(len(raw))
    with open(path, "xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_dir(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


IDENTITY_FILE = "pipeline-identity.json"
IDENTITY_NOTE = ("pipelineKey is the SHA-256 of the canonical identity inside the supplied UIE_PIPELINE_IDENTITY1 record (pipeline-identity.json). "
                 "Only the record was validated here; it covers declared inputs only, and the runtime must rescan and "
                 "match its current inputs before launch.")


def prepare_warm_plan(batches, output_dir, *, world, dimension, pipeline_key,
                      max_write_bytes=512 * 1024 ** 2, min_free_bytes=1024 ** 3, pipeline_identity=None) -> dict:
    if not isinstance(batches, (list, tuple)) or not 1 <= len(batches) <= MAX_JOBS:
        raise WarmPlanError(f"batches must be a list of 1..{MAX_JOBS} directories")
    if not all(isinstance(s, str) and s.strip() for s in (world, dimension)):
        raise WarmPlanError("world and dimension are required")
    if not isinstance(pipeline_key, str) or not HEX64.fullmatch(pipeline_key):
        raise WarmPlanError("pipeline_key must be 64 lowercase hex (a caller assertion)")
    identity_bytes = None
    if pipeline_identity is not None:
        import pipeline_identity as identity_module   # only when requested: the legacy path is unchanged
        try:
            record = identity_module.validate_pipeline_identity(pipeline_identity)
        except identity_module.PipelineIdentityError as exc:
            raise WarmPlanError(f"pipeline_identity is invalid: {exc}") from None
        if record["pipelineKey"] != pipeline_key:
            raise WarmPlanError("pipeline_key does not equal pipeline_identity.pipelineKey")
        identity_bytes = _pretty(record)
    for name, value in (("max_write_bytes", max_write_bytes), ("min_free_bytes", min_free_bytes)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise WarmPlanError(f"{name} must be a non-negative integer")

    output = Path(os.path.abspath(output_dir))
    _refuse_redirection(output, "output")
    output_real = output.resolve()
    sources, seen = [], set()
    for item in batches:
        path = Path(os.path.abspath(item))
        _refuse_redirection(path, "batch")
        if not os.path.isdir(path):
            raise WarmPlanError(f"batch {path} is not a directory")
        real = path.resolve()
        if real in seen:
            raise WarmPlanError(f"batch {path} is listed twice")
        if _within(output_real, real) or _within(real, output_real):
            raise WarmPlanError(f"output overlaps batch {path}")
        seen.add(real)
        sources.append(path)
    if os.path.lexists(output):
        raise WarmPlanError(f"output {output} already exists; it is never reused or overwritten")

    records = [inspect_batch(path) for path in sources]
    records.sort(key=lambda r: (r["core"].z, r["core"].x, r["snapshotSha256"]))
    owners: dict = {}
    for record in records:
        record["id"] = job_id(world, dimension, pipeline_key, record)
        if not JOB_ID.fullmatch(record["id"]):
            raise WarmPlanError("job id does not satisfy WarmBatch's identity rule")
        for chunk in record["coreChunks"]:
            if chunk in owners:
                other = owners[chunk]
                reason = "the same batch content twice" if other["id"] == record["id"] else "overlapping core chunks"
                raise WarmPlanError(f"{other['path']} and {record['path']}: {reason} (e.g. chunk {chunk})")
            owners[chunk] = record

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.parent / f".{output.name}.warm-{uuid.uuid4().hex}"
    os.mkdir(stage)
    written = 0
    created_output = False
    moved: list[Path] = []
    success = False

    def reserve(size: int) -> None:
        nonlocal written
        if written + size > max_write_bytes:
            raise WarmPlanError("write budget exceeded")
        # Advisory check before every write, not a reservation against other writers.
        if shutil.disk_usage(stage).free < size + min_free_bytes:
            raise WarmPlanError("free disk reserve would be exhausted")
        written += size

    try:
        inventory, plan_jobs, job_docs = {}, [], []
        os.mkdir(stage / "inputs")
        for record in records:
            directory = stage / "inputs" / record["id"]
            os.mkdir(directory)
            names = [SNAPSHOT, TILES, PROVENANCE] + ([SIDECAR] if SIDECAR in record["files"] else [])
            for name in names:
                _copy(record["path"] / name, directory / name, record["files"][name], LIMITS[name], reserve)
                inventory[f"inputs/{record['id']}/{name}"] = record["files"][name][0]
            plan_jobs.append({"id": record["id"], "input": f"inputs/{record['id']}/{SNAPSHOT}",
                              "snapshotSha256": record["snapshotSha256"]})
            sort = lambda chunks: sorted([list(c) for c in chunks], key=lambda c: (c[1], c[0]))
            job_docs.append({
                "id": record["id"], "input": f"inputs/{record['id']}/{SNAPSHOT}",
                "files": {name: record["files"][name][0] for name in names},
                "source": {"path": str(record["path"]), "completeSha256": record["files"][COMPLETE][0],
                           "snapshotSha256": record["snapshotSha256"], "tilesSha256": record["files"][TILES][0],
                           "provenanceSha256": record["files"][PROVENANCE][0],
                           "stagedSourceSha256": record["files"].get(SIDECAR, (None,))[0]},
                "area": _area(record["area"]), "core": _area(record["core"]),
                "coreChunks": sort(record["coreChunks"]), "presentCoreChunks": sort(record["presentCoreChunks"]),
                "absentCoreChunks": sort(record["coreChunks"] - record["presentCoreChunks"]),
                "sourcePolicy": record["policy"],
            })
        plan_bytes = _pretty({"format": PLAN_FORMAT, "jobs": plan_jobs})
        if len(plan_bytes) > 1024 * 1024:
            raise WarmPlanError("plan.json would exceed WarmBatch's 1 MiB limit")
        jobs_doc = {
            "format": JOBS_FORMAT, "world": world, "dimension": dimension, "pipelineKey": pipeline_key,
            "pipelineKeySource": "caller-assertion",
            "pipelineKeyNote": "Asserted by the caller; not derived from mods, configuration or the capture client.",
            "plan": "plan.json", "jobs": job_docs,
        }
        if identity_bytes is not None:
            jobs_doc["pipelineKeySource"] = "declared-inputs-sha256"
            jobs_doc["pipelineKeyNote"] = IDENTITY_NOTE
            jobs_doc["pipelineIdentity"] = {"file": IDENTITY_FILE, "sha256": _sha(identity_bytes)}
            _write_new(stage / IDENTITY_FILE, identity_bytes, reserve)
            inventory[IDENTITY_FILE] = _sha(identity_bytes)
        jobs_bytes = _pretty(jobs_doc)
        _write_new(stage / "plan.json", plan_bytes, reserve)
        _write_new(stage / "jobs.json", jobs_bytes, reserve)
        inventory["plan.json"], inventory["jobs.json"] = _sha(plan_bytes), _sha(jobs_bytes)
        complete_bytes = _pretty({"format": COMPLETE_FORMAT, "files": inventory})

        # Sources and the staged copies are both re-verified before anything becomes visible.
        for record in records:
            if inspect_batch(record["path"])["files"] != record["files"]:
                raise WarmPlanError(f"batch {record['path']} changed while the plan was prepared")
        for relative, digest in inventory.items():
            if _hash(stage / relative, LIMITS.get(Path(relative).name, 1024 * 1024 * 64))[0] != digest:
                raise WarmPlanError(f"staged {relative} changed")
        _write_new(stage / "COMPLETE.pending", complete_bytes, reserve)
        for directory in [*(stage / "inputs").iterdir(), stage / "inputs", stage]:
            _sync_dir(directory)

        try:
            os.mkdir(output)  # exclusive reservation; COMPLETE inside it is what marks a plan
        except FileExistsError:
            raise WarmPlanError(f"output {output} appeared during the run; nothing was overwritten") from None
        created_output = True
        for name in ("inputs", "plan.json", "jobs.json", *([IDENTITY_FILE] if identity_bytes is not None else [])):
            if os.path.lexists(output / name):
                raise WarmPlanError(f"unexpected {name} in the reserved output")
            os.replace(stage / name, output / name)
            moved.append(output / name)
        for relative, digest in inventory.items():
            if _hash(output / relative, LIMITS.get(Path(relative).name, 1024 * 1024 * 64))[0] != digest:
                raise WarmPlanError(f"{relative} changed while it was published")
        _sync_dir(output)
        if os.path.lexists(output / COMPLETE):
            raise WarmPlanError("unexpected COMPLETE in the reserved output")
        os.replace(stage / "COMPLETE.pending", output / COMPLETE)  # commit marker, last
        moved.append(output / COMPLETE)
        _sync_dir(output)
        success = True
        return json.loads(jobs_bytes)
    finally:
        try:
            if not success:
                for path in reversed(moved):  # only what this run moved in
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                if created_output:
                    try:
                        os.rmdir(output)
                    except OSError:
                        pass  # something foreign appeared inside; leave it untouched
        finally:
            if stage.exists() and not stage.is_symlink() and stage.name.startswith(f".{output.name}.warm-"):
                shutil.rmtree(stage)
